"""Application scope and uncertain transport contracts, without a provider or GUI."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from wx_agent.application_client import ApplicationClient, ApplicationError, ResultUnknown


CID = "a" * 32


class ApplicationClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "channel"
        self.state.mkdir(mode=0o700)
        self.ui = self.root / "ui"
        self.ui.mkdir(mode=0o700)
        marker = self.state / "managed-coordinator.json"
        marker.write_text(json.dumps({"version": 1, "root": str(self.ui)}))
        marker.chmod(0o600)
        self.path = self.root / "application.json"
        self.config = {"version": 1, "application_id": "app_a",
                       "wx_agent": str(self.root / "wx-agent"),
                       "wx_cli": str(self.root / "wx-cli"),
                       "conversation_config": str(self.root / "conversations.json"),
                       "coordinator_root": str(self.ui),
                       "conversations": {CID: {"state_dir": str(self.state),
                                               "owner": "existing_owner", "expected_epoch": 4}}}
        self.write_config()
        self.calls = []
        self.epoch = 4
        self.owner = "existing_owner"
        self.control = "active"
        self.reply_policy = "authorized"
        self.binding_state = str(self.state)
        self.outbox_status = "managed_queued"
        self.outbox_attempt = 0
        self.response_id = "app_a.notice-001"
        self.response_text = "first body"
        self.outbox_publisher = "app_a"
        self.fail_code = None
        self.timeout = False

    def write_config(self):
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def transport(self, argv, data, timeout):
        self.calls.append((argv, data))
        if self.timeout and "send" in argv:
            raise subprocess.TimeoutExpired(argv, timeout)
        if "coordinator" in argv:
            value = {"conversation_id": CID, "channel_state_dir": self.binding_state,
                     "owner": self.owner, "epoch": self.epoch,
                     "control": self.control, "reply_policy": self.reply_policy}
        elif "conversations" in argv:
            if self.fail_code:
                return SimpleNamespace(returncode=1, stdout="", stderr=json.dumps(
                    {"error": {"code": self.fail_code, "message": "withheld"}}))
            value = {"request_id": self.response_id, "status": "pending", "dispatch_id": 7}
        else:
            value = {"request_id": self.response_id, "status": self.outbox_status,
                     "managed_owner": "existing_owner", "managed_epoch": 4,
                     "publisher_id": self.outbox_publisher,
                     "attempt_number": self.outbox_attempt, "text": self.response_text,
                     "result_evidence": "private evidence"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

    def client(self):
        return ApplicationClient(self.path, transport=self.transport)

    def test_send_uses_frozen_owner_epoch_and_no_wait_then_status_filters_body(self):
        client = self.client()
        sent = client.send_text(CID, "notice-001", "first body")
        self.assertEqual(sent, {"application_id": "app_a", "conversation_id": CID,
                                "request_id": "app_a.notice-001", "status": "pending"})
        argv, data = self.calls[-1]
        self.assertEqual(data, "first body")
        self.assertIn("--no-wait", argv)
        self.assertEqual(argv[argv.index("--owner") + 1], "existing_owner")
        self.assertEqual(argv[argv.index("--expected-epoch") + 1], "4")
        self.assertEqual(argv[argv.index("--publisher") + 1], "app_a")
        shown = client.status(CID, "notice-001")
        self.assertEqual(shown["request_id"], sent["request_id"])
        self.assertNotIn("text", shown)
        self.assertNotIn("result_evidence", shown)

    def test_scope_request_and_epoch_drift_refuse_before_send(self):
        client = self.client()
        for cid, rid in (("b" * 32, "notice-001"), (CID, "../foreign"), (CID, "")):
            with self.assertRaises(ApplicationError):
                client.send_text(cid, rid, "body")
        self.assertEqual(self.calls, [])
        self.epoch = 5
        with self.assertRaises(ApplicationError) as error:
            client.send_text(CID, "notice-001", "body")
        self.assertEqual(error.exception.code, "owner_epoch_drift")
        self.assertFalse(any("conversations" in argv for argv, _ in self.calls))

    def test_content_conflict_keeps_original_full_id(self):
        self.fail_code = "text_conflict"
        with self.assertRaises(ApplicationError) as error:
            self.client().send_text(CID, "notice-001", "changed body")
        self.assertEqual((error.exception.code, error.exception.request_id),
                         ("text_conflict", "app_a.notice-001"))

    def test_timeout_is_unknown_with_same_id_and_no_retry(self):
        self.timeout = True
        with self.assertRaises(ResultUnknown) as error:
            self.client().send_text(CID, "notice-001", "first body")
        self.assertEqual(error.exception.request_id, "app_a.notice-001")
        self.assertEqual(sum("conversations" in argv for argv, _ in self.calls), 1)

    def test_ambiguous_cli_failure_keeps_original_id_for_inspection(self):
        self.fail_code = "command_failed"
        with self.assertRaises(ResultUnknown) as error:
            self.client().send_text(CID, "notice-001", "first body")
        self.assertEqual(error.exception.request_id, "app_a.notice-001")
        self.assertEqual(sum("conversations" in argv for argv, _ in self.calls), 1)

    def test_status_refuses_other_request_identity_and_changed_journal_mapping(self):
        client = self.client()
        self.response_id = "app_b.notice-001"
        with self.assertRaises(ApplicationError) as error:
            client.status(CID, "notice-001")
        self.assertEqual(error.exception.code, "request_scope_drift")
        self.response_id = "app_a.notice-001"
        self.binding_state = str(self.root / "different-channel")
        with self.assertRaises(ApplicationError) as error:
            client.status(CID, "notice-001")
        self.assertEqual(error.exception.code, "coordinator_drift")

    def test_old_completed_or_unknown_result_survives_permission_and_epoch_change(self):
        client = self.client()
        self.owner = "new_owner"
        self.epoch = 5
        self.control = "taken_over"
        self.reply_policy = "observe"
        for state in ("ui_observed", "indeterminate"):
            with self.subTest(state=state):
                self.outbox_status = state
                self.outbox_attempt = 1
                shown = client.status(CID, "notice-001")
                self.assertEqual((shown["status"], shown["attempt_number"]), (state, 1))
                self.assertEqual(shown["request_id"], "app_a.notice-001")
                self.assertNotIn("text", shown)
                with self.assertRaises(ApplicationError) as error:
                    client.send_text(CID, "new-notice", "body")
                self.assertEqual(error.exception.code, "owner_epoch_drift")
        self.assertFalse(any("conversations" in argv for argv, _ in self.calls))

    def test_same_application_new_grant_reads_old_publication_and_sends_new(self):
        self.config["conversations"][CID].update(owner="successor_owner", expected_epoch=5)
        self.write_config()
        self.owner, self.epoch = "successor_owner", 5
        client = self.client()
        old = client.status(CID, "notice-001")
        self.assertEqual((old["request_id"], old["status"]),
                         ("app_a.notice-001", "managed_queued"))
        self.response_id = "app_a.notice-002"
        sent = client.send_text(CID, "notice-002", "new body")
        self.assertEqual(sent["status"], "pending")
        argv, _ = self.calls[-1]
        self.assertEqual(argv[argv.index("--owner") + 1], "successor_owner")
        self.assertEqual(argv[argv.index("--expected-epoch") + 1], "5")

    def test_status_rejects_foreign_or_legacy_null_publisher(self):
        for publisher in ("app_b", None):
            with self.subTest(publisher=publisher):
                self.outbox_publisher = publisher
                with self.assertRaises(ApplicationError) as error:
                    self.client().status(CID, "notice-001")
                self.assertEqual(error.exception.code, "request_scope_drift")

    def test_status_requires_business_key_not_returned_full_id(self):
        with self.assertRaises(ApplicationError) as error:
            self.client().status(CID, "app_a.notice-001")
        self.assertEqual(error.exception.code, "already_namespaced")
        self.assertEqual(self.calls, [])

    def test_send_response_identity_drift_is_unknown_without_second_send(self):
        self.response_id = "app_b.notice-001"
        with self.assertRaises(ResultUnknown) as error:
            self.client().send_text(CID, "notice-001", "first body")
        self.assertEqual(error.exception.request_id, "app_a.notice-001")
        self.assertEqual(sum("conversations" in argv for argv, _ in self.calls), 1)


if __name__ == "__main__":
    unittest.main()
