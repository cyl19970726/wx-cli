"""Synthetic, read-only historical two-attempt clipboard chain proof."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wx_agent import conversations, dispatch, macos_sender_driver as driver
from scripts import test_clipboard_archive_driver as archive_fixture


class ClipboardChainDriverTests(unittest.TestCase):
    def setUp(self):
        fixture = archive_fixture.ClipboardArchiveDriverTests(
            'test_exact_original_trace_is_archive_only_and_does_not_change_files')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.ui, self.config, self.base = fixture.ui, fixture.config, fixture.tid
        old = dict(fixture.request)
        old.pop('content_kind')
        old.pop('clipboard_archive_protocol_version')
        old.update(attempt_number=2, recovery_from_attempt=1,
                   clipboard_recovery_protocol_version=1)
        a2 = self.base + '-a2'
        driver.private_create(self.ui / (a2 + '.client-result.json'),
            {'id': a2, 'attempt_number': 2, 'request_id': old['request_id'],
             'stage': 'started', 'status': 'indeterminate',
             'channel_id': old['channel_id'], 'session_id': old['session_id'],
             'started_at': '2026-01-01T00:00:00+00:00', 'recovery_from_attempt': 1})
        with self.guards():
            self.parent = Path(driver.prove_unsent(old, self.config, 2,
                                                   driver.time.monotonic() + 5))
        self.request = dict(old)
        self.request.pop('clipboard_recovery_protocol_version')
        self.request.update(attempt_number=3, recovery_from_attempt=2,
                            clipboard_chain_recovery_protocol_version=1)
        for path in list(self.ui.iterdir()):
            if (not path.name.startswith(self.base) or path.name.startswith(a2)
                    or path == self.parent):
                continue
            target = self.ui / path.name.replace(self.base, a2, 1)
            if path.suffix == '.png':
                target.write_bytes(path.read_bytes())
                target.chmod(0o600)
                continue
            value = driver.private_read(path)
            def replace(obj):
                if isinstance(obj, str):
                    return obj.replace(self.base, a2)
                if isinstance(obj, list):
                    return [replace(item) for item in obj]
                if isinstance(obj, dict):
                    return {key: replace(item) for key, item in obj.items()}
                return obj
            value = replace(value)
            if path.name == self.base + '.client-result.json':
                value.update(attempt_number=2, recovery_from_attempt=1,
                             recovery_proof=str(self.parent))
            if path.name == self.base + '.prepare-visual.result.json':
                value['error'] = driver.COMPOSER_COPY_ARCHIVE_ERROR
            if target.exists():
                driver.private_update(target, value)
            else:
                driver.private_create(target, value)
        # Copy mismatch can stop before the first empty-check capture.
        (self.ui / (a2 + '.old-draft-empty-check.png')).unlink()
        report = driver.private_read(self.ui / (a2 + '.client-result.json'))
        report['error'] = 'composer draft mismatch'
        driver.private_update(self.ui / (a2 + '.client-result.json'), report)

    def guards(self, live=False):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(driver, '_guard_helper'))
        stack.enter_context(mock.patch.object(driver, 'status', return_value=0))
        stack.enter_context(mock.patch.object(driver, '_helper_is_live', return_value=live))
        bridge = stack.enter_context(mock.patch.object(driver, 'bridge'))
        self.addCleanup(bridge.assert_not_called)
        return stack

    def put(self, name, value):
        path = self.ui / name
        if path.exists():
            driver.private_update(path, value)
        else:
            driver.private_create(path, value)
        return path

    def started(self, **changes):
        report = {'id': self.base + '-a3', 'attempt_number': 3,
                  'request_id': self.request['request_id'], 'stage': 'started',
                  'status': 'indeterminate', 'channel_id': self.request['channel_id'],
                  'session_id': self.request['session_id'],
                  'started_at': '2026-01-01T00:00:00+00:00', 'recovery_from_attempt': 2}
        report.update(changes)
        return self.put(self.base + '-a3.client-result.json', report)

    def preflight(self, request=None, config=None):
        with self.guards():
            return driver.clipboard_chain_recovery_preflight(request or self.request,
                                                            config or self.config)

    def test_valid_preflight_no_effects_and_current_started_proof(self):
        before = {p.name: p.read_bytes() for p in self.ui.iterdir()}
        self.assertEqual(self.preflight(), {'protocol_version': 1,
            'clipboard_chain_recovery_protocol_version': 1, 'unsent_proven': True})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.ui.iterdir()})
        self.started()
        with self.guards():
            path = driver.prove_unsent(self.request, self.config, 3,
                                       driver.time.monotonic() + 5)
        proof = driver.private_read(Path(path))
        self.assertEqual((proof['from_attempt'], proof['attempt_number'],
                          proof['prior_focus_mode']),
                         (2, 3, 'clipboard-first-two-before-paste'))
        self.assertEqual(proof['text_sha256'],
                         driver.hashlib.sha256(self.request['text'].encode()).hexdigest())
        self.assertEqual(self.parent.read_bytes(), before[self.parent.name])
        with self.guards(), self.assertRaises(driver.Refused):
            driver.prove_unsent(self.request, self.config, 3, driver.time.monotonic() + 5)

    def test_request_types_extras_and_parent_binding_refuse(self):
        for change in ({'attempt_number': True}, {'attempt_number': 2},
                       {'recovery_from_attempt': True}, {'recovery_from_attempt': 1},
                       {'protocol_version': True}, {'clipboard_chain_recovery_protocol_version': True},
                       {'text': 'changed'}, {'talker': 'wrong'}, {'media': {}},
                       {'content_kind': 'text'}):
            with self.subTest(change=change), self.assertRaises(driver.Refused):
                self.preflight(dict(self.request, **change))
        original = self.parent.read_bytes()
        for change in ({'prior_focus_mode': 'wrong'}, {'text_sha256': '0'*64},
                       {'from_attempt': True}, {'attempt_number': 3}, {'request_id': 'wrong'}):
            value = json.loads(original)
            value.update(change)
            self.put(self.parent.name, value)
            with self.subTest(change=change), self.assertRaises(driver.Refused):
                self.preflight()
        self.parent.write_bytes(original)

    def test_both_native_traces_and_every_effect_are_required(self):
        for name, change in ((self.base + '-a2.prepare-visual.result.json', {'error': 'other'}),
                             (self.base + '.prepare-visual.result.json', {'error': 'other'}),
                             (self.base + '-a2.visual-state.json', {'stage': 'prepared'}),
                             (self.base + '-a2.client-result.json', {'recovery_from_attempt': 3}),
                             (self.base + '.client-result.json', {'recovery_proof': 'wrong'})):
            path = self.ui / name
            original = path.read_bytes()
            value = json.loads(original)
            value.update(change)
            self.put(name, value)
            with self.subTest(name=name), self.assertRaises(driver.Refused):
                self.preflight()
            path.write_bytes(original)
        for suffix in ('.replace-draft-before-paste.png', '.new-draft-before-copy.png',
                       '.submit-attempted.json', '.submit-visual.job.json',
                       '.late-observation.json', '.unknown.json', '-a4.client-result.json'):
            for prefix in (self.base, self.base + '-a2'):
                path = self.ui / (prefix + suffix)
                path.write_bytes(b'present')
                path.chmod(0o600)
                with self.subTest(name=path.name), self.assertRaises(driver.Refused):
                    self.preflight()
                path.unlink()

    def test_changed_file_live_helper_and_current_effect_refuse(self):
        with self.guards(live=True), self.assertRaises(driver.Refused):
            driver.clipboard_chain_recovery_preflight(self.request, self.config)
        reference = self.fixture.reference
        original = reference.read_bytes()
        reference.write_bytes(original + b'changed')
        with self.assertRaises(driver.Refused):
            self.preflight()
        reference.write_bytes(original)
        self.started(stage='inspected')
        with self.guards(), self.assertRaises(driver.Refused):
            driver.prove_unsent(self.request, self.config, 3, driver.time.monotonic() + 5)

    def test_missing_second_trace_and_changed_snapshot_refuse(self):
        path = self.ui / (self.base + '-a2.old-draft-copy-ready.png')
        raw = path.read_bytes()
        path.unlink()
        with self.assertRaises(driver.Refused):
            self.preflight()
        path.write_bytes(raw)
        path.chmod(0o600)
        calls = []
        def changing_status(*_):
            calls.append(True)
            if len(calls) == 2:
                report = self.ui / (self.base + '-a2.client-result.json')
                value = driver.private_read(report)
                value['elapsed_seconds'] = 999
                driver.private_update(report, value)
            return 0
        with self.guards(), mock.patch.object(driver, 'status', side_effect=changing_status), \
             self.assertRaisesRegex(driver.Refused, 'changed during proof'):
            driver.clipboard_chain_recovery_preflight(self.request, self.config)

    def test_third_start_must_have_exact_nine_fields(self):
        for changes in ({'recovery_from_attempt': True}, {'stage': 'inspected'},
                        {'request_id': 'different'}, {'recovery_proof': 'already-written'}):
            path = self.started(**changes)
            with self.subTest(changes=changes), self.guards(), self.assertRaises(driver.Refused):
                driver.prove_unsent(self.request, self.config, 3,
                                    driver.time.monotonic() + 5)
            path.unlink()

    def test_historical_focus_modes_frames_and_references_are_independent(self):
        a2 = self.base + '-a2'
        frame = {'x': 12, 'y': 16, 'width': 880, 'height': 640}
        for suffix in ('-inspect.result.json', '-focus.focus-group.job.json',
                       '-focus.focus-evidence.json', '.prepare-visual.job.json',
                       '.visual-state.json'):
            path = self.ui / (a2 + suffix)
            value = driver.private_read(path)
            if suffix == '-inspect.result.json':
                value['windows'][0]['frame'] = frame
            else:
                value['expectedFrame'] = frame
            self.put(path.name, value)
        for suffix in ('-focus.result.json', '-focus.focus-evidence.json'):
            path = self.ui / (a2 + suffix)
            value = driver.private_read(path)
            value.update(source='search', category='group-chats')
            value.pop('headerConfirmations', None)
            if suffix == '-focus.result.json':
                value['alreadySelected'] = False
            self.put(path.name, value)
        report = self.ui / (a2 + '.client-result.json')
        value = driver.private_read(report)
        value['focus_category'] = 'group-chats'
        self.put(report.name, value)
        new_reference = self.ui / 'second-empty.png'
        new_reference.write_bytes(self.fixture.reference.read_bytes() + b'second')
        new_reference.chmod(0o600)
        descriptor = {'path': str(new_reference),
                      'sha256': hashlib.sha256(new_reference.read_bytes()).hexdigest()}
        for suffix in ('.prepare-visual.job.json', '.visual-state.json'):
            path = self.ui / (a2 + suffix)
            value = driver.private_read(path)
            value['emptyComposerReference'] = descriptor
            self.put(path.name, value)
        current_reference = self.ui / 'current-empty.png'
        current_reference.write_bytes(self.fixture.reference.read_bytes() + b'current')
        current_reference.chmod(0o600)
        current = dict(self.config, empty_composer_reference={
            'path': str(current_reference),
            'sha256': hashlib.sha256(current_reference.read_bytes()).hexdigest()})
        self.assertTrue(self.preflight(config=current)['unsent_proven'])

    def test_capability_is_separate_from_v1(self):
        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(driver.main(['--clipboard-chain-recovery-capabilities']), 0)
        self.assertEqual(json.loads(out.getvalue()), {'protocol_version': 1,
            'clipboard_chain_recovery_protocol_version': 1,
            'first_two_text_before_paste_only': True, 'max_attempt': 3})
        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(driver.main(['--clipboard-recovery-capabilities']), 0)
        self.assertEqual(json.loads(out.getvalue())['max_attempt'], 2)


class ClipboardChainDispatchTests(unittest.TestCase):
    def args(self):
        return ['--config', '/synthetic/settings.json', 'recover-clipboard-chain',
                '--dispatch-id', '4', '--request-id', 'original-synthetic',
                '--expected-attempt', '2', '--owner', 'original-owner',
                '--expected-epoch', '3', '--reason', 'reviewed two exact failures']

    def test_route_once_and_no_fallback(self):
        settings = {'wx_cli': '/synthetic/wx-cli'}
        with mock.patch.object(conversations, 'config', return_value=settings), \
             mock.patch.object(dispatch, 'root_from_settings', return_value=Path('/synthetic/ui')), \
             mock.patch.object(dispatch, 'call', return_value={'surface_paused': True}) as call, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.main(self.args()), 0)
        call.assert_called_once_with(settings['wx_cli'], Path('/synthetic/ui'), 'dispatch',
            'recover-clipboard-chain', '--dispatch-id', 4, '--request-id', 'original-synthetic',
            '--expected-attempt', 2, '--owner', 'original-owner', '--expected-epoch', 3,
            '--reason', 'reviewed two exact failures')
        with mock.patch.object(conversations, 'config', return_value=settings), \
             mock.patch.object(dispatch, 'root_from_settings', return_value=Path('/synthetic/ui')), \
             mock.patch.object(dispatch, 'call', side_effect=dispatch.DispatchError('refused')) as call, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.main(self.args()), 1)
        self.assertEqual(call.call_count, 1)


if __name__ == '__main__':
    unittest.main()
