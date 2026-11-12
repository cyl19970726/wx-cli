"""Compiled CLI admission for one historical two-attempt clipboard chain."""

import hashlib
import json
import sqlite3
import sys
import unittest

from scripts import test_clipboard_recovery as recovery_fixture
from scripts import test_multi_conversation as fixtures


class ClipboardChainTests(unittest.TestCase):
    setUpClass = recovery_fixture.ClipboardRecoveryTests.setUpClass
    cli = recovery_fixture.ClipboardRecoveryTests.cli
    coordinator = recovery_fixture.ClipboardRecoveryTests.coordinator
    outbox = recovery_fixture.ClipboardRecoveryTests.outbox
    queue = recovery_fixture.ClipboardRecoveryTests.queue
    calls = recovery_fixture.ClipboardRecoveryTests.calls
    archive = recovery_fixture.ClipboardRecoveryTests.archive
    write_artifact = recovery_fixture.ClipboardRecoveryTests.write_artifact

    def setUp(self):
        recovery_fixture.ClipboardRecoveryTests.setUp(self)
        first = self.tid
        second = first + '-a2'
        proof = self.ui / (first + '.recovery-proof-to-2.json')
        proof.write_text(json.dumps(dict(request_id=self.request, from_attempt=1,
            attempt_number=2, transaction_id=first,
            text_sha256=hashlib.sha256(b'synthetic reply').hexdigest(),
            prior_focus_mode='archived-clipboard-before-paste',
            proof_saved_at='2026-11-12T00:00:00+00:00')))
        proof.chmod(0o600)
        for path in list(self.ui.iterdir()):
            if path.name.startswith(first + '.') or path.name.startswith(first + '-'):
                if path == proof:
                    continue
                suffix = path.name[len(first):]
                if suffix.startswith('-focus') or suffix.startswith('-inspect') or suffix.startswith('.'):
                    dest = self.ui / (second + suffix)
                    if path.suffix == '.json' and path.stat().st_size:
                        data = json.loads(path.read_text())
                        if isinstance(data, dict):
                            if data.get('id', '').startswith(first):
                                data['id'] = second + data['id'][len(first):]
                            if suffix == '.client-result.json':
                                data.update(attempt_number=2, recovery_from_attempt=1,
                                            recovery_proof=str(proof))
                        dest.write_text(json.dumps(data))
                    else:
                        dest.write_bytes(path.read_bytes())
                    dest.chmod(0o600)
        self.second = second
        self.proof = proof
        second_report = str(self.ui / (second + '.client-result.json'))
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("UPDATE outgoing_intent SET attempt_number=2,result_evidence=? WHERE request_id=?",
                       (second_report, self.request))
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,recovery_from_attempt,recovery_prior_status,claimed_at,finished_at) VALUES(?,2,'indeterminate',?,1,'indeterminate',1,2)",
                       (self.request, second_report))
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET status='unknown',result='indeterminate' WHERE id=?",
                       (self.parent['dispatch_id'],))
        self.original = self.outbox('group', 'show', '--request-id', self.request)
        self.registered.write_text('#!' + sys.executable + r'''
import json, pathlib, sqlite3, sys
root=pathlib.Path(__file__).resolve().parent.parent
mode=sys.argv[1] if len(sys.argv)>1 else 'execute'
with (root/'recovery-calls').open('a') as log: log.write(mode+'\n')
if mode=='--clipboard-chain-recovery-capabilities':
    result=dict(protocol_version=1,clipboard_chain_recovery_protocol_version=1,
                first_two_text_before_paste_only=True,max_attempt=3)
    if (root/'bad-cap').exists(): result['max_attempt']=4
elif mode=='--clipboard-chain-recovery-preflight':
    data=json.load(sys.stdin)
    assert data['attempt_number']==3 and data['recovery_from_attempt']==2
    assert data['clipboard_chain_recovery_protocol_version']==1
    assert data['text']=='synthetic reply' and 'media' not in data
    result=dict(protocol_version=1,clipboard_chain_recovery_protocol_version=1,unsent_proven=True)
    if (root/'bad-proof').exists(): result['unsent_proven']=False
    if (root/'mutate-artifact').exists():
        (root/'ui'/'historical-empty-reference.png').write_bytes(b'drift')
    if (root/'mutate-epoch').exists():
        with sqlite3.connect(root/'ui'/'coordinator.sqlite3') as db:
            db.execute("UPDATE binding SET epoch=epoch+1 WHERE owner='owner_group'")
    if (root/'mutate-outbox').exists():
        with sqlite3.connect(pathlib.Path(__file__).parent/'outgoing.sqlite3') as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("UPDATE outgoing_intent SET text='changed' WHERE request_id=?",(data['request_id'],))
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
elif mode=='--dispatch-preflight': result=dict(ready=True)
elif mode=='execute':
    data=json.load(sys.stdin)
    assert data['attempt_number']==3 and data['recovery_from_attempt']==2
    assert data['clipboard_chain_recovery_protocol_version']==1
    with sqlite3.connect(root/'ui'/'coordinator.sqlite3') as db:
        assert db.execute('SELECT status,result FROM dispatch WHERE request_id=?',(data['request_id'],)).fetchone()==('claimed','reviewed_clipboard_chain_recovery')
    result=dict(status='indeterminate' if (root/'unknown-result').exists() else 'ui_observed',
                evidence='synthetic local observation')
else: raise SystemExit(9)
print(json.dumps(result))
''')
        self.registered.chmod(0o700)

    def recover(self, okay=True, **overrides):
        args = dict(dispatch_id=self.parent['dispatch_id'], request_id=self.request,
                    expected_attempt=2, owner='owner_group', expected_epoch=1,
                    reason='reviewed synthetic two clipboard failures')
        args.update(overrides)
        command = ['dispatch', 'recover-clipboard-chain']
        for key, value in args.items():
            command.extend(('--' + key.replace('_', '-'), value))
        return self.coordinator(*command, okay=okay)

    def test_exact_chain_one_shot_preserves_history(self):
        before = {p.name:p.read_bytes() for p in self.ui.iterdir() if p.name.startswith(self.tid)}
        result = self.recover()
        self.assertEqual((result['status'], result['result'], result['attempt_number']),
                         ('done','ui_observed',3))
        current = self.outbox('group','show','--request-id',self.request)
        self.assertEqual(current['attempts'][:2], self.original['attempts'])
        self.assertEqual(current['attempts'][2]['recovery_from_attempt'],2)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.ui.iterdir() if p.name.startswith(self.tid)})
        self.recover(okay=False)
        self.assertEqual(self.calls().count('execute'),1)

    def test_wrong_authority_and_existing_later_effect_refuse_before_probe(self):
        for override in (dict(owner='other'),dict(expected_epoch=2),dict(expected_attempt=1),
                         dict(request_id='other')):
            with self.subTest(override=override):
                self.recover(okay=False,**override)
                self.assertEqual(self.calls(),[])
        for suffix in ('.replace-draft-before-paste.png','.submit-attempted.json',
                       '-a3.client-result.json'):
            p=self.ui/(self.tid+suffix);p.write_bytes(b'{}');p.chmod(0o600)
            self.recover(okay=False)
            p.unlink()
            self.assertEqual(self.calls(),[])

    def test_bad_second_trace_proof_and_probe_drift_refuse(self):
        for path, key, value in ((self.ui/(self.second+'.client-result.json'),'stage','prepared'),
                                 (self.proof,'prior_focus_mode','other')):
            raw=path.read_bytes(); data=json.loads(raw); data[key]=value
            path.write_text(json.dumps(data))
            self.recover(okay=False)
            path.write_bytes(raw)
            self.assertEqual(self.calls(),[])
        for flag in ('bad-cap','bad-proof','mutate-artifact','mutate-epoch','mutate-outbox'):
            (self.root/flag).touch()
            self.recover(okay=False)
            (self.root/flag).unlink()
            self.assertNotIn('execute',self.calls())
            self.ref.write_bytes(fixtures.PNG)
            with sqlite3.connect(self.scheduler) as db:
                db.execute("UPDATE binding SET epoch=1 WHERE owner='owner_group'")
            with sqlite3.connect(self.outgoing) as db:
                db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
                db.execute("UPDATE outgoing_intent SET text='synthetic reply' WHERE request_id=?",(self.request,))
                db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')

    def test_first_trace_body_batch_and_existing_third_refuse(self):
        first = self.ui/(self.tid+'.prepare-visual.result.json')
        raw=first.read_bytes(); data=json.loads(raw); data['error']='other'
        first.write_text(json.dumps(data))
        self.recover(okay=False)
        first.write_bytes(raw)
        self.assertEqual(self.calls(),[])
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("UPDATE outgoing_intent SET text='changed' WHERE request_id=?",(self.request,))
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        self.recover(okay=False)
        self.assertEqual(self.calls(),[])
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("UPDATE outgoing_intent SET text='synthetic reply' WHERE request_id=?",(self.request,))
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET batch_id='changed',part_index=0 WHERE id=?",(self.parent['dispatch_id'],))
        self.recover(okay=False)
        self.assertEqual(self.calls(),[])
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET batch_id=NULL,part_index=NULL WHERE id=?",(self.parent['dispatch_id'],))
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,claimed_at) VALUES(?,3,'attempt_recorded',3)",(self.request,))
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        self.recover(okay=False)
        self.assertEqual(self.calls(),[])

    def test_crashed_reservation_refuses_without_probe(self):
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET status='claimed',result='reviewed_clipboard_chain_recovery',token='crashed',worker='reviewed-clipboard-chain-recovery' WHERE id=?",
                       (self.parent['dispatch_id'],))
            db.execute('UPDATE surface SET active_dispatch=? WHERE singleton=1',(self.parent['dispatch_id'],))
        self.recover(okay=False)
        self.coordinator('dispatch','resume',okay=False)
        self.assertEqual(self.calls(),[])
        self.assertEqual(self.outbox('group','show','--request-id',self.request),self.original)

    def test_positive_batch_part_zero_allows_new_child(self):
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET batch_id='synthetic-batch',part_index=0 WHERE id=?",(self.parent['dispatch_id'],))
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("UPDATE outgoing_intent SET batch_id='synthetic-batch',part_index=0 WHERE request_id=?",(self.request,))
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        self.recover()
        child=self.queue('group','synthetic-batch-child',batch_id='synthetic-batch',part_index=1)
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status FROM dispatch WHERE id=?',(child['dispatch_id'],)).fetchone()[0],'queued')

    def test_unknown_three_cannot_retry(self):
        (self.root/'unknown-result').touch()
        self.assertEqual(self.recover()['result'],'indeterminate')
        self.recover(okay=False)
        self.assertEqual(self.calls().count('execute'),1)


if __name__ == '__main__':
    import unittest
    unittest.main()
