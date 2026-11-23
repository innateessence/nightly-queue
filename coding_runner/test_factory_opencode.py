import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vibearr_executor.artifacts import pack, result_manifest

from vibearr_executor.native_task import command
from vibearr_runner.native_cli import NativeCLIAdapter, NativeEvents
from vibearr_runner.native_sessions import NativeSessionError


def stream(*events):
    return b"\n".join(json.dumps(event).encode() for event in events) + b"\n"


def event(kind, part=None, session="ses_factory"):
    return {"type": kind, "sessionID": session, "part": part or {}}


class OpenCodeFactoryEventTests(unittest.TestCase):
    def test_complete_role_text_is_separate_from_display_preview(self):
        payload = json.dumps({"summary": "a" * 6000})
        parser = NativeEvents("opencode")
        parser.feed(
            stream(
                event("step_start"),
                event("text", {"text": payload}),
                event("step_finish", {"reason": "stop", "tokens": {"output": 12}}),
            ),
            final=True,
        )
        self.assertTrue(parser.completed)
        self.assertEqual(parser.result_text, payload)
        self.assertEqual(len(parser.events[2]["text"]), 4000)

    def test_only_final_step_text_is_the_role_result(self):
        parser = NativeEvents("opencode")
        parser.feed(
            stream(
                event("step_start"),
                event("text", {"text": "Inspecting source."}),
                event("step_finish", {"reason": "tool-calls"}),
                event("step_start"),
                event("text", {"text": '{"verdict":"pass"}'}),
                event("step_finish", {"reason": "stop"}),
            )
        )
        self.assertEqual(parser.result_text, '{"verdict":"pass"}')

    def test_tokens_include_all_steps_and_cache_counts(self):
        parser = NativeEvents("opencode")
        parser.feed(
            stream(
                event("step_finish", {"reason": "tool-calls", "tokens": {"input": 4}}),
                event(
                    "step_finish",
                    {"reason": "stop", "tokens": {"input": 8, "cache": {"read": 2}}},
                ),
            )
        )
        self.assertEqual(parser.usage, {"input": 12, "cache_read": 2})

    def test_duplicate_terminal_completion_is_rejected(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(stream(event("step_finish", {"reason": "stop"})) * 2)

    def test_duplicate_intermediate_step_is_rejected(self):
        parser = NativeEvents("opencode")
        part = {"id": "prt_once", "reason": "tool-calls", "tokens": {"input": 1}}
        with self.assertRaises(NativeSessionError):
            parser.feed(stream(event("step_finish", part)) * 2)

    def test_error_cannot_be_overwritten_by_a_stop(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(
                stream(event("error"), event("step_finish", {"reason": "stop"}))
            )
        self.assertTrue(parser.failed)
        self.assertFalse(parser.completed)

    def test_length_limited_finish_is_not_success(self):
        parser = NativeEvents("opencode")
        parser.feed(stream(event("step_finish", {"reason": "length"})))
        self.assertTrue(parser.failed)
        self.assertFalse(parser.completed)

    def test_event_without_session_identity_is_rejected(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(b'{"type":"step_finish","part":{"reason":"stop"}}\n')

    def test_changed_session_identity_is_rejected(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(
                stream(event("step_start"), event("step_start", session="ses_other"))
            )

    def test_nested_part_session_must_match(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(stream(event("text", {"text": "ok", "sessionID": "ses_other"})))

    def test_boolean_token_count_is_rejected(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(
                stream(
                    event("step_finish", {"reason": "stop", "tokens": {"input": True}})
                )
            )

    def test_nested_cache_token_count_is_validated(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(
                stream(
                    event(
                        "step_finish",
                        {"reason": "stop", "tokens": {"cache": {"read": -1}}},
                    )
                )
            )

    def test_tokens_must_be_an_object(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(stream(event("step_finish", {"reason": "stop", "tokens": []})))

    def test_duplicate_json_fields_are_rejected(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(b'{"type":"error","type":"step_finish","sessionID":"ses_x"}\n')

    def test_nonfinite_json_is_rejected(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(
                b'{"type":"step_finish","sessionID":"ses_x","part":{"reason":"stop","tokens":{"input":NaN}}}\n'
            )

    def test_truncated_final_json_is_rejected(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(b'{"type":"step_finish"', final=True)

    def test_events_are_bounded_even_when_payloads_are_small(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(stream(event("step_start")) * 2048)

    def test_text_after_completion_is_rejected(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(
                stream(
                    event("step_finish", {"reason": "stop"}),
                    event("text", {"text": "late"}),
                )
            )


class OpenCodeFactoryOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.source = pack({"source.txt": (0o644, b"original")})
        self.logs = stream(event("step_finish", {"reason": "stop"}))
        self.parser = NativeEvents("opencode")
        self.parser.feed(self.logs, final=True)
        self.session = SimpleNamespace(
            manifest={
                "source_digest": hashlib.sha256(self.source).hexdigest(),
                "operation_id": "operation",
            }
        )
        self.receipt = {
            "exit_code": 0,
            "timed_out": False,
            "canceled": False,
            "log_truncated": False,
            "log_digest": hashlib.sha256(self.logs).hexdigest(),
        }
        self.observed = {
            "state": "exited",
            "descendants_terminated": True,
            "result": self.receipt,
        }
        self.adapter = NativeCLIAdapter(SimpleNamespace(), "opencode")
        self.adapter.inspect = lambda session: self.observed
        self.adapter.read_events = lambda session: (self.parser, self.logs)
        self.adapter.client.export = lambda operation, source: (
            result_manifest(source, source),
            source,
        )

    def test_completed_output_with_nonzero_exit_is_not_success(self):
        self.receipt["exit_code"] = 1
        self.assertFalse(self.adapter.outcome(self.session, self.source)["successful"])

    def test_completed_output_with_remaining_descendants_stays_fenced(self):
        self.observed["descendants_terminated"] = False
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(self.session, self.source)

    def test_completed_output_with_timeout_is_not_success(self):
        self.receipt["timed_out"] = True
        self.assertFalse(self.adapter.outcome(self.session, self.source)["successful"])

    def test_completed_output_with_cancellation_is_not_success(self):
        self.receipt["canceled"] = True
        self.assertFalse(self.adapter.outcome(self.session, self.source)["successful"])

    def test_completed_output_requires_matching_protected_log(self):
        self.receipt["log_digest"] = "0" * 64
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(self.session, self.source)


class OpenCodeFactoryConfigurationTests(unittest.TestCase):
    def prepare(self, directory, role=None):
        if role:
            os.environ["VIBEARR_FACTORY_ROLE"] = role
        arguments = command(
            "opencode", "qwen3-coder:30b", Path(directory), factory_role=role
        )
        config = json.loads((Path(directory) / "opencode.json").read_text())
        return arguments, config

    def test_all_read_only_roles_use_default_deny_v1_permissions(self):
        roles = (
            "intake",
            "specification",
            "reviewer-code",
            "reviewer-security",
            "reviewer-ux",
            "reviewer-ui",
            "decision",
        )
        for role in roles:
            with (
                self.subTest(role=role),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {}, clear=True),
            ):
                _, config = self.prepare(directory, role)
                self.assertEqual(
                    config["permission"],
                    {
                        "*": "deny",
                        "read": "allow",
                        "glob": "allow",
                        "grep": "allow",
                        "list": "allow",
                    },
                )
                self.assertNotIn("permissions", config)

    def test_builder_keeps_task_scoped_tools(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            _, config = self.prepare(directory, "build")
        self.assertEqual(config["permission"], "allow")

    def test_compact_relay_uses_only_the_registered_grant(self):
        environment = {
            "VIBEARR_OLLAMA_URL": "http://172.20.0.2:3130/v1",
            "VIBEARR_EGRESS_GRANT": "g" * 64,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, environment, clear=True),
        ):
            _, config = self.prepare(directory)
        options = config["provider"]["ollama"]["options"]
        self.assertEqual(
            options,
            {
                "baseURL": environment["VIBEARR_OLLAMA_URL"],
                "headers": {"X-Vibearr-Grant": "g" * 64},
            },
        )

    def test_compact_relay_without_a_grant_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"VIBEARR_OLLAMA_URL": "http://172.20.0.2:3130/v1"},
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            self.prepare(directory)

    def test_relay_cannot_be_an_arbitrary_provider_or_metadata_url(self):
        for relay in (
            "https://api.openai.com/v1",
            "http://169.254.169.254:3130/v1",
            "http://172.20.0.2:8791/v1",
            "http://172.20.0.2:3130/control",
        ):
            with (
                self.subTest(relay=relay),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(
                    os.environ,
                    {"VIBEARR_OLLAMA_URL": relay, "VIBEARR_EGRESS_GRANT": "g" * 64},
                    clear=True,
                ),
                self.assertRaises(ValueError),
            ):
                self.prepare(directory)

    def test_context_cannot_choose_a_different_role(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"VIBEARR_FACTORY_ROLE": "reviewer-code"}, clear=True
            ),
            self.assertRaises(ValueError),
        ):
            command("opencode", "qwen3", Path(directory), factory_role="build")

    def test_unknown_registered_role_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"VIBEARR_FACTORY_ROLE": "admin"}, clear=True),
            self.assertRaises(ValueError),
        ):
            command("opencode", "qwen3", Path(directory), factory_role="admin")

    def test_inherited_opencode_configuration_is_removed(self):
        inherited = {
            "OPENCODE_PERMISSION": '"allow"',
            "OPENCODE_DB": "/control/state",
            "OPENCODE_SERVER_PASSWORD": "other",
            "OPENCODE_CONFIG": "/work/untrusted.json",
            "OPENCODE_EXPERIMENTAL": "true",
            "OPENCODE_MODELS_URL": "http://metadata.invalid",
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, inherited, clear=True),
        ):
            self.prepare(directory, "reviewer-code")
            for name in inherited:
                if name != "OPENCODE_CONFIG":
                    self.assertNotIn(name, os.environ)
            self.assertEqual(os.environ["OPENCODE_DISABLE_PROJECT_CONFIG"], "true")
            self.assertTrue(os.environ["OPENCODE_CONFIG"].startswith(directory))
            self.assertTrue(os.environ["XDG_STATE_HOME"].startswith(directory))
            self.assertTrue(os.environ["XDG_RUNTIME_DIR"].startswith(directory))

    def test_cli_has_no_shared_server_attach_or_resume_arguments(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            arguments, _ = self.prepare(directory)
        self.assertEqual(arguments[1:5], ["run", "--pure", "--format", "json"])
        self.assertNotIn("--attach", arguments)
        self.assertNotIn("--session", arguments)
        self.assertNotIn("--continue", arguments)

    def test_symlinked_config_is_never_followed(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(directory)
            target = root / "private"
            target.write_text("unchanged")
            (root / "opencode.json").symlink_to(target)
            with self.assertRaises(OSError):
                command("opencode", "qwen3", root)
            self.assertEqual(target.read_text(), "unchanged")

    def test_config_is_private_and_only_selected_ollama_is_enabled(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            _, config = self.prepare(directory)
            self.assertEqual(
                (Path(directory) / "opencode.json").stat().st_mode & 0o777, 0o600
            )
        self.assertEqual(config["enabled_providers"], ["ollama"])
        self.assertEqual(
            config["provider"]["ollama"]["options"]["baseURL"],
            "http://127.0.0.1:3130/v1",
        )
        self.assertEqual(config["plugin"], [])
        self.assertEqual(config["mcp"], {})

    def test_model_cannot_supply_provider_flags_or_urls(self):
        for model in (
            "http://metadata.invalid",
            "qwen3 --attach=elsewhere",
            "ollama/model?other",
        ):
            with (
                self.subTest(model=model),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {}, clear=True),
                self.assertRaises(ValueError),
            ):
                command("opencode", model, Path(directory))


class OpenCodeFactoryConnectionTests(unittest.TestCase):
    def test_connection_cannot_override_the_selected_model(self):
        adapter = NativeCLIAdapter(None, "opencode")
        with self.assertRaises(NativeSessionError):
            adapter.connection(
                {
                    "credential": {
                        "provider": "opencode",
                        "kind": "connection",
                        "id": 1,
                    },
                    "material": {
                        "secret": '{"provider":"ollama","base_url":"http://192.168.10.20:11434","model":"other"}'
                    },
                }
            )

    def test_duplicate_provider_fields_are_rejected(self):
        adapter = NativeCLIAdapter(None, "opencode")
        with self.assertRaises(NativeSessionError):
            adapter.connection(
                {
                    "credential": {
                        "provider": "opencode",
                        "kind": "connection",
                        "id": 1,
                    },
                    "material": {
                        "secret": '{"provider":"openai","provider":"ollama","base_url":"http://192.168.10.20:11434"}'
                    },
                }
            )

    def test_selected_provider_cannot_be_replaced(self):
        adapter = NativeCLIAdapter(None, "opencode")
        with self.assertRaises(NativeSessionError):
            adapter.connection({"credential": {"provider": "claude", "kind": "login"}})

    def test_selected_endpoint_cannot_be_an_internal_service(self):
        adapter = NativeCLIAdapter(None, "opencode")
        with self.assertRaises(ValueError):
            adapter.connection(
                {
                    "credential": {
                        "provider": "opencode",
                        "kind": "connection",
                        "id": 1,
                    },
                    "material": {
                        "secret": '{"provider":"ollama","base_url":"http://postgres:5432"}'
                    },
                }
            )

    def test_external_connection_remains_operator_selected(self):
        adapter = NativeCLIAdapter(None, "opencode")
        connection = adapter.connection(
            {
                "credential": {"provider": "opencode", "kind": "connection", "id": 9},
                "material": {
                    "secret": '{"provider":"ollama","base_url":"http://192.168.10.20:11434/api","api_key":""}'
                },
            }
        )
        self.assertEqual(
            connection,
            {
                "provider": "opencode",
                "endpoint": "http://192.168.10.20:11434",
                "credential_id": 9,
            },
        )
