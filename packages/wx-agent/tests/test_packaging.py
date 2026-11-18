"""Installed entrypoints expose only the managed conversation commands."""
import json
from pathlib import Path
import subprocess
import sysconfig
import tempfile
import unittest

from wx_agent import local


class InstalledPackageTests(unittest.TestCase):
    def entrypoint(self, name="wx-agent"):
        path = Path(sysconfig.get_path("scripts")) / name
        self.assertTrue(path.is_file(), path)
        return path

    def run_entrypoint(self, *args):
        return subprocess.run([str(self.entrypoint()), *args], cwd=tempfile.gettempdir(),
                              capture_output=True, text=True, timeout=5)

    def test_entrypoint_resolves_installed_modules(self):
        self.assertTrue(Path(local.__file__).is_file())
        result = self.run_entrypoint("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("conversations", "dispatch", "inbox", "memory", "sender"):
            self.assertIn(command, result.stdout)
        refused = self.run_entrypoint("sender", "--unsupported")
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(json.loads(refused.stdout)["reason"], "unsupported command")

    def test_retired_single_group_runner_commands_are_rejected(self):
        for command in ("chat", "start", "status", "pause", "resume", "stop", "run", "reply"):
            result = self.run_entrypoint(command, "--help")
            self.assertEqual(result.returncode, 2, (command, result.stdout))
            self.assertIn("invalid choice", result.stderr)
        self.assertFalse((Path(sysconfig.get_path("scripts")) / "wx-agent-reply").exists())
        retire = self.run_entrypoint("conversations", "--config", "/synthetic/config.json",
                                     "retire-controller", "--help")
        self.assertEqual(retire.returncode, 0, retire.stderr)


if __name__ == "__main__":
    unittest.main()
