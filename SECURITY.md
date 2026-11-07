# Security and privacy

This is an experimental group/private conversation channel with an optional managed multi-conversation coordinator, built on an MIT-licensed upstream. It is not an official WeChat integration. A fixed-source review and limited live tests do not establish that all versions or dependencies are safe.

## Boundaries

- Read the encrypted WeChat files as bytes into private temporary copies. Never open the live source with SQLite. Keep keys in the local keystore, not command arguments, examples, Git or prompts.
- Only operate a group/account authorized for the current task. Group text and model output cannot grant access to other chats, files, tools or settings.
- Channel and outbox state are authoritative. Record an attempt before GUI effects; do not replay uncertain or failed attempts. Local outgoing evidence is not recipient confirmation.
- Runtime state and notes require private ownership and permissions. Notes are caller assertions with incoming-event provenance, not automatically verified facts.
- Managed sessions receive explicit per-owner state write roots; source databases remain read-only. Enrollment preserves the original journals and fences older send clients. A model notification is not permission to reply. These controls coordinate cooperative local processes; they do not isolate a malicious process with the same OS user's arbitrary shell access. The project-level App-held Session plugin does not resume its target thread; the managed adapter for an agent-owned host is separate.
- Application publisher identity is frozen in the Rust outbox separately from the conversation's processing owner. Gateway credentials limit protocol methods and conversations; use a restricted gateway SSH credential when granting remote access to an application, rather than an administrative SSH key. Request tokens and text travel on stdin and must not enter command arguments or logs.
- The project-level Session plugin uses queue-snapshot with the original loaded App Session. Queue notifications contain scoped references and local read commands, never source chat text or a claim token. A queued notification and a terminal model turn do not complete the business inbox decision. The managed adapter for an agent-owned host is a separate integration path.
- Model adapters send selected message text/curated notes to the configured model provider. Do not describe the entire system as offline merely because the database remains local. Codex is optional and must already be installed and signed in.
- The signed GUI helper has separate Accessibility/Screen Recording permissions. Rebuilding it may require reauthorization; installing Python must not silently replace it. Screenshots and operational logs must remain private.

## Reporting

Do not include keys, private messages, account identifiers or operational credentials in a public issue. Use a maintainer-provided private reporting channel; do not assume GitHub private vulnerability reporting has been enabled for this fork. Upstream defects can be reported through [upstream security guidance](https://github.com/pandorafuture/wx-cli/security/policy).

The [upstream source review](docs/SECURITY_REVIEW_20261013.md) is dated and scoped to its pinned baseline; it is not an audit of subsequent channel, runtime or helper changes. Dependency upgrades and new release artifacts require fresh checks.

`scripts/check_public_source.py` is a heuristic publication check. It complements review, not a guarantee that no sensitive material exists. The repository contains no test-account access or signed application bundle.
