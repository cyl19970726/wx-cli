import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wx_agent.session_adapter import (
    Delivery, DeliveryUnknown, ManagedSession, RustDeliveryEvidence, SessionAdapter,
    SessionBusy, SessionError,
)


class FakeServer:
    def __init__(self, state="idle", fail_start=False):
        self.state = state
        self.fail_start = fail_start
        self.calls = []

    def close(self):
        self.calls.append(("close", {}))

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"id": "thread-1", "status": {"type": self.state},
                               "turns": [{"id": "turn-1", "status": "completed"}]}}
        if method == "thread/resume":
            if self.state=="notLoaded":
                self.state = "idle"
            return {"thread": {"id": "thread-1", "status": {"type": self.state}}}
        if method == "turn/start":
            if self.fail_start:
                raise DeliveryUnknown("disconnected")
            self.state = "active"
            return {"turn": {"id": "turn-1"}}
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)


class Evidence:
    def __init__(self):
        self.reserved = set()
        self.turns = {}

    def reserve(self, delivery, session):
        delivery_id = delivery.delivery_id
        if delivery_id in self.reserved:
            return False
        self.reserved.add(delivery_id)
        return True

    def record_turn(self, delivery, session, turn_id):
        self.turns[delivery.delivery_id] = (session.thread_id, turn_id)


class SessionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = ManagedSession("codex", "test-host", "thread-1", self.temp.name,
                                      str(Path(self.temp.name) / "owner.lock"))
        self.server = FakeServer()
        self.evidence = Evidence()
        self.adapter = SessionAdapter(self.session, self.server, self.evidence,
                                      local_host="test-host")
        self.addCleanup(self.adapter.close)
        self.delivery = Delivery("delivery-1", "conversation-1", 1, 3,
                                 "owner-1", 1, "claim-token-1", ("attachment-1",))

    def test_persistent_thread_created_only_by_explicit_provisioning(self):
        ref = SessionAdapter.create_managed(self.server, "test-host", self.temp.name,
                                            self.session.lock_path, local_host="test-host")
        self.assertEqual(ref.thread_id, "thread-1")
        self.assertEqual(self.server.calls[0], ("thread/start", {"cwd": self.temp.name,
                                                               "ephemeral": False}))

    def test_provisioning_on_wrong_host_does_not_create_thread(self):
        with self.assertRaises(SessionError):
            SessionAdapter.create_managed(self.server, "test-host", self.temp.name,
                                          self.session.lock_path, local_host="other-host")
        self.assertEqual(self.server.calls, [])

    def test_explicit_state_write_roots_are_scoped_and_invalid_root_does_not_provision(self):
        root=Path(self.temp.name).resolve()
        state=root/"private-state";state.mkdir(mode=0o700)
        ref=ManagedSession("codex","test-host","thread-1",str(root),self.session.lock_path,
                           writable_roots=(str(state),))
        adapter=SessionAdapter(ref,self.server,self.evidence,local_host="test-host")
        self.addCleanup(adapter.close)
        adapter.deliver(self.delivery)
        params=self.server.calls[-1][1]
        self.assertEqual(params["sandboxPolicy"]["writableRoots"],[str(state)])
        self.assertEqual(params["sandboxPolicy"]["type"],"workspaceWrite")
        self.assertFalse(params["sandboxPolicy"]["networkAccess"])
        count=len(self.server.calls)
        with self.assertRaises(SessionError):
            SessionAdapter.create_managed(self.server,"test-host",str(root),str(root/"invalid.lock"),
                                          local_host="test-host",writable_roots=(str(root/"absent"),))
        self.assertEqual(len(self.server.calls),count)

    def test_delivers_once_with_evidence_ids(self):
        receipt = self.adapter.deliver(self.delivery)
        self.assertEqual(receipt["turn_id"], "turn-1")
        self.assertEqual(self.evidence.turns["delivery-1"], ("thread-1", "turn-1"))
        self.assertEqual(self.server.calls[-1][1]["clientUserMessageId"], "delivery-1")
        self.assertIn('"conversation_id": "conversation-1"',
                      self.server.calls[-1][1]["input"][0]["text"])
        self.server.state = "idle"
        self.adapter.turn_status("turn-1")
        with self.assertRaises(DeliveryUnknown):
            self.adapter.deliver(self.delivery)
        self.assertEqual(len([c for c in self.server.calls if c[0] == "turn/start"]), 1)

    def test_rust_integer_delivery_id_is_canonicalized(self):
        delivery = Delivery(42, "conversation-1", 1, 1, "owner-1", 1, "claim-token-1")
        self.adapter.deliver(delivery)
        self.assertEqual(self.server.calls[-1][1]["clientUserMessageId"], "42")
        self.assertEqual(self.evidence.turns["42"], ("thread-1", "turn-1"))

    def test_busy_does_not_reserve_or_start(self):
        self.server.state = "active"
        with self.assertRaises(SessionBusy):
            self.adapter.deliver(self.delivery)
        self.assertFalse(self.evidence.reserved)
        self.assertFalse(any(c[0] == "turn/start" for c in self.server.calls))

    def test_explicit_interrupt_does_not_release_active_turn_without_terminal_evidence(self):
        self.adapter.deliver(self.delivery)
        with self.assertRaises(SessionError):
            self.adapter.interrupt("another-turn")
        self.assertEqual(self.adapter.interrupt("turn-1")["state"],"stop_requested")
        with self.assertRaises(SessionBusy):
            self.adapter.deliver(self.delivery)
        self.assertEqual(sum(method=="turn/start" for method,_ in self.server.calls),1)
        self.adapter.turn_status("turn-1")
        self.assertIsNone(self.adapter._held_fd)

    def test_lock_held_by_other_worker_does_not_start(self):
        descriptor = os.open(self.session.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(SessionBusy):
                self.adapter.deliver(self.delivery)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.assertFalse(self.evidence.reserved)

    def test_second_process_refused_until_first_turn_terminal(self):
        self.adapter.deliver(self.delivery)
        second = SessionAdapter(self.session, FakeServer("notLoaded"), Evidence(),
                                local_host="test-host")
        self.addCleanup(second.close)
        with self.assertRaises(SessionBusy):
            second.deliver(Delivery("delivery-2", "conversation-1", 4, 4,
                                    "owner-1", 1, "claim-token-2"))
        self.assertEqual(second.server.calls, [])
        self.assertEqual(second.status()["state"], "busy")
        self.assertEqual(second.server.calls, [])
        self.assertEqual(self.adapter.turn_status("turn-1")["status"], "completed")
        self.assertEqual(second.status()["state"], "idle")

    def test_unknown_result_is_never_retried(self):
        self.server.fail_start = True
        with self.assertRaises(DeliveryUnknown):
            self.adapter.deliver(self.delivery)
        self.server.fail_start = False
        self.adapter.close()
        another = SessionAdapter(self.session, self.server, self.evidence,
                                 local_host="test-host")
        self.addCleanup(another.close)
        with self.assertRaises(DeliveryUnknown):
            another.deliver(self.delivery)
        self.assertEqual(len([c for c in self.server.calls if c[0] == "turn/start"]), 1)

    def test_resume_same_thread_and_query_turn(self):
        self.server.state = "notLoaded"
        self.assertEqual(self.adapter.status()["state"], "idle")
        self.assertEqual(self.server.calls[0],("thread/resume",{"threadId":"thread-1","cwd":self.temp.name,"excludeTurns":True}))
        self.assertEqual(self.adapter.turn_status("turn-1")["status"], "completed")

    def test_cold_connection_resumes_before_reading_a_managed_thread(self):
        class Cold(FakeServer):
            loaded=False
            def request(inner,method,params):
                if method=="thread/read" and not inner.loaded:
                    raise SessionError("thread not loaded")
                if method=="thread/resume":
                    inner.loaded=True
                return super(Cold,inner).request(method,params)
        server=Cold()
        adapter=SessionAdapter(self.session,server,self.evidence,local_host="test-host")
        self.addCleanup(adapter.close)
        self.assertEqual(adapter.status()["state"],"idle")
        self.assertEqual(adapter.turn_status("turn-1")["status"],"completed")
        self.assertFalse(any(method in ("thread/start","turn/start") for method,_ in server.calls))

    def test_desktop_owned_is_unsupported(self):
        with self.assertRaises(SessionError):
            ManagedSession("codex", "host", "thread-1", self.temp.name,
                           self.session.lock_path, "desktop-held")

    def test_wrong_host_refuses_before_reservation(self):
        other = SessionAdapter(self.session, self.server, self.evidence,
                               local_host="other-host")
        with self.assertRaises(SessionError):
            other.deliver(self.delivery)
        self.assertFalse(self.evidence.reserved)

    def test_trusted_command_hint_excludes_claim_token(self):
        delivery = Delivery(42, "conversation-1", 1, 1, "owner-1", 1,
                            "private-token", command_hint="wx-agent inbox show --delivery-id 42")
        self.adapter.deliver(delivery)
        prompt = self.server.calls[-1][1]["input"][0]["text"]
        self.assertIn("wx-agent inbox show", prompt)
        self.assertNotIn("private-token", prompt)

    def test_rust_evidence_command_contract(self):
        evidence = RustDeliveryEvidence("/bin/wx-cli", "/private/state")
        delivery = Delivery(42, "conversation-1", 1, 1, "owner-1", 1, "private-token")
        responses = iter((
            '{"delivery_id":42,"turn_state":"reserved","session_ref":"codex:test-host:thread-1","already_reserved":false}',
            '{"delivery_id":42,"turn_state":"recorded","thread_id":"thread-1","turn_id":"turn-1"}',
        ))
        with patch("wx_agent.channel_agent.command", side_effect=lambda argv, **kw: next(responses)) as run:
            self.assertTrue(evidence.reserve(delivery, self.session))
            evidence.record_turn(delivery, self.session, "turn-1")
        self.assertEqual(run.call_args_list[0].args[0], [
            "/bin/wx-cli", "coordinator", "--state-dir", "/private/state", "inbox",
            "reserve-turn", "--delivery-id", "42", "--token", "private-token",
            "--session-ref", "codex:test-host:thread-1"])
        self.assertEqual(run.call_args_list[1].args[0][-4:],
                         ["--thread-id", "thread-1", "--turn-id", "turn-1"])

    def test_rust_existing_reservation_does_not_start_turn(self):
        evidence = RustDeliveryEvidence("/bin/wx-cli", "/private/state")
        delivery = Delivery(42, "conversation-1", 1, 1, "owner-1", 1, "private-token")
        response = ('{"delivery_id":42,"turn_state":"reserved",'
                    '"session_ref":"codex:test-host:thread-1","already_reserved":true}')
        with patch("wx_agent.channel_agent.command", return_value=response):
            adapter = SessionAdapter(self.session, self.server, evidence,
                                     local_host="test-host")
            self.addCleanup(adapter.close)
            with self.assertRaises(DeliveryUnknown):
                adapter.deliver(delivery)
        self.assertFalse(any(c[0] == "turn/start" for c in self.server.calls))


if __name__ == "__main__":
    unittest.main()
