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
const MAX_CAPABILITY_OUTPUT_BYTES: u64 = 4096;
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
    /// Explicitly claim a new attempt after an operator has established the previous send did not submit.
    RecoverUnsent {
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        expected_attempt: i64,
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
        OutgoingAction::RecoverUnsent {
            request_id,
            expected_attempt,
            driver,
        } => {
            drop(conn);
            let result = recover_unsent_with_timeout(
                state_dir,
                &binding,
                &request_id,
                expected_attempt,
                &driver,
                DRIVER_TIMEOUT,
            )?;
            println!(
                "{}",
                json!({"request_id": request_id, "attempt_number": expected_attempt + 1, "status": result.as_str()})
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
    invoke_claimed_attempt(state_dir, binding, attempt, None, driver, timeout)
}

fn recover_unsent_with_timeout(
    state_dir: &Path,
    binding: &BindingView,
    request_id: &str,
    expected_attempt: i64,
    driver: &Path,
    timeout: Duration,
) -> Result<AttemptResult, Box<dyn Error>> {
    check_request_id(request_id)?;
    if !(1..i64::MAX).contains(&expected_attempt) {
        return Err(invalid("expected attempt must be positive and below i64::MAX").into());
    }
    validate_driver(driver)?;
    check_recovery_capabilities(driver, timeout)?;
    let candidate = load_recovery_candidate(state_dir, binding, request_id, expected_attempt)?;
    let payload = driver_input(binding, &candidate, Some(expected_attempt))?;
    check_recovery_preflight(driver, &payload, timeout)?;
    let attempt = claim_recovery(state_dir, request_id, expected_attempt)?;
    invoke_claimed_attempt(
        state_dir,
        binding,
        attempt,
        Some(expected_attempt),
        driver,
        timeout,
    )
}

/// This read does not reserve the request. claim_recovery repeats the exact state
/// check in an immediate transaction after the driver finishes preflight.
fn load_recovery_candidate(
    state_dir: &Path,
    binding: &BindingView,
    request_id: &str,
    expected_attempt: i64,
) -> Result<Attempt, Box<dyn Error>> {
    let conn = open_journal(state_dir)?;
    let text: String = conn.query_row(
        "SELECT text FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND attempt_number=?4 AND status IN ('failed','indeterminate')",
        params![request_id, binding.channel_id, binding.session_id, expected_attempt],
        |r| r.get(0),
    ).optional()?.ok_or_else(|| invalid("recovery requires the current failed or indeterminate attempt and its exact number"))?;
    Ok(Attempt {
        request_id: request_id.to_owned(),
        attempt_number: expected_attempt + 1,
        text,
    })
}

/// Probe before claiming: an older driver that ignores recovery fields must never
/// receive the original message as a recovery attempt. This is a driver contract;
/// Rust cannot inspect the desktop or independently prove the driver's UI checks.
fn check_recovery_capabilities(driver: &Path, timeout: Duration) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(driver, "--recovery-capabilities", None, timeout)?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 3
            && object.get("protocol_version") == Some(&json!(1))
            && object.get("recovery_protocol_version") == Some(&json!(1))
            && object.get("pre_submit_proof_required") == Some(&json!(true))
    });
    if !valid {
        return Err(invalid("driver lacks required recovery capability").into());
    }
    Ok(())
}

fn check_recovery_preflight(
    driver: &Path,
    payload: &[u8],
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(driver, "--recovery-preflight", Some(payload), timeout)?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 3
            && object.get("protocol_version") == Some(&json!(1))
            && object.get("recovery_protocol_version") == Some(&json!(1))
            && object.get("unsent_proven") == Some(&json!(true))
    });
    if !valid {
        return Err(invalid("driver did not prove the previous attempt unsent").into());
    }
    Ok(())
}

fn invoke_recovery_probe(
    driver: &Path,
    flag: &str,
    payload: Option<&[u8]>,
    timeout: Duration,
) -> Result<serde_json::Value, Box<dyn Error>> {
    let mut child = Command::new(driver)
        .arg(flag)
        .stdin(if payload.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()?;
    let writer = payload.map(|payload| {
        let mut stdin = child.stdin.take().expect("piped stdin");
        let bytes = payload.to_vec();
        std::thread::spawn(move || {
            let result = stdin.write_all(&bytes);
            drop(stdin);
            result
        })
    });
    let stdout = child.stdout.take().expect("piped stdout");
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take(MAX_CAPABILITY_OUTPUT_BYTES + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let stderr = child.stderr.take().expect("piped stderr");
    let stderr_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .take(MAX_CAPABILITY_OUTPUT_BYTES + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes.len())
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            Ok(None) | Err(_) => {
                unsafe { libc::killpg(child.id() as i32, libc::SIGKILL) };
                let _ = child.kill();
                let _ = child.wait();
                break None;
            }
        }
    };
    // A driver may exit while a descendant still owns an output pipe.
    unsafe { libc::killpg(child.id() as i32, libc::SIGKILL) };
    let wrote = writer.is_none_or(|writer| writer.join().is_ok_and(|result| result.is_ok()));
    let output = reader.join().ok().and_then(Result::ok);
    let _ = stderr_reader.join();
    let Some(status) = status else {
        return Err(invalid("recovery probe timed out").into());
    };
    if !status.success() || !wrote {
        return Err(invalid("driver rejected recovery probe or input").into());
    }
    let Some(bytes) = output else {
        return Err(invalid("recovery probe output could not be read").into());
    };
    if bytes.len() as u64 > MAX_CAPABILITY_OUTPUT_BYTES {
        return Err(invalid("recovery probe output exceeded limit").into());
    }
    let value: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|_| invalid("driver returned invalid recovery probe JSON"))?;
    Ok(value)
}

fn invoke_claimed_attempt(
    state_dir: &Path,
    binding: &BindingView,
    attempt: Attempt,
    recovery_from_attempt: Option<i64>,
    driver: &Path,
    timeout: Duration,
) -> Result<AttemptResult, Box<dyn Error>> {
    let input = driver_input(binding, &attempt, recovery_from_attempt)?;
    let (result, evidence) = invoke_driver(driver, &input, timeout);
    record_result(
        state_dir,
        &attempt.request_id,
        attempt.attempt_number,
        result,
        &evidence,
    )?;
    Ok(result)
}

fn driver_input(
    binding: &BindingView,
    attempt: &Attempt,
    recovery_from_attempt: Option<i64>,
) -> Result<Vec<u8>, Box<dyn Error>> {
    let mut input = json!({
        "protocol_version": 1,
        "request_id": attempt.request_id,
        "attempt_number": attempt.attempt_number,
        "channel_id": binding.channel_id,
        "session_id": binding.session_id,
        "account": binding.account,
        "talker": binding.talker,
        "text": attempt.text,
    });
    if let Some(previous) = recovery_from_attempt {
        input["recovery_from_attempt"] = json!(previous);
    }
    Ok(serde_json::to_vec(&input)?)
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
        );
        CREATE TABLE IF NOT EXISTS outgoing_attempt (
            request_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
            status TEXT NOT NULL CHECK(status IN ('attempt_recorded','ui_observed','failed','indeterminate')),
            result_evidence TEXT,
            recovery_from_attempt INTEGER,
            recovery_prior_status TEXT,
            claimed_at INTEGER NOT NULL,
            finished_at INTEGER,
            PRIMARY KEY (request_id, attempt_number)
        );
        INSERT OR IGNORE INTO outgoing_attempt
            (request_id,attempt_number,status,result_evidence,claimed_at,finished_at)
        SELECT request_id,attempt_number,
            CASE WHEN status='recipient_confirmed' THEN 'ui_observed' ELSE status END,
            result_evidence,created_at,
            CASE WHEN status='attempt_recorded' THEN NULL ELSE updated_at END
        FROM outgoing_intent WHERE attempt_number > 0;"
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
    let mut item =
        item.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "outgoing request not found"))?;
    let mut stmt = conn.prepare(
        "SELECT attempt_number,status,result_evidence,recovery_from_attempt,recovery_prior_status,claimed_at,finished_at FROM outgoing_attempt WHERE request_id=?1 ORDER BY attempt_number",
    )?;
    let attempts = stmt.query_map([request_id], |r| {
        Ok(json!({
            "attempt_number": r.get::<_, i64>(0)?,
            "status": r.get::<_, String>(1)?,
            "result_evidence": r.get::<_, Option<String>>(2)?,
            "recovery_from_attempt": r.get::<_, Option<i64>>(3)?,
            "recovery_prior_status": r.get::<_, Option<String>>(4)?,
            "claimed_at": r.get::<_, i64>(5)?,
            "finished_at": r.get::<_, Option<i64>>(6)?,
        }))
    })?;
    item["attempts"] = json!(attempts.collect::<rusqlite::Result<Vec<_>>>()?);
    Ok(item)
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
    let timestamp = now()?;
    tx.execute("UPDATE outgoing_intent SET status='attempt_recorded',attempt_number=?2,updated_at=?3 WHERE request_id=?1",
        params![request_id, attempt_number, timestamp])?;
    tx.execute("INSERT INTO outgoing_attempt (request_id,attempt_number,status,claimed_at) VALUES (?1,?2,'attempt_recorded',?3)",
        params![request_id, attempt_number, timestamp])?;
    tx.commit()?;
    Ok(Attempt {
        request_id: request_id.to_owned(),
        attempt_number,
        text,
    })
}

/// One operator recovery claim is a single CAS transaction. The previous attempt is
/// retained; the next attempt is already claimed when the driver starts.
fn claim_recovery(
    state_dir: &Path,
    request_id: &str,
    expected_attempt: i64,
) -> Result<Attempt, Box<dyn Error>> {
    check_request_id(request_id)?;
    if !(1..i64::MAX).contains(&expected_attempt) {
        return Err(invalid("expected attempt must be positive and below i64::MAX").into());
    }
    let binding = load_binding(state_dir)?;
    let mut conn = open_journal(state_dir)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let previous: Option<(String, String)> = tx.query_row(
        "SELECT text,status FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND attempt_number=?4 AND status IN ('failed','indeterminate')",
        params![request_id, binding.channel_id, binding.session_id, expected_attempt],
        |r| Ok((r.get(0)?, r.get(1)?)),
    ).optional()?;
    let (text, prior_status) = previous.ok_or_else(|| {
        invalid(
            "recovery requires the current failed or indeterminate attempt and its exact number",
        )
    })?;
    let attempt_number = expected_attempt + 1;
    let timestamp = now()?;
    let changed = tx.execute(
        "UPDATE outgoing_intent SET status='attempt_recorded',attempt_number=?1,result_evidence=NULL,receipt_evidence=NULL,updated_at=?2 WHERE request_id=?3 AND channel_id=?4 AND session_id=?5 AND attempt_number=?6 AND status=?7",
        params![attempt_number, timestamp, request_id, binding.channel_id, binding.session_id, expected_attempt, prior_status],
    )?;
    if changed != 1 {
        return Err(invalid("stale outgoing recovery claim").into());
    }
    tx.execute(
        "INSERT INTO outgoing_attempt (request_id,attempt_number,status,recovery_from_attempt,recovery_prior_status,claimed_at) VALUES (?1,?2,'attempt_recorded',?3,?4,?5)",
        params![request_id, attempt_number, expected_attempt, prior_status, timestamp],
    )?;
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
    let changed = tx.execute(
        "UPDATE outgoing_attempt SET status=?1,result_evidence=?2,finished_at=?3 WHERE request_id=?4 AND attempt_number=?5 AND status='attempt_recorded'",
        params![result.as_str(), evidence, now()?, request_id, attempt_number],
    )?;
    if changed != 1 {
        return Err(invalid("missing or stale outgoing attempt audit").into());
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
    use clap::Parser;
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

    fn fake_recovery_driver(temp: &TempDir, name: &str, body: &str) -> PathBuf {
        fake_driver(
            temp,
            name,
            &format!(
                "if [ \"$1\" = '--recovery-capabilities' ]; then\n  printf '%s\\n' '{{\"protocol_version\":1,\"recovery_protocol_version\":1,\"pre_submit_proof_required\":true}}'\n  exit 0\nfi\nif [ \"$1\" = '--recovery-preflight' ]; then\n  cat >/dev/null\n  printf '%s\\n' '{{\"protocol_version\":1,\"recovery_protocol_version\":1,\"unsent_proven\":true}}'\n  exit 0\nfi\n{body}"
            ),
        )
    }

    #[test]
    fn recovery_cli_accepts_exact_operator_arguments() {
        #[derive(Parser)]
        struct Cli {
            #[command(subcommand)]
            action: OutgoingAction,
        }
        let parsed = Cli::try_parse_from([
            "outbox",
            "recover-unsent",
            "--request-id",
            "request_1",
            "--expected-attempt",
            "2",
            "--driver",
            "/tmp/recovery-driver",
        ])
        .unwrap();
        match parsed.action {
            OutgoingAction::RecoverUnsent {
                request_id,
                expected_attempt,
                driver,
            } => {
                assert_eq!(request_id, "request_1");
                assert_eq!(expected_attempt, 2);
                assert_eq!(driver, PathBuf::from("/tmp/recovery-driver"));
            }
            _ => panic!("wrong outbox action"),
        }
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
        assert_eq!(input["attempt_number"], 1);
        assert!(input.get("recovery_from_attempt").is_none());
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
    fn operator_recovery_preserves_attempts_and_uses_original_text() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "recover_1", "original text").unwrap();
        drop(conn);
        claim_attempt(&state, "recover_1").unwrap();
        record_result(
            &state,
            "recover_1",
            1,
            AttemptResult::Indeterminate,
            "clipboard snapshot unavailable before Send",
        )
        .unwrap();
        let captured = temp.path().join("recovery-input.json");
        let driver = fake_recovery_driver(
            &temp,
            "recovery-driver",
            &format!(
                "cat > '{}'\nprintf '%s\\n' '{{\"status\":\"ui_observed\",\"evidence\":\"verified empty draft under UI lock; local outgoing row observed\"}}'",
                captured.display()
            ),
        );
        assert_eq!(
            recover_unsent_with_timeout(
                &state,
                &binding,
                "recover_1",
                1,
                &driver,
                Duration::from_secs(2)
            )
            .unwrap(),
            AttemptResult::UiObserved
        );
        let input: serde_json::Value =
            serde_json::from_slice(&fs::read(captured).unwrap()).unwrap();
        assert_eq!(input["request_id"], "recover_1");
        assert_eq!(input["text"], "original text");
        assert_eq!(input["attempt_number"], 2);
        assert_eq!(input["recovery_from_attempt"], 1);
        assert_eq!(input["channel_id"], binding.channel_id);
        let conn = open_journal(&state).unwrap();
        let shown = show_intent(&conn, &binding, "recover_1").unwrap();
        assert_eq!(shown["attempt_number"], 2);
        assert_eq!(shown["attempts"][0]["status"], "indeterminate");
        assert_eq!(
            shown["attempts"][0]["result_evidence"],
            "clipboard snapshot unavailable before Send"
        );
        assert_eq!(shown["attempts"][1]["recovery_from_attempt"], 1);
        assert_eq!(
            shown["attempts"][1]["recovery_prior_status"],
            "indeterminate"
        );
        assert_eq!(shown["attempts"][1]["status"], "ui_observed");
        assert!(claim_attempt(&state, "recover_1").is_err());
    }

    #[test]
    fn recovery_claim_is_atomic_and_crash_state_cannot_be_reclaimed() {
        let (_temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "race", "same text").unwrap();
        drop(conn);
        claim_attempt(&state, "race").unwrap();
        record_result(&state, "race", 1, AttemptResult::Failed, "pre-submit stop").unwrap();
        let state_a = state.clone();
        let state_b = state.clone();
        let a = std::thread::spawn(move || claim_recovery(&state_a, "race", 1).is_ok());
        let b = std::thread::spawn(move || claim_recovery(&state_b, "race", 1).is_ok());
        assert_ne!(a.join().unwrap(), b.join().unwrap());
        assert!(claim_recovery(&state, "race", 1).is_err());
        assert!(claim_recovery(&state, "race", 2).is_err());
        let conn = open_journal(&state).unwrap();
        let shown = show_intent(&conn, &binding, "race").unwrap();
        assert_eq!(shown["status"], "attempt_recorded");
        assert_eq!(shown["attempts"].as_array().unwrap().len(), 2);
        assert_eq!(shown["attempts"][0]["result_evidence"], "pre-submit stop");
        assert_eq!(shown["attempts"][1]["recovery_prior_status"], "failed");
    }

    #[test]
    fn recovery_rejects_queued_observed_and_stale_attempts() {
        let (_temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "queued", "first").unwrap();
        queue_intent(&mut conn, &binding, "observed", "second").unwrap();
        queue_intent(&mut conn, &binding, "stale", "third").unwrap();
        drop(conn);
        assert!(claim_recovery(&state, "queued", 1).is_err());
        claim_attempt(&state, "observed").unwrap();
        record_result(
            &state,
            "observed",
            1,
            AttemptResult::UiObserved,
            "P3 UI observed",
        )
        .unwrap();
        assert!(claim_recovery(&state, "observed", 1).is_err());
        claim_attempt(&state, "stale").unwrap();
        record_result(&state, "stale", 1, AttemptResult::Failed, "pre-submit stop").unwrap();
        assert!(claim_recovery(&state, "stale", 2).is_err());
        assert!(claim_recovery(&state, "stale", 0).is_err());
        assert!(claim_recovery(&state, "stale", i64::MAX).is_err());
        assert!(claim_attempt(&state, "stale").is_err());
    }

    #[test]
    fn recovery_driver_refusal_records_new_failed_attempt_without_retry() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "refused", "same body").unwrap();
        drop(conn);
        claim_attempt(&state, "refused").unwrap();
        record_result(
            &state,
            "refused",
            1,
            AttemptResult::Failed,
            "prepare stopped",
        )
        .unwrap();
        let count = temp.path().join("refusal-invocations");
        let driver = fake_recovery_driver(
            &temp,
            "refusing-driver",
            &format!(
                "cat >/dev/null\necho call >> '{}'\nprintf '%s\\n' '{{\"status\":\"failed\",\"evidence\":\"positive pre-submit proof unavailable under UI lock\"}}'",
                count.display()
            ),
        );
        assert_eq!(
            recover_unsent_with_timeout(
                &state,
                &binding,
                "refused",
                1,
                &driver,
                Duration::from_secs(2)
            )
            .unwrap(),
            AttemptResult::Failed
        );
        assert!(
            send_with_timeout(&state, &binding, "refused", &driver, Duration::from_secs(2))
                .is_err()
        );
        assert_eq!(fs::read_to_string(count).unwrap().lines().count(), 1);
        let conn = open_journal(&state).unwrap();
        let shown = show_intent(&conn, &binding, "refused").unwrap();
        assert_eq!(shown["status"], "failed");
        assert_eq!(shown["attempts"][0]["result_evidence"], "prepare stopped");
        assert_eq!(
            shown["attempts"][1]["result_evidence"],
            "positive pre-submit proof unavailable under UI lock"
        );
    }

    #[test]
    fn legacy_driver_cannot_claim_recovery_or_receive_message() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "legacy_driver", "secret original body").unwrap();
        drop(conn);
        claim_attempt(&state, "legacy_driver").unwrap();
        record_result(
            &state,
            "legacy_driver",
            1,
            AttemptResult::Failed,
            "pre-submit failure",
        )
        .unwrap();
        let captured = temp.path().join("legacy-driver-input");
        let driver = fake_driver(
            &temp,
            "legacy-recovery-driver",
            &format!(
                "cat > '{}'\nprintf '%s\\n' '{{\"status\":\"ui_observed\",\"evidence\":\"legacy success\"}}'",
                captured.display()
            ),
        );
        assert!(recover_unsent_with_timeout(
            &state,
            &binding,
            "legacy_driver",
            1,
            &driver,
            Duration::from_secs(2)
        )
        .is_err());
        assert_eq!(fs::read(&captured).unwrap(), b"");
        let conn = open_journal(&state).unwrap();
        let shown = show_intent(&conn, &binding, "legacy_driver").unwrap();
        assert_eq!(shown["status"], "failed");
        assert_eq!(shown["attempt_number"], 1);
        assert_eq!(shown["attempts"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn invalid_or_unavailable_recovery_capability_does_not_claim() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "bad_capability", "body").unwrap();
        drop(conn);
        claim_attempt(&state, "bad_capability").unwrap();
        record_result(
            &state,
            "bad_capability",
            1,
            AttemptResult::Indeterminate,
            "pre-submit uncertainty",
        )
        .unwrap();
        let invalid = fake_driver(
            &temp,
            "wrong-capability",
            "printf '%s\\n' '{\"protocol_version\":1,\"recovery_protocol_version\":1,\"pre_submit_proof_required\":false}'",
        );
        let nonzero = fake_driver(&temp, "nonzero-capability", "exit 2");
        let timeout = fake_driver(&temp, "timeout-capability", "sleep 2");
        for (driver, limit) in [
            (&invalid, Duration::from_secs(2)),
            (&nonzero, Duration::from_secs(2)),
            (&timeout, Duration::from_millis(30)),
        ] {
            assert!(recover_unsent_with_timeout(
                &state,
                &binding,
                "bad_capability",
                1,
                driver,
                limit
            )
            .is_err());
            let conn = open_journal(&state).unwrap();
            assert_eq!(
                show_intent(&conn, &binding, "bad_capability").unwrap()["attempt_number"],
                1
            );
        }
    }

    #[test]
    fn failed_read_only_preflight_preserves_previous_attempt() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "preflight_refusal", "original body").unwrap();
        drop(conn);
        claim_attempt(&state, "preflight_refusal").unwrap();
        record_result(
            &state,
            "preflight_refusal",
            1,
            AttemptResult::Failed,
            "clipboard unavailable before Send",
        )
        .unwrap();
        let captured = temp.path().join("preflight-input");
        let normal = temp.path().join("normal-invoked");
        let driver = fake_driver(
            &temp,
            "preflight-refusal-driver",
            &format!(
                "if [ \"$1\" = '--recovery-capabilities' ]; then\n  printf '%s\\n' '{{\"protocol_version\":1,\"recovery_protocol_version\":1,\"pre_submit_proof_required\":true}}'\n  exit 0\nfi\nif [ \"$1\" = '--recovery-preflight' ]; then\n  cat > '{}'\n  printf '%s\\n' '{{\"protocol_version\":1,\"recovery_protocol_version\":1,\"unsent_proven\":false}}'\n  exit 0\nfi\necho normal >> '{}'",
                captured.display(),
                normal.display()
            ),
        );
        assert!(recover_unsent_with_timeout(
            &state,
            &binding,
            "preflight_refusal",
            1,
            &driver,
            Duration::from_secs(2)
        )
        .is_err());
        let input: serde_json::Value =
            serde_json::from_slice(&fs::read(captured).unwrap()).unwrap();
        assert_eq!(input["text"], "original body");
        assert_eq!(input["attempt_number"], 2);
        assert_eq!(input["recovery_from_attempt"], 1);
        assert!(!normal.exists());
        let conn = open_journal(&state).unwrap();
        let shown = show_intent(&conn, &binding, "preflight_refusal").unwrap();
        assert_eq!(shown["status"], "failed");
        assert_eq!(shown["attempt_number"], 1);
        assert_eq!(shown["attempts"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn race_after_preflight_is_rejected_by_claim_cas() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "preflight_race", "body").unwrap();
        drop(conn);
        claim_attempt(&state, "preflight_race").unwrap();
        record_result(
            &state,
            "preflight_race",
            1,
            AttemptResult::Failed,
            "old failure",
        )
        .unwrap();
        let ready = temp.path().join("preflight-ready");
        let release = temp.path().join("preflight-release");
        let normal = temp.path().join("normal-invoked");
        let driver = fake_driver(
            &temp,
            "race-driver",
            &format!(
                "if [ \"$1\" = '--recovery-capabilities' ]; then\n  printf '%s\\n' '{{\"protocol_version\":1,\"recovery_protocol_version\":1,\"pre_submit_proof_required\":true}}'\n  exit 0\nfi\nif [ \"$1\" = '--recovery-preflight' ]; then\n  cat >/dev/null\n  touch '{}'\n  while [ ! -e '{}' ]; do sleep 0.01; done\n  printf '%s\\n' '{{\"protocol_version\":1,\"recovery_protocol_version\":1,\"unsent_proven\":true}}'\n  exit 0\nfi\necho normal >> '{}'",
                ready.display(),
                release.display(),
                normal.display()
            ),
        );
        let state_for_thread = state.clone();
        let binding_for_thread = binding.clone();
        let worker = std::thread::spawn(move || {
            recover_unsent_with_timeout(
                &state_for_thread,
                &binding_for_thread,
                "preflight_race",
                1,
                &driver,
                Duration::from_secs(3),
            )
            .is_err()
        });
        let deadline = Instant::now() + Duration::from_secs(2);
        while !ready.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(ready.exists());
        claim_recovery(&state, "preflight_race", 1).unwrap();
        fs::write(&release, "go").unwrap();
        assert!(worker.join().unwrap());
        assert!(!normal.exists());
        let conn = open_journal(&state).unwrap();
        let shown = show_intent(&conn, &binding, "preflight_race").unwrap();
        assert_eq!(shown["status"], "attempt_recorded");
        assert_eq!(shown["attempt_number"], 2);
        assert_eq!(shown["attempts"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn opening_legacy_journal_backfills_existing_attempt_once() {
        let (_temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        queue_intent(&mut conn, &binding, "legacy", "old body").unwrap();
        conn.execute(
            "UPDATE outgoing_intent SET status='failed',attempt_number=1,result_evidence='old pre-submit failure' WHERE request_id='legacy'",
            [],
        )
        .unwrap();
        conn.execute("DROP TABLE outgoing_attempt", []).unwrap();
        drop(conn);
        let conn = open_journal(&state).unwrap();
        let shown = show_intent(&conn, &binding, "legacy").unwrap();
        assert_eq!(
            shown["attempts"][0]["result_evidence"],
            "old pre-submit failure"
        );
        assert_eq!(shown["attempts"].as_array().unwrap().len(), 1);
        drop(conn);
        let conn = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&conn, &binding, "legacy").unwrap()["attempts"]
                .as_array()
                .unwrap()
                .len(),
            1
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
