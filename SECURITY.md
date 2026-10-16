# Security and privacy

This is an experimental, single-group channel built on an MIT-licensed upstream. It is not an official WeChat integration. A fixed-source review and limited live tests do not establish that all versions or dependencies are safe.

## Boundaries

- Read the encrypted WeChat files as bytes into private temporary copies. Never open the live source with SQLite. Keep keys in the local keystore, not command arguments, examples, Git or prompts.
- Only operate a group/account authorized for the current task. Group text and model output cannot grant access to other chats, files, tools or settings.
- Channel and outbox state are authoritative. Record an attempt before GUI effects; do not replay uncertain or failed attempts. Local outgoing evidence is not recipient confirmation.
- Runtime state and notes require private ownership and permissions. Notes are caller assertions with incoming-event provenance, not automatically verified facts.
- Model adapters send selected message text/curated notes to the configured model provider. Do not describe the entire system as offline merely because the database remains local. Codex is optional and must already be installed and signed in.
- The signed GUI helper has separate Accessibility/Screen Recording permissions. Rebuilding it may require reauthorization; installing Python must not silently replace it. Screenshots and operational logs must remain private.

## Reporting

Do not include keys, private messages, account identifiers or operational credentials in a public issue. Use a maintainer-provided private reporting channel; do not assume GitHub private vulnerability reporting has been enabled for this fork. Upstream defects can be reported through [upstream security guidance](https://github.com/pandorafuture/wx-cli/security/policy).

The [upstream source review](docs/SECURITY_REVIEW_20261013.md) is dated and scoped to its pinned baseline; it is not an audit of subsequent channel, runtime or helper changes. Dependency upgrades and new release artifacts require fresh checks.

`scripts/check_public_source.py` is a heuristic publication check. It complements review, not a guarantee that no sensitive material exists. The repository contains no test-account access or signed application bundle.
