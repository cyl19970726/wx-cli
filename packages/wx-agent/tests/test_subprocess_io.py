"""Synthetic subprocess regressions for readiness followed by EAGAIN."""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from wx_agent import channel_agent, macos_sender_driver


class SubprocessIOTests(unittest.TestCase):
    def test_channel_command_retries_nonblocking_read_and_write_without_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "starts"
            child = ("import pathlib,sys; "
                     "pathlib.Path(sys.argv[1]).open('a').write('1'); "
                     "data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)")
            payload = "payload" * 20000
            actual_read, actual_write = os.read, os.write
            injected = {"read": False, "write": False}

            def read(fd, size):
                if not injected["read"] and not os.get_blocking(fd):
                    injected["read"] = True
                    raise BlockingIOError(errno.EAGAIN, "try again")
                return actual_read(fd, size)

            def write(fd, data):
                if not injected["write"] and not os.get_blocking(fd):
                    injected["write"] = True
                    raise BlockingIOError(errno.EWOULDBLOCK, "try again")
                return actual_write(fd, data)

            with mock.patch.object(channel_agent.os, "read", side_effect=read), \
                 mock.patch.object(channel_agent.os, "write", side_effect=write):
                result = channel_agent.command(
                    [sys.executable, "-c", child, str(marker)], data=payload, timeout=5)
            self.assertEqual(result, payload)
            self.assertEqual(injected, {"read": True, "write": True})
            self.assertEqual(marker.read_text(), "1")

    def test_sender_call_retries_nonblocking_read_without_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "starts"
            child = ("import pathlib,sys; "
                     "pathlib.Path(sys.argv[1]).open('a').write('1'); print('ready')")
            actual_read = os.read
            injected = False

            def read(fd, size):
                nonlocal injected
                if not injected and not os.get_blocking(fd):
                    injected = True
                    raise BlockingIOError(errno.EAGAIN, "try again")
                return actual_read(fd, size)

            with mock.patch.object(macos_sender_driver.os, "read", side_effect=read):
                result = macos_sender_driver.call(
                    [sys.executable, "-c", child, str(marker)],
                    macos_sender_driver.time.monotonic() + 5)
            self.assertEqual(result, "ready\n")
            self.assertTrue(injected)
            self.assertEqual(marker.read_text(), "1")


if __name__ == "__main__":
    unittest.main()
