"""Archive-only native evidence checks; no clipboard, GUI or send is used."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from wx_agent import macos_sender_driver as driver


class ClipboardArchiveDriverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ui = Path(temp.name).resolve()
        self.ui.chmod(0o700)
        (self.ui / 'sender.lock').touch(mode=0o600)
        self.config = {'target_name': 'Synthetic Group', 'target_group': 'Synthetic Group',
            'conversation_kind': 'group', 'expected_draft': '', 'expected_pid': 42,
            'window_size': {'width': 880, 'height': 640}, 'ui_dir': self.ui,
            'binding': {'channel_id': 'synthetic-channel', 'session_id': 'synthetic-session',
                        'account': 'synthetic', 'talker': 'synthetic@chatroom'},
            'composer_rect': {'x': 308, 'y': 497, 'width': 559, 'height': 93},
            'button_point': {'x': 829, 'y': 606},
            'conversation_list_rect': {'x': 60, 'y': 55, 'width': 240, 'height': 570},
            'search_point': {'x': 155, 'y': 28}}
        self.request = {'protocol_version': 1, 'clipboard_archive_protocol_version': 1,
            'content_kind': 'text', 'request_id': 'synthetic-original', 'attempt_number': 1,
            **self.config['binding'], 'text': 'Original synthetic text'}
        self.reference = self.ui / 'old-empty.png'
        self.reference.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + struct.pack('>II', 559, 93))
        self.reference.chmod(0o600)
        self.config['empty_composer_reference'] = {'path': str(self.reference),
            'sha256': hashlib.sha256(self.reference.read_bytes()).hexdigest()}
        self.tid = driver.request_ok(self.request, self.config, 1)
        self.permissions = {'accessibility': True, 'screenRecording': True, 'postEvents': True}
        self.frame = {'x': 0, 'y': 0, 'width': 880, 'height': 640}
        focus = self.tid + '-focus'
        self.write('-inspect.inspect.job.json', {'id': self.tid + '-inspect', 'action': 'inspect'})
        self.write('-inspect.result.json', {'id': self.tid + '-inspect', 'action': 'inspect', 'ok': True,
            'weChatPID': 42, 'windows': [{'id': 20, 'frame': self.frame}], 'permissions': self.permissions,
            'session': {'onConsole': True, 'locked': False}})
        self.write('-focus.focus-group.job.json', {'id': focus, 'action': 'focus-group',
            'targetGroup': 'Synthetic Group', 'targetKind': 'group', 'allowCurrentHeader': True,
            'expectedPID': 42, 'expectedWindowID': 20, 'expectedFrame': self.frame,
            'conversationListRect': self.config['conversation_list_rect'], 'searchPoint': self.config['search_point']})
        self.write('-focus.result.json', {'id': focus, 'action': 'focus-group', 'ok': True,
            'permissions': self.permissions, 'focused': True, 'alreadySelected': True,
            'source': 'current-header', 'category': 'group-header', 'targetKind': 'group',
            'focusEvidenceID': focus, 'headerConfirmations': 2})
        self.write('-focus.focus-evidence.json', {'id': focus, 'targetGroup': 'Synthetic Group',
            'targetKind': 'group', 'expectedPID': 42, 'expectedWindowID': 20, 'expectedFrame': self.frame,
            'source': 'current-header', 'category': 'group-header', 'selectedAt': 1234.5, 'headerConfirmations': 2})
        job = dict(driver._prepare_command(self.request, self.config, 20, self.frame, focus),
                   id=self.tid, action='prepare-visual')
        self.write('.prepare-visual.job.json', job)
        state = dict(job, stage='prepare-attempted'); state.pop('action')
        self.write('.visual-state.json', state)
        self.write('.prepare-visual.result.json', {'id': self.tid, 'ok': False,
            'error': driver.CLIPBOARD_ARCHIVE_ERROR, 'permissions': self.permissions})
        self.write('.client-result.json', {'id': self.tid, 'request_id': self.request['request_id'],
            'attempt_number': 1, 'channel_id': self.request['channel_id'], 'session_id': self.request['session_id'],
            'stage': 'group_focused', 'status': 'indeterminate', 'error': 'composer draft mismatch',
            'target_kind': 'group', 'focus_evidence_id': focus, 'focus_category': 'group-header'})
        for suffix in ('.prepare-start.png', '.old-draft-before-click.png', '.old-draft-before-select.png',
                       '.old-draft-before-copy.png', '.old-draft-copy-ready.png', '.old-draft-empty-check.png'):
            path = self.ui / (self.tid + suffix); path.write_bytes(b'synthetic private frame'); path.chmod(0o600)

    def write(self, suffix, value):
        path = self.ui / (self.tid + suffix)
        if path.exists():
            driver.private_update(path, value)
        else:
            driver.private_create(path, value)

    def mutate(self, suffix, **fields):
        value = driver.private_read(self.ui / (self.tid + suffix)); value.update(fields)
        self.write(suffix, value)

    def prove(self):
        with mock.patch.object(driver, '_guard_helper'), mock.patch.object(driver, 'status', return_value=0), \
                mock.patch.object(driver, '_helper_is_live', return_value=False), \
                mock.patch.object(driver, 'bridge') as bridge:
            result = driver.clipboard_rejection_preflight(self.request, self.config)
        bridge.assert_not_called()
        return result

    def test_exact_original_trace_is_archive_only_and_does_not_change_files(self):
        before = {p.name: p.read_bytes() for p in self.ui.iterdir()}
        result = self.prove()
        self.assertEqual(result, {'protocol_version': 1, 'clipboard_archive_protocol_version': 1,
                                 'clipboard_rejection_proven': True, 'archive_only': True})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.ui.iterdir()})

    def test_current_calibration_can_change_without_adopting_old_trace_for_retry(self):
        new = self.ui / 'new-empty.png'; new.write_bytes(self.reference.read_bytes() + b'new')
        new.chmod(0o600)
        self.config['empty_composer_reference'] = {'path': str(new), 'sha256': hashlib.sha256(new.read_bytes()).hexdigest()}
        self.assertTrue(self.prove()['archive_only'])
        self.reference.write_bytes(self.reference.read_bytes() + b'changed')
        with self.assertRaises(driver.Refused):
            self.prove()

    def test_exact_native_error_never_gains_recovery_authority(self):
        self.assertNotIn(driver.CLIPBOARD_ARCHIVE_ERROR, driver.PRE_SUBMIT_ERRORS)
        recovery = dict(self.request, attempt_number=2, recovery_from_attempt=1)
        with self.assertRaisesRegex(driver.Refused, 'not an approved failure'):
            driver.prove_unsent(recovery, self.config, 2, driver.time.monotonic() + 5, persist=False)

    def test_paste_submit_later_attempt_and_unknown_extra_artifacts_refuse(self):
        for suffix in ('.replace-draft-before-paste.png', '.new-draft-before-copy.png',
                       '.submit-visual.job.json', '.submit-attempted.json', '-a2.client-result.json', '.unrecognized.json'):
            with self.subTest(suffix=suffix):
                path = self.ui / (self.tid + suffix); path.write_bytes(b'present'); path.chmod(0o600)
                with self.assertRaises(driver.Refused):
                    self.prove()
                path.unlink()

    def test_wrong_error_prepared_stage_and_text_change_refuse(self):
        for suffix, change in (('.prepare-visual.result.json', {'error': driver.COMPOSER_COPY_ARCHIVE_ERROR + ' '}),
                               ('.visual-state.json', {'stage': 'prepared'}),
                               ('.prepare-visual.job.json', {'text': 'changed'}),
                               ('.client-result.json', {'event_cursor': 10})):
            with self.subTest(suffix=suffix, change=change):
                path = self.ui / (self.tid + suffix); original = path.read_bytes()
                self.mutate(suffix, **change)
                with self.assertRaises(driver.Refused):
                    self.prove()
                path.write_bytes(original)

    def test_exact_empty_draft_copy_mismatch_accepts_no_empty_check_frames(self):
        self.mutate('.prepare-visual.result.json', error=driver.COMPOSER_COPY_ARCHIVE_ERROR)
        (self.ui / (self.tid + '.old-draft-empty-check.png')).unlink()
        before = {p.name: p.read_bytes() for p in self.ui.iterdir()}
        self.assertTrue(self.prove()['archive_only'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.ui.iterdir()})

    def test_exact_copy_mismatch_with_visual_checks_still_archives_only(self):
        self.mutate('.prepare-visual.result.json', error=driver.COMPOSER_COPY_ARCHIVE_ERROR)
        self.assertTrue(self.prove()['archive_only'])

    def test_copy_mismatch_still_requires_copy_ready_and_contiguous_checks(self):
        self.mutate('.prepare-visual.result.json', error=driver.COMPOSER_COPY_ARCHIVE_ERROR)
        ready = self.ui / (self.tid + '.old-draft-copy-ready.png')
        original = ready.read_bytes(); ready.unlink()
        with self.assertRaises(driver.Refused):
            self.prove()
        ready.write_bytes(original); ready.chmod(0o600)
        (self.ui / (self.tid + '.old-draft-empty-check.png')).unlink()
        late = self.ui / (self.tid + '.old-draft-empty-check-1.png')
        late.write_bytes(b'late frame without first'); late.chmod(0o600)
        with self.assertRaises(driver.Refused):
            self.prove()

    def test_original_clipboard_change_cannot_omit_first_empty_check(self):
        (self.ui / (self.tid + '.old-draft-empty-check.png')).unlink()
        with self.assertRaises(driver.Refused):
            self.prove()

    def test_copy_mismatch_does_not_adopt_a_different_expected_draft(self):
        self.mutate('.prepare-visual.result.json', error=driver.COMPOSER_COPY_ARCHIVE_ERROR)
        self.config['expected_draft'] = '\n\n\n'
        with self.assertRaises(driver.Refused):
            self.prove()

    def test_changed_trace_during_readonly_check_refuses(self):
        calls = []
        def status(*_):
            calls.append(True)
            if len(calls) == 2:
                self.mutate('.client-result.json', elapsed_seconds=999)
        with mock.patch.object(driver, '_guard_helper'), mock.patch.object(driver, 'status', side_effect=status), \
                mock.patch.object(driver, '_helper_is_live', return_value=False):
            with self.assertRaisesRegex(driver.Refused, 'trace changed'):
                driver.clipboard_rejection_preflight(self.request, self.config)

    def test_capability_and_failed_preflight_do_not_call_a_gui(self):
        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(driver.main(['--clipboard-archive-capabilities']), 0)
        self.assertEqual(json.loads(out.getvalue()), {'protocol_version': 1,
            'clipboard_archive_protocol_version': 1, 'archive_only': True, 'before_paste_only': True})


if __name__ == '__main__':
    unittest.main()
