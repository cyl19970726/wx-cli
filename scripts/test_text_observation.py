"""No-send compiled text reconciliation using synthetic source/attempt evidence."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone

from scripts.test_local_channel import fixture, GROUP

BINARY = Path(__file__).resolve().parents[1] / "target/debug/wx-cli"


class TextObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise unittest.SkipTest("compiled target/debug/wx-cli is required")

    def call(self, section, state, *args, okay=True, data=None):
        result = subprocess.run([str(BINARY), section, "--state-dir", str(state), *map(str, args)],
                                input=data, text=True, capture_output=True, timeout=15)
        if okay:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def write(self, path, value):
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        path.chmod(0o600)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wx-text-observation-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state, self.source, self.ui = [self.root / name for name in ("state", "source", "ui")]
        fixture(self.source)
        self.ui.mkdir(mode=0o700)
        self.binding = self.call("channel", self.state, "bind", "--account", "synthetic",
                                 "--talker", GROUP, "--session", "synthetic-session", "--source", self.source)
        with sqlite3.connect(self.state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self',encrypted_snapshot=1")
        self.call("coordinator", self.ui, "bind", "--conversation-id", "synthetic-conversation",
                  "--channel-state-dir", self.state, "--owner", "synthetic-owner",
                  "--input", "pull", "--reply-policy", "authorized")
        self.rid = "original-text"
        self.text = "Please check these offers:\n\n1. https://example.test/one\n\nSetup:\nNetwork and power ✅"
        self.call("outbox", self.state, "queue", "--request-id", self.rid,
                  "--channel-id", self.binding["channel_id"], "--session-id", "synthetic-session",
                  "--owner", "synthetic-owner", "--expected-epoch", 1, "--text-stdin", data=self.text)
        self.out, self.coord = self.state / "outgoing.sqlite3", self.ui / "coordinator.sqlite3"
        self.now = int(time.time())
        self.tid = "send-" + hashlib.sha256((self.binding["channel_id"] + "\0" + self.rid).encode()).hexdigest()[:48]
        self.original_path = self.ui / (self.tid + ".client-result.json")
        self.verified_path = self.ui / (self.tid + ".late-observed.json")
        self.original = dict(id=self.tid, request_id=self.rid, attempt_number=1,
                             channel_id=self.binding["channel_id"], session_id="synthetic-session",
                             status="indeterminate", stage="submit_clicked", target_kind="group",
                             focus_evidence_id=self.tid + "-focus", observation={"start_cursor": 0},
                             started_at=datetime.fromtimestamp(self.now - 10, timezone.utc).isoformat(),
                             elapsed_seconds=20)
        self.write(self.original_path, self.original)
        command = dict(id=self.tid, text=self.text, targetGroup="Synthetic Group", targetKind="group",
                       focusEvidenceID=self.tid + "-focus", expectedPID=100, expectedWindowID=10,
                       expectedFrame={"x": 0, "y": 0, "width": 880, "height": 640},
                       composerRect={"x": 300, "y": 450, "width": 500, "height": 140},
                       buttonPoint={"x": 750, "y": 610}, expectedDraft="")
        permissions = dict(accessibility=True, postEvents=True, screenRecording=True)
        for action, flag in (("prepare-visual", "prepared"), ("submit-visual", "submitted")):
            self.write(self.ui / (self.tid + "." + action + ".job.json"), dict(command, action=action))
            self.write(self.ui / (self.tid + "." + action + ".result.json"),
                       dict(id=self.tid, action=action, ok=True, permissions=permissions, **{flag: True}))
        self.write(self.ui / (self.tid + ".visual-state.json"), dict(command, stage="submitted-unverified"))
        self.write(self.ui / (self.tid + ".submit-attempted.json"), b"")
        with sqlite3.connect(self.out) as db:
            db.execute("UPDATE managed_control SET gate=1")
            db.execute("UPDATE outgoing_intent SET status='indeterminate',attempt_number=1,result_evidence=?",
                       (str(self.original_path),))
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,claimed_at,finished_at) VALUES (?,1,'indeterminate',?,?,?)",
                       (self.rid, str(self.original_path), self.now - 10, self.now + 10))
            db.execute("UPDATE managed_control SET gate=0")
        with sqlite3.connect(self.coord) as db:
            db.execute("INSERT INTO dispatch(conversation_id,request_id,operation,channel_id,epoch,status,result,created_at,updated_at) VALUES ('synthetic-conversation',?,'send',?,1,'unknown','indeterminate',?,?)",
                       (self.rid, self.binding["channel_id"], self.now - 10, self.now + 10))
            db.execute("UPDATE surface SET paused_reason='original uncertainty'")
        self.event = dict(sort_seq=100, server_id=900, msg_type=1, sub_type=0, sender="synthetic_self",
                          talker=GROUP, create_time=self.now, content={"Text": self.text}, status=0)
        prefix, body = self.text.split(":\n", 1)
        self.add_event(dict(self.event, sender=prefix, content={"Text": body}))
        self.add_event(self.event, kind="message_updated")
        self.before_attempt = self.attempts()
        self.before_report = self.original_path.read_bytes()

    def add_event(self, event, kind="message_observed"):
        with sqlite3.connect(self.state / "channel.sqlite3") as db:
            db.execute("INSERT INTO channel_events(kind,server_id,message_json) VALUES (?,?,?)",
                       (kind, event["server_id"], json.dumps(event)))

    def attempts(self):
        with sqlite3.connect(self.out) as db:
            return db.execute("SELECT * FROM outgoing_attempt ORDER BY request_id,attempt_number").fetchall()

    def reconcile(self, okay=True, **kwargs):
        args = dict(request_id=self.rid, expected_attempt=1, cursor=2, expected_epoch=1,
                    reason="Reviewed original submitted text and corrected source")
        args.update(kwargs)
        result = self.call("outbox", self.state, "reconcile-text-event",
                           *[part for key, value in args.items() for part in ("--" + key.replace("_", "-"), value)], okay=okay)
        self.assertEqual(self.attempts(), self.before_attempt)
        self.assertEqual(self.original_path.read_bytes(), self.before_report)
        return result

    def test_corrected_source_commits_once_without_resending(self):
        result = self.reconcile()
        self.assertEqual(result["status"], "ui_observed")
        self.assertEqual(result["original_attempt_status"], "indeterminate")
        self.assertEqual(result["new_send_attempts"], 0)
        self.assertNotIn("source_event", result)
        self.assertEqual(result, self.reconcile())
        current = self.call("outbox", self.state, "show", "--request-id", self.rid)
        self.assertEqual(current["attempt_number"], 1)
        self.assertEqual(current["late_observation"]["cursor"], 2)
        with sqlite3.connect(self.coord) as db:
            self.assertEqual(db.execute("SELECT status,result FROM dispatch").fetchone(), ("done", "ui_observed"))
            self.assertEqual(db.execute("SELECT paused_reason FROM surface").fetchone()[0], "original uncertainty")
        with sqlite3.connect(self.out) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outgoing_observation").fetchone()[0], 1)
            for sql in ("UPDATE outgoing_observation SET cursor=3", "DELETE FROM outgoing_observation"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)

    def test_distinct_identical_message_ids_are_ambiguous(self):
        self.add_event(dict(self.event, server_id=901))
        self.reconcile(okay=False)

    def test_revisions_of_one_id_are_not_duplicates(self):
        self.add_event(self.event, kind="message_updated")
        self.assertEqual(self.reconcile(cursor=3)["status"], "ui_observed")

    def test_selected_revision_must_be_latest(self):
        self.add_event(dict(self.event, content={"Text": "changed"}), kind="message_updated")
        self.reconcile(okay=False)

    def test_correction_cannot_change_original_message_time_or_omit_first_observation(self):
        with sqlite3.connect(self.state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_events SET kind='message_updated' WHERE cursor=1")
        self.reconcile(okay=False)
        with sqlite3.connect(self.state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_events SET kind='message_observed' WHERE cursor=1")
            changed = dict(self.event, create_time=self.now + 1)
            db.execute("UPDATE channel_events SET message_json=? WHERE cursor=2", (json.dumps(changed),))
        self.reconcile(okay=False)

    def test_source_id_first_seen_before_baseline_is_not_this_send(self):
        original = dict(self.original, observation={"start_cursor": 1})
        self.write(self.original_path, original)
        self.before_report = self.original_path.read_bytes()
        self.reconcile(okay=False)

    def test_different_message_later_than_attempt_window_is_unrelated(self):
        self.add_event(dict(self.event, server_id=901, create_time=self.now + 100))
        self.assertEqual(self.reconcile()["status"], "ui_observed")

    def test_wrong_sender_text_talker_type_time_or_zero_id_refuses(self):
        variants = [dict(sender="other"), dict(content={"Text": self.text.rstrip("✅")}),
                    dict(talker="other@chatroom"), dict(msg_type=49), dict(create_time=self.now - 100),
                    dict(server_id=0)]
        for patch in variants:
            with self.subTest(patch=patch):
                with sqlite3.connect(self.state / "channel.sqlite3") as db:
                    bad = dict(self.event, **patch)
                    db.execute("UPDATE channel_events SET server_id=?,message_json=? WHERE cursor=2",
                               (bad["server_id"], json.dumps(bad)))
                self.reconcile(okay=False)

    def test_missing_submit_marker_or_result_refuses(self):
        marker = self.ui / (self.tid + ".submit-attempted.json")
        marker.unlink()
        self.reconcile(okay=False)
        self.write(marker, b"")
        (self.ui / (self.tid + ".submit-visual.result.json")).unlink()
        self.reconcile(okay=False)

    def test_exact_native_body_and_input_binding_required(self):
        path = self.ui / (self.tid + ".prepare-visual.job.json")
        original = json.loads(path.read_text())
        for patch in (dict(text=self.text + " "), dict(targetKind="private"), dict(focusEvidenceID="other"),
                      dict(expectedPID=999)):
            self.write(path, dict(original, **patch))
            self.reconcile(okay=False)

    def test_false_native_submit_and_denied_permission_refuse(self):
        path = self.ui / (self.tid + ".submit-visual.result.json")
        original = json.loads(path.read_text())
        self.write(path, dict(original, submitted=False))
        self.reconcile(okay=False)
        self.write(path, dict(original, permissions=dict(original["permissions"], accessibility=False)))
        self.reconcile(okay=False)

    def test_preparation_only_and_later_attempts_refuse(self):
        self.write(self.original_path, dict(self.original, stage="prepared"))
        self.before_report = self.original_path.read_bytes()
        self.reconcile(okay=False)
        self.write(self.original_path, self.original)
        self.before_report = self.original_path.read_bytes()
        with sqlite3.connect(self.out) as db:
            db.execute("UPDATE managed_control SET gate=1")
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,claimed_at,finished_at) VALUES (?,2,'indeterminate',?,?,?)",
                       (self.rid, str(self.original_path), self.now, self.now + 1))
            db.execute("UPDATE managed_control SET gate=0")
        self.before_attempt = self.attempts()
        self.reconcile(okay=False)

    def test_current_epoch_fenced_and_takeover_refused(self):
        self.reconcile(expected_epoch=2, okay=False)
        with sqlite3.connect(self.coord) as db:
            db.execute("UPDATE binding SET control='taken_over'")
        self.reconcile(okay=False)

    def test_origin_epoch_and_owner_cannot_be_adopted(self):
        with sqlite3.connect(self.coord) as db:
            db.execute("UPDATE dispatch SET epoch=2")
        self.reconcile(okay=False)
        with sqlite3.connect(self.coord) as db:
            db.execute("UPDATE dispatch SET epoch=1")
            db.execute("UPDATE binding SET owner='another-owner'")
        self.reconcile(okay=False)

    def test_conflicting_reconciliation_cannot_replace_first_proof(self):
        self.reconcile()
        proof = self.verified_path.read_bytes()
        self.reconcile(okay=False, reason="Changed explanation cannot rewrite immutable proof")
        self.assertEqual(self.verified_path.read_bytes(), proof)

    def test_original_attempt_and_requested_cursor_are_explicit(self):
        self.reconcile(expected_attempt=2, okay=False)
        self.reconcile(cursor=1, okay=False)
        self.reconcile(cursor=99, okay=False)

    def test_shared_locks_and_active_dispatch_refuse(self):
        for name in ("executor.lock", "sender.lock"):
            with (self.ui / name).open("a") as stream:
                os.chmod(self.ui / name, 0o600)
                fcntl.flock(stream, fcntl.LOCK_EX)
                self.reconcile(okay=False)
        with sqlite3.connect(self.coord) as db:
            db.execute("UPDATE surface SET active_dispatch=1")
        self.reconcile(okay=False)

    def test_cross_file_crash_finishes_original_without_new_observation(self):
        with sqlite3.connect(self.coord) as db:
            db.execute("CREATE TRIGGER synthetic_finish_failure BEFORE UPDATE ON dispatch BEGIN SELECT RAISE(ABORT,'synthetic commit failure'); END")
        self.reconcile(okay=False)
        current = self.call("outbox", self.state, "show", "--request-id", self.rid)
        self.assertEqual(current["status"], "ui_observed")
        with sqlite3.connect(self.coord) as db:
            self.assertEqual(db.execute("SELECT status FROM dispatch").fetchone()[0], "unknown")
            db.execute("DROP TRIGGER synthetic_finish_failure")
        self.assertEqual(self.reconcile()["status"], "ui_observed")

    def test_unsafe_original_artifact_refuses(self):
        path = self.ui / (self.tid + ".submit-visual.result.json")
        actual = self.root / "private-copy.json"
        path.rename(actual)
        path.symlink_to(actual)
        self.reconcile(okay=False)

    def test_reconciliation_does_not_make_original_send_retryable(self):
        self.reconcile()
        driver = self.root / "must-not-run"
        marker = self.root / "driver-was-run"
        driver.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        driver.chmod(0o700)
        self.call("outbox", self.state, "send", "--request-id", self.rid, "--driver", driver, okay=False)
        self.assertFalse(marker.exists())
        self.assertEqual(self.attempts(), self.before_attempt)


if __name__ == "__main__":
    unittest.main()
