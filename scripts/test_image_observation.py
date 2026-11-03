#!/usr/bin/env python3
"""Compiled late IMAGE reconciliation; synthetic XOR .dat, no GUI/driver/keys."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import zlib
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone

from scripts.test_local_channel import fixture, GROUP

BINARY = Path(__file__).resolve().parents[1] / 'target/debug/wx-cli'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


class ImageObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise unittest.SkipTest('compiled target/debug/wx-cli is required')

    def call(self, section, state, *args, okay=True):
        p = subprocess.run([str(BINARY), section, '--state-dir', str(state), *map(str,args)],
                           capture_output=True, text=True, timeout=15)
        if okay:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        return p.stderr

    def write(self, path, value):
        path.write_bytes(value if isinstance(value,bytes) else json.dumps(value).encode())
        path.chmod(0o600)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wx-image-observation-synthetic-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state, self.source, self.ui = [self.root / x for x in ('state','source','ui')]
        fixture(self.source)
        self.ui.mkdir(mode=0o700)
        self.binding = self.call('channel',self.state,'bind','--account','synthetic', '--talker',GROUP,
            '--session','synthetic-session','--source',self.source)
        with sqlite3.connect(self.state/'channel.sqlite3') as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self',encrypted_snapshot=1")
        self.call('coordinator',self.ui,'bind','--conversation-id','synthetic-conversation',
            '--channel-state-dir',self.state,'--owner','synthetic-owner','--input','push','--reply-policy','authorized')
        image = self.root/'image.png'; image.write_bytes(PNG)
        self.rid = 'original-image'
        self.call('outbox',self.state,'queue-image','--request-id',self.rid,'--image',image,
            '--channel-id',self.binding['channel_id'],'--session-id','synthetic-session',
            '--owner','synthetic-owner','--expected-epoch','1')
        self.out = self.state/'outgoing.sqlite3'; self.coord = self.ui/'coordinator.sqlite3'
        with sqlite3.connect(self.out) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM outgoing_intent').fetchone()
            self.media = dict(zip(('path','sha256','mime_type','size_bytes'),
                [row[x] for x in ('media_path','media_sha256','media_mime_type','media_size_bytes')]))
        self.tid = 'send-'+hashlib.sha256((self.binding['channel_id']+'\0'+self.rid).encode()).hexdigest()[:48]
        self.original_path = self.ui/(self.tid+'.client-result.json')
        self.verified_path = self.ui/(self.tid+'.late-observed.json')
        self.capture = self.ui/(self.tid+'.submit-after-click.png'); self.write(self.capture,PNG)
        p = dict(self.media, exact_pixels=True,pixel_sha256='a'*64,composer_pixel_sha256='a'*64,
                 width=1,height=1,composer_copy_type='public.png')
        p.pop('path')
        s = dict(p,prepared_copy_type='public.png',post_click_capture=str(self.capture),
            post_click_capture_sha256=hashlib.sha256(PNG).hexdigest(),post_click_header_verified=True)
        self.now = int(time.time())
        self.original = dict(id=self.tid,request_id=self.rid,attempt_number=1,
            channel_id=self.binding['channel_id'],session_id='synthetic-session',status='indeterminate',stage='submit_clicked',
            started_at=datetime.fromtimestamp(self.now-10,timezone.utc).isoformat(),elapsed_seconds=20,
            observation={'start_cursor':0},media_prepared=p,media_submitted=s,
            focus_evidence_id=self.tid+'-focus',target_kind='group')
        self.write(self.original_path,self.original)
        command = dict(id=self.tid,media=self.media,targetGroup='Synthetic Group',targetKind='group',
            focusEvidenceID=self.tid+'-focus',expectedPID=100,expectedWindowID=10,
            expectedFrame={'x':0,'y':0,'width':800,'height':600},composerRect={'x':0,'y':400,'width':800,'height':100},
            buttonPoint={'x':750,'y':550},expectedDraft='')
        for action in ('prepare-visual','submit-visual'):
            self.write(self.ui/(self.tid+'.'+action+'.job.json'),dict(command,action=action))
        permissions=dict(accessibility=True,postEvents=True,screenRecording=True)
        self.write(self.ui/(self.tid+'.prepare-visual.result.json'),dict(id=self.tid,action='prepare-visual',ok=True,prepared=True,media_prepared=p,permissions=permissions))
        self.write(self.ui/(self.tid+'.submit-visual.result.json'),dict(id=self.tid,action='submit-visual',ok=True,submitted=True,media_submitted=s,permissions=permissions))
        self.write(self.ui/(self.tid+'.visual-state.json'),dict(command,stage='submitted-unverified',text='',preparedPixelSHA256='a'*64,preparedCopyType='public.png'))
        self.write(self.ui/(self.tid+'.submit-attempted.json'),b'')
        with sqlite3.connect(self.out) as db:
            # Seed a historical synthetic attempt under the existing coordinator
            # gate, then restore it; no driver or send command is ever called.
            db.execute('UPDATE managed_control SET gate=1')
            db.execute("UPDATE outgoing_intent SET status='indeterminate',attempt_number=1,result_evidence=?",(str(self.original_path),))
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,claimed_at,finished_at) VALUES (?,1,'indeterminate',?,?,?)",(self.rid,str(self.original_path),self.now-10,self.now+10))
            db.execute('UPDATE managed_control SET gate=0')
        with sqlite3.connect(self.coord) as db:
            db.execute("INSERT INTO dispatch(conversation_id,request_id,operation,channel_id,epoch,status,result,created_at,updated_at) VALUES ('synthetic-conversation',?,'send',?,1,'unknown','indeterminate',?,?)",(self.rid,self.binding['channel_id'],self.now-10,self.now+10))
            db.execute("UPDATE surface SET paused_reason='original uncertainty'")
        self.md5 = hashlib.md5(PNG).hexdigest()
        attach = self.source/'msg/attach'/hashlib.md5(GROUP.encode()).hexdigest()/'2026-11/Img'
        attach.mkdir(parents=True)
        self.dat = attach/(self.md5+'.dat'); self.dat.write_bytes(bytes(b ^ 0x5a for b in PNG))
        self.event = dict(sort_seq=100,server_id=900,msg_type=3,sub_type=0,sender='synthetic_self',talker=GROUP,
                          create_time=self.now,content={'Image':{'md5':self.md5}},status=0)
        self.add_event(self.event)
        self.before_attempt = self.attempts()
        self.before_report = self.original_path.read_bytes()

    def add_event(self,event):
        with sqlite3.connect(self.state/'channel.sqlite3') as db:
            db.execute("INSERT INTO channel_events(kind,server_id,message_json) VALUES ('message_observed',?,?)",(event['server_id'],json.dumps(event)))

    def attempts(self):
        with sqlite3.connect(self.out) as db:
            return db.execute('SELECT * FROM outgoing_attempt ORDER BY request_id,attempt_number').fetchall()

    def reconcile(self,okay=True,**kw):
        args = dict(request_id=self.rid,expected_attempt=1,cursor=1,expected_epoch=1,reason='independent late source proof')
        args.update(kw)
        return self.call('outbox',self.state,'reconcile-image-event',
            *[v for k,value in args.items() for v in ('--'+k.replace('_','-'),str(value))],okay=okay)

    def unchanged(self):
        self.assertEqual(self.attempts(),self.before_attempt)
        self.assertEqual(self.original_path.read_bytes(),self.before_report)

    def test_positive_idempotent_original_history_immutable(self):
        report = self.reconcile()
        self.assertEqual(report['status'],'ui_observed')
        self.assertEqual(report['original_attempt_status'],'indeterminate')
        self.assertEqual(report['new_send_attempts'],0)
        self.assertNotIn('source_event',report)
        self.assertNotIn('native_proof',report)
        self.assertEqual(report,self.reconcile())
        current=self.call('outbox',self.state,'show','--request-id',self.rid)
        self.assertEqual(current['late_observation']['original_attempt_status'],'indeterminate')
        self.unchanged()
        with sqlite3.connect(self.out) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outgoing_observation').fetchone()[0],1)
            for sql in ('UPDATE outgoing_observation SET cursor=2','DELETE FROM outgoing_observation'):
                with self.assertRaises(sqlite3.IntegrityError): db.execute(sql)
        with sqlite3.connect(self.coord) as db:
            self.assertEqual(db.execute('SELECT status,result,epoch FROM dispatch').fetchone(),('done','ui_observed',1))
            self.assertEqual(db.execute('SELECT paused_reason FROM surface').fetchone()[0],'original uncertainty')

    def test_absent_submit_refuses_prepare_failure(self):
        (self.ui/(self.tid+'.submit-visual.result.json')).unlink()
        self.reconcile(okay=False); self.unchanged()

    def test_ambiguous_event_refuses(self):
        self.add_event(dict(self.event,server_id=901)); self.reconcile(okay=False,cursor=2); self.unchanged()

    def test_second_candidate_after_selected_cursor_is_still_ambiguous(self):
        self.add_event(dict(self.event,server_id=901))
        self.reconcile(okay=False,cursor=1)
        self.unchanged()

    def test_later_image_outside_original_time_window_is_unrelated(self):
        self.add_event(dict(self.event,server_id=901,create_time=self.now+100))
        self.assertEqual(self.reconcile(cursor=1)['status'],'ui_observed')
        self.unchanged()

    def test_malformed_second_image_is_still_ambiguous(self):
        self.add_event(dict(self.event,server_id=901,content={}))
        self.reconcile(okay=False,cursor=2); self.unchanged()

    def test_missing_source_and_missing_or_nonnew_baseline_refuse(self):
        self.dat.unlink(); self.reconcile(okay=False)
        self.dat.write_bytes(bytes(b ^ 0x5a for b in PNG))
        for observation in ({}, {'start_cursor':1}):
            report = dict(self.original,observation=observation)
            self.write(self.original_path,report)
            self.reconcile(okay=False)
        self.write(self.original_path,self.before_report)
        self.unchanged()

    def test_wrong_media_refuses(self):
        def chunk(kind,data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
        wrong = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))
                 +chunk(b'IDAT',zlib.compress(b'\0\xff\0\0'))+chunk(b'IEND',b''))
        self.dat.write_bytes(bytes(b ^ 0x5a for b in wrong))
        self.reconcile(okay=False); self.unchanged()

    def test_changed_epoch_refuses(self):
        with sqlite3.connect(self.coord) as db: db.execute('UPDATE binding SET epoch=2')
        self.reconcile(okay=False); self.unchanged()

    def test_wrong_cursor_and_request_refuse(self):
        for kw in ({'cursor':0},{'cursor':2},{'request_id':'different-request'},{'expected_attempt':2}):
            self.reconcile(okay=False,**kw)
        self.unchanged()

    def test_capture_tamper_refuses(self):
        self.write(self.capture,PNG+b'tampered'); self.reconcile(okay=False); self.unchanged()

    def test_each_shared_lock_busy_refuses(self):
        for name in ('executor.lock','sender.lock'):
            path = self.ui/name
            path.touch(mode=0o600,exist_ok=True)
            with path.open('r+') as f:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.reconcile(okay=False)
        self.unchanged()

    def test_duplicate_source_event_refuses(self):
        with sqlite3.connect(self.out) as db:
            db.execute("INSERT INTO outgoing_observation VALUES ('another-request',1,?,?,1,900,'indeterminate','old','sha','new','sha',1)",
                       (self.binding['channel_id'],'synthetic-session'))
        self.reconcile(okay=False); self.unchanged()
        self.assertFalse(self.verified_path.exists())

    def test_current_epoch_can_differ_from_original_without_changing_history(self):
        with sqlite3.connect(self.coord) as db: db.execute('UPDATE binding SET epoch=2')
        self.assertEqual(self.reconcile(expected_epoch=2)['status'],'ui_observed')
        self.unchanged()
        with sqlite3.connect(self.coord) as db:
            self.assertEqual(db.execute('SELECT epoch FROM dispatch').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT epoch FROM binding').fetchone()[0],2)

    def test_original_prepare_failure_never_passes(self):
        self.write(self.ui/(self.tid+'.prepare-visual.result.json'),dict(id=self.tid,ok=False,error='synthetic prepare failure'))
        self.reconcile(okay=False); self.unchanged()

    def test_wrong_native_action_kind_or_permission_is_not_positive_proof(self):
        cases=(('.prepare-visual.result.json','action','inspect'),
               ('.submit-visual.result.json','action','prepare-visual'),
               ('.submit-visual.result.json','permissions',dict(accessibility=False,postEvents=True,screenRecording=True)))
        for suffix,key,value in cases:
            path=self.ui/(self.tid+suffix);original=path.read_bytes();data=json.loads(original)
            data[key]=value;self.write(path,data);self.reconcile(okay=False);self.write(path,original)
        report=json.loads(self.original_path.read_bytes());report['target_kind']='private'
        self.write(self.original_path,report);self.reconcile(okay=False);self.write(self.original_path,self.before_report)
        self.unchanged()

    def test_event_outside_original_time_window_refuses(self):
        with sqlite3.connect(self.state/'channel.sqlite3') as db:
            event = dict(self.event,create_time=self.now+100)
            db.execute('UPDATE channel_events SET message_json=?',(json.dumps(event),))
        self.reconcile(okay=False); self.unchanged()

    def test_binding_source_and_native_identity_conflicts_refuse(self):
        for suffix,key,value in (('.submit-visual.result.json','id','other'),
                                 ('.submit-visual.job.json','media',{}),
                                 ('.visual-state.json','stage','prepared')):
            path = self.ui/(self.tid+suffix); original = path.read_bytes()
            data = json.loads(original); data[key] = value; self.write(path,data)
            self.reconcile(okay=False); self.write(path,original)
        self.unchanged()

    def test_private_nonlinked_artifact_boundary(self):
        path = self.ui/(self.tid+'.submit-visual.result.json')
        path.chmod(0o644); self.reconcile(okay=False); path.chmod(0o600)
        alias = self.ui/'alias'; os.link(path,alias)
        self.reconcile(okay=False); alias.unlink()
        moved = self.ui/'moved'; path.rename(moved); path.symlink_to(moved)
        self.reconcile(okay=False); path.unlink(); moved.rename(path)
        self.unchanged()

    def test_changed_owner_control_and_active_surface_refuse(self):
        for sql,undo in (("UPDATE binding SET owner='other'", "UPDATE binding SET owner='synthetic-owner'"),
                         ("UPDATE binding SET control='taken_over'", "UPDATE binding SET control='active'"),
                         ('UPDATE surface SET active_dispatch=1','UPDATE surface SET active_dispatch=NULL')):
            with sqlite3.connect(self.coord) as db: db.execute(sql)
            self.reconcile(okay=False)
            with sqlite3.connect(self.coord) as db: db.execute(undo)
        self.unchanged()

    def test_outgoing_origin_recognizes_new_report_without_manual_epoch_bump(self):
        self.reconcile()
        self.call('coordinator',self.ui,'inbox','reconcile','--conversation-id','synthetic-conversation')
        with sqlite3.connect(self.coord) as db:
            self.assertEqual(db.execute('SELECT epoch FROM binding').fetchone()[0],1)
        self.unchanged()

    def test_outbox_committed_coordinator_not_committed_recovers(self):
        with sqlite3.connect(self.coord) as db:
            db.execute("CREATE TRIGGER synthetic_commit_failure BEFORE UPDATE OF status ON dispatch WHEN NEW.status='done' BEGIN SELECT RAISE(ABORT,'synthetic commit failure'); END")
        self.reconcile(okay=False)
        with sqlite3.connect(self.out) as db:
            self.assertEqual(db.execute('SELECT status FROM outgoing_intent').fetchone()[0],'ui_observed')
        with sqlite3.connect(self.coord) as db:
            self.assertEqual(db.execute('SELECT status FROM dispatch').fetchone()[0],'unknown')
            db.execute('DROP TRIGGER synthetic_commit_failure')
        report = self.reconcile()
        self.assertEqual(report['status'],'ui_observed'); self.unchanged()
        with sqlite3.connect(self.coord) as db:
            self.assertEqual(db.execute('SELECT status FROM dispatch').fetchone()[0],'done')

    def test_report_published_outbox_not_committed_recovers(self):
        with sqlite3.connect(self.out) as db:
            db.execute("CREATE TRIGGER synthetic_commit_failure BEFORE INSERT ON outgoing_observation BEGIN SELECT RAISE(ABORT,'synthetic commit failure'); END")
        self.reconcile(okay=False)
        expected = self.verified_path.read_bytes()
        with sqlite3.connect(self.out) as db:
            self.assertEqual(db.execute('SELECT status FROM outgoing_intent').fetchone()[0],'indeterminate')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outgoing_observation').fetchone()[0],0)
            db.execute('DROP TRIGGER synthetic_commit_failure')
        self.reconcile()
        self.assertEqual(self.verified_path.read_bytes(),expected)
        self.unchanged()


if __name__ == '__main__':
    unittest.main()
