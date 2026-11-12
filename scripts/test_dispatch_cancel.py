"""Compiled coordinator cancellation tests using synthetic managed outboxes."""

import sqlite3
import unittest

from scripts import test_multi_conversation as fixtures


class DispatchCancelTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.MultiConversationTests.setUpClass.__func__)
    cli = fixtures.MultiConversationTests.cli
    coordinator = fixtures.MultiConversationTests.coordinator
    outbox = fixtures.MultiConversationTests.outbox
    queue = fixtures.MultiConversationTests.queue

    def setUp(self):
        fixtures.MultiConversationTests.setUp(self)
        self.outgoing = self.channels['group']['state'] / 'outgoing.sqlite3'
        self.scheduler = self.ui / 'coordinator.sqlite3'
        self.coordinator('dispatch','pause','--reason','synthetic operator cancellation review')

    def cancel(self, request, attempt=0, name='group', okay=True):
        return self.coordinator('dispatch','cancel-request',
            '--conversation-id',self.channels[name]['id'],'--request-id',request,
            '--expected-attempt',attempt,'--reason','user revoked future sending',okay=okay)

    def show_dispatch(self, name, request, okay=True):
        return self.coordinator('dispatch','show','--conversation-id',self.channels[name]['id'],
                                '--request-id',request,okay=okay)

    def test_show_single_id_exact_metadata_and_absent(self):
        queued=self.queue('group','show-one')
        shown=self.show_dispatch('group','show-one')
        self.assertEqual(shown, {
            'conversation_id':self.channels['group']['id'],
            'request_id':'show-one','surface_paused':True,
            'dispatch':{'dispatch_id':queued['dispatch_id'],'status':'queued',
                        'result':None,'operation':'send','epoch':1}})
        self.assertEqual(self.show_dispatch('group','never-queued'), {
            'conversation_id':self.channels['group']['id'],
            'request_id':'never-queued','surface_paused':True,'dispatch':None})
        self.coordinator('dispatch','resume')
        self.assertFalse(self.show_dispatch('group','show-one')['surface_paused'])
        self.show_dispatch('group','../invalid',okay=False)
        self.coordinator('dispatch','show','--conversation-id','unknown_cid',
                         '--request-id','show-one',okay=False)

    def test_show_isolates_identical_request_ids_by_conversation(self):
        group=self.queue('group','shared-request')
        self.assertIsNone(self.show_dispatch('private','shared-request')['dispatch'])
        private=self.queue('private','shared-request')
        group_shown=self.show_dispatch('group','shared-request')
        private_shown=self.show_dispatch('private','shared-request')
        self.assertEqual(group_shown['dispatch']['dispatch_id'],group['dispatch_id'])
        self.assertEqual(private_shown['dispatch']['dispatch_id'],private['dispatch_id'])
        self.assertNotEqual(group_shown['dispatch']['dispatch_id'],private_shown['dispatch']['dispatch_id'])
        self.assertEqual(set(group_shown['dispatch']),{'dispatch_id','status','result','operation','epoch'})

    def terminal(self, request, count, status='indeterminate'):
        self.queue('group',request)
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute('UPDATE outgoing_intent SET status=?,attempt_number=? WHERE request_id=?',
                       (status,count,request))
            for number in range(1,count+1):
                db.execute('INSERT INTO outgoing_attempt(request_id,attempt_number,status,claimed_at,finished_at) VALUES(?,?,?,?,?)',
                           (request,number,status,number,number+1))
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET status='unknown',result='indeterminate' WHERE request_id=?",(request,))

    def legacy_orphan(self, request, status='indeterminate'):
        self.terminal(request,1,status=status)
        with sqlite3.connect(self.scheduler) as db:
            db.execute('DELETE FROM dispatch WHERE request_id=?',(request,))
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute('UPDATE outgoing_intent SET managed_owner=NULL,managed_epoch=NULL WHERE request_id=?',(request,))
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')

    def test_queued_idempotent_and_reconcile_never_revives(self):
        parent=self.queue('group','cancel-queued')
        before=self.outgoing.read_bytes()
        result=self.cancel('cancel-queued')
        self.assertEqual((result['dispatch_id'],result['status'],result['result'],
                          result['outbox_status'],result['already_cancelled']),
                         (parent['dispatch_id'],'done','cancelled_by_user','managed_queued',False))
        self.assertEqual(self.outgoing.read_bytes(),before)
        self.assertTrue(self.cancel('cancel-queued')['already_cancelled'])
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM surface_review WHERE reason LIKE 'cancelled_by_user%'").fetchone()[0],1)
        self.assertEqual(self.coordinator('enqueue','--conversation-id',self.channels['group']['id'],
            '--request-id','cancel-queued','--expected-epoch',1)['status'],'done')
        self.coordinator('reconcile-outbox','--conversation-id',self.channels['group']['id'])
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute("SELECT status,result FROM dispatch WHERE request_id='cancel-queued'").fetchone(),
                             ('done','cancelled_by_user'))
        self.assertFalse(self.log.exists())

    def test_blocked_batch_parent_no_credit_and_other_conversation_untouched(self):
        parent=self.queue('group','cancel-parent',batch_id='cancel-batch',part_index=0)
        child=self.queue('group','cancel-child',batch_id='cancel-batch',part_index=1)
        private=self.queue('private','private-other')
        self.cancel('cancel-parent')
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',(child['dispatch_id'],)).fetchone(),
                             ('blocked','batch_predecessor_not_sent'))
            self.assertEqual(db.execute('SELECT status FROM dispatch WHERE id=?',(private['dispatch_id'],)).fetchone()[0],'queued')
        self.assertEqual(self.cancel('cancel-child')['status'],'done')
        self.assertEqual(parent['status'],'queued')

    def test_orphan_old_epoch_gets_terminal_tombstone(self):
        self.queue('group','cancel-orphan',batch_id='old-batch',part_index=0,priority=2)
        with sqlite3.connect(self.scheduler) as db:
            db.execute("DELETE FROM dispatch WHERE request_id='cancel-orphan'")
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("UPDATE outgoing_intent SET managed_epoch=0 WHERE request_id='cancel-orphan'")
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        before=self.outgoing.read_bytes()
        result=self.cancel('cancel-orphan')
        self.assertEqual(result['result'],'cancelled_by_user')
        self.assertEqual(self.outgoing.read_bytes(),before)
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status,epoch,batch_id,part_index,priority FROM dispatch WHERE id=?',
                (result['dispatch_id'],)).fetchone(),('done',0,'old-batch',0,2))
        self.coordinator('reconcile-outbox','--conversation-id',self.channels['group']['id'])
        self.assertTrue(self.cancel('cancel-orphan')['already_cancelled'])

    def test_legacy_orphans_get_same_id_tombstones_without_outbox_backfill(self):
        for status in ('indeterminate','failed'):
            request='legacy-'+status
            self.legacy_orphan(request,status)
            before=self.outgoing.read_bytes()
            result=self.cancel(request,1)
            self.assertEqual((result['status'],result['result'],result['outbox_status'],
                              result['already_cancelled']),
                             ('done','cancelled_by_user',status,False))
            self.assertEqual(self.outgoing.read_bytes(),before)
            with sqlite3.connect(self.scheduler) as db:
                self.assertEqual(db.execute('SELECT request_id,epoch,status,result FROM dispatch WHERE id=?',
                    (result['dispatch_id'],)).fetchone(),(request,1,'done','cancelled_by_user'))
                audit=db.execute('SELECT reason FROM surface_review ORDER BY id DESC LIMIT 1').fetchone()[0]
                self.assertIn('legacy_missing_origin=true',audit)
        self.assertFalse(self.log.exists())

    def test_legacy_tombstone_stays_idempotent_after_binding_epoch_change(self):
        request='legacy-after-handback'
        self.legacy_orphan(request)
        first=self.cancel(request,1)
        before=self.outgoing.read_bytes()
        group=self.channels['group']['id']
        self.coordinator('takeover','--conversation-id',group,'--expected-epoch',1)
        self.coordinator('handback','--conversation-id',group,'--expected-epoch',2)
        again=self.cancel(request,1)
        self.assertEqual((again['dispatch_id'],again['already_cancelled']),
                         (first['dispatch_id'],True))
        self.assertEqual(self.outgoing.read_bytes(),before)
        self.assertEqual(self.coordinator('enqueue','--conversation-id',group,
            '--request-id',request,'--expected-epoch',3)['result'],'cancelled_by_user')
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM surface_review WHERE reason LIKE 'cancelled_by_user%' AND reason LIKE '%legacy_missing_origin=true%'").fetchone()[0],1)

    def test_partial_missing_origin_or_existing_legacy_dispatch_refuses(self):
        for request, field in (('legacy-no-owner','managed_owner'),
                               ('legacy-no-epoch','managed_epoch')):
            self.legacy_orphan(request)
            with sqlite3.connect(self.outgoing) as db:
                db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
                db.execute(f"UPDATE outgoing_intent SET {field}=? WHERE request_id=?",
                           ('owner_group' if field=='managed_owner' else 1,request))
                db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
            self.cancel(request,1,okay=False)
            with sqlite3.connect(self.scheduler) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM dispatch WHERE request_id=?',(request,)).fetchone()[0],0)
        request='legacy-conflicting-dispatch'
        self.legacy_orphan(request)
        with sqlite3.connect(self.scheduler) as db:
            db.execute("INSERT INTO dispatch(conversation_id,request_id,operation,channel_id,epoch,status,created_at,updated_at,batch_id,part_index,priority) VALUES(?,?,'send',?,1,'unknown',1,1,NULL,NULL,1)",
                       (self.channels['group']['id'],request,self.channels['group']['binding']['channel_id']))
        self.cancel(request,1,okay=False)

    def test_legacy_old_outbox_recovery_remains_managed_fenced(self):
        request='legacy-managed-fence'
        self.legacy_orphan(request)
        self.cancel(request,1)
        before=self.outgoing.read_bytes()
        error=self.outbox('group','recover-unsent','--request-id',request,
            '--expected-attempt',1,'--driver',str(self.channels['group']['state']/'conversation-sender'),
            okay=False)
        self.assertIn('managed recovery requires explicit coordinated review',error)
        self.assertEqual(self.outgoing.read_bytes(),before)
        self.assertFalse(self.log.exists())

    def test_terminal_unknown_attempts_one_to_three_preserve_history(self):
        for count in (1,2,3):
            request=f'cancel-unknown-{count}'
            self.terminal(request,count)
            if count == 2:
                (self.ui/'synthetic-submit-evidence.json').write_text('{"submit":"unknown"}')
            before=self.outgoing.read_bytes()
            result=self.cancel(request,count)
            self.assertEqual((result['attempt_number'],result['outbox_status'],result['result']),
                             (count,'indeterminate','cancelled_by_user'))
            self.assertEqual(self.outgoing.read_bytes(),before)
        self.assertFalse(self.log.exists())

    def test_failed_and_done_archive_can_revoke_future_recovery(self):
        self.terminal('cancel-failed',1,status='failed')
        self.assertEqual(self.cancel('cancel-failed',1)['outbox_status'],'failed')
        self.terminal('cancel-archived',2)
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET status='done',result='clipboard_prepare_rejected' WHERE request_id='cancel-archived'")
        self.assertEqual(self.cancel('cancel-archived',2)['result'],'cancelled_by_user')
        self.assertFalse(self.log.exists())

    def test_queued_media_and_positive_outbox(self):
        self.queue('group','cancel-media',image=True)
        before=self.outgoing.read_bytes()
        self.assertEqual(self.cancel('cancel-media')['outbox_status'],'managed_media_queued')
        self.assertEqual(self.outgoing.read_bytes(),before)
        self.terminal('cancel-positive',1)
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("UPDATE outgoing_intent SET status='ui_observed' WHERE request_id='cancel-positive'")
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        self.cancel('cancel-positive',1,okay=False)
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET status='done',result='ui_observed' WHERE request_id='cancel-positive'")
        self.cancel('cancel-positive',1,okay=False)

    def test_active_positive_and_wrong_identity_refuse(self):
        self.terminal('cancel-active',1)
        self.cancel('cancel-active',2,okay=False)
        with sqlite3.connect(self.outgoing) as db:
            db.execute('UPDATE managed_control SET gate=1 WHERE singleton=1')
            db.execute("UPDATE outgoing_attempt SET status='attempt_recorded',finished_at=NULL WHERE request_id='cancel-active'")
            db.execute('UPDATE managed_control SET gate=0 WHERE singleton=1')
        self.cancel('cancel-active',1,okay=False)
        self.cancel('cancel-active',1,name='private',okay=False)
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute("SELECT status FROM dispatch WHERE request_id='cancel-active'").fetchone()[0],'unknown')

    def test_unpaused_or_claimed_desktop_refuses(self):
        parent=self.queue('group','cancel-busy')
        self.coordinator('dispatch','resume')
        self.cancel('cancel-busy',okay=False)
        self.coordinator('dispatch','pause','--reason','synthetic operator cancellation review')
        with sqlite3.connect(self.scheduler) as db:
            db.execute("UPDATE dispatch SET status='claimed',token='synthetic' WHERE id=?",(parent['dispatch_id'],))
            db.execute('UPDATE surface SET active_dispatch=? WHERE singleton=1',(parent['dispatch_id'],))
        self.cancel('cancel-busy',okay=False)
        with sqlite3.connect(self.scheduler) as db:
            self.assertEqual(db.execute('SELECT status FROM dispatch WHERE id=?',(parent['dispatch_id'],)).fetchone()[0],'claimed')


if __name__ == '__main__':
    unittest.main()
