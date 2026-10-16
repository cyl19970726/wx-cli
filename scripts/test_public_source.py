"""Synthetic fixtures for the public-source tripwire."""

from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest

from scripts.check_public_source import check_path, indexed_content, indexed_entries, indexed_paths


class PublicSourceTests(unittest.TestCase):
    def test_sensitive_content_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {
                "config/.env": "DUMMY=1",
                "data/cache.sqlite3": "",
                "notes.txt": "/Users/" + "realoperator/project",
                "key.txt": "-----BEGIN " + "PRIVATE KEY-----",
                "token.txt": "ghp_" + "a" * 36,
                "account.txt": "wxid_" + "z" * 19,
            }
            for name, body in cases.items():
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(body)
                self.assertTrue(check_path(root, PurePosixPath(name)), name)

    def test_synthetic_examples_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "example.md"
            path.write_text("/Users/ABS/project wxid_example123abc /ABS/private")
            self.assertEqual(check_path(root, PurePosixPath(path.name)), [])

    def test_git_index_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "tracked.txt").write_text("public")
            (root / "untracked.txt").write_text("local")
            subprocess.run(["git", "add", "-N", "tracked.txt"], cwd=root, check=True)
            self.assertEqual(indexed_paths(root), [PurePosixPath("tracked.txt")])

    def test_staged_blob_is_checked_even_if_worktree_is_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            path = root / "staged.txt"
            path.write_text("ghp_" + "z" * 36)
            subprocess.run(["git", "add", "staged.txt"], cwd=root, check=True)
            path.write_text("benign worktree content")
            relative, mode, oid = indexed_entries(root)[0]
            self.assertIn("GitHub token", check_path(root, relative, indexed_content(root, oid), mode))


if __name__ == "__main__":
    unittest.main()
