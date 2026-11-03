//! Durable outgoing intent journal. UI delivery belongs to a caller-supplied driver.

use std::error::Error;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clap::Subcommand;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::json;

use crate::cmd::channel::{load_binding, BindingView};
use crate::cmd::outgoing_media::{self, Media};

const DB_NAME: &str = "outgoing.sqlite3";
const MAX_TEXT_BYTES: usize = 64 * 1024;
const MAX_DRIVER_OUTPUT_BYTES: u64 = 16 * 1024;
const MAX_CAPABILITY_OUTPUT_BYTES: u64 = 4096;
const MAX_EVIDENCE_BYTES: usize = 4096;
const DRIVER_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(clap::Args, Debug)]
pub struct QueueOrigin {
    #[arg(long)]
    publisher: Option<String>,
    #[arg(long)]
    owner: Option<String>,
    #[arg(long)]
    expected_epoch: Option<i64>,
    #[arg(long, requires = "part_index")]
    batch_id: Option<String>,
    #[arg(long, requires = "batch_id")]
    part_index: Option<u16>,
    /// 0: explicit manual request, 1: ordinary business, 2: proactive notice.
    #[arg(long, default_value_t=1, value_parser=clap::value_parser!(u8).range(0..=2))]
    priority: u8,
    #[arg(long)]
    expires_at: Option<i64>,
}
impl QueueOrigin {
    fn validate(&self, state: &Path) -> Result<(), Box<dyn Error>> {
        validate_queue_owner(state, self.owner.as_deref(), self.expected_epoch)?;
        if let Some(publisher) = &self.publisher {
            let mut bytes = publisher.bytes();
            if publisher.len() > 40
                || !bytes.next().is_some_and(|b| b.is_ascii_alphabetic())
                || !bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            {
                return Err(invalid("publisher must be a bounded application ID").into());
            }
        }
        if let Some(batch) = &self.batch_id {
            check_request_id(batch)?;
        }
        if self.batch_id.is_some() != self.part_index.is_some()
            || self.expires_at.is_some_and(|x| x <= 0)
        {
            return Err(invalid("batch requires part index and expiry must be positive").into());
        }
        Ok(())
    }
}

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
        #[command(flatten)]
        origin: QueueOrigin,
    },
    /// Save one immutable PNG or JPEG image intent without sending it.
    QueueImage {
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        channel_id: String,
        #[arg(long)]
        session_id: String,
        #[arg(long)]
        image: PathBuf,
        /// Digest observed by the caller before queueing; source drift is refused.
        #[arg(long)]
        expected_sha256: Option<String>,
        #[command(flatten)]
        origin: QueueOrigin,
    },
    /// List intent IDs and states (message bodies are withheld).
    List,
    /// Show one intent, including its message body.
    Show {
        #[arg(long)]
        request_id: String,
    },
    /// Verify that one exact outgoing image event matches a frozen intent.
    VerifyImageEvent {
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        cursor: i64,
    },
    /// Check only the bound account's image verification credentials; never send.
    ImageVerificationStatus,
    /// Reconcile an original submitted image using later positive source evidence; never resend.
    ReconcileImageEvent {
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        expected_attempt: i64,
        #[arg(long)]
        cursor: i64,
        #[arg(long)]
        expected_epoch: i64,
        #[arg(long)]
        reason: String,
    },
    /// Claim one queued intent and pass it to a local executable exactly once.
    Send {
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        driver: std::path::PathBuf,
    },
    /// Enroll a channel in the shared desktop coordinator without resetting progress.
    Manage {
        #[arg(long)]
        root: PathBuf,
    },
    /// Execute an exact coordinator claim while holding the shared desktop lock.
    ManagedSend {
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        driver: PathBuf,
        #[arg(long)]
        root: PathBuf,
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        token: String,
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
    pub media: Option<Media>,
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
            origin,
        } => {
            if !text_stdin {
                return Err(invalid("queue requires --text-stdin").into());
            }
            check_binding(&binding, &channel_id, &session_id)?;
            check_request_id(&request_id)?;
            origin.validate(state_dir)?;
            let mut text = String::new();
            io::stdin()
                .take((MAX_TEXT_BYTES + 1) as u64)
                .read_to_string(&mut text)?;
            let status = queue_intent_published(
                &mut conn,
                &binding,
                &request_id,
                &text,
                origin.publisher.as_deref(),
            )?;
            stamp_queue_owner(&mut conn, state_dir, &request_id, &origin)?;
            println!("{}", json!({"request_id": request_id, "status": status}));
        }
        OutgoingAction::QueueImage {
            request_id,
            channel_id,
            session_id,
            image,
            expected_sha256,
            origin,
        } => {
            check_binding(&binding, &channel_id, &session_id)?;
            check_request_id(&request_id)?;
            origin.validate(state_dir)?;
            let status = queue_image_checked_published(
                &mut conn,
                state_dir,
                &binding,
                &request_id,
                &image,
                expected_sha256.as_deref(),
                origin.publisher.as_deref(),
            )?;
            stamp_queue_owner(&mut conn, state_dir, &request_id, &origin)?;
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
        OutgoingAction::VerifyImageEvent { request_id, cursor } => {
            let result =
                super::outgoing_verify::verify(state_dir, &binding, &conn, &request_id, cursor)?;
            println!("{}", result);
        }
        OutgoingAction::ImageVerificationStatus => {
            println!("{}", super::outgoing_verify::readiness(&binding));
        }
        OutgoingAction::ReconcileImageEvent {
            request_id,
            expected_attempt,
            cursor,
            expected_epoch,
            reason,
        } => {
            let result = super::outgoing_observation::reconcile(
                state_dir,
                &binding,
                &mut conn,
                &request_id,
                expected_attempt,
                cursor,
                expected_epoch,
                &reason,
            )?;
            println!("{}", result);
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
        OutgoingAction::Manage { root } => {
            drop(conn);
            enroll_managed(state_dir, &root)?;
            println!("{}", json!({"managed":true}));
        }
        OutgoingAction::ManagedSend {
            request_id,
            driver,
            root,
            dispatch_id,
            token,
        } => {
            drop(conn);
            let result = managed_send(state_dir, &request_id, &driver, &root, dispatch_id, &token)?;
            println!(
                "{}",
                json!({"request_id":request_id,"status":result.as_str()})
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
    if managed_root(state_dir)?.is_some() {
        return Err(invalid("managed channel requires coordinator dispatch").into());
    }
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
    if managed_root(state_dir)?.is_some() {
        return Err(invalid("managed recovery requires explicit coordinated review").into());
    }
    check_request_id(request_id)?;
    if !(1..i64::MAX).contains(&expected_attempt) {
        return Err(invalid("expected attempt must be positive and below i64::MAX").into());
    }
    validate_driver(driver)?;
    // Media has no recovery protocol. Refuse before invoking even a probe.
    if is_media_request(state_dir, binding, request_id)? {
        return Err(invalid("image attempts cannot be recovered").into());
    }
    check_recovery_capabilities(driver, timeout)?;
    let candidate = load_recovery_candidate(state_dir, binding, request_id, expected_attempt)?;
    let payload = driver_input(binding, &candidate, Some(expected_attempt))?;
    check_recovery_preflight(driver, &payload, timeout, None)?;
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
        "SELECT text FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND content_kind='text' AND attempt_number=?4 AND status IN ('failed','indeterminate')",
        params![request_id, binding.channel_id, binding.session_id, expected_attempt],
        |r| r.get(0),
    ).optional()?.ok_or_else(|| invalid("recovery requires the current failed or indeterminate attempt and its exact number"))?;
    Ok(Attempt {
        request_id: request_id.to_owned(),
        attempt_number: expected_attempt + 1,
        text,
        media: None,
    })
}

/// Probe before claiming: an older driver that ignores recovery fields must never
/// receive the original message as a recovery attempt. This is a driver contract;
/// Rust cannot inspect the desktop or independently prove the driver's UI checks.
pub(super) fn check_recovery_capabilities(
    driver: &Path,
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(driver, "--recovery-capabilities", None, timeout, None)?;
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

pub(super) fn check_recovery_preflight(
    driver: &Path,
    payload: &[u8],
    timeout: Duration,
    lock_fd: Option<i32>,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(
        driver,
        "--recovery-preflight",
        Some(payload),
        timeout,
        lock_fd,
    )?;
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

pub(super) fn check_image_rejection_capabilities(
    driver: &Path,
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(
        driver,
        "--image-rejection-capabilities",
        None,
        timeout,
        None,
    )?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 4
            && object.get("protocol_version") == Some(&json!(1))
            && object.get("image_rejection_protocol_version") == Some(&json!(1))
            && object.get("prepare_copy_rejection_only") == Some(&json!(true))
            && object.get("no_submit_evidence_required") == Some(&json!(true))
    });
    if !valid {
        return Err(invalid("driver lacks image copy rejection review capability").into());
    }
    Ok(())
}

pub(super) fn check_image_rejection_preflight(
    driver: &Path,
    payload: &[u8],
    timeout: Duration,
    lock_fd: i32,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(
        driver,
        "--image-rejection-preflight",
        Some(payload),
        timeout,
        Some(lock_fd),
    )?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 3
            && object.get("protocol_version") == Some(&json!(1))
            && object.get("image_rejection_protocol_version") == Some(&json!(1))
            && object.get("prepare_rejection_proven") == Some(&json!(true))
    });
    if !valid {
        return Err(invalid("driver did not prove the image preparation copy rejection").into());
    }
    Ok(())
}

pub(super) fn check_image_abandon_capabilities(
    driver: &Path,
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(driver, "--abandon-capabilities", None, timeout, None)?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 4
            && object.get("protocol_version") == Some(&json!(1))
            && object.get("abandon_protocol_version") == Some(&json!(1))
            && object.get("image_before_prepare_only") == Some(&json!(true))
            && object.get("pre_submit_proof_required") == Some(&json!(true))
    });
    if !valid {
        return Err(invalid("driver lacks image pre-prepare abandonment capability").into());
    }
    Ok(())
}

pub(super) fn check_text_button_rejection_capabilities(
    driver: &Path,
    timeout: Duration,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(
        driver,
        "--text-button-rejection-capabilities",
        None,
        timeout,
        None,
    )?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 4
            && object.get("protocol_version") == Some(&json!(1))
            && object.get("text_button_rejection_protocol_version") == Some(&json!(1))
            && object.get("prepared_text_only") == Some(&json!(true))
            && object.get("submit_marker_forbidden") == Some(&json!(true))
    });
    if !valid {
        return Err(
            invalid("driver lacks prepared text button rejection review capability").into(),
        );
    }
    Ok(())
}

pub(super) fn check_text_button_rejection_preflight(
    driver: &Path,
    payload: &[u8],
    timeout: Duration,
    lock_fd: i32,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(
        driver,
        "--text-button-rejection-preflight",
        Some(payload),
        timeout,
        Some(lock_fd),
    )?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 3
            && object.get("protocol_version") == Some(&json!(1))
            && object.get("text_button_rejection_protocol_version") == Some(&json!(1))
            && object.get("button_rejection_proven") == Some(&json!(true))
    });
    if !valid {
        return Err(invalid("driver did not prove the prepared text button rejection").into());
    }
    Ok(())
}

pub(super) fn check_image_abandon_preflight(
    driver: &Path,
    payload: &[u8],
    timeout: Duration,
    lock_fd: i32,
) -> Result<(), Box<dyn Error>> {
    let value = invoke_recovery_probe(
        driver,
        "--abandon-preflight",
        Some(payload),
        timeout,
        Some(lock_fd),
    )?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 4
            && object.get("protocol_version") == Some(&json!(1))
            && object.get("abandon_protocol_version") == Some(&json!(1))
            && object.get("unsent_proven") == Some(&json!(true))
            && object.get("before_prepare") == Some(&json!(true))
    });
    if !valid {
        return Err(invalid("driver did not prove image stopped before prepare").into());
    }
    Ok(())
}

fn invoke_recovery_probe(
    driver: &Path,
    flag: &str,
    payload: Option<&[u8]>,
    timeout: Duration,
    lock_fd: Option<i32>,
) -> Result<serde_json::Value, Box<dyn Error>> {
    let mut command = Command::new(driver);
    command.arg(flag);
    if let Some(fd) = lock_fd {
        command.env("WX_DESKTOP_LOCK_FD", fd.to_string());
        unsafe {
            command.pre_exec(move || {
                if libc::fcntl(fd, libc::F_SETFD, 0) == -1 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    let mut child = command
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
    if let Some(media) = &attempt.media {
        if let Err(error) = outgoing_media::verify(media) {
            record_result(
                state_dir,
                &attempt.request_id,
                attempt.attempt_number,
                AttemptResult::Indeterminate,
                "frozen image missing or changed before driver",
            )?;
            return Err(error);
        }
    }
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
        "protocol_version": if attempt.media.is_some() { 2 } else { 1 },
        "request_id": attempt.request_id,
        "attempt_number": attempt.attempt_number,
        "channel_id": binding.channel_id,
        "session_id": binding.session_id,
        "account": binding.account,
        "talker": binding.talker,
        "text": attempt.text,
    });
    if let Some(media) = &attempt.media {
        input["media"] = media.descriptor();
    }
    if let Some(previous) = recovery_from_attempt {
        input["recovery_from_attempt"] = json!(previous);
    }
    Ok(serde_json::to_vec(&input)?)
}

fn invoke_driver(driver: &Path, input: &[u8], timeout: Duration) -> (AttemptResult, String) {
    invoke_driver_locked(driver, input, timeout, None)
}

fn invoke_driver_locked(
    driver: &Path,
    input: &[u8],
    timeout: Duration,
    lock_fd: Option<i32>,
) -> (AttemptResult, String) {
    invoke_driver_mode(driver, input, timeout, lock_fd, false)
}

fn invoke_driver_mode(
    driver: &Path,
    input: &[u8],
    timeout: Duration,
    lock_fd: Option<i32>,
    preflight: bool,
) -> (AttemptResult, String) {
    let mut command = Command::new(driver);
    if preflight {
        command.arg("--dispatch-preflight");
    }
    command.env_remove("WX_PREFLIGHT_CONTENT_KIND");
    let image_preflight = preflight
        && serde_json::from_slice::<serde_json::Value>(input)
            .is_ok_and(|value| value["content_kind"] == "image");
    if image_preflight {
        command.env("WX_PREFLIGHT_CONTENT_KIND", "image");
    }
    if let Some(fd) = lock_fd {
        command.env("WX_DESKTOP_LOCK_FD", fd.to_string());
        // The driver retains the same open-file description if its parent dies.
        unsafe {
            command.pre_exec(move || {
                if libc::fcntl(fd, libc::F_SETFD, 0) == -1 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
    } else {
        command.env_remove("WX_DESKTOP_LOCK_FD");
    }
    let mut child = match command
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
    if preflight {
        if image_preflight
            && value.as_object().is_some_and(|object| object.len() == 3)
            && value["ready"] == false
            && value["reason_code"] == "image_verification_unavailable"
            && value["desktop_touched"] == false
        {
            return (
                AttemptResult::Failed,
                "image verification unavailable before desktop effects".into(),
            );
        }
        return if value.get("ready").and_then(|v| v.as_bool()) == Some(true) {
            (
                AttemptResult::UiObserved,
                "desktop readiness checked before attempt".into(),
            )
        } else {
            (AttemptResult::Failed, "desktop readiness refused".into())
        };
    }
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

pub(crate) fn managed_root(state_dir: &Path) -> Result<Option<PathBuf>, Box<dyn Error>> {
    let path = state_dir.join("managed-coordinator.json");
    if !path.try_exists()? {
        return Ok(None);
    }
    secure_file(&path)?;
    let data: serde_json::Value = serde_json::from_slice(&fs::read(&path)?)?;
    let root = PathBuf::from(
        data["root"]
            .as_str()
            .ok_or_else(|| invalid("invalid managed root"))?,
    );
    if data["version"] != 1 || !root.is_absolute() || root.canonicalize()? != root {
        return Err(invalid("invalid managed root identity").into());
    }
    Ok(Some(root))
}

pub(crate) fn enroll_managed(state_dir: &Path, root: &Path) -> Result<(), Box<dyn Error>> {
    if !root.is_absolute() || root.canonicalize()? != root {
        return Err(invalid("coordinator root must be canonical").into());
    }
    let meta = fs::symlink_metadata(root)?;
    if !meta.is_dir()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o777 != 0o700
    {
        return Err(invalid("coordinator directory must be private and owned").into());
    }
    if let Some(existing) = managed_root(state_dir)? {
        if existing != root {
            return Err(invalid("channel belongs to another coordinator").into());
        }
    }
    let mut conn = open_journal(state_dir)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute_batch("CREATE TABLE IF NOT EXISTS managed_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1),gate INTEGER NOT NULL);
        INSERT OR IGNORE INTO managed_control VALUES (1,0);
        UPDATE outgoing_intent SET status=CASE status WHEN 'queued' THEN 'managed_queued' ELSE 'managed_media_queued' END WHERE status IN ('queued','media_queued');
        CREATE TRIGGER IF NOT EXISTS managed_queue AFTER INSERT ON outgoing_intent WHEN NEW.status IN ('queued','media_queued') BEGIN
          UPDATE outgoing_intent SET status=CASE NEW.status WHEN 'queued' THEN 'managed_queued' ELSE 'managed_media_queued' END WHERE request_id=NEW.request_id;
        END;
        CREATE TRIGGER IF NOT EXISTS managed_attempt BEFORE INSERT ON outgoing_attempt WHEN (SELECT gate FROM managed_control WHERE singleton=1)=0 BEGIN
          SELECT RAISE(ABORT,'managed channel requires coordinator dispatch');
        END;")?;
    tx.commit()?;
    let marker = state_dir.join("managed-coordinator.json");
    if !marker.try_exists()? {
        let mut file = tempfile::NamedTempFile::new_in(state_dir)?;
        file.write_all(&serde_json::to_vec(&json!({"version":1,"root":root}))?)?;
        file.as_file().sync_all()?;
        if let Err(error) = file.persist_noclobber(&marker) {
            if error.error.kind() != io::ErrorKind::AlreadyExists
                || managed_root(state_dir)?.as_deref() != Some(root)
            {
                return Err(error.error.into());
            }
        }
        fs::File::open(state_dir)?.sync_all()?;
    }
    Ok(())
}

#[cfg(test)]
pub(crate) fn intent_view(
    state_dir: &Path,
    request_id: &str,
) -> Result<serde_json::Value, Box<dyn Error>> {
    show_intent(
        &open_journal(state_dir)?,
        &load_binding(state_dir)?,
        request_id,
    )
}

fn managed_send(
    state_dir: &Path,
    request_id: &str,
    driver: &Path,
    root: &Path,
    dispatch_id: i64,
    token: &str,
) -> Result<AttemptResult, Box<dyn Error>> {
    if managed_root(state_dir)?.as_deref() != Some(root) {
        return Err(invalid("managed root mismatch").into());
    }
    validate_driver(driver)?;
    let path = root.join("sender.lock");
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&path)?;
    secure_file(&path)?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(invalid("desktop busy; request remains unattempted").into());
    }
    super::coordinator::validate_dispatch(root, state_dir, request_id, dispatch_id, token)?;
    let binding = load_binding(state_dir)?;
    let image = is_media_request(state_dir, &binding, request_id)?;
    let hint: &[u8] = if image {
        br#"{"content_kind":"image"}"#
    } else {
        b"{}"
    };
    let (ready, evidence) = invoke_driver_mode(
        driver,
        hint,
        Duration::from_secs(20),
        Some(lock.as_raw_fd()),
        true,
    );
    if ready == AttemptResult::Failed
        && evidence == "image verification unavailable before desktop effects"
    {
        super::coordinator::mark_image_setup_refusal(
            root,
            state_dir,
            request_id,
            dispatch_id,
            token,
        )?;
        return Err(
            invalid("image verification unavailable; original intent remains unattempted").into(),
        );
    }
    if ready != AttemptResult::UiObserved {
        super::coordinator::pause_surface(root, "desktop readiness requires review")?;
        return Err(invalid("desktop readiness refused; intent remains unattempted").into());
    }
    // The preflight could take time; repeat the owner/control fence before claim.
    let attempt = super::coordinator::with_dispatch_authority(
        root,
        state_dir,
        request_id,
        dispatch_id,
        token,
        || claim_attempt_mode(state_dir, request_id, true),
    )?;
    let binding = load_binding(state_dir)?;
    let payload = driver_input(&binding, &attempt, None)?;
    let (result, evidence) =
        invoke_driver_locked(driver, &payload, DRIVER_TIMEOUT, Some(lock.as_raw_fd()));
    record_result(
        state_dir,
        request_id,
        attempt.attempt_number,
        result,
        &evidence,
    )?;
    Ok(result)
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
    let intent_schema = "CREATE TABLE IF NOT EXISTS outgoing_intent (
            request_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            text TEXT NOT NULL,
            content_kind TEXT NOT NULL DEFAULT 'text' CHECK(content_kind IN ('text','image')),
            media_path TEXT,
            media_sha256 TEXT,
            media_mime_type TEXT,
            media_size_bytes INTEGER,
            status TEXT NOT NULL CHECK(status IN ('queued','media_queued','managed_queued','managed_media_queued','attempt_recorded','ui_observed','failed','indeterminate','recipient_confirmed')),
            attempt_number INTEGER NOT NULL DEFAULT 0,
            result_evidence TEXT,
            receipt_evidence TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );";
    conn.execute_batch(intent_schema)?;
    let columns: Vec<String> = conn
        .prepare("PRAGMA table_info(outgoing_intent)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<_, _>>()?;
    let old_schema: String = conn.query_row(
        "SELECT sql FROM sqlite_master WHERE name='outgoing_intent'",
        [],
        |r| r.get(0),
    )?;
    if !columns.iter().any(|column| column == "content_kind")
        || !old_schema.contains("managed_queued")
    {
        let tx = conn.unchecked_transaction()?;
        tx.execute_batch("ALTER TABLE outgoing_intent RENAME TO outgoing_intent_legacy")?;
        tx.execute_batch(intent_schema)?;
        if columns.iter().any(|column| column == "content_kind") {
            tx.execute_batch("INSERT INTO outgoing_intent SELECT * FROM outgoing_intent_legacy; DROP TABLE outgoing_intent_legacy;")?;
        } else {
            tx.execute_batch("INSERT INTO outgoing_intent
            (request_id,channel_id,session_id,text,status,attempt_number,result_evidence,receipt_evidence,created_at,updated_at)
            SELECT request_id,channel_id,session_id,text,status,attempt_number,result_evidence,receipt_evidence,created_at,updated_at
            FROM outgoing_intent_legacy;
            DROP TABLE outgoing_intent_legacy;")?;
        }
        tx.commit()?;
    }
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS outgoing_attempt (
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
        FROM outgoing_intent WHERE attempt_number > 0 AND NOT EXISTS
            (SELECT 1 FROM outgoing_attempt a WHERE a.request_id=outgoing_intent.request_id AND a.attempt_number=outgoing_intent.attempt_number);"
    )?;
    let columns: Vec<String> = conn
        .prepare("PRAGMA table_info(outgoing_intent)")?
        .query_map([], |r| r.get(1))?
        .collect::<rusqlite::Result<_>>()?;
    if !columns.iter().any(|c| c == "managed_owner") {
        conn.execute_batch("ALTER TABLE outgoing_intent ADD COLUMN managed_owner TEXT; ALTER TABLE outgoing_intent ADD COLUMN managed_epoch INTEGER;")?;
    }
    if !columns.iter().any(|c| c == "batch_id") {
        conn.execute_batch("ALTER TABLE outgoing_intent ADD COLUMN batch_id TEXT;
          ALTER TABLE outgoing_intent ADD COLUMN part_index INTEGER;
          ALTER TABLE outgoing_intent ADD COLUMN priority INTEGER NOT NULL DEFAULT 1;
          ALTER TABLE outgoing_intent ADD COLUMN expires_at INTEGER;
          CREATE UNIQUE INDEX IF NOT EXISTS outgoing_batch_parts ON outgoing_intent(batch_id,part_index) WHERE batch_id IS NOT NULL;")?;
    }
    if !columns.iter().any(|c| c == "publisher_id") {
        conn.execute_batch("ALTER TABLE outgoing_intent ADD COLUMN publisher_id TEXT;")?;
    }
    conn.execute_batch(
        "CREATE TRIGGER IF NOT EXISTS outgoing_publisher_immutable
        BEFORE UPDATE OF publisher_id ON outgoing_intent
        WHEN OLD.publisher_id IS NOT NEW.publisher_id
        BEGIN SELECT RAISE(ABORT, 'publisher_id is immutable'); END;",
    )?;
    super::outgoing_observation::ensure_schema(&conn)?;
    Ok(conn)
}

fn validate_queue_owner(
    state: &Path,
    owner: Option<&str>,
    epoch: Option<i64>,
) -> Result<(), Box<dyn Error>> {
    if let Some(root) = managed_root(state)? {
        let owner = owner.ok_or_else(|| invalid("managed queue requires originating owner"))?;
        let epoch = epoch.ok_or_else(|| invalid("managed queue requires expected epoch"))?;
        super::coordinator::validate_owner(&root, state, owner, epoch)?;
    }
    Ok(())
}

fn stamp_queue_owner(
    conn: &mut Connection,
    state: &Path,
    request: &str,
    origin: &QueueOrigin,
) -> Result<(), Box<dyn Error>> {
    if managed_root(state)?.is_none() {
        return Ok(());
    }
    origin.validate(state)?;
    let owner = origin.owner.as_deref();
    let epoch = origin.expected_epoch;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (old_owner, old_epoch): (Option<String>, Option<i64>) = tx.query_row(
        "SELECT managed_owner,managed_epoch FROM outgoing_intent WHERE request_id=?1",
        [request],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if old_owner.is_some() && (old_owner.as_deref() != owner || old_epoch != epoch) {
        return Err(invalid("intent originated under another owner epoch").into());
    }
    let saved: (Option<String>, Option<i64>, i64, Option<i64>) = tx.query_row(
        "SELECT batch_id,part_index,priority,expires_at FROM outgoing_intent WHERE request_id=?1",
        [request],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
    )?;
    if old_owner.is_some()
        && saved
            != (
                origin.batch_id.clone(),
                origin.part_index.map(i64::from),
                i64::from(origin.priority),
                origin.expires_at,
            )
    {
        return Err(invalid("request already has different scheduling metadata").into());
    }
    tx.execute("UPDATE outgoing_intent SET managed_owner=?2,managed_epoch=?3,batch_id=?4,part_index=?5,priority=?6,expires_at=?7 WHERE request_id=?1 AND managed_owner IS NULL AND attempt_number=0",params![request,owner,epoch,origin.batch_id,origin.part_index,origin.priority,origin.expires_at])?;
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
fn queue_intent(
    conn: &mut Connection,
    binding: &BindingView,
    request_id: &str,
    text: &str,
) -> Result<String, Box<dyn Error>> {
    queue_intent_published(conn, binding, request_id, text, None)
}

fn queue_intent_published(
    conn: &mut Connection,
    binding: &BindingView,
    request_id: &str,
    text: &str,
    publisher: Option<&str>,
) -> Result<String, Box<dyn Error>> {
    check_request_id(request_id)?;
    check_text(text)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let existing: Option<(String, String, String, String, String, Option<String>)> = tx
        .query_row(
            "SELECT channel_id, session_id, text, status, content_kind, publisher_id FROM outgoing_intent WHERE request_id=?1",
            [request_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
        )
        .optional()?;
    let status = if let Some((channel, session, old_text, status, kind, old_publisher)) = existing {
        if channel != binding.channel_id
            || session != binding.session_id
            || old_text != text
            || kind != "text"
            || old_publisher.as_deref() != publisher
        {
            return Err(
                invalid("request ID already exists with different content or binding").into(),
            );
        }
        status
    } else {
        let timestamp = now()?;
        tx.execute(
            "INSERT INTO outgoing_intent (request_id,channel_id,session_id,text,status,created_at,updated_at,publisher_id) VALUES (?1,?2,?3,?4,'queued',?5,?5,?6)",
            params![request_id, binding.channel_id, binding.session_id, text, timestamp, publisher],
        )?;
        "queued".to_owned()
    };
    tx.commit()?;
    Ok(tx_status(conn, request_id)?.unwrap_or(status))
}

fn tx_status(conn: &Connection, request_id: &str) -> Result<Option<String>, Box<dyn Error>> {
    Ok(conn
        .query_row(
            "SELECT status FROM outgoing_intent WHERE request_id=?1",
            [request_id],
            |r| r.get(0),
        )
        .optional()?)
}

#[cfg(test)]
fn queue_image(
    conn: &mut Connection,
    state_dir: &Path,
    binding: &BindingView,
    request_id: &str,
    image: &Path,
) -> Result<String, Box<dyn Error>> {
    queue_image_checked(conn, state_dir, binding, request_id, image, None)
}

#[cfg(test)]
fn queue_image_checked(
    conn: &mut Connection,
    state_dir: &Path,
    binding: &BindingView,
    request_id: &str,
    image: &Path,
    expected_sha256: Option<&str>,
) -> Result<String, Box<dyn Error>> {
    queue_image_checked_published(
        conn,
        state_dir,
        binding,
        request_id,
        image,
        expected_sha256,
        None,
    )
}

fn queue_image_checked_published(
    conn: &mut Connection,
    state_dir: &Path,
    binding: &BindingView,
    request_id: &str,
    image: &Path,
    expected_sha256: Option<&str>,
    publisher: Option<&str>,
) -> Result<String, Box<dyn Error>> {
    let (bytes, mut media) = outgoing_media::inspect(image)?;
    if let Some(expected) = expected_sha256 {
        if expected != media.sha256 {
            return Err(invalid("image changed since caller inspection").into());
        }
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    type ExistingImageIntent = (
        String,
        String,
        String,
        String,
        String,
        i64,
        String,
        Option<String>,
    );
    let existing: Option<ExistingImageIntent> = tx.query_row(
        "SELECT channel_id,session_id,content_kind,COALESCE(media_sha256,''),COALESCE(media_mime_type,''),COALESCE(media_size_bytes,0),status,publisher_id FROM outgoing_intent WHERE request_id=?1",
        [request_id],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?)),
    ).optional()?;
    if let Some((channel, session, kind, sha, mime, size, status, old_publisher)) = existing {
        if channel != binding.channel_id
            || session != binding.session_id
            || kind != "image"
            || sha != media.sha256
            || mime != media.mime_type
            || size != media.size_bytes as i64
            || old_publisher.as_deref() != publisher
        {
            return Err(
                invalid("request ID already exists with different content or binding").into(),
            );
        }
        media.path = outgoing_media::frozen_path(state_dir, request_id)?;
        outgoing_media::verify(&media)?;
        tx.commit()?;
        return Ok(status);
    }
    outgoing_media::freeze(state_dir, request_id, &bytes, &mut media)?;
    let timestamp = now()?;
    tx.execute(
        "INSERT INTO outgoing_intent (request_id,channel_id,session_id,text,content_kind,media_path,media_sha256,media_mime_type,media_size_bytes,status,created_at,updated_at,publisher_id) VALUES (?1,?2,?3,'','image',?4,?5,?6,?7,'media_queued',?8,?8,?9)",
        params![request_id,binding.channel_id,binding.session_id,media.path.to_string_lossy(),media.sha256,media.mime_type,media.size_bytes as i64,timestamp,publisher],
    )?;
    tx.commit()?;
    Ok(tx_status(conn, request_id)?.unwrap_or_else(|| "media_queued".to_owned()))
}

fn is_media_request(
    state_dir: &Path,
    binding: &BindingView,
    request_id: &str,
) -> Result<bool, Box<dyn Error>> {
    let conn = open_journal(state_dir)?;
    Ok(conn.query_row(
        "SELECT content_kind FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3",
        params![request_id,binding.channel_id,binding.session_id],
        |r| r.get::<_, String>(0),
    ).optional()?.is_some_and(|kind| kind == "image"))
}

fn list_intents(
    conn: &Connection,
    binding: &BindingView,
) -> Result<Vec<serde_json::Value>, Box<dyn Error>> {
    let mut stmt = conn.prepare("SELECT request_id,status,attempt_number,created_at,updated_at,content_kind,media_path,media_sha256,media_mime_type,media_size_bytes FROM outgoing_intent WHERE channel_id=?1 AND session_id=?2 ORDER BY created_at,request_id")?;
    let rows = stmt.query_map(params![binding.channel_id, binding.session_id], |r| {
        let mut item = json!({"request_id": r.get::<_, String>(0)?, "status": r.get::<_, String>(1)?, "attempt_number": r.get::<_, i64>(2)?, "created_at": r.get::<_, i64>(3)?, "updated_at": r.get::<_, i64>(4)?});
        if r.get::<_, String>(5)? == "image" {
            item["media"] = json!({"path":r.get::<_, String>(6)?,"sha256":r.get::<_, String>(7)?,"mime_type":r.get::<_, String>(8)?,"size_bytes":r.get::<_, i64>(9)?});
        }
        Ok(item)
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
        "SELECT text,status,attempt_number,result_evidence,receipt_evidence,created_at,updated_at,content_kind,media_path,media_sha256,media_mime_type,media_size_bytes FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3",
        params![request_id, binding.channel_id, binding.session_id],
        |r| {
            let mut item = json!({"request_id":request_id,"text":r.get::<_, String>(0)?,"status":r.get::<_, String>(1)?,"attempt_number":r.get::<_, i64>(2)?,"result_evidence":r.get::<_, Option<String>>(3)?,"receipt_evidence":r.get::<_, Option<String>>(4)?,"created_at":r.get::<_, i64>(5)?,"updated_at":r.get::<_, i64>(6)?});
            if r.get::<_, String>(7)? == "image" {
                item["media"] = json!({"path":r.get::<_, String>(8)?,"sha256":r.get::<_, String>(9)?,"mime_type":r.get::<_, String>(10)?,"size_bytes":r.get::<_, i64>(11)?});
            }
            Ok(item)
        },
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
    let (owner, epoch): (Option<String>, Option<i64>) = conn.query_row(
        "SELECT managed_owner,managed_epoch FROM outgoing_intent WHERE request_id=?1",
        [request_id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    item["managed_owner"] = json!(owner);
    item["managed_epoch"] = json!(epoch);
    let publisher: Option<String> = conn.query_row(
        "SELECT publisher_id FROM outgoing_intent WHERE request_id=?1",
        [request_id],
        |r| r.get(0),
    )?;
    item["publisher_id"] = json!(publisher);
    let scheduling: (Option<String>, Option<i64>, i64, Option<i64>) = conn.query_row(
        "SELECT batch_id,part_index,priority,expires_at FROM outgoing_intent WHERE request_id=?1",
        [request_id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
    )?;
    item["batch_id"] = json!(scheduling.0);
    item["part_index"] = json!(scheduling.1);
    item["priority"] = json!(scheduling.2);
    item["expires_at"] = json!(scheduling.3);
    let observation: Option<(i64, i64, i64)> = conn
        .query_row(
            "SELECT attempt_number,cursor,recorded_at FROM outgoing_observation
         WHERE request_id=?1 AND channel_id=?2 AND session_id=?3",
            params![request_id, binding.channel_id, binding.session_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    if let Some((attempt, cursor, recorded_at)) = observation {
        item["late_observation"] = json!({"attempt_number":attempt,"cursor":cursor,
            "recorded_at":recorded_at,"original_attempt_status":"indeterminate"});
    }
    Ok(item)
}

/// Persist a single attempt before any sender touches the UI. Repeated calls fail closed.
#[allow(dead_code)]
pub(crate) fn claim_attempt(state_dir: &Path, request_id: &str) -> Result<Attempt, Box<dyn Error>> {
    claim_attempt_mode(state_dir, request_id, false)
}

fn claim_attempt_mode(
    state_dir: &Path,
    request_id: &str,
    managed: bool,
) -> Result<Attempt, Box<dyn Error>> {
    check_request_id(request_id)?;
    let binding = load_binding(state_dir)?;
    let mut conn = open_journal(state_dir)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if managed {
        tx.execute("UPDATE managed_control SET gate=1 WHERE singleton=1", [])?;
    } else if managed_root(state_dir)?.is_some() {
        return Err(invalid("managed channel requires coordinator dispatch").into());
    }
    let row: (String, String, Option<String>, Option<String>, Option<String>, Option<i64>) = tx.query_row(
        "SELECT text,content_kind,media_path,media_sha256,media_mime_type,media_size_bytes FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND status IN (?4,?5)",
        params![request_id, binding.channel_id, binding.session_id, if managed {"managed_queued"} else {"queued"}, if managed {"managed_media_queued"} else {"media_queued"}],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)),
    ).optional()?.ok_or_else(|| invalid("request is absent, mismatched, or already attempted"))?;
    let (text, kind, path, sha, mime, size) = row;
    let media = if kind == "image" {
        let path = PathBuf::from(path.ok_or_else(|| invalid("image intent lacks path"))?);
        let expected_path = outgoing_media::frozen_path(state_dir, request_id)?;
        if path != expected_path {
            return Err(invalid("image intent has invalid frozen path").into());
        }
        let mime_type = match mime.as_deref() {
            Some("image/png") => "image/png",
            Some("image/jpeg") => "image/jpeg",
            _ => return Err(invalid("image intent has invalid media type").into()),
        };
        let media = Media {
            path,
            sha256: sha.ok_or_else(|| invalid("image intent lacks digest"))?,
            mime_type,
            size_bytes: size.ok_or_else(|| invalid("image intent lacks size"))? as u64,
        };
        outgoing_media::verify(&media)?;
        Some(media)
    } else {
        None
    };
    let attempt_number = 1;
    let timestamp = now()?;
    tx.execute("UPDATE outgoing_intent SET status='attempt_recorded',attempt_number=?2,updated_at=?3 WHERE request_id=?1",
        params![request_id, attempt_number, timestamp])?;
    tx.execute("INSERT INTO outgoing_attempt (request_id,attempt_number,status,claimed_at) VALUES (?1,?2,'attempt_recorded',?3)",
        params![request_id, attempt_number, timestamp])?;
    if managed {
        tx.execute("UPDATE managed_control SET gate=0 WHERE singleton=1", [])?;
    }
    tx.commit()?;
    Ok(Attempt {
        request_id: request_id.to_owned(),
        attempt_number,
        text,
        media,
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
        "SELECT text,status FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND content_kind='text' AND attempt_number=?4 AND status IN ('failed','indeterminate')",
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
        media: None,
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

    #[test]
    fn managed_migration_fences_legacy_clients_and_preserves_attempts() {
        let (temp, state, binding) = setup();
        let root = temp.path().join("ui");
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let root = root.canonicalize().unwrap();
        let mut db = open_journal(&state).unwrap();
        queue_intent(&mut db, &binding, "done", "historical").unwrap();
        let old = claim_attempt(&state, "done").unwrap();
        record_result(
            &state,
            "done",
            old.attempt_number,
            AttemptResult::UiObserved,
            "historical-evidence",
        )
        .unwrap();
        queue_intent(&mut db, &binding, "pending", "queued").unwrap();
        drop(db);
        enroll_managed(&state, &root).unwrap();
        enroll_managed(&state, &root).unwrap();
        let mut db = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&db, &binding, "done").unwrap()["attempts"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            show_intent(&db, &binding, "pending").unwrap()["status"],
            "managed_queued"
        );
        assert!(claim_attempt(&state, "pending").is_err());
        // Simulate an old binary inserting its old queued status directly.
        db.execute("INSERT INTO outgoing_intent(request_id,channel_id,session_id,text,status,created_at,updated_at) VALUES('old-new',?1,?2,'body','queued',0,0)",params![binding.channel_id,binding.session_id]).unwrap();
        assert_eq!(
            show_intent(&db, &binding, "old-new").unwrap()["status"],
            "managed_queued"
        );
        // Even an old recovery client's unconditional attempt insert is fenced.
        assert!(db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,claimed_at) VALUES('old-new',1,'attempt_recorded',0)",[]).is_err());
        queue_intent(&mut db, &binding, "fresh", "new").unwrap();
        let attempt = claim_attempt_mode(&state, "fresh", true).unwrap();
        record_result(
            &state,
            "fresh",
            attempt.attempt_number,
            AttemptResult::Indeterminate,
            "unknown",
        )
        .unwrap();
        assert!(claim_attempt_mode(&state, "fresh", true).is_err());
        assert_eq!(intent_view(&state, "fresh").unwrap()["attempt_number"], 1);
    }

    #[test]
    fn busy_desktop_does_not_consume_managed_attempt() {
        let (temp, state, binding) = setup();
        let root = temp.path().join("ui");
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let root = root.canonicalize().unwrap();
        enroll_managed(&state, &root).unwrap();
        queue_intent(&mut open_journal(&state).unwrap(), &binding, "busy", "body").unwrap();
        let path = root.join("sender.lock");
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .unwrap();
        assert_eq!(
            unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
        let driver = fake_driver(&temp, "never-started", "exit 1");
        let err = managed_send(&state, "busy", &driver, &root, 1, "invalid").unwrap_err();
        assert!(err.to_string().contains("desktop busy"));
        let item = intent_view(&state, "busy").unwrap();
        assert_eq!(item["attempt_number"], 0);
        assert!(item["attempts"].as_array().unwrap().is_empty());
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
        // This verifies recovery identity/history, not subprocess startup time.
        let fixture_probe_timeout = Duration::from_secs(10);
        assert_eq!(
            recover_unsent_with_timeout(
                &state,
                &binding,
                "recover_1",
                1,
                &driver,
                fixture_probe_timeout
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
        // This fixture checks the recorded refusal, not deadline enforcement;
        // parallel CI may delay the two probe subprocesses past two seconds.
        let fixture_timeout = Duration::from_secs(10);
        assert_eq!(
            recover_unsent_with_timeout(&state, &binding, "refused", 1, &driver, fixture_timeout)
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
        // Let the handshake finish under parallel test load before checking
        // that the legacy driver received no original message bytes.
        let fixture_probe_timeout = Duration::from_secs(10);
        assert!(recover_unsent_with_timeout(
            &state,
            &binding,
            "legacy_driver",
            1,
            &driver,
            fixture_probe_timeout
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
        let refusal = recover_unsent_with_timeout(
            &state,
            &binding,
            "preflight_refusal",
            1,
            &driver,
            Duration::from_secs(10),
        )
        .unwrap_err();
        assert_eq!(
            refusal.to_string(),
            "driver did not prove the previous attempt unsent"
        );
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
        // This test exercises the CAS race, not the probe deadline. Parallel
        // tests can delay both short-lived driver subprocesses.
        let fixture_probe_timeout = Duration::from_secs(10);
        let worker = std::thread::spawn(move || {
            recover_unsent_with_timeout(
                &state_for_thread,
                &binding_for_thread,
                "preflight_race",
                1,
                &driver,
                fixture_probe_timeout,
            )
            .map_err(|error| error.to_string())
        });
        let deadline = Instant::now() + fixture_probe_timeout * 2 + Duration::from_secs(2);
        while !ready.exists() && !worker.is_finished() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        if !ready.exists() {
            // Release a late preflight before joining, so a failed readiness
            // assertion cannot leave a child waiting behind the test.
            fs::write(&release, "go").unwrap();
            let result = worker.join().unwrap();
            panic!("preflight did not signal readiness before deadline; worker result: {result:?}");
        }
        claim_recovery(&state, "preflight_race", 1).unwrap();
        fs::write(&release, "go").unwrap();
        let result = worker.join().unwrap();
        let error = result.expect_err("stale recovery claim must be rejected");
        assert!(
            error.contains("recovery requires the current failed or indeterminate attempt"),
            "unexpected recovery failure: {error}"
        );
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

    fn sample_image(format: image::ImageFormat) -> Vec<u8> {
        let pixels = image::RgbImage::from_pixel(2, 2, image::Rgb([23, 42, 91]));
        let mut output = io::Cursor::new(Vec::new());
        image::DynamicImage::ImageRgb8(pixels)
            .write_to(&mut output, format)
            .unwrap();
        output.into_inner()
    }

    #[test]
    fn image_intents_freeze_original_bytes_and_bind_request_identity() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        for (id, format, mime) in [
            ("png_image", image::ImageFormat::Png, "image/png"),
            ("jpeg_image", image::ImageFormat::Jpeg, "image/jpeg"),
        ] {
            let source = temp.path().join(format!("中文 图片 {id}"));
            let original = sample_image(format);
            fs::write(&source, &original).unwrap();
            assert_eq!(
                queue_image(&mut conn, &state, &binding, id, &source).unwrap(),
                "media_queued"
            );
            let shown = show_intent(&conn, &binding, id).unwrap();
            assert_eq!(shown["media"]["mime_type"], mime);
            assert_eq!(shown["media"]["size_bytes"], original.len());
            assert!(shown["media"]["path"].as_str().unwrap().starts_with('/'));
            assert_eq!(
                list_intents(&conn, &binding)
                    .unwrap()
                    .iter()
                    .find(|item| item["request_id"] == id)
                    .unwrap()["media"]["sha256"],
                shown["media"]["sha256"]
            );
            fs::write(&source, b"changed source").unwrap();
            let attempt = claim_attempt(&state, id).unwrap();
            let media = attempt.media.as_ref().unwrap();
            assert_eq!(fs::read(&media.path).unwrap(), original);
            let payload: serde_json::Value =
                serde_json::from_slice(&driver_input(&binding, &attempt, None).unwrap()).unwrap();
            assert_eq!(payload["protocol_version"], 2);
            assert_eq!(payload["text"], "");
            assert_eq!(payload["media"]["mime_type"], mime);
            assert_eq!(payload["media"]["path"], shown["media"]["path"]);
            assert!(queue_image(&mut conn, &state, &binding, id, &source).is_err());
        }
    }

    #[test]
    fn invalid_images_and_changed_frozen_bytes_are_refused_before_attempt() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        let source = temp.path().join("source.png");
        assert!(queue_image(&mut conn, &state, &binding, "missing", &source).is_err());
        fs::write(&source, b"not an image").unwrap();
        assert!(queue_image(&mut conn, &state, &binding, "bad", &source).is_err());
        let valid = sample_image(image::ImageFormat::Png);
        let mut animated = valid.clone();
        let insert_at = animated.len() - 12;
        let mut actl = Vec::from([
            0, 0, 0, 8, b'a', b'c', b'T', b'L', 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0,
        ]);
        animated.splice(insert_at..insert_at, actl.drain(..));
        fs::write(&source, &animated).unwrap();
        assert!(queue_image(&mut conn, &state, &binding, "animated", &source).is_err());
        fs::write(&source, &valid).unwrap();
        queue_image(&mut conn, &state, &binding, "frozen", &source).unwrap();
        let path = outgoing_media::frozen_path(&state, "frozen").unwrap();
        fs::write(path, b"corrupt").unwrap();
        assert!(claim_attempt(&state, "frozen").is_err());
        assert_eq!(
            show_intent(&conn, &binding, "frozen").unwrap()["status"],
            "media_queued"
        );
    }

    #[test]
    fn expected_digest_mismatch_does_not_create_an_intent_or_frozen_file() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        let source = temp.path().join("source.png");
        fs::write(&source, sample_image(image::ImageFormat::Png)).unwrap();
        assert!(queue_image_checked(
            &mut conn,
            &state,
            &binding,
            "drift",
            &source,
            Some(&"0".repeat(64))
        )
        .is_err());
        assert!(show_intent(&conn, &binding, "drift").is_err());
        assert!(!outgoing_media::frozen_path(&state, "drift")
            .unwrap()
            .exists());
    }

    #[test]
    fn image_failed_attempt_has_no_recovery_path() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        let source = temp.path().join("source.jpg");
        fs::write(&source, sample_image(image::ImageFormat::Jpeg)).unwrap();
        queue_image(&mut conn, &state, &binding, "image_recovery", &source).unwrap();
        claim_attempt(&state, "image_recovery").unwrap();
        record_result(
            &state,
            "image_recovery",
            1,
            AttemptResult::Indeterminate,
            "synthetic unknown",
        )
        .unwrap();
        assert!(claim_attempt(&state, "image_recovery").is_err());
        assert!(claim_recovery(&state, "image_recovery", 1).is_err());
        let driver = fake_driver(&temp, "probe", "touch should-not-run");
        assert!(recover_unsent_with_timeout(
            &state,
            &binding,
            "image_recovery",
            1,
            &driver,
            Duration::from_secs(1)
        )
        .is_err());
        assert!(!temp.path().join("should-not-run").exists());
    }

    #[test]
    fn image_driver_receives_frozen_descriptor_once_after_restart() {
        let (temp, state, binding) = setup();
        let mut conn = open_journal(&state).unwrap();
        let source = temp.path().join("图片 with spaces.jpg");
        let bytes = sample_image(image::ImageFormat::Jpeg);
        fs::write(&source, &bytes).unwrap();
        queue_image(&mut conn, &state, &binding, "driver_image", &source).unwrap();
        fs::write(&source, b"source replaced").unwrap();
        let capture = temp.path().join("driver-input.json");
        let driver = fake_driver(&temp, "image-driver", &format!("cat > '{}'\nprintf '%s\\n' '{{\"status\":\"ui_observed\",\"evidence\":\"synthetic local observation\"}}'", capture.display()));
        assert_eq!(
            send_with_timeout(
                &state,
                &binding,
                "driver_image",
                &driver,
                Duration::from_secs(5)
            )
            .unwrap(),
            AttemptResult::UiObserved
        );
        let payload: serde_json::Value =
            serde_json::from_slice(&fs::read(capture).unwrap()).unwrap();
        assert_eq!(payload["protocol_version"], 2);
        assert_eq!(payload["media"]["mime_type"], "image/jpeg");
        assert_eq!(
            fs::read(payload["media"]["path"].as_str().unwrap()).unwrap(),
            bytes
        );
        assert!(send_with_timeout(
            &state,
            &binding,
            "driver_image",
            &driver,
            Duration::from_secs(5)
        )
        .is_err());
        assert_eq!(
            show_intent(&open_journal(&state).unwrap(), &binding, "driver_image").unwrap()
                ["attempt_number"],
            1
        );
    }

    #[test]
    fn legacy_schema_migrates_text_history_and_old_claim_cannot_take_media() {
        let (temp, state, binding) = setup();
        let db = state.join(DB_NAME);
        let old = Connection::open(&db).unwrap();
        old.execute_batch("CREATE TABLE outgoing_intent (
            request_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, session_id TEXT NOT NULL,
            text TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','attempt_recorded','ui_observed','failed','indeterminate','recipient_confirmed')),
            attempt_number INTEGER NOT NULL DEFAULT 0, result_evidence TEXT, receipt_evidence TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);").unwrap();
        old.execute("INSERT INTO outgoing_intent(request_id,channel_id,session_id,text,status,created_at,updated_at) VALUES('old',?1,?2,'legacy text','queued',1,1)", params![binding.channel_id,binding.session_id]).unwrap();
        drop(old);
        fs::set_permissions(&db, fs::Permissions::from_mode(0o600)).unwrap();
        let mut conn = open_journal(&state).unwrap();
        assert_eq!(
            show_intent(&conn, &binding, "old").unwrap()["text"],
            "legacy text"
        );
        let source = temp.path().join("fixture.png");
        fs::write(&source, sample_image(image::ImageFormat::Png)).unwrap();
        queue_image(&mut conn, &state, &binding, "new", &source).unwrap();
        let old_binary_claimable: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM outgoing_intent WHERE request_id='new' AND status='queued'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(old_binary_claimable, 0);
        assert_eq!(claim_attempt(&state, "old").unwrap().text, "legacy text");
    }

    #[test]
    fn publisher_stays_frozen_across_restart_and_rejects_reuse_by_other_origin() {
        let (temp, state, binding) = setup();
        let image = temp.path().join("published.png");
        fs::write(&image, sample_image(image::ImageFormat::Png)).unwrap();
        let mut db = open_journal(&state).unwrap();
        queue_intent_published(&mut db, &binding, "app_a.notice", "body", Some("app_a")).unwrap();
        queue_image_checked_published(
            &mut db,
            &state,
            &binding,
            "app_a.image",
            &image,
            None,
            Some("app_a"),
        )
        .unwrap();
        drop(db);
        let mut db = open_journal(&state).unwrap();
        for (id, kind) in [("app_a.notice", "text"), ("app_a.image", "image")] {
            let shown = show_intent(&db, &binding, id).unwrap();
            assert_eq!(shown["publisher_id"], "app_a");
            assert_eq!(shown["attempt_number"], 0);
            if kind == "text" {
                assert!(
                    queue_intent_published(&mut db, &binding, id, "body", Some("app_b")).is_err()
                );
                assert!(queue_intent_published(&mut db, &binding, id, "body", None).is_err());
            } else {
                assert!(queue_image_checked_published(
                    &mut db,
                    &state,
                    &binding,
                    id,
                    &image,
                    None,
                    Some("app_b")
                )
                .is_err());
            }
            assert_eq!(show_intent(&db, &binding, id).unwrap(), shown);
        }
        queue_intent(&mut db, &binding, "legacy", "body").unwrap();
        assert!(show_intent(&db, &binding, "legacy").unwrap()["publisher_id"].is_null());
        assert!(
            queue_intent_published(&mut db, &binding, "legacy", "body", Some("app_a")).is_err()
        );
        assert!(db
            .execute(
                "UPDATE outgoing_intent SET publisher_id='app_a' WHERE request_id='legacy'",
                []
            )
            .is_err());
        assert_eq!(
            queue_intent(&mut db, &binding, "legacy", "body").unwrap(),
            "queued"
        );
    }

    #[test]
    fn publisher_migration_keeps_legacy_attempt_and_null_origin() {
        let (_temp, state, binding) = setup();
        let db_path = state.join(DB_NAME);
        let old = Connection::open(&db_path).unwrap();
        old.execute_batch("CREATE TABLE outgoing_intent (
            request_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, session_id TEXT NOT NULL,
            text TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','attempt_recorded','ui_observed','failed','indeterminate','recipient_confirmed')),
            attempt_number INTEGER NOT NULL DEFAULT 0, result_evidence TEXT, receipt_evidence TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);").unwrap();
        old.execute("INSERT INTO outgoing_intent(request_id,channel_id,session_id,text,status,attempt_number,result_evidence,created_at,updated_at)
            VALUES('historical',?1,?2,'old body','failed',1,'old evidence',11,12)",
            params![binding.channel_id,binding.session_id]).unwrap();
        drop(old);
        fs::set_permissions(&db_path, fs::Permissions::from_mode(0o600)).unwrap();
        let db = open_journal(&state).unwrap();
        let shown = show_intent(&db, &binding, "historical").unwrap();
        assert_eq!(shown["publisher_id"], serde_json::Value::Null);
        assert_eq!(shown["status"], "failed");
        assert_eq!(shown["result_evidence"], "old evidence");
        assert_eq!(shown["attempt_number"], 1);
        assert_eq!(shown["attempts"][0]["result_evidence"], "old evidence");
        assert_eq!(
            show_intent(&open_journal(&state).unwrap(), &binding, "historical").unwrap(),
            shown
        );
    }
}
