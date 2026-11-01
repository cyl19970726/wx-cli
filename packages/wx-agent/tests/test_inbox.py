"""Push routing boundaries without a model, desktop or WeChat process."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wx_agent import inbox
from wx_agent import session_adapter


class FakeServer:
    def __init__(self, *_):
        pass

    def close(self):
        pass


class FakeAdapter:
    state = "idle"
    failure = None
    deliveries = []
    turn_status_value = "completed"

    def __init__(self, session, *_):
        self.session = session

    def status(self):
        return {"state": self.state}

    def deliver(self, delivery):
        self.deliveries.append(delivery.delivery_id)
        if self.failure:
            raise self.failure
        return {"turn_id": "test-turn"}

    def close(self):
        pass

    def turn_status(self, turn_id):
        return {"turn_id": turn_id, "status": self.turn_status_value}


class PushLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        FakeAdapter.state = "idle"
        FakeAdapter.failure = None
        FakeAdapter.deliveries = []
        FakeAdapter.turn_status_value = "completed"
        self.calls = []
        self.claims = 0
        self.previous = []
        self.patches = [
            mock.patch.object(session_adapter, "AppServer", FakeServer),
            mock.patch.object(session_adapter, "SessionAdapter", FakeAdapter),
            mock.patch.object(session_adapter, "RustDeliveryEvidence", lambda *_: object()),
            mock.patch.object(inbox.dispatch, "call", side_effect=self.coordinator),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.loop = inbox.PushLoop("/bin/false", Path(self.temp.name),
                                   Path(self.temp.name) / "config.json",
                                   {"owner1": dict(provider="codex", host=socket.gethostname(),
                                                   thread_id="thread1", cwd=self.temp.name,
                                                   lock_path=str(Path(self.temp.name) / "lock"))})
        self.addCleanup(self.loop.close)

    def coordinator(self, _binary, _root, *args):
        self.calls.append(args)
        if args == ("status",):
            return {"conversations": ["conversation1"]}
        if args[:2] == ("status", "--conversation-id"):
            return {"owner": "owner1", "input": "push", "control": "active"}
        if args[:2] == ("inbox", "status"):
            return {"deliveries": self.previous}
        if args[:2] == ("inbox", "show"):
            return {"delivery_id": 42, "turn_id": "turn-42", "thread_id": "thread1",
                    "transport_token": "synthetic-transport-token", "status": "claimed"}
        if args[:2] == ("inbox", "finish-turn"):
            return {"delivery_id": 42, "turn_state": "completed"}
        if args[:2] == ("inbox", "claim"):
            self.claims += 1
            return {"status": "claimed", "delivery_id": 42,
                    "token": "synthetic-token", "epoch": 1,
                    "cursor_start": 4, "cursor_end": 4}
        raise AssertionError(args)

    def test_busy_session_leaves_inbox_unclaimed(self):
        FakeAdapter.state = "busy"
        self.loop.tick()
        self.assertEqual(self.claims, 0)
        self.assertEqual(FakeAdapter.deliveries, [])

    def test_unknown_delivery_requires_review_before_another_claim(self):
        FakeAdapter.failure = session_adapter.DeliveryUnknown("synthetic disconnect")
        self.assertEqual(self.loop.tick()["attention"],
                         {"owner1": "delivery requires inspection; no replacement turn started"})
        self.assertEqual(self.claims, 1)
        self.loop.tick()
        self.assertEqual(self.claims, 1, "unknown delivery must stop later claims for this owner")
        self.assertEqual(FakeAdapter.deliveries, ["42"])

    def test_restart_reconciles_original_recorded_turn_without_new_delivery(self):
        session = self.loop.adapters["owner1"].session
        self.previous = [{"delivery_id": 42, "turn_state": "recorded",
                          "session_ref": session.session_ref,
                          "thread_id": session.thread_id, "turn_id": "turn-42"}]
        result = self.loop.tick(allow_new=False)
        self.assertEqual(result["active_sessions"], 0)
        self.assertEqual(FakeAdapter.deliveries, [])
        self.assertEqual(self.claims, 0)
        self.assertTrue(any(args[:2] == ("inbox", "finish-turn") for args in self.calls))

    def test_restart_keeps_unmapped_reservation_for_inspection(self):
        session = self.loop.adapters["owner1"].session
        self.previous = [{"delivery_id": 42, "turn_state": "reserved",
                          "session_ref": session.session_ref,
                          "thread_id": None, "turn_id": None}]
        result = self.loop.tick(allow_new=False)
        self.assertIn("owner1", result["attention"])
        self.assertEqual(FakeAdapter.deliveries, [])
        self.assertEqual(self.claims, 0)

    def test_restart_does_not_touch_recorded_turn_while_other_transport_holds_lock(self):
        session = self.loop.adapters["owner1"].session
        self.previous = [{"delivery_id": 42, "turn_state": "recorded",
                          "session_ref": session.session_ref,
                          "thread_id": session.thread_id, "turn_id": "turn-42"}]
        FakeAdapter.state = "busy"
        result = self.loop.tick(allow_new=False)
        self.assertEqual(result["active_sessions"], 0)
        self.assertFalse(any(args[:2] in (("inbox", "show"), ("inbox", "finish-turn"))
                             for args in self.calls))
        self.assertEqual(self.claims, 0)


if __name__ == "__main__":
    unittest.main()
