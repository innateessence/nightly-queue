from pathlib import Path
import sys
import tempfile
import unittest

from .codex_account_rpc import AccountRPC


class AccountRPCTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def binary(self, body, version="0.169.0"):
        path = self.home / "codex-fixture"
        prelude = f"import json,os,sys,time\nif '--version' in sys.argv:\n print('codex-cli {version}'); sys.exit(0)\n"
        path.write_text("#!" + sys.executable + "\n" + prelude + body)
        path.chmod(0o700)
        return str(path)

    def test_rpc_waits_for_initialize_before_reading_the_account(self):
        binary = self.binary(
            "first=json.loads(sys.stdin.readline())\n"
            "assert first['method']=='initialize'\n"
            "print(json.dumps({'id':first['id'],'result':{}}),flush=True)\n"
            "ready=json.loads(sys.stdin.readline())\n"
            "assert ready['method']=='initialized'\n"
            "read=json.loads(sys.stdin.readline())\n"
            "assert read['method']=='account/read' and read['params']['refreshToken'] is True\n"
            "print(json.dumps({'id':read['id'],'result':{'account':{'type':'chatgpt'}}}),flush=True)\n"
            "time.sleep(60)\n"
        )
        with AccountRPC(self.home, binary=binary) as client:
            client.request(
                1, "initialize", {"clientInfo": {"name": "test", "version": "1"}}
            )
            client.send({"method": "initialized", "params": {}})
            result = client.request(2, "account/read", {"refreshToken": True})
        self.assertEqual(result["account"]["type"], "chatgpt")
        self.assertIsNotNone(client.process.poll())

    def test_rpc_deadline_stops_the_authentication_process(self):
        binary = self.binary("time.sleep(60)\n")
        with self.assertRaises(TimeoutError):
            with AccountRPC(self.home, binary=binary, timeout=0.2) as client:
                client.request(1, "initialize", {})
        self.assertIsNotNone(client.process.poll())

    def test_rpc_output_is_bounded(self):
        binary = self.binary(
            "sys.stdout.write('x'*(2*1024**2)); sys.stdout.flush(); time.sleep(60)\n"
        )
        with self.assertRaisesRegex(ValueError, "exceeded its limit"):
            with AccountRPC(self.home, binary=binary) as client:
                client.request(1, "initialize", {})
        self.assertIsNotNone(client.process.poll())

    def test_rpc_errors_do_not_expose_provider_error_material(self):
        binary = self.binary(
            "request=json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'id':request['id'],'error':{'message':'private-provider-detail'}}),flush=True)\n"
            "time.sleep(60)\n"
        )
        with self.assertRaisesRegex(ValueError, "login needs attention") as result:
            with AccountRPC(self.home, binary=binary) as client:
                client.request(1, "initialize", {})
        self.assertNotIn("private-provider-detail", str(result.exception))

    def test_unreviewed_cli_version_cannot_manage_the_login(self):
        binary = self.binary(
            "raise AssertionError('server must not start')\n", version="0.0.0"
        )
        with self.assertRaisesRegex(ValueError, "pinned Codex CLI"):
            with AccountRPC(self.home, binary=binary):
                self.fail("An unreviewed CLI was accepted.")
