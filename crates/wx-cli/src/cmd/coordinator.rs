//! Private, reference-only coordinator for managed conversations.
//! The channel journal and outgoing journal remain the authoritative stores.
use std::error::Error;
use std::fs::{self, OpenOptions};
use std::io::{self, Read};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use clap::{Subcommand, ValueEnum};
use rusqlite::{
    params, Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::channel::load_binding;

const DB_NAME: &str = "coordinator.sqlite3";
type DispatchClaimRow = (String, String, String, String, i64, String, Option<i64>);
type DeliveryCompletionRow = (String, String, i64, String, Option<i64>, Option<String>);
type DeliveryDecisionDetails = (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<i64>,
    Option<String>,
);
type DeliveryShowRow = (
    String,
    i64,
    i64,
    String,
    i64,
    String,
    Option<String>,
    Option<i64>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);
type TurnReserveRow = (
    String,
    String,
    i64,
    String,
    Option<i64>,
    Option<String>,
    Option<String>,
);
type TurnRecordRow = (
    String,
    i64,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
);
struct DecisionInput<'a> {
    decision: Decision,
    request: Option<&'a str>,
    draft: Option<&'a str>,
    reason: Option<&'a str>,
    condition: Option<&'a str>,
    due_at: Option<i64>,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum InputMode {
    Pull,
    Push,
}
impl InputMode {
    fn as_str(self) -> &'static str {
        match self {
            Self::Pull => "pull",
            Self::Push => "push",
        }
    }
}
#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum ReplyPolicy {
    Observe,
    Draft,
    Authorized,
}
impl ReplyPolicy {
    fn as_str(self) -> &'static str {
        match self {
            Self::Observe => "observe",
            Self::Draft => "draft",
            Self::Authorized => "authorized",
        }
    }
}

#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum Decision {
    Reply,
    Skip,
    Defer,
    Draft,
}
impl Decision {
    fn as_str(self) -> &'static str {
        match self {
            Self::Reply => "reply",
            Self::Skip => "skip",
            Self::Defer => "defer",
            Self::Draft => "draft",
        }
    }
}

#[derive(Debug, Subcommand)]
pub enum CoordinatorAction {
    Bind {
        #[arg(long)]
        conversation_id: String,
        #[arg(long)]
        channel_state_dir: PathBuf,
        #[arg(long)]
        owner: String,
        #[arg(long, value_enum)]
        input: InputMode,
        #[arg(long, value_enum, default_value_t=ReplyPolicy::Observe)]
        reply_policy: ReplyPolicy,
    },
    Configure {
        #[arg(long)]
        conversation_id: String,
        #[arg(long)]
        expected_epoch: i64,
        #[arg(long)]
        owner: String,
        #[arg(long, value_enum)]
        input: InputMode,
        #[arg(long, value_enum, default_value_t=ReplyPolicy::Observe)]
        reply_policy: ReplyPolicy,
    },
    Takeover {
        #[arg(long)]
        conversation_id: String,
        #[arg(long)]
        expected_epoch: i64,
    },
    Handback {
        #[arg(long)]
        conversation_id: String,
        #[arg(long)]
        expected_epoch: i64,
    },
    Enqueue {
        #[arg(long)]
        conversation_id: String,
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        expected_epoch: i64,
    },
    EnqueueFocus {
        #[arg(long)]
        conversation_id: String,
        #[arg(long)]
        operation_id: String,
        #[arg(long)]
        expected_epoch: i64,
    },
    ReconcileOutbox {
        #[arg(long)]
        conversation_id: String,
    },
    Dispatch {
        #[command(subcommand)]
        action: DispatchAction,
    },
    Inbox {
        #[command(subcommand)]
        action: InboxAction,
    },
    Status {
        #[arg(long)]
        conversation_id: Option<String>,
    },
}

#[derive(Debug, Subcommand)]
pub enum DispatchAction {
    Claim {
        #[arg(long)]
        worker: String,
        #[arg(long, default_value_t = 120)]
        lease_seconds: i64,
    },
    Complete {
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        token: String,
        #[arg(long, value_enum)]
        focus_result: Option<FocusResult>,
    },
    Validate {
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        token: String,
    },
    Status,
    Pause {
        #[arg(long)]
        reason: String,
    },
    Resume,
    /// Release the surface after independent helper and desktop safety review; never replay unknown work.
    Review {
        #[arg(long)]
        reason: String,
    },
    RestoreQueued {
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        reason: String,
    },
    /// One reviewed first TEXT search failure, same dispatch, while retaining pause.
    RecoverUnsent {
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        expected_attempt: i64,
        #[arg(long)]
        owner: String,
        #[arg(long)]
        expected_epoch: i64,
        #[arg(long)]
        reason: String,
    },
    /// One reviewed first TEXT prepare-header rejection, without any submit, retaining pause.
    RecoverPrepareHeader {
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        expected_attempt: i64,
        #[arg(long)]
        owner: String,
        #[arg(long)]
        expected_epoch: i64,
        #[arg(long)]
        reason: String,
    },
    /// Retire only the original known IMAGE Copy rejection; never authorize a retry.
    ArchiveImageCopyRejection {
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        expected_attempt: i64,
        #[arg(long)]
        reason: String,
    },
    /// Retire one prepared TEXT first-button-guard rejection without replay.
    ArchiveTextButtonRejection {
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        expected_attempt: i64,
        #[arg(long)]
        reason: String,
    },
    /// Archive only a proven pre-submit attempt; retain its unknown outbox state forever.
    AbandonUnsent {
        #[arg(long)]
        dispatch_id: i64,
        #[arg(long)]
        reason: String,
        #[arg(long)]
        expected_attempt: i64,
    },
}
#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum FocusResult {
    Success,
    Failed,
}
#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum TurnEnd {
    Completed,
    Interrupted,
    Failed,
}
impl TurnEnd {
    fn as_str(self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::Interrupted => "interrupted",
            Self::Failed => "failed",
        }
    }
}

#[derive(Debug, Subcommand)]
pub enum InboxAction {
    /// Explicitly grant one current conversation to a deliberately shared Session.
    AuthorizeSharedSession {
        #[arg(long)]
        session_ref: String,
        #[arg(long)]
        conversation_id: String,
        #[arg(long)]
        owner: String,
        #[arg(long)]
        expected_epoch: i64,
        #[arg(long)]
        reason: String,
    },
    Reconcile {
        #[arg(long)]
        conversation_id: String,
    },
    Claim {
        #[arg(long)]
        conversation_id: String,
        #[arg(long)]
        owner: String,
        #[arg(long, default_value_t = 300)]
        lease_seconds: i64,
        /// Fence a subscriber's original generation before taking its lease.
        #[arg(long)]
        expected_epoch: Option<i64>,
        /// Pin the intended notification Session before taking its shared slot.
        #[arg(long)]
        session_ref: Option<String>,
    },
    Complete {
        #[arg(long)]
        delivery_id: i64,
        #[arg(long)]
        token: String,
        #[arg(long, value_enum)]
        decision: Decision,
        #[arg(long)]
        request_id: Option<String>,
        #[arg(long)]
        draft_stdin: bool,
        #[arg(long)]
        reason: Option<String>,
        #[arg(long)]
        condition: Option<String>,
        #[arg(long)]
        due_at: Option<i64>,
    },
    /// Link one canonical already observed publisher reply; never queue or send.
    CompleteObservedReply {
        #[arg(long)]
        delivery_id: i64,
        #[arg(long)]
        token: String,
        #[arg(long)]
        publisher: String,
        #[arg(long)]
        request_id: String,
        #[arg(long)]
        reason: String,
    },
    ReserveTurn {
        #[arg(long)]
        delivery_id: i64,
        #[arg(long)]
        token: String,
        #[arg(long)]
        session_ref: String,
    },
    RecordTurn {
        #[arg(long)]
        delivery_id: i64,
        #[arg(long)]
        token: String,
        #[arg(long)]
        thread_id: String,
        #[arg(long)]
        turn_id: String,
    },
    FinishTurn {
        #[arg(long)]
        delivery_id: i64,
        #[arg(long)]
        token: String,
        #[arg(long, value_enum)]
        status: TurnEnd,
    },
    Show {
        #[arg(long)]
        delivery_id: i64,
        #[arg(long)]
        owner: String,
    },
    Resume {
        #[arg(long)]
        delivery_id: i64,
        #[arg(long)]
        owner: String,
        #[arg(long)]
        expected_epoch: i64,
    },
    Cancel {
        #[arg(long)]
        delivery_id: i64,
        #[arg(long)]
        owner: String,
        #[arg(long)]
        expected_epoch: i64,
        #[arg(long)]
        reason: String,
    },
    Status {
        #[arg(long)]
        conversation_id: String,
    },
}

fn invalid(message: impl Into<String>) -> Box<dyn Error> {
    io::Error::new(io::ErrorKind::InvalidInput, message.into()).into()
}
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}
fn token() -> Result<String, Box<dyn Error>> {
    let mut bytes = [0u8; 24];
    fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(hex::encode(bytes))
}
fn check_id(value: &str) -> Result<(), Box<dyn Error>> {
    let bytes = value.as_bytes();
    if bytes.is_empty()
        || bytes.len() > 128
        || !bytes[0].is_ascii_alphanumeric()
        || !bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b':' | b'@'))
    {
        return Err(invalid("identifier must be 1..128 safe ASCII characters"));
    }
    Ok(())
}
fn private(path: &Path, dir: bool) -> Result<(), Box<dyn Error>> {
    let m = fs::symlink_metadata(path)?;
    if m.file_type().is_symlink()
        || m.uid() != unsafe { libc::geteuid() }
        || (dir && !m.is_dir())
        || (!dir && (!m.is_file() || m.nlink() != 1))
        || m.permissions().mode() & 0o777 != if dir { 0o700 } else { 0o600 }
    {
        return Err(invalid(
            "coordinator path must be private, owned and regular",
        ));
    }
    Ok(())
}
fn open(root: &Path, create: bool) -> Result<Connection, Box<dyn Error>> {
    if !root.exists() && create {
        fs::DirBuilder::new().mode(0o700).create(root)?;
    }
    private(root, true)?;
    let path = root.join(DB_NAME);
    if !path.exists() && create {
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&path)?;
    }
    private(&path, false)?;
    let flags = if create {
        OpenFlags::SQLITE_OPEN_READ_WRITE
    } else {
        OpenFlags::SQLITE_OPEN_READ_ONLY
    };
    let db = Connection::open_with_flags(path, flags)?;
    let version: i64 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
    if version > 1 {
        return Err(invalid("coordinator schema is newer than this client"));
    }
    db.busy_timeout(std::time::Duration::from_secs(5))?;
    if create {
        db.execute_batch("PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS binding (
          conversation_id TEXT PRIMARY KEY, channel_state_dir TEXT NOT NULL UNIQUE,
          channel_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
          owner TEXT NOT NULL, input_mode TEXT NOT NULL CHECK(input_mode IN ('pull','push')),
          reply_policy TEXT NOT NULL DEFAULT 'observe' CHECK(reply_policy IN ('observe','draft','authorized')),
          control TEXT NOT NULL DEFAULT 'active' CHECK(control IN ('active','taken_over')),
          epoch INTEGER NOT NULL DEFAULT 1 CHECK(epoch > 0),
          watermark INTEGER NOT NULL DEFAULT 0 CHECK(watermark >= 0),
          processed_cursor INTEGER NOT NULL DEFAULT 0 CHECK(processed_cursor >= 0),
          cutover_cursor INTEGER NOT NULL DEFAULT 0 CHECK(cutover_cursor >= 0)
        );
        CREATE TABLE IF NOT EXISTS dispatch (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES binding(conversation_id),
          request_id TEXT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('send','focus')),
          channel_id TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL
          CHECK(status IN ('queued','claimed','done','blocked','unknown')),
          token TEXT, worker TEXT, lease_until INTEGER, result TEXT, created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL, UNIQUE(conversation_id,request_id)
        );
        CREATE INDEX IF NOT EXISTS dispatch_ready ON dispatch(status,id);
        CREATE TABLE IF NOT EXISTS surface (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), paused_reason TEXT,
          active_dispatch INTEGER, epoch INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO surface(singleton) VALUES(1);
        CREATE TABLE IF NOT EXISTS delivery (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES binding(conversation_id),
          cursor_start INTEGER NOT NULL, cursor_end INTEGER NOT NULL, owner TEXT NOT NULL,
          epoch INTEGER NOT NULL, status TEXT NOT NULL
          CHECK(status IN ('pending','claimed','replied','skipped','deferred','draft')),
          token TEXT, lease_until INTEGER, decision_request_id TEXT, draft_text TEXT,
          reason TEXT, resume_condition TEXT, due_at INTEGER,
          attachment_refs TEXT NOT NULL DEFAULT '[]',
          turn_state TEXT, transport_token TEXT, session_ref TEXT, thread_id TEXT, turn_id TEXT,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          UNIQUE(conversation_id,cursor_start,cursor_end)
        );
        CREATE INDEX IF NOT EXISTS delivery_ready ON delivery(conversation_id,status,id);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn_per_session ON delivery(session_ref) WHERE turn_state IN ('reserved','recorded');
        CREATE TABLE IF NOT EXISTS delivery_turn_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id INTEGER NOT NULL,
          turn_state TEXT NOT NULL, owner TEXT NOT NULL, epoch INTEGER NOT NULL,
          session_ref TEXT, thread_id TEXT, turn_id TEXT,
          archived_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS surface_review (
          id INTEGER PRIMARY KEY AUTOINCREMENT, reason TEXT NOT NULL,
          reviewed_at INTEGER NOT NULL, unknown_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS delivery_decision_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id INTEGER NOT NULL,
          owner TEXT NOT NULL, epoch INTEGER NOT NULL, decision TEXT NOT NULL,
          request_id TEXT, draft_text TEXT, reason TEXT, resume_condition TEXT,
          due_at INTEGER, recorded_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS control_event (
          id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL,
          epoch INTEGER NOT NULL,action TEXT NOT NULL,reason TEXT,recorded_at INTEGER NOT NULL
        );
    ")?;
        let mut columns = db.prepare("PRAGMA table_info(dispatch)")?;
        let names = columns
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        if !names.iter().any(|n| n == "batch_id") {
            db.execute_batch("ALTER TABLE dispatch ADD COLUMN batch_id TEXT;
              ALTER TABLE dispatch ADD COLUMN part_index INTEGER;
              ALTER TABLE dispatch ADD COLUMN priority INTEGER NOT NULL DEFAULT 1;
              ALTER TABLE dispatch ADD COLUMN expires_at INTEGER;
              CREATE UNIQUE INDEX IF NOT EXISTS dispatch_batch_part ON dispatch(conversation_id,batch_id,part_index) WHERE batch_id IS NOT NULL;")?;
        }
        db.pragma_update(None, "user_version", 1)?;
        let columns = db
            .prepare("PRAGMA table_info(binding)")?
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        if !columns.iter().any(|x| x == "session_ref") {
            db.execute_batch("ALTER TABLE binding ADD COLUMN session_ref TEXT;")?;
        }
        super::shared_session_scope::ensure_schema(&db)?;
    }
    Ok(db)
}
#[derive(Debug)]
struct Binding {
    state: PathBuf,
    channel_id: String,
    session_id: String,
    owner: String,
    input: String,
    reply_policy: String,
    control: String,
    epoch: i64,
    watermark: i64,
    processed_cursor: i64,
    session_ref: Option<String>,
}
fn binding(db: &Connection, id: &str) -> Result<Binding, Box<dyn Error>> {
    check_id(id)?;
    db.query_row("SELECT channel_state_dir,channel_id,session_id,owner,input_mode,reply_policy,control,epoch,watermark,processed_cursor,session_ref FROM binding WHERE conversation_id=?1",[id],|r| Ok(Binding {
        state: PathBuf::from(r.get::<_,String>(0)?), channel_id:r.get(1)?,session_id:r.get(2)?,
        owner:r.get(3)?,input:r.get(4)?,reply_policy:r.get(5)?,control:r.get(6)?,epoch:r.get(7)?,watermark:r.get(8)?,processed_cursor:r.get(9)?,session_ref:r.get(10)?
    })).optional()?.ok_or_else(|| invalid("conversation is not registered"))
}
fn verify_binding(b: &Binding) -> Result<(), Box<dyn Error>> {
    let actual = load_binding(&b.state)?;
    if actual.channel_id != b.channel_id || actual.session_id != b.session_id {
        return Err(invalid("channel binding changed"));
    }
    Ok(())
}
pub(crate) fn validate_owner(
    root: &Path,
    state: &Path,
    owner: &str,
    epoch: i64,
) -> Result<(), Box<dyn Error>> {
    check_id(owner)?;
    let state = state.canonicalize()?;
    let db = open(root, false)?;
    let id: Option<String> = db
        .query_row(
            "SELECT conversation_id FROM binding WHERE channel_state_dir=?1",
            [state.to_string_lossy().as_ref()],
            |r| r.get(0),
        )
        .optional()?;
    let id = id.ok_or_else(|| invalid("channel is not registered with coordinator"))?;
    let b = binding(&db, &id)?;
    verify_binding(&b)?;
    if b.owner != owner
        || b.epoch != epoch
        || b.control != "active"
        || b.reply_policy != "authorized"
    {
        return Err(invalid("owner, control epoch or reply policy changed"));
    }
    Ok(())
}
/// Hold the coordinator write reservation across the outbox commit. The callback
/// may only reconcile evidence; this grants no desktop/send authority. Outbox is
/// committed first so a crash can only leave an unknown dispatch to finish.
#[allow(clippy::too_many_arguments)]
pub(super) fn with_observation_authority<F>(
    root: &Path,
    state: &Path,
    request: &str,
    owner: &str,
    origin_epoch: i64,
    expected_epoch: i64,
    already_observed: bool,
    finish: F,
) -> Result<Value, Box<dyn Error>>
where
    F: FnOnce() -> Result<Value, Box<dyn Error>>,
{
    let mut db = open(root, false)?;
    // Open read/write without schema migration: observation must not repair or
    // manufacture coordinator authority.
    drop(db);
    private(&root.join(DB_NAME), false)?;
    db = Connection::open_with_flags(root.join(DB_NAME), OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    db.busy_timeout(Duration::ZERO)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let conversation: String = tx.query_row(
        "SELECT conversation_id FROM binding WHERE channel_state_dir=?1",
        [state.to_string_lossy().as_ref()],
        |r| r.get(0),
    )?;
    let b = binding(&tx, &conversation)?;
    verify_binding(&b)?;
    if b.owner != owner || b.epoch != expected_epoch || b.control != "active" {
        return Err(invalid(
            "late observation owner/control/current epoch changed",
        ));
    }
    let active: Option<i64> = tx.query_row(
        "SELECT active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    let (id, status, result, epoch, channel): (i64,String,Option<String>,i64,String) = tx.query_row(
        "SELECT id,status,result,epoch,channel_id FROM dispatch WHERE conversation_id=?1 AND request_id=?2 AND operation='send'",
        params![conversation,request], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
    if active.is_some()
        || epoch != origin_epoch
        || channel != b.channel_id
        || !(status == "unknown" && result.as_deref() == Some("indeterminate")
            || already_observed && status == "done" && result.as_deref() == Some("ui_observed"))
    {
        return Err(invalid(
            "late observation requires original idle unknown dispatch",
        ));
    }
    let value = finish()?;
    if status == "unknown" {
        tx.execute("UPDATE dispatch SET status='done',result='ui_observed',updated_at=?2 WHERE id=?1 AND status='unknown' AND result='indeterminate'",params![id,now()])?;
    }
    // Deliberately preserve surface pause, control epoch, token/lease and all
    // control/turn history. An observation is never surface review or resume.
    tx.commit()?;
    Ok(value)
}

fn channel_max(state: &Path) -> Result<i64, Box<dyn Error>> {
    private(state, true)?;
    let path = state.join("channel.sqlite3");
    private(&path, false)?;
    let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    Ok(db.query_row(
        "SELECT COALESCE(MAX(cursor),0) FROM channel_events",
        [],
        |r| r.get(0),
    )?)
}
struct IntentRef {
    status: String,
    attempts: i64,
    owner: Option<String>,
    epoch: Option<i64>,
    batch_id: Option<String>,
    part_index: Option<i64>,
    priority: i64,
    expires_at: Option<i64>,
    content_kind: String,
}
impl IntentRef {
    fn ready_for(&self, b: &Binding) -> bool {
        matches!(
            self.status.as_str(),
            "managed_queued" | "managed_media_queued"
        ) && self.attempts == 0
            && self.owner.as_deref() == Some(b.owner.as_str())
            && self.epoch == Some(b.epoch)
    }
}
fn intent(b: &Binding, request: &str) -> Result<IntentRef, Box<dyn Error>> {
    check_id(request)?;
    verify_binding(b)?;
    let path = b.state.join("outgoing.sqlite3");
    private(&path, false)?;
    let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    db.query_row("SELECT status,attempt_number,managed_owner,managed_epoch,batch_id,part_index,priority,expires_at,content_kind FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3",
        params![request,b.channel_id,b.session_id],|r| Ok(IntentRef{status:r.get(0)?,attempts:r.get(1)?,owner:r.get(2)?,epoch:r.get(3)?,batch_id:r.get(4)?,part_index:r.get(5)?,priority:r.get(6)?,expires_at:r.get(7)?,content_kind:r.get(8)?}))
        .optional()?.ok_or_else(||invalid("outbox reference is absent or binding mismatched"))
}
fn enroll(
    root: &Path,
    id: &str,
    state: &Path,
    owner: &str,
    input: InputMode,
    policy: ReplyPolicy,
) -> Result<Value, Box<dyn Error>> {
    check_id(id)?;
    check_id(owner)?;
    private(state, true)?;
    let state = state.canonicalize()?;
    let b = load_binding(&state)?;
    let latest = channel_max(&state)?;
    let mut db = open(root, true)?;
    let root = root.canonicalize()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let existing: Option<(String,String,String,String,String,String)> = tx.query_row(
        "SELECT channel_state_dir,channel_id,session_id,owner,input_mode,reply_policy FROM binding WHERE conversation_id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    if let Some(ref e) = existing {
        if e != &(
            state.to_string_lossy().into_owned(),
            b.channel_id.clone(),
            b.session_id.clone(),
            owner.into(),
            input.as_str().into(),
            policy.as_str().into(),
        ) {
            return Err(invalid(
                "existing conversation binding differs; use configure for owner changes",
            ));
        }
    } else {
        let occupied: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM binding WHERE channel_state_dir=?1 OR channel_id=?2)",
            params![state.to_string_lossy(), b.channel_id],
            |r| r.get(0),
        )?;
        if occupied {
            return Err(invalid(
                "channel is already registered to another conversation",
            ));
        }
    }
    super::outgoing::enroll_managed(&state, &root)?;
    if existing.is_none() {
        tx.execute("INSERT INTO binding(conversation_id,channel_state_dir,channel_id,session_id,owner,input_mode,reply_policy,watermark,processed_cursor,cutover_cursor) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?8,?8)",
            params![id,state.to_string_lossy(),b.channel_id,b.session_id,owner,input.as_str(),policy.as_str(),latest])?;
    }
    let epoch: i64 = tx.query_row(
        "SELECT epoch FROM binding WHERE conversation_id=?1",
        [id],
        |r| r.get(0),
    )?;
    tx.commit()?;
    Ok(
        json!({"conversation_id":id,"epoch":epoch,"cutover_cursor":latest,"reply_policy":policy.as_str()}),
    )
}
fn configure(
    root: &Path,
    id: &str,
    expected: i64,
    owner: &str,
    input: InputMode,
    policy: ReplyPolicy,
) -> Result<Value, Box<dyn Error>> {
    check_id(owner)?;
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let b = binding(&tx, id)?;
    verify_binding(&b)?;
    if b.epoch != expected {
        return Err(invalid("control epoch changed"));
    }
    if b.control != "active" {
        return Err(invalid("conversation is taken over"));
    }
    let next = expected + 1;
    tx.execute(
        "UPDATE binding SET owner=?2,input_mode=?3,reply_policy=?4,epoch=?5,session_ref=NULL WHERE conversation_id=?1",
        params![id, owner, input.as_str(),policy.as_str(), next],
    )?;
    tx.execute("UPDATE delivery SET owner=?2,epoch=?3,status=CASE WHEN status='claimed' THEN 'pending' ELSE status END,token=NULL,lease_until=NULL,updated_at=?4 WHERE conversation_id=?1 AND status IN ('pending','claimed','deferred','draft') AND turn_state IS NULL",params![id,owner,next,now()])?;
    tx.execute("UPDATE dispatch SET status='blocked',result='owner_epoch_changed',updated_at=?2 WHERE conversation_id=?1 AND status='queued'",params![id,now()])?;
    tx.execute("INSERT INTO control_event(conversation_id,epoch,action,recorded_at) VALUES(?1,?2,'configure',?3)",params![id,next,now()])?;
    tx.commit()?;
    Ok(json!({"conversation_id":id,"epoch":next,"owner":owner}))
}
fn control(root: &Path, id: &str, expected: i64, takeover: bool) -> Result<Value, Box<dyn Error>> {
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let b = binding(&tx, id)?;
    if b.epoch != expected {
        return Err(invalid("control epoch changed"));
    }
    let wanted = if takeover { "active" } else { "taken_over" };
    if b.control != wanted {
        return Err(invalid("conversation control state changed"));
    }
    let next = expected + 1;
    tx.execute(
        "UPDATE binding SET control=?2,epoch=?3 WHERE conversation_id=?1",
        params![id, if takeover { "taken_over" } else { "active" }, next],
    )?;
    tx.execute("UPDATE dispatch SET status='blocked',result='control_epoch_changed',updated_at=?2 WHERE conversation_id=?1 AND status='queued'",params![id,now()])?;
    tx.execute("UPDATE delivery SET status=CASE WHEN status='claimed' THEN 'pending' ELSE status END,token=NULL,lease_until=NULL,epoch=?2,updated_at=?3 WHERE conversation_id=?1 AND status IN ('pending','claimed','deferred','draft') AND turn_state IS NULL",params![id,next,now()])?;
    tx.execute(
        "INSERT INTO control_event(conversation_id,epoch,action,recorded_at) VALUES(?1,?2,?3,?4)",
        params![
            id,
            next,
            if takeover { "takeover" } else { "handback" },
            now()
        ],
    )?;
    tx.commit()?;
    Ok(
        json!({"conversation_id":id,"epoch":next,"control":if takeover {"taken_over"} else {"active"}}),
    )
}
fn enqueue(
    root: &Path,
    id: &str,
    request: &str,
    expected: i64,
    focus: bool,
) -> Result<Value, Box<dyn Error>> {
    check_id(request)?;
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let b = binding(&tx, id)?;
    verify_binding(&b)?;
    if b.epoch != expected || b.control != "active" {
        return Err(invalid("conversation control epoch or state changed"));
    }
    if !focus && b.reply_policy != "authorized" {
        return Err(invalid("reply policy does not authorize dispatch"));
    }
    if !focus && !intent(&b, request)?.ready_for(&b) {
        return Err(invalid(
            "outbox intent is not an unattempted managed intent",
        ));
    }
    let operation = if focus { "focus" } else { "send" };
    let existing:Option<(i64,String,i64,String)>=tx.query_row("SELECT id,operation,epoch,status FROM dispatch WHERE conversation_id=?1 AND request_id=?2",params![id,request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let dispatch_id = if let Some((n, op, epoch, _)) = existing {
        if op != operation || epoch != expected {
            return Err(invalid(
                "dispatch reference already exists with different operation or epoch",
            ));
        }
        n
    } else {
        let scheduling = if focus {
            None
        } else {
            Some(intent(&b, request)?)
        };
        tx.execute("INSERT INTO dispatch(conversation_id,request_id,operation,channel_id,epoch,status,created_at,updated_at,batch_id,part_index,priority,expires_at) VALUES(?1,?2,?3,?4,?5,'queued',?6,?6,?7,?8,?9,?10)",params![id,request,operation,b.channel_id,expected,now(),scheduling.as_ref().and_then(|x|x.batch_id.as_deref()),scheduling.as_ref().and_then(|x|x.part_index),scheduling.as_ref().map_or(0,|x|x.priority),scheduling.as_ref().and_then(|x|x.expires_at)])?;
        tx.last_insert_rowid()
    };
    tx.commit()?;
    Ok(
        json!({"dispatch_id":dispatch_id,"conversation_id":id,"request_id":request,"operation":operation,"status":"queued"}),
    )
}
fn reconcile_outbox(root: &Path, id: &str) -> Result<Value, Box<dyn Error>> {
    let db = open(root, true)?;
    let b = binding(&db, id)?;
    verify_binding(&b)?;
    let path = b.state.join("outgoing.sqlite3");
    private(&path, false)?;
    let out = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let mut stmt=out.prepare("SELECT request_id FROM outgoing_intent WHERE channel_id=?1 AND session_id=?2 AND status IN ('managed_queued','managed_media_queued') AND attempt_number=0 ORDER BY created_at,rowid")?;
    let requests: Vec<String> = stmt
        .query_map(params![b.channel_id, b.session_id], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    drop(stmt);
    drop(out);
    drop(db);
    let mut created = 0;
    let mut quarantined = 0;
    for request in &requests {
        if !intent(&b, request)?.ready_for(&b) {
            quarantined += 1;
            continue;
        }
        let db = open(root, true)?;
        let exists: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM dispatch WHERE conversation_id=?1 AND request_id=?2)",
            params![id, request],
            |r| r.get(0),
        )?;
        drop(db);
        if !exists && b.control == "active" && b.reply_policy == "authorized" {
            enqueue(root, id, request, b.epoch, false)?;
            created += 1;
        }
    }
    Ok(
        json!({"conversation_id":id,"scanned":requests.len(),"created":created,"quarantined":quarantined}),
    )
}

/// A terminal archive is not a successful batch prerequisite. Retain its
/// unattempted descendants as blocked, including after explicit reauthorization.
fn block_unsent_batch_descendants(tx: &Transaction<'_>) -> Result<(), Box<dyn Error>> {
    tx.execute("UPDATE dispatch AS child SET status='blocked',result='batch_predecessor_not_sent',updated_at=?1
      WHERE child.status='queued' AND child.batch_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM dispatch parent WHERE parent.conversation_id=child.conversation_id
          AND parent.batch_id=child.batch_id AND parent.part_index<child.part_index
          AND parent.status='done' AND NOT (parent.operation='send' AND COALESCE(parent.result,'') IN ('ui_observed','recipient_confirmed')))", [now()])?;
    Ok(())
}

fn dispatch_claim(root: &Path, worker: &str, lease: i64) -> Result<Value, Box<dyn Error>> {
    check_id(worker)?;
    if !(30..=3600).contains(&lease) {
        return Err(invalid("dispatch lease must be 30..3600 seconds"));
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (paused, active): (Option<String>, Option<i64>) = tx.query_row(
        "SELECT paused_reason,active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if let Some(reason) = paused {
        return Ok(json!({"status":"paused","reason":reason}));
    }
    if let Some(id) = active {
        let (until, status): (Option<i64>, String) = tx.query_row(
            "SELECT lease_until,status FROM dispatch WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if status == "claimed" && until.unwrap_or(0) <= now() {
            tx.execute("UPDATE dispatch SET status='unknown',result='lease_expired_requires_review',updated_at=?2 WHERE id=?1",params![id,now()])?;
            tx.execute("UPDATE surface SET active_dispatch=NULL,paused_reason='expired dispatch requires review' WHERE singleton=1",[])?;
            tx.commit()?;
            return Ok(
                json!({"status":"paused","reason":"expired dispatch requires review","dispatch_id":id}),
            );
        }
        return Ok(json!({"status":"busy","dispatch_id":id}));
    }
    block_unsent_batch_descendants(&tx)?;
    let candidate:Option<(i64,String,String,String,i64)>=tx.query_row(
        "SELECT d.id,d.conversation_id,d.request_id,d.operation,d.epoch FROM dispatch d JOIN binding b ON b.conversation_id=d.conversation_id WHERE d.status='queued' AND b.control='active' AND b.epoch=d.epoch AND (d.operation='focus' OR b.reply_policy='authorized') AND NOT EXISTS (SELECT 1 FROM dispatch earlier WHERE earlier.conversation_id=d.conversation_id AND earlier.status IN ('queued','claimed','blocked','unknown') AND NOT (earlier.status='blocked' AND earlier.result='batch_predecessor_not_sent' AND earlier.batch_id IS NOT NULL AND (d.batch_id IS NULL OR earlier.batch_id<>d.batch_id)) AND NOT (earlier.status='blocked' AND earlier.epoch<>d.epoch AND (earlier.operation='focus' OR earlier.result IN ('owner_epoch_changed','control_epoch_changed','possible_manual_outgoing')) AND earlier.result IN ('focus_failed_requires_review','owner_epoch_changed','control_epoch_changed','possible_manual_outgoing')) AND ((d.batch_id IS NOT NULL AND earlier.batch_id=d.batch_id AND earlier.part_index<d.part_index) OR ((d.batch_id IS NULL OR earlier.batch_id IS NULL OR earlier.batch_id<>d.batch_id) AND earlier.id<d.id))) AND (d.batch_id IS NULL OR (SELECT COUNT(*) FROM dispatch p WHERE p.conversation_id=d.conversation_id AND p.batch_id=d.batch_id AND p.part_index<d.part_index AND p.status='done' AND p.operation='send' AND p.result IN ('ui_observed','recipient_confirmed'))=d.part_index) ORDER BY CASE WHEN d.created_at<=?1-300 THEN 0 ELSE 1 END, CASE WHEN d.created_at<=?1-300 THEN d.id ELSE d.priority END,d.id LIMIT 1",[now()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let Some((id, conversation, request, operation, epoch)) = candidate else {
        tx.commit()?;
        return Ok(json!({"status":"empty"}));
    };
    let b = binding(&tx, &conversation)?;
    verify_binding(&b)?;
    let expired: bool = tx.query_row(
        "SELECT expires_at IS NOT NULL AND expires_at<=?2 FROM dispatch WHERE id=?1",
        params![id, now()],
        |r| r.get(0),
    )?;
    if expired {
        tx.execute("UPDATE dispatch SET status='blocked',result='expired_requires_owner_review',updated_at=?2 WHERE id=?1",params![id,now()])?;
        tx.commit()?;
        return Ok(
            json!({"status":"blocked","dispatch_id":id,"reason":"expired_requires_owner_review"}),
        );
    }
    if operation == "send" && !intent(&b, &request)?.ready_for(&b) {
        tx.execute("UPDATE dispatch SET status='blocked',result='outbox_state_changed',updated_at=?2 WHERE id=?1",params![id,now()])?;
        tx.commit()?;
        return Ok(json!({"status":"blocked","dispatch_id":id,"reason":"outbox_state_changed"}));
    }
    let token = token()?;
    tx.execute("UPDATE dispatch SET status='claimed',token=?2,worker=?3,lease_until=?4,updated_at=?5 WHERE id=?1",params![id,token,worker,now()+lease,now()])?;
    tx.execute(
        "UPDATE surface SET active_dispatch=?1,epoch=epoch+1 WHERE singleton=1",
        [id],
    )?;
    tx.commit()?;
    Ok(
        json!({"status":"claimed","dispatch_id":id,"token":token,"conversation_id":conversation,
        "request_id":request,"operation":operation,"channel_state_dir":b.state,"channel_id":b.channel_id,
        "session_id":b.session_id,"owner":b.owner,"epoch":epoch,"lease_until":now()+lease}),
    )
}

/// Validate a claimed dispatch just before a managed outbox attempt is recorded.
/// This is a fence, not a cross-database transaction with outgoing.sqlite3.
pub(crate) fn validate_dispatch(
    root: &Path,
    state: &Path,
    request: &str,
    dispatch_id: i64,
    token: &str,
) -> Result<(), Box<dyn Error>> {
    let db = open(root, false)?;
    validate_dispatch_in(&db, state, request, dispatch_id, token)
}

/// Record a fenced, zero-attempt image setup refusal. Completion retires this
/// claim without pausing unrelated conversations; this never records an attempt.
pub(crate) fn mark_image_setup_refusal(
    root: &Path,
    state: &Path,
    request: &str,
    dispatch_id: i64,
    token: &str,
) -> Result<(), Box<dyn Error>> {
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    validate_dispatch_in(&tx, state, request, dispatch_id, token)?;
    let conversation: String = tx.query_row(
        "SELECT conversation_id FROM dispatch WHERE id=?1",
        [dispatch_id],
        |r| r.get(0),
    )?;
    let b = binding(&tx, &conversation)?;
    let path = b.state.join("outgoing.sqlite3");
    private(&path, false)?;
    let mut outbox = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let out = outbox.transaction()?;
    let eligible: bool = out.query_row("SELECT EXISTS(SELECT 1 FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND content_kind='image' AND status='managed_media_queued' AND attempt_number=0 AND managed_owner=?4 AND managed_epoch=?5 AND NOT EXISTS(SELECT 1 FROM outgoing_attempt WHERE request_id=?1))",
        params![request,b.channel_id,b.session_id,b.owner,b.epoch],|r|r.get(0))?;
    if !eligible {
        return Err(invalid(
            "image setup refusal requires a current zero-attempt image",
        ));
    }
    let changed = tx.execute("UPDATE dispatch SET result='image_verification_unavailable',updated_at=?3 WHERE id=?1 AND status='claimed' AND token=?2",params![dispatch_id,token,now()])?;
    if changed != 1 {
        return Err(invalid("image setup refusal claim changed"));
    }
    // Leave claim, token, surface and original intent intact. execute_one must
    // still complete this exact claim through the ordinary completion path.
    out.commit()?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn with_dispatch_authority<T>(
    root: &Path,
    state: &Path,
    request: &str,
    dispatch_id: i64,
    token: &str,
    work: impl FnOnce() -> Result<T, Box<dyn Error>>,
) -> Result<T, Box<dyn Error>> {
    let mut db = open(root, false)?;
    let tx = db.transaction()?;
    validate_dispatch_in(&tx, state, request, dispatch_id, token)?;
    // Hold the coordinator read snapshot through the outbox claim commit.
    // Control writers cannot commit a new epoch in the validation/claim gap.
    // This remains a recoverable two-file protocol, not a cross-db transaction.
    let result = work()?;
    tx.commit()?;
    Ok(result)
}

fn validate_dispatch_in(
    db: &Connection,
    state: &Path,
    request: &str,
    dispatch_id: i64,
    token: &str,
) -> Result<(), Box<dyn Error>> {
    check_id(request)?;
    let row:Option<DispatchClaimRow>=db.query_row(
        "SELECT d.conversation_id,d.request_id,d.operation,d.status,d.epoch,d.token,d.lease_until FROM dispatch d JOIN surface s ON s.active_dispatch=d.id AND s.singleton=1 WHERE d.id=?1 AND s.paused_reason IS NULL",[dispatch_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
    let Some((conversation, rid, operation, status, epoch, saved_token, until)) = row else {
        return Err(invalid("dispatch claim is absent or surface is paused"));
    };
    let b = binding(db, &conversation)?;
    if rid != request
        || operation != "send"
        || status != "claimed"
        || saved_token != token
        || until.unwrap_or(0) <= now()
        || b.epoch != epoch
        || b.control != "active"
        || b.reply_policy != "authorized"
        || b.state != state.canonicalize()?
    {
        return Err(invalid(
            "dispatch claim, binding, control epoch or lease changed",
        ));
    }
    verify_binding(&b)?;
    let item = intent(&b, request)?;
    if item.expires_at.is_some_and(|t| t <= now()) {
        return Err(invalid("intent expired before desktop effect"));
    }
    if !item.ready_for(&b) {
        return Err(invalid("managed intent is no longer unattempted"));
    }
    Ok(())
}

fn dispatch_validate(root: &Path, id: i64, token: &str) -> Result<Value, Box<dyn Error>> {
    let db = open(root, false)?;
    let row:Option<DispatchClaimRow>=db.query_row(
        "SELECT d.conversation_id,d.request_id,d.operation,d.status,d.epoch,d.token,d.lease_until FROM dispatch d JOIN surface s ON s.active_dispatch=d.id AND s.singleton=1 WHERE d.id=?1 AND s.paused_reason IS NULL",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
    let Some((conversation, request, operation, status, epoch, saved, until)) = row else {
        return Err(invalid("dispatch is not active"));
    };
    let b = binding(&db, &conversation)?;
    verify_binding(&b)?;
    if status != "claimed"
        || saved != token
        || until.unwrap_or(0) <= now()
        || b.epoch != epoch
        || b.control != "active"
        || (operation == "send" && b.reply_policy != "authorized")
    {
        return Err(invalid("dispatch claim or control fence changed"));
    }
    if operation == "send" && !intent(&b, &request)?.ready_for(&b) {
        return Err(invalid("outbox intent changed"));
    }
    Ok(
        json!({"dispatch_id":id,"conversation_id":conversation,"request_id":request,"operation":operation,"channel_state_dir":b.state,"channel_id":b.channel_id,"epoch":epoch,"status":"claimed"}),
    )
}

fn dispatch_complete(
    root: &Path,
    id: i64,
    token: &str,
    focus_result: Option<FocusResult>,
) -> Result<Value, Box<dyn Error>> {
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let row: Option<(String, String, String, String, String, Option<String>)> = tx
        .query_row(
            "SELECT conversation_id,request_id,operation,status,token,result FROM dispatch WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
        )
        .optional()?;
    let Some((conversation, request, operation, status, saved, saved_result)) = row else {
        return Err(invalid("dispatch is absent"));
    };
    if saved != token || status != "claimed" {
        return Err(invalid("dispatch completion token or state changed"));
    }
    let active: Option<i64> = tx.query_row(
        "SELECT active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    if active != Some(id) {
        return Err(invalid("dispatch no longer owns surface"));
    }
    let (next, result, pause) = if operation == "focus" {
        match focus_result {
            Some(FocusResult::Success) => ("done", "focus_completed", false),
            Some(FocusResult::Failed) => ("blocked", "focus_failed_requires_review", true),
            None => {
                return Err(invalid(
                    "focus completion requires --focus-result success|failed",
                ))
            }
        }
    } else {
        if focus_result.is_some() {
            return Err(invalid("send completion cannot carry focus result"));
        }
        let b = binding(&tx, &conversation)?;
        let item = intent(&b, &request)?;
        match (item.status.as_str(), item.attempts) {
            ("ui_observed", n) if n > 0 => ("done", "ui_observed", false),
            ("recipient_confirmed", n) if n > 0 => ("done", "recipient_confirmed", false),
            ("managed_media_queued", 0)
                if saved_result.as_deref() == Some("image_verification_unavailable")
                    && item.content_kind == "image" =>
            {
                ("blocked", "image_verification_unavailable", false)
            }
            ("managed_queued" | "managed_media_queued", 0) => ("queued", "not_attempted", false),
            ("attempt_recorded", _) => ("unknown", "attempt_recorded", true),
            ("indeterminate", _) => ("unknown", "indeterminate", true),
            ("failed", _) => ("blocked", "failed_requires_review", true),
            _ => ("unknown", "outbox_state_requires_review", true),
        }
    };
    tx.execute("UPDATE dispatch SET status=?2,result=?3,token=NULL,worker=NULL,lease_until=NULL,updated_at=?4 WHERE id=?1",params![id,next,result,now()])?;
    tx.execute("UPDATE surface SET active_dispatch=NULL,paused_reason=CASE WHEN ?1 THEN 'dispatch result requires review' ELSE paused_reason END WHERE singleton=1",[pause])?;
    tx.commit()?;
    Ok(json!({"dispatch_id":id,"status":next,"result":result,"surface_paused":pause}))
}

fn dispatch_status(root: &Path) -> Result<Value, Box<dyn Error>> {
    let db = open(root, false)?;
    let (paused, active, epoch): (Option<String>, Option<i64>, i64) = db.query_row(
        "SELECT paused_reason,active_dispatch,epoch FROM surface WHERE singleton=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let mut stmt=db.prepare("SELECT id,conversation_id,request_id,operation,status,result,epoch,batch_id,part_index,priority,expires_at,created_at FROM dispatch ORDER BY id")?;
    let rows=stmt.query_map([],|r|Ok(json!({"dispatch_id":r.get::<_,i64>(0)?,"conversation_id":r.get::<_,String>(1)?,"request_id":r.get::<_,String>(2)?,"operation":r.get::<_,String>(3)?,"status":r.get::<_,String>(4)?,"result":r.get::<_,Option<String>>(5)?,"epoch":r.get::<_,i64>(6)?,"batch_id":r.get::<_,Option<String>>(7)?,"part_index":r.get::<_,Option<i64>>(8)?,"priority":r.get::<_,i64>(9)?,"expires_at":r.get::<_,Option<i64>>(10)?,"queued_seconds":now()-r.get::<_,i64>(11)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(
        json!({"paused_reason":paused,"active_dispatch":active,"surface_epoch":epoch,"dispatches":rows}),
    )
}

fn restore_queued(root: &Path, id: i64, reason: &str) -> Result<Value, Box<dyn Error>> {
    if reason.trim().len() < 8 || reason.len() > 512 {
        return Err(invalid("restoration requires a bounded review reason"));
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (conversation, request, status, operation, epoch): (String, String, String, String, i64) =
        tx.query_row(
            "SELECT conversation_id,request_id,status,operation,epoch FROM dispatch WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )?;
    let active: Option<i64> = tx.query_row(
        "SELECT active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    let b = binding(&tx, &conversation)?;
    if active.is_some()
        || operation != "send"
        || !matches!(status.as_str(), "unknown" | "blocked")
        || b.control != "active"
        || b.reply_policy != "authorized"
    {
        return Err(invalid(
            "only a current-owner, unattempted managed intent can return to queue",
        ));
    }
    let marker = format!("restore-pending:{id}:{epoch}:{}", b.epoch);
    let pending: Option<i64> = tx
        .query_row(
            "SELECT 1 FROM surface_review WHERE reason=?1",
            [&marker],
            |r| r.get(0),
        )
        .optional()?;
    let original = intent(&b, &request)?;
    if !matches!(
        original.status.as_str(),
        "managed_queued" | "managed_media_queued"
    ) || original.attempts != 0
        || original.owner.as_deref() != Some(b.owner.as_str())
        || original.epoch != Some(epoch) && !(pending.is_some() && original.epoch == Some(b.epoch))
    {
        return Err(invalid(
            "unattempted intent does not match its original owner epoch",
        ));
    }
    // The two SQLite journals cannot commit atomically. First persist this
    // exact recovery transition; a crash after the outbox commit can then be
    // safely resumed without reinterpreting any other intent.
    tx.execute(
        "INSERT INTO surface_review(reason,reviewed_at,unknown_count) VALUES(?1,?2,0)",
        params![marker, now()],
    )?;
    tx.commit()?;
    let outbox = b.state.join("outgoing.sqlite3");
    let mut outbox = Connection::open_with_flags(&outbox, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    let outbox_tx = outbox.transaction_with_behavior(TransactionBehavior::Immediate)?;
    outbox_tx.execute(
        "UPDATE outgoing_intent SET managed_owner=?1,managed_epoch=?2 WHERE request_id=?3 AND managed_owner=?4 AND managed_epoch=?5 AND attempt_number=0 AND status IN ('managed_queued','managed_media_queued')",
        params![b.owner,b.epoch,request,original.owner,original.epoch],
    )?;
    outbox_tx.commit()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute(
        "UPDATE dispatch SET epoch=?2 WHERE id=?1",
        params![id, b.epoch],
    )?;
    tx.execute("UPDATE dispatch SET status='queued',token=NULL,worker=NULL,lease_until=NULL,result='restored_before_attempt',updated_at=?2 WHERE id=?1",params![id,now()])?;
    tx.execute("DELETE FROM surface_review WHERE reason=?1", [&marker])?;
    tx.execute("INSERT INTO surface_review(reason,reviewed_at,unknown_count) VALUES(?1,?2,(SELECT COUNT(*) FROM dispatch WHERE status='unknown'))",params![reason,now()])?;
    tx.commit()?;
    Ok(json!({"dispatch_id":id,"status":"queued","request_id":request,"attempt_number":0}))
}
fn surface_pause(root: &Path, reason: Option<&str>) -> Result<Value, Box<dyn Error>> {
    let db = open(root, true)?;
    if let Some(r) = reason {
        if r.trim().is_empty() || r.len() > 256 {
            return Err(invalid("pause reason must be 1..256 bytes"));
        }
    }
    if reason.is_none() {
        let active: Option<i64> = db.query_row(
            "SELECT active_dispatch FROM surface WHERE singleton=1",
            [],
            |r| r.get(0),
        )?;
        if active.is_some() {
            return Err(invalid("cannot resume with active dispatch"));
        }
        let unknown: i64 = db.query_row(
            "SELECT COUNT(*) FROM dispatch WHERE status='unknown'",
            [],
            |r| r.get(0),
        )?;
        if unknown > 0 {
            return Err(invalid(
                "unknown dispatch requires explicit review before resume",
            ));
        }
    }
    db.execute(
        "UPDATE surface SET paused_reason=?1 WHERE singleton=1",
        [reason],
    )?;
    Ok(json!({"paused_reason":reason}))
}

fn no_submit_artifacts(root: &Path, tid: &str, later_tid: &str) -> Result<(), Box<dyn Error>> {
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(&format!("{later_tid}."))
            || name.starts_with(&format!("{later_tid}-"))
            || ((name.starts_with(&format!("{tid}.")) || name.starts_with(&format!("{tid}-")))
                && name.contains("submit"))
        {
            return Err(invalid("native submit or later attempt artifact exists"));
        }
    }
    let state = root.join(format!("{tid}.visual-state.json"));
    if state.symlink_metadata().is_ok() {
        private(&state, false)?;
        let value: Value = serde_json::from_reader(OpenOptions::new().read(true).open(state)?)?;
        if value["id"] != tid || value["stage"] != "prepare-attempted" {
            return Err(invalid(
                "native visual state is inconsistent with pre-submit stop",
            ));
        }
    }
    Ok(())
}

fn no_image_prepare_artifacts(
    root: &Path,
    tid: &str,
    later_tid: &str,
) -> Result<(), Box<dyn Error>> {
    let permitted = [
        format!("{tid}.client-result.json"),
        format!("{tid}-inspect.inspect.job.json"),
        format!("{tid}-inspect.result.json"),
        format!("{tid}-focus.focus-group.job.json"),
        format!("{tid}-focus.focus-private.job.json"),
        format!("{tid}-focus.result.json"),
        format!("{tid}-focus.focus-attempted.json"),
        format!("{tid}-focus.before-search-click.png"),
        format!("{tid}-focus.before-search-select.png"),
        format!("{tid}-focus.before-search-paste.png"),
        format!("{tid}-focus.before-search-copy.png"),
        format!("{tid}-focus.search-copy-ready.png"),
        format!("{tid}-focus.focus-initial.png"),
        format!("{tid}-focus.focus-search-result-main.png"),
        format!("{tid}-focus.focus-search-result-confirm.png"),
        format!("{tid}-focus.focus-polls.json"),
    ];
    for entry in fs::read_dir(root)? {
        let name = entry?.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(&format!("{later_tid}."))
            || name.starts_with(&format!("{later_tid}-"))
            || ((name.starts_with(&format!("{tid}.")) || name.starts_with(&format!("{tid}-")))
                && !permitted.iter().any(|allowed| name == allowed.as_str()))
        {
            return Err(invalid(
                "image prepare, submit or later attempt artifact exists",
            ));
        }
    }
    Ok(())
}

const FOCUS_ARCHIVE_REPORT_ERROR: &str =
    "focus failed before message preparation: target group header mismatch";
const FOCUS_ARCHIVE_NATIVE_ERROR: &str = "Selected chat header is not exact target and kind";

const FOCUS_RECOVERY_NATIVE_ERRORS: [&str; 2] = [
    "Search popup had no unique category-eligible exact target within six seconds",
    "Search popup not enumerated by ScreenCaptureKit within six seconds",
];

/// Capture the exact authority and original outbox before any recovery probe.
/// Re-reading this projection is the CAS fence after the potentially slow proof.
fn focus_recovery_context(
    db: &Connection,
    id: i64,
    request: &str,
    owner: &str,
    epoch: i64,
) -> Result<(Binding, Value), Box<dyn Error>> {
    let (conversation, channel, dispatch_epoch, status, result, operation, saved_request):
        (String, String, i64, String, Option<String>, String, String) = db.query_row(
        "SELECT conversation_id,channel_id,epoch,status,result,operation,request_id FROM dispatch WHERE id=?1",
        [id], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?)))?;
    let (paused, active, surface_epoch): (Option<String>, Option<i64>, i64) = db.query_row(
        "SELECT paused_reason,active_dispatch,epoch FROM surface WHERE singleton=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let live: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM dispatch WHERE status='claimed')",
        [],
        |r| r.get(0),
    )?;
    let b = binding(db, &conversation)?;
    verify_binding(&b)?;
    if paused.is_none()
        || active.is_some()
        || live
        || status != "unknown"
        || result.as_deref() != Some("indeterminate")
        || operation != "send"
        || saved_request != request
        || channel != b.channel_id
        || dispatch_epoch != epoch
        || b.epoch != epoch
        || b.owner != owner
        || b.control != "active"
        || b.reply_policy != "authorized"
        || super::outgoing::managed_root(&b.state)?.as_deref()
            != db.path().and_then(|p| Path::new(p).parent())
    {
        return Err(invalid(
            "focus recovery requires original authorized epoch and paused idle unknown dispatch",
        ));
    }
    let item = intent(&b, request)?;
    let original = super::outgoing::intent_view(&b.state, request)?;
    let attempts = original["attempts"]
        .as_array()
        .ok_or_else(|| invalid("missing attempts"))?;
    if item.content_kind != "text"
        || item.status != "indeterminate"
        || item.attempts != 1
        || item.owner.as_deref() != Some(owner)
        || item.epoch != Some(epoch)
        || item.expires_at.is_some_and(|t| t <= now())
        || attempts.len() != 1
        || attempts[0]["attempt_number"] != 1
        || attempts[0]["status"] != "indeterminate"
        || !attempts[0]["finished_at"].is_i64()
        || !attempts[0]["recovery_from_attempt"].is_null()
        || original["result_evidence"] != attempts[0]["result_evidence"]
        || !original["receipt_evidence"].is_null()
        || original.get("late_observation").is_some()
    {
        return Err(invalid(
            "focus recovery requires a finished unrecovered first text attempt",
        ));
    }
    let snapshot = json!({"conversation":conversation,"channel":channel,"session":b.session_id,
        "state":b.state,"owner":owner,"epoch":epoch,"surface_epoch":surface_epoch,
        "paused":paused,"original":original});
    Ok((b, snapshot))
}

fn focus_recovery_artifacts(
    root: &Path,
    b: &Binding,
    request: &str,
    original: &Value,
) -> Result<Vec<(String, String)>, Box<dyn Error>> {
    let identity = format!("{}\0{}", b.channel_id, request);
    let tid = format!(
        "send-{}",
        &hex::encode(Sha256::digest(identity.as_bytes()))[..48]
    );
    let snapshot = focus_archive_snapshot(root, &tid, &format!("{tid}-a2"))?;
    if snapshot
        .iter()
        .any(|(name, _)| name.contains("before-click"))
    {
        return Err(invalid(
            "focus recovery cannot follow a search result click",
        ));
    }
    let report_path = root.join(format!("{tid}.client-result.json"));
    if original["result_evidence"] != report_path.to_string_lossy().as_ref() {
        return Err(invalid(
            "focus recovery report is not the canonical original",
        ));
    }
    let report: Value = serde_json::from_slice(&image_rejection_read(&report_path, 65536)?)?;
    let allowed = [
        "id",
        "request_id",
        "attempt_number",
        "channel_id",
        "session_id",
        "status",
        "stage",
        "error",
        "started_at",
        "elapsed_seconds",
    ];
    if report
        .as_object()
        .is_none_or(|o| o.keys().any(|k| !allowed.contains(&k.as_str())))
        || report["id"] != tid
        || report["request_id"] != request
        || report["attempt_number"] != 1
        || report["channel_id"] != b.channel_id
        || report["session_id"] != b.session_id
        || report["status"] != "indeterminate"
        || report["stage"] != "inspected"
        || report["error"] != "focus failed before message preparation: helper rejected action"
    {
        return Err(invalid(
            "focus recovery report is not the exact before-prepare search failure",
        ));
    }
    let native: Value = serde_json::from_slice(&image_rejection_read(
        &root.join(format!("{tid}-focus.result.json")),
        65536,
    )?)?;
    if !FOCUS_RECOVERY_NATIVE_ERRORS.iter().any(|error| {
        native
            == json!({"id":format!("{tid}-focus"),"ok":false,"error":error,
            "permissions":{"accessibility":true,"postEvents":true,"screenRecording":true}})
    }) {
        return Err(invalid(
            "focus recovery native result does not prove the exact search failure",
        ));
    }
    Ok(snapshot)
}

fn prepare_header_recovery_artifacts(
    root: &Path,
    b: &Binding,
    request: &str,
    original: &Value,
) -> Result<Vec<(String, String)>, Box<dyn Error>> {
    let tid = format!(
        "send-{}",
        &hex::encode(Sha256::digest(
            format!("{}\0{}", b.channel_id, request).as_bytes()
        ))[..48]
    );
    let snapshot = text_rejection_snapshot(root, &tid, true)?;
    let report_path = root.join(format!("{tid}.client-result.json"));
    if original["result_evidence"] != report_path.to_string_lossy().as_ref() {
        return Err(invalid(
            "prepare-header recovery report is not the canonical original",
        ));
    }
    let report: Value = serde_json::from_slice(&image_rejection_read(&report_path, 65536)?)?;
    let allowed = [
        "id",
        "request_id",
        "attempt_number",
        "channel_id",
        "session_id",
        "status",
        "stage",
        "error",
        "started_at",
        "elapsed_seconds",
        "target_kind",
        "focus_evidence_id",
        "focus_category",
    ];
    if report.as_object().is_none_or(|o| {
        o.keys().any(|k| !allowed.contains(&k.as_str()))
            || allowed
                .iter()
                .any(|k| *k != "elapsed_seconds" && !o.contains_key(*k))
    }) || report["id"] != tid
        || report["request_id"] != request
        || report["attempt_number"] != 1
        || report["channel_id"] != b.channel_id
        || report["session_id"] != b.session_id
        || report["status"] != "indeterminate"
        || report["stage"] != "group_focused"
        || report["error"] != "target group header mismatch"
        || report["target_kind"] != "group"
        || report["focus_evidence_id"] != format!("{tid}-focus")
        || report["focus_category"] != "group-header"
    {
        return Err(invalid(
            "prepare-header recovery report is not the exact prepare rejection",
        ));
    }
    let native: Value = serde_json::from_slice(&image_rejection_read(
        &root.join(format!("{tid}.prepare-visual.result.json")),
        65536,
    )?)?;
    if native
        != json!({"id":tid,"ok":false,
        "error":"Fresh chat header is not exact target and kind",
        "permissions":{"accessibility":true,"postEvents":true,"screenRecording":true}})
    {
        return Err(invalid("prepare-header recovery native result differs"));
    }
    let state: Value = serde_json::from_slice(&image_rejection_read(
        &root.join(format!("{tid}.visual-state.json")),
        65536,
    )?)?;
    if state["id"] != tid || state["stage"] != "prepare-attempted" {
        return Err(invalid(
            "prepare-header recovery requires original prepare-attempted state",
        ));
    }
    Ok(snapshot.into_iter().collect())
}

#[derive(Clone, Copy)]
enum RecoveryKind {
    FocusSearch,
    PrepareHeader,
}
impl RecoveryKind {
    fn label(self) -> &'static str {
        match self {
            Self::FocusSearch => "focus_recovery",
            Self::PrepareHeader => "prepare_header_recovery",
        }
    }
    fn artifacts(
        self,
        root: &Path,
        binding: &Binding,
        request: &str,
        original: &Value,
    ) -> Result<Vec<(String, String)>, Box<dyn Error>> {
        match self {
            Self::FocusSearch => focus_recovery_artifacts(root, binding, request, original),
            Self::PrepareHeader => {
                prepare_header_recovery_artifacts(root, binding, request, original)
            }
        }
    }
    fn preflight(
        self,
        state: &Path,
        request: &str,
        driver: &Path,
        fd: i32,
    ) -> Result<(), Box<dyn Error>> {
        match self {
            Self::FocusSearch => {
                super::outgoing::focus_recovery_preflight(state, request, driver, fd)
            }
            Self::PrepareHeader => {
                super::outgoing::prepare_header_recovery_preflight(state, request, driver, fd)
            }
        }
    }
}

struct RecoveryReview<'a> {
    id: i64,
    request: &'a str,
    expected_attempt: i64,
    owner: &'a str,
    epoch: i64,
    reason: &'a str,
}

fn recover_reviewed(
    root: &Path,
    kind: RecoveryKind,
    review: RecoveryReview<'_>,
) -> Result<Value, Box<dyn Error>> {
    let RecoveryReview {
        id,
        request,
        expected_attempt,
        owner,
        epoch,
        reason,
    } = review;
    let root = &root.canonicalize()?;
    check_id(request)?;
    check_id(owner)?;
    if expected_attempt != 1 || reason.trim().len() < 8 || reason.len() > 512 {
        return Err(invalid(
            "focus recovery requires attempt 1 and a bounded audit reason",
        ));
    }
    let mut locks = Vec::new();
    for name in ["executor.lock", "sender.lock"] {
        let path = root.join(name);
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&path)?;
        private(&path, false)?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(invalid("desktop busy; focus recovery remains unattempted"));
        }
        locks.push(lock);
    }
    let sender_fd = locks[1].as_raw_fd();
    let mut db = open(root, true)?;
    let (b, snapshot) = focus_recovery_context(&db, id, request, owner, epoch)?;
    let artifacts = kind.artifacts(root, &b, request, &snapshot["original"])?;
    let driver = b.state.join("conversation-sender");
    let meta = fs::symlink_metadata(&driver)?;
    if !meta.is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.nlink() != 1
        || meta.permissions().mode() & 0o777 != 0o700
    {
        return Err(invalid(
            "registered sender driver is not private and executable",
        ));
    }
    kind.preflight(&b.state, request, &driver, sender_fd)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (_, latest) = focus_recovery_context(&tx, id, request, owner, epoch)?;
    if latest != snapshot || kind.artifacts(root, &b, request, &snapshot["original"])? != artifacts
    {
        return Err(invalid(
            "focus recovery authority or proof changed during preflight",
        ));
    }
    let claim_token = token()?;
    let purpose = format!("reviewed_{}", kind.label());
    let worker = format!("reviewed-{}", kind.label().replace('_', "-"));
    let changed = tx.execute("UPDATE dispatch SET status='claimed',result=?6,token=?2,worker=?7,lease_until=?3,updated_at=?4 WHERE id=?1 AND status='unknown' AND result='indeterminate' AND epoch=?5",
        params![id,claim_token,now()+120,now(),epoch,purpose,worker])?;
    if changed != 1 {
        return Err(invalid("stale focus recovery dispatch claim"));
    }
    tx.execute("UPDATE surface SET active_dispatch=?1,epoch=epoch+1 WHERE singleton=1 AND active_dispatch IS NULL AND paused_reason IS NOT NULL",[id])?;
    tx.execute("INSERT INTO surface_review(reason,reviewed_at,unknown_count) VALUES(?1,?2,(SELECT COUNT(*) FROM dispatch WHERE status='unknown'))",
        params![format!("{} dispatch={id} request={request} owner={owner} epoch={epoch} attempt=1->2: {reason}", kind.label()),now()])?;
    // Durable recovery reservation and audit precede attempt 2 and all effects.
    tx.commit()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    // No control writer may commit between admission, attempt and driver result.
    // A crash retains the committed reservation; it never requeues the request.
    let valid: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM dispatch d JOIN surface s ON s.active_dispatch=d.id JOIN binding b ON b.conversation_id=d.conversation_id WHERE d.id=?1 AND d.request_id=?2 AND d.status='claimed' AND d.result=?6 AND d.token=?3 AND d.epoch=?4 AND b.epoch=?4 AND b.owner=?5 AND b.control='active' AND b.reply_policy='authorized' AND s.paused_reason IS NOT NULL)",
        params![id,request,claim_token,epoch,owner,purpose],|r|r.get(0))?;
    if !valid {
        return Err(invalid("reviewed focus recovery reservation changed"));
    }
    let current = binding(&tx, snapshot["conversation"].as_str().unwrap())?;
    verify_binding(&current)?;
    if current.state != b.state
        || current.channel_id != b.channel_id
        || current.session_id != b.session_id
        || kind.artifacts(root, &b, request, &snapshot["original"])? != artifacts
    {
        return Err(invalid(
            "focus recovery binding or evidence changed before attempt",
        ));
    }
    let attempt =
        super::outgoing::claim_managed_focus_recovery(&b.state, request, &snapshot["original"])?;
    super::outgoing::execute_managed_focus_recovery(&b.state, &driver, attempt, sender_fd)?;
    tx.commit()?;
    let mut completed = dispatch_complete(root, id, &claim_token, None)?;
    completed["surface_paused"] = json!(true);
    completed["attempt_number"] = json!(2);
    completed["recovery_from_attempt"] = json!(1);
    Ok(completed)
}

// Shared bounded artifact snapshot; admission and native failure checks remain
// separate for the archive-only and first-search-recovery commands.
fn focus_archive_snapshot(
    root: &Path,
    tid: &str,
    later_tid: &str,
) -> Result<Vec<(String, String)>, Box<dyn Error>> {
    let mut snapshot = Vec::new();
    let mut total = 0usize;
    for (index, entry) in fs::read_dir(root)?.enumerate() {
        if index >= 10000 {
            return Err(invalid("focus archive directory exceeds bounded proof"));
        }
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with(&format!("{later_tid}.")) || name.starts_with(&format!("{later_tid}-"))
        {
            return Err(invalid("later native attempt artifact exists"));
        }
        if !name.starts_with(&format!("{tid}.")) && !name.starts_with(&format!("{tid}-")) {
            continue;
        }
        let lower = name.to_ascii_lowercase();
        if [
            "prepare",
            "submit",
            "visual-state",
            "late",
            "recovery",
            "focus-evidence",
        ]
        .iter()
        .any(|word| lower.contains(word))
        {
            return Err(invalid(
                "focus archival trace contains later desktop evidence",
            ));
        }
        if name != format!("{tid}.client-result.json")
            && !name.starts_with(&format!("{tid}-inspect."))
            && !name.starts_with(&format!("{tid}-focus."))
        {
            return Err(invalid(
                "focus archival trace contains prepare, state or submit artifact",
            ));
        }
        let bytes = image_rejection_read(
            &entry.path(),
            if name.ends_with(".png") {
                16 * 1024 * 1024
            } else {
                65536
            },
        )?;
        total += bytes.len();
        if total > 128 * 1024 * 1024 {
            return Err(invalid("focus archive evidence exceeds bounded proof"));
        }
        snapshot.push((name, hex::encode(Sha256::digest(&bytes))));
    }
    snapshot.sort();
    for required in [
        format!("{tid}.client-result.json"),
        format!("{tid}-inspect.inspect.job.json"),
        format!("{tid}-inspect.result.json"),
        format!("{tid}-focus.focus-group.job.json"),
        format!("{tid}-focus.result.json"),
        format!("{tid}-focus.focus-attempted.json"),
    ] {
        if !snapshot.iter().any(|(name, _)| name == &required) {
            return Err(invalid("focus archival trace is incomplete"));
        }
    }
    Ok(snapshot)
}

fn check_native_focus_failure(root: &Path, tid: &str) -> Result<(), Box<dyn Error>> {
    let path = root.join(format!("{tid}-focus.result.json"));
    private(&path, false)?;
    if fs::metadata(&path)?.len() > 65536 {
        return Err(invalid("native focus result is too large"));
    }
    let value: Value = serde_json::from_slice(&fs::read(path)?)?;
    let valid = value.as_object().is_some_and(|object| {
        object.len() == 4
            && object.get("id") == Some(&json!(format!("{tid}-focus")))
            && object.get("ok") == Some(&json!(false))
            && object.get("error") == Some(&json!(FOCUS_ARCHIVE_NATIVE_ERROR))
            && object
                .get("permissions")
                .and_then(Value::as_object)
                .is_some_and(|permissions| {
                    permissions.len() == 3
                        && permissions.get("accessibility") == Some(&json!(true))
                        && permissions.get("postEvents") == Some(&json!(true))
                        && permissions.get("screenRecording") == Some(&json!(true))
                })
    });
    if !valid {
        return Err(invalid(
            "native focus result does not prove exact guarded failure",
        ));
    }
    Ok(())
}

fn abandon_unsent(
    root: &Path,
    id: i64,
    reason: &str,
    expected_attempt: i64,
) -> Result<Value, Box<dyn Error>> {
    if reason.trim().len() < 8 || reason.len() > 512 || !(1..i64::MAX).contains(&expected_attempt) {
        return Err(invalid(
            "abandonment requires a review reason and exact attempt",
        ));
    }
    // The proof handshake, artifact reads and final scheduler update share the
    // executor and desktop locks. The driver inherits this sender lock.
    let executor_path = root.join("executor.lock");
    let executor = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&executor_path)?;
    private(&executor_path, false)?;
    if unsafe { libc::flock(executor.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(invalid("desktop executor busy; abandonment retained"));
    }
    let lock_path = root.join("sender.lock");
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&lock_path)?;
    private(&lock_path, false)?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(invalid("desktop sender busy; abandonment retained"));
    }
    let mut db = open(root, true)?;
    let (conversation, request, status, result): (String, String, String, Option<String>) = db
        .query_row(
            "SELECT conversation_id,request_id,status,result FROM dispatch WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )?;
    let active: Option<i64> = db.query_row(
        "SELECT active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    if active.is_some() || status != "unknown" || result.as_deref() != Some("indeterminate") {
        return Err(invalid(
            "only an idle indeterminate dispatch can be reviewed",
        ));
    }
    let b = binding(&db, &conversation)?;
    let outbox = Connection::open_with_flags(
        b.state.join("outgoing.sqlite3"),
        OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let (evidence, finished_at): (String, Option<i64>) = outbox.query_row(
        "SELECT result_evidence,finished_at FROM outgoing_attempt WHERE request_id=?1 AND attempt_number=?2 AND status='indeterminate'",
        params![request, expected_attempt],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let (intent_status, intent_attempt, intent_evidence, kind, text):
        (String, i64, Option<String>, String, String) = outbox.query_row(
        "SELECT status,attempt_number,result_evidence,content_kind,text FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3",
        params![request,b.channel_id,b.session_id],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)),
    )?;
    if intent_status != "indeterminate"
        || intent_attempt != expected_attempt
        || intent_evidence.as_deref() != Some(evidence.as_str())
        || !matches!(kind.as_str(), "text" | "image")
        || kind == "image" && expected_attempt != 1
    {
        return Err(invalid(
            "outbox current attempt or content does not match review",
        ));
    }
    let identity = format!("{}\0{}", b.channel_id, request);
    let suffix = if expected_attempt == 1 {
        String::new()
    } else {
        format!("-a{expected_attempt}")
    };
    let tid = format!(
        "send-{}{}",
        &hex::encode(Sha256::digest(identity.as_bytes()))[..48],
        suffix
    );
    let evidence_path = root.join(format!("{tid}.client-result.json"));
    if Path::new(&evidence) != evidence_path {
        return Err(invalid(
            "attempt report is outside the canonical UI root or misnamed",
        ));
    }
    private(&evidence_path, false)?;
    if fs::metadata(&evidence_path)?.len() > 65536 {
        return Err(invalid("attempt report is too large"));
    }
    let report_bytes = fs::read(&evidence_path)?;
    let report: Value = serde_json::from_slice(&report_bytes)?;
    let focus_archive = kind == "text" && report["error"] == FOCUS_ARCHIVE_REPORT_ERROR;
    let stages = ["started", "inspected", "group_focused"];
    if report["request_id"] != request
        || report["id"] != tid
        || report["attempt_number"] != expected_attempt
        || report["channel_id"] != b.channel_id
        || report["session_id"] != b.session_id
        || report["status"] != "indeterminate"
        || (kind == "text" && !stages.contains(&report["stage"].as_str().unwrap_or("")))
        || (kind == "image" && report["stage"] != "inspected")
        || (kind == "image"
            && [
                "media_prepared",
                "media_submitted",
                "event_cursor",
                "focus_evidence_id",
                "focus_category",
                "recovery_from_attempt",
            ]
            .iter()
            .any(|key| report.get(key).is_some()))
    {
        return Err(invalid("attempt evidence does not prove a pre-submit stop"));
    }
    if focus_archive {
        let later_attempts: i64 = outbox.query_row(
            "SELECT COUNT(*) FROM outgoing_attempt WHERE request_id=?1 AND attempt_number>1",
            [&request],
            |r| r.get(0),
        )?;
        if expected_attempt != 1
            || finished_at.is_none()
            || later_attempts != 0
            || report["stage"] != "inspected"
            || b.control != "active"
            || [
                "focus_evidence_id",
                "focus_category",
                "event_cursor",
                "recovery_from_attempt",
                "late_observation",
                "lateObservation",
                "media_prepared",
                "media_submitted",
            ]
            .iter()
            .any(|key| report.get(key).is_some())
        {
            return Err(invalid(
                "focus archive requires the finished first attempt before prepare",
            ));
        }
    }
    let base_tid = tid.split("-a").next().unwrap_or(&tid);
    let later_tid = format!("{base_tid}-a{}", expected_attempt + 1);
    let focus_snapshot = if focus_archive {
        let snapshot = focus_archive_snapshot(root, &tid, &later_tid)?;
        check_native_focus_failure(root, &tid)?;
        Some(snapshot)
    } else {
        None
    };
    if kind == "image" {
        no_image_prepare_artifacts(root, &tid, &later_tid)?;
    } else if !focus_archive {
        no_submit_artifacts(root, &tid, &later_tid)?;
    }
    let channel = load_binding(&b.state)?;
    let payload = if focus_archive {
        serde_json::to_vec(&json!({
            "protocol_version":1,
            "focus_abandon_protocol_version":1,
            "content_kind":"text",
            "request_id":request,
            "attempt_number":1,
            "channel_id":b.channel_id,
            "session_id":b.session_id,
            "account":channel.account,
            "talker":channel.talker,
            "text":text,
        }))?
    } else if kind == "image" {
        serde_json::to_vec(&json!({
            "protocol_version":1,
            "abandon_protocol_version":1,
            "content_kind":"image",
            "request_id":request,
            "attempt_number":expected_attempt,
            "channel_id":b.channel_id,
            "session_id":b.session_id,
            "account":channel.account,
            "talker":channel.talker,
        }))?
    } else {
        serde_json::to_vec(&json!({
            "protocol_version":1,
            "request_id":request,
            "attempt_number":expected_attempt+1,
            "recovery_from_attempt":expected_attempt,
            "channel_id":b.channel_id,
            "session_id":b.session_id,
            "account":channel.account,
            "talker":channel.talker,
            "text":text,
        }))?
    };
    let driver = b.state.join("conversation-sender");
    let meta = fs::symlink_metadata(&driver)?;
    if !meta.is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.nlink() != 1
        || meta.permissions().mode() & 0o777 != 0o700
    {
        return Err(invalid(
            "registered sender driver is not private and executable",
        ));
    }
    if focus_archive {
        super::outgoing::check_focus_abandon_capabilities(&driver, Duration::from_secs(20))?;
        super::outgoing::check_focus_abandon_preflight(
            &driver,
            &payload,
            Duration::from_secs(25),
            lock.as_raw_fd(),
        )?;
        if focus_archive_snapshot(root, &tid, &later_tid)? != focus_snapshot.unwrap() {
            return Err(invalid(
                "native focus artifacts changed during driver proof",
            ));
        }
        check_native_focus_failure(root, &tid)?;
    } else if kind == "image" {
        super::outgoing::check_image_abandon_capabilities(&driver, Duration::from_secs(20))?;
        super::outgoing::check_image_abandon_preflight(
            &driver,
            &payload,
            Duration::from_secs(25),
            lock.as_raw_fd(),
        )?;
        no_image_prepare_artifacts(root, &tid, &later_tid)?;
    } else {
        super::outgoing::check_recovery_capabilities(&driver, Duration::from_secs(20))?;
        super::outgoing::check_recovery_preflight(
            &driver,
            &payload,
            Duration::from_secs(25),
            Some(lock.as_raw_fd()),
        )?;
        no_submit_artifacts(root, &tid, &later_tid)?;
    }
    if fs::read(&evidence_path)? != report_bytes {
        return Err(invalid("attempt report changed during driver proof"));
    }
    let current: (String, i64, Option<String>, String, String) = outbox.query_row(
        "SELECT status,attempt_number,result_evidence,content_kind,text FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3",
        params![request,b.channel_id,b.session_id],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)),
    )?;
    if current.0 != "indeterminate"
        || current.1 != expected_attempt
        || current.2.as_deref() != Some(evidence.as_str())
        || current.3 != kind
        || current.4 != text
    {
        return Err(invalid("outbox attempt changed during driver proof"));
    }
    if kind == "image" || focus_archive {
        let (attempt_evidence, latest_finished_at): (String, Option<i64>) = outbox.query_row(
            "SELECT result_evidence,finished_at FROM outgoing_attempt WHERE request_id=?1 AND attempt_number=?2 AND status='indeterminate'",
            params![request, expected_attempt],
            |r| Ok((r.get(0)?,r.get(1)?)),
        )?;
        if attempt_evidence != evidence || (focus_archive && latest_finished_at != finished_at) {
            return Err(invalid("attempt evidence changed during driver proof"));
        }
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if kind == "image" || focus_archive {
        let latest_identity: (String, String) = tx.query_row(
            "SELECT conversation_id,request_id FROM dispatch WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if latest_identity != (conversation.clone(), request.clone()) {
            return Err(invalid("scheduler identity changed during driver proof"));
        }
    }
    let latest: (String, Option<String>) = tx.query_row(
        "SELECT status,result FROM dispatch WHERE id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let active: Option<i64> = tx.query_row(
        "SELECT active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    let current_binding = binding(&tx, &conversation)?;
    if latest.0 != "unknown"
        || latest.1.as_deref() != Some("indeterminate")
        || active.is_some()
        || current_binding.epoch != b.epoch
        || current_binding.state != b.state
        || current_binding.channel_id != b.channel_id
        || current_binding.session_id != b.session_id
        || (focus_archive
            && (current_binding.control != "active" || current_binding.owner != b.owner))
    {
        return Err(invalid("scheduler or binding changed during driver proof"));
    }
    tx.execute(
        "UPDATE dispatch SET status='done',result='abandoned_pre_submit',updated_at=?2 WHERE id=?1",
        params![id, now()],
    )?;
    tx.execute(
        "INSERT INTO surface_review(reason,reviewed_at,unknown_count) VALUES(?1,?2,(SELECT COUNT(*) FROM dispatch WHERE status='unknown'))",
        params![reason, now()],
    )?;
    tx.commit()?;
    Ok(
        json!({"dispatch_id": id, "status": "done", "result": "abandoned_pre_submit", "outbox_status": "indeterminate", "attempt_number": expected_attempt}),
    )
}
// Independent of abandon-unsent: this recognizes one native IMAGE Copy
// rejection, not a general proof of unsent content or permission to replay.
fn image_rejection_read(path: &Path, limit: u64) -> Result<Vec<u8>, Box<dyn Error>> {
    private(path, false)?;
    if path.canonicalize()? != path {
        return Err(invalid("noncanonical image rejection evidence"));
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let before = file.metadata()?;
    if !before.is_file()
        || before.nlink() != 1
        || before.uid() != unsafe { libc::geteuid() }
        || before.permissions().mode() & 0o777 != 0o600
        || before.len() > limit
    {
        return Err(invalid("unsafe image rejection evidence"));
    }
    let mut bytes = Vec::new();
    (&file).take(limit + 1).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    if bytes.len() as u64 != before.len()
        || before.dev() != after.dev()
        || before.ino() != after.ino()
        || before.len() != after.len()
        || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || before.ctime() != after.ctime()
        || before.ctime_nsec() != after.ctime_nsec()
    {
        return Err(invalid("image rejection evidence changed while reading"));
    }
    Ok(bytes)
}
fn image_rejection_snapshot(
    root: &Path,
    tid: &str,
) -> Result<std::collections::BTreeMap<String, String>, Box<dyn Error>> {
    let mut snapshot = std::collections::BTreeMap::new();
    let mut total = 0;
    for (index, entry) in fs::read_dir(root)?.enumerate() {
        if index >= 10000 {
            return Err(invalid("image rejection directory exceeds bounded proof"));
        }
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with(&format!("{tid}.")) && !name.starts_with(&format!("{tid}-")) {
            continue;
        }
        if name.contains("submit")
            || name.starts_with(&format!("{tid}-a"))
            || name.contains("late-observed")
            || name.contains("recovery")
        {
            return Err(invalid(
                "submit, late observation or later attempt conflicts with Copy rejection",
            ));
        }
        let bytes = image_rejection_read(
            &entry.path(),
            if name.ends_with(".png") {
                16 * 1024 * 1024
            } else {
                65536
            },
        )?;
        total += bytes.len();
        if total > 128 * 1024 * 1024 {
            return Err(invalid("image rejection evidence exceeds bounded proof"));
        }
        snapshot.insert(name, hex::encode(Sha256::digest(&bytes)));
    }
    for suffix in [
        ".client-result.json",
        "-inspect.inspect.job.json",
        "-inspect.result.json",
        "-focus.focus-group.job.json",
        "-focus.result.json",
        "-focus.focus-evidence.json",
        ".prepare-visual.job.json",
        ".prepare-visual.result.json",
        ".visual-state.json",
    ] {
        if !snapshot.contains_key(&format!("{tid}{suffix}")) {
            return Err(invalid("incomplete original IMAGE Copy rejection trace"));
        }
    }
    Ok(snapshot)
}
fn known_image_copy_rejection(value: &Value) -> bool {
    let Some(error) = value.as_str().filter(|s| s.len() <= 2048) else {
        return false;
    };
    let Some(types) = error
        .strip_prefix("Composer Copy did not replace image sentinel exactly once (types:")
        .and_then(|s| s.strip_suffix(')'))
    else {
        return false;
    };
    // The Swift producer emits one ASCII space after the colon. Retain the
    // older zero-space spelling, but never trim arbitrary whitespace.
    let types = types.strip_prefix(' ').unwrap_or(types);
    let parts: Vec<&str> = types.split(',').collect();
    parts
        .iter()
        .any(|s| matches!(*s, "public.png" | "public.tiff"))
        && parts.iter().all(|s| {
            !s.is_empty()
                && s.as_bytes()[0].is_ascii_alphanumeric()
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.+-".contains(&b))
        })
}
fn archive_image_copy_rejection(
    root: &Path,
    id: i64,
    expected_attempt: i64,
    reason: &str,
) -> Result<Value, Box<dyn Error>> {
    if expected_attempt != 1 || reason.trim().len() < 8 || reason.len() > 512 {
        return Err(invalid(
            "IMAGE rejection archive requires original attempt 1 and bounded reason",
        ));
    }
    private(root, true)?;
    if root.canonicalize()? != root {
        return Err(invalid("IMAGE rejection root must be canonical"));
    }
    let mut locks = Vec::new();
    for name in ["executor.lock", "sender.lock"] {
        let path = root.join(name);
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(&path)?;
        private(&path, false)?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(invalid("desktop busy; IMAGE rejection retained"));
        }
        locks.push(lock);
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (conversation,request,status,result,operation,dispatch_channel,dispatch_epoch): (String,String,String,Option<String>,String,String,i64) = tx.query_row(
        "SELECT conversation_id,request_id,status,result,operation,channel_id,epoch FROM dispatch WHERE id=?1",[id],
        |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?)))?;
    let active: Option<i64> = tx.query_row(
        "SELECT active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    if status != "unknown"
        || result.as_deref() != Some("indeterminate")
        || operation != "send"
        || active.is_some()
    {
        return Err(invalid(
            "only an idle original indeterminate IMAGE dispatch may be archived",
        ));
    }
    let b = binding(&tx, &conversation)?;
    verify_binding(&b)?;
    if super::outgoing::managed_root(&b.state)?.as_deref() != Some(root) {
        return Err(invalid("original IMAGE managed root differs"));
    }
    if b.control != "active" || dispatch_channel != b.channel_id {
        return Err(invalid("IMAGE rejection binding is not active"));
    }
    private(&b.state.join("outgoing.sqlite3"), false)?;
    let mut outbox = Connection::open_with_flags(
        b.state.join("outgoing.sqlite3"),
        OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let out = outbox.transaction()?;
    let (evidence,media_path,media_sha,mime,size,owner,epoch): (String,String,String,String,i64,String,i64) = out.query_row(
        "SELECT result_evidence,media_path,media_sha256,media_mime_type,media_size_bytes,managed_owner,managed_epoch FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND content_kind='image' AND status='indeterminate' AND attempt_number=1",
        params![request,b.channel_id,b.session_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?)))?;
    if owner != b.owner || epoch != dispatch_epoch {
        return Err(invalid("original IMAGE owner/dispatch epoch differs"));
    }
    let attempts: i64 = out.query_row(
        "SELECT COUNT(*) FROM outgoing_attempt WHERE request_id=?1",
        [&request],
        |r| r.get(0),
    )?;
    let original: bool = out.query_row("SELECT EXISTS(SELECT 1 FROM outgoing_attempt WHERE request_id=?1 AND attempt_number=1 AND status='indeterminate' AND result_evidence=?2 AND finished_at IS NOT NULL AND finished_at>=claimed_at AND recovery_from_attempt IS NULL AND recovery_prior_status IS NULL)",params![request,evidence],|r|r.get(0))?;
    if attempts != 1 || !original {
        return Err(invalid(
            "IMAGE archive requires the finished original attempt",
        ));
    }
    let observations: bool = out.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='outgoing_observation')",[],|r|r.get(0))?;
    if observations {
        let observed: bool = out.query_row(
            "SELECT EXISTS(SELECT 1 FROM outgoing_observation WHERE request_id=?1)",
            [&request],
            |r| r.get(0),
        )?;
        if observed {
            return Err(invalid("late IMAGE observation already exists"));
        }
    }
    let media = super::outgoing_media::Media {
        path: PathBuf::from(media_path),
        sha256: media_sha,
        mime_type: match mime.as_str() {
            "image/png" => "image/png",
            "image/jpeg" => "image/jpeg",
            _ => return Err(invalid("invalid frozen IMAGE type")),
        },
        size_bytes: u64::try_from(size)?,
    };
    if media.path != super::outgoing_media::frozen_path(&b.state, &request)? {
        return Err(invalid("IMAGE frozen path differs"));
    }
    super::outgoing_media::verify(&media)?;
    let tid = format!(
        "send-{}",
        &hex::encode(Sha256::digest(
            format!("{}\0{}", b.channel_id, request).as_bytes()
        ))[..48]
    );
    let report_path = root.join(format!("{tid}.client-result.json"));
    if Path::new(&evidence) != report_path {
        return Err(invalid("original IMAGE report path differs"));
    }
    let before = image_rejection_snapshot(root, &tid)?;
    let report: Value = serde_json::from_slice(&image_rejection_read(&report_path, 65536)?)?;
    if report["id"] != tid
        || report["request_id"] != request
        || report["channel_id"] != b.channel_id
        || report["session_id"] != b.session_id
        || report["attempt_number"] != 1
        || report["status"] != "indeterminate"
        || report["stage"] != "group_focused"
        || [
            "media_prepared",
            "media_submitted",
            "event_cursor",
            "recovery_from_attempt",
        ]
        .iter()
        .any(|k| report.get(*k).is_some())
    {
        return Err(invalid(
            "original IMAGE report is not a prepare Copy rejection",
        ));
    }
    let prep: Value = serde_json::from_slice(&image_rejection_read(
        &root.join(format!("{tid}.prepare-visual.result.json")),
        65536,
    )?)?;
    let visual: Value = serde_json::from_slice(&image_rejection_read(
        &root.join(format!("{tid}.visual-state.json")),
        65536,
    )?)?;
    if prep["id"] != tid
        || prep["ok"] != false
        || !known_image_copy_rejection(&prep["error"])
        || visual["id"] != tid
        || visual["stage"] != "prepare-attempted"
        || visual["media"] != media.descriptor()
        || [
            "preparedPixelSHA256",
            "preparedCopyType",
            "media_prepared",
            "media_submitted",
            "event_cursor",
        ]
        .iter()
        .any(|k| visual.get(*k).is_some())
    {
        return Err(invalid("native IMAGE prepare rejection differs"));
    }
    let channel = load_binding(&b.state)?;
    let payload = serde_json::to_vec(
        &json!({"protocol_version":1,"image_rejection_protocol_version":1,
        "content_kind":"image","request_id":request,"attempt_number":1,"channel_id":b.channel_id,
        "session_id":b.session_id,"account":channel.account,"talker":channel.talker,"media":media.descriptor()}),
    )?;
    let driver = b.state.join("conversation-sender");
    private(
        driver
            .parent()
            .ok_or_else(|| invalid("missing driver parent"))?,
        true,
    )?;
    let meta = fs::symlink_metadata(&driver)?;
    if !meta.is_file()
        || meta.nlink() != 1
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o777 != 0o700
    {
        return Err(invalid("registered IMAGE archive driver is not private"));
    }
    super::outgoing::check_image_rejection_capabilities(&driver, Duration::from_secs(20))?;
    super::outgoing::check_image_rejection_preflight(
        &driver,
        &payload,
        Duration::from_secs(25),
        locks[1].as_raw_fd(),
    )?;
    if image_rejection_snapshot(root, &tid)? != before {
        return Err(invalid("IMAGE rejection artifacts changed during proof"));
    }
    super::outgoing_media::verify(&media)?;
    verify_binding(&b)?;
    if super::outgoing::managed_root(&b.state)?.as_deref() != Some(root) {
        return Err(invalid("original IMAGE managed root changed during proof"));
    }
    // Both database reservations and shared desktop locks remain held. No
    // outbox/attempt write, epoch change, pause clearing or retry is permitted.
    let updated = tx.execute("UPDATE dispatch SET status='done',result='image_prepare_rejected',updated_at=?2 WHERE id=?1 AND status='unknown' AND result='indeterminate'",params![id,now()])?;
    if updated != 1 {
        return Err(invalid("IMAGE rejection dispatch CAS failed"));
    }
    tx.execute("INSERT INTO surface_review(reason,reviewed_at,unknown_count) VALUES(?1,?2,(SELECT COUNT(*) FROM dispatch WHERE status='unknown'))",
        params![format!("image_prepare_rejected: {reason}"),now()])?;
    block_unsent_batch_descendants(&tx)?;
    out.commit()?;
    tx.commit()?;
    Ok(
        json!({"dispatch_id":id,"status":"done","result":"image_prepare_rejected",
        "outbox_status":"indeterminate","attempt_number":1}),
    )
}
const TEXT_BUTTON_REJECTION_ERROR: &str =
    "Expected exactly one visible Send label in lower conversation pane";

fn text_button_rejection_snapshot(
    root: &Path,
    tid: &str,
) -> Result<std::collections::BTreeMap<String, String>, Box<dyn Error>> {
    text_rejection_snapshot(root, tid, false)
}

fn text_rejection_snapshot(
    root: &Path,
    tid: &str,
    prepare_header_only: bool,
) -> Result<std::collections::BTreeMap<String, String>, Box<dyn Error>> {
    let mut required = vec![
        ".client-result.json",
        "-inspect.inspect.job.json",
        "-inspect.result.json",
        "-focus.focus-group.job.json",
        "-focus.result.json",
        "-focus.focus-evidence.json",
        ".prepare-visual.job.json",
        ".prepare-visual.result.json",
        ".visual-state.json",
    ];
    if prepare_header_only {
        required.extend([
            "-focus.focus-attempted.json",
            "-focus.focus-initial.png",
            "-focus.current-header-confirmed.png",
            ".prepare-start.png",
            ".old-draft-before-click.png",
            ".old-draft-before-select.png",
            ".old-draft-before-copy.png",
            ".old-draft-copy-ready.png",
            ".old-draft-empty-check.png",
            ".replace-draft-before-paste.png",
            ".new-draft-before-click.png",
        ]);
    } else {
        required.extend([".submit-visual.job.json", ".submit-visual.result.json"]);
    }
    let focus = [
        "focus-attempted.json",
        "focus-polls.json",
        "before-search-click.png",
        "before-search-select.png",
        "before-search-paste.png",
        "before-search-copy.png",
        "search-copy-ready.png",
        "focus-initial.png",
        "focus-search-result-main.png",
        "focus-search-result-popup.png",
        "focus-search-result-confirm.png",
        "focus-search-before-click-main.png",
        "focus-search-before-click-popup.png",
        "focus-search-before-click-confirm.png",
        "focus-search-after-click.png",
    ];
    let visual = [
        "prepare-start",
        "old-draft-before-click",
        "old-draft-before-select",
        "old-draft-before-copy",
        "old-draft-copy-ready",
        "old-draft-empty-check",
        "old-draft-empty-check-1",
        "old-draft-empty-check-2",
        "replace-draft-before-paste",
        "new-draft-before-click",
        "new-draft-before-select",
        "new-draft-before-copy",
        "new-draft-copy-ready",
        "submit-draft-before-click",
        "submit-draft-before-select",
        "submit-draft-before-copy",
        "submit-draft-copy-ready",
        "before-submit",
    ];
    let mut allowed = std::collections::BTreeSet::new();
    for suffix in &required {
        allowed.insert(format!("{tid}{suffix}"));
    }
    for suffix in focus {
        allowed.insert(format!("{tid}-focus.{suffix}"));
    }
    if prepare_header_only {
        allowed.insert(format!("{tid}-focus.current-header-confirmed.png"));
    }
    for suffix in visual {
        if prepare_header_only
            && matches!(
                suffix,
                "new-draft-before-select"
                    | "new-draft-before-copy"
                    | "new-draft-copy-ready"
                    | "submit-draft-before-click"
                    | "submit-draft-before-select"
                    | "submit-draft-before-copy"
                    | "submit-draft-copy-ready"
                    | "before-submit"
            )
        {
            continue;
        }
        allowed.insert(format!("{tid}.{suffix}.png"));
    }
    let mut snapshot = std::collections::BTreeMap::new();
    let mut total = 0;
    for (index, entry) in fs::read_dir(root)?.enumerate() {
        if index >= 10000 {
            return Err(invalid("TEXT rejection directory exceeds bounded proof"));
        }
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with(&format!("{tid}.")) && !name.starts_with(&format!("{tid}-")) {
            continue;
        }
        if !allowed.contains(&name) {
            return Err(invalid(
                "submit marker, later/extra attempt or conflicting TEXT artifact exists",
            ));
        }
        let bytes = image_rejection_read(
            &entry.path(),
            if name.ends_with(".png") {
                16 * 1024 * 1024
            } else {
                65536
            },
        )?;
        total += bytes.len();
        if total > 128 * 1024 * 1024 {
            return Err(invalid("TEXT rejection evidence exceeds bounded proof"));
        }
        snapshot.insert(name, hex::encode(Sha256::digest(&bytes)));
    }
    if required
        .iter()
        .any(|suffix| !snapshot.contains_key(&format!("{tid}{suffix}")))
    {
        return Err(invalid(
            "incomplete original prepared TEXT button rejection trace",
        ));
    }
    Ok(snapshot)
}

fn archive_text_button_rejection(
    root: &Path,
    id: i64,
    expected_attempt: i64,
    reason: &str,
) -> Result<Value, Box<dyn Error>> {
    if expected_attempt != 1 || reason.trim().len() < 8 || reason.len() > 512 {
        return Err(invalid(
            "TEXT rejection archive requires original attempt 1 and bounded reason",
        ));
    }
    private(root, true)?;
    if root.canonicalize()? != root {
        return Err(invalid("TEXT rejection root must be canonical"));
    }
    let mut locks = Vec::new();
    for name in ["executor.lock", "sender.lock"] {
        let path = root.join(name);
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(&path)?;
        private(&path, false)?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(invalid("desktop busy; TEXT rejection retained"));
        }
        locks.push(lock);
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (conversation,request,status,result,operation,dispatch_channel,dispatch_epoch): (String,String,String,Option<String>,String,String,i64) = tx.query_row(
        "SELECT conversation_id,request_id,status,result,operation,channel_id,epoch FROM dispatch WHERE id=?1",[id],
        |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?)))?;
    let active: Option<i64> = tx.query_row(
        "SELECT active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    if status != "unknown"
        || result.as_deref() != Some("indeterminate")
        || operation != "send"
        || active.is_some()
    {
        return Err(invalid(
            "only an idle original indeterminate TEXT dispatch may be archived",
        ));
    }
    let b = binding(&tx, &conversation)?;
    verify_binding(&b)?;
    if super::outgoing::managed_root(&b.state)?.as_deref() != Some(root) {
        return Err(invalid("original TEXT managed root differs"));
    }
    if b.control != "active" || dispatch_channel != b.channel_id {
        return Err(invalid("TEXT rejection binding is not active"));
    }
    private(&b.state.join("outgoing.sqlite3"), false)?;
    let mut outbox = Connection::open_with_flags(
        b.state.join("outgoing.sqlite3"),
        OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let out = outbox.transaction()?;
    let (evidence,text,owner,epoch): (String,String,String,i64) = out.query_row(
        "SELECT result_evidence,text,managed_owner,managed_epoch FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND content_kind='text' AND status='indeterminate' AND attempt_number=1",
        params![request,b.channel_id,b.session_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
    if owner != b.owner || epoch != dispatch_epoch {
        return Err(invalid("original TEXT owner/dispatch epoch differs"));
    }
    let attempts: i64 = out.query_row(
        "SELECT COUNT(*) FROM outgoing_attempt WHERE request_id=?1",
        [&request],
        |r| r.get(0),
    )?;
    let original: bool = out.query_row("SELECT EXISTS(SELECT 1 FROM outgoing_attempt WHERE request_id=?1 AND attempt_number=1 AND status='indeterminate' AND result_evidence=?2 AND finished_at IS NOT NULL AND finished_at>=claimed_at AND recovery_from_attempt IS NULL AND recovery_prior_status IS NULL)",params![request,evidence],|r|r.get(0))?;
    if attempts != 1 || !original {
        return Err(invalid(
            "TEXT archive requires the finished original attempt",
        ));
    }
    let observations: bool = out.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='outgoing_observation')",[],|r|r.get(0))?;
    if observations {
        let observed: bool = out.query_row(
            "SELECT EXISTS(SELECT 1 FROM outgoing_observation WHERE request_id=?1)",
            [&request],
            |r| r.get(0),
        )?;
        if observed {
            return Err(invalid("late TEXT observation already exists"));
        }
    }
    let tid = format!(
        "send-{}",
        &hex::encode(Sha256::digest(
            format!("{}\0{}", b.channel_id, request).as_bytes()
        ))[..48]
    );
    let report_path = root.join(format!("{tid}.client-result.json"));
    if Path::new(&evidence) != report_path {
        return Err(invalid("original TEXT report path differs"));
    }
    let before = text_button_rejection_snapshot(root, &tid)?;
    let report: Value = serde_json::from_slice(&image_rejection_read(&report_path, 65536)?)?;
    if report["id"] != tid
        || report["request_id"] != request
        || report["channel_id"] != b.channel_id
        || report["session_id"] != b.session_id
        || report["attempt_number"] != 1
        || report["status"] != "indeterminate"
        || report["stage"] != "prepared"
        || [
            "media_prepared",
            "media_submitted",
            "event_cursor",
            "recovery_from_attempt",
            "observation",
            "ui_submit_seconds",
            "submit_action_seconds",
            "verification_seconds",
        ]
        .iter()
        .any(|k| report.get(*k).is_some())
    {
        return Err(invalid(
            "original TEXT report is not a prepared button rejection",
        ));
    }
    let read_json = |suffix: &str| -> Result<Value, Box<dyn Error>> {
        Ok(serde_json::from_slice(&image_rejection_read(
            &root.join(format!("{tid}{suffix}")),
            65536,
        )?)?)
    };
    let prep = read_json(".prepare-visual.result.json")?;
    let rejected = read_json(".submit-visual.result.json")?;
    let visual = read_json(".visual-state.json")?;
    let prepare_job = read_json(".prepare-visual.job.json")?;
    let submit_job = read_json(".submit-visual.job.json")?;
    let permissions = |value: &Value| {
        ["accessibility", "screenRecording", "postEvents"]
            .iter()
            .all(|key| value["permissions"][*key] == true)
    };
    if prep["id"] != tid
        || prep["action"] != "prepare-visual"
        || prep["ok"] != true
        || prep["prepared"] != true
        || !permissions(&prep)
        || rejected.as_object().is_none_or(|o| o.len() != 4)
        || rejected["id"] != tid
        || rejected["ok"] != false
        || rejected["error"] != TEXT_BUTTON_REJECTION_ERROR
        || !permissions(&rejected)
        || report["error"] != "submit outcome uncertain: Send button not confirmed"
        || visual["id"] != tid
        || visual["stage"] != "prepared"
        || visual["text"] != text
        || prepare_job["id"] != tid
        || prepare_job["action"] != "prepare-visual"
        || prepare_job["text"] != text
        || submit_job["id"] != tid
        || submit_job["action"] != "submit-visual"
        || submit_job["text"] != text
        || [
            "media",
            "preparedPixelSHA256",
            "preparedCopyType",
            "media_prepared",
            "media_submitted",
            "event_cursor",
            "submitted",
        ]
        .iter()
        .any(|key| visual.get(*key).is_some())
        || [
            "media_prepared",
            "media_submitted",
            "submitted",
            "event_cursor",
            "error",
        ]
        .iter()
        .any(|key| prep.get(*key).is_some())
    {
        return Err(invalid(
            "native TEXT proof is not the exact prepared first-button rejection",
        ));
    }
    let mut expected_submit = prepare_job.clone();
    expected_submit["action"] = json!("submit-visual");
    let mut expected_visual = prepare_job.clone();
    expected_visual
        .as_object_mut()
        .ok_or_else(|| invalid("invalid TEXT prepare job"))?
        .remove("action");
    expected_visual["stage"] = json!("prepared");
    if expected_submit != submit_job || expected_visual != visual {
        return Err(invalid(
            "TEXT jobs, frozen body or prepared native state conflict",
        ));
    }
    let channel = load_binding(&b.state)?;
    let payload = serde_json::to_vec(
        &json!({"protocol_version":1,"text_button_rejection_protocol_version":1,
        "content_kind":"text","request_id":request,"attempt_number":1,"channel_id":b.channel_id,
        "session_id":b.session_id,"account":channel.account,"talker":channel.talker,"text":text}),
    )?;
    let driver = b.state.join("conversation-sender");
    private(
        driver
            .parent()
            .ok_or_else(|| invalid("missing driver parent"))?,
        true,
    )?;
    let meta = fs::symlink_metadata(&driver)?;
    if !meta.is_file()
        || meta.nlink() != 1
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o777 != 0o700
    {
        return Err(invalid("registered TEXT archive driver is not private"));
    }
    super::outgoing::check_text_button_rejection_capabilities(&driver, Duration::from_secs(20))?;
    super::outgoing::check_text_button_rejection_preflight(
        &driver,
        &payload,
        Duration::from_secs(25),
        locks[1].as_raw_fd(),
    )?;
    if text_button_rejection_snapshot(root, &tid)? != before {
        return Err(invalid("TEXT rejection artifacts changed during proof"));
    }
    verify_binding(&b)?;
    if super::outgoing::managed_root(&b.state)?.as_deref() != Some(root) {
        return Err(invalid("original TEXT managed root changed during proof"));
    }
    // Both database reservations and shared desktop locks remain held. No
    // outbox/attempt write, epoch change, pause clearing or retry is permitted.
    let updated = tx.execute("UPDATE dispatch SET status='done',result='text_button_guard_rejected',updated_at=?2 WHERE id=?1 AND status='unknown' AND result='indeterminate'",params![id,now()])?;
    if updated != 1 {
        return Err(invalid("TEXT rejection dispatch CAS failed"));
    }
    tx.execute("INSERT INTO surface_review(reason,reviewed_at,unknown_count) VALUES(?1,?2,(SELECT COUNT(*) FROM dispatch WHERE status='unknown'))",
        params![format!("text_button_guard_rejected: {reason}"),now()])?;
    block_unsent_batch_descendants(&tx)?;
    out.commit()?;
    tx.commit()?;
    Ok(
        json!({"dispatch_id":id,"status":"done","result":"text_button_guard_rejected",
        "outbox_status":"indeterminate","attempt_number":1}),
    )
}
pub(crate) fn pause_surface(root: &Path, reason: &str) -> Result<(), Box<dyn Error>> {
    surface_pause(root, Some(reason))?;
    Ok(())
}
fn surface_review(root: &Path, reason: &str) -> Result<Value, Box<dyn Error>> {
    if reason.trim().len() < 8 || reason.len() > 512 {
        return Err(invalid("review reason must be 8..512 bytes"));
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (paused, active): (Option<String>, Option<i64>) = tx.query_row(
        "SELECT paused_reason,active_dispatch FROM surface WHERE singleton=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if paused.is_none() || active.is_some() {
        return Err(invalid("surface must be paused without active dispatch"));
    }
    let unknown: i64 = tx.query_row(
        "SELECT COUNT(*) FROM dispatch WHERE status='unknown'",
        [],
        |r| r.get(0),
    )?;
    tx.execute(
        "INSERT INTO surface_review(reason,reviewed_at,unknown_count) VALUES(?1,?2,?3)",
        params![reason, now(), unknown],
    )?;
    tx.execute(
        "UPDATE surface SET paused_reason=NULL WHERE singleton=1",
        [],
    )?;
    tx.commit()?;
    Ok(json!({"status":"ready","unknown_dispatches_retained":unknown}))
}

fn inbox_reconcile(root: &Path, id: &str) -> Result<Value, Box<dyn Error>> {
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let b = binding(&tx, id)?;
    verify_binding(&b)?;
    let latest = channel_max(&b.state)?;
    if latest < b.watermark {
        return Err(invalid("channel event log regressed"));
    }
    let path = b.state.join("channel.sqlite3");
    private(&path, false)?;
    let source = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let self_id: Option<String> = source.query_row(
        "SELECT self_wxid FROM channel_binding WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    let self_id = self_id
        .filter(|s| !s.is_empty())
        .ok_or_else(|| invalid("trusted self identity is required before inbox reconciliation"))?;
    let mut stmt=source.prepare("SELECT cursor,kind,server_id,message_json FROM channel_events WHERE cursor>?1 AND cursor<=?2 ORDER BY cursor")?;
    let events = stmt
        .query_map(params![b.watermark, latest], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, i64>(2)?,
                r.get::<_, String>(3)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut created = 0;
    let mut upto = latest;
    let mut effective_epoch = b.epoch;
    let mut manual_pause = false;
    let mut batch_start = None;
    let mut batch_end = 0;
    let mut batch_count = 0;
    let mut batch_refs = Vec::<Value>::new();
    for (cursor, kind, server_id, raw) in events {
        let message: Value = serde_json::from_str(&raw)?;
        let sender = message["sender"].as_str().unwrap_or("");
        if sender.is_empty() {
            return Err(invalid(
                "source sender identity is unresolved; watermark retained",
            ));
        }
        // Unknown direction is withheld rather than routed to an agent.
        let incoming = !sender.is_empty() && sender != self_id;
        if sender == self_id {
            match outgoing_origin(root, &b, &source, server_id)? {
                OutgoingOrigin::Pending => {
                    upto = cursor - 1;
                    break;
                }
                OutgoingOrigin::Manual if b.control == "active" && !manual_pause => {
                    effective_epoch += 1;
                    manual_pause = true;
                    tx.execute(
                        "UPDATE binding SET control='taken_over',epoch=?2 WHERE conversation_id=?1",
                        params![id, effective_epoch],
                    )?;
                    tx.execute("UPDATE dispatch SET status='blocked',result='possible_manual_outgoing',updated_at=?2 WHERE conversation_id=?1 AND status='queued'",params![id,now()])?;
                    tx.execute("UPDATE delivery SET status=CASE WHEN status='claimed' THEN 'pending' ELSE status END,token=NULL,lease_until=NULL,epoch=?2,updated_at=?3 WHERE conversation_id=?1 AND status IN ('pending','claimed','deferred','draft') AND turn_state IS NULL",params![id,effective_epoch,now()])?;
                    tx.execute("INSERT INTO control_event(conversation_id,epoch,action,reason,recorded_at) VALUES(?1,?2,'possible_manual_outgoing','unassociated self outgoing observed',?3)",params![id,effective_epoch,now()])?;
                }
                _ => {}
            }
        }
        if matches!(kind.as_str(), "message_observed" | "message_updated") && incoming {
            batch_start.get_or_insert(cursor);
            batch_end = cursor;
            batch_count += 1;
            if message["msg_type"].as_u64() == Some(3) {
                batch_refs.push(json!({"cursor":cursor,"server_id":server_id,"msg_type":3,"availability":"unresolved"}));
            }
        }
        if (!incoming || batch_count >= 256) && batch_start.is_some() {
            created += materialize_batch(
                &tx,
                &b,
                id,
                effective_epoch,
                batch_start.take().expect("checked batch"),
                batch_end,
                &batch_refs,
            )?;
            batch_count = 0;
            batch_refs.clear();
        }
    }
    if let Some(start) = batch_start {
        created += materialize_batch(&tx, &b, id, effective_epoch, start, batch_end, &batch_refs)?;
    }
    // Only deferred work opts in to timer/new-information resumption. Drafts
    // always wait for an explicit approval action and are never auto-sent.
    let mut todo=tx.prepare("SELECT id FROM delivery WHERE conversation_id=?1 AND status='deferred' AND (turn_state IS NULL OR turn_state IN ('completed','interrupted','failed')) AND ((due_at IS NOT NULL AND due_at<=?2) OR (resume_condition='new_information' AND cursor_end<?3))")?;
    let ready = todo
        .query_map(
            params![id, now(), if created > 0 { upto } else { 0 }],
            |r| r.get::<_, i64>(0),
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    drop(todo);
    for delivery in ready {
        tx.execute("INSERT INTO delivery_turn_history(delivery_id,turn_state,owner,epoch,session_ref,thread_id,turn_id,archived_at) SELECT id,turn_state,owner,epoch,session_ref,thread_id,turn_id,?2 FROM delivery WHERE id=?1 AND turn_state IS NOT NULL",params![delivery,now()])?;
        tx.execute("UPDATE delivery SET status='pending',owner=?2,epoch=?3,token=NULL,lease_until=NULL,turn_state=NULL,transport_token=NULL,session_ref=NULL,thread_id=NULL,turn_id=NULL,due_at=NULL,updated_at=?4 WHERE id=?1",params![delivery,b.owner,effective_epoch,now()])?;
    }
    tx.execute(
        "UPDATE binding SET watermark=?2 WHERE conversation_id=?1",
        params![id, upto],
    )?;
    advance_processed(&tx, id)?;
    tx.commit()?;
    Ok(
        json!({"conversation_id":id,"observed_cursor":latest,"watermark":upto,"created":created,"possible_manual_outgoing":manual_pause}),
    )
}

fn materialize_batch(
    tx: &Transaction<'_>,
    b: &Binding,
    id: &str,
    epoch: i64,
    start: i64,
    end: i64,
    refs: &[Value],
) -> Result<usize, Box<dyn Error>> {
    Ok(tx.execute("INSERT OR IGNORE INTO delivery(conversation_id,cursor_start,cursor_end,owner,epoch,status,attachment_refs,created_at,updated_at) VALUES(?1,?2,?3,?4,?5,'pending',?6,?7,?7)",params![id,start,end,b.owner,epoch,serde_json::to_string(refs)?,now()])?)
}

enum OutgoingOrigin {
    Known,
    Pending,
    Manual,
}
fn outgoing_origin(
    root: &Path,
    b: &Binding,
    source: &Connection,
    server_id: i64,
) -> Result<OutgoingOrigin, Box<dyn Error>> {
    let path = b.state.join("outgoing.sqlite3");
    private(&path, false)?;
    let out = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let mut rows=out.prepare("SELECT request_id,status,result_evidence,attempt_number,managed_owner FROM outgoing_intent WHERE channel_id=?1 AND session_id=?2 AND attempt_number>0")?;
    let intents = rows
        .query_map(params![b.channel_id, b.session_id], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, Option<String>>(4)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut pending = false;
    for (request, status, evidence, attempt, owner) in intents {
        pending |= status == "attempt_recorded";
        if !matches!(status.as_str(), "ui_observed" | "recipient_confirmed") {
            continue;
        }
        let Some(evidence) = evidence else {
            continue;
        };
        let path = PathBuf::from(evidence);
        if path.parent() != Some(root)
            || private(&path, false).is_err()
            || fs::metadata(&path)?.len() > 65536
        {
            continue;
        }
        let Ok(report) = serde_json::from_slice::<Value>(&fs::read(path)?) else {
            continue;
        };
        if report["request_id"] != request || report["status"] != "ui_observed" {
            continue;
        }
        let identity = format!("{}\0{}", b.channel_id, request);
        let qualified = format!(
            "send-{}",
            &hex::encode(Sha256::digest(identity.as_bytes()))[..48]
        );
        let legacy = format!(
            "send-{}",
            &hex::encode(Sha256::digest(request.as_bytes()))[..48]
        );
        let suffix = if attempt == 1 {
            String::new()
        } else {
            format!("-a{attempt}")
        };
        let id = report["id"].as_str().unwrap_or("");
        if report["attempt_number"].as_i64() != Some(attempt)
            || (id != format!("{qualified}{suffix}")
                && !(owner.is_none() && id == format!("{legacy}{suffix}")))
        {
            continue;
        }
        if owner.is_some()
            && (report["channel_id"] != b.channel_id || report["session_id"] != b.session_id)
        {
            continue;
        }
        let Some(cursor) = report["event_cursor"].as_i64() else {
            continue;
        };
        let observed: Option<i64> = source
            .query_row(
                "SELECT server_id FROM channel_events WHERE cursor=?1",
                [cursor],
                |r| r.get(0),
            )
            .optional()?;
        if observed == Some(server_id) {
            return Ok(OutgoingOrigin::Known);
        }
    }
    Ok(if pending {
        OutgoingOrigin::Pending
    } else {
        OutgoingOrigin::Manual
    })
}
fn advance_processed(db: &Connection, id: &str) -> Result<(), Box<dyn Error>> {
    let (watermark, processed): (i64, i64) = db.query_row(
        "SELECT watermark,processed_cursor FROM binding WHERE conversation_id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let first_pending:Option<i64>=db.query_row("SELECT MIN(cursor_start) FROM delivery WHERE conversation_id=?1 AND status IN ('pending','claimed')",[id],|r|r.get(0))?;
    let next = first_pending.map_or(watermark, |c| c - 1).max(processed);
    db.execute(
        "UPDATE binding SET processed_cursor=?2 WHERE conversation_id=?1",
        params![id, next],
    )?;
    Ok(())
}
fn attachment_refs(db: &Connection, id: i64) -> Result<Value, Box<dyn Error>> {
    let raw: String = db.query_row(
        "SELECT attachment_refs FROM delivery WHERE id=?1",
        [id],
        |r| r.get(0),
    )?;
    Ok(serde_json::from_str(&raw)?)
}

#[cfg(test)]
fn inbox_claim(
    root: &Path,
    id: &str,
    owner: &str,
    lease: i64,
    expected_epoch: Option<i64>,
) -> Result<Value, Box<dyn Error>> {
    inbox_claim_for_session(root, id, owner, lease, expected_epoch, None)
}

fn inbox_claim_for_session(
    root: &Path,
    id: &str,
    owner: &str,
    lease: i64,
    expected_epoch: Option<i64>,
    session: Option<&str>,
) -> Result<Value, Box<dyn Error>> {
    check_id(owner)?;
    if !(30..=3600).contains(&lease) {
        return Err(invalid("inbox lease must be 30..3600 seconds"));
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let b = binding(&tx, id)?;
    verify_binding(&b)?;
    if b.owner != owner
        || b.control != "active"
        || expected_epoch.is_some_and(|expected| expected != b.epoch)
    {
        return Err(invalid("owner or control state changed"));
    }
    if let Some(session) = session {
        check_id(session)?;
        if b.session_ref
            .as_deref()
            .is_some_and(|saved| saved != session)
        {
            return Err(invalid("notification Session binding changed"));
        }
        let shared = super::shared_session_scope::allowed(&tx, session, id, &b.owner, b.epoch)?;
        let dedicated: Option<String> = tx
            .query_row(
                "SELECT conversation_id FROM session_scope WHERE session_ref=?1",
                [session],
                |r| r.get(0),
            )
            .optional()?;
        if !shared
            && (super::shared_session_scope::has_history(&tx, session)?
                || dedicated.as_deref().is_some_and(|old| old != id))
        {
            return Err(invalid("shared Session grant is absent or stale"));
        }
        let other: bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM delivery WHERE session_ref=?1 AND conversation_id<>?2 AND (turn_state IN ('reserved','recorded') OR (status='claimed' AND turn_state IS NULL AND lease_until>?3)))",
            params![session,id,now()],|r|r.get(0))?;
        if other {
            return Ok(json!({"status":"session_busy"}));
        }
    }
    let row:Option<(i64,i64,i64,String,Option<i64>)>=tx.query_row("SELECT d.id,d.cursor_start,d.cursor_end,d.status,d.lease_until FROM delivery d WHERE d.conversation_id=?1 AND d.owner=?2 AND d.epoch=?3 AND d.turn_state IS NULL AND (d.status='pending' OR (d.status='claimed' AND (d.lease_until<=?4 OR (?5 IS NOT NULL AND d.session_ref=?5)))) AND NOT EXISTS (SELECT 1 FROM delivery earlier WHERE earlier.conversation_id=d.conversation_id AND earlier.id<d.id AND earlier.status IN ('pending','claimed')) ORDER BY d.id LIMIT 1",params![id,owner,b.epoch,now(),session],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let Some((delivery, start, end, status, until)) = row else {
        return Ok(json!({"status":"empty"}));
    };
    let token = if status == "claimed" && until.unwrap_or(0) > now() && session.is_some() {
        tx.query_row("SELECT token FROM delivery WHERE id=?1", [delivery], |r| {
            r.get::<_, String>(0)
        })?
    } else {
        let token = token()?;
        tx.execute("UPDATE delivery SET status='claimed',token=?2,lease_until=?3,updated_at=?4,session_ref=?5 WHERE id=?1",
            params![delivery, token, now() + lease, now(),session])?;
        token
    };
    tx.commit()?;
    let refs = attachment_refs(&db, delivery)?;
    Ok(
        json!({"status":"claimed","delivery_id":delivery,"token":token,"conversation_id":id,"owner":owner,"epoch":b.epoch,"cursor_start":start,"cursor_end":end,"attachment_refs":refs,"channel_state_dir":b.state}),
    )
}

fn inbox_complete(
    root: &Path,
    id: i64,
    token: &str,
    input: DecisionInput<'_>,
) -> Result<Value, Box<dyn Error>> {
    inbox_complete_mode(root, id, token, input, None)
}

/// Observe-only completion is intentionally distinct from the original
/// unattempted-intent contract, which existing callers continue to use.
fn inbox_complete_mode(
    root: &Path,
    id: i64,
    token: &str,
    input: DecisionInput<'_>,
    observed_publisher: Option<&str>,
) -> Result<Value, Box<dyn Error>> {
    let DecisionInput {
        decision,
        request,
        draft,
        reason,
        condition,
        due_at,
    } = input;
    if matches!(decision, Decision::Reply) != request.is_some() {
        return Err(invalid(
            "reply requires request ID; other decisions cannot carry it",
        ));
    }
    if matches!(decision, Decision::Draft) != draft.is_some() {
        return Err(invalid("draft requires --draft-stdin only"));
    }
    if let Some(text) = draft {
        if text.trim().is_empty() || text.len() > 65536 || text.contains('\0') {
            return Err(invalid(
                "draft must be nonempty UTF-8 at most 64 KiB without NUL",
            ));
        }
    }
    if matches!(decision, Decision::Defer | Decision::Draft) {
        if reason.is_none_or(|r| r.trim().is_empty() || r.len() > 512) {
            return Err(invalid("defer/draft requires a reason of 1..512 bytes"));
        }
    } else if condition.is_some() || due_at.is_some() {
        return Err(invalid("resume conditions apply only to defer/draft"));
    }
    if reason.is_some_and(|r| r.trim().is_empty() || r.len() > 512) {
        return Err(invalid("reason must be 1..512 bytes"));
    }
    if condition.is_some_and(|c| c.len() > 512) || due_at.is_some_and(|t| t < 0) {
        return Err(invalid("invalid resume condition or due time"));
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let row:Option<DeliveryCompletionRow>=tx.query_row("SELECT conversation_id,owner,epoch,token,lease_until,turn_state FROM delivery WHERE id=?1 AND status='claimed'",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((conversation, owner, epoch, saved, until, turn_state)) = row else {
        return Err(invalid("delivery is not claimed"));
    };
    let b = binding(&tx, &conversation)?;
    if saved != token
        || (until.unwrap_or(0) <= now() && turn_state.is_none())
        || b.owner != owner
        || b.epoch != epoch
        || b.control != "active"
    {
        return Err(invalid("delivery claim expired or owner epoch changed"));
    }
    if let Some(rid) = request {
        if b.reply_policy != "authorized" {
            return Err(invalid("reply policy does not authorize reply"));
        }
        if let Some(publisher) = observed_publisher {
            observed_reply_intent(&b, &conversation, id, publisher, rid)?;
        } else if !intent(&b, rid)?.ready_for(&b) {
            return Err(invalid("reply intent does not match owner epoch"));
        }
    }
    let status = match decision {
        Decision::Reply => "replied",
        Decision::Skip => "skipped",
        Decision::Defer => "deferred",
        Decision::Draft => "draft",
    };
    tx.execute("INSERT INTO delivery_decision_history(delivery_id,owner,epoch,decision,request_id,draft_text,reason,resume_condition,due_at,recorded_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",params![id,b.owner,b.epoch,decision.as_str(),request,draft,reason,condition,due_at,now()])?;
    tx.execute("UPDATE delivery SET status=?2,decision_request_id=?3,draft_text=?4,reason=?5,resume_condition=?6,due_at=?7,token=NULL,lease_until=NULL,updated_at=?8 WHERE id=?1",params![id,status,request,draft,reason,condition,due_at,now()])?;
    advance_processed(&tx, &conversation)?;
    tx.commit()?;
    Ok(json!({"delivery_id":id,"decision":decision.as_str(),"status":status,"request_id":request}))
}

fn observed_reply_intent(
    b: &Binding,
    conversation: &str,
    delivery: i64,
    publisher: &str,
    request: &str,
) -> Result<(), Box<dyn Error>> {
    let mut bytes = publisher.bytes();
    if delivery <= 0
        || publisher.len() > 40
        || !bytes.next().is_some_and(|c| c.is_ascii_alphabetic())
        || !bytes.all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
        || request != format!("{publisher}.reply-{conversation}-{delivery}")
    {
        return Err(invalid(
            "observed reply requires canonical publisher and delivery key",
        ));
    }
    let path = b.state.join("outgoing.sqlite3");
    private(&path, false)?;
    let out = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let matched: bool = out.query_row("SELECT EXISTS(SELECT 1 FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3 AND publisher_id=?4 AND managed_owner=?5 AND managed_epoch=?6 AND content_kind='text' AND length(text)>0 AND attempt_number=1 AND status IN ('ui_observed','recipient_confirmed'))",
        params![request,b.channel_id,b.session_id,publisher,b.owner,b.epoch], |r|r.get(0))?;
    let attempts: i64 = out.query_row(
        "SELECT COUNT(*) FROM outgoing_attempt WHERE request_id=?1",
        [request],
        |r| r.get(0),
    )?;
    let positive: bool = out.query_row("SELECT EXISTS(SELECT 1 FROM outgoing_attempt a WHERE a.request_id=?1 AND a.attempt_number=1 AND a.finished_at IS NOT NULL AND a.finished_at>=a.claimed_at AND (a.status='ui_observed' OR (a.status='indeterminate' AND EXISTS(SELECT 1 FROM outgoing_observation o WHERE o.request_id=a.request_id AND o.attempt_number=1 AND o.channel_id=?2 AND o.session_id=?3))))",
        params![request,b.channel_id,b.session_id],|r|r.get(0))?;
    if !matched || attempts != 1 || !positive {
        return Err(invalid(
            "original publisher reply is not positively observed for this owner epoch",
        ));
    }
    Ok(())
}

fn inbox_show(root: &Path, id: i64, owner: &str) -> Result<Value, Box<dyn Error>> {
    check_id(owner)?;
    let db = open(root, false)?;
    let row:Option<DeliveryShowRow>=db.query_row(
        "SELECT conversation_id,cursor_start,cursor_end,owner,epoch,status,token,lease_until,turn_state,session_ref,thread_id,turn_id,transport_token FROM delivery WHERE id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?,r.get(10)?,r.get(11)?,r.get(12)?))).optional()?;
    let Some((
        conversation,
        start,
        end,
        saved_owner,
        epoch,
        status,
        token,
        until,
        turn_state,
        session_ref,
        thread_id,
        turn_id,
        transport_token,
    )) = row
    else {
        return Err(invalid("delivery absent"));
    };
    let b = binding(&db, &conversation)?;
    if b.owner != owner || (saved_owner != owner && turn_state.is_none()) {
        return Err(invalid("delivery owner changed"));
    }
    let refs = attachment_refs(&db, id)?;
    let saved: DeliveryDecisionDetails = db.query_row(
        "SELECT draft_text,reason,resume_condition,due_at,decision_request_id FROM delivery WHERE id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?,r.get(4)?)),
    )?;
    Ok(
        json!({"delivery_id":id,"conversation_id":conversation,"cursor_start":start,"cursor_end":end,"owner":owner,"epoch":epoch,"status":status,"token":token,"transport_token":transport_token,"lease_until":until,"turn_state":turn_state,"session_ref":session_ref,"thread_id":thread_id,"turn_id":turn_id,"attachment_refs":refs,"channel_state_dir":b.state,"draft_text":saved.0,"reason":saved.1,"condition":saved.2,"due_at":saved.3,"request_id":saved.4}),
    )
}

fn turn_reserve(root: &Path, id: i64, token: &str, session: &str) -> Result<Value, Box<dyn Error>> {
    check_id(session)?;
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let row:Option<TurnReserveRow>=tx.query_row(
        "SELECT conversation_id,owner,epoch,token,lease_until,turn_state,session_ref FROM delivery WHERE id=?1 AND status='claimed'",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
    let Some((conversation, owner, epoch, saved, until, turn_state, session_ref)) = row else {
        return Err(invalid("delivery not claimed"));
    };
    let b = binding(&tx, &conversation)?;
    if saved != token || b.owner != owner || b.epoch != epoch || b.control != "active" {
        return Err(invalid("delivery claim or owner epoch changed"));
    }
    if b.session_ref
        .as_deref()
        .is_some_and(|existing| existing != session)
    {
        return Err(invalid("owner is bound to another persistent Session; configure with expected epoch to replace it"));
    }
    let scope: Option<String> = tx
        .query_row(
            "SELECT conversation_id FROM session_scope WHERE session_ref=?1",
            [session],
            |r| r.get(0),
        )
        .optional()?;
    let shared =
        super::shared_session_scope::allowed(&tx, session, &conversation, &b.owner, b.epoch)?;
    if !shared
        && (super::shared_session_scope::has_history(&tx, session)?
            || scope.as_deref().is_some_and(|old| old != conversation))
    {
        return Err(invalid(
            "Session conversation scope cannot be reused for another chat",
        ));
    }
    if let Some(state) = turn_state {
        if session_ref.as_deref() != Some(session) {
            return Err(invalid("turn reserved for another session"));
        }
        return Ok(
            json!({"delivery_id":id,"turn_state":state,"session_ref":session,"already_reserved":true}),
        );
    }
    if until.unwrap_or(0) <= now() {
        return Err(invalid("delivery claim expired"));
    }
    tx.execute(
        "UPDATE binding SET session_ref=?2 WHERE conversation_id=?1 AND session_ref IS NULL",
        params![conversation, session],
    )?;
    tx.execute(
        "UPDATE delivery SET turn_state='reserved',transport_token=?2,session_ref=?3,updated_at=?4 WHERE id=?1",
        params![id, token, session, now()],
    )?;
    tx.commit()?;
    Ok(
        json!({"delivery_id":id,"turn_state":"reserved","session_ref":session,"already_reserved":false}),
    )
}
fn turn_record(
    root: &Path,
    id: i64,
    token: &str,
    thread: &str,
    turn: &str,
) -> Result<Value, Box<dyn Error>> {
    check_id(thread)?;
    check_id(turn)?;
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let row:Option<TurnRecordRow>=tx.query_row(
        "SELECT conversation_id,epoch,transport_token,turn_state,thread_id,turn_id FROM delivery WHERE id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((_conversation, _epoch, saved, state, old_thread, old_turn)) = row else {
        return Err(invalid("delivery not claimed"));
    };
    if saved != token {
        return Err(invalid("turn reservation fence changed"));
    }
    if state.as_deref() == Some("recorded") {
        if old_thread.as_deref() != Some(thread) || old_turn.as_deref() != Some(turn) {
            return Err(invalid("different turn already recorded"));
        }
        return Ok(
            json!({"delivery_id":id,"turn_state":"recorded","thread_id":thread,"turn_id":turn}),
        );
    }
    if state.as_deref() != Some("reserved") {
        return Err(invalid("turn was not reserved"));
    }
    tx.execute("UPDATE delivery SET turn_state='recorded',thread_id=?2,turn_id=?3,updated_at=?4 WHERE id=?1",params![id,thread,turn,now()])?;
    tx.commit()?;
    Ok(json!({"delivery_id":id,"turn_state":"recorded","thread_id":thread,"turn_id":turn}))
}
fn turn_finish(root: &Path, id: i64, token: &str, end: TurnEnd) -> Result<Value, Box<dyn Error>> {
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let row: Option<(String, i64, String, Option<String>)> = tx
        .query_row(
            "SELECT conversation_id,epoch,transport_token,turn_state FROM delivery WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let Some((conversation, epoch, saved, state)) = row else {
        return Err(invalid("delivery not claimed"));
    };
    if saved != token {
        return Err(invalid("turn reservation fence changed"));
    }
    if state.as_deref() == Some(end.as_str()) {
        return Ok(json!({"delivery_id":id,"turn_state":end.as_str()}));
    }
    if !matches!(state.as_deref(), Some("reserved" | "recorded")) {
        return Err(invalid("turn not reserved or already terminal"));
    }
    if matches!(end, TurnEnd::Completed) && state.as_deref() != Some("recorded") {
        return Err(invalid("completed turn requires recorded turn ID"));
    }
    tx.execute(
        "UPDATE delivery SET turn_state=?2,updated_at=?3 WHERE id=?1",
        params![id, end.as_str(), now()],
    )?;
    let current = binding(&tx, &conversation)?;
    let (saved_owner, business_status): (String, String) =
        tx.query_row("SELECT owner,status FROM delivery WHERE id=?1", [id], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })?;
    if business_status == "claimed" && (current.epoch != epoch || current.owner != saved_owner) {
        tx.execute("INSERT INTO delivery_turn_history(delivery_id,turn_state,owner,epoch,session_ref,thread_id,turn_id,archived_at) SELECT id,turn_state,owner,epoch,session_ref,thread_id,turn_id,?2 FROM delivery WHERE id=?1",params![id,now()])?;
        tx.execute("UPDATE delivery SET status='pending',owner=?2,epoch=?3,token=NULL,lease_until=NULL,turn_state=NULL,transport_token=NULL,session_ref=NULL,thread_id=NULL,turn_id=NULL,updated_at=?4 WHERE id=?1",params![id,current.owner,current.epoch,now()])?;
    }
    tx.commit()?;
    Ok(json!({"delivery_id":id,"turn_state":end.as_str()}))
}

fn inbox_resume(root: &Path, id: i64, owner: &str, epoch: i64) -> Result<Value, Box<dyn Error>> {
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let conversation: Option<String> = tx
        .query_row(
            "SELECT conversation_id FROM delivery WHERE id=?1 AND (status IN ('deferred','draft') OR (status='claimed' AND turn_state IN ('completed','interrupted','failed')))",
            [id],
            |r| r.get(0),
        )
        .optional()?;
    let Some(conversation) = conversation else {
        return Err(invalid(
            "delivery is not a todo or an explicitly terminal undecided turn",
        ));
    };
    let b = binding(&tx, &conversation)?;
    if b.owner != owner || b.epoch != epoch || b.control != "active" {
        return Err(invalid("owner epoch or control state changed"));
    }
    let turn_state: Option<String> =
        tx.query_row("SELECT turn_state FROM delivery WHERE id=?1", [id], |r| {
            r.get(0)
        })?;
    if turn_state
        .as_deref()
        .is_some_and(|s| !matches!(s, "completed" | "interrupted" | "failed"))
    {
        return Err(invalid(
            "active or unknown turn must be reconciled before todo resumption",
        ));
    }
    tx.execute("INSERT INTO delivery_turn_history(delivery_id,turn_state,owner,epoch,session_ref,thread_id,turn_id,archived_at) SELECT id,turn_state,owner,epoch,session_ref,thread_id,turn_id,?2 FROM delivery WHERE id=?1 AND turn_state IS NOT NULL",params![id,now()])?;
    tx.execute("UPDATE delivery SET status='pending',decision_request_id=NULL,turn_state=NULL,transport_token=NULL,session_ref=NULL,thread_id=NULL,turn_id=NULL,due_at=NULL,owner=?2,epoch=?3,updated_at=?4 WHERE id=?1",params![id,owner,epoch,now()])?;
    tx.commit()?;
    Ok(json!({"delivery_id":id,"status":"pending"}))
}
fn inbox_status(root: &Path, id: &str) -> Result<Value, Box<dyn Error>> {
    let db = open(root, false)?;
    let b = binding(&db, id)?;
    let mut stmt=db.prepare("SELECT id,cursor_start,cursor_end,owner,epoch,status,decision_request_id,draft_text,reason,resume_condition,due_at,turn_state,session_ref,thread_id,turn_id FROM delivery WHERE conversation_id=?1 ORDER BY id")?;
    let rows=stmt.query_map([id],|r|Ok(json!({"delivery_id":r.get::<_,i64>(0)?,"cursor_start":r.get::<_,i64>(1)?,"cursor_end":r.get::<_,i64>(2)?,"owner":r.get::<_,String>(3)?,"epoch":r.get::<_,i64>(4)?,"status":r.get::<_,String>(5)?,"request_id":r.get::<_,Option<String>>(6)?,"has_draft":r.get::<_,Option<String>>(7)?.is_some(),"reason":r.get::<_,Option<String>>(8)?,"condition":r.get::<_,Option<String>>(9)?,"due_at":r.get::<_,Option<i64>>(10)?,"turn_state":r.get::<_,Option<String>>(11)?,"session_ref":r.get::<_,Option<String>>(12)?,"thread_id":r.get::<_,Option<String>>(13)?,"turn_id":r.get::<_,Option<String>>(14)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(
        json!({"conversation_id":id,"owner":b.owner,"epoch":b.epoch,"watermark":b.watermark,"processed_cursor":b.processed_cursor,"deliveries":rows}),
    )
}

fn inbox_cancel(
    root: &Path,
    id: i64,
    owner: &str,
    epoch: i64,
    reason: &str,
) -> Result<Value, Box<dyn Error>> {
    if reason.trim().is_empty() || reason.len() > 4096 {
        return Err(invalid("cancellation requires bounded reason"));
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (conversation, status, turn): (String, String, Option<String>) = tx.query_row(
        "SELECT conversation_id,status,turn_state FROM delivery WHERE id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let b = binding(&tx, &conversation)?;
    if b.owner != owner
        || b.epoch != epoch
        || !matches!(status.as_str(), "deferred" | "draft")
        || turn
            .as_deref()
            .is_some_and(|x| matches!(x, "reserved" | "recorded"))
    {
        return Err(invalid(
            "todo ownership changed or transport is still active",
        ));
    }
    tx.execute("INSERT INTO delivery_decision_history(delivery_id,owner,epoch,decision,reason,recorded_at) VALUES(?1,?2,?3,'cancel',?4,?5)",params![id,owner,epoch,reason,now()])?;
    tx.execute(
        "UPDATE delivery SET status='skipped',reason=?2,due_at=NULL,updated_at=?3 WHERE id=?1",
        params![id, reason, now()],
    )?;
    advance_processed(&tx, &conversation)?;
    tx.commit()?;
    Ok(json!({"delivery_id":id,"decision":"cancel","status":"skipped"}))
}
fn status(root: &Path, id: Option<&str>) -> Result<Value, Box<dyn Error>> {
    let db = open(root, false)?;
    if let Some(id) = id {
        let b = binding(&db, id)?;
        let last_control:Option<Value>=db.query_row("SELECT action,epoch,reason,recorded_at FROM control_event WHERE conversation_id=?1 ORDER BY id DESC LIMIT 1",[id],|r|Ok(json!({"action":r.get::<_,String>(0)?,"epoch":r.get::<_,i64>(1)?,"reason":r.get::<_,Option<String>>(2)?,"recorded_at":r.get::<_,i64>(3)?}))).optional()?;
        return Ok(
            json!({"conversation_id":id,"channel_state_dir":b.state,"channel_id":b.channel_id,"session_id":b.session_id,"owner":b.owner,"input":b.input,"reply_policy":b.reply_policy,"control":b.control,"epoch":b.epoch,"watermark":b.watermark,"processed_cursor":b.processed_cursor,"owner_session_ref":b.session_ref,"last_control_event":last_control}),
        );
    }
    let mut stmt = db.prepare("SELECT conversation_id FROM binding ORDER BY conversation_id")?;
    let ids = stmt
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(json!({"conversations":ids}))
}

pub fn run(root: &Path, action: CoordinatorAction) -> Result<(), Box<dyn Error>> {
    let value = match action {
        CoordinatorAction::Bind {
            conversation_id,
            channel_state_dir,
            owner,
            input,
            reply_policy,
        } => enroll(
            root,
            &conversation_id,
            &channel_state_dir,
            &owner,
            input,
            reply_policy,
        )?,
        CoordinatorAction::Configure {
            conversation_id,
            expected_epoch,
            owner,
            input,
            reply_policy,
        } => configure(
            root,
            &conversation_id,
            expected_epoch,
            &owner,
            input,
            reply_policy,
        )?,
        CoordinatorAction::Takeover {
            conversation_id,
            expected_epoch,
        } => control(root, &conversation_id, expected_epoch, true)?,
        CoordinatorAction::Handback {
            conversation_id,
            expected_epoch,
        } => control(root, &conversation_id, expected_epoch, false)?,
        CoordinatorAction::Enqueue {
            conversation_id,
            request_id,
            expected_epoch,
        } => enqueue(root, &conversation_id, &request_id, expected_epoch, false)?,
        CoordinatorAction::EnqueueFocus {
            conversation_id,
            operation_id,
            expected_epoch,
        } => enqueue(root, &conversation_id, &operation_id, expected_epoch, true)?,
        CoordinatorAction::ReconcileOutbox { conversation_id } => {
            reconcile_outbox(root, &conversation_id)?
        }
        CoordinatorAction::Dispatch { action } => match action {
            DispatchAction::Claim {
                worker,
                lease_seconds,
            } => dispatch_claim(root, &worker, lease_seconds)?,
            DispatchAction::Complete {
                dispatch_id,
                token,
                focus_result,
            } => dispatch_complete(root, dispatch_id, &token, focus_result)?,
            DispatchAction::Validate { dispatch_id, token } => {
                dispatch_validate(root, dispatch_id, &token)?
            }
            DispatchAction::Status => dispatch_status(root)?,
            DispatchAction::Pause { reason } => surface_pause(root, Some(&reason))?,
            DispatchAction::Resume => surface_pause(root, None)?,
            DispatchAction::Review { reason } => surface_review(root, &reason)?,
            DispatchAction::RestoreQueued {
                dispatch_id,
                reason,
            } => restore_queued(root, dispatch_id, &reason)?,
            DispatchAction::RecoverUnsent {
                dispatch_id,
                request_id,
                expected_attempt,
                owner,
                expected_epoch,
                reason,
            } => recover_reviewed(
                root,
                RecoveryKind::FocusSearch,
                RecoveryReview {
                    id: dispatch_id,
                    request: &request_id,
                    expected_attempt,
                    owner: &owner,
                    epoch: expected_epoch,
                    reason: &reason,
                },
            )?,
            DispatchAction::RecoverPrepareHeader {
                dispatch_id,
                request_id,
                expected_attempt,
                owner,
                expected_epoch,
                reason,
            } => recover_reviewed(
                root,
                RecoveryKind::PrepareHeader,
                RecoveryReview {
                    id: dispatch_id,
                    request: &request_id,
                    expected_attempt,
                    owner: &owner,
                    epoch: expected_epoch,
                    reason: &reason,
                },
            )?,
            DispatchAction::ArchiveImageCopyRejection {
                dispatch_id,
                expected_attempt,
                reason,
            } => archive_image_copy_rejection(root, dispatch_id, expected_attempt, &reason)?,
            DispatchAction::ArchiveTextButtonRejection {
                dispatch_id,
                expected_attempt,
                reason,
            } => archive_text_button_rejection(root, dispatch_id, expected_attempt, &reason)?,
            DispatchAction::AbandonUnsent {
                dispatch_id,
                reason,
                expected_attempt,
            } => abandon_unsent(root, dispatch_id, &reason, expected_attempt)?,
        },
        CoordinatorAction::Inbox { action } => match action {
            InboxAction::AuthorizeSharedSession {
                session_ref,
                conversation_id,
                owner,
                expected_epoch,
                reason,
            } => {
                check_id(&session_ref)?;
                check_id(&conversation_id)?;
                check_id(&owner)?;
                let mut db = open(root, true)?;
                super::shared_session_scope::authorize(
                    &mut db,
                    &session_ref,
                    &conversation_id,
                    &owner,
                    expected_epoch,
                    &reason,
                )?
            }
            InboxAction::Reconcile { conversation_id } => inbox_reconcile(root, &conversation_id)?,
            InboxAction::Claim {
                conversation_id,
                owner,
                lease_seconds,
                expected_epoch,
                session_ref,
            } => inbox_claim_for_session(
                root,
                &conversation_id,
                &owner,
                lease_seconds,
                expected_epoch,
                session_ref.as_deref(),
            )?,
            InboxAction::Complete {
                delivery_id,
                token,
                decision,
                request_id,
                draft_stdin,
                reason,
                condition,
                due_at,
            } => {
                let draft = if draft_stdin {
                    let mut text = String::new();
                    io::stdin().take(65537).read_to_string(&mut text)?;
                    Some(text)
                } else {
                    None
                };
                inbox_complete(
                    root,
                    delivery_id,
                    &token,
                    DecisionInput {
                        decision,
                        request: request_id.as_deref(),
                        draft: draft.as_deref(),
                        reason: reason.as_deref(),
                        condition: condition.as_deref(),
                        due_at,
                    },
                )?
            }
            InboxAction::CompleteObservedReply {
                delivery_id,
                token,
                publisher,
                request_id,
                reason,
            } => inbox_complete_mode(
                root,
                delivery_id,
                &token,
                DecisionInput {
                    decision: Decision::Reply,
                    request: Some(&request_id),
                    draft: None,
                    reason: Some(&reason),
                    condition: None,
                    due_at: None,
                },
                Some(&publisher),
            )?,
            InboxAction::Resume {
                delivery_id,
                owner,
                expected_epoch,
            } => inbox_resume(root, delivery_id, &owner, expected_epoch)?,
            InboxAction::Cancel {
                delivery_id,
                owner,
                expected_epoch,
                reason,
            } => inbox_cancel(root, delivery_id, &owner, expected_epoch, &reason)?,
            InboxAction::ReserveTurn {
                delivery_id,
                token,
                session_ref,
            } => turn_reserve(root, delivery_id, &token, &session_ref)?,
            InboxAction::RecordTurn {
                delivery_id,
                token,
                thread_id,
                turn_id,
            } => turn_record(root, delivery_id, &token, &thread_id, &turn_id)?,
            InboxAction::FinishTurn {
                delivery_id,
                token,
                status,
            } => turn_finish(root, delivery_id, &token, status)?,
            InboxAction::Show { delivery_id, owner } => inbox_show(root, delivery_id, &owner)?,
            InboxAction::Status { conversation_id } => inbox_status(root, &conversation_id)?,
        },
        CoordinatorAction::Status { conversation_id } => status(root, conversation_id.as_deref())?,
    };
    println!("{value}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cmd::channel::{self, ChannelAction};

    fn fixture() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("source");
        fs::create_dir(&source).unwrap();
        let state = temp.path().join("channel");
        channel::run(
            &state,
            ChannelAction::Bind {
                account: "synthetic_account".into(),
                talker: "synthetic@chatroom".into(),
                session: "synthetic_session".into(),
                source,
                encrypted_snapshot: false,
            },
        )
        .unwrap();
        let root = temp.path().join("desktop");
        (temp, state, root)
    }
    fn observed(state: &Path, sender: &str) -> i64 {
        let db = Connection::open(state.join("channel.sqlite3")).unwrap();
        db.execute(
            "UPDATE channel_binding SET self_wxid='synthetic_self' WHERE singleton=1",
            [],
        )
        .unwrap();
        db.execute("INSERT INTO channel_events(kind,server_id,message_json) VALUES('message_observed',?1,?2)",params![100+now(),json!({"sender":sender,"msg_type":3}).to_string()]).unwrap();
        db.last_insert_rowid()
    }
    fn queued(state: &Path, request: &str, epoch: i64) {
        let b = load_binding(state).unwrap();
        let db = Connection::open(state.join("outgoing.sqlite3")).unwrap();
        db.execute("INSERT INTO outgoing_intent(request_id,channel_id,session_id,text,content_kind,status,managed_owner,managed_epoch,created_at,updated_at) VALUES(?1,?2,?3,'fixture','text','queued','owner1',?4,?5,?5)",params![request,b.channel_id,b.session_id,epoch,now()]).unwrap();
    }
    #[test]
    fn image_copy_rejection_accepts_producer_spacing_only() {
        let prefix = "Composer Copy did not replace image sentinel exactly once (types:";
        for types in [
            " public.file-url,public.tiff",
            "public.file-url,public.tiff",
        ] {
            assert!(known_image_copy_rejection(&json!(format!(
                "{prefix}{types})"
            ))));
        }
        for types in [
            "  public.tiff",
            "\tpublic.tiff",
            "\npublic.tiff",
            "\u{a0}public.tiff",
            "",
            " ",
            " public.file-url",
            "public.file-url",
            " public.tiff,",
            " public.tiff, public.png",
        ] {
            assert!(
                !known_image_copy_rejection(&json!(format!("{prefix}{types})"))),
                "unexpected types spelling: {types:?}"
            );
        }
        assert!(!known_image_copy_rejection(&json!(
            "Other error (types: public.tiff)"
        )));
        assert!(!known_image_copy_rejection(&json!(format!(
            "{prefix} public.tiff) trailing"
        ))));
    }
    #[test]
    fn image_setup_refusal_requires_exact_claim_and_only_completion_releases_surface() {
        let (_tmp, state, root) = fixture();
        enroll(
            &root,
            "conversation1",
            &state,
            "owner1",
            InputMode::Push,
            ReplyPolicy::Authorized,
        )
        .unwrap();
        queued(&state, "setup-image", 1);
        enqueue(&root, "conversation1", "setup-image", 1, false).unwrap();
        let claim = dispatch_claim(&root, "worker1", 120).unwrap();
        let id = claim["dispatch_id"].as_i64().unwrap();
        let token = claim["token"].as_str().unwrap();
        assert!(
            mark_image_setup_refusal(&root, &state, "setup-image", id, token).is_err(),
            "text is not an image setup refusal"
        );
        let out = Connection::open(state.join("outgoing.sqlite3")).unwrap();
        out.execute("UPDATE outgoing_intent SET content_kind='image',status='managed_media_queued' WHERE request_id='setup-image'",[]).unwrap();
        assert!(mark_image_setup_refusal(&root, &state, "setup-image", id, "wrong-token").is_err());
        assert!(mark_image_setup_refusal(&root, &state, "wrong-request", id, token).is_err());
        let before = dispatch_status(&root).unwrap();
        mark_image_setup_refusal(&root, &state, "setup-image", id, token).unwrap();
        let marked = dispatch_status(&root).unwrap();
        assert_eq!(marked["active_dispatch"], before["active_dispatch"]);
        assert_eq!(marked["surface_epoch"], before["surface_epoch"]);
        assert!(marked["paused_reason"].is_null());
        assert_eq!(marked["dispatches"][0]["status"], "claimed");
        assert_eq!(
            marked["dispatches"][0]["result"],
            "image_verification_unavailable"
        );
        validate_dispatch(&root, &state, "setup-image", id, token).unwrap();
        let complete = dispatch_complete(&root, id, token, None).unwrap();
        assert_eq!(complete["status"], "blocked");
        assert_eq!(complete["result"], "image_verification_unavailable");
        assert_eq!(complete["surface_paused"], false);
        assert!(dispatch_status(&root).unwrap()["active_dispatch"].is_null());
        assert!(mark_image_setup_refusal(&root, &state, "setup-image", id, token).is_err());
        assert_eq!(
            out.query_row("SELECT COUNT(*) FROM outgoing_attempt", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            out.query_row(
                "SELECT attempt_number FROM outgoing_intent WHERE request_id='setup-image'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
    }
    #[test]
    fn paused_pull_events_are_reconciled_and_draft_resumes_without_rewinding() {
        let (_tmp, state, root) = fixture();
        enroll(
            &root,
            "conversation1",
            &state,
            "owner1",
            InputMode::Pull,
            ReplyPolicy::Observe,
        )
        .unwrap();
        control(&root, "conversation1", 1, true).unwrap();
        let cursor = observed(&state, "synthetic_other");
        assert_eq!(
            inbox_reconcile(&root, "conversation1").unwrap()["created"],
            1
        );
        assert_eq!(
            inbox_reconcile(&root, "conversation1").unwrap()["created"],
            0
        );
        assert_eq!(
            inbox_status(&root, "conversation1").unwrap()["watermark"],
            cursor
        );
        assert!(inbox_claim(&root, "conversation1", "owner1", 300, None).is_err());
        control(&root, "conversation1", 2, false).unwrap();
        let claim = inbox_claim(&root, "conversation1", "owner1", 300, None).unwrap();
        let id = claim["delivery_id"].as_i64().unwrap();
        let token = claim["token"].as_str().unwrap();
        inbox_complete(
            &root,
            id,
            token,
            DecisionInput {
                decision: Decision::Draft,
                request: None,
                draft: Some("review first"),
                reason: Some("needs approval"),
                condition: None,
                due_at: None,
            },
        )
        .unwrap();
        assert_eq!(
            inbox_status(&root, "conversation1").unwrap()["processed_cursor"],
            cursor
        );
        inbox_resume(&root, id, "owner1", 3).unwrap();
        assert_eq!(
            inbox_status(&root, "conversation1").unwrap()["processed_cursor"],
            cursor
        );
        assert_eq!(
            inbox_claim(&root, "conversation1", "owner1", 300, None).unwrap()["delivery_id"],
            id
        );
    }
    #[test]
    fn resumed_terminal_todo_moves_to_current_owner_without_losing_history() {
        for (decision, draft) in [(Decision::Defer, None), (Decision::Draft, Some("review"))] {
            let (_tmp, state, root) = fixture();
            enroll(
                &root,
                "conversation1",
                &state,
                "owner1",
                InputMode::Push,
                ReplyPolicy::Observe,
            )
            .unwrap();
            let cursor = observed(&state, "synthetic_other");
            inbox_reconcile(&root, "conversation1").unwrap();
            let claim = inbox_claim(&root, "conversation1", "owner1", 300, Some(1)).unwrap();
            let id = claim["delivery_id"].as_i64().unwrap();
            let token = claim["token"].as_str().unwrap();
            turn_reserve(&root, id, token, "codex:synthetic:original").unwrap();
            turn_record(&root, id, token, "original", "original-turn").unwrap();
            inbox_complete(
                &root,
                id,
                token,
                DecisionInput {
                    decision,
                    request: None,
                    draft,
                    reason: Some("review later"),
                    condition: None,
                    due_at: None,
                },
            )
            .unwrap();
            turn_finish(&root, id, token, TurnEnd::Completed).unwrap();
            configure(
                &root,
                "conversation1",
                1,
                "owner2",
                InputMode::Push,
                ReplyPolicy::Observe,
            )
            .unwrap();
            assert!(inbox_resume(&root, id, "owner1", 2).is_err());
            inbox_resume(&root, id, "owner2", 2).unwrap();
            let resumed = inbox_claim(&root, "conversation1", "owner2", 300, Some(2)).unwrap();
            assert_eq!(resumed["delivery_id"], id);
            assert_eq!(resumed["owner"], "owner2");
            assert_eq!(resumed["epoch"], 2);
            assert_eq!(resumed["cursor_start"], cursor);
            assert_eq!(resumed["cursor_end"], cursor);
            let db = open(&root, false).unwrap();
            let history: (String, i64, String) = db
                .query_row(
                    "SELECT owner,epoch,turn_id FROM delivery_turn_history WHERE delivery_id=?1",
                    [id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .unwrap();
            assert_eq!(
                history,
                ("owner1".to_owned(), 1, "original-turn".to_owned())
            );
        }
    }
    #[test]
    fn dispatch_requires_authorization_and_unknown_never_replays() {
        let (_tmp, state, root) = fixture();
        enroll(
            &root,
            "conversation1",
            &state,
            "owner1",
            InputMode::Push,
            ReplyPolicy::Observe,
        )
        .unwrap();
        queued(&state, "stale-request", 1);
        assert!(enqueue(&root, "conversation1", "stale-request", 1, false).is_err());
        configure(
            &root,
            "conversation1",
            1,
            "owner1",
            InputMode::Push,
            ReplyPolicy::Authorized,
        )
        .unwrap();
        assert_eq!(
            reconcile_outbox(&root, "conversation1").unwrap()["quarantined"],
            1
        );
        queued(&state, "request1", 2);
        enqueue(&root, "conversation1", "request1", 2, false).unwrap();
        let claim = dispatch_claim(&root, "worker1", 120).unwrap();
        let id = claim["dispatch_id"].as_i64().unwrap();
        let token = claim["token"].as_str().unwrap();
        validate_dispatch(&root, &state, "request1", id, token).unwrap();
        assert!(validate_dispatch(&root, &state, "request1", id, "wrong").is_err());
        let out = Connection::open(state.join("outgoing.sqlite3")).unwrap();
        out.execute("UPDATE outgoing_intent SET status='attempt_recorded',attempt_number=1 WHERE request_id='request1'",[]).unwrap();
        assert_eq!(
            dispatch_complete(&root, id, token, None).unwrap()["status"],
            "unknown"
        );
        assert_eq!(
            dispatch_claim(&root, "worker2", 120).unwrap()["status"],
            "paused"
        );
        assert!(surface_pause(&root, None).is_err());
        surface_review(&root, "helper exited and desktop checked").unwrap();
        assert_eq!(
            dispatch_claim(&root, "worker2", 120).unwrap()["status"],
            "empty"
        );
    }
    #[test]
    fn turn_reservation_is_single_session_and_transport_survives_business_completion() {
        let (_tmp, state, root) = fixture();
        enroll(
            &root,
            "conversation1",
            &state,
            "owner1",
            InputMode::Push,
            ReplyPolicy::Observe,
        )
        .unwrap();
        observed(&state, "synthetic_other");
        inbox_reconcile(&root, "conversation1").unwrap();
        observed(&state, "synthetic_other");
        inbox_reconcile(&root, "conversation1").unwrap();
        let first = inbox_claim(&root, "conversation1", "owner1", 300, None).unwrap();
        let id = first["delivery_id"].as_i64().unwrap();
        let token = first["token"].as_str().unwrap();
        turn_reserve(&root, id, token, "session1").unwrap();
        assert_eq!(
            inbox_claim(&root, "conversation1", "owner1", 300, None).unwrap()["status"],
            "empty"
        );
        turn_record(&root, id, token, "thread1", "turn1").unwrap();
        inbox_complete(
            &root,
            id,
            token,
            DecisionInput {
                decision: Decision::Skip,
                request: None,
                draft: None,
                reason: None,
                condition: None,
                due_at: None,
            },
        )
        .unwrap();
        let second = inbox_claim(&root, "conversation1", "owner1", 300, None).unwrap();
        let id2 = second["delivery_id"].as_i64().unwrap();
        let token2 = second["token"].as_str().unwrap();
        assert!(turn_reserve(&root, id2, token2, "session1").is_err());
        turn_finish(&root, id, token, TurnEnd::Completed).unwrap();
        turn_reserve(&root, id2, token2, "session1").unwrap();
        assert_eq!(
            inbox_show(&root, id2, "owner1").unwrap()["turn_state"],
            "reserved"
        );
    }
    #[test]
    fn updated_message_creates_new_delivery_without_replaying_old_cursor() {
        let (_tmp, state, root) = fixture();
        enroll(
            &root,
            "conversation1",
            &state,
            "owner1",
            InputMode::Push,
            ReplyPolicy::Observe,
        )
        .unwrap();
        let first = observed(&state, "synthetic_other");
        assert_eq!(
            inbox_reconcile(&root, "conversation1").unwrap()["created"],
            1
        );
        let source = Connection::open(state.join("channel.sqlite3")).unwrap();
        source.execute("INSERT INTO channel_events(kind,server_id,message_json) VALUES('message_updated',100,?1)",[json!({"sender":"synthetic_other","msg_type":3,"revision":"changed"}).to_string()]).unwrap();
        let second = source.last_insert_rowid();
        assert!(second > first);
        assert_eq!(
            inbox_reconcile(&root, "conversation1").unwrap()["created"],
            1
        );
        assert_eq!(
            inbox_reconcile(&root, "conversation1").unwrap()["created"],
            0
        );
        assert_eq!(
            inbox_status(&root, "conversation1").unwrap()["deliveries"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }
    #[test]
    fn terminal_old_turn_rehomes_undecided_delivery_and_retains_transport_history() {
        let (_tmp, state, root) = fixture();
        enroll(
            &root,
            "conversation1",
            &state,
            "owner1",
            InputMode::Push,
            ReplyPolicy::Observe,
        )
        .unwrap();
        observed(&state, "synthetic_other");
        inbox_reconcile(&root, "conversation1").unwrap();
        let claim = inbox_claim(&root, "conversation1", "owner1", 300, None).unwrap();
        let id = claim["delivery_id"].as_i64().unwrap();
        let token = claim["token"].as_str().unwrap();
        turn_reserve(&root, id, token, "session1").unwrap();
        configure(
            &root,
            "conversation1",
            1,
            "owner2",
            InputMode::Push,
            ReplyPolicy::Observe,
        )
        .unwrap();
        assert_eq!(
            inbox_show(&root, id, "owner2").unwrap()["transport_token"],
            token
        );
        assert!(
            inbox_claim(&root, "conversation1", "owner2", 300, None).unwrap()["status"] == "empty"
        );
        turn_record(&root, id, token, "thread1", "turn1").unwrap();
        turn_finish(&root, id, token, TurnEnd::Completed).unwrap();
        let resumed = inbox_claim(&root, "conversation1", "owner2", 300, None).unwrap();
        assert_eq!(resumed["delivery_id"], id);
        let db = open(&root, false).unwrap();
        let count:i64=db.query_row("SELECT COUNT(*) FROM delivery_turn_history WHERE delivery_id=?1 AND turn_state='completed' AND owner='owner1' AND epoch=1",[id],|r|r.get(0)).unwrap();
        assert_eq!(count, 1);
    }
}
