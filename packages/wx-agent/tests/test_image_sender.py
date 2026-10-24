"""Image protocol boundaries also run against the installed wheel, without WeChat."""
import base64
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wx_agent import macos_sender_driver as driver

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


class ImageSenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ui = Path(self.tmp.name)
        self.ui.chmod(0o700)
        self.path = self.ui / '图片 with space.png'
        self.path.write_bytes(PNG)
        self.path.chmod(0o600)
        self.binding = dict(channel_id='channel', session_id='session', account='account',
                            talker='synthetic@chatroom', self_wxid='self')
        self.request = dict(protocol_version=2, request_id='image.1', attempt_number=1,
                            media=driver.image_descriptor(self.path), text='',
                            **{k: v for k, v in self.binding.items() if k != 'self_wxid'})
        self.frame = dict(x=0, y=0, width=880, height=640)
        self.config = dict(binding=self.binding, ui_dir=self.ui, target_group='Synthetic Group',
                           target_name='Synthetic Group', expected_pid=42,
                           window_size=dict(width=880, height=640), conversation_kind='group',
                           composer_rect=dict(x=320, y=500, width=400, height=80),
                           button_point=dict(x=800, y=610), search_point=dict(x=155,y=28),
                           conversation_list_rect=dict(x=60,y=55,width=240,height=500))
        self.actions = []
        self.native_proof = True
        self.mutate_after_prepare = False
        self.permissions = True

    def bridge(self, config, tid, action, command, deadline, seconds):
        self.actions.append(action)
        if action == 'inspect':
            return dict(weChatPID=42, windows=[dict(id=20,frame=self.frame)],
                        session=dict(onConsole=True, locked=False),
                        permissions=dict(accessibility=self.permissions,screenRecording=self.permissions,
                                         postEvents=self.permissions))
        if action == 'focus-group':
            driver.private_create(self.ui / (tid+'.focus-evidence.json'),
                dict(id=tid,targetGroup=config['target_group'],targetKind='group',expectedPID=42,
                     expectedWindowID=20,expectedFrame=self.frame,source='search',category='group-chats',selectedAt=1))
            return dict(focused=True,alreadySelected=False,source='search',targetKind='group',
                        focusEvidenceID=tid,category='group-chats')
        self.assertEqual(command['media'], self.request['media'])
        self.assertNotIn('text',command)
        key = 'media_prepared' if action == 'prepare-visual' else 'media_submitted'
        result = {'prepared' if action == 'prepare-visual' else 'submitted': True}
        if self.native_proof:
            result[key] = dict(self.request['media'],exact_pixels=True,pixel_sha256='a'*64,composer_pixel_sha256='a'*64,width=1,height=1)
            if action == 'submit-visual':
                capture=self.ui/(tid+'.submit-after-click.png')
                capture.write_bytes(PNG);capture.chmod(0o600)
                result[key].update(post_click_capture=str(capture),post_click_header_verified=True,
                                   post_click_capture_sha256=hashlib.sha256(PNG).hexdigest())
        if action == 'prepare-visual' and self.mutate_after_prepare:
            self.path.write_bytes(PNG+b'changed')
        return result

    def run_driver(self):
        with mock.patch.object(driver, 'status', return_value=5), \
             mock.patch.object(driver, '_helper_is_live', return_value=False), \
             mock.patch.object(driver, 'verify_target'), \
             mock.patch.object(driver, 'bridge', side_effect=self.bridge), \
             mock.patch.object(driver, 'channel', return_value=''), \
             mock.patch.object(driver, 'observe', return_value=6):
            return driver.run(self.request,self.config)

    def test_image_protocol_refuses_missing_changed_mixed_recovery_and_dirty_draft(self):
        for change in (dict(protocol_version=1),dict(media=None),dict(text='path.png'),
                       dict(recovery_from_attempt=1,attempt_number=2),dict(talker='wrong')):
            with self.subTest(change=change),self.assertRaises(driver.Refused):
                driver.request_ok(dict(self.request,**change),self.config)
        with self.assertRaises(driver.Refused):
            driver.request_ok(self.request,dict(self.config,expected_draft='user draft'))
        self.path.write_bytes(PNG+b'changed')
        with self.assertRaisesRegex(driver.Refused,'differs'):
            driver.request_ok(self.request,self.config)
        self.assertEqual(self.actions,[])

    def test_invalid_files_symlinks_and_unreadable_frozen_permissions(self):
        for raw in (b'',b'GIF89a',b'not image'):
            self.path.write_bytes(raw)
            with self.assertRaises(driver.Refused): driver.image_descriptor(self.path)
        self.path.write_bytes(PNG)
        self.path.chmod(0o644)
        with self.assertRaises(driver.Refused): driver.validate_media(self.request['media'])
        link=self.ui/'link.png';link.symlink_to(self.path)
        with self.assertRaises(driver.Refused): driver.image_descriptor(link)
        with self.assertRaises(OSError): driver.image_descriptor(self.ui/'absent.png')

    def test_native_pixel_proof_required_before_submit_and_no_replay_after_restart(self):
        self.native_proof=False
        result=self.run_driver()
        self.assertEqual(result['status'],'indeterminate')
        self.assertNotIn('submit-visual',self.actions)
        with self.assertRaises(driver.Uncertain): self.run_driver()
        self.assertNotIn('submit-visual',self.actions)

    def test_frozen_mutation_after_prepare_prevents_submit(self):
        self.mutate_after_prepare=True
        result=self.run_driver()
        self.assertEqual(result['status'],'indeterminate')
        self.assertNotIn('submit-visual',self.actions)

    def test_image_lock_and_permissions_fail_closed(self):
        with (self.ui/'sender.lock').open('a+') as lock:
            (self.ui/'sender.lock').chmod(0o600)
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(driver.Refused,'busy'): self.run_driver()
        self.assertEqual(self.actions,[])
        self.permissions=False
        self.assertEqual(self.run_driver()['status'],'failed')
        self.assertNotIn('focus-group',self.actions)

    def test_image_success_requires_media_specific_native_evidence(self):
        result=self.run_driver()
        self.assertEqual(result['status'],'ui_observed')
        self.assertEqual(self.actions,['inspect','focus-group','prepare-visual','submit-visual'])
        report=json.loads(Path(result['evidence']).read_text())
        self.assertTrue(report['media_prepared']['exact_pixels'])
        self.assertTrue(report['media_submitted']['exact_pixels'])
        with self.assertRaises(driver.Uncertain): self.run_driver()
        self.assertEqual(self.actions.count('submit-visual'),1)

    def image_event(self,cursor):
        return dict(cursor=cursor,kind='message_observed',direction='outgoing',
                    **{k:v for k,v in self.request.items() if k in ('channel_id','session_id','account','talker')},
                    message=dict(talker=self.binding['talker'],sender='self',msg_type=3,
                                 content={'Image':{'md5':'server-may-reencode'}}))

    def test_event_association_checks_kind_talker_sender_direction_and_fresh_cursor(self):
        event=self.image_event(6)
        self.assertTrue(driver.matching_event(event,self.request,'self',5))
        for changes in (dict(cursor=5),dict(talker='wrong'),dict(direction='incoming'),
                        dict(message=dict(event['message'],sender='other')),
                        dict(message=dict(event['message'],msg_type=1,content={'Text':str(self.path)}))):
            self.assertFalse(driver.matching_event(dict(event,**changes),self.request,'self',5))

    def test_multiple_new_images_are_ambiguous_not_success(self):
        def channel(config,deadline,*args):
            return '\n'.join(json.dumps(self.image_event(c)) for c in (6,7)) if args[0]=='events' else ''
        with mock.patch.object(driver,'channel',side_effect=channel), \
             self.assertRaisesRegex(driver.Uncertain,'ambiguous'):
            driver.observe(self.config,self.request,5,driver.time.monotonic()+1)

    def test_image_event_content_proof_binds_request_cursor_and_frozen_digest(self):
        self.config.update(wx_cli='/synthetic/wx-cli',channel_state_dir='/synthetic/channel')
        proof=dict(verified=True,request_id=self.request['request_id'],cursor=6,
                   media_sha256=self.request['media']['sha256'],comparison='bounded-reencode')
        with mock.patch.object(driver,'call',return_value=json.dumps(proof)) as call:
            self.assertEqual(driver.verify_image_event(self.config,self.request,6,100),proof)
            self.assertIn('verify-image-event',call.call_args[0][0])
        for changes in (dict(verified=False),dict(cursor=7),dict(request_id='other'),
                        dict(media_sha256='0'*64),dict(comparison='type-only')):
            with mock.patch.object(driver,'call',return_value=json.dumps(dict(proof,**changes))), \
                 self.assertRaises(driver.Uncertain):
                driver.verify_image_event(self.config,self.request,6,100)

    def test_one_new_image_without_verified_content_does_not_report_success(self):
        tick=[0]
        def now():
            tick[0]+=.2
            return tick[0]
        def channel(config,deadline,*args):
            return json.dumps(self.image_event(6)) if args[0]=='events' else ''
        with mock.patch.object(driver,'channel',side_effect=channel), \
             mock.patch.object(driver.time,'monotonic',side_effect=now), \
             mock.patch.object(driver.time,'sleep'), \
             mock.patch.object(driver,'verify_image_event',side_effect=driver.Refused('not ready')):
            self.assertIsNone(driver.observe(self.config,self.request,5,5))
