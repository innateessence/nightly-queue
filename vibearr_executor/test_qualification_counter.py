from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from .control_qualification import control_profile, pause_resume_interval
from .contracts import Conflict, canonical, digest
from .engine import EngineError
from .qualification_counter import COMMAND, PURPOSE, CounterObserver, cleanup


class FrozenIntervalTests(unittest.TestCase):
    def test_slow_unpaused_rpcs_do_not_enter_the_frozen_measurement(self):
        paused, value = False, 10
        events = []

        def pause(_operation):
            nonlocal paused, value
            if not paused:
                value += 100
            paused = True
            events.append("pause")
            return {"paused": True, "state": "running", "container_id": "original"}

        def read():
            self.assertTrue(paused)
            events.append("read")
            return {"counter": value}

        def resume(_operation):
            nonlocal paused, value
            paused = False
            value += 100
            events.append("resume")
            return {"paused": False, "container_id": "original"}

        def advancing(_docker, _container, baseline):
            self.assertFalse(paused)
            self.assertGreater(value, baseline)
            return value

        client = SimpleNamespace(pause=pause, resume=resume)
        harness = SimpleNamespace(
            client=client, state=Path("unused"), shutdown=Mock(), start=Mock()
        )
        docker = Mock()
        docker.inspect.return_value = {"State": {"Pid": 42}}
        observer = Mock()
        observer.read.side_effect = read
        with (
            patch(
                "vibearr_executor.qualification_counter.CounterObserver",
                return_value=observer,
            ),
            patch(
                "vibearr_executor.control_qualification.wait_counter",
                side_effect=advancing,
            ),
            patch("vibearr_executor.control_qualification.time.sleep"),
        ):
            result = pause_resume_interval(harness, docker, "operation", "original", 42)
        self.assertEqual(events, ["pause", "read", "pause", "read", "resume"])
        self.assertEqual(result["frozen_counter"], 110)
        self.assertEqual(result["recovered_counter"], 110)
        self.assertEqual(result["resumed_counter"], 210)

    def test_a_changed_frozen_counter_fails_before_resume_with_numeric_evidence(self):
        client = Mock()
        client.pause.return_value = {
            "paused": True,
            "state": "running",
            "container_id": "original",
        }
        harness = SimpleNamespace(
            client=client, state=Path("unused"), shutdown=Mock(), start=Mock()
        )
        observer = Mock()
        observer.read.side_effect = [{"counter": 42}, {"counter": 43}]
        with (
            patch(
                "vibearr_executor.qualification_counter.CounterObserver",
                return_value=observer,
            ),
            patch("vibearr_executor.control_qualification.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "before=42 after=43"),
        ):
            pause_resume_interval(harness, Mock(), "operation", "original", 42)
        client.resume.assert_not_called()


class CounterObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.instance, self.operation = str(uuid4()), str(uuid4())
        (self.state / "instance").write_text(self.instance)
        self.directory = self.state / "operations" / self.operation
        self.directory.mkdir(parents=True)
        self.image, self.target = "sha256:" + "a" * 64, "b" * 64
        self.record = {
            "operation_id": self.operation,
            "container_id": self.target,
            "name": "vibearr-exec-owned",
            "profile": control_profile(self.image).data(),
        }
        (self.directory / "record.json").write_bytes(canonical(self.record))
        self.original = {
            "Id": self.target,
            "State": {"Running": True, "Paused": True, "Pid": 42},
            "Config": {
                "Labels": {
                    "vibearr.executor": self.instance,
                    "vibearr.operation": self.operation,
                }
            },
        }
        self.helper = None
        self.docker = Mock()
        self.docker.inspect.side_effect = self.inspect
        self.docker.run.side_effect = self.engine_reply
        self.docker.remove.side_effect = self.remove
        self.observer = CounterObserver(self.state, self.operation, self.docker)

    def inspect(self, name):
        return self.original if name == self.target else self.helper

    def engine_reply(self, *arguments, **kwargs):
        if arguments[:2] == ("image", "inspect"):
            return canonical([{"Config": {}}])
        intent = json.loads((self.directory / "counter-reader.json").read_bytes())
        if arguments[0] == "create":
            self.assertTrue(self.original["State"]["Paused"])
            self.helper = {
                "Id": "c" * 64,
                "Image": self.image,
                "Mounts": [],
                "Config": {
                    "User": "0:0",
                    "Entrypoint": ["/usr/local/bin/python3.12"],
                    "Cmd": COMMAND,
                    "Labels": {
                        "vibearr.executor": self.instance,
                        "vibearr.operation": self.operation,
                        "vibearr.purpose": PURPOSE,
                        "vibearr.counter-recipe": digest(canonical(COMMAND)),
                    },
                },
                "HostConfig": {
                    "PidMode": "container:" + self.target,
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "CapAdd": ["SYS_PTRACE", "DAC_READ_SEARCH"],
                    "CapDrop": ["ALL"],
                    "Memory": 64 * 1024**2,
                    "MemorySwap": 64 * 1024**2,
                    "PidsLimit": 16,
                    "NanoCpus": 500000000,
                    "SecurityOpt": ["no-new-privileges:true"],
                },
                "State": {"Running": False, "ExitCode": 0},
            }
            self.assertEqual(intent["target_container_id"], self.target)
            return b"c" * 64
        self.assertEqual(arguments[:2], ("start", "-a"))
        self.assertEqual(arguments[2], self.helper["Id"])
        self.assertEqual(kwargs["limit"], 32)
        self.assertEqual(kwargs["timeout"], 10)
        return b"42"

    def remove(self, kind, name):
        self.assertEqual(kind, "container")
        self.assertEqual(name, self.helper["Id"])
        self.helper = None

    def test_observer_reads_only_in_separate_bounded_namespace_and_confirms_removal(
        self,
    ):
        result = self.observer.read()
        self.assertEqual(result["counter"], 42)
        self.assertTrue(self.original["State"]["Paused"])
        self.assertIsNone(self.helper)
        self.assertTrue(
            json.loads((self.directory / "counter-reader.json").read_bytes())["removed"]
        )
        self.assertFalse(
            any(call.args[0] == "exec" for call in self.docker.run.call_args_list)
        )

    def test_unpaused_original_never_creates_an_observer(self):
        self.original["State"]["Paused"] = False
        with self.assertRaises(EngineError):
            self.observer.read()
        self.docker.run.assert_not_called()

    def test_lost_create_response_still_cleans_exact_owned_observer(self):
        normal = self.engine_reply

        def lost(*args, **kwargs):
            result = normal(*args, **kwargs)
            if args[0] == "create":
                raise EngineError("lost response")
            return result

        self.docker.run.side_effect = lost
        with self.assertRaises(EngineError):
            self.observer.read()
        self.assertIsNone(self.helper)
        self.docker.remove.assert_called_once()

    def test_cleanup_rejects_replaced_observer_identity(self):
        self.observer.read()
        intent = json.loads((self.directory / "counter-reader.json").read_bytes())
        self.engine_reply("create")
        self.helper["Id"] = "d" * 64
        with self.assertRaises(Conflict):
            cleanup(self.docker, self.directory, self.record, self.instance)
        self.docker.remove.assert_called_once()
        self.assertNotEqual(self.helper["Id"], intent["container_id"])

    def test_cleanup_rejects_foreign_namespace_or_credentials_mount(self):
        self.observer.read()
        self.engine_reply("create")
        original = deepcopy(self.helper)
        for key, value in [
            ("HostConfig", {"PidMode": "host"}),
            ("Mounts", [{"Source": "/foreign"}]),
        ]:
            self.helper = deepcopy(original)
            if key == "HostConfig":
                self.helper[key].update(value)
            else:
                self.helper[key] = value
            with self.subTest(key=key), self.assertRaises(Conflict):
                cleanup(self.docker, self.directory, self.record, self.instance)
        self.docker.remove.assert_called_once()
