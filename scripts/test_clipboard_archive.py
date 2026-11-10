"""Offline admission tests for the archive-only clipboard prepare rejection."""

import hashlib
import json
import sqlite3
import sys
import unittest

from scripts import test_managed_focus_recovery as focus
from scripts import test_multi_conversation as fixtures


class ClipboardArchiveTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.MultiConversationTests.setUpClass.__func__)
    cli = fixtures.MultiConversationTests.cli
    coordinator = fixtures.MultiConversationTests.coordinator
    outbox = fixtures.MultiConversationTests.outbox
    queue = fixtures.MultiConversationTests.queue
    calls = focus.ManagedFocusRecoveryTests.calls
    unchanged = focus.ManagedFocusRecoveryTests.unchanged

    def setUp(self):
        focus.ManagedFocusRecoveryTests.setUp(self)
        self.report.update(stage="group_focused", error="clipboard state changed")
        self.write_artifact(".client-result.json", self.report)
        self.write_artifact("-focus.result.json", {
            "id": self.tid + "-focus", "ok": True, "focused": True,
            "category": "group-chats",
        })
        self.write_artifact("-focus.focus-evidence.json", {
            "id": self.tid + "-focus", "category": "group-chats",
        })
        self.native_error = "Clipboard changed during empty draft verification"
        self.write_artifact(".prepare-visual.result.json", {
            "id": self.tid, "ok": False, "error": self.native_error,
            "permissions": {"accessibility": True, "postEvents": True, "screenRecording": True},
        })
        self.ref = self.ui / "historical-empty-reference.png"
        self.ref.write_bytes(fixtures.PNG)
        self.ref.chmod(0o600)
        self.job = {
            "id": self.tid, "action": "prepare-visual", "text": "synthetic reply",
            "expectedDraft": "", "targetGroup": "Synthetic Group",
            "emptyComposerReference": {"path": str(self.ref),
                "sha256": hashlib.sha256(fixtures.PNG).hexdigest()},
        }
        self.write_artifact(".prepare-visual.job.json", self.job)
        state = dict(self.job, stage="prepare-attempted")
        state.pop("action")
        self.write_artifact(".visual-state.json", state)
        for suffix in (".prepare-start.png", ".old-draft-before-click.png",
                       ".old-draft-before-select.png", ".old-draft-before-copy.png",
                       ".old-draft-copy-ready.png", ".old-draft-empty-check.png"):
            path = self.ui / (self.tid + suffix)
            path.write_bytes(fixtures.PNG)
            path.chmod(0o600)
        self.registered.write_text("#!" + sys.executable + r'''
import json, pathlib, sys
root = pathlib.Path(__file__).resolve().parent.parent
mode = sys.argv[1] if len(sys.argv)>1 else 'execute'
with (root/'recovery-calls').open('a') as log: log.write(mode+'\n')
if mode == '--clipboard-archive-capabilities':
    result=dict(protocol_version=1,clipboard_archive_protocol_version=1,
                archive_only=True,before_paste_only=True)
    if (root/'bad-cap').exists(): result['before_paste_only']=False
elif mode == '--clipboard-archive-preflight':
    data=json.load(sys.stdin)
    assert data == dict(protocol_version=1,clipboard_archive_protocol_version=1,
        content_kind='text',request_id='unknown-first-search-recovery',attempt_number=1,
        channel_id=data['channel_id'],session_id=data['session_id'],
        account=data['account'],talker=data['talker'],text='synthetic reply')
    result=dict(protocol_version=1,clipboard_archive_protocol_version=1,
                clipboard_rejection_proven=True,archive_only=True)
    if (root/'bad-proof').exists(): result['clipboard_rejection_proven']=False
    if (root/'mutate-reference').exists():
        path=root/'ui'/'historical-empty-reference.png'
        path.write_bytes(b'changed')
elif mode == 'execute':
    (root/'unexpected-send').touch()
    raise SystemExit(9)
else:
    raise SystemExit(9)
print(json.dumps(result))
''')
        self.registered.chmod(0o700)

    def write_artifact(self, suffix, value):
        path = self.ui / (self.tid + suffix)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def archive(self, okay=True, **overrides):
        values = dict(dispatch_id=self.parent["dispatch_id"], expected_attempt=1,
                      reason="reviewed exact first clipboard rejection")
        values.update(overrides)
        command = ["dispatch", "archive-clipboard-rejection"]
        for key, value in values.items():
            command.extend(("--" + key.replace("_", "-"), value))
        return self.coordinator(*command, okay=okay)

    def test_archive_retains_unknown_history_and_pause(self):
        before = {p.name: p.read_bytes() for p in self.ui.iterdir()
                  if p.name.startswith(self.tid)}
        result = self.archive()
        self.assertEqual((result["status"], result["result"], result["outbox_status"],
                          result["attempt_number"]),
                         ("done", "clipboard_prepare_rejected", "indeterminate", 1))
        self.assertEqual(self.calls(), ["--clipboard-archive-capabilities",
                                        "--clipboard-archive-preflight"])
        self.assertFalse((self.root / "unexpected-send").exists())
        self.unchanged()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.ui.iterdir()
                                  if p.name.startswith(self.tid)})
        self.archive(okay=False)
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute("SELECT status,result FROM dispatch WHERE id=?",
                (self.parent["dispatch_id"],)).fetchone(),
                ("done", "clipboard_prepare_rejected"))

    def test_historical_draft_category_accepts_only_exact_clipboard_native_error(self):
        self.write_artifact(".client-result.json",
                            dict(self.report, error="composer draft mismatch"))
        self.assertEqual(self.archive()["result"], "clipboard_prepare_rejected")
        self.unchanged()

    def test_wrong_error_and_later_artifacts_refuse_before_driver(self):
        native = self.ui / (self.tid + ".prepare-visual.result.json")
        original = native.read_bytes()
        for error in ("other clipboard failure", "Copied composer text does not exactly match expected draft"):
            with self.subTest(error=error):
                self.write_artifact(".client-result.json", dict(self.report,
                    error="composer draft mismatch" if error.startswith("Copied") else "clipboard state changed"))
                self.write_artifact(".prepare-visual.result.json", {
                    "id": self.tid, "ok": False, "error": error,
                    "permissions": {"accessibility": True, "postEvents": True, "screenRecording": True}})
                self.archive(okay=False)
                self.assertEqual(self.calls(), [])
        native.write_bytes(original)
        self.write_artifact(".client-result.json", self.report)
        for suffix in (".replace-draft-before-paste.png", ".new-draft-before-click.png",
                       ".submit-visual.job.json", ".submit-attempted.json",
                       "-a2.client-result.json"):
            with self.subTest(suffix=suffix):
                path = self.ui / (self.tid + suffix)
                path.write_bytes(b"{}")
                path.chmod(0o600)
                self.archive(okay=False)
                path.unlink()
                self.assertEqual(self.calls(), [])
        self.unchanged()

    def test_frozen_text_epoch_reference_and_driver_proof_are_fenced(self):
        job = self.ui / (self.tid + ".prepare-visual.job.json")
        original = job.read_bytes()
        self.write_artifact(".prepare-visual.job.json", dict(self.job, text="changed"))
        self.archive(okay=False)
        job.write_bytes(original)
        self.ref.write_bytes(b"changed")
        self.archive(okay=False)
        self.ref.write_bytes(fixtures.PNG)
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE binding SET epoch=epoch+1 WHERE conversation_id=?",
                       (self.channels["group"]["id"],))
        self.archive(okay=False)
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE binding SET epoch=epoch-1 WHERE conversation_id=?",
                       (self.channels["group"]["id"],))
        self.assertEqual(self.calls(), [])
        (self.root / "bad-cap").touch()
        self.archive(okay=False)
        (self.root / "bad-cap").unlink()
        (self.root / "bad-proof").touch()
        self.archive(okay=False)
        self.unchanged()
        self.assertFalse((self.root / "unexpected-send").exists())

    def test_reference_change_during_preflight_refuses(self):
        (self.root / "mutate-reference").touch()
        self.archive(okay=False)
        self.unchanged()
        self.assertFalse((self.root / "unexpected-send").exists())

    def test_batch_child_stays_blocked_but_independent_first_attempt_can_claim(self):
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET batch_id='synthetic-batch',part_index=0 WHERE id=?",
                       (self.parent["dispatch_id"],))
        child = self.queue("group", "synthetic-batch-child",
                           batch_id="synthetic-batch", part_index=1)
        independent = self.queue("group", "synthetic-independent")
        self.archive()
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute("SELECT status,result FROM dispatch WHERE id=?",
                (child["dispatch_id"],)).fetchone(),
                ("blocked", "batch_predecessor_not_sent"))
        self.coordinator("dispatch", "review", "--reason",
                         "synthetic clipboard archive and fresh first intent reviewed")
        claimed = self.coordinator("dispatch", "claim", "--worker", "synthetic-worker")
        self.assertEqual(claimed["dispatch_id"], independent["dispatch_id"])
        self.assertEqual(self.outbox("group", "show", "--request-id", self.request), self.original)
        self.assertFalse((self.root / "unexpected-send").exists())


if __name__ == "__main__":
    unittest.main()
