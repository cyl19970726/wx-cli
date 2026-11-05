"""A known first text paste must keep its draft and never inherit submit permission."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock
from wx_agent import macos_sender_driver as driver
from scripts import test_macos_sender_driver as fixtures


class PrepareHeaderProofTests(unittest.TestCase):
    def fixture(self,directory):
        req,cfg,tid=fixtures.ImageAbandonProofTests.fixture(self,directory)
        req.pop('abandon_protocol_version');req.pop('content_kind')
        req.update(text='Original frozen body\n正文',attempt_number=2,recovery_from_attempt=1)
        cfg.update(expected_draft='',composer_rect={'x':320,'y':500,'width':400,'height':80},button_point={'x':800,'y':610})
        ui=Path(directory);focus=tid+'-focus';frame={'x':100,'y':100,'width':880,'height':640}
        permissions={'accessibility':True,'postEvents':True,'screenRecording':True}
        (ui/(focus+'.focus-polls.json')).unlink()
        p=ui/(focus+'.focus-group.job.json');v=driver.private_read(p);v['allowCurrentHeader']=True;driver.private_update(p,v)
        driver.private_update(ui/(focus+'.result.json'),{'id':focus,'action':'focus-group','ok':True,
            'focused':True,'alreadySelected':True,'source':'current-header','targetKind':'group',
            'focusEvidenceID':focus,'category':'group-header','headerConfirmations':2,'permissions':permissions})
        driver.private_create(ui/(focus+'.focus-evidence.json'),{'id':focus,'targetGroup':cfg['target_group'],
            'targetKind':'group','expectedPID':42,'expectedWindowID':21,'expectedFrame':frame,
            'source':'current-header','category':'group-header','headerConfirmations':2,'selectedAt':1})
        report=driver.private_read(ui/(tid+'.client-result.json'));report.update(stage='group_focused',
            error='target group header mismatch',target_kind='group',focus_evidence_id=focus,focus_category='group-header')
        driver.private_update(ui/(tid+'.client-result.json'),report)
        job=dict(driver._prepare_command(req,cfg,21,frame,focus),id=tid,action='prepare-visual')
        driver.private_create(ui/(tid+'.prepare-visual.job.json'),job)
        driver.private_create(ui/(tid+'.prepare-visual.result.json'),{'id':tid,'ok':False,
            'error':driver.PREPARE_HEADER_REJECTION_ERROR,'permissions':permissions})
        state=dict(job,stage='prepare-attempted');state.pop('action');driver.private_create(ui/(tid+'.visual-state.json'),state)
        for suffix in ['-focus.focus-initial','-focus.current-header-confirmed','.prepare-start',
                '.old-draft-before-click','.old-draft-before-select','.old-draft-before-copy','.old-draft-copy-ready',
                '.old-draft-empty-check','.replace-draft-before-paste','.new-draft-before-click']:
            p=ui/(tid+suffix+'.png');p.write_bytes(b'synthetic saved private frame');p.chmod(0o600)
        return req,cfg,tid

    def proof(self,req,cfg,persist=False):
        with mock.patch.object(driver,'_helper_is_live',return_value=False),mock.patch.object(driver,'status'), \
             mock.patch.object(driver,'bridge',side_effect=AssertionError('proof cannot operate UI')):
            if persist:return driver.prove_unsent(req,cfg,2,driver.time.monotonic()+20)
            return driver.prepare_header_recovery_preflight(req,cfg)

    def test_readonly_proof_preserves_original_text_and_trace(self):
        with tempfile.TemporaryDirectory() as d:
            req,cfg,tid=self.fixture(d);before={p.name:p.read_bytes() for p in Path(d).iterdir()}
            self.assertEqual(self.proof(req,cfg),{'protocol_version':1,'prepare_header_recovery_protocol_version':1,'unsent_proven':True})
            self.assertEqual(before,{p.name:p.read_bytes() for p in Path(d).iterdir()})
            result=driver.private_read(Path(self.proof(req,cfg,persist=True)))
            self.assertEqual(result['recovery_draft_sha256'],hashlib.sha256(req['text'].encode()).hexdigest())

    def test_submit_prepared_later_copy_wrong_text_and_other_error_refuse(self):
        for change in ['submit','prepared','later-copy','wrong-text','wrong-error','later-attempt','draft-config','live']:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as d:
                req,cfg,tid=self.fixture(d);ui=Path(d)
                if change=='submit':driver.private_create(ui/(tid+'.submit-attempted.json'),{})
                elif change=='prepared':
                    p=ui/(tid+'.visual-state.json');v=driver.private_read(p);v['stage']='prepared';driver.private_update(p,v)
                elif change=='later-copy':driver.private_create(ui/(tid+'.new-draft-before-copy.png'),{})
                elif change=='wrong-text':req['text']='Changed original body'
                elif change=='wrong-error':
                    p=ui/(tid+'.prepare-visual.result.json');v=driver.private_read(p);v['error']='other header error';driver.private_update(p,v)
                elif change=='later-attempt':req.update(attempt_number=3,recovery_from_attempt=2)
                elif change=='draft-config':cfg['expected_draft']='user draft'
                if change=='live':
                    with mock.patch.object(driver,'_helper_is_live',return_value=True),self.assertRaises(driver.Refused):
                        driver.prepare_header_recovery_preflight(req,cfg)
                else:
                    with self.assertRaises(driver.Refused):self.proof(req,cfg)

    def test_recovery_prepares_only_against_exact_known_original_draft(self):
        with tempfile.TemporaryDirectory() as d:
            req,cfg,tid=self.fixture(d);proof=Path(self.proof(req,cfg,persist=True));captured=[]
            @contextmanager
            def locked(_):yield
            def bridge(config,ident,action,command,*_):
                if action=='prepare-visual':captured.append(command);raise driver.Refused('synthetic stop before effects')
                return {}
            with mock.patch.object(driver,'desktop_lock',locked),mock.patch.object(driver,'status'), \
                    mock.patch.object(driver,'_guard_helper'),mock.patch.object(driver,'prove_unsent',return_value=str(proof)), \
                    mock.patch.object(driver,'verify_target'),mock.patch.object(driver,'bridge',side_effect=bridge), \
                    mock.patch.object(driver,'inspect_ok',return_value=(21,{'x':100,'y':100,'width':880,'height':640})), \
                    mock.patch.object(driver,'focus_result_ok',return_value={'category':'group-header'}):
                driver.run(req,cfg)
            self.assertEqual(len(captured),1)
            self.assertEqual(captured[0]['expectedDraft'],req['text'])
            self.assertEqual(captured[0]['text'],req['text'])
            self.assertEqual(cfg['expected_draft'],'')


if __name__=='__main__':unittest.main()
