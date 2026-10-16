# Conversation reliability candidate · 2026-10-17

The private chat → test group → paginated read → reply flow now has additional
offline regression coverage and a candidate fix based on `bfe335e`. This is a
proposal for independent review, not a live deployment or recipient-confirmed round trip.
The installed helper remains the documented v5. The exact cause of the earlier
v4 failure remains unknown; these regressions identify code failure paths,
not retrospective proof of that incident's cause.

## Changes and evidence

- The Python driver previously allowed only eight seconds for native focus,
  although the native six-second search loop begins after guarded UI setup and
  is followed by row and header confirmation. A virtual-clock regression first
  failed in both focus-only and sending when focus completed after nine seconds.
  Both now allow 18 seconds for focus; standalone focus uses the existing
  48-second driver budget for the complete operation. The test also checks late
  rejection, no preparation/submission after failure, and unchanged original
  artifacts when the same send is presented again.
- A helper launched through `open` may outlive a timed-out Python driver and its
  UI lock. Both desktop entrypoints now reuse the existing helper-liveness check
  under that lock before launching anything. A live helper or failed process
  inspection refuses new desktop actions. Regression cases verify that prior
  artifacts remain unchanged.
- Native discovery discards a popup capture if its ID/frame changes or it
  disappears during the read-only probe, then observes again within the same
  six-second budget. Final pre-click changes still refuse navigation. Main
  window, popup geometry, uniqueness, category, target kind, header and draft
  guards remain required. Swift cases cover loading resize, disappearance,
  replacement, geometry tolerance, and strict confirmation. The search budget
  now uses monotonic uptime.
- The synthetic private-to-group regression paginates incoming events, replies
  with channel-scoped stable IDs, repeats replies without another send, and
  verifies no consumer acknowledgement or runner lifecycle action. An existing
  indeterminate request and its attempt evidence remain unchanged; presenting
  that original request again refuses another send. No Rust schema, binding,
  cursor, outbox or recovery contract changes are needed.

## Toolchain and required checks

macOS arm64; Git 2.50.1; rustc/cargo 1.97.1; Clippy 0.1.97; rustfmt 1.9.0;
Apple Swift 6.3.3. Python 3.9.6 uses pip 26.0.1; Python 3.12.14 uses pip 26.2.1.
Both Python environments were created specifically for this validation. The
3.12 interpreter came from the bundled workspace runtime. Wheel checks install
into fresh temporary environments and execute package tests outside the checkout.

Commands below use these aliases for the actual validation paths:

```sh
check_dir=.local/reliability-validation-20261017
py39="$check_dir/python39/bin/python"
py312="$check_dir/python312/bin/python"
```

| Command | Actual result |
| --- | --- |
| `cargo fmt --check` | Pass. |
| `cargo clippy --locked --all-targets -- -D warnings` | Pass. |
| `cargo test --locked` | 841 passed, 0 failed, 11 pre-existing ignored across 46 reported suites. Ignored tests are not passes. |
| `cargo build --locked` | Pass. |
| `"$py312" scripts/test_local_channel.py --binary target/debug/wx-cli` | Pass: synthetic reads, 1,100-message backlog, deduplication, restart acknowledgement, unchanged source during reads, and queued-only outbox. No real WeChat used. |
| `PYTHONPATH=packages/wx-agent/src "$py312" -m unittest scripts.test_macos_sender_driver.SenderTests.test_real_synthetic_channel_event_contract` | 1 passed, 0 failed/skipped. |
| `PYTHONPATH=packages/wx-agent/src "$py39" -m unittest discover -s scripts -p 'test_*.py'` | 76 passed, 0 failed/skipped. |
| `PYTHONPATH=packages/wx-agent/src "$py312" -m unittest discover -s scripts -p 'test_*.py'` | 76 passed, 0 failed/skipped. |
| `"$py39" -m pip wheel --no-deps --wheel-dir "$check_dir/wheels39" ./packages/wx-agent` | Pass. |
| `"$py39" scripts/check_package.py "$check_dir"/wheels39/*.whl --tests packages/wx-agent/tests` | 50 installed-package tests passed, 0 failed/skipped; entrypoint and isolated import passed. |
| `"$py312" -m pip wheel --no-deps --wheel-dir "$check_dir/wheels312" ./packages/wx-agent` | Pass. |
| `"$py312" scripts/check_package.py "$check_dir"/wheels312/*.whl --tests packages/wx-agent/tests` | 50 installed-package tests passed, 0 failed/skipped; entrypoint and isolated import passed. |
| `WX_BRIDGE_BUILD_DIR="$PWD/$check_dir/native" native/macos/wechat-bridge/test-parameters.sh` | Pass, including the isolated named-pasteboard checks. One test executable; it does not report an assertion count and does not operate WeChat. |
| `WX_BRIDGE_BUILD_DIR="$PWD/$check_dir/candidate" native/macos/wechat-bridge/build.sh` | Isolated candidate app built; ad hoc signature verification passed. It was not launched or installed. |
| `"$py39" scripts/check_public_source.py` and `"$py312" scripts/check_public_source.py` | Both pass; tracked-source heuristic only, supplemented by manual diff review. |
| `git diff --check` | Pass. |

Full native source typechecking also passed:

```sh
xcrun swiftc -typecheck -parse-as-library -swift-version 5 \
  -target arm64-apple-macos14.0 \
  -framework AppKit -framework ApplicationServices -framework CoreGraphics \
  -framework ScreenCaptureKit -framework Vision \
  native/macos/wechat-bridge/main.swift \
  native/macos/wechat-bridge/VisualParameters.swift \
  native/macos/wechat-bridge/ClipboardSupport.swift
```

Candidate helper SHA-256:
`e72ea65efe2732d58fd40164f65d4a65324f558f8dbca9f4e2e68711ee5af02a`.
Detailed logs and candidate artifacts remain in the ignored validation directory.

## Preserved runtime and controlled acceptance plan

A fresh, metadata-only inspection found the runner stopped, no manual pending
work or active interactive lease, zero blocking outbox entries, and three
retained historical uncertain requests. Nineteen files under the bound state
and registry directories were fingerprinted and verified unchanged while
preparing private rollback copies. The old helper copy passed signature
verification and matches the documented v5 digest. Relevant configs were copied
privately. No source database, live configuration, helper or installed package
was modified; no GUI navigation or message send was performed.

Independent review and explicit inclusion of the helper/runtime upgrade in the
task scope are required by [the deployment agreement](REMOTE_DEVELOPMENT.md)
before installing this candidate. The prepared sequence is:

1. Review the complete diff and these results. Recheck stopped writer, bindings,
   consumer/cursors, pending work and original outbox before installation. Keep
   the private rollback material and original Python environment.
2. Install the candidate wheel into a separate environment and replace the helper
   only at its existing fixed app path. Update the relevant private helper
   digests/executable routing without changing account, source or state paths.
3. Verify the installed helper hash/signature and fresh Accessibility, Screen
   Recording and Post Events permissions. Recheck the dedicated desktop and
   current calibrated window; do not infer coordinates or relax guards.
4. Perform a bounded private-chat → group → paginated-read → reply test using
   only the already authorized test identities and distinct new synthetic
   intents. Preserve all old requests, including the failed v4 request. Stop on
   any focus/send failure; do not retry, replace an ID, or use GUI workarounds.
   Confirm matching local outgoing talker/events and then obtain independent
   receipt confirmation. Keep private messages/screenshots off public outputs.
5. Leave the runner in its prior stopped state. If rollback is needed, restore
   the old code/helper/config routing and verify identity/permissions again.
   Retain the *current* outbox and cursors, including every new attempt; never
   restore an older state snapshot over post-attempt records.

The candidate has not passed live navigation, sending, recipient confirmation,
the complete desktop round trip, or long-run acceptance. Historical v3/v5
evidence remains scoped as described in the [existing validation record](CONVERSATIONS_VALIDATION_20261016.md).
