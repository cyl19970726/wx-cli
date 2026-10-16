#!/usr/bin/env python3
"""Heuristic guard for files about to become public source.

Only Git's index is checked by default, so ignored runtime data is never read.
This is a tripwire, not a substitute for a secret scanner or human review.
"""

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".log"}
PRIVATE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json"}
PRIVATE_DIRS = {".local", "__pycache__", "node_modules", "target", "dist", "build"}
TOKEN_RULES = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{30,}")),
    ("OpenAI key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{24,}")),
    ("local user path", re.compile(r"/Users/(?!ABS/|example/|username/|user/)[A-Za-z0-9._-]+/")),
    ("WeChat account ID", re.compile(r"wxid_(?!example|test|nonexistent)[A-Za-z0-9]{16,}")),
)
MAX_TEXT_BYTES = 2_000_000


def indexed_entries(root):
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--stage", "-z", "--full-name"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    entries = []
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        metadata, name = item.split(b"\t", 1)
        mode, oid, _stage = metadata.split(b" ", 2)
        entries.append((PurePosixPath(name.decode("utf-8", errors="surrogateescape")),
                        mode.decode("ascii"), oid.decode("ascii")))
    return entries


def indexed_paths(root):
    return [path for path, _mode, _oid in indexed_entries(root)]


def check_path(root, relative, content_bytes=None, mode=None):
    """Return labels for likely private material in one indexed path."""
    relative = PurePosixPath(relative)
    labels = []
    name = relative.name.lower()
    if any(part in PRIVATE_DIRS for part in relative.parts[:-1]):
        labels.append("private/generated directory")
    if name in PRIVATE_NAMES or (name.startswith(".env.") and name != ".env.example"):
        labels.append("private filename")
    if "screenshot" in name or "screen-shot" in name:
        labels.append("possible private screenshot")
    if relative.suffix.lower() in PRIVATE_SUFFIXES:
        labels.append("private data/key extension")
    path = root.joinpath(*relative.parts)
    if mode == "120000" or path.is_symlink():
        labels.append("symlink requires manual review")
        return labels
    if content_bytes is None and not path.is_file():
        labels.append("indexed file is missing")
        return labels
    raw = path.read_bytes() if content_bytes is None else content_bytes
    if len(raw) > MAX_TEXT_BYTES:
        labels.append("large file; inspect manually")
        return labels
    if b"\0" in raw:
        return labels
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return labels
    for label, rule in TOKEN_RULES:
        if rule.search(content):
            labels.append(label)
    return labels


def indexed_content(root, oid):
    # Intent-to-add entries have a zero object ID. They have no staged blob yet.
    if set(oid) == {"0"}:
        return None
    return subprocess.run(["git", "cat-file", "blob", oid], cwd=root,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    root = args.root.resolve()
    entries = indexed_entries(root)
    problems = [
        (str(path), check_path(root, path, indexed_content(root, oid), mode))
        for path, mode, oid in entries
    ]
    problems = [(path, labels) for path, labels in problems if labels]
    for path, labels in problems:
        print("{}: {}".format(path, ", ".join(labels)), file=sys.stderr)
    if problems:
        print("Public-source guard found {} suspect file(s).".format(len(problems)), file=sys.stderr)
        return 1
    print("Public-source heuristic check passed ({} indexed files).".format(len(entries)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
