"""Queue-only project installation and narrowly scoped legacy Hook retirement."""

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

from wx_agent import plugin_install as installer


class PluginInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.config = self.root / "private queue config.json"
        self.config.write_text("{}\n", encoding="utf-8")
        self.config.chmod(0o600)
        self.executable = self.root / "wx-agent-queue-session"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(0o700)
        self.skill = self.project / ".agents/skills/wechat-inbox/SKILL.md"
        self.record = self.project / ".local" / installer.RECORD_NAME
        self.archive = self.project / ".local" / installer.ARCHIVE_NAME
        self.hooks = self.project / ".codex/hooks.json"
        self.bundle = Path(__file__).resolve().parents[3] / "plugins/codex-wechat"

    def install(self, **kwargs):
        kwargs.setdefault("bundle", self.bundle)
        return installer.install(self.project, self.config, self.executable, **kwargs)

    def source_skill(self):
        return (self.bundle / ".agents/skills/wechat-inbox/SKILL.md").read_bytes()

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def worktree(self):
        main = self.root / "main checkout"
        main.mkdir()
        self.git(main, "init", "-q")
        (main / ".gitignore").write_text("/.local/\n", encoding="utf-8")
        self.git(main, "add", ".gitignore")
        self.git(main, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        self.project.rmdir()
        self.git(main, "worktree", "add", "-q", "-b", "fixture-worktree", str(self.project))
        return main

    def legacy(self, *, hooks_project=None, recorded_source=True, original=None,
               skill_created=True, record_source=True):
        """Create an authentic v1 record, including the old exact after-image."""
        target = self.project if hooks_project is None else hooks_project
        hook_path = target / ".codex/hooks.json"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        if original is None:
            before = None
            value = {"hooks": {}}
        else:
            before = original
            hook_path.write_bytes(before)
            value = json.loads(before)
        command = shlex.join((str(self.executable), "--config", str(self.config), "hook"))
        groups = {event: {"hooks": [{"type": "command", "command": command,
                                    "timeout": 60, "additionalContextLimit": 1200}]}
                  for event in installer.EVENTS}
        hooks = value.setdefault("hooks", {})
        for event, group in groups.items():
            hooks.setdefault(event, []).append(group)
        after = installer._json_bytes(value)
        hook_path.write_bytes(after)
        hook_path.chmod(0o600)
        self.skill.parent.mkdir(parents=True, exist_ok=True)
        old_skill = b"---\nname: wechat-inbox\ndescription: Old Hook Skill\n---\n"
        self.skill.write_bytes(old_skill)
        self.record.parent.mkdir(parents=True, exist_ok=True)
        record = {"version": 1, "project": str(self.project), "config": str(self.config),
                  "executable": str(self.executable), "groups": groups,
                  "skill_sha256": installer._digest(old_skill), "skill_created": skill_created,
                  "hooks_before": None if before is None else base64.b64encode(before).decode("ascii"),
                  "hooks_before_mode": None if before is None else 0o600,
                  "hooks_after_sha256": installer._digest(after)}
        if recorded_source:
            record["hooks_project"] = str(target)
        if not record_source:
            record.pop("config")
            record.pop("executable")
        self.record.write_bytes(installer._json_bytes(record))
        self.record.chmod(0o600)
        return hook_path, groups

    def test_new_install_writes_only_project_skill_and_private_record(self):
        self.hooks.parent.mkdir()
        original = b'{ "hooks": {"Stop": [{"hooks": [{"command":"user"}]}]} }\n'
        self.hooks.write_bytes(original)
        result = self.install()
        self.assertEqual(result["status"], "installed")
        self.assertEqual(self.skill.read_bytes(), self.source_skill())
        self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.hooks.read_bytes(), original)
        self.assertEqual(json.loads(self.record.read_text())["version"], 2)
        self.assertEqual(self.install()["status"], "already_installed")
        self.assertEqual(installer.uninstall(self.project)["status"], "uninstalled")
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.record.exists())
        self.assertEqual(self.hooks.read_bytes(), original)

    def test_new_install_preserves_preexisting_matching_skill(self):
        self.skill.parent.mkdir(parents=True)
        self.skill.write_bytes(self.source_skill())
        self.install()
        self.assertFalse(json.loads(self.record.read_text())["skill_created"])
        installer.uninstall(self.project)
        self.assertEqual(self.skill.read_bytes(), self.source_skill())

    def test_new_install_refuses_changed_skill_record_and_private_config(self):
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("user Skill", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "different content"):
            self.install()
        self.skill.unlink()
        self.config.chmod(0o644)
        with self.assertRaisesRegex(installer.InstallError, "broad permissions"):
            self.install()
        self.config.chmod(0o600)
        self.install()
        self.skill.write_text("user edited", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "changed"):
            installer.uninstall(self.project)
        self.assertTrue(self.record.exists())
        self.assertEqual(self.skill.read_text(), "user edited")

    def test_project_scope_symlink_and_hardlink_refusals(self):
        with self.assertRaisesRegex(installer.InstallError, "system directories"):
            installer.install(Path.home(), self.config, self.executable)
        with self.assertRaisesRegex(installer.InstallError, "system directories"):
            installer.install("/usr", self.config, self.executable)
        alias = self.root / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "canonical"):
            installer.install(alias, self.config, self.executable)
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / ".agents").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "symbolic link"):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])
        (self.project / ".agents").unlink()
        other = self.root / "hardlinked"
        os.link(self.config, other)
        with self.assertRaisesRegex(installer.InstallError, "single-link"):
            self.install()

    def test_private_record_and_global_agent_descendant_refuse(self):
        fake_home = self.root / "fake-home"
        global_project = fake_home / ".codex/project"
        global_project.mkdir(parents=True)
        with mock.patch.object(Path, "home", return_value=fake_home):
            with self.assertRaisesRegex(installer.InstallError, "global agent"):
                installer.install(global_project, self.config, self.executable, self.bundle)
        self.install()
        self.record.chmod(0o644)
        with self.assertRaisesRegex(installer.InstallError, "broad permissions"):
            installer.uninstall(self.project)
        self.record.chmod(0o600)
        other = self.root / "record-hardlink"
        os.link(self.record, other)
        with self.assertRaisesRegex(installer.InstallError, "single-link"):
            installer.uninstall(self.project)
        self.assertTrue(self.record.exists())

    def test_git_worktree_install_uses_only_worktree_project(self):
        main = self.worktree()
        shared = main / ".codex/hooks.json"
        shared.parent.mkdir()
        shared.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")
        before = shared.read_bytes()
        result = self.install()
        self.assertEqual(result["project"], str(self.project))
        self.assertEqual(shared.read_bytes(), before)
        self.assertFalse(self.hooks.parent.exists())
        self.assertFalse((main / ".agents").exists())
        self.assertTrue(self.skill.exists())
        self.assertEqual(installer.uninstall(self.project)["status"], "uninstalled")
        self.assertEqual(shared.read_bytes(), before)

    def test_git_requires_ignored_private_record_but_no_hooks_ignore(self):
        self.git(self.project, "init", "-q")
        with self.assertRaisesRegex(installer.InstallError, "git-ignored"):
            self.install()
        self.assertFalse(self.record.exists())
        (self.project / ".gitignore").write_text("/.local/\n", encoding="utf-8")
        self.install()
        self.assertFalse(self.hooks.parent.exists())

    def test_new_install_refuses_live_legacy_record_then_explicit_retirement_upgrades_owned_skill(self):
        hook_path, _ = self.legacy()
        with self.assertRaisesRegex(installer.InstallError, "retire-hooks"):
            self.install()
        self.assertTrue(hook_path.exists())
        retired = installer.retire_hooks(self.project)
        self.assertEqual(retired["status"], "hooks_retired")
        self.assertFalse(hook_path.exists())
        self.assertFalse(self.record.exists())
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        self.assertIn(b"Old Hook Skill", self.skill.read_bytes())
        self.assertEqual(installer.retire_hooks(self.project)["status"], "already_retired")
        self.install()
        self.assertEqual(self.skill.read_bytes(), self.source_skill())
        self.assertTrue(json.loads(self.record.read_text())["skill_created"])
        installer.uninstall(self.project)
        self.assertFalse(self.skill.exists())
        self.assertTrue(self.archive.exists())

    def test_retirement_keeps_modified_skill_and_unrelated_hook_groups(self):
        original = b'{"description":"user", "hooks":{"Stop":[{"hooks":[{"command":"user"}]}]}}\n'
        hook_path, groups = self.legacy(original=original)
        self.skill.write_text("user edited old Skill", encoding="utf-8")
        value = json.loads(hook_path.read_text())
        value["description"] = "new user metadata"
        value["hooks"]["SessionStart"].append({"hooks": [{"command": "later user Hook"}]})
        hook_path.write_text(json.dumps(value), encoding="utf-8")
        result = installer.retire_hooks(self.project)
        self.assertEqual(result["status"], "hooks_retired")
        remaining = json.loads(hook_path.read_text())
        self.assertEqual(remaining["description"], "new user metadata")
        self.assertEqual(remaining["hooks"]["Stop"], value["hooks"]["Stop"])
        self.assertEqual(len(remaining["hooks"]["SessionStart"]), 1)
        self.assertEqual(remaining["hooks"]["SessionStart"][0]["hooks"][0]["command"],
                         "later user Hook")
        self.assertNotIn(groups["PostToolUse"], remaining["hooks"].get("PostToolUse", []))
        self.assertEqual(self.skill.read_text(), "user edited old Skill")
        with self.assertRaisesRegex(installer.InstallError, "preserve user edits"):
            self.install()

    def test_retirement_restores_exact_old_hook_file_and_mode(self):
        original = b'{ "hooks": {}, "keep": true }\n'
        hook_path, _ = self.legacy(original=original)
        installer.retire_hooks(self.project)
        self.assertEqual(hook_path.read_bytes(), original)
        self.assertEqual(hook_path.stat().st_mode & 0o777, 0o600)

    def test_modified_recorded_hook_refuses_without_changes(self):
        hook_path, _ = self.legacy()
        value = json.loads(hook_path.read_text())
        value["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "user modified"
        hook_path.write_text(json.dumps(value), encoding="utf-8")
        before = hook_path.read_bytes()
        with self.assertRaisesRegex(installer.InstallError, "groups changed"):
            installer.retire_hooks(self.project)
        self.assertEqual(hook_path.read_bytes(), before)
        self.assertTrue(self.record.exists())
        self.assertFalse(self.archive.exists())

    def test_malformed_legacy_before_image_refuses_before_archive(self):
        hook_path, _ = self.legacy(original=b'{"hooks":{}}\n')
        saved = json.loads(self.record.read_text())
        saved["hooks_before"] = base64.b64encode(b'{"hooks":{"Stop":[]}}\n').decode("ascii")
        self.record.write_bytes(installer._json_bytes(saved))
        before = hook_path.read_bytes()
        with self.assertRaisesRegex(installer.InstallError, "before-image"):
            installer.retire_hooks(self.project)
        self.assertEqual(hook_path.read_bytes(), before)
        self.assertFalse(self.archive.exists())

    def test_legacy_archive_requires_private_git_ignore(self):
        self.git(self.project, "init", "-q")
        hook_path, _ = self.legacy()
        (self.project / ".gitignore").write_text("/.local/" + installer.RECORD_NAME + "\n",
                                                  encoding="utf-8")
        self.assertEqual(self.git(self.project, "check-ignore", ".local/" + installer.RECORD_NAME),
                         ".local/" + installer.RECORD_NAME)
        before = hook_path.read_bytes()
        with self.assertRaisesRegex(installer.InstallError, installer.ARCHIVE_NAME):
            installer.retire_hooks(self.project)
        self.assertEqual(hook_path.read_bytes(), before)
        self.assertTrue(self.record.exists())
        self.assertFalse(self.archive.exists())

    def test_retirement_resumes_after_archive_publication(self):
        original = b'{"description":"user", "hooks":{"Stop":[{"hooks":[{"command":"user"}]}]}}\n'
        hook_path, groups = self.legacy(original=original)
        value = json.loads(hook_path.read_text())
        value["hooks"]["SessionStart"].append({"hooks": [{"command": "later user Hook"}]})
        hook_path.write_bytes(installer._json_bytes(value))
        active_record = self.record.read_bytes()
        original_atomic = installer._atomic

        def interrupted(path, data, mode):
            original_atomic(path, data, mode)
            if path == self.archive:
                raise RuntimeError("injected interruption after archive publication")

        with mock.patch.object(installer, "_atomic", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "injected interruption"):
                installer.retire_hooks(self.project)
        self.assertEqual(self.archive.read_bytes(), active_record)
        self.assertEqual(self.record.read_bytes(), active_record)
        self.assertEqual(json.loads(hook_path.read_text()), value)

        self.assertEqual(installer.retire_hooks(self.project)["status"], "hooks_retired")
        self.assertEqual(self.archive.read_bytes(), active_record)
        self.assertFalse(self.record.exists())
        remaining = json.loads(hook_path.read_text())
        self.assertEqual(remaining["description"], "user")
        self.assertEqual(remaining["hooks"]["Stop"], value["hooks"]["Stop"])
        self.assertEqual(remaining["hooks"]["SessionStart"], [value["hooks"]["SessionStart"][1]])
        for event, group in groups.items():
            self.assertNotIn(group, remaining["hooks"].get(event, []))

    def test_retirement_resumes_after_owned_groups_removed(self):
        hook_path, groups = self.legacy()
        value = json.loads(hook_path.read_text())
        value["hooks"]["Stop"] = [{"hooks": [{"command": "unrelated user Hook"}]}]
        hook_path.write_bytes(installer._json_bytes(value))
        active_record = self.record.read_bytes()
        original_atomic = installer._atomic

        def interrupted(path, data, mode):
            original_atomic(path, data, mode)
            if path == hook_path:
                raise RuntimeError("injected interruption after Hook removal")

        with mock.patch.object(installer, "_atomic", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "injected interruption"):
                installer.retire_hooks(self.project)
        self.assertEqual(self.archive.read_bytes(), active_record)
        self.assertTrue(self.record.exists())
        after_removal = hook_path.read_bytes()
        for event, group in groups.items():
            self.assertNotIn(group, json.loads(after_removal)["hooks"].get(event, []))
        self.assertEqual(installer.retire_hooks(self.project)["status"], "hooks_retired")
        self.assertEqual(hook_path.read_bytes(), after_removal)
        self.assertEqual(self.archive.read_bytes(), active_record)
        self.assertFalse(self.record.exists())

    def test_retirement_refuses_archive_that_differs_from_active_record(self):
        hook_path, _ = self.legacy()
        before = hook_path.read_bytes()
        self.archive.write_bytes(self.record.read_bytes() + b"\n")
        self.archive.chmod(0o600)
        with self.assertRaisesRegex(installer.InstallError, "archive differs"):
            installer.retire_hooks(self.project)
        self.assertEqual(hook_path.read_bytes(), before)
        self.assertTrue(self.record.exists())
        self.assertEqual(self.archive.read_bytes(), self.record.read_bytes() + b"\n")

    def test_legacy_worktree_shared_source_validated_and_local_v1_supported(self):
        main = self.worktree()
        shared, _ = self.legacy(hooks_project=main)
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        self.git(unrelated, "init", "-q")
        saved = json.loads(self.record.read_text())
        saved["hooks_project"] = str(unrelated)
        self.record.write_bytes(installer._json_bytes(saved))
        with self.assertRaisesRegex(installer.InstallError, "corresponding main checkout"):
            installer.retire_hooks(self.project)
        self.assertTrue(shared.exists())
        self.assertFalse(self.archive.exists())
        saved["hooks_project"] = str(main)
        self.record.write_bytes(installer._json_bytes(saved))
        self.assertEqual(installer.retire_hooks(self.project)["hooks_project"], str(main))
        self.assertFalse(shared.exists())
        self.assertFalse(self.hooks.parent.exists())

        # Old records without hooks_project remain scoped to their worktree.
        self.archive.unlink()
        local, _ = self.legacy(recorded_source=False, record_source=False)
        shared.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")
        before = shared.read_bytes()
        self.assertEqual(installer.retire_hooks(self.project)["hooks_project"], str(self.project))
        self.assertFalse(local.exists())
        self.assertEqual(shared.read_bytes(), before)

    def test_preexisting_legacy_skill_is_not_replaced_or_deleted(self):
        self.legacy(skill_created=False)
        installer.retire_hooks(self.project)
        with self.assertRaisesRegex(installer.InstallError, "preexisted"):
            self.install()
        self.assertIn(b"Old Hook Skill", self.skill.read_bytes())
        installer.uninstall(self.project)
        self.assertTrue(self.skill.exists())

    def test_legacy_uninstall_removes_owned_hook_and_skill_but_keeps_archive(self):
        hook_path, _ = self.legacy()
        self.assertEqual(installer.uninstall(self.project)["status"], "uninstalled")
        self.assertFalse(hook_path.exists())
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.record.exists())
        self.assertTrue(self.archive.exists())

    def test_main_and_bundle_contract(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            installer.main(["--project", str(self.project), "--config", str(self.config),
                            "--executable", str(self.executable), "--bundle", str(self.bundle)])
        self.assertEqual(json.loads(output.getvalue())["status"], "installed")
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["--project", str(self.project), "--action", "uninstall"])
        manifest = json.loads((self.bundle / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(manifest["skills"], "./.agents/skills/")
        self.assertNotIn("hooks", manifest)
        self.assertFalse((self.bundle / "hooks/hooks.json").exists())
        self.assertFalse((self.bundle / "hooks/bootstrap.sh").exists())
        package = (self.bundle.parents[1] / "packages/wx-agent/pyproject.toml").read_text()
        self.assertNotIn("wx-agent-plugin =", package)
        for name in ("wx-agent-queue-session", "wx-agent-plugin-runner", "wx-agent-plugin-install"):
            self.assertIn(name + " =", package)

    def test_installed_wheel_needs_explicit_bundle_and_non_git_install_survives_missing_git(self):
        with mock.patch.object(installer, "BUNDLE", self.root / "wheel-without-plugin-directory"):
            with self.assertRaisesRegex(installer.InstallError, "require --bundle"):
                installer.install(self.project, self.config, self.executable)
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(self.install()["status"], "installed")


if __name__ == "__main__":
    unittest.main()
