"""Strict no-effect proof and one-shot dispatcher for clipboard recovery."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from wx_agent import dispatch, macos_sender_driver as driver
from wx_agent import conversations
from scripts import test_clipboard_archive_driver as archive_fixture


class ClipboardRecoveryDriverTests(unittest.TestCase):
    def setUp(self):
        self.fixture = archive_fixture.ClipboardArchiveDriverTests('test_exact_original_trace_is_archive_only_and_does_not_change_files')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.request = dict(self.fixture.request)
        self.request.pop('content_kind')
        self.request.pop('clipboard_archive_protocol_version')
        self.request.update(attempt_number=2, recovery_from_attempt=1,
                            clipboard_recovery_protocol_version=1)

    def guards(self, live=False):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(driver, '_guard_helper'))
        stack.enter_context(mock.patch.object(driver, 'status', return_value=0))
        stack.enter_context(mock.patch.object(driver, '_helper_is_live', return_value=live))
        bridge = stack.enter_context(mock.patch.object(driver, 'bridge'))
        self.addCleanup(bridge.assert_not_called)
        return stack

    def current(self, **changes):
        report = {'id': self.fixture.tid + '-a2', 'attempt_number': 2,
                  'request_id': self.request['request_id'], 'stage': 'started',
                  'status': 'indeterminate', 'channel_id': self.request['channel_id'],
                  'session_id': self.request['session_id'],
                  'started_at': '2026-01-01T00:00:00+00:00', 'recovery_from_attempt': 1}
        report.update(changes)
        self.fixture.write('-a2.client-result.json', report)

    def test_readonly_preflight_preserves_every_original_file(self):
        before = {p.name: p.read_bytes() for p in self.fixture.ui.iterdir()}
        with self.guards():
            proof = driver.clipboard_recovery_preflight(self.request, self.config)
        self.assertEqual(proof, {'protocol_version': 1, 'clipboard_recovery_protocol_version': 1,
                                'unsent_proven': True})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.fixture.ui.iterdir()})

    def test_persisted_proof_is_bound_to_original_text_and_attempt_two(self):
        self.current()
        before = {p.name: p.read_bytes() for p in self.fixture.ui.iterdir()}
        with self.guards():
            path = driver.prove_unsent(self.request, self.config, 2, driver.time.monotonic()+5)
        value = json.loads(Path(path).read_text())
        self.assertEqual((value['from_attempt'], value['attempt_number'], value['transaction_id']),
                         (1, 2, self.fixture.tid))
        self.assertEqual(value['text_sha256'], driver.hashlib.sha256(self.request['text'].encode()).hexdigest())
        for name, raw in before.items():
            self.assertEqual((self.fixture.ui/name).read_bytes(), raw)
        with self.guards(), self.assertRaises((driver.Refused, OSError)):
            driver.prove_unsent(self.request, self.config, 2, driver.time.monotonic()+5)

    def test_copy_mismatch_without_empty_check_has_same_strict_recovery(self):
        self.fixture.mutate('.prepare-visual.result.json', error=driver.COMPOSER_COPY_ARCHIVE_ERROR)
        (self.fixture.ui/(self.fixture.tid+'.old-draft-empty-check.png')).unlink()
        with self.guards():
            self.assertTrue(driver.clipboard_recovery_preflight(self.request,self.config)['unsent_proven'])

    def test_protocol_extra_fields_boolean_numbers_and_other_attempts_refuse(self):
        for change in ({'attempt_number':3},{'clipboard_recovery_protocol_version':True},
                       {'protocol_version':True},{'recovery_from_attempt':True},
                       {'content_kind':'text'},{'media':{}},{'publisher':'other'}):
            with self.subTest(change=change),self.guards(),self.assertRaises(driver.Refused):
                driver.clipboard_recovery_preflight(dict(self.request,**change),self.config)

    def test_changed_text_binding_and_current_nonempty_draft_refuse(self):
        for change in ({'text':'a different original'}, {'talker':'other@chatroom'}):
            with self.subTest(change=change),self.guards(),self.assertRaises(driver.Refused):
                driver.clipboard_recovery_preflight(dict(self.request,**change),self.config)
        with self.guards(),self.assertRaises(driver.Refused):
            driver.clipboard_recovery_preflight(self.request,dict(self.config,expected_draft='\n'))

    def test_preflight_never_accepts_existing_second_attempt_or_submit(self):
        for suffix in ('-a2.client-result.json','.replace-draft-before-paste.png',
                       '.new-draft-before-copy.png','.submit-visual.job.json','.submit-attempted.json'):
            with self.subTest(suffix=suffix):
                p=self.fixture.ui/(self.fixture.tid+suffix);p.write_bytes(b'{}');p.chmod(0o600)
                with self.guards(),self.assertRaises(driver.Refused):
                    driver.clipboard_recovery_preflight(self.request,self.config)
                p.unlink()

    def test_current_start_report_is_the_only_allowed_new_artifact(self):
        for changes in ({'stage':'inspected'},{'status':'failed'},{'recovery_from_attempt':True},
                        {'request_id':'other'},{'error':'already failed'}):
            self.current(**changes)
            with self.subTest(changes=changes),self.guards(),self.assertRaises(driver.Refused):
                driver.prove_unsent(self.request,self.config,2,driver.time.monotonic()+5)

    def test_current_report_cannot_whitelist_an_arbitrary_file(self):
        self.current()
        original={k:v for k,v in self.fixture.request.items()}
        with self.guards(),self.assertRaises(driver.Refused):
            driver.clipboard_rejection_preflight(original,self.config,'../../elsewhere.json')

    def test_live_helper_and_historical_reference_drift_refuse(self):
        with self.guards(live=True),self.assertRaises(driver.Refused):
            driver.clipboard_recovery_preflight(self.request,self.config)
        self.fixture.reference.write_bytes(self.fixture.reference.read_bytes()+b'drift')
        with self.guards(),self.assertRaises(driver.Refused):
            driver.clipboard_recovery_preflight(self.request,self.config)


class ClipboardRecoveryDispatchTests(unittest.TestCase):
    def args(self):
        return ['--config','/synthetic/settings.json','recover-clipboard-rejection',
                '--dispatch-id','4','--request-id','original-synthetic',
                '--expected-attempt','1','--owner','original-owner','--expected-epoch','3',
                '--reason','reviewed exact first text pre-paste failure']

    def test_recovery_routes_once_with_explicit_original_identity(self):
        settings={'wx_cli':'/synthetic/wx-cli'}
        with mock.patch.object(conversations,'config',return_value=settings), \
                mock.patch.object(dispatch,'root_from_settings',return_value=Path('/synthetic/ui')), \
                mock.patch.object(dispatch,'call',return_value={'surface_paused':True}) as call, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.main(self.args()),0)
        call.assert_called_once_with(settings['wx_cli'],Path('/synthetic/ui'),'dispatch',
            'recover-clipboard-rejection','--dispatch-id',4,'--request-id','original-synthetic',
            '--expected-attempt',1,'--owner','original-owner','--expected-epoch',3,
            '--reason','reviewed exact first text pre-paste failure')

    def test_failed_recovery_never_falls_back_or_retries(self):
        with mock.patch.object(conversations,'config',return_value={'wx_cli':'/synthetic/wx-cli'}), \
                mock.patch.object(dispatch,'root_from_settings',return_value=Path('/synthetic/ui')), \
                mock.patch.object(dispatch,'call',side_effect=dispatch.DispatchError('proof refused')) as call, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.main(self.args()),1)
        self.assertEqual(call.call_count,1)

    def test_original_owner_is_required_instead_of_inferred(self):
        args=self.args(); at=args.index('--owner');del args[at:at+2]
        with mock.patch.object(dispatch,'call') as call,contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            dispatch.main(args)
        self.assertEqual(raised.exception.code,2);call.assert_not_called()


if __name__=='__main__':
    unittest.main()
