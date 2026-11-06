//! Positive late source proof. Never invokes a driver or changes an attempt.
use std::collections::BTreeMap;
use std::error::Error;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::channel::{self, BindingView};
use super::{coordinator, outgoing_media, outgoing_verify};

type Result<T> = std::result::Result<T, Box<dyn Error>>;
fn invalid(message: &str) -> Box<dyn Error> {
    io::Error::new(io::ErrorKind::InvalidData, message).into()
}
fn require(ok: bool, message: &str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(invalid(message))
    }
}
fn digest(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

pub(super) fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch("CREATE TABLE IF NOT EXISTS outgoing_observation (
        request_id TEXT PRIMARY KEY, attempt_number INTEGER NOT NULL CHECK(attempt_number=1),
        channel_id TEXT NOT NULL, session_id TEXT NOT NULL, cursor INTEGER NOT NULL CHECK(cursor>0),
        event_server_id INTEGER NOT NULL, original_status TEXT NOT NULL CHECK(original_status='indeterminate'),
        original_path TEXT NOT NULL, original_sha256 TEXT NOT NULL,
        verified_path TEXT NOT NULL, verified_sha256 TEXT NOT NULL, recorded_at INTEGER NOT NULL,
        UNIQUE(channel_id,session_id,cursor), UNIQUE(channel_id,session_id,event_server_id)
    );
    CREATE TRIGGER IF NOT EXISTS outgoing_observation_no_update BEFORE UPDATE ON outgoing_observation
      BEGIN SELECT RAISE(ABORT,'outgoing observations are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS outgoing_observation_no_delete BEFORE DELETE ON outgoing_observation
      BEGIN SELECT RAISE(ABORT,'outgoing observations are retained'); END;")?;
    Ok(())
}

fn private(path: &Path, directory: bool) -> Result<()> {
    require(
        path.is_absolute() && path.canonicalize()? == path,
        "artifact path is not canonical",
    )?;
    let m = fs::symlink_metadata(path)?;
    require(
        m.uid() == unsafe { libc::geteuid() }
            && !m.file_type().is_symlink()
            && if directory {
                m.is_dir() && m.permissions().mode() & 0o777 == 0o700
            } else {
                m.is_file() && m.nlink() == 1 && m.permissions().mode() & 0o777 == 0o600
            },
        "artifact must be private, owned, regular and unlinked",
    )
}
fn read(path: &Path, limit: u64, empty: bool) -> Result<Vec<u8>> {
    private(path, false)?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let before = file.metadata()?;
    require(
        before.is_file()
            && before.nlink() == 1
            && before.uid() == unsafe { libc::geteuid() }
            && before.permissions().mode() & 0o777 == 0o600
            && before.len() <= limit
            && (empty || before.len() > 0),
        "artifact size or descriptor invalid",
    )?;
    let mut bytes = Vec::new();
    (&file).take(limit + 1).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    require(
        bytes.len() as u64 == before.len()
            && before.dev() == after.dev()
            && before.ino() == after.ino()
            && before.len() == after.len()
            && before.mtime() == after.mtime()
            && before.mtime_nsec() == after.mtime_nsec()
            && before.ctime() == after.ctime()
            && before.ctime_nsec() == after.ctime_nsec(),
        "artifact changed during read",
    )?;
    Ok(bytes)
}
fn lock(root: &Path, name: &str) -> Result<File> {
    let path = root.join(name);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&path)?;
    private(&path, false)?;
    require(
        unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0,
        "shared executor/sender lock busy",
    )?;
    Ok(file)
}
#[derive(Debug, PartialEq)]
struct Intent {
    status: String,
    evidence: String,
    owner: String,
    epoch: i64,
    text: String,
    media: Value,
}
#[derive(Clone, Copy, PartialEq)]
enum ContentKind {
    Image,
    Text,
}
impl ContentKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Image => "image",
            Self::Text => "text",
        }
    }
}
fn intent(
    conn: &Connection,
    binding: &BindingView,
    rid: &str,
    kind: ContentKind,
) -> Result<Intent> {
    let row = conn.query_row("SELECT status,result_evidence,managed_owner,managed_epoch,media_path,media_sha256,media_mime_type,media_size_bytes,text
       FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND content_kind=?4 AND attempt_number=1",
       params![rid,binding.channel_id,binding.session_id,kind.as_str()], |r| Ok(Intent {
           status:r.get(0)?, evidence:r.get(1)?, owner:r.get(2)?, epoch:r.get(3)?,
           media:json!({"path":r.get::<_,Option<String>>(4)?,"sha256":r.get::<_,Option<String>>(5)?,
               "mime_type":r.get::<_,Option<String>>(6)?,"size_bytes":r.get::<_,Option<i64>>(7)?}), text:r.get(8)?,
       }))?;
    require(
        !row.owner.is_empty() && row.epoch > 0,
        "missing original owner binding",
    )?;
    require(
        match kind {
            ContentKind::Image => row.text.is_empty(),
            ContentKind::Text => {
                !row.text.is_empty()
                    && row.text.len() <= 65536
                    && row
                        .media
                        .as_object()
                        .is_some_and(|m| m.values().all(Value::is_null))
            }
        },
        "frozen content kind conflicts",
    )?;
    Ok(row)
}
fn attempt(conn: &Connection, rid: &str, original: &Path) -> Result<Value> {
    let rows: i64 = conn.query_row(
        "SELECT COUNT(*) FROM outgoing_attempt WHERE request_id=?1",
        [rid],
        |r| r.get(0),
    )?;
    require(rows == 1, "only original attempt may be reconciled")?;
    let row: (String,String,i64,Option<i64>,Option<i64>,Option<String>) = conn.query_row(
        "SELECT status,result_evidence,claimed_at,finished_at,recovery_from_attempt,recovery_prior_status FROM outgoing_attempt WHERE request_id=?1 AND attempt_number=1",
        [rid], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)))?;
    require(
        row.0 == "indeterminate"
            && Path::new(&row.1) == original
            && row.3.is_some()
            && row.3 >= Some(row.2)
            && row.4.is_none()
            && row.5.is_none(),
        "original attempt must remain finished indeterminate",
    )?;
    Ok(json!(row))
}
struct Artifacts {
    values: Vec<(PathBuf, Vec<u8>)>,
}
impl Artifacts {
    fn new() -> Self {
        Self { values: Vec::new() }
    }
    fn bytes(&mut self, path: PathBuf, limit: u64, empty: bool) -> Result<Vec<u8>> {
        let data = read(&path, limit, empty)?;
        self.values.push((path, data.clone()));
        Ok(data)
    }
    fn json(&mut self, path: PathBuf) -> Result<Value> {
        Ok(serde_json::from_slice(&self.bytes(path, 65536, false)?)?)
    }
    fn recheck(&self) -> Result<()> {
        for (p, b) in &self.values {
            require(
                read(p, b.len() as u64, true)? == *b,
                "evidence changed during proof",
            )?;
        }
        Ok(())
    }
}
fn hash_string(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
fn media_proof(proof: &Value, media: &Value) -> Result<()> {
    for key in ["sha256", "mime_type", "size_bytes"] {
        require(
            !media[key].is_null() && proof[key] == media[key],
            "native media differs from frozen image",
        )?;
    }
    require(
        proof["exact_pixels"] == true
            && hash_string(&proof["pixel_sha256"])
            && proof["composer_pixel_sha256"] == proof["pixel_sha256"],
        "native composer pixel proof missing",
    )?;
    let width = proof["width"].as_u64().unwrap_or(0);
    let height = proof["height"].as_u64().unwrap_or(0);
    require(
        width > 0 && height > 0 && width <= 8192 && height <= 8192 && width * height <= 40_000_000,
        "native pixel dimensions invalid",
    )
}
fn original_proof(
    root: &Path,
    tid: &str,
    rid: &str,
    binding: &BindingView,
    initial: &Intent,
    content_kind: ContentKind,
    artifacts: &mut Artifacts,
) -> Result<Value> {
    let report = artifacts.json(root.join(format!("{tid}.client-result.json")))?;
    require(
        report["id"] == tid
            && report["request_id"] == rid
            && report["attempt_number"] == 1
            && report["channel_id"] == binding.channel_id
            && report["session_id"] == binding.session_id
            && report["status"] == "indeterminate"
            && report["stage"] == "submit_clicked",
        "original report is not an exact indeterminate submitted transaction",
    )?;
    let prep = artifacts.json(root.join(format!("{tid}.prepare-visual.result.json")))?;
    let submit = artifacts.json(root.join(format!("{tid}.submit-visual.result.json")))?;
    let kind = if binding.talker.ends_with("@chatroom") {
        "group"
    } else {
        "private"
    };
    require(
        prep["id"] == tid
            && prep["action"] == "prepare-visual"
            && prep["ok"] == true
            && prep["prepared"] == true
            && submit["id"] == tid
            && submit["action"] == "submit-visual"
            && submit["ok"] == true
            && submit["submitted"] == true
            && report["target_kind"] == kind
            && ["accessibility", "postEvents", "screenRecording"]
                .iter()
                .all(|key| prep["permissions"][key] == true && submit["permissions"][key] == true),
        "positive native prepare and submit are required",
    )?;
    let p = &prep["media_prepared"];
    let s = &submit["media_submitted"];
    let media = &initial.media;
    if content_kind == ContentKind::Image {
        media_proof(p, media)?;
        media_proof(s, media)?;
        require(
            report["media_prepared"] == *p
                && report["media_submitted"] == *s
                && p["pixel_sha256"] == s["pixel_sha256"]
                && p["width"] == s["width"]
                && p["height"] == s["height"],
            "native and original pixel evidence conflict",
        )?;
        let capture = root.join(format!("{tid}.submit-after-click.png"));
        require(
            s["post_click_capture"].as_str() == capture.to_str()
                && s["post_click_header_verified"] == true
                && hash_string(&s["post_click_capture_sha256"]),
            "post-click header/capture proof absent",
        )?;
        let bytes = artifacts.bytes(capture, 16 * 1024 * 1024, false)?;
        require(
            bytes.starts_with(b"\x89PNG\r\n\x1a\n")
                && s["post_click_capture_sha256"] == digest(&bytes),
            "post-click capture tampered",
        )?;
    } else {
        require(
            p.is_null()
                && s.is_null()
                && report["media_prepared"].is_null()
                && report["media_submitted"].is_null(),
            "TEXT contains conflicting native media",
        )?;
    }
    let prep_job = artifacts.json(root.join(format!("{tid}.prepare-visual.job.json")))?;
    let submit_job = artifacts.json(root.join(format!("{tid}.submit-visual.job.json")))?;
    let state = artifacts.json(root.join(format!("{tid}.visual-state.json")))?;
    require(
        prep_job["id"] == tid
            && prep_job["action"] == "prepare-visual"
            && submit_job["id"] == tid
            && submit_job["action"] == "submit-visual"
            && state["id"] == tid
            && state["stage"] == "submitted-unverified",
        "native jobs/state do not prove the one submitted transaction",
    )?;
    if content_kind == ContentKind::Image {
        require(
            prep_job["media"] == *media
                && submit_job["media"] == *media
                && state["media"] == *media
                && state["preparedPixelSHA256"] == p["pixel_sha256"]
                && state["preparedCopyType"] == p["composer_copy_type"]
                && s["prepared_copy_type"] == p["composer_copy_type"]
                && p["composer_copy_type"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty())
                && s["composer_copy_type"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty()),
            "native jobs/state do not prove the one submitted transaction",
        )?;
    } else {
        require(
            prep_job["media"].is_null()
                && submit_job["media"].is_null()
                && state["media"].is_null()
                && state["text"] == initial.text
                && prep_job["text"] == initial.text
                && (prep_job["body"].is_null() || prep_job["body"] == initial.text),
            "native TEXT differs from frozen intent",
        )?;
    }
    let mut a = prep_job.clone();
    let mut b = submit_job.clone();
    a.as_object_mut()
        .ok_or_else(|| invalid("invalid prepare job"))?
        .remove("action");
    b.as_object_mut()
        .ok_or_else(|| invalid("invalid submit job"))?
        .remove("action");
    require(a == b, "prepare/submit jobs conflict")?;
    for key in [
        "targetGroup",
        "targetKind",
        "focusEvidenceID",
        "expectedPID",
        "expectedWindowID",
        "expectedFrame",
        "composerRect",
        "buttonPoint",
        "expectedDraft",
    ] {
        require(
            !prep_job[key].is_null() && state[key] == prep_job[key],
            "native state/job inputs conflict",
        )?;
    }
    for key in [
        "expectedDraftMedia",
        "emptyComposerReference",
        "emptyComposerAlternates",
    ] {
        require(
            state[key] == prep_job[key],
            "optional native state/job inputs conflict",
        )?;
    }
    if content_kind == ContentKind::Image {
        require(
            state["text"] == ""
                && prep_job["text"].as_str().unwrap_or("").is_empty()
                && prep_job["body"].as_str().unwrap_or("").is_empty(),
            "IMAGE transaction contains conflicting text",
        )?;
    }
    require(
        prep_job["focusEvidenceID"] == report["focus_evidence_id"]
            && prep_job["targetKind"] == report["target_kind"],
        "original focus binding conflicts",
    )?;
    // Native marker is an empty O_EXCL-created file, not a JSON assertion.
    require(
        artifacts
            .bytes(root.join(format!("{tid}.submit-attempted.json")), 0, true)?
            .is_empty(),
        "invalid submit marker",
    )?;
    for entry in fs::read_dir(root)? {
        let name = entry?.file_name().to_string_lossy().into_owned();
        require(
            !(name.starts_with(&format!("{tid}-a"))
                && (name.contains("submit")
                    || name.contains("prepare")
                    || name.ends_with(".client-result.json")
                    || name.ends_with(".visual-state.json"))),
            "later transaction artifacts conflict with original submit",
        )?;
    }
    Ok(report)
}
fn event_proof(
    state: &Path,
    binding: &BindingView,
    original: &Value,
    cursor: i64,
    content_kind: ContentKind,
    text: &str,
) -> Result<Value> {
    if content_kind == ContentKind::Text {
        return text_event_proof(state, binding, original, cursor, text);
    }
    let baseline = original["observation"]["start_cursor"]
        .as_i64()
        .ok_or_else(|| invalid("original baseline absent"))?;
    require(
        baseline >= 0 && cursor > baseline,
        "cursor is not after original baseline",
    )?;
    let started = chrono::DateTime::parse_from_rfc3339(
        original["started_at"]
            .as_str()
            .ok_or_else(|| invalid("original start absent"))?,
    )?
    .timestamp_millis();
    let elapsed = original["elapsed_seconds"]
        .as_f64()
        .ok_or_else(|| invalid("original duration absent"))?;
    require(
        elapsed.is_finite() && (0.0..=300.0).contains(&elapsed),
        "original observation window invalid",
    )?;
    let lower = started.saturating_sub(2000);
    let upper = started
        .saturating_add((elapsed * 1000.0).ceil() as i64)
        .saturating_add(2000);
    let path = state.join("channel.sqlite3");
    private(&path, false)?;
    let source = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    // The selected cursor is not an ambiguity boundary. Inspect the complete
    // bounded original time window, including candidates after that cursor.
    let mut stmt = source
        .prepare("SELECT cursor FROM channel_events WHERE cursor>?1 ORDER BY cursor LIMIT 10001")?;
    let cursors = stmt
        .query_map([baseline], |r| r.get::<_, i64>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    require(
        cursors.len() <= 10000,
        "event interval exceeds bounded observation",
    )?;
    let mut matches = Vec::new();
    for c in cursors {
        let event = channel::event_at_cursor(state, c)?;
        if event["kind"] == "message_observed"
            && event["direction"] == "outgoing"
            && event["message"]["msg_type"] == 3
        {
            let at = event["message"]["create_time"]
                .as_i64()
                .and_then(|value| value.checked_mul(1000))
                .ok_or_else(|| invalid("candidate image timestamp absent"))?;
            if at >= lower && at <= upper {
                matches.push(event);
            }
        }
    }
    require(
        matches.len() == 1 && matches[0]["cursor"] == cursor,
        "outgoing IMAGE event is absent or ambiguous",
    )?;
    let event = matches.remove(0);
    require(
        binding.self_wxid.as_ref().is_some_and(|s| !s.is_empty())
            && event["channel_id"] == binding.channel_id
            && event["session_id"] == binding.session_id
            && event["account"] == binding.account
            && event["talker"] == binding.talker
            && event["message"]["talker"] == binding.talker
            && event["message"]["sender"].as_str() == binding.self_wxid.as_deref(),
        "source binding conflicts",
    )?;
    let time = event["message"]["create_time"]
        .as_i64()
        .and_then(|t| t.checked_mul(1000))
        .ok_or_else(|| invalid("event timestamp absent"))?;
    require(
        time >= lower && time <= upper,
        "event outside original attempt window",
    )?;
    require(
        event["server_id"].as_i64().is_some_and(|n| n > 0),
        "source event server identity absent",
    )?;
    Ok(event)
}

/// A parser correction may append several revisions of the same source ID.
/// Require the latest revision and one distinct source message, first observed
/// after this attempt's baseline. Never adopt an old or merely similar message.
fn text_event_proof(
    state: &Path,
    binding: &BindingView,
    original: &Value,
    cursor: i64,
    text: &str,
) -> Result<Value> {
    let baseline = original["observation"]["start_cursor"]
        .as_i64()
        .ok_or_else(|| invalid("original baseline absent"))?;
    require(
        baseline >= 0 && cursor > baseline,
        "cursor is not after original baseline",
    )?;
    let started = chrono::DateTime::parse_from_rfc3339(
        original["started_at"]
            .as_str()
            .ok_or_else(|| invalid("original start absent"))?,
    )?
    .timestamp_millis();
    let elapsed = original["elapsed_seconds"]
        .as_f64()
        .ok_or_else(|| invalid("original duration absent"))?;
    require(
        elapsed.is_finite() && (0.0..=300.0).contains(&elapsed),
        "original observation window invalid",
    )?;
    let lower = started.saturating_sub(2000);
    let upper = started
        .saturating_add((elapsed * 1000.0).ceil() as i64)
        .saturating_add(2000);
    let path = state.join("channel.sqlite3");
    private(&path, false)?;
    let source = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let mut stmt = source
        .prepare("SELECT cursor FROM channel_events WHERE cursor>?1 ORDER BY cursor LIMIT 10001")?;
    let cursors = stmt
        .query_map([baseline], |r| r.get::<_, i64>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    require(
        cursors.len() <= 10000,
        "event interval exceeds bounded observation",
    )?;
    let mut matches = BTreeMap::new();
    for c in cursors {
        let event = channel::event_at_cursor(state, c)?;
        if matches!(
            event["kind"].as_str(),
            Some("message_observed" | "message_updated")
        ) && event["direction"] == "outgoing"
            && event["message"]["msg_type"] == 1
            && event["message"]["content"] == json!({"Text": text})
        {
            let at = event["message"]["create_time"]
                .as_i64()
                .and_then(|v| v.checked_mul(1000))
                .ok_or_else(|| invalid("candidate text timestamp absent"))?;
            if at >= lower && at <= upper {
                let sid = event["server_id"]
                    .as_i64()
                    .filter(|id| *id > 0)
                    .ok_or_else(|| invalid("source event server identity absent"))?;
                require(
                    event["message"]["server_id"] == sid,
                    "source identities conflict",
                )?;
                matches.insert(sid, event);
            }
        }
    }
    require(
        matches.len() == 1,
        "outgoing TEXT source ID is absent or ambiguous",
    )?;
    let (sid, event) = matches
        .into_iter()
        .next()
        .ok_or_else(|| invalid("TEXT event absent"))?;
    let (first, latest): (i64, i64) = source.query_row(
        "SELECT MIN(cursor),MAX(cursor) FROM channel_events WHERE server_id=?1",
        [sid],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    require(
        first > baseline && latest == cursor && event["cursor"] == cursor,
        "TEXT is preexisting or selected revision is stale",
    )?;
    let first_event = channel::event_at_cursor(state, first)?;
    require(
        first_event["kind"] == "message_observed"
            && first_event["message"]["server_id"] == sid
            && first_event["message"]["talker"] == binding.talker
            && first_event["message"]["create_time"] == event["message"]["create_time"]
            && first_event["message"]["sort_seq"] == event["message"]["sort_seq"],
        "TEXT correction lacks the same original source identity",
    )?;
    require(
        binding.self_wxid.as_ref().is_some_and(|s| !s.is_empty())
            && event["channel_id"] == binding.channel_id
            && event["session_id"] == binding.session_id
            && event["account"] == binding.account
            && event["talker"] == binding.talker
            && event["message"]["talker"] == binding.talker
            && event["message"]["sender"].as_str() == binding.self_wxid.as_deref(),
        "source TEXT binding conflicts",
    )?;
    Ok(event)
}

#[allow(clippy::too_many_arguments)]
pub(super) fn reconcile(
    state: &Path,
    binding: &BindingView,
    conn: &mut Connection,
    rid: &str,
    expected_attempt: i64,
    cursor: i64,
    expected_epoch: i64,
    reason: &str,
) -> Result<Value> {
    reconcile_content(
        state,
        binding,
        conn,
        rid,
        expected_attempt,
        cursor,
        expected_epoch,
        reason,
        ContentKind::Image,
    )
}

#[allow(clippy::too_many_arguments)]
pub(super) fn reconcile_text(
    state: &Path,
    binding: &BindingView,
    conn: &mut Connection,
    rid: &str,
    expected_attempt: i64,
    cursor: i64,
    expected_epoch: i64,
    reason: &str,
) -> Result<Value> {
    reconcile_content(
        state,
        binding,
        conn,
        rid,
        expected_attempt,
        cursor,
        expected_epoch,
        reason,
        ContentKind::Text,
    )
}

#[allow(clippy::too_many_arguments)]
fn reconcile_content(
    state: &Path,
    binding: &BindingView,
    conn: &mut Connection,
    rid: &str,
    expected_attempt: i64,
    cursor: i64,
    expected_epoch: i64,
    reason: &str,
    content_kind: ContentKind,
) -> Result<Value> {
    require(
        expected_attempt == 1
            && cursor > 0
            && expected_epoch > 0
            && reason.trim().len() >= 8
            && reason.len() <= 512,
        "reconciliation requires exact attempt, cursor, current epoch and review reason",
    )?;
    private(state, true)?;
    let marker = read(&state.join("managed-coordinator.json"), 65536, false)?;
    // The general managed-root reader is not size-bounded; parse the already
    // bounded private marker here rather than rereading it without a limit.
    let managed: Value = serde_json::from_slice(&marker)?;
    require(
        managed["version"] == 1,
        "invalid managed root marker version",
    )?;
    let root = PathBuf::from(
        managed["root"]
            .as_str()
            .ok_or_else(|| invalid("managed root marker required"))?,
    );
    private(&root, true)?;
    let _executor = lock(&root, "executor.lock")?;
    let _sender = lock(&root, "sender.lock")?;
    let actual = channel::load_binding(state)?;
    require(
        serde_json::to_value(&actual)? == serde_json::to_value(binding)?,
        "channel binding changed",
    )?;
    let identity = format!("{}\0{}", binding.channel_id, rid);
    let tid = format!("send-{}", &digest(identity.as_bytes())[..48]);
    let original_path = root.join(format!("{tid}.client-result.json"));
    let verified_path = root.join(format!("{tid}.late-observed.json"));
    let initial = intent(conn, binding, rid, content_kind)?;
    require(
        initial.status == "indeterminate" || initial.status == "ui_observed",
        "only original indeterminate/current observed intent permitted",
    )?;
    let already = initial.status == "ui_observed";
    require(
        Path::new(&initial.evidence)
            == if already {
                &verified_path
            } else {
                &original_path
            },
        "intent evidence path conflicts",
    )?;
    let original_attempt = attempt(conn, rid, &original_path)?;
    coordinator::with_observation_authority(
        &root,
        state,
        rid,
        &initial.owner,
        initial.epoch,
        expected_epoch,
        already,
        || {
            // Reserve the outbox before slow proof too: no concurrent attempt/result
            // writer can change authority between validation and commit.
            let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
            require(
                intent(&tx, binding, rid, content_kind)? == initial,
                "intent changed before proof",
            )?;
            require(
                read(&state.join("managed-coordinator.json"), 65536, false)? == marker,
                "managed root changed",
            )?;
            let mut artifacts = Artifacts::new();
            let original = original_proof(
                &root,
                &tid,
                rid,
                binding,
                &initial,
                content_kind,
                &mut artifacts,
            )?;
            let original_sha = digest(&artifacts.values[0].1);
            let event = event_proof(
                state,
                binding,
                &original,
                cursor,
                content_kind,
                &initial.text,
            )?;
            let proof = if content_kind == ContentKind::Image {
                let proof = outgoing_verify::verify(state, binding, &tx, rid, cursor)?;
                require(
                    proof["verified"] == true
                        && proof["request_id"] == rid
                        && proof["cursor"] == cursor
                        && proof["media_sha256"] == initial.media["sha256"],
                    "full source content proof conflicts",
                )?;
                let frozen_path = outgoing_media::frozen_path(state, rid)?;
                private(&frozen_path, false)?;
                let (_, mut frozen) = outgoing_media::inspect(&frozen_path)?;
                frozen.path = frozen_path;
                require(
                    frozen.descriptor() == initial.media,
                    "frozen media changed after source proof",
                )?;
                proof
            } else {
                json!({"verified":true,"request_id":rid,"cursor":cursor,
                    "text_sha256":digest(initial.text.as_bytes()),"exact_full_text":true})
            };
            artifacts.recheck()?;
            require(
                read(&state.join("managed-coordinator.json"), 65536, false)? == marker,
                "managed root changed after proof",
            )?;
            require(
                event_proof(
                    state,
                    binding,
                    &original,
                    cursor,
                    content_kind,
                    &initial.text,
                )? == event
                    && attempt(&tx, rid, &original_path)? == original_attempt
                    && intent(&tx, binding, rid, content_kind)? == initial,
                "authority/source changed after slow proof",
            )?;
            require(
                serde_json::to_value(channel::load_binding(state)?)?
                    == serde_json::to_value(binding)?,
                "binding changed after proof",
            )?;
            let native_artifacts: Vec<Value> = artifacts
                .values
                .iter()
                .skip(1)
                .map(|(path, bytes)| json!({"path":path,"sha256":digest(bytes)}))
                .collect();
            let mut report = json!({"id":tid,"request_id":rid,"attempt_number":1,"channel_id":binding.channel_id,
            "session_id":binding.session_id,"status":"ui_observed","stage":format!("late_source_{}_observed",content_kind.as_str()),
            "event_cursor":cursor,"original_report":{"path":original_path,"sha256":original_sha,"status":"indeterminate"},
            "native_proof":{"media_prepared":original["media_prepared"],"media_submitted":original["media_submitted"]},
            "native_artifacts":native_artifacts,"image_content_proof":proof,"source_event":event,"reason":reason});
            if content_kind == ContentKind::Text {
                report
                    .as_object_mut()
                    .unwrap()
                    .remove("image_content_proof");
                report["text_content_proof"] = proof;
                report["native_proof"] =
                    json!({"prepared":true,"submitted":true,"exact_frozen_text":true});
            }
            let bytes = serde_json::to_vec_pretty(&report)?;
            require(bytes.len() <= 65536, "late observation report too large")?;
            let verified_sha = digest(&bytes);
            let existing: Option<(i64,String,String)> = tx.query_row("SELECT cursor,original_sha256,verified_sha256 FROM outgoing_observation WHERE request_id=?1",[rid],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
            if let Some((old_cursor, old_original, old_verified)) = existing {
                require(
                    already
                        && old_cursor == cursor
                        && old_original == original_sha
                        && old_verified == verified_sha
                        && read(&verified_path, 65536, false)? == bytes,
                    "observation replay conflicts",
                )?;
                let exact: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM outgoing_observation WHERE request_id=?1 AND attempt_number=1 AND channel_id=?2 AND session_id=?3 AND event_server_id=?4 AND original_status='indeterminate' AND original_path=?5 AND verified_path=?6)",
                    params![rid,binding.channel_id,binding.session_id,event["server_id"].as_i64(),original_path.to_str(),verified_path.to_str()], |r|r.get(0))?;
                require(exact, "stored observation identity conflicts")?;
            } else {
                require(!already, "observed intent lacks immutable observation")?;
                let used: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM outgoing_observation WHERE channel_id=?1 AND session_id=?2 AND (cursor=?3 OR event_server_id=?4))",
                params![binding.channel_id,binding.session_id,cursor,event["server_id"].as_i64()],|r|r.get(0))?;
                require(!used, "source event already belongs to another request")?;
                // Publish with no replacement. An orphan from a crash is reusable only
                // byte-for-byte; no report or original evidence is overwritten.
                let mut temp = tempfile::NamedTempFile::new_in(&root)?;
                temp.as_file()
                    .set_permissions(fs::Permissions::from_mode(0o600))?;
                temp.write_all(&bytes)?;
                temp.as_file().sync_all()?;
                match temp.persist_noclobber(&verified_path) {
                    Ok(_) => File::open(&root)?.sync_all()?,
                    Err(e) if e.error.kind() == io::ErrorKind::AlreadyExists => {
                        require(
                            read(&verified_path, 65536, false)? == bytes,
                            "orphan observation conflicts",
                        )?;
                    }
                    Err(e) => return Err(e.error.into()),
                }
                tx.execute("INSERT INTO outgoing_observation(request_id,attempt_number,channel_id,session_id,cursor,event_server_id,original_status,original_path,original_sha256,verified_path,verified_sha256,recorded_at)
              VALUES(?1,1,?2,?3,?4,?5,'indeterminate',?6,?7,?8,?9,strftime('%s','now'))",
              params![rid,binding.channel_id,binding.session_id,cursor,event["server_id"].as_i64(),original_path.to_str(),original_sha,verified_path.to_str(),verified_sha])?;
                let changed = tx.execute("UPDATE outgoing_intent SET status='ui_observed',result_evidence=?2,updated_at=strftime('%s','now') WHERE request_id=?1 AND status='indeterminate' AND attempt_number=1 AND result_evidence=?3",
                params![rid,verified_path.to_str(),original_path.to_str()])?;
                require(changed == 1, "intent CAS failed")?;
            }
            tx.commit()?;
            Ok(report)
        },
    )?;
    // The complete native/source report stays private. Ordinary command output
    // must not dump message bodies, CDN metadata or filesystem routes.
    Ok(
        json!({"request_id":rid,"status":"ui_observed","attempt_number":1,
        "original_attempt_status":"indeterminate","cursor":cursor,
        "reconciled":true,"new_send_attempts":0}),
    )
}
