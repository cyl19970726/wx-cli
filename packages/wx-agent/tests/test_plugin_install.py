"""Project-only installation of the native Codex WeChat hook wrapper."""

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
        self.config = self.root / "private config.json"
        self.config.write_text("{}\n", encoding="utf-8")
        self.executable = self.root / "wx-agent-plugin"
        self.executable.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
        self.executable.chmod(0o700)
        self.hooks = self.project / ".codex" / "hooks.json"
        self.skill = self.project / ".agents" / "skills" / "wechat-inbox" / "SKILL.md"
        self.record = self.project / ".local" / installer.RECORD_NAME
        self.bundle = Path(__file__).resolve().parents[3] / "plugins" / "codex-wechat"

    def install(self, **kwargs):
        kwargs.setdefault("bundle", self.bundle)
        return installer.install(self.project, self.config, self.executable, **kwargs)

    def test_preserves_existing_hooks_and_restores_exact_bytes_on_uninstall(self):
        self.hooks.parent.mkdir()
        original = b'{"description":"keep", "hooks":{"Stop":[{"hooks":[{"type":"command","command":"user-stop"}]}]}}\n'
        self.hooks.write_bytes(original)
        self.hooks.chmod(0o644)
        installed = self.install()
        self.assertEqual(installed["status"], "installed")
        self.assertEqual(installed["hook_trust"], "host_review_required")
        value = json.loads(self.hooks.read_text())
        self.assertEqual(value["hooks"]["Stop"][0]["hooks"][0]["command"], "user-stop")
        for event in installer.EVENTS:
            command = value["hooks"][event][0]["hooks"][0]["command"]
            self.assertEqual(shlex.split(command), [str(self.executable), "--config", str(self.config), "hook"])
        self.assertEqual(self.skill.read_bytes(), (self.bundle / ".agents" / "skills" /
                                                  "wechat-inbox" / "SKILL.md").read_bytes())
        self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.hooks.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.install()["status"], "already_installed")
        self.assertEqual(installer.uninstall(self.project)["status"], "uninstalled")
        self.assertEqual(self.hooks.read_bytes(), original)
        self.assertEqual(self.hooks.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.record.exists())

    def test_existing_private_hook_mode_is_preserved_on_uninstall(self):
        self.hooks.parent.mkdir()
        original = b'{"hooks":{}}\n'
        self.hooks.write_bytes(original)
        self.hooks.chmod(0o600)
        self.install()
        installer.uninstall(self.project)
        self.assertEqual(self.hooks.read_bytes(), original)
        self.assertEqual(self.hooks.stat().st_mode & 0o777, 0o600)

    def test_uninstall_removes_only_owned_entries_after_user_adds_hook(self):
        self.install()
        value = json.loads(self.hooks.read_text())
        value["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "user-stop"}]}]
        self.hooks.write_text(json.dumps(value), encoding="utf-8")
        installer.uninstall(self.project)
        remaining = json.loads(self.hooks.read_text())
        self.assertEqual(list(remaining["hooks"]), ["Stop"])
        self.assertEqual(remaining["hooks"]["Stop"], value["hooks"]["Stop"])

    def test_conflicts_and_user_edits_are_never_overwritten(self):
        self.install()
        before = self.hooks.read_bytes()
        other = self.root / "other.json"
        other.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "different parameters"):
            installer.install(self.project, other, self.executable, bundle=self.bundle)
        self.assertEqual(self.hooks.read_bytes(), before)
        self.skill.write_text("user edited skill", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "changed"):
            self.install()
        with self.assertRaisesRegex(installer.InstallError, "changed"):
            installer.uninstall(self.project)
        self.assertEqual(self.skill.read_text(), "user edited skill")
        self.assertEqual(self.hooks.read_bytes(), before)

    def test_existing_different_skill_and_unmanaged_same_hook_rejected(self):
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("different skill", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "different content"):
            self.install()
        self.assertFalse(self.hooks.exists())
        self.skill.unlink()
        command = installer.shlex.join((str(self.executable), "--config", str(self.config), "hook"))
        self.hooks.parent.mkdir()
        self.hooks.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": command, "timeout": 5}]}]}}))
        with self.assertRaisesRegex(installer.InstallError, "unmanaged hook"):
            self.install()
        self.assertFalse(self.record.exists())

    def test_rejects_home_system_and_symbolic_link_destinations(self):
        with self.assertRaisesRegex(installer.InstallError, "system directories"):
            installer.install(Path.home(), self.config, self.executable)
        with self.assertRaisesRegex(installer.InstallError, "system directories"):
            installer.install("/usr", self.config, self.executable)
        with self.assertRaisesRegex(installer.InstallError, "system directories"):
            installer.install("/usr/bin", self.config, self.executable)
        alias = self.root / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "canonical"):
            installer.install(alias, self.config, self.executable)
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / ".codex").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "symbolic link"):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])
        (self.project / ".codex").unlink()
        (self.project / ".codex").mkdir()
        self.hooks.symlink_to(outside / "hooks.json")
        with self.assertRaisesRegex(installer.InstallError, "symbolic link"):
            self.install()

    def test_installed_wheel_requires_explicit_single_bundle_source(self):
        bundle = self.bundle
        source = bundle / ".agents" / "skills" / "wechat-inbox" / "SKILL.md"
        with mock.patch.object(installer, "BUNDLE", self.root / "wheel-without-plugin-directory"):
            with self.assertRaisesRegex(installer.InstallError, "require --bundle"):
                installer.install(self.project, self.config, self.executable)
            self.install(bundle=bundle)
        self.assertEqual(self.skill.read_bytes(), source.read_bytes())

    def test_global_agent_roots_descendants_and_aliases_rejected(self):
        fake_home = self.root / "fake-home"
        fake_home.mkdir()
        with mock.patch.object(Path, "home", return_value=fake_home):
            for name in (".codex", ".agents", ".claude"):
                global_root = fake_home / name
                nested = global_root / "nested"
                nested.mkdir(parents=True)
                for target in (global_root, nested):
                    with self.subTest(target=target), self.assertRaisesRegex(installer.InstallError, "global agent"):
                        installer.install(target, self.config, self.executable)
                    self.assertFalse((target / ".local").exists())
            alias = self.root / "alias-to-global"
            alias.symlink_to(fake_home / ".codex", target_is_directory=True)
            with self.assertRaisesRegex(installer.InstallError, "canonical"):
                installer.install(alias, self.config, self.executable)
            custom = self.root / "custom-codex-home"
            nested = custom / "project-like-subdir"
            nested.mkdir(parents=True)
            custom_alias = self.root / "custom-codex-home-alias"
            custom_alias.symlink_to(custom, target_is_directory=True)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom_alias)}):
                for target in (custom, nested):
                    with self.subTest(target=target), self.assertRaisesRegex(installer.InstallError, "global agent"):
                        installer.install(target, self.config, self.executable)
                    self.assertFalse((target / ".local").exists())

    def test_git_project_requires_private_paths_ignored(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        with self.assertRaisesRegex(installer.InstallError, "git-ignored"):
            self.install()
        self.assertFalse(self.record.exists())
        (self.project / ".gitignore").write_text("/.codex/hooks.json\n/.local/\n", encoding="utf-8")
        self.assertEqual(self.install()["status"], "installed")
        self.assertEqual(subprocess.run(["git", "-C", str(self.project), "status", "--short"],
                                        capture_output=True, text=True, check=True).stdout,
                         "?? .agents/\n?? .gitignore\n")

    def test_modified_own_hook_cannot_be_removed(self):
        self.install()
        value = json.loads(self.hooks.read_text())
        value["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "user-changed"
        self.hooks.write_text(json.dumps(value), encoding="utf-8")
        before = self.hooks.read_bytes()
        with self.assertRaisesRegex(installer.InstallError, "hooks changed"):
            installer.uninstall(self.project)
        self.assertEqual(self.hooks.read_bytes(), before)
        self.assertTrue(self.record.exists())

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, check=True).stdout

    def worktree(self):
        main = self.root / "main checkout"
        main.mkdir()
        self.git(main, "init", "-q")
        (main / ".gitignore").write_text("/.codex/hooks.json\n/.local/\n")
        self.git(main, "add", ".gitignore")
        self.git(main, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        self.project.rmdir()
        self.git(main, "worktree", "add", "-q", "-b", "fixture-worktree", str(self.project))
        return main

    def tree_bytes(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_linked_worktree_default_refuses_before_any_writes(self):
        main = self.worktree()
        before = self.tree_bytes(self.root)
        with self.assertRaisesRegex(installer.InstallError, "shared Hook source.*no files written") as error:
            self.install()
        self.assertIn("--hooks-project", str(error.exception))
        self.assertIn(str(main), str(error.exception))
        self.assertEqual(self.tree_bytes(self.root), before)
        self.assertFalse(self.hooks.parent.exists())
        self.assertFalse(self.skill.parent.exists())
        self.assertFalse(self.record.parent.exists())

    def test_explicit_shared_hooks_merge_idempotence_and_uninstall_preserve_user_content(self):
        main = self.worktree()
        shared = main / ".codex" / "hooks.json"
        shared.parent.mkdir()
        existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "user-start"}]}]}}
        shared.write_text(json.dumps(existing))
        main_skill = main / ".agents" / "skills" / "wechat-inbox" / "SKILL.md"
        main_skill.parent.mkdir(parents=True)
        main_skill.write_text("main user skill")
        result = self.install(hooks_project=main)
        self.assertEqual(result["hooks_project"], str(main))
        self.assertEqual(result["project"], str(self.project))
        self.assertEqual(result["hooks_path"], str(shared))
        self.assertEqual(result["skill_path"], str(self.skill))
        self.assertEqual(result["record_path"], str(self.record))
        self.assertTrue(result["shared_hook_source"])
        self.assertFalse(self.hooks.exists())
        self.assertFalse((main / ".local").exists())
        self.assertEqual(json.loads(self.record.read_text())["hooks_project"], str(main))
        before = self.tree_bytes(self.root)
        self.assertEqual(self.install(hooks_project=main)["status"], "already_installed")
        self.assertEqual(self.tree_bytes(self.root), before)
        value = json.loads(shared.read_text())
        self.assertEqual(len(value["hooks"]["SessionStart"]), 2)
        value["description"] = "user added metadata"
        value["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": "later-start"}]})
        value["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "later-stop"}]}]
        shared.write_text(json.dumps(value))
        saved = json.loads(self.record.read_text())
        for event, group in saved["groups"].items():
            value["hooks"][event].remove(group)
            if not value["hooks"][event]:
                del value["hooks"][event]
        self.assertEqual(installer.uninstall(self.project)["hooks_project"], str(main))
        self.assertEqual(json.loads(shared.read_text()), value)
        self.assertEqual(main_skill.read_text(), "main user skill")
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.record.exists())

    def test_shared_hooks_restore_exact_original_bytes_and_keep_preexisting_skill(self):
        main = self.worktree()
        shared = main / ".codex" / "hooks.json"
        shared.parent.mkdir()
        original = b'{ "hooks": {}, "keep": true }\n'
        shared.write_bytes(original)
        shared.chmod(0o640)
        source = self.bundle / ".agents" / "skills" / "wechat-inbox" / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_bytes(source.read_bytes())
        self.install(hooks_project=main)
        installer.uninstall(self.project)
        self.assertEqual(shared.read_bytes(), original)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.skill.read_bytes(), source.read_bytes())

    def test_wrong_hook_roots_refuse_before_writes(self):
        main = self.worktree()
        other = self.root / "other repo"
        other.mkdir()
        self.git(other, "init", "-q")
        sibling = self.root / "sibling worktree"
        self.git(main, "worktree", "add", "-q", "-b", "sibling", str(sibling))
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.root / "main alias"
        alias.symlink_to(main, target_is_directory=True)
        for target in (other, sibling, outside, self.project, Path.home(), alias):
            with self.subTest(target=target):
                before = self.tree_bytes(main), self.tree_bytes(self.project), self.tree_bytes(other)
                with self.assertRaises(installer.InstallError):
                    self.install(hooks_project=target)
                self.assertEqual((self.tree_bytes(main), self.tree_bytes(self.project),
                                  self.tree_bytes(other)), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_shared_hook_symlink_refused_before_skill_or_record_writes(self):
        main = self.worktree()
        outside = self.root / "outside"
        outside.mkdir()
        (main / ".codex").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "symbolic link"):
            self.install(hooks_project=main)
        self.assertFalse(self.skill.parent.exists())
        self.assertFalse(self.record.parent.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_nested_worktree_requires_corresponding_main_project_directory(self):
        main = self.worktree()
        for root in (main, self.project):
            (root / "module").mkdir()
        nested = self.project / "module"
        with self.assertRaisesRegex(installer.InstallError, "shared Hook source"):
            installer.install(nested, self.config, self.executable, bundle=self.bundle)
        with self.assertRaisesRegex(installer.InstallError, "corresponding"):
            installer.install(nested, self.config, self.executable, bundle=self.bundle, hooks_project=main)
        # Ignore rules apply to each installation path, including nested projects.
        common = Path(self.git(main, "rev-parse", "--absolute-git-dir").strip())
        (common / "info" / "exclude").write_text("**/.codex/hooks.json\n**/.local/\n")
        result = installer.install(nested, self.config, self.executable, bundle=self.bundle,
                                   hooks_project=main / "module")
        self.assertEqual(result["hooks_project"], str(main / "module"))
        installer.uninstall(nested)

    def test_old_v1_record_without_hooks_project_uninstalls_in_linked_worktree(self):
        main = self.worktree()
        # Reconstruct the legacy local layout; the old installer had no source-root field.
        command = shlex.join((str(self.executable), "--config", str(self.config), "hook"))
        groups = installer._hook_groups(command)
        after = installer._json_bytes({"hooks": {event: [group] for event, group in groups.items()}})
        source = self.bundle / ".agents" / "skills" / "wechat-inbox" / "SKILL.md"
        self.hooks.parent.mkdir()
        self.hooks.write_bytes(after)
        self.skill.parent.mkdir(parents=True)
        self.skill.write_bytes(source.read_bytes())
        self.record.parent.mkdir()
        self.record.write_text(json.dumps({"version": 1, "project": str(self.project),
            "groups": groups, "skill_sha256": installer._digest(source.read_bytes()),
            "skill_created": True, "hooks_before": None, "hooks_before_mode": None,
            "hooks_after_sha256": installer._digest(after)}))
        shared = main / ".codex" / "hooks.json"
        shared.parent.mkdir()
        shared.write_text('{"hooks": {}, "keep": "main checkout"}\n')
        before = self.tree_bytes(main)
        local = json.loads(self.hooks.read_text())
        user_group = {"hooks": [{"type": "command", "command": "user-added-start"}]}
        local["hooks"]["SessionStart"].append(user_group)
        local["description"] = "user-added metadata"
        self.hooks.write_text(json.dumps(local))
        result = installer.uninstall(self.project)
        self.assertEqual(result["hooks_project"], str(self.project))
        self.assertEqual(json.loads(self.hooks.read_text()), {
            "hooks": {"SessionStart": [user_group]}, "description": "user-added metadata"})
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.record.exists())
        self.assertEqual(self.tree_bytes(main), before)

    def test_record_cannot_redirect_uninstall_to_an_unrelated_project(self):
        main = self.worktree()
        self.install(hooks_project=main)
        saved = json.loads(self.record.read_text())
        saved["hooks_project"] = str(self.root)
        self.record.write_text(json.dumps(saved))
        before = self.tree_bytes(self.root)
        with self.assertRaisesRegex(installer.InstallError, "corresponding"):
            installer.uninstall(self.project)
        self.assertEqual(self.tree_bytes(self.root), before)

    def test_corresponding_path_cannot_be_a_different_nested_worktree(self):
        main = self.worktree()
        nested = self.project / "module"
        nested.mkdir()
        target = main / "module"
        self.git(main, "worktree", "add", "-q", "-b", "nested-worktree", str(target))
        before = self.tree_bytes(self.root)
        with self.assertRaisesRegex(installer.InstallError, "corresponding checkout"):
            installer.install(nested, self.config, self.executable, bundle=self.bundle,
                              hooks_project=target)
        self.assertEqual(self.tree_bytes(self.root), before)

    def test_main_accepts_explicit_shared_hook_source(self):
        main = self.worktree()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            installer.main(["--project", str(self.project), "--hooks-project", str(main),
                            "--config", str(self.config), "--executable", str(self.executable),
                            "--bundle", str(self.bundle)])
        self.assertEqual(json.loads(output.getvalue())["hooks_project"], str(main))
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["--project", str(self.project), "--action", "uninstall"])

    def test_non_git_project_still_installs_without_git_available(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(self.install()["status"], "installed")
        installer.uninstall(self.project)

    def test_bundle_hooks_have_only_supported_events_and_local_bootstrap(self):
        package = self.bundle
        manifest = json.loads((package / ".codex-plugin" / "plugin.json").read_text())
        hooks = json.loads((package / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(manifest["skills"], "./.agents/skills/")
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")
        self.assertEqual(set(hooks), set(installer.EVENTS))
        self.assertTrue((package / "hooks" / "bootstrap.sh").stat().st_mode & 0o111)
        for groups in hooks.values():
            self.assertEqual(groups[0]["hooks"][0]["type"], "command")
            self.assertIn("${PLUGIN_ROOT}/hooks/bootstrap.sh", groups[0]["hooks"][0]["command"])
            self.assertEqual(groups[0]["hooks"][0]["timeout"], 60)
            self.assertEqual(groups[0]["hooks"][0]["additionalContextLimit"], 1200)

    def test_main_installs_and_uninstalls_in_explicit_project(self):
        output = io.StringIO()
        bundle = self.bundle
        with mock.patch.object(installer, "BUNDLE", self.root / "wheel-without-plugin-directory"), \
             contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(["--project", str(self.project), "--config", str(self.config),
                                             "--executable", str(self.executable),
                                             "--bundle", str(bundle)]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "installed")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(["--project", str(self.project),
                                             "--action", "uninstall"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "uninstalled")

    def test_bootstrap_passes_stdin_to_hook_and_is_inert_without_config(self):
        script = self.bundle / "hooks" / "bootstrap.sh"
        env = dict(os.environ, WX_AGENT_PLUGIN_EXECUTABLE=str(self.executable))
        env.pop("WX_AGENT_PLUGIN_CONFIG", None)
        inert = subprocess.run([str(script)], input=b'{"hook_event_name":"SessionStart"}',
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=True)
        self.assertEqual(inert.stdout, b"")
        env["WX_AGENT_PLUGIN_CONFIG"] = str(self.config)
        payload = b'{"hook_event_name":"PostToolUse"}'
        result = subprocess.run([str(script)], input=payload, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, check=True)
        self.assertEqual(result.stdout, payload)


if __name__ == "__main__":
    unittest.main()
