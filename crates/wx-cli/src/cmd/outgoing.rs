//! Durable outgoing intent journal. UI delivery belongs to a caller-supplied driver.

use std::error::Error;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clap::Subcommand;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::json;

use crate::cmd::channel::{load_binding, BindingView};

const DB_NAME: &str = "outgoing.sqlite3";
const MAX_TEXT_BYTES: usize = 64 * 1024;
const MAX_DRIVER_OUTPUT_BYTES: u64 = 16 * 1024;
const MAX_EVIDENCE_BYTES: usize = 4096;
const DRIVER_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Subcommand, Debug)]
pub enum OutgoingAction {
    /// Save an outgoing text intent without sending it.
    Queue {
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        channel_id: String,
        #[arg(long)]
        session_id: String,
        /// Read the exact message body from standard input.
        #[arg(long, required = true)]
        text_stdin: bool,
    },
    /// List intent IDs and states (message bodies are withheld).
    List,
    /// Show one intent, including its message body.
    Show {
        #[arg(long)]
        request_id: String,
    },
    /// Claim one queued intent and pass it to a local executable exactly once.
    Send {
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        driver: std::path::PathBuf,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Attempt {
    pub request_id: String,
    pub attempt_number: i64,
    pub text: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AttemptResult {
    UiObserved,
    Failed,
    Indeterminate,
}

impl AttemptResult {
    fn as_str(self) -> &'static str {
        match self {
            Self::UiObserved => "ui_observed",
            Self::Failed => "failed",
            Self::Indeterminate => "indeterminate",
        }
    }
}

pub fn run(state_dir: &Path, action: OutgoingAction) -> Result<(), Box<dyn Error>> {
    let binding = load_binding(state_dir)?;
    let mut conn = open_journal(state_dir)?;
    match action {
        OutgoingAction::Queue {
            request_id,
            channel_id,
            session_id,
            text_stdin,
        } => {
            if !text_stdin {
                return Err(invalid("queue requires --text-stdin").into());
            }
            check_binding(&binding, &channel_id, &session_id)?;
            check_request_id(&request_id)?;
            let mut text = String::new();
            io::stdin()
                .take((MAX_TEXT_BYTES + 1) as u64)
                .read_to_string(&mut text)?;
            let status = queue_intent(&mut conn, &binding, &request_id, &text)?;
            println!("{}", json!({"request_id": request_id, "status": status}));
        }
        OutgoingAction::List => {
            let items = list_intents(&conn, &binding)?;
            println!("{}", serde_json::to_string(&items)?);
        }
        OutgoingAction::Show { request_id } => {
            let item = show_intent(&conn, &binding, &request_id)?;
            println!("{}", item);
        }
        OutgoingAction::Send { request_id, driver } => {
            drop(conn);
            let result =
                send_with_timeout(state_dir, &binding, &request_id, &driver, DRIVER_TIMEOUT)?;
            println!(
                "{}",
                json!({"request_id": request_id, "status": result.as_str()})
            );
        }
    }
    Ok(())
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn now() -> Result<i64, Box<dyn Error>> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs() as i64)
}

fn check_binding(
    binding: &BindingView,
    channel_id: &str,
    session_id: &str,
) -> Result<(), Box<dyn Error>> {
    if binding.channel_id != channel_id || binding.session_id != session_id {
        return Err(invalid("channel/session does not match the saved binding").into());
    }
    Ok(())
}

fn check_request_id(request_id: &str) -> Result<(), Box<dyn Error>> {
    let bytes = request_id.as_bytes();
    if bytes.is_empty()
        || bytes.len() > 128
        || !bytes[0].is_ascii_alphanumeric()
        || !bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
    {
        return Err(invalid(
            "request ID must be 1-128 safe ASCII characters, starting with a letter or digit",
        )
        .into());
    }
    Ok(())
}

fn check_text(text: &str) -> Result<(), Box<dyn Error>> {
    if text.trim().is_empty() || text.len() > MAX_TEXT_BYTES || text.contains('\0') {
        return Err(
            invalid("message text must be nonempty UTF-8 without NUL and at most 64 KiB").into(),
        );
    }
    Ok(())
}

fn validate_driver(driver: &Path) -> Result<(), Box<dyn Error>> {
    if !driver.is_absolute() {
        return Err(invalid("driver path must be absolute").into());
    }
    let meta = fs::metadata(driver)?;
    if !meta.is_file() || meta.permissions().mode() & 0o111 == 0 {
        return Err(invalid("driver must be an executable regular file").into());
    }
    Ok(())
}

/// The claim is committed before the process can start. Every post-claim failure is
/// recorded as indeterminate, because the driver may have touched the UI.
fn send_with_timeout(
    state_dir: &Path,
    binding: &BindingView,
    request_id: &str,
    driver: &Path,
    timeout: Duration,
) -> Result<AttemptResult, Box<dyn Error>> {
    // Do not consume the intent if the executable is obviously unusable.
    validate_driver(driver)?;
    let attempt = claim_attempt(state_dir, request_id)?;
    let input = json!({
        "protocol_version": 1,
        "request_id": attempt.request_id,
        "channel_id": binding.channel_id,
        "session_id": binding.session_id,
        "account": binding.account,
        "talker": binding.talker,
        "text": attempt.text,
    });
    let (result, evidence) = invoke_driver(driver, &serde_json::to_vec(&input)?, timeout);
    record_result(
        state_dir,
        request_id,
        attempt.attempt_number,
        result,
        &evidence,
    )?;
    Ok(result)
}

fn invoke_driver(driver: &Path, input: &[u8], timeout: Duration) -> (AttemptResult, String) {
    let mut child = match Command::new(driver)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
    {
        Ok(child) => child,
        Err(_) => return indeterminate("driver could not start"),
    };
    let mut stdin = child.stdin.take().expect("piped stdin");
    let payload = input.to_vec();
    let writer = std::thread::spawn(move || stdin.write_all(&payload));
    let stdout = child.stdout.take().expect("piped stdout");
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take(MAX_DRIVER_OUTPUT_BYTES + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let stderr = child.stderr.take().expect("piped stderr");
    let stderr_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .take(MAX_DRIVER_OUTPUT_BYTES + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes.len())
    });

    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            Ok(None) | Err(_) => {
                // Kill descendants that inherited a pipe as well as the driver.
                unsafe { libc::killpg(child.id() as i32, libc::SIGKILL) };
                let _ = child.kill();
                let _ = child.wait();
                break None;
            }
        }
    };
    let wrote = writer.join().is_ok_and(|result| result.is_ok());
    let output = reader.join().ok().and_then(Result::ok);
    let _ = stderr_reader.join(); // Never expose driver stderr; it may contain message data.
    if status.is_none() {
        return indeterminate("driver timed out or could not be observed");
    }
    if !wrote || !status.expect("checked above").success() {
        return indeterminate("driver input or process failed");
    }
    let Some(bytes) = output else {
        return indeterminate("driver output could not be read");
    };
    if bytes.len() as u64 > MAX_DRIVER_OUTPUT_BYTES {
        return indeterminate("driver output exceeded limit");
    }
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return indeterminate("driver returned invalid JSON");
    };
    let (Some(status), Some(evidence)) = (
        value.get("status").and_then(|v| v.as_str()),
        value.get("evidence").and_then(|v| v.as_str()),
    ) else {
        return indeterminate("driver result lacked status or evidence");
    };
    if evidence.trim().is_empty() || evidence.len() > MAX_EVIDENCE_BYTES {
        return indeterminate("driver evidence was empty or too large");
    }
    let result = match status {
        "ui_observed" => AttemptResult::UiObserved,
        "failed" => AttemptResult::Failed,
        "indeterminate" => AttemptResult::Indeterminate,
        _ => return indeterminate("driver returned unknown status"),
    };
    (result, evidence.to_owned())
}

fn indeterminate(reason: &str) -> (AttemptResult, String) {
    (AttemptResult::Indeterminate, reason.to_owned())
}

fn secure_file(path: &Path) -> Result<(), Box<dyn Error>> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.nlink() != 1
        || meta.permissions().mode() & 0o077 != 0
    {
        return Err(
            invalid("outgoing journal must be a private regular file owned by this user").into(),
        );
    }
    Ok(())
}

fn open_journal(state_dir: &Path) -> Result<Connection, Box<dyn Error>> {
    let meta = fs::symlink_metadata(state_dir)?;
    if !meta.file_type().is_dir()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0
    {
        return Err(invalid(
            "state directory must be private, owned by this user, and not a symlink",
        )
        .into());
    }
    let path = state_dir.join(DB_NAME);
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
    {
        Ok(_) => {}
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e.into()),
    }
    secure_file(&path)?;
    let conn = Connection::open(&path)?;
    // DELETE mode keeps all state in the private directory without WAL sidecars.
    conn.pragma_update(None, "journal_mode", "DELETE")?;
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS outgoing_intent (
            request_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','attempt_recorded','ui_observed','failed','indeterminate','recipient_confirmed')),
            attempt_number INTEGER NOT NULL DEFAULT 0,
            result_evidence TEXT,
            receipt_evidence TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );"
    )?;
    Ok(conn)
}

fn queue_intent(
    conn: &mut Connection,
    binding: &BindingView,
    request_id: &str,
    text: &str,
) -> Result<String, Box<dyn Error>> {
    check_request_id(request_id)?;
    check_text(text)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let existing: Option<(String, String, String, String)> = tx
        .query_row(
            "SELECT channel_id, session_id, text, status FROM outgoing_intent WHERE request_id=?1",
            [request_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let status = if let Some((channel, session, old_text, status)) = existing {
        if channel != binding.channel_id || session != binding.session_id || old_text != text {
            return Err(
                invalid("request ID already exists with different content or binding").into(),
            );
        }
        status
    } else {
        let timestamp = now()?;
        tx.execute(
            "INSERT INTO outgoing_intent (request_id,channel_id,session_id,text,status,created_at,updated_at) VALUES (?1,?2,?3,?4,'queued',?5,?5)",
            params![request_id, binding.channel_id, binding.session_id, text, timestamp],
        )?;
        "queued".to_owned()
    };
    tx.commit()?;
    Ok(status)
}

fn list_intents(
    conn: &Connection,
    binding: &BindingView,
) -> Result<Vec<serde_json::Value>, Box<dyn Error>> {
    let mut stmt = conn.prepare("SELECT request_id,status,attempt_number,created_at,updated_at FROM outgoing_intent WHERE channel_id=?1 AND session_id=?2 ORDER BY created_at,request_id")?;
    let rows = stmt.query_map(params![binding.channel_id, binding.session_id], |r| {
        Ok(json!({"request_id": r.get::<_, String>(0)?, "status": r.get::<_, String>(1)?, "attempt_number": r.get::<_, i64>(2)?, "created_at": r.get::<_, i64>(3)?, "updated_at": r.get::<_, i64>(4)?}))
    })?;
    Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
}

fn show_intent(
    conn: &Connection,
    binding: &BindingView,
    request_id: &str,
) -> Result<serde_json::Value, Box<dyn Error>> {
    check_request_id(request_id)?;
    let item = conn.query_row(
        "SELECT text,status,attempt_number,result_evidence,receipt_evidence,created_at,updated_at FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3",
        params![request_id, binding.channel_id, binding.session_id],
        |r| Ok(json!({"request_id":request_id,"text":r.get::<_, String>(0)?,"status":r.get::<_, String>(1)?,"attempt_number":r.get::<_, i64>(2)?,"result_evidence":r.get::<_, Option<String>>(3)?,"receipt_evidence":r.get::<_, Option<String>>(4)?,"created_at":r.get::<_, i64>(5)?,"updated_at":r.get::<_, i64>(6)?})),
    ).optional()?;
    item.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "outgoing request not found").into())
}

/// Persist a single attempt before any sender touches the UI. Repeated calls fail closed.
#[allow(dead_code)]
pub(crate) fn claim_attempt(state_dir: &Path, request_id: &str) -> Result<Attempt, Box<dyn Error>> {
    check_request_id(request_id)?;
    let binding = load_binding(state_dir)?;
    let mut conn = open_journal(state_dir)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let text: String = tx.query_row(
        "SELECT text FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND status='queued'",
        params![request_id, binding.channel_id, binding.session_id], |r| r.get(0),
    ).optional()?.ok_or_else(|| invalid("request is absent, mismatched, or already attempted"))?;
    let attempt_number = 1;
    tx.execute("UPDATE outgoing_intent SET status='attempt_recorded',attempt_number=?2,updated_at=?3 WHERE request_id=?1",
        params![request_id, attempt_number, now()?])?;
    tx.commit()?;
    Ok(Attempt {
        request_id: request_id.to_owned(),
        attempt_number,
        text,
    })
}

/// Record observed UI outcome; none of these states asserts recipient delivery.
#[allow(dead_code)]
pub(crate) fn record_result(
    state_dir: &Path,
    request_id: &str,
    attempt_number: i64,
    result: AttemptResult,
    evidence: &str,
) -> Result<(), Box<dyn Error>> {
    check_request_id(request_id)?;
    if evidence.trim().is_empty() {
        return Err(invalid("attempt result requires evidence").into());
    }
    let binding = load_binding(state_dir)?;
    let mut conn = open_journal(state_dir)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let changed = tx.execute(
        "UPDATE outgoing_intent SET status=?1,result_evidence=?2,updated_at=?3 WHERE request_id=?4 AND channel_id=?5 AND session_id=?6 AND attempt_number=?7 AND status='attempt_recorded'",
        params![result.as_str(), evidence, now()?, request_id, binding.channel_id, binding.session_id, attempt_number],
    )?;
    if changed != 1 {
        return Err(invalid("invalid or stale outgoing attempt transition").into());
    }
    tx.commit()?;
    Ok(())
}

/// Independent recipient evidence is required after an observed UI send.
#[allow(dead_code)]
pub(crate) fn confirm_recipient(
    state_dir: &Path,
    request_id: &str,
    evidence: &str,
) -> Result<(), Box<dyn Error>> {
    check_request_id(request_id)?;
    if evidence.trim().is_empty() {
        return Err(invalid("recipient confirmation requires independent evidence").into());
    }
    let binding = load_binding(state_dir)?;
    let mut conn = open_journal(state_dir)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let changed = tx.execute(
        "UPDATE outgoing_intent SET status='recipient_confirmed',receipt_evidence=?1,updated_at=?2 WHERE request_id=?3 AND channel_id=?4 AND session_id=?5 AND status='ui_observed'",
        params![evidence, now()?, request_id, binding.channel_id, binding.session_id],
    )?;
    if changed != 1 {
        return Err(invalid("recipient confirmation requires an observed UI send").into());
    }
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cmd::channel::{self, ChannelAction};
    use std::path::PathBuf;
    use tempfile::TempDir;

    fn setup() -> (TempDir, PathBuf, BindingView) {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let source = temp.path().join("decrypted");
        fs::create_dir(&source).unwrap();
        channel::run(
            &state,
            ChannelAction::Bind {
                account: "test_account".into(),
                talker: "test_group@chatroom".into(),
                session: "test_session".into(),
                source,
                encrypted_snapshot: false,
            },
        )
        .unwrap();
        let binding = load_binding(&state).unwrap();
        (temp, state, binding)
    }

    fn fake_driver(temp: &TempDir, name: &str, body: &str) -> PathBuf {
        let path = temp.path().join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        path
    }

    #[test]
    fn driver_receives_bound_request_and_is_invoked_only_once() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "send_1", "test message").unwrap();
        drop(conn);
        let captured = temp.path().join("captured.json");
        let count = temp.path().join("invocations");
        let script = format!(
            "cat > '{}'\necho call >> '{}'\nprintf '%s\\n' '{{\"status\":\"ui_observed\",\"evidence\":\"local UI row observed\"}}'",
            captured.display(),
            count.display()
        );
        let driver = fake_driver(&temp, "good-driver", &script);
        validate_driver(&driver).unwrap();
        assert_eq!(
            send_with_timeout(&state, &binding, "send_1", &driver, Duration::from_secs(2)).unwrap(),
            AttemptResult::UiObserved
        );
        let input: serde_json::Value =
            serde_json::from_slice(&fs::read(captured).unwrap()).unwrap();
        assert_eq!(input["protocol_version"], 1);
        assert_eq!(input["request_id"], "send_1");
        assert_eq!(input["channel_id"], binding.channel_id);
        assert_eq!(input["session_id"], binding.session_id);
        assert_eq!(input["account"], binding.account);
        assert_eq!(input["talker"], binding.talker);
        assert_eq!(input["text"], "test message");
        assert!(
            send_with_timeout(&state, &binding, "send_1", &driver, Duration::from_secs(2)).is_err()
        );
        assert_eq!(fs::read_to_string(count).unwrap().lines().count(), 1);
        let conn = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&conn, &binding, "send_1").unwrap()["status"],
            "ui_observed"
        );
    }

    #[test]
    fn invalid_driver_does_not_claim_but_invalid_result_does() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "send_2", "test message").unwrap();
        drop(conn);
        assert!(send_with_timeout(
            &state,
            &binding,
            "send_2",
            Path::new("relative-driver"),
            Duration::from_secs(2)
        )
        .is_err());
        let conn = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&conn, &binding, "send_2").unwrap()["status"],
            "queued"
        );
        drop(conn);
        let driver = fake_driver(
            &temp,
            "bad-driver",
            "cat >/dev/null\nprintf '%s\\n' '{\"status\":\"unexpected\",\"evidence\":\"unknown\"}'",
        );
        assert_eq!(
            send_with_timeout(&state, &binding, "send_2", &driver, Duration::from_secs(2)).unwrap(),
            AttemptResult::Indeterminate
        );
        assert!(
            send_with_timeout(&state, &binding, "send_2", &driver, Duration::from_secs(2)).is_err()
        );
        let conn = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&conn, &binding, "send_2").unwrap()["status"],
            "indeterminate"
        );
    }

    #[test]
    fn driver_timeout_is_indeterminate_and_never_retried() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "send_3", "test message").unwrap();
        drop(conn);
        let driver = fake_driver(&temp, "slow-driver", "sleep 2");
        assert_eq!(
            send_with_timeout(
                &state,
                &binding,
                "send_3",
                &driver,
                Duration::from_millis(30)
            )
            .unwrap(),
            AttemptResult::Indeterminate
        );
        assert!(
            send_with_timeout(&state, &binding, "send_3", &driver, Duration::from_secs(2)).is_err()
        );
        let conn = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&conn, &binding, "send_3").unwrap()["status"],
            "indeterminate"
        );
    }

    #[test]
    fn duplicate_request_is_idempotent_but_payload_conflict_fails() {
        let (_temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        assert_eq!(
            queue_intent(&mut conn, &binding, "r_1", "secret body").unwrap(),
            "queued"
        );
        assert_eq!(
            queue_intent(&mut conn, &binding, "r_1", "secret body").unwrap(),
            "queued"
        );
        assert!(queue_intent(&mut conn, &binding, "r_1", "changed body").is_err());
        let mut wrong = binding.clone();
        wrong.session_id = "another_session".into();
        assert!(queue_intent(&mut conn, &wrong, "r_1", "secret body").is_err());
        assert!(check_binding(&binding, &binding.channel_id, "another_session").is_err());
        assert!(check_binding(&binding, "another_channel", &binding.session_id).is_err());
        assert_eq!(list_intents(&conn, &binding).unwrap().len(), 1);
        assert!(
            !serde_json::to_string(&list_intents(&conn, &binding).unwrap())
                .unwrap()
                .contains("secret body")
        );
        assert_eq!(
            show_intent(&conn, &binding, "r_1").unwrap()["text"],
            "secret body"
        );
    }

    #[test]
    fn attempt_is_persisted_before_return_and_cannot_be_reclaimed_after_restart() {
        let (_temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "r2", "deliver once").unwrap();
        drop(conn);
        let attempt = claim_attempt(&state, "r2").unwrap();
        assert_eq!(attempt.text, "deliver once");
        assert_eq!(attempt.attempt_number, 1);
        assert!(claim_attempt(&state, "r2").is_err());
        let conn = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&conn, &binding, "r2").unwrap()["status"],
            "attempt_recorded"
        );
        drop(conn);
        assert!(confirm_recipient(&state, "r2", "recipient says yes").is_err());
        assert!(record_result(&state, "r2", 2, AttemptResult::UiObserved, "UI changed").is_err());
        record_result(&state, "r2", 1, AttemptResult::Indeterminate, "UI timeout").unwrap();
        assert!(record_result(&state, "r2", 1, AttemptResult::UiObserved, "later claim").is_err());
        assert!(confirm_recipient(&state, "r2", "recipient says yes").is_err());

        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "r2_failed", "second message").unwrap();
        drop(conn);
        claim_attempt(&state, "r2_failed").unwrap();
        record_result(
            &state,
            "r2_failed",
            1,
            AttemptResult::Failed,
            "UI rejected send",
        )
        .unwrap();
        assert!(claim_attempt(&state, "r2_failed").is_err());
    }

    #[test]
    fn confirmation_requires_observed_send_and_private_journal() {
        let (_temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "r3", "hello").unwrap();
        drop(conn);
        claim_attempt(&state, "r3").unwrap();
        record_result(
            &state,
            "r3",
            1,
            AttemptResult::UiObserved,
            "UI transcript observed",
        )
        .unwrap();
        assert!(confirm_recipient(&state, "r3", " ").is_err());
        confirm_recipient(&state, "r3", "recipient independently acknowledged").unwrap();
        assert!(confirm_recipient(&state, "r3", "again").is_err());
        let conn = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&conn, &binding, "r3").unwrap()["status"],
            "recipient_confirmed"
        );
        assert_eq!(
            fs::symlink_metadata(state.join(DB_NAME))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert_eq!(
            fs::symlink_metadata(&state).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn symlink_journal_is_rejected() {
        let (_temp, state, _binding) = setup();
        let outside = state.parent().unwrap().join("outside.db");
        fs::write(&outside, "outside").unwrap();
        std::os::unix::fs::symlink(&outside, state.join(DB_NAME)).unwrap();
        assert!(open_journal(&state).is_err());
    }
}
