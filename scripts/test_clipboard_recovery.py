"""Compiled CLI admission tests for one archived TEXT clipboard recovery."""

import json
import sqlite3
import sys
import unittest

from scripts import test_clipboard_archive as archive_fixture
from scripts import test_multi_conversation as fixtures


class ClipboardRecoveryTests(unittest.TestCase):
    setUpClass = archive_fixture.ClipboardArchiveTests.setUpClass
    cli = archive_fixture.ClipboardArchiveTests.cli
    coordinator = archive_fixture.ClipboardArchiveTests.coordinator
    outbox = archive_fixture.ClipboardArchiveTests.outbox
    queue = archive_fixture.ClipboardArchiveTests.queue
    calls = archive_fixture.ClipboardArchiveTests.calls
    archive = archive_fixture.ClipboardArchiveTests.archive
    write_artifact = archive_fixture.ClipboardArchiveTests.write_artifact

    def setUp(self):
        archive_fixture.ClipboardArchiveTests.setUp(self)
        self.archive()
        (self.root / "recovery-calls").unlink()
        self.registered.write_text("#!" + sys.executable + r'''
import json, pathlib, sqlite3, sys
root = pathlib.Path(__file__).resolve().parent.parent
mode = sys.argv[1] if len(sys.argv)>1 else 'execute'
with (root/'recovery-calls').open('a') as log: log.write(mode+'\n')
if mode == '--clipboard-recovery-capabilities':
    result=dict(protocol_version=1,clipboard_recovery_protocol_version=1,
                first_text_before_paste_only=True,max_attempt=2)
    if (root/'bad-cap').exists(): result['max_attempt']=3
elif mode == '--clipboard-recovery-preflight':
    data=json.load(sys.stdin)
    assert data['attempt_number']==2 and data['recovery_from_attempt']==1
    assert data['clipboard_recovery_protocol_version']==1 and data['text']=='synthetic reply'
    assert 'media' not in data
    result=dict(protocol_version=1,clipboard_recovery_protocol_version=1,unsent_proven=True)
    if (root/'bad-proof').exists(): result['unsent_proven']=False
    if (root/'mutate-artifact').exists():
        path=root/'ui'/'historical-empty-reference.png'
        path.write_bytes(b'changed')
    if (root/'mutate-outbox').exists():
        with sqlite3.connect(pathlib.Path(__file__).parent/'outgoing.sqlite3') as db:
            db.execute("UPDATE outgoing_intent SET text='changed' WHERE request_id=?",(data['request_id'],))
    if (root/'mutate-epoch').exists():
        with sqlite3.connect(root/'ui'/'coordinator.sqlite3') as db:
            db.execute("UPDATE binding SET epoch=epoch+1 WHERE owner='owner_group'")
elif mode == '--dispatch-preflight':
    result=dict(ready=True)
elif mode == 'execute':
    data=json.load(sys.stdin)
    assert data['attempt_number']==2 and data['recovery_from_attempt']==1
    assert data['clipboard_recovery_protocol_version']==1 and data['text']=='synthetic reply'
    with sqlite3.connect(root/'ui'/'coordinator.sqlite3') as db:
        assert db.execute("SELECT status,result,worker FROM dispatch WHERE request_id=?",(data['request_id'],)).fetchone()==(
            'claimed','reviewed_clipboard_rejection_recovery','reviewed-clipboard-rejection-recovery')
        assert db.execute("SELECT active_dispatch,paused_reason FROM surface").fetchone()[0] is not None
    with sqlite3.connect(pathlib.Path(__file__).parent/'outgoing.sqlite3') as db:
        assert db.execute("SELECT attempt_number,status FROM outgoing_intent WHERE request_id=?",(data['request_id'],)).fetchone()==(2,'attempt_recorded')
    result=dict(status='indeterminate' if (root/'unknown-result').exists() else 'ui_observed',
                evidence='synthetic local observation')
else:
    raise SystemExit(9)
print(json.dumps(result))
''')
        self.registered.chmod(0o700)

    def recover(self, okay=True, **overrides):
        args = dict(dispatch_id=self.parent['dispatch_id'], request_id=self.request,
                    expected_attempt=1, owner='owner_group', expected_epoch=1,
                    reason='reviewed synthetic archived clipboard rejection')
        args.update(overrides)
        command = ['dispatch', 'recover-clipboard-rejection']
        for key, value in args.items():
            command.extend(('--' + key.replace('_', '-'), value))
        return self.coordinator(*command, okay=okay)

    def test_one_recovery_updates_projection_and_preserves_original_history(self):
        before = {p.name: p.read_bytes() for p in self.ui.iterdir() if p.name.startswith(self.tid)}
        result = self.recover()
        self.assertEqual((result['status'], result['result'], result['surface_paused']),
                         ('done', 'ui_observed', True))
        current = self.outbox('group', 'show', '--request-id', self.request)
        for key in ('request_id','text','publisher_id','managed_owner','managed_epoch','batch_id','part_index'):
            self.assertEqual(current[key], self.original[key])
        self.assertEqual(current['attempts'][0], self.original['attempts'][0])
        self.assertEqual(current['attempts'][1]['recovery_from_attempt'], 1)
        self.assertEqual(current['attempt_number'], 2)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.ui.iterdir() if p.name.startswith(self.tid)})
        self.assertEqual(self.calls(), ['--clipboard-recovery-capabilities','--clipboard-recovery-preflight',
                                        '--dispatch-preflight','execute'])
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                (self.parent['dispatch_id'],)).fetchone(), ('done','ui_observed'))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM surface_review WHERE reason LIKE 'clipboard_prepare_rejected:%'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM surface_review WHERE reason LIKE 'clipboard_rejection_recovery %'").fetchone()[0], 1)
        self.assertIsNotNone(self.coordinator('dispatch','status')['paused_reason'])
        self.recover(okay=False)
        self.assertEqual(self.calls().count('execute'), 1)

    def test_unknown_result_cannot_retry(self):
        (self.root/'unknown-result').touch()
        result=self.recover()
        self.assertEqual((result['status'],result['result'],result['surface_paused']),
                         ('unknown','indeterminate',True))
        self.recover(okay=False)
        self.assertEqual(self.calls().count('execute'), 1)

    def make_part_zero_batch(self):
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET batch_id='synthetic-batch',part_index=0 WHERE id=?",
                       (self.parent['dispatch_id'],))
        with sqlite3.connect(self.outgoing) as db:
            db.execute("UPDATE outgoing_intent SET batch_id='synthetic-batch',part_index=0 WHERE request_id=?",
                       (self.request,))

    def test_archive_alone_blocks_existing_child_and_recovery_does_not_restore_it(self):
        self.make_part_zero_batch()
        child = self.queue('group','synthetic-batch-child',batch_id='synthetic-batch',part_index=1)
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                (self.parent['dispatch_id'],)).fetchone(), ('done','clipboard_prepare_rejected'))
        self.coordinator('dispatch','review','--reason','synthetic recovery reviewed')
        claimed = self.coordinator('dispatch','claim','--worker','synthetic-worker')
        self.assertNotEqual(claimed.get('dispatch_id'), child['dispatch_id'])
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                (child['dispatch_id'],)).fetchone(), ('blocked','batch_predecessor_not_sent'))
        self.coordinator('dispatch','pause','--reason','reviewed synthetic clipboard recovery')
        self.recover()
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                (child['dispatch_id'],)).fetchone(), ('blocked','batch_predecessor_not_sent'))

    def test_positive_part_zero_allows_new_authorized_part_one(self):
        self.make_part_zero_batch()
        self.recover()
        child = self.queue('group','synthetic-batch-child',batch_id='synthetic-batch',part_index=1)
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                (self.parent['dispatch_id'],)).fetchone(), ('done','ui_observed'))
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                (child['dispatch_id'],)).fetchone(), ('queued',None))
        self.coordinator('dispatch','review','--reason','positive part zero reviewed')
        claimed = self.coordinator('dispatch','claim','--worker','synthetic-worker')
        self.assertEqual(claimed['dispatch_id'], child['dispatch_id'])

    def test_unknown_part_zero_cannot_credit_new_part_one(self):
        self.make_part_zero_batch()
        (self.root/'unknown-result').touch()
        self.recover()
        child = self.queue('group','synthetic-batch-child',batch_id='synthetic-batch',part_index=1)
        self.coordinator('dispatch','review','--reason','unknown part zero reviewed')
        claimed = self.coordinator('dispatch','claim','--worker','synthetic-worker')
        self.assertNotEqual(claimed.get('dispatch_id'), child['dispatch_id'])
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                (child['dispatch_id'],)).fetchone(), ('queued',None))

    def test_existing_reservation_blocks_after_crash_without_probe(self):
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET status='claimed',result='reviewed_clipboard_rejection_recovery',token='crashed',worker='reviewed-clipboard-rejection-recovery' WHERE id=?",
                       (self.parent['dispatch_id'],))
            db.execute('UPDATE surface SET active_dispatch=? WHERE singleton=1',
                       (self.parent['dispatch_id'],))
        self.recover(okay=False)
        self.coordinator('dispatch','resume',okay=False)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.outbox('group','show','--request-id',self.request), self.original)

    def test_archived_batch_mismatch_and_original_report_effects_refuse(self):
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET batch_id='other',part_index=0 WHERE id=?",
                       (self.parent['dispatch_id'],))
        self.recover(okay=False)
        self.assertEqual(self.calls(), [])
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET batch_id=NULL,part_index=NULL WHERE id=?",
                       (self.parent['dispatch_id'],))
        path=self.ui/(self.tid+'.client-result.json')
        original=path.read_bytes()
        for key in ('observation','ui_submit_seconds','late_observation','media_prepared'):
            with self.subTest(key=key):
                report=json.loads(original); report[key]='synthetic extra'
                path.write_text(json.dumps(report))
                self.recover(okay=False)
                self.assertEqual(self.calls(), [])
        path.write_bytes(original)

    def test_wrong_identity_and_unproven_artifacts_refuse_before_probe(self):
        for override in (dict(owner='other'),dict(expected_epoch=2),dict(expected_attempt=2),
                         dict(request_id='other'),dict(dispatch_id=999999),dict(reason='short')):
            with self.subTest(override=override):
                self.recover(okay=False, **override)
                self.assertEqual(self.calls(), [])
        for suffix in ('.replace-draft-before-paste.png','.submit-attempted.json',
                       '-a2.client-result.json'):
            with self.subTest(suffix=suffix):
                path=self.ui/(self.tid+suffix); path.write_bytes(b'{}'); path.chmod(0o600)
                self.recover(okay=False)
                path.unlink()
                self.assertEqual(self.calls(), [])

    def test_historical_client_category_with_exact_native_error_is_accepted(self):
        path=self.ui/(self.tid+'.client-result.json')
        report=json.loads(path.read_text()); report['error']='composer draft mismatch'
        path.write_text(json.dumps(report))
        self.assertEqual(self.recover()['result'], 'ui_observed')

    def test_copy_mismatch_native_with_other_historical_category_is_accepted(self):
        path=self.ui/(self.tid+'.prepare-visual.result.json')
        native=json.loads(path.read_text())
        native['error']='Copied composer text does not exactly match expected draft'
        path.write_text(json.dumps(native))
        self.assertEqual(self.recover()['result'], 'ui_observed')

    def test_near_native_error_refuses_before_probe(self):
        path=self.ui/(self.tid+'.prepare-visual.result.json')
        native=json.loads(path.read_text())
        native['error']='Clipboard changed during empty draft verification '
        path.write_text(json.dumps(native))
        self.recover(okay=False)
        self.assertEqual(self.calls(), [])

    def test_bad_capability_proof_and_slow_probe_changes_refuse(self):
        for flag in ('bad-cap','bad-proof','mutate-artifact','mutate-outbox','mutate-epoch'):
            with self.subTest(flag=flag):
                (self.root/flag).touch()
                self.recover(okay=False)
                (self.root/flag).unlink()
                self.assertNotIn('execute', self.calls())
                with sqlite3.connect(self.scheduler) as db:
                    self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                        (self.parent['dispatch_id'],)).fetchone(), ('done','clipboard_prepare_rejected'))
                    db.execute("UPDATE binding SET epoch=1 WHERE owner='owner_group'")
                with sqlite3.connect(self.outgoing) as db:
                    db.execute("UPDATE outgoing_intent SET text='synthetic reply' WHERE request_id=?",(self.request,))
                self.ref.write_bytes(fixtures.PNG)


if __name__ == '__main__':
    unittest.main()
