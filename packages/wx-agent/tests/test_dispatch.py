"""Python routing guard for a managed intent's originating owner and epoch."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wx_agent import dispatch


class DispatchRoutingTests(unittest.TestCase):
    def test_stale_or_missing_origin_never_reaches_coordinator_enqueue(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "channel"
            root = Path(temporary) / "desktop"
            binding = {"conversation_id": "synthetic-conversation",
                       "owner": "current-owner", "epoch": 7}
            with mock.patch.object(dispatch, "managed_root", return_value=root), \
                 mock.patch.object(dispatch, "resolve_binding", return_value=binding), \
                 mock.patch.object(dispatch, "call") as call:
                for owner, epoch in ((None, None), ("old-owner", 7),
                                     ("current-owner", 6)):
                    with self.subTest(owner=owner, epoch=epoch), \
                         self.assertRaises(dispatch.DispatchError):
                        dispatch.enqueue_existing("/bin/wx-cli", state, "request-1",
                                                  owner=owner, expected_epoch=epoch)
                call.assert_not_called()
                call.return_value = {"dispatch_id": 4, "status": "queued"}
                _, queued = dispatch.enqueue_existing("/bin/wx-cli", state, "request-1",
                                                      owner="current-owner", expected_epoch=7)
                self.assertEqual(queued["dispatch_id"], 4)
                call.assert_called_once_with("/bin/wx-cli", root, "enqueue",
                                             "--conversation-id", "synthetic-conversation",
                                             "--request-id", "request-1", "--expected-epoch", 7)

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
