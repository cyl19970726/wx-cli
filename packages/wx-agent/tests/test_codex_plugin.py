"""Native Session callbacks and queue uncertainty without model or desktop."""
import copy
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wx_agent.codex_plugin import CodexPlugin, GatewayInbox, PluginError, read_terminal_turn


class Inbox:
    def __init__(self):
        self.calls = []
        self.cid = "a" * 32
        self.epoch = 1
        self.delivery = dict(delivery_id=1, conversation_id=self.cid, owner="owner-a", epoch=1,
                             status="pending", turn_state=None, token=None, transport_token=None,
                             session_ref=None, thread_id=None, turn_id=None, cursor_start=1, cursor_end=2)

    def call(self, *parts):
        self.calls.append(parts)
        if parts[0] == "status":
            return dict(conversation_id=self.cid, owner="owner-a", epoch=self.epoch,
                        control="active", input="push", owner_session_ref=None)
        action = parts[1]
        if action == "status":
            return {"deliveries": [copy.deepcopy(self.delivery)]}
        if action == "show":
            return copy.deepcopy(self.delivery)
        if action == "claim":
            if self.delivery["status"] != "pending":
                return {"status": "empty"}
            self.delivery.update(status="claimed", token="PRIVATE-CLAIM-TOKEN")
            return copy.deepcopy(self.delivery)
        if action == "reserve-turn":
            self.delivery.update(turn_state="reserved", transport_token=self.delivery["token"], session_ref=parts[-1])
            return dict(delivery_id=1, turn_state="reserved", already_reserved=False)
        if action == "record-turn":
            self.delivery.update(turn_state="recorded", thread_id=parts[-3], turn_id=parts[-1])
            return {key: self.delivery[key] for key in ("delivery_id", "turn_state", "thread_id", "turn_id")}
        if action == "finish-turn":
            self.delivery["turn_state"] = parts[-1]
            return dict(delivery_id=1, turn_state=parts[-1])
        raise AssertionError(parts)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = self.root / "plugin.json"
        self.value = dict(version=1, conversation_config=str(self.root / "channel.json"),
                          wx_agent="/bin/echo", codex="/bin/echo", bindings=[dict(
                              session_id="original-session", conversation_id="a" * 32, owner="owner-a",
                              expected_epoch=1, project=str(self.root), agent_host=socket.gethostname())])
        self.save()
        self.inbox = Inbox()
        self.queue_calls = []
        self.queue_failure = False
        self.terminal_state = "unknown"

        def process(argv, **_):
            if argv[-1] == "--help":
                return SimpleNamespace(returncode=0, stdout="--resume", stderr="")
            self.queue_calls.append(argv)
            self.assertEqual(self.inbox.delivery["turn_state"], "reserved")
            self.assertNotIn("PRIVATE-CLAIM-TOKEN", str(argv))
            if self.queue_failure:
                raise subprocess.TimeoutExpired(argv, 20)
            return SimpleNamespace(returncode=0, stdout="queued", stderr="resume warning is not receipt")
        self.process = process

    def save(self):
        self.config.write_text(json.dumps(self.value)); self.config.chmod(0o600)

    def plugin(self):
        return CodexPlugin(self.config, inbox=self.inbox, turn_reader=lambda *_: self.terminal_state,
                           process_runner=self.process)

    def event(self, name="UserPromptSubmit", **extra):
        return dict(hook_event_name=name, session_id="original-session", cwd=str(self.root), turn_id="native-turn", **extra)

    def test_native_hook_reserves_exact_original_turn_and_never_starts_model(self):
        plugin = self.plugin()
        response = plugin.hook(self.event())
        self.assertEqual(self.inbox.delivery["turn_id"], "native-turn")
        self.assertEqual(self.inbox.delivery["thread_id"], "original-session")
        self.assertNotIn("PRIVATE-CLAIM-TOKEN", json.dumps(response))
        self.assertIn("events --delivery-id 1", json.dumps(response))
        self.assertEqual(self.queue_calls, [])
        self.assertEqual([row[1] for row in self.inbox.calls if row[0] == "inbox" and row[1] in ("claim", "reserve-turn", "record-turn")],
                         ["claim", "reserve-turn", "record-turn"])

    def test_duplicate_callback_does_not_record_or_notify_again(self):
        plugin = self.plugin(); plugin.hook(self.event())
        self.assertEqual(plugin.hook(self.event("PostToolUse")), {})
        self.assertEqual(sum(row[:2] == ("inbox", "record-turn") for row in self.inbox.calls), 1)

    def test_wrong_session_project_subagent_and_diagnostic_hook_have_no_effect(self):
        plugin = self.plugin()
        cases = [dict(self.event(), session_id="different-session"), dict(self.event(), cwd="/tmp"),
                 self.event(agent_id="subagent"), self.event("PreModelRequest")]
        for event in cases:
            self.assertEqual(plugin.hook(event), {})
        self.assertEqual(self.inbox.calls, [])

    def test_native_root_agent_identity_is_not_mistaken_for_a_subagent(self):
        self.plugin().hook(self.event(agent_id="original-session"))
        self.assertEqual(self.inbox.delivery["turn_id"], "native-turn")

    def test_queue_acceptance_is_not_receipt_and_is_not_replayed(self):
        plugin = self.plugin()
        result = plugin.notify_once()["notifications"][0]
        self.assertEqual(result["status"], "queued_for_host")
        self.assertFalse(result["session_receipt"])
        self.assertFalse(result["business_completed"])
        plugin.notify_once()
        self.assertEqual(len(self.queue_calls), 1)
        self.assertEqual(self.inbox.delivery["turn_state"], "reserved")

    def test_lost_queue_response_stays_reserved_until_matching_native_callback(self):
        self.queue_failure = True
        plugin = self.plugin()
        self.assertEqual(plugin.notify_once()["notifications"][0]["status"], "notification_unknown")
        plugin.notify_once()
        self.assertEqual(len(self.queue_calls), 1)
        self.assertEqual(plugin.hook(self.event(prompt="unrelated user input")), {})
        self.assertIsNone(self.inbox.delivery["turn_id"])
        queued = self.queue_calls[0]
        prompt = queued[queued.index("--message") + 1]
        response = plugin.hook(self.event(prompt=prompt))
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(self.inbox.delivery["turn_id"], "native-turn")

    def test_unknown_previous_turn_retains_reservation_without_new_claim(self):
        plugin = self.plugin(); plugin.hook(self.event())
        self.assertEqual(plugin.hook(dict(self.event(), turn_id="later-turn")), {})
        self.assertEqual(self.inbox.delivery["turn_state"], "recorded")
        self.assertFalse(any(row[:2] == ("inbox", "finish-turn") for row in self.inbox.calls))

    def test_terminal_host_evidence_does_not_complete_business_decision(self):
        plugin = self.plugin(); plugin.hook(self.event())
        self.terminal_state = "completed"
        response = plugin.hook(dict(self.event(), turn_id="later-turn"))
        self.assertEqual(self.inbox.delivery["turn_state"], "completed")
        self.assertEqual(self.inbox.delivery["status"], "claimed")
        self.assertIn("delivery-id 1", json.dumps(response))
        self.assertEqual(sum(row[:2] == ("inbox", "claim") for row in self.inbox.calls), 1)

    def test_unloaded_terminal_projection_never_finishes_recorded_transport(self):
        plugin = self.plugin(); plugin.hook(self.event())
        route = plugin.routes[0]
        projection = {"id": "native-turn", "status": "interrupted",
                      "startedAt": 1793680000, "completedAt": None, "durationMs": None,
                      "items": []}
        class Server:
            def __init__(self, *_args, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def request(self, method, params):
                self_test.assertEqual((method, params),
                    ("thread/read", {"threadId": route.session_id, "includeTurns": True}))
                return {"thread": {"id": route.session_id, "turns": [projection]}}
        self_test = self
        plugin.turn_reader = lambda selected, turn: read_terminal_turn("/bin/echo", selected, turn)
        with patch("wx_agent.session_adapter.AppServer", Server):
            self.assertEqual(plugin.reconcile(route), [self.inbox.delivery])
            self.assertEqual(self.inbox.delivery["turn_state"], "recorded")
            self.assertFalse(any(row[:2] == ("inbox", "finish-turn") for row in self.inbox.calls))
            projection.update(status="completed", completedAt=1793680002, durationMs=2000)
            self.assertEqual(plugin.reconcile(route), [])
        self.assertEqual(self.inbox.delivery["turn_state"], "completed")
        self.assertEqual(self.inbox.delivery["status"], "claimed")

    def test_terminal_reader_requires_exact_identity_and_completion_time(self):
        route = self.plugin().routes[0]
        thread = {"id": route.session_id, "turns": [{"id": "native-turn", "status": "interrupted",
                  "startedAt": 100, "completedAt": None, "durationMs": None, "items": []}]}
        class Server:
            def __init__(self, *_args, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def request(self, *_args): return {"thread": thread}
        with patch("wx_agent.session_adapter.AppServer", Server):
            self.assertEqual(read_terminal_turn("/bin/echo", route, ""), "unknown")
            self.assertEqual(read_terminal_turn("/bin/echo", route, "native-turn"), "unknown")
            thread["turns"][0]["status"] = "cancelled"
            self.assertEqual(read_terminal_turn("/bin/echo", route, "native-turn"), "unknown")
            thread["turns"][0]["status"] = "interrupted"
            thread["turns"][0]["completedAt"] = 102
            self.assertEqual(read_terminal_turn("/bin/echo", route, "native-turn"), "interrupted")
            thread["turns"][0]["completedAt"] = None
            thread["turns"][0]["status"] = "completed"
            self.assertEqual(read_terminal_turn("/bin/echo", route, "native-turn"), "unknown")
            thread["turns"][0]["completedAt"] = 102
            self.assertEqual(read_terminal_turn("/bin/echo", route, "other-turn"), "unknown")
            thread["id"] = "other-thread"
            with self.assertRaisesRegex(PluginError, "host_thread_mismatch"):
                read_terminal_turn("/bin/echo", route, "")

    def test_generation_drift_refuses_before_claim(self):
        self.inbox.epoch = 2
        with self.assertRaisesRegex(PluginError, "binding_changed"):
            self.plugin().hook(self.event())
        self.assertEqual(self.inbox.delivery["status"], "pending")

    def test_shared_routes_require_explicit_queue_opt_in_and_same_native_identity(self):
        original = json.loads(self.config.read_text())
        second = dict(original["bindings"][0], conversation_id="b" * 32)
        original["bindings"].append(second)
        self.config.write_text(json.dumps(original))
        with self.assertRaisesRegex(PluginError, "ambiguous_binding"):
            self.plugin()
        original["allow_shared_session"] = True
        self.config.write_text(json.dumps(original))
        with self.assertRaisesRegex(PluginError, "shared_session_requires_queue_transport"):
            self.plugin()
        shared = CodexPlugin(self.config, inbox=self.inbox, allow_shared_routes=True)
        self.assertEqual(len(shared.routes), 2)
        self.assertEqual(shared.routes[0].session_ref, shared.routes[1].session_ref)
        second["agent_host"] = "different-host"
        self.config.write_text(json.dumps(original))
        with self.assertRaisesRegex(PluginError, "shared_session_identity_changed"):
            CodexPlugin(self.config, inbox=self.inbox, allow_shared_routes=True)

    def test_session_start_only_advertises_pending_without_claim(self):
        event = self.event("SessionStart"); event.pop("turn_id")
        response = self.plugin().hook(event)
        self.assertIn("hookSpecificOutput", response)
        self.assertEqual(self.inbox.delivery["status"], "pending")

    def test_missing_queue_resume_capability_cannot_reserve(self):
        self.value["resume_queued"] = True; self.save()
        plugin = self.plugin()
        plugin.process_runner = lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr="")
        with self.assertRaisesRegex(PluginError, "queue_capability_unavailable"):
            plugin.notify_once()
        self.assertEqual(self.inbox.delivery["status"], "pending")

    def test_gateway_bridge_uses_pinned_session_and_original_delivery(self):
        route = self.plugin().routes[0]
        calls = []
        class Client:
            def request(inner, method, params):
                calls.append((method, params))
                if method == "transport_status":
                    return {"deliveries": []}
                if method == "binding":
                    return dict(conversation_id=route.conversation_id, owner=route.owner, epoch=1,
                                control="active", input="push", owner_session_ref=None)
                if method == "inbox_status":
                    return {"deliveries": []}
                if method == "inbox_claim":
                    return dict(status="claimed", delivery_id=4, conversation_id=route.conversation_id,
                                owner=route.owner, epoch=1, token="private-token")
                if method == "reserve_turn":
                    self.assertNotIn("session_ref", params)
                    return dict(delivery_id=4, turn_state="reserved", already_reserved=False)
                if method == "record_turn":
                    self.assertNotIn("thread_id", params)
                    return dict(delivery_id=4, turn_state="recorded")
                raise AssertionError(method)
        plugin = self.plugin(); plugin.inbox = GatewayInbox(Client(), plugin.routes)
        response = plugin.hook(self.event())
        self.assertNotIn("private-token", json.dumps(response))
        self.assertEqual([method for method, _ in calls],
                         ["transport_status", "binding", "binding", "inbox_status", "inbox_claim", "reserve_turn", "record_turn"])
        self.assertEqual(calls[-1][1], dict(conversation_id=route.conversation_id, delivery_id=4,
                                          token="private-token", turn_id="native-turn"))

    def test_gateway_bridge_refuses_delivery_identity_from_another_conversation(self):
        route = self.plugin().routes[0]
        class Client:
            def request(self, *_):
                return dict(conversation_id=route.conversation_id,
                            deliveries=[dict(delivery_id=4, conversation_id="b" * 32)])
        bridge = GatewayInbox(Client(), [route])
        with self.assertRaisesRegex(PluginError, "gateway_scope_changed"):
            bridge.call("inbox", "status", "--conversation-id", route.conversation_id)


if __name__ == "__main__":
    unittest.main()
