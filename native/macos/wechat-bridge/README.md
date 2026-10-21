# WeChat Test Bridge

Single-purpose macOS helper source for a controlled WeChat text test. It is an
`LSUIElement` app with bundle ID `io.codex.wechat-test-bridge`, targeting arm64
macOS 14 or later. It uses native Apple frameworks only. Build with `./build.sh`;
the script places the app in the repository's ignored `.local/bridge-build/`,
signs it ad hoc, and verifies its signature. No app bundle or runtime output is
committed. A rebuild can change the code signature associated with macOS permissions.

Limited live text, group-selection and refusal tests are summarized in
[validation scope](../../../docs/REAL_READ_VALIDATION_20261015.md). Installing the
Python package does not rebuild or replace this separately authorized app.

## Commands

Put each JSON input under `~/wechat-wx-cli-test/ui/`, then launch from the
logged-in macOS desktop context:

```sh
open -g -n "/path/to/WeChat Test Bridge.app" --args --input "$HOME/wechat-wx-cli-test/ui/command.json"
```

Actions are `request-permissions`, `inspect`, `capture`, `focus-group`,
`prepare-visual`, and `submit-visual`. The first three check permissions,
inspect WeChat window metadata, and take a window-only PNG, respectively. `inspect` reports
`frontmost` and session lock flags separately.

`focus-group` takes its own unique `id`, exact target title, and `targetKind`
(`group` or `private`). Use a
fresh `inspect` and `capture` to calibrate the sole window and the left
conversation list/search control; these coordinates are examples, not defaults:

```json
{"id":"example-focus-001","action":"focus-group",
 "targetGroup":"Synthetic Test Group","targetKind":"group","expectedPID":12345,
 "expectedWindowID":678,
 "expectedFrame":{"x":100,"y":100,"width":900,"height":650},
 "conversationListRect":{"x":0,"y":60,"width":270,"height":570},
 "searchPoint":{"x":100,"y":30}}
```

The helper checks permissions, console state, the expected running PID and
window ID/frame before requesting activation of that same process. If it is
already frontmost, no activation is requested. Otherwise it makes one
`NSRunningApplication.activate(options: [])` request, waits at most two seconds,
then re-enumerates and rechecks the PID, window and actual foreground before
capturing or entering search text. It does not open an application by name or
path. A denied request, changed process/window or unavailable foreground stops
the operation. macOS may decline an activation request; see
[Apple's activation documentation](https://developer.apple.com/documentation/appkit/nsrunningapplication/activate%28options%3A%29).
The focus attempt marker is created before activation or input.

The Python driver delegates activation to this action. Deploy the matching
Python and native candidates together through the controlled upgrade process;
an older helper still requires WeChat to be frontmost and may refuse. Building
either component alone does not upgrade the live runtime.

The helper pastes and copies back the exact title in the calibrated search box.
Search may create one additional WeChat-owned popup beside the expected main
window. Navigation captures that popup separately and accepts its bounded search
geometry down to 160px high. The short popup is also visible to the sole-window
send guard, so send cannot proceed while it is open. Navigation accepts a unique
exact result below Contacts for `private`, Group Chats for
`group`, or Most used for either kind (including Chinese labels). Most used
does not establish the conversation kind; the exact postclick header does.
Duplicate exact names across conversation categories are refused. Internet
search results and web suggestions are never selectable. Unrecognized text
aligned with a section heading closes the preceding category; it cannot open
an eligible category. Alignment uses half the smaller text height around the
recognized heading's left edge. This prevents a misread history heading from
assigning history rows to Contacts or Group Chats. Exact-label recognition,
the 80px row distance limit and duplicate-result refusal remain required. It
checks the popup ID, frame and row again immediately before clicking, then
requires the sole expected main window and kind-aware exact chat header afterward.
Group headers must show the exact name followed by a numeric member count;
private headers must show the exact name without that suffix. Names already
ending in a numeric parenthesized suffix are refused as ambiguous.
Missing, ambiguous, moved, or mismatched results stop. Before navigation it
creates `<id>.focus-attempted.json`; an uncertain attempt with the same ID is
not replayed. A successful search writes `<id>.focus-evidence.json` with the
title, kind, PID, window ID/frame, actual search category, source and time. The
output is `<id>.result.json`.
Popup discovery is bounded to six seconds. The private `<id>.focus-polls.json`
records each probe's elapsed time, WeChat window IDs and frames, and whether a
popup and eligible result were seen. It contains no OCR text, chat titles or
message content. Timeout errors distinguish an unseen popup from a visible
popup without a unique eligible result.
The search budget uses monotonic time. During read-only discovery, a popup
that resizes, disappears, or changes ID during capture causes those pixels to
be discarded and another observation within the same budget. Any such change
during the final pre-click confirmation still stops navigation.
Title matching depends
on fresh OCR and is not proof of delivery or account identity.

The two visual actions use the **same** transaction `id` and all the same input
fields. Change only `action` from `prepare-visual` to `submit-visual`:

```json
{"id":"example-001","action":"prepare-visual",
 "targetGroup":"Synthetic Test Group","targetKind":"group",
 "focusEvidenceID":"example-focus-001","expectedDraft":"old draft",
 "text":"Hello, test message 😊\nSecond line",
 "expectedPID":12345,"expectedWindowID":678,
 "expectedFrame":{"x":100,"y":100,"width":900,"height":650},
 "composerRect":{"x":310,"y":500,"width":560,"height":95},
 "buttonPoint":{"x":830,"y":610}}
```

`body` is accepted as an alias for `text`; if both appear, they must match.
`text` supports Unicode Chinese, punctuation, emoji, tabs, and multiple lines.
It must contain meaningful text, have at most 2,000 Swift characters and
16,000 UTF-8 bytes, and contain no NUL, DEL, or other control characters except
tab and line endings. `expectedDraft` follows the same limits and may be empty.
Only CRLF/CR-to-LF normalization is allowed when comparing copied text; spaces
and all other characters remain significant. `targetGroup` must be an explicit
single-line name of at most 100 characters. Visual actions require a matching
category-verified `focusEvidenceID` less than 120 seconds old. The example PID, window ID, frame,
composer rect, and button point are not defaults: obtain them from a fresh
`inspect` and `capture` immediately before use. Rects/points are image-relative;
the frame is in global screen coordinates.

An empty composer can display input guidance that is detected as text. An
optional `emptyComposerReference` object with `path` and `sha256` permits an
explicitly calibrated empty-state PNG. The path must be a direct child of the
private output directory, owned, mode `0600`, not a symlink or hard link; its
pinned SHA-256 and decoded dimensions must match the integral composer rect.
Calibrate only after establishing that no user draft exists. Keep this image
private. The helper compares every composer pixel in sRGB without tolerance,
masked regions or a text whitelist. It checks up to three fresh observations
to accommodate a blinking caret. Target/window/focus checks and the unchanged
clipboard sentinel are still required throughout; copied nonempty text never
uses this fallback. Both visual stages must retain the same reference. Without
the option, the existing blank-image check applies. Recalibrate after any UI
appearance or geometry change; do not capture an unknown draft as a reference.

The Send-button OCR accepts the exact labels `Send` or `发送`. There must be
exactly one accepted label in the lower conversation pane, and the calibrated
button crop must contain that label alone. Duplicate labels, mixed-language
duplicates, extended labels and labels outside the button crop still refuse.

The app requires its output directory and parent to be owned by the current
user, non-symlinks, and mode `0700`. Runtime output stays in
`~/wechat-wx-cli-test/ui/`. This path is currently fixed in the code and must
be made configurable before using the helper as a reusable channel component.
Deployment alone does not make this a validated or production-ready sender.
Read-only actions write `<id>.result.json`; visual actions write
`<id>.<action>.result.json` and step-named window PNGs.
`prepare-visual` creates `<id>.visual-state.json` exclusively before the first
mouse event. It becomes `prepared` only after exact copied-text verification.
`submit-visual` requires that state and creates `<id>.submit-attempted.json`
exclusively before the Send click. A failed or uncertain attempt cannot be
replayed with the same ID.

Every send UI step requires one WeChat process/window, the declared PID/ID/frame,
WeChat frontmost, `onConsole=true`, no explicit locked flag, Accessibility,
Screen Recording, Post Events access, a nonblank window-only screenshot, and
fresh OCR of the exact `targetGroup` header. An absent lock flag remains
`unknown`; it is not proof of unlock. Send must be the unique recognized button
in the lower conversation pane. The helper never uses Return to send.
Only search navigation may observe the bounded second WeChat popup; it cannot
prepare or submit a message while that popup is present.

For draft verification, the helper clicks the declared composer, selects it,
sets a unique clipboard sentinel, copies, and compares the copied plain text
exactly to `expectedDraft` or `text`. If copying an empty draft leaves the
sentinel unchanged, it additionally requires a visually blank composer; any
uncertainty stops. Preparation pastes plain text over the selected old draft,
then copies it back to verify. Submission copies it back again before a fresh
header and Send-button check and one click. It does not send media.

The original clipboard is snapshotted in memory only and never written to an
output file. Restoration occurs only if the pasteboard change count still
matches the helper's last known write. A detected external change is preserved
and stops the command. macOS pasteboard has no atomic compare-and-swap, so an
unobserved change in the tiny interval between the final count check and
restoration cannot be ruled out. Large or unreadable clipboard items stop the
command rather than being discarded.

Posting a Send click is **not delivery proof**; verify independently with the
authorized test account. A failed step may leave a partial draft. `./test-parameters.sh`
runs local, no-GUI checks of text validation, line-ending comparison,
transaction consistency, empty-copy handling, and clipboard restoration
conditions.

## Outbox sender integration

`wx-agent-sender` from the installed `wx-agent` package is the one-attempt
driver for `wx-cli outbox send --request-id ... --driver /ABS/venv/bin/wx-agent-sender`. It reads
the outbox JSON from standard input and returns a JSON object with `status`
(`ui_observed`, `failed`, or `indeterminate`) and nonempty `evidence`.
The executable and its `WX_SENDER_CONFIG` must reside on the Mac running
WeChat; `outbox send` passes the driver path to that same machine. The private
configuration is a `0600` JSON file in a `0700` parent directory. It binds the
encrypted-copy channel (`binding` as returned by `channel status`, including a
known `self_wxid`), absolute `wx_cli` and `channel_state_dir` paths, exact
`target_group`, absolute built `helper_app`, `helper_sha256` of its executable,
fixed private `ui_dir` (`~/wechat-wx-cli-test/ui`), `expected_pid`, current
`window_size`, `composer_rect`, `button_point`, `conversation_list_rect`, and
`search_point`. `expected_draft` is optional and defaults to empty. Use fresh
window inspection and capture to set the geometry; never reuse example values.

To select the configured test group independently of any message or outbox
request, run this on the Mac with the unlocked WeChat desktop and the final
helper's permissions restored:

```sh
WX_SENDER_CONFIG=/ABS/private/sender-config.json \
  /ABS/venv/bin/wx-agent sender --focus-only
```

This reads no message from stdin, creates no outgoing intent, and does not
prepare a draft or click Send. It returns JSON with `focused`,
`already_selected`, and `source` (`current`, `visible-list`, or `search`) on
success. Failure returns `focused:false` and a reason. `--focus-only` uses the
same bound account, exact group title, window calibration, UI lock, and fresh
header verification as the sender. Its result is a focus check, never a send
or delivery confirmation.

The driver inspects the window, focuses the group, prepares and verifies the
Unicode draft, records a fresh channel cursor, submits once, and seeks the
matching local outgoing event. Its `ui_observed` result means this local event
was seen; independent receipt is still a separate check. It keeps a private
attempt report and refuses to replay a request. Any uncertain stage returns
`indeterminate`, which the outbox and channel runner must not automatically
retry. See the linked validation scope for the historical evidence and limitations.

The Python driver waits up to 18 seconds for the complete native focus action,
including setup and result reconfirmation, within its 48-second total budget.
After acquiring the shared UI lock, both focus-only and sending refuse to
launch a helper if any earlier helper remains live or process inspection fails.
A timeout can outlive the driver lock; acquiring that lock alone is insufficient
evidence that the desktop is idle. These source changes have offline coverage;
their deployment and live acceptance status are recorded in
[the reliability candidate record](../../../docs/CONVERSATIONS_RELIABILITY_20261017.md).
