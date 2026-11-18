"""Control panel over a synthetic coordinator: views, draft approval and HTTP guards."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from wx_agent import conversations as conv
from wx_agent import panel as panel_module

CID = "0123456789abcdef0123456789abcdef"


def write_private(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


class FakeCoordinator:
    def __init__(self):
        self.binding = {"conversation_id": CID, "owner": "owner1", "input": "push",
                        "reply_policy": "authorized", "control": "active", "epoch": 4,
                        "watermark": 9, "processed_cursor": 9, "last_control_event": None}
        self.delivery = {"delivery_id": 7, "conversation_id": CID, "cursor_start": 5, "cursor_end": 6,
                         "owner": "owner1", "epoch": 4, "status": "draft", "token": "lease-secret",
                         "transport_token": "transport-secret", "turn_state": "completed",
                         "draft_text": "approved words", "reason": "needs review", "condition": None,
                         "due_at": None, "request_id": None}
        self.calls = []

    def __call__(self, binary, root, *parts):
        parts = tuple(str(part) for part in parts)
        self.calls.append(parts)
        if parts == ("status",):
            return {"conversations": [CID]}
        if parts == ("status", "--conversation-id", CID):
            return dict(self.binding)
        if parts == ("dispatch", "status"):
            return {"paused_reason": None, "active_dispatch": None, "surface_epoch": 1,
                    "dispatches": [{"dispatch_id": 1, "conversation_id": CID, "request_id": "r1",
                                    "operation": "send", "status": "unknown", "result": None},
                                   {"dispatch_id": 2, "conversation_id": CID, "request_id": "r2",
                                    "operation": "send", "status": "done", "result": "ui_observed"}]}
        if parts[:2] == ("inbox", "status"):
            return {"deliveries": [dict(self.delivery, has_draft=True),
                                   {"delivery_id": 3, "status": "replied"}]}
        if parts[:2] == ("inbox", "show"):
            return dict(self.delivery)
        if parts[:2] == ("inbox", "approve-draft"):
            request = parts[parts.index("--request-id") + 1]
            self.delivery.update(status="replied", request_id=request)
            return {"delivery_id": 7, "status": "replied", "request_id": request, "already_approved": False}
        if parts[:2] == ("inbox", "resume"):
            self.delivery.update(status="pending", turn_state=None)
            return {"delivery_id": 7, "status": "pending"}
        if parts[:2] == ("dispatch", "pause"):
            return {"paused_reason": parts[-1]}
        raise AssertionError("unexpected coordinator call: %r" % (parts,))


class PanelBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        ui = base / "ui"
        ui.mkdir(mode=0o700)
        registry = base / "registry"
        registry.mkdir(mode=0o700)
        template = base / "template.json"
        write_private(template, {"ui_dir": str(ui)})
        self.settings = {"wx_cli": "/synthetic/wx-cli", "registry_dir": str(registry), "account": "account",
                         "source": str(base / "account"), "sender_template": str(template)}
        conv.save_registry(self.settings, {"version": 1, "account": "account", "source": self.settings["source"],
                                           "selected_id": None, "entries": {CID: {
                                               "conversation_id": CID, "name": "Synthetic Group", "kind": "group",
                                               "imported": False}}})
        self.fake = FakeCoordinator()
        patcher = mock.patch.object(panel_module.dispatch, "call", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.panel = panel_module.Panel(self.settings)
        self.events = [
            {"cursor": 5, "kind": "message_observed", "direction": "incoming",
             "message": {"sender": "member", "content": {"Text": "question"}}},
            {"cursor": 6, "kind": "message_observed", "direction": "incoming",
             "message": {"sender": "member", "content": {"Image": {}}}},
        ]

    def tearDown(self):
        self.temp.cleanup()


class PanelTests(PanelBase):
    def test_overview_summarizes_todos_and_attention(self):
        view = self.panel.overview()
        self.assertEqual(view["desktop"]["dispatch_counts"], {"unknown": 1, "done": 1})
        self.assertEqual([d["dispatch_id"] for d in view["desktop"]["attention"]], [1])
        item = view["conversations"][0]
        self.assertEqual(item["name"], "Synthetic Group")
        self.assertEqual(item["reply_policy"], "authorized")
        self.assertEqual([t["delivery_id"] for t in item["todos"]], [7])
        self.assertTrue(item["todos"][0]["has_draft"])
        self.assertNotIn("lease-secret", json.dumps(view))

    def test_delivery_view_hides_tokens_and_marks_replyable_sources(self):
        with mock.patch.object(conv, "selected", return_value=({}, {"state_dir": "/s"})), \
             mock.patch.object(conv, "events", return_value=self.events) as events:
            view = self.panel.delivery(CID, 7)
        events.assert_called_once_with(self.settings, {"state_dir": "/s"}, 4, 2)
        dumped = json.dumps(view)
        self.assertNotIn("lease-secret", dumped)
        self.assertNotIn("transport-secret", dumped)
        self.assertEqual(view["delivery"]["draft_text"], "approved words")
        self.assertEqual([e["replyable"] for e in view["events"]], [True, False])
        self.assertEqual(view["events"][1]["content_type"], "Image")

    def test_approve_queues_exact_draft_then_links_decision(self):
        order = []
        def reply(settings, cursor, text, cid, owner=None, expected_epoch=None, wait=True):
            order.append(("reply", cursor, text, cid, owner, expected_epoch, wait))
            return {"request_id": "reply-abc", "status": "pending", "dispatch_id": 9}
        with mock.patch.object(conv, "reply", side_effect=reply):
            result = self.panel.approve_draft(CID, 7, 4, 5)
        self.assertEqual(order, [("reply", 5, "approved words", CID, "owner1", 4, False)])
        approve = [c for c in self.fake.calls if c[:2] == ("inbox", "approve-draft")]
        self.assertEqual(approve, [("inbox", "approve-draft", "--delivery-id", "7", "--owner", "owner1",
                                    "--expected-epoch", "4", "--request-id", "reply-abc", "--approver", "panel")])
        self.assertEqual(result["status"], "replied")
        self.assertEqual(result["send_status"], "pending")
        with mock.patch.object(conv, "reply", side_effect=AssertionError("resent")):
            again = self.panel.approve_draft(CID, 7, 4, 5)
        self.assertTrue(again["already_approved"])

    def test_approve_refuses_before_any_send(self):
        cases = [
            (lambda: self.fake.binding.update(epoch=5), (CID, 7, 4, 5), "epoch_changed"),
            (lambda: self.fake.binding.update(reply_policy="draft"), (CID, 7, 4, 5), "policy"),
            (lambda: self.fake.binding.update(control="taken_over"), (CID, 7, 4, 5), "taken_over"),
            (lambda: self.fake.delivery.update(status="deferred"), (CID, 7, 4, 5), "not_draft"),
            (lambda: None, (CID, 7, 4, 9), "cursor"),
            (lambda: self.fake.delivery.update(conversation_id="f" * 32), (CID, 7, 4, 5), "delivery_drift"),
            (lambda: None, (CID, 7, True, 5), "expected_epoch"),
        ]
        for mutate, args, code in cases:
            fake = FakeCoordinator()
            self.fake.binding, self.fake.delivery = fake.binding, fake.delivery
            mutate()
            with mock.patch.object(conv, "reply", side_effect=AssertionError("sent")):
                with self.assertRaises(panel_module.PanelError) as caught:
                    self.panel.approve_draft(*args)
            self.assertEqual(caught.exception.code, code)
        self.assertFalse([c for c in self.fake.calls if c[:2] == ("inbox", "approve-draft")])

    def test_resume_only_hands_back_a_finished_undecided_turn(self):
        with self.assertRaises(panel_module.PanelError) as caught:
            self.panel.resume_delivery(CID, 7, 4)
        self.assertEqual(caught.exception.code, "not_stuck")
        self.fake.delivery.update(status="claimed", turn_state="failed")
        with self.assertRaises(panel_module.PanelError):
            self.panel.resume_delivery(CID, 7, 3)
        self.assertEqual(self.panel.resume_delivery(CID, 7, 4)["status"], "pending")
        resumed = [c for c in self.fake.calls if c[:2] == ("inbox", "resume")]
        self.assertEqual(resumed, [("inbox", "resume", "--delivery-id", "7", "--owner", "owner1",
                                    "--expected-epoch", "4")])

    def test_pause_requires_reason(self):
        with self.assertRaises(panel_module.PanelError):
            self.panel.pause("  ")
        self.assertEqual(self.panel.pause("operator check")["paused_reason"], "operator check")

    def test_approver_and_host_are_restricted(self):
        with self.assertRaises(panel_module.PanelError):
            panel_module.Panel(self.settings, approver="bad approver")
        for host in ("0.0.0.0", "192.168.1.2", "::1"):
            with self.assertRaises(panel_module.PanelError):
                panel_module.serve(self.panel, host=host, port=0)


class PanelHttpTests(PanelBase):
    def setUp(self):
        super().setUp()
        self.server, self.url = panel_module.serve(self.panel, port=0, token="synthetic-token")
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        merged = {"Host": host or "127.0.0.1:%d" % self.port}
        merged.update(headers or {})
        payload = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        connection.request(method, path, body=payload, headers=merged)
        response = connection.getresponse()
        data = response.read().decode()
        connection.close()
        return response, data

    def test_url_carries_token_only_in_fragment(self):
        self.assertTrue(self.url.endswith("/#token=synthetic-token"))
        self.assertTrue(self.url.startswith("http://127.0.0.1:"))

    def test_page_has_nonce_csp_and_no_token(self):
        response, page = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        csp = response.getheader("Content-Security-Policy")
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn('nonce="%s"' % nonce, page)
        self.assertNotIn("__NONCE__", page)
        self.assertNotIn("synthetic-token", page)
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

    def test_api_requires_token_and_expected_host(self):
        response, _ = self.request("GET", "/api/overview")
        self.assertEqual(response.status, 401)
        response, _ = self.request("GET", "/api/overview", headers={"X-Panel-Token": "wrong"})
        self.assertEqual(response.status, 401)
        response, _ = self.request("GET", "/api/overview", headers={"X-Panel-Token": "synthetic-token"},
                                   host="attacker.example:%d" % self.port)
        self.assertEqual(response.status, 403)
        response, _ = self.request("GET", "/", host="attacker.example")
        self.assertEqual(response.status, 403)
        response, data = self.request("GET", "/api/overview", headers={"X-Panel-Token": "synthetic-token"},
                                      host="localhost:%d" % self.port)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["conversations"][0]["conversation_id"], CID)

    def test_post_requires_json_and_bounded_body(self):
        token = {"X-Panel-Token": "synthetic-token"}
        response, _ = self.request("POST", "/api/pause", body=b"reason=x",
                                   headers=dict(token, **{"Content-Type": "application/x-www-form-urlencoded"}))
        self.assertEqual(response.status, 415)
        response, _ = self.request("POST", "/api/pause", body=b"x" * (panel_module.MAX_BODY + 1),
                                   headers=dict(token, **{"Content-Type": "application/json"}))
        self.assertEqual(response.status, 413)
        response, _ = self.request("POST", "/api/pause", body={"reason": "check"},
                                   headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 401)
        response, data = self.request("POST", "/api/pause", body={"reason": "check"},
                                      headers=dict(token, **{"Content-Type": "application/json"}))
        self.assertEqual(response.status, 200, data)

    def test_http_approve_maps_refusal_to_conflict(self):
        self.fake.binding["reply_policy"] = "observe"
        response, data = self.request("POST", "/api/approve-draft",
                                      body={"conversation_id": CID, "delivery_id": 7, "expected_epoch": 4, "cursor": 5},
                                      headers={"X-Panel-Token": "synthetic-token", "Content-Type": "application/json"})
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(data)["error"]["code"], "policy")


if __name__ == "__main__":
    unittest.main()
