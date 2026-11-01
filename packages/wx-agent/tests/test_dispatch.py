"""Python routing guard for a managed intent's originating owner and epoch."""
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


if __name__ == "__main__":
    unittest.main()
