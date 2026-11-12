"""Python routing guard for a managed intent's originating owner and epoch."""
import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wx_agent import dispatch
from wx_agent import conversations


class DispatchRoutingTests(unittest.TestCase):
    def test_show_rejects_scope_and_malformed_cancelled_record(self):
        base = {"conversation_id": "synthetic-conversation", "request_id": "original-1",
                "surface_paused": False, "dispatch": None}
        for changed in ({"conversation_id": "other"}, {"request_id": "other"},
                        {"surface_paused": "false"}, {"dispatch": []},
                        {"dispatch": {"dispatch_id": 7, "status": "queued",
                                      "result": "cancelled_by_user", "operation": "send", "epoch": 7}},
                        {"dispatch": {"dispatch_id": 7, "status": "done",
                                      "result": "cancelled_by_user", "operation": "focus", "epoch": 7}}):
            with self.subTest(changed=changed), self.assertRaises(dispatch.DispatchError):
                dispatch.validate_show_response({**base, **changed}, "synthetic-conversation", "original-1")
        self.assertFalse(dispatch.validate_show_response(base, "synthetic-conversation", "original-1")["dispatch"])

    def test_stale_or_missing_origin_never_reaches_coordinator_enqueue(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "channel"
            root = Path(temporary) / "desktop"
            binding = {"conversation_id": "synthetic-conversation",
                       "owner": "current-owner", "epoch": 7}
            with mock.patch.object(dispatch, "managed_root", return_value=root), \
                 mock.patch.object(dispatch, "resolve_binding", return_value=binding), \
                 mock.patch.object(dispatch, "call") as call:
                call.return_value = {"conversation_id": "synthetic-conversation", "request_id": "request-1",
                                     "surface_paused": False, "dispatch": None}
                for owner, epoch in ((None, None), ("old-owner", 7),
                                     ("current-owner", 6)):
                    with self.subTest(owner=owner, epoch=epoch), \
                         self.assertRaises(dispatch.DispatchError):
                        dispatch.enqueue_existing("/bin/wx-cli", state, "request-1",
                                                  owner=owner, expected_epoch=epoch)
                call.assert_not_called()
                call.side_effect = [call.return_value, {"dispatch_id": 4, "status": "queued"}]
                _, queued = dispatch.enqueue_existing("/bin/wx-cli", state, "request-1",
                                                      owner="current-owner", expected_epoch=7)
                self.assertEqual(queued["dispatch_id"], 4)
                call.assert_any_call("/bin/wx-cli", root, "enqueue",
                                     "--conversation-id", "synthetic-conversation",
                                     "--request-id", "request-1", "--expected-epoch", 7)

    def test_cancel_request_forwards_exact_arguments_and_result(self):
        args = ["--config", "/synthetic/config.json", "cancel-request",
                "--conversation-id", "synthetic-conversation", "--request-id", "original-1",
                "--expected-attempt", "2", "--reason", "user cancelled old message"]
        result = {"dispatch_id": 7, "status": "done", "result": "cancelled_by_user",
                  "outbox_status": "indeterminate", "attempt_number": 2, "already_cancelled": False}
        settings = {"wx_cli": "/synthetic/wx-cli"}
        with mock.patch.object(conversations, "config", return_value=settings), \
             mock.patch.object(dispatch, "root_from_settings", return_value=Path("/synthetic/ui")), \
             mock.patch.object(dispatch, "call", return_value=result) as call, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(dispatch.main(args), 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        call.assert_called_once_with("/synthetic/wx-cli", Path("/synthetic/ui"),
                                     "dispatch", "cancel-request", "--conversation-id",
                                     "synthetic-conversation", "--request-id", "original-1",
                                     "--expected-attempt", 2, "--reason", "user cancelled old message")

    def test_cancelled_request_cannot_enqueue_or_wait(self):
        root = Path("/synthetic/ui")
        binding = {"conversation_id": "synthetic-conversation", "owner": "owner", "epoch": 7}
        queue = {"conversation_id": "synthetic-conversation", "request_id": "original-1",
                 "surface_paused": False, "dispatch": {"dispatch_id": 7,
                 "status": "done", "result": "cancelled_by_user", "operation": "send", "epoch": 7}}
        with mock.patch.object(dispatch, "managed_root", return_value=root), \
             mock.patch.object(dispatch, "resolve_binding", return_value=binding), \
             mock.patch.object(dispatch, "call", return_value=queue) as call, \
             mock.patch.object(dispatch, "drain") as drain, \
             mock.patch.object(dispatch.time, "sleep") as sleep:
            for operation in (dispatch.enqueue_existing, dispatch.send_existing):
                with self.subTest(operation=operation.__name__), \
                     self.assertRaisesRegex(dispatch.DispatchError, "cancelled_by_user"):
                    operation("/synthetic/wx-cli", Path("/synthetic/channel"), "original-1",
                              owner="owner", expected_epoch=7)
            self.assertTrue(all(item.args[2:] == ("dispatch", "show", "--conversation-id",
                                  "synthetic-conversation", "--request-id", "original-1")
                                for item in call.call_args_list))
            drain.assert_not_called()
            sleep.assert_not_called()

    def test_cancelled_after_enqueue_stops_before_desktop_execution(self):
        root = Path("/synthetic/ui")
        queued = {"dispatch_id": 7, "conversation_id": "synthetic-conversation", "status": "queued"}
        with mock.patch.object(dispatch, "enqueue_existing", return_value=(root, queued)), \
             mock.patch.object(dispatch, "_cancelled_request", return_value=True), \
             mock.patch.object(dispatch, "drain") as drain, \
             mock.patch.object(dispatch.time, "sleep") as sleep, \
             self.assertRaisesRegex(dispatch.DispatchError, "cancelled_by_user"):
            dispatch.send_existing("/synthetic/wx-cli", "/synthetic/channel", "original-1")
        drain.assert_not_called()
        sleep.assert_not_called()

    def test_rust_enqueue_cancellation_is_not_reported_as_pending(self):
        root = Path("/synthetic/ui")
        binding = {"conversation_id": "synthetic-conversation", "owner": "owner", "epoch": 7}
        responses = [{"conversation_id": "synthetic-conversation", "request_id": "original-1",
                      "surface_paused": False, "dispatch": None},
                     {"dispatch_id": 7, "conversation_id": "synthetic-conversation",
                                         "status": "done", "result": "cancelled_by_user"}]
        with mock.patch.object(dispatch, "managed_root", return_value=root), \
             mock.patch.object(dispatch, "resolve_binding", return_value=binding), \
             mock.patch.object(dispatch, "call", side_effect=responses) as call, \
             mock.patch.object(dispatch, "drain") as drain, \
             self.assertRaisesRegex(dispatch.DispatchError, "cancelled_by_user"):
            dispatch.enqueue_existing("/synthetic/wx-cli", Path("/synthetic/channel"),
                                      "original-1", owner="owner", expected_epoch=7)
        self.assertEqual(call.call_count, 2)
        drain.assert_not_called()

    def test_cancellation_after_drain_preserves_outbox_uncertainty(self):
        root = Path("/synthetic/ui")
        queued = {"dispatch_id": 7, "conversation_id": "synthetic-conversation", "status": "queued"}
        queue = {"conversation_id": "synthetic-conversation", "request_id": "original-1",
                 "surface_paused": False, "dispatch": {"dispatch_id": 7, "status": "done",
                 "result": "cancelled_by_user", "operation": "send", "epoch": 7}}
        with mock.patch.object(dispatch, "enqueue_existing", return_value=(root, queued)), \
             mock.patch.object(dispatch, "_cancelled_request", return_value=False), \
             mock.patch.object(dispatch, "drain") as drain, \
             mock.patch.object(dispatch, "_outbox", return_value={"status": "indeterminate"}), \
             mock.patch.object(dispatch, "call", return_value=queue), \
             mock.patch.object(dispatch.time, "sleep") as sleep, \
             self.assertRaisesRegex(dispatch.DispatchError, "cancelled_by_user"):
            dispatch.send_existing("/synthetic/wx-cli", "/synthetic/channel", "original-1")
        drain.assert_called_once()
        sleep.assert_not_called()

    def test_paused_single_request_returns_pending_without_global_list(self):
        root = Path("/synthetic/ui")
        queued = {"dispatch_id": 7, "conversation_id": "synthetic-conversation", "status": "queued"}
        shown = {"conversation_id": "synthetic-conversation", "request_id": "original-1",
                 "surface_paused": True, "dispatch": {"dispatch_id": 7, "status": "queued",
                 "result": None, "operation": "send", "epoch": 7}}
        with mock.patch.object(dispatch, "enqueue_existing", return_value=(root, queued)), \
             mock.patch.object(dispatch, "drain"), \
             mock.patch.object(dispatch, "_outbox", return_value={"status": "managed_queued"}), \
             mock.patch.object(dispatch, "call", return_value=shown) as call, \
             mock.patch.object(dispatch.time, "sleep") as sleep:
            result = dispatch.send_existing("/synthetic/wx-cli", "/synthetic/channel", "original-1")
        self.assertEqual(result, {"request_id": "original-1", "status": "pending", "dispatch_id": 7})
        self.assertTrue(all(item.args[2:4] == ("dispatch", "show") for item in call.call_args_list))
        sleep.assert_not_called()

    def test_restore_preflight_uses_restored_target_not_other_conversation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("private", "group"):
                (root / name).mkdir()
            bindings = {
                "private": {"channel_state_dir": str(root / "private"), "conversation_id": "private"},
                "group": {"channel_state_dir": str(root / "group"), "conversation_id": "group"},
            }
            def call(_, __, *parts):
                if parts == ("status",):
                    return {"conversations": ["private", "group"]}
                if parts == ("dispatch", "status"):
                    return {"dispatches": [{"dispatch_id": 6, "conversation_id": "group"}]}
                if parts[:2] == ("status", "--conversation-id"):
                    return bindings[parts[2]]
                if parts[:2] == ("dispatch", "restore-queued"):
                    return {"attempt_number": 0}
                if parts[:2] == ("dispatch", "review"):
                    return {"status": "ready"}
                raise AssertionError(parts)
            def driver(args, env, pass_fds, capture_output, timeout):
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": json.dumps({"ready": args[0].endswith("group/conversation-sender")}),
                })()
            with mock.patch.object(dispatch, "call", side_effect=call), \
                    mock.patch.object(dispatch.subprocess, "run", side_effect=driver) as run:
                result = dispatch.review_surface("/bin/wx-cli", root, "same unattempted target", True, 6)
            self.assertEqual(result["status"], "ready")
            self.assertTrue(run.call_args.args[0][0].endswith("group/conversation-sender"))

    def test_review_preflights_every_managed_conversation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bindings = {name: {"channel_state_dir": str(root / name), "conversation_id": name}
                        for name in ("private", "group")}
            def call(_, __, *parts):
                if parts == ("status",):
                    return {"conversations": ["private", "group"]}
                if parts[:2] == ("status", "--conversation-id"):
                    return bindings[parts[2]]
                if parts[:2] == ("dispatch", "review"):
                    return {"status": "ready"}
                raise AssertionError(parts)
            def driver(args, env, pass_fds, capture_output, timeout):
                return type("Result", (), {"returncode": 0, "stdout": json.dumps({"ready": True})})()
            with mock.patch.object(dispatch, "call", side_effect=call), \
                    mock.patch.object(dispatch.subprocess, "run", side_effect=driver) as run:
                self.assertEqual(dispatch.review_surface("/bin/wx-cli", root, "all targets ready", True)["status"], "ready")
            self.assertEqual([item.args[0][0] for item in run.call_args_list],
                             [str(root / "private/conversation-sender"), str(root / "group/conversation-sender")])

    def test_intent_only_setup_restore_keeps_unpaused_other_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'group').mkdir()
            operations=[]
            def call(_,__,*parts):
                operations.append(parts)
                if parts==('status',):return {'conversations':['group']}
                if parts==('dispatch','status'):
                    return {'paused_reason':None,'dispatches':[{'dispatch_id':6,'conversation_id':'group','status':'blocked'}]}
                if parts[:2]==('status','--conversation-id'):
                    return {'channel_state_dir':str(root/'group')}
                if parts[:2]==('dispatch','restore-queued'):return {'attempt_number':0}
                raise AssertionError('unexpected global operation: '+str(parts))
            result=type('Result',(),{'returncode':0,'stdout':'{"ready":true}'})()
            with mock.patch.object(dispatch,'call',side_effect=call), \
                 mock.patch.object(dispatch.subprocess,'run',return_value=result):
                value=dispatch.review_surface('/synthetic/wx-cli',root,'image setup repaired for original request',True,6)
            self.assertFalse(value['surface_paused'])
            self.assertEqual(value['restored_dispatch_id'],6)
            self.assertFalse(any(parts[:2]==('dispatch','review') for parts in operations))


if __name__ == "__main__":
    unittest.main()
