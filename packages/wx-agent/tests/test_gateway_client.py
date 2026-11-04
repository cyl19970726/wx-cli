"""Client transport keeps credentials on stdin and uncertain results fenced."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from wx_agent.gateway_client import GatewayClient, GatewayRejected, GatewayUnknown
from wx_agent.gateway import GatewayError
from wx_agent.errors import IntegrationError


class GatewayClientTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / "client.json"
        self.config = {"version": 1, "application_id": "app_a", "token": "secret-token",
                       "command": ["/usr/bin/true", "--config", str(self.root / "policy.json"), "request"]}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def test_local_request_uses_structured_argv_and_stdin(self):
        response = {"protocol_version": 1, "id": "call-1", "ok": True,
                    "result": {"status": "pending"}}
        with mock.patch("wx_agent.gateway_client.subprocess.run", return_value=mock.Mock(
                returncode=0, stdout=json.dumps(response).encode())) as run:
            result = GatewayClient(self.path).request("send_text",
                    {"conversation_id": "a" * 32, "text": "private body"}, request_id="call-1")
        self.assertEqual(result, {"status": "pending"})
        args, kwargs = run.call_args
        self.assertEqual(args[0], self.config["command"])
        self.assertNotIn("secret-token", " ".join(args[0]))
        self.assertNotIn("private body", " ".join(args[0]))
        self.assertEqual(json.loads(kwargs["input"])["token"], "secret-token")

    def test_ssh_uses_existing_alias_and_shell_quotes_only_fixed_command(self):
        self.config["ssh_target"] = "trusted-mini"
        self.config["command"][2] = str(self.root / "policy with spaces.json")
        self.save()
        response = {"protocol_version": 1, "id": "call-2", "ok": True, "result": {}}
        with mock.patch("wx_agent.gateway_client.subprocess.run", return_value=mock.Mock(
                returncode=0, stdout=json.dumps(response).encode())) as run:
            GatewayClient(self.path).request("binding", {"conversation_id": "a" * 32}, request_id="call-2")
        args = run.call_args.args[0]
        self.assertEqual(args[:7], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                    "-T", "trusted-mini"])
        self.assertIn("'" + self.config["command"][2] + "'", args[7])
        self.assertNotIn("secret-token", args[7])

    def test_private_ssh_config_is_passed_as_local_ssh_option(self):
        ssh_path = self.root / "ssh config"
        ssh_path.write_text("Host trusted-mini\n  HostName example.invalid\n")
        ssh_path.chmod(0o600)
        self.config.update(ssh_target="trusted-mini", ssh_config=str(ssh_path))
        self.save()
        response = {"protocol_version": 1, "id": "call-ssh", "ok": True, "result": {}}
        with mock.patch("wx_agent.gateway_client.subprocess.run", return_value=mock.Mock(
                returncode=0, stdout=json.dumps(response).encode())) as run:
            GatewayClient(self.path).request("send_text",
                    {"conversation_id": "a" * 32, "text": "private body"}, request_id="call-ssh")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:9], ["ssh", "-F", str(ssh_path), "-o", "BatchMode=yes",
                                    "-o", "ConnectTimeout=5", "-T", "trusted-mini"])
        self.assertEqual(argv[9], "/usr/bin/true --config " + str(self.root / "policy.json") + " request")
        self.assertNotIn("secret-token", " ".join(argv))
        self.assertNotIn("private body", " ".join(argv))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["token"], "secret-token")
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["params"]["text"], "private body")

    def test_ssh_config_requires_target(self):
        ssh_path = self.root / "ssh.conf"
        ssh_path.write_text("Host trusted-mini\n")
        ssh_path.chmod(0o600)
        self.config["ssh_config"] = str(ssh_path)
        self.save()
        with mock.patch("wx_agent.gateway_client.subprocess.run") as run:
            with self.assertRaises(GatewayError) as caught:
                GatewayClient(self.path)
        self.assertEqual(caught.exception.code, "invalid_config")
        run.assert_not_called()

    def test_ssh_config_rejects_nonprivate_noncanonical_and_linked_files(self):
        ssh_path = self.root / "ssh.conf"
        ssh_path.write_text("Host trusted-mini\n")
        ssh_path.chmod(0o600)
        linked = self.root / "linked.conf"
        symlink = self.root / "symlink.conf"
        symlink.symlink_to(ssh_path)
        self.config["ssh_target"] = "trusted-mini"
        invalid = ["ssh.conf", str(self.root / "missing.conf"), str(self.root),
                   str(symlink), str(self.root / ".." / self.root.name / ssh_path.name), 42]
        ssh_path.chmod(0o644)
        invalid.append(str(ssh_path))
        with mock.patch("wx_agent.gateway_client.subprocess.run") as run:
            for candidate in invalid:
                with self.subTest(candidate=candidate):
                    self.config["ssh_config"] = candidate
                    self.save()
                    with self.assertRaises(GatewayError) as caught:
                        GatewayClient(self.path)
                    self.assertEqual(caught.exception.code, "invalid_config")
            ssh_path.chmod(0o600)
            os.link(ssh_path, linked)
            self.config["ssh_config"] = str(ssh_path)
            self.save()
            with self.assertRaises(GatewayError) as caught:
                GatewayClient(self.path)
            self.assertEqual(caught.exception.code, "invalid_config")
        run.assert_not_called()

    def test_timeout_and_bad_response_keep_same_correlation_without_retry(self):
        client = GatewayClient(self.path)
        with mock.patch("wx_agent.gateway_client.subprocess.run", side_effect=subprocess.TimeoutExpired("gateway", 1)) as run:
            with self.assertRaises(GatewayUnknown) as caught:
                client.request("send_text", {"conversation_id": "a" * 32}, request_id="call-3")
            self.assertEqual(caught.exception.request_id, "call-3")
            self.assertIsInstance(caught.exception, IntegrationError)
            run.assert_called_once()
        with mock.patch("wx_agent.gateway_client.subprocess.run", return_value=mock.Mock(
                returncode=0, stdout=b"not-json")) as run:
            with self.assertRaises(GatewayUnknown) as caught:
                client.request("result", {"conversation_id": "a" * 32}, request_id="call-3")
            self.assertEqual(caught.exception.request_id, "call-3")
            run.assert_called_once()

    def test_definite_error_and_private_config(self):
        response = {"protocol_version": 1, "id": "call-4", "ok": False,
                    "error": {"code": "permission_denied"}}
        with mock.patch("wx_agent.gateway_client.subprocess.run", return_value=mock.Mock(
                returncode=1, stdout=json.dumps(response).encode())):
            with self.assertRaises(GatewayRejected) as caught:
                GatewayClient(self.path).request("binding", {"conversation_id": "a" * 32}, request_id="call-4")
            self.assertEqual(caught.exception.code, "permission_denied")
        self.path.chmod(0o644)
        with self.assertRaises(Exception):
            GatewayClient(self.path)

    def test_oversized_client_request_refuses_before_process_start(self):
        with mock.patch("wx_agent.gateway_client.subprocess.run") as run:
            with self.assertRaises(GatewayRejected) as caught:
                GatewayClient(self.path).request("send_text", {"text": "x" * (128 * 1024)},
                                                 request_id="large-1")
        self.assertEqual((caught.exception.code, caught.exception.request_id),
                         ("request_too_large", "large-1"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
