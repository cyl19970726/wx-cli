# Contributing

Keep changes within the ownership boundaries in [architecture](docs/architecture.md). Preserve upstream attribution and the MIT license. Runtime secrets, screenshots, live messages and deployment files stay outside Git. Use synthetic fixtures.

## Checks

`.github/workflows/ci.yml` is the source of truth for required CI. It runs on macOS and covers:

- Rust formatting, all-target Clippy with warnings denied, tests and build; a synthetic channel CLI integration and sender contract check on Python 3.12.
- Repository scripts and wheel build/install/package tests on both Python 3.9 and 3.12.
- Swift native bridge parameter tests.

Before acceptance, run the relevant CI checks locally and run the full matrix for cross-layer, protocol, packaging, or native changes. The commands are maintained in the workflow; report which checks ran, exact interpreter/tool versions, pass/fail counts, pre-existing ignored tests and anything not run. Do not describe ignored tests as passed. Public-source and diff checks are also expected before handoff:

```sh
python3 scripts/check_public_source.py
git diff --check
```

For local wheel work, use a project virtual environment and a current pip rather than macOS's system Python/pip. For example:

```sh
python3.12 -m venv .local/wheel-venv
.local/wheel-venv/bin/python -m pip install --upgrade pip
mkdir -p .local
wheel_dir="$(mktemp -d .local/wheel-check.XXXXXX)"
.local/wheel-venv/bin/python -m pip wheel --no-deps --wheel-dir "$wheel_dir" ./packages/wx-agent
.local/wheel-venv/bin/python scripts/check_package.py "$wheel_dir"/*.whl --tests packages/wx-agent/tests
```

If an old system pip creates a malformed wheel (for example, with missing project metadata), discard that artifact and rebuild in the virtual environment; do not weaken package checks. `check_package.py` installs the wheel in an isolated environment and runs package tests outside the checkout, including console entrypoints. A source-only `PYTHONPATH` does not verify installed entrypoints. Native parameter tests do not operate WeChat or prove desktop delivery.

## Changes and reviews

Use a focused `codex/` branch for agent-authored work. Include the triggering problem, resulting behavior, actual checks and remaining limits. Protocol or state changes need compatibility and restart tests. Preserve private binding, cursors, pending replies and outbox; never delete them to make a migration pass. UI driver changes need evidence at their actual layer, not just process exit codes. For remote development, handoff and independent local acceptance, follow [REMOTE_DEVELOPMENT.md](docs/REMOTE_DEVELOPMENT.md).

`packages/wx-agent` is an independently installable Python distribution in the same repository. Rust modules, Python orchestration and Swift helper must not duplicate each other's authoritative stores. Keep old source launchers thin. Public package metadata and fixture-only tests must survive wheel installation outside the source tree.

## Releases and upgrades

Tags trigger the repository's release workflow; do not create tags as part of ordinary testing. CI gates precede artifact publication. A Rust archive/Python wheel does not include a signed/notarized GUI application, macOS permissions, credentials or runtime state. Versioned source and release artifacts do not mean the live test host has been upgraded.

Upgrade an active deployment explicitly: inspect status/pending, stop cleanly, back up private state, install separately, verify binding/command migration and rollback, then run bounded checks. Keep the old signed helper until a separately reviewed helper upgrade is needed.

## Skills and diagrams

The only project skill lives at `.agents/skills/wechat-group-agent/`. Do not create global links while developing it. The user's private machine-operation skill is separate and is not distributed in this public repository.

Architecture diagram source and generated HTML are versioned together, with source references pinned to a code commit. Use Archify to regenerate and pass its checks; do not manually edit generated HTML. Tests cannot certify visual design or deployment claims.
