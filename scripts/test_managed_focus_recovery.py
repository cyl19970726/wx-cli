"""Offline first-search recovery admission against the compiled Rust CLI."""
import concurrent.futures
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import time
import unittest

from scripts import test_multi_conversation as fixtures


class ManagedFocusRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.MultiConversationTests.setUpClass.__func__)
    cli = fixtures.MultiConversationTests.cli
    coordinator = fixtures.MultiConversationTests.coordinator
    outbox = fixtures.MultiConversationTests.outbox
    queue = fixtures.MultiConversationTests.queue

    def setUp(self):
        fixtures.MultiConversationTests.setUp(self)
        from wx_agent import dispatch
        self.request = "unknown-first-search-recovery"
        channel = self.channels["group"]
        self.outbox("group", "queue", "--request-id", self.request,
                    "--channel-id", channel["binding"]["channel_id"],
                    "--session-id", channel["binding"]["session_id"],
                    "--owner", "owner_group", "--expected-epoch", 1,
                    "--publisher", "fixture_app", "--text-stdin", data="synthetic reply")
        self.parent = self.coordinator("enqueue", "--conversation-id", channel["id"],
                                       "--request-id", self.request, "--expected-epoch", 1)
        dispatch.drain(fixtures.BINARY, self.ui)
        self.state = self.channels["group"]["state"]
        self.outgoing = self.state / "outgoing.sqlite3"
        self.scheduler = self.ui / "coordinator.sqlite3"
        binding = self.channels["group"]["binding"]
        self.tid = "send-" + hashlib.sha256(
            (binding["channel_id"] + "\0" + self.request).encode()).hexdigest()[:48]
        self.report = {
            "id": self.tid, "request_id": self.request, "attempt_number": 1,
            "channel_id": binding["channel_id"], "session_id": binding["session_id"],
            "status": "indeterminate", "stage": "inspected",
            "started_at": "2026-01-01T00:00:00Z", "elapsed_seconds": 23.1,
            "error": "focus failed before message preparation: helper rejected action",
        }
        artifacts = {
            ".client-result.json": self.report,
            "-inspect.inspect.job.json": {"id": self.tid + "-inspect", "action": "inspect"},
            "-inspect.result.json": {"id": self.tid + "-inspect", "ok": True},
            "-focus.focus-group.job.json": {"id": self.tid + "-focus", "action": "focus-group"},
            "-focus.result.json": {"id": self.tid + "-focus", "ok": False,
                "error": "Search popup had no unique category-eligible exact target within six seconds",
                "permissions": {"accessibility": True, "postEvents": True, "screenRecording": True}},
            "-focus.focus-polls.json": [{"elapsedMs": 5100, "popupSelected": False}],
        }
        for suffix, value in artifacts.items():
            path = self.ui / (self.tid + suffix)
            path.write_text(json.dumps(value)); path.chmod(0o600)
        (self.ui / (self.tid + "-focus.focus-attempted.json")).touch(mode=0o600)
        evidence = str(self.ui / (self.tid + ".client-result.json"))
        with sqlite3.connect(self.outgoing) as db:
            db.execute("UPDATE outgoing_intent SET result_evidence=? WHERE request_id=?",
                       (evidence, self.request))
            db.execute("UPDATE outgoing_attempt SET result_evidence=? WHERE request_id=? AND attempt_number=1",
                       (evidence, self.request))
        self.original = self.outbox("group", "show", "--request-id", self.request)
        self.pause = self.coordinator("dispatch", "status")["paused_reason"]
        # Synthetic proof deliberately replaces only the registered driver. The
        # real driver's artifact semantics are independently covered by its tests.
        self.registered = self.state / "conversation-sender"
        self.registered.write_text("#!" + sys.executable + r'''
import fcntl, json, os, pathlib, sqlite3, sys, time
root = pathlib.Path(__file__).resolve().parent.parent
mode = sys.argv[1] if len(sys.argv)>1 else 'execute'
with (root/'recovery-calls').open('a') as log: log.write(mode+'\n')
fd = int(os.environ['WX_DESKTOP_LOCK_FD'])
os.fstat(fd)
for name in ('sender.lock','executor.lock'):
    with (root/'ui'/name).open('r+') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: pass
        else: raise AssertionError('shared lock not held')
if mode == '--focus-recovery-capabilities':
    result = dict(protocol_version=1,focus_recovery_protocol_version=1,first_text_before_prepare_only=True)
    if (root/'bad-cap').exists(): result['first_text_before_prepare_only']=False
    if (root/'extra-cap').exists(): result['extra']=True
elif mode == '--dispatch-preflight':
    result = dict(ready=not (root/'not-ready').exists())
elif mode in ('--focus-recovery-preflight','execute'):
    data=json.load(sys.stdin)
    assert data['attempt_number']==2 and data['recovery_from_attempt']==1
    assert data['text']=='synthetic reply' and 'media' not in data
    if mode == '--focus-recovery-preflight':
        result=dict(protocol_version=1,focus_recovery_protocol_version=1,unsent_proven=True)
        if (root/'bad-proof').exists(): result['unsent_proven']=False
        if (root/'extra-proof').exists(): result['extra']=True
        if (root/'mutate-proof').exists():
            with sqlite3.connect(pathlib.Path(__file__).parent/'outgoing.sqlite3') as db:
                db.execute("UPDATE outgoing_intent SET result_evidence='changed' WHERE request_id=?",(data['request_id'],))
        if (root/'mutate-epoch').exists():
            with sqlite3.connect(root/'ui'/'coordinator.sqlite3') as db:
                db.execute("UPDATE binding SET epoch=epoch+1 WHERE owner='owner_group'")
        if (root/'mutate-artifact').exists():
            identity=data['channel_id']+'\0'+data['request_id']
            import hashlib
            tid='send-'+hashlib.sha256(identity.encode()).hexdigest()[:48]
            (root/'ui'/(tid+'-focus.focus-polls.json')).write_text('[]')
    else:
        with sqlite3.connect(root/'ui'/'coordinator.sqlite3') as db:
            row=db.execute('SELECT paused_reason,active_dispatch FROM surface').fetchone()
            assert row[0] is not None and row[1] is not None
            assert db.execute("SELECT COUNT(*) FROM surface_review WHERE reason LIKE 'focus_recovery %'").fetchone()[0]==1
        with sqlite3.connect(pathlib.Path(__file__).parent/'outgoing.sqlite3') as db:
            assert db.execute('SELECT attempt_number,status FROM outgoing_intent WHERE request_id=?',(data['request_id'],)).fetchone()==(2,'attempt_recorded')
        if (root/'slow-execute').exists():
            (root/'execution-started').touch()
            time.sleep(0.4)
        status='ui_observed'
        if (root/'unknown-result').exists(): status='indeterminate'
        if (root/'failed-result').exists(): status='failed'
        if (root/'false-status').exists(): status='false'
        result=dict(status=status,evidence='synthetic recovered local observation')
else:
    raise AssertionError('unexpected protocol '+mode)
print(json.dumps(result))
''')
        self.registered.chmod(0o700)

    def recover(self, okay=True, **overrides):
        args = dict(dispatch_id=self.parent["dispatch_id"], request_id=self.request,
                    expected_attempt=1, owner="owner_group", expected_epoch=1,
                    reason="reviewed synthetic first search failure")
        args.update(overrides)
        command = ["dispatch", "recover-unsent"]
        for key, value in args.items():
            command.extend(("--" + key.replace("_", "-"), value))
        return self.coordinator(*command, okay=okay)

    def calls(self):
        path = self.root / "recovery-calls"
        return path.read_text().splitlines() if path.exists() else []

    def unchanged(self):
        self.assertEqual(self.outbox("group", "show", "--request-id", self.request), self.original)
        surface = self.coordinator("dispatch", "status")
        self.assertEqual(surface["paused_reason"], self.pause)
        self.assertIsNone(surface["active_dispatch"])

    def test_success_keeps_identity_history_publisher_epoch_and_pause(self):
        self.assert_success_keeps_identity_history_publisher_epoch_and_pause()

    def assert_success_keeps_identity_history_publisher_epoch_and_pause(self):
        result = self.recover()
        self.assertEqual((result["status"], result["result"], result["surface_paused"]),
                         ("done", "ui_observed", True))
        current = self.outbox("group", "show", "--request-id", self.request)
        for key in ("request_id", "text", "publisher_id", "managed_owner", "managed_epoch",
                    "batch_id", "part_index", "priority", "expires_at", "created_at"):
            self.assertEqual(current[key], self.original[key])
        self.assertEqual(current["attempts"][0], self.original["attempts"][0])
        self.assertEqual(current["attempts"][1]["recovery_from_attempt"], 1)
        self.assertEqual(current["attempt_number"], 2)
        self.assertEqual(self.calls(), ['--focus-recovery-capabilities', '--focus-recovery-preflight',
                                        '--dispatch-preflight', 'execute'])
        self.assertEqual(self.coordinator("dispatch", "status")["paused_reason"], self.pause)
        self.recover(okay=False)
        self.assertEqual(self.calls().count('execute'), 1)
        with sqlite3.connect(self.scheduler) as db:
            audit = db.execute("SELECT reason FROM surface_review WHERE reason LIKE 'focus_recovery %'").fetchone()[0]
            self.assertIn("attempt=1->2", audit)
            self.assertIn("reviewed synthetic first search failure", audit)
            self.assertEqual(db.execute("SELECT epoch FROM binding WHERE owner='owner_group'").fetchone()[0], 1)

    def test_bad_explicit_identity_attempt_and_arbitrary_driver_have_no_probe(self):
        for override in (dict(owner="other_owner"), dict(expected_epoch=2), dict(expected_attempt=2),
                         dict(request_id="other-request"), dict(dispatch_id=123456), dict(reason="short"),
                         dict(driver=str(self.driver))):
            with self.subTest(override=override):
                self.recover(okay=False, **override)
                self.unchanged()
                self.assertEqual(self.calls(), [])

    def test_not_enumerated_search_keeps_recovery_identity_and_single_attempt(self):
        path = self.ui / (self.tid + '-focus.result.json')
        native = json.loads(path.read_text())
        native['error'] = 'Search popup not enumerated by ScreenCaptureKit within six seconds'
        path.write_text(json.dumps(native))
        (self.ui / (self.tid + '-focus.focus-polls.json')).write_text(json.dumps([
            {'elapsedMs': 100, 'popupSelected': False, 'windowCount': 1},
            {'elapsedMs': 6100, 'popupSelected': False, 'windowCount': 1},
        ]))
        # This fixture tests Rust admission; the registered proof driver remains
        # synthetic. Error-specific screenshot/poll validation belongs to Python.
        self.assert_success_keeps_identity_history_publisher_epoch_and_pause()

    def test_other_native_errors_never_probe_or_change_history(self):
        path = self.ui / (self.tid + '-focus.result.json')
        native = json.loads(path.read_text())
        for error in (
            'Timed out waiting for focus',
            'Search popup timed out within six seconds',
            'Clipboard verification failed',
            'Selected chat header is not exact target and kind',
            'Search popup not enumerated by ScreenCaptureKit within seven seconds',
            'Search popup not enumerated by ScreenCaptureKit within six seconds (timeout)',
        ):
            with self.subTest(error=error):
                native['error'] = error
                path.write_text(json.dumps(native))
                self.recover(okay=False)
                self.unchanged()
                self.assertEqual(self.calls(), [])

    def test_bad_capability_proof_readiness_never_claims_attempt(self):
        for flag in ('bad-cap','extra-cap','bad-proof','extra-proof','not-ready'):
            with self.subTest(flag=flag):
                (self.root/flag).touch()
                self.recover(okay=False)
                (self.root/flag).unlink()
                self.unchanged()
                self.assertNotIn('execute', self.calls())
                with sqlite3.connect(self.scheduler) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM surface_review WHERE reason LIKE 'focus_recovery %'").fetchone()[0], 0)

    def test_unpaused_active_other_dispatch_and_control_are_rejected(self):
        changes = (
            ("UPDATE surface SET paused_reason=NULL", "UPDATE surface SET paused_reason=?", (self.pause,)),
            ("UPDATE surface SET active_dispatch=999", "UPDATE surface SET active_dispatch=NULL", ()),
            ("UPDATE binding SET control='taken_over' WHERE owner='owner_group'", "UPDATE binding SET control='active' WHERE owner='owner_group'", ()),
            ("UPDATE binding SET reply_policy='observe' WHERE owner='owner_group'", "UPDATE binding SET reply_policy='authorized' WHERE owner='owner_group'", ()),
        )
        for change, undo, params in changes:
            with self.subTest(change=change):
                with sqlite3.connect(self.scheduler) as db: db.execute(change)
                self.recover(okay=False)
                with sqlite3.connect(self.scheduler) as db: db.execute(undo, params)
                self.assertEqual(self.calls(), [])
                self.unchanged()
        other = self.queue("private", "other-dispatch")
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET status='claimed' WHERE id=?", (other["dispatch_id"],))
        self.recover(okay=False)
        self.recover(okay=False, dispatch_id=other["dispatch_id"])
        self.assertEqual(self.calls(), [])

    def test_media_origin_epoch_and_live_attempt_are_rejected(self):
        changes = (
            ("UPDATE outgoing_intent SET content_kind='image'", "UPDATE outgoing_intent SET content_kind='text'"),
            ("UPDATE outgoing_intent SET managed_owner='other'", "UPDATE outgoing_intent SET managed_owner='owner_group'"),
            ("UPDATE outgoing_intent SET managed_epoch=2", "UPDATE outgoing_intent SET managed_epoch=1"),
            ("UPDATE outgoing_intent SET attempt_number=2", "UPDATE outgoing_intent SET attempt_number=1"),
            ("UPDATE outgoing_attempt SET finished_at=NULL", None),
        )
        with sqlite3.connect(self.outgoing) as db:
            finished = db.execute("SELECT finished_at FROM outgoing_attempt").fetchone()[0]
        for change, undo in changes:
            with self.subTest(change=change):
                with sqlite3.connect(self.outgoing) as db: db.execute(change)
                self.recover(okay=False)
                with sqlite3.connect(self.outgoing) as db:
                    if undo: db.execute(undo)
                    else: db.execute("UPDATE outgoing_attempt SET finished_at=?", (finished,))
                self.assertEqual(self.calls(), [])
                self.unchanged()

    def test_prepare_submit_click_and_later_artifacts_or_wrong_error_rejected(self):
        for suffix in ('.prepare-visual.job.json','.submit-visual.job.json',
                       '-focus.focus-search-before-click-main.png','-a2.client-result.json'):
            with self.subTest(suffix=suffix):
                path = self.ui / (self.tid+suffix)
                path.write_text('{}'); path.chmod(0o600)
                self.recover(okay=False)
                path.unlink()
                self.unchanged()
                self.assertEqual(self.calls(), [])
        path=self.ui/(self.tid+'-focus.result.json')
        native=json.loads(path.read_text())
        native['error']='Selected chat header is not exact target and kind'
        path.write_text(json.dumps(native))
        self.recover(okay=False)
        self.assertEqual(self.calls(), [])

    def test_proof_races_refuse_before_recovery_claim(self):
        for flag in ('mutate-proof','mutate-epoch','mutate-artifact'):
            with self.subTest(flag=flag):
                (self.root/flag).touch()
                self.recover(okay=False)
                (self.root/flag).unlink()
                self.assertNotIn('execute', self.calls())
                with sqlite3.connect(self.outgoing) as db:
                    self.assertEqual(db.execute('SELECT attempt_number FROM outgoing_intent').fetchone()[0], 1)
                    db.execute("UPDATE outgoing_intent SET result_evidence=?", (self.original['result_evidence'],))
                with sqlite3.connect(self.scheduler) as db:
                    db.execute("UPDATE binding SET epoch=1 WHERE owner='owner_group'")
                (self.ui/(self.tid+'-focus.focus-polls.json')).write_text(json.dumps([{"elapsedMs":5100,"popupSelected":False}]))
                self.unchanged()

    def test_unknown_and_false_status_stop_at_attempt_two(self):
        for flag in ('unknown-result', 'false-status', 'failed-result'):
            with self.subTest(flag=flag):
                # Each result requires its own immutable intent history.
                test = type(self)('test_success_keeps_identity_history_publisher_epoch_and_pause')
                test.setUp()
                try:
                    (test.root/flag).touch()
                    result=test.recover()
                    expected = 'blocked' if flag=='failed-result' else 'unknown'
                    self.assertEqual(result['status'],expected)
                    test.recover(okay=False)
                    item=test.outbox('group','show','--request-id',test.request)
                    self.assertEqual(item['attempt_number'],2)
                    self.assertEqual(item['attempts'][0],test.original['attempts'][0])
                    self.assertEqual(test.calls().count('execute'),1)
                finally: test.doCleanups()

    def test_shared_locks_and_parallel_recovery_never_duplicate(self):
        for name in ('sender.lock','executor.lock'):
            with (self.ui/name).open('r+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.recover(okay=False)
                self.unchanged()
                self.assertEqual(self.calls(),[])
        (self.root/'slow-execute').touch()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(self.recover)
            deadline=time.monotonic()+5
            while not (self.root/'execution-started').exists() and time.monotonic()<deadline:
                time.sleep(0.01)
            self.assertTrue((self.root/'execution-started').exists())
            self.recover(okay=False)
            self.assertEqual(first.result()['status'],'done')
        self.assertEqual(self.calls().count('execute'),1)


if __name__ == '__main__':
    unittest.main()
