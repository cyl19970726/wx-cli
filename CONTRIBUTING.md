# Contributing

Keep changes within the ownership boundaries in [architecture](docs/architecture.md). Preserve upstream attribution and the MIT license. Runtime secrets, screenshots, live messages and deployment files stay outside Git. Use synthetic fixtures.

## Checks

From the repository root on macOS:

```sh
cargo fmt --check
cargo clippy --locked -- -D warnings
cargo test --locked
cargo build --locked
python3 scripts/test_local_channel.py --binary target/debug/wx-cli
PYTHONPATH=packages/wx-agent/src python3 -W error::ResourceWarning -m unittest discover -s scripts -p 'test_*.py'
PYTHONPATH=packages/wx-agent/src python3 -W error::ResourceWarning -m unittest discover -s packages/wx-agent/tests -p 'test_*.py'
python3 -m pip wheel --no-deps --wheel-dir .local/wheels ./packages/wx-agent
python3 scripts/check_package.py .local/wheels/wx_agent-0.2.0-py3-none-any.whl --tests packages/wx-agent/tests
native/macos/wechat-bridge/test-parameters.sh
python3 scripts/check_public_source.py
git diff --check
```

Use Python 3.9 or newer; CI exercises the supported floor and a newer version. Package smoke builds a wheel, installs it in an isolated environment and checks entrypoints outside the checkout. Native parameter tests do not operate WeChat or prove desktop delivery. Check the workflow for the exact current matrix.

## Changes and reviews

Use a focused `codex/` branch for agent-authored work. Include the triggering problem, resulting behavior, actual checks and remaining limits. Protocol or state changes need compatibility and restart tests. Preserve private binding, cursors, pending replies and outbox; never delete them to make a migration pass. UI driver changes need evidence at their actual layer, not just process exit codes.

`packages/wx-agent` is an independently installable Python distribution in the same repository. Rust modules, Python orchestration and Swift helper must not duplicate each other's authoritative stores. Keep old source launchers thin. Public package metadata and fixture-only tests must survive wheel installation outside the source tree.

## Releases and upgrades

Tags trigger the repository's release workflow; do not create tags as part of ordinary testing. CI gates precede artifact publication. A Rust archive/Python wheel does not include a signed/notarized GUI application, macOS permissions, credentials or runtime state. Versioned source and release artifacts do not mean the live test host has been upgraded.

Upgrade an active deployment explicitly: inspect status/pending, stop cleanly, back up private state, install separately, verify binding/command migration and rollback, then run bounded checks. Keep the old signed helper until a separately reviewed helper upgrade is needed.

## Skills and diagrams

The only project skill lives at `.agents/skills/wechat-group-agent/`. Do not create global links while developing it. The user's private machine-operation skill is separate and is not distributed in this public repository.

Architecture diagram source and generated HTML are versioned together, with source references pinned to a code commit. Use Archify to regenerate and pass its checks; do not manually edit generated HTML. Tests cannot certify visual design or deployment claims.
