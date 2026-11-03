"""Authorization and Rust fencing for the one-shot gateway."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from wx_agent import gateway
from wx_agent.application_client import ResultUnknown
from wx_agent.errors import IntegrationError


CID = "a" * 32
OTHER = "b" * 32
TOKEN = "synthetic-private-token"


class FakeClient:
    def __init__(self, root):
        self.application_id = "app_a"
        self.wx_cli = Path("/bin/false")
        self.coordinator_root = root / "ui"
        self.conversations = {CID: {"state_dir": root / "state", "owner": "owner1", "expected_epoch": 3}}
        self.effects = []

    def send_text(self, cid, request_id, text):
        self.effects.append((cid, request_id, text))
        return {"request_id": "app_a." + request_id, "status": "pending"}

    def status(self, cid, request_id):
        self.effects.append(("status", cid, request_id))
        return {"request_id": "app_a." + request_id, "status": "managed_queued", "attempt_number": 0}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.client = FakeClient(self.root)
        (self.root / "state").mkdir(mode=0o700)
        marker = self.root / "state" / "managed-coordinator.json"
        marker.write_text(json.dumps({"version": 1, "root": str(self.root / "ui")}))
        marker.chmod(0o600)
        self.policy = self.root / "policy.json"
        self.grant = {"token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                      "client_config": str(self.root / "app.json"),
                      "permissions": ["send", "result", "inbox"],
                      "session_bindings": {CID: {"session_ref": "codex:host:thread1", "thread_id": "thread1"}}}
        self.save()
        self.gateway = gateway.Gateway(self.policy)
        self.calls = []
        self.binding = {"conversation_id": CID, "channel_state_dir": str(self.root / "state"),
                        "owner": "owner1", "epoch": 3, "control": "active", "input": "push",
                        "reply_policy": "authorized", "owner_session_ref": None}
        self.shown = {"delivery_id": 9, "conversation_id": CID, "channel_state_dir": str(self.root / "state"),
                      "owner": "owner1", "epoch": 3, "status": "claimed", "token": "lease1",
                      "transport_token": "lease1", "session_ref": None, "thread_id": None,
                      "cursor_start": 4, "cursor_end": 4}
        self.transport_rows = []
        def co(_client, *parts, data=None):
            self.calls.append((parts, data))
            if parts[0] == "status": return self.binding.copy()
            if parts[:2] == ("inbox", "show"): return self.shown.copy()
            if parts[:2] == ("inbox", "claim"):
                return {"conversation_id": CID, "owner": "owner1", "epoch": 3,
                        "status": "claimed", "delivery_id": 9, "token": "lease1",
                        "channel_state_dir": str(self.root / "state")}
            if parts[:2] == ("inbox", "status"):
                return {"conversation_id": CID, "owner": self.binding["owner"],
                        "epoch": self.binding["epoch"], "deliveries": self.transport_rows}
            return {"delivery_id": 9, "turn_state": "reserved"}
        self.gateway._co = co
        self.patch = mock.patch.object(gateway, "ApplicationClient", return_value=self.client)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save(self):
        self.policy.write_text(json.dumps({"version": 1, "applications": {"app_a": self.grant}}))
        self.policy.chmod(0o600)

    def envelope(self, method, params, app="app_a", token=TOKEN):
        return {"protocol_version": 1, "id": "req-1", "application_id": app,
                "token": token, "method": method, "params": params}

    def test_unknown_application_and_wrong_token_have_same_error_without_effect(self):
        a = self.gateway.handle(self.envelope("send_text", {"conversation_id": CID, "request_id": "n1", "text": "hi"}, app="absent"))
        b = self.gateway.handle(self.envelope("send_text", {"conversation_id": CID, "request_id": "n1", "text": "hi"}, token="wrong"))
        self.assertEqual(a, b)
        self.assertEqual(a["error"], {"code": "auth_failed"})
        self.assertEqual(self.client.effects, [])

    def test_cross_conversation_and_excess_params_fail_before_backend(self):
        for params in ({"conversation_id": OTHER, "request_id": "n1", "text": "hi"},
                       {"conversation_id": CID, "request_id": "n1", "text": "hi", "driver": "evil"}):
            self.assertFalse(self.gateway.handle(self.envelope("send_text", params))["ok"])
        self.assertEqual(self.client.effects, [])
        self.assertEqual(self.calls, [])

    def test_send_only_cannot_read_and_inbox_only_cannot_reply(self):
        self.grant["permissions"] = ["send"]
        self.grant["session_bindings"] = {}
        self.save()
        sent = gateway.Gateway(self.policy)
        self.assertEqual(sent.handle(self.envelope("binding", {"conversation_id": CID}))["error"]["code"], "permission_denied")
        self.grant["permissions"] = ["inbox"]
        self.grant["session_bindings"] = {CID: {"session_ref": "codex:host:thread1", "thread_id": "thread1"}}
        self.save()
        read = gateway.Gateway(self.policy)
        read._co = self.gateway._co
        result = read.handle(self.envelope("complete", {"conversation_id": CID, "delivery_id": 9,
                                                         "token": "lease1", "decision": "reply", "request_id": "n1"}))
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(self.client.effects, [])
        self.assertEqual(self.calls, [])

    def test_unknown_send_keeps_correlation_and_never_echoes_secret(self):
        with mock.patch.object(self.client, "send_text", side_effect=ResultUnknown("transport_unknown", "app_a.n1")) as send:
            result = self.gateway.handle(self.envelope("send_text", {"conversation_id": CID,
                "request_id": "n1", "text": "private body"}))
        self.assertEqual(result, {"protocol_version": 1, "id": "req-1", "ok": False,
                                  "error": {"code": "result_unknown"}})
        self.assertNotIn(TOKEN, json.dumps(result))
        send.assert_called_once()

    def test_bounded_request_and_response(self):
        with mock.patch.object(self.client, "send_text", return_value={"body": "x" * (gateway.MAX_RESPONSE + 1)}):
            result = self.gateway.handle(self.envelope("send_text", {"conversation_id": CID,
                "request_id": "n1", "text": "small"}))
        self.assertEqual(result["error"]["code"], "response_too_large")
        done = subprocess.run([sys.executable, "-m", "wx_agent.gateway", "--config",
                               str(self.policy), "request"], input=b"x" * (gateway.MAX_REQUEST + 1),
                              capture_output=True, timeout=10)
        self.assertEqual(json.loads(done.stdout)["error"]["code"], "request_too_large")

    def test_claim_uses_pinned_epoch_and_strips_internal_path(self):
        result = self.gateway.handle(self.envelope("inbox_claim", {"conversation_id": CID}))
        self.assertTrue(result["ok"], result)
        self.assertNotIn("channel_state_dir", result["result"])
        args = self.calls[-1][0]
        self.assertEqual(args[args.index("--expected-epoch") + 1], 3)
        self.binding["epoch"] = 4
        self.assertEqual(self.gateway.handle(self.envelope("inbox_claim", {"conversation_id": CID}))["error"]["code"], "binding_changed")
        self.assertEqual(sum(parts[:2] == ("inbox", "claim") for parts, _ in self.calls), 1)

    def test_delivery_scope_and_transport_identity_are_pinned(self):
        self.shown["conversation_id"] = OTHER
        result = self.gateway.handle(self.envelope("reserve_turn", {"conversation_id": CID, "delivery_id": 9, "token": "lease1"}))
        self.assertEqual(result["error"]["code"], "delivery_out_of_scope")
        self.shown["conversation_id"] = CID
        self.gateway.handle(self.envelope("reserve_turn", {"conversation_id": CID, "delivery_id": 9, "token": "lease1"}))
        args = self.calls[-1][0]
        self.assertEqual(args[args.index("--session-ref") + 1], "codex:host:thread1")
        self.shown.update(session_ref="codex:host:thread1", turn_state="reserved")
        self.gateway.handle(self.envelope("record_turn", {"conversation_id": CID, "delivery_id": 9,
                                                           "token": "lease1", "turn_id": "turn1"}))
        args = self.calls[-1][0]
        self.assertEqual(args[args.index("--thread-id") + 1], "thread1")

    def test_old_grant_can_finish_only_its_pinned_transport_after_takeover(self):
        self.binding.update(owner="successor", epoch=4, control="taken_over",
                            owner_session_ref="codex:host:new-session")
        self.shown.update(owner="successor", epoch=3, session_ref="codex:host:thread1", thread_id="thread1",
                          turn_state="recorded", turn_id="old-turn")
        self.transport_rows = [{"delivery_id": 9, "epoch": 3, "turn_state": "recorded",
                                "session_ref": "codex:host:thread1", "thread_id": "thread1",
                                "turn_id": "old-turn"}]
        old = self.gateway.handle(self.envelope("transport_status", {"conversation_id": CID}))
        self.assertTrue(old["ok"], old)
        self.assertEqual(old["result"]["deliveries"][0]["transport_token"], "lease1")
        self.assertEqual(old["result"]["deliveries"][0]["owner"], "successor")
        self.assertNotIn("draft_text", old["result"]["deliveries"][0])
        finished = self.gateway.handle(self.envelope("finish_turn", {"conversation_id": CID,
            "delivery_id": 9, "token": "lease1", "status": "completed"}))
        self.assertTrue(finished["ok"], finished)
        self.assertEqual(self.gateway.handle(self.envelope("inbox_claim", {"conversation_id": CID}))["error"]["code"],
                         "binding_changed")
        self.assertEqual(self.gateway.handle(self.envelope("inbox_show", {"conversation_id": CID,
            "delivery_id": 9}))["error"]["code"], "binding_changed")
        self.shown["epoch"] = 4
        self.transport_rows[0]["epoch"] = 4
        self.assertEqual(self.gateway.handle(self.envelope("transport_status", {"conversation_id": CID}))["result"]["deliveries"], [])
        self.assertEqual(self.gateway.handle(self.envelope("finish_turn", {"conversation_id": CID,
            "delivery_id": 9, "token": "lease1", "status": "completed"}))["error"]["code"],
                         "delivery_out_of_scope")
        self.shown["epoch"] = 3
        self.transport_rows[0]["epoch"] = 3
        self.shown["session_ref"] = "codex:host:other"
        denied = self.gateway.handle(self.envelope("finish_turn", {"conversation_id": CID,
            "delivery_id": 9, "token": "lease1", "status": "completed"}))
        self.assertEqual(denied["error"]["code"], "delivery_out_of_scope")

    def test_transport_route_drift_denies_before_delivery_read(self):
        self.binding["channel_state_dir"] = str(self.root / "different-state")
        result = self.gateway.handle(self.envelope("transport_status", {"conversation_id": CID}))
        self.assertEqual(result["error"]["code"], "binding_changed")
        self.assertFalse(any(parts[:2] == ("inbox", "status") for parts, _ in self.calls))

    def test_reply_requires_existing_namespaced_result_and_draft_uses_stdin(self):
        reply = self.gateway.handle(self.envelope("complete", {"conversation_id": CID, "delivery_id": 9,
                                                          "token": "lease1", "decision": "reply", "request_id": "n1"}))
        self.assertTrue(reply["ok"], reply)
        self.assertIn("app_a.n1", self.calls[-1][0])
        draft = self.gateway.handle(self.envelope("complete", {"conversation_id": CID, "delivery_id": 9,
                                                          "token": "lease1", "decision": "draft",
                                                          "reason": "review", "text": "private draft"}))
        self.assertTrue(draft["ok"], draft)
        self.assertNotIn("private draft", self.calls[-1][0])
        self.assertEqual(self.calls[-1][1], "private draft")

    def test_private_policy_and_provider_blocked_import(self):
        self.assertTrue(issubclass(gateway.GatewayError, IntegrationError))
        self.policy.chmod(0o644)
        with self.assertRaises(gateway.GatewayError):
            gateway.Gateway(self.policy)
        script = """import importlib.abc,sys\nclass Block(importlib.abc.MetaPathFinder):\n def find_spec(self,name,path=None,target=None):\n  if name in ('wx_agent.session_adapter','wx_agent.integrations.codex','wx_agent.codex_plugin'): raise AssertionError(name)\nsys.meta_path.insert(0,Block())\nimport wx_agent.gateway\nassert 'wx_agent.session_adapter' not in sys.modules\n"""
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
