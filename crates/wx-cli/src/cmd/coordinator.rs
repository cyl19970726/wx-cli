//! Private, reference-only coordinator for managed conversations.
//! The channel journal and outgoing journal remain the authoritative stores.
use std::error::Error;
use std::fs::{self, OpenOptions};
use std::io::{self, Read};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

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
        db.execute_batch("CREATE UNIQUE INDEX IF NOT EXISTS dedicated_session_per_conversation ON binding(session_ref) WHERE session_ref IS NOT NULL;")?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS session_scope (
          session_ref TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
          created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        INSERT OR IGNORE INTO session_scope(session_ref,conversation_id) SELECT session_ref,conversation_id FROM binding WHERE session_ref IS NOT NULL;
        CREATE TRIGGER IF NOT EXISTS session_scope_immutable BEFORE UPDATE ON session_scope BEGIN SELECT RAISE(ABORT,'Session conversation scope is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS session_scope_retained BEFORE DELETE ON session_scope BEGIN SELECT RAISE(ABORT,'Session conversation scope history must be retained'); END;
        CREATE TRIGGER IF NOT EXISTS dedicated_session_scope AFTER UPDATE OF session_ref ON binding WHEN NEW.session_ref IS NOT NULL BEGIN
          INSERT OR IGNORE INTO session_scope(session_ref,conversation_id) VALUES(NEW.session_ref,NEW.conversation_id);
          SELECT CASE WHEN (SELECT conversation_id FROM session_scope WHERE session_ref=NEW.session_ref)<>NEW.conversation_id THEN RAISE(ABORT,'Session belongs to another conversation') END;
        END;")?;
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
    db.query_row("SELECT status,attempt_number,managed_owner,managed_epoch,batch_id,part_index,priority,expires_at FROM outgoing_intent WHERE request_id=?1 AND channel_id=?2 AND session_id=?3",
        params![request,b.channel_id,b.session_id],|r| Ok(IntentRef{status:r.get(0)?,attempts:r.get(1)?,owner:r.get(2)?,epoch:r.get(3)?,batch_id:r.get(4)?,part_index:r.get(5)?,priority:r.get(6)?,expires_at:r.get(7)?}))
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
    let candidate:Option<(i64,String,String,String,i64)>=tx.query_row(
        "SELECT d.id,d.conversation_id,d.request_id,d.operation,d.epoch FROM dispatch d JOIN binding b ON b.conversation_id=d.conversation_id WHERE d.status='queued' AND b.control='active' AND b.epoch=d.epoch AND (d.operation='focus' OR b.reply_policy='authorized') AND NOT EXISTS (SELECT 1 FROM dispatch earlier WHERE earlier.conversation_id=d.conversation_id AND earlier.status IN ('queued','claimed','blocked','unknown') AND NOT (earlier.status='blocked' AND earlier.epoch<>d.epoch AND earlier.result IN ('owner_epoch_changed','control_epoch_changed','possible_manual_outgoing')) AND ((d.batch_id IS NOT NULL AND earlier.batch_id=d.batch_id AND earlier.part_index<d.part_index) OR ((d.batch_id IS NULL OR earlier.batch_id IS NULL OR earlier.batch_id<>d.batch_id) AND earlier.id<d.id))) AND (d.batch_id IS NULL OR (SELECT COUNT(*) FROM dispatch p WHERE p.conversation_id=d.conversation_id AND p.batch_id=d.batch_id AND p.part_index<d.part_index AND p.status='done')=d.part_index) ORDER BY CASE WHEN d.created_at<=?1-300 THEN 0 ELSE 1 END, CASE WHEN d.created_at<=?1-300 THEN d.id ELSE d.priority END,d.id LIMIT 1",[now()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let Some((id, conversation, request, operation, epoch)) = candidate else {
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
    let row: Option<(String, String, String, String, String)> = tx
        .query_row(
            "SELECT conversation_id,request_id,operation,status,token FROM dispatch WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )
        .optional()?;
    let Some((conversation, request, operation, status, saved)) = row else {
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
        || !intent(&b, &request)?.ready_for(&b)
    {
        return Err(invalid(
            "only a current-owner, unattempted managed intent can return to queue",
        ));
    }
    let original: (Option<String>, Option<i64>) =
        intent(&b, &request).map(|item| (item.owner, item.epoch))?;
    if original.0.as_deref() != Some(b.owner.as_str()) || original.1 != Some(epoch) {
        return Err(invalid(
            "unattempted intent does not match its original owner epoch",
        ));
    }
    // A reviewed configure may advance the owner generation before this
    // recovery. Keep the immutable content and reconcile only this unattempted
    // original intent to that current generation; never replay an attempt.
    let outbox = b.state.join("outgoing.sqlite3");
    let mut outbox = Connection::open_with_flags(&outbox, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    let outbox_tx = outbox.transaction_with_behavior(TransactionBehavior::Immediate)?;
    outbox_tx.execute(
        "UPDATE outgoing_intent SET managed_owner=?1,managed_epoch=?2 WHERE request_id=?3 AND managed_owner=?4 AND managed_epoch=?5 AND attempt_number=0 AND status IN ('managed_queued','managed_media_queued')",
        params![b.owner,b.epoch,request,original.0,original.1],
    )?;
    outbox_tx.commit()?;
    tx.execute(
        "UPDATE dispatch SET epoch=?2 WHERE id=?1",
        params![id, b.epoch],
    )?;
    tx.execute("UPDATE dispatch SET status='queued',token=NULL,worker=NULL,lease_until=NULL,result='restored_before_attempt',updated_at=?2 WHERE id=?1",params![id,now()])?;
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

fn inbox_claim(root: &Path, id: &str, owner: &str, lease: i64) -> Result<Value, Box<dyn Error>> {
    check_id(owner)?;
    if !(30..=3600).contains(&lease) {
        return Err(invalid("inbox lease must be 30..3600 seconds"));
    }
    let mut db = open(root, true)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let b = binding(&tx, id)?;
    verify_binding(&b)?;
    if b.owner != owner || b.control != "active" {
        return Err(invalid("owner or control state changed"));
    }
    let row:Option<(i64,i64,i64,String,Option<i64>)>=tx.query_row("SELECT d.id,d.cursor_start,d.cursor_end,d.status,d.lease_until FROM delivery d WHERE d.conversation_id=?1 AND d.owner=?2 AND d.epoch=?3 AND d.turn_state IS NULL AND (d.status='pending' OR (d.status='claimed' AND d.lease_until<=?4)) AND NOT EXISTS (SELECT 1 FROM delivery earlier WHERE earlier.conversation_id=d.conversation_id AND earlier.id<d.id AND earlier.status IN ('pending','claimed')) ORDER BY d.id LIMIT 1",params![id,owner,b.epoch,now()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let Some((delivery, start, end, _, _)) = row else {
        return Ok(json!({"status":"empty"}));
    };
    let token = token()?;
    tx.execute(
        "UPDATE delivery SET status='claimed',token=?2,lease_until=?3,updated_at=?4 WHERE id=?1",
        params![delivery, token, now() + lease, now()],
    )?;
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
        if !intent(&b, rid)?.ready_for(&b) {
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
    let saved: (Option<String>, Option<String>, Option<String>, Option<i64>) = db.query_row(
        "SELECT draft_text,reason,resume_condition,due_at FROM delivery WHERE id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
    )?;
    Ok(
        json!({"delivery_id":id,"conversation_id":conversation,"cursor_start":start,"cursor_end":end,"owner":owner,"epoch":epoch,"status":status,"token":token,"transport_token":transport_token,"lease_until":until,"turn_state":turn_state,"session_ref":session_ref,"thread_id":thread_id,"turn_id":turn_id,"attachment_refs":refs,"channel_state_dir":b.state,"draft_text":saved.0,"reason":saved.1,"condition":saved.2,"due_at":saved.3}),
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
    if scope.as_deref().is_some_and(|old| old != conversation) {
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
    tx.execute("UPDATE delivery SET status='pending',decision_request_id=NULL,turn_state=NULL,transport_token=NULL,session_ref=NULL,thread_id=NULL,turn_id=NULL,due_at=NULL,epoch=?2,updated_at=?3 WHERE id=?1",params![id,epoch,now()])?;
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
        },
        CoordinatorAction::Inbox { action } => match action {
            InboxAction::Reconcile { conversation_id } => inbox_reconcile(root, &conversation_id)?,
            InboxAction::Claim {
                conversation_id,
                owner,
                lease_seconds,
            } => inbox_claim(root, &conversation_id, &owner, lease_seconds)?,
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
        assert!(inbox_claim(&root, "conversation1", "owner1", 300).is_err());
        control(&root, "conversation1", 2, false).unwrap();
        let claim = inbox_claim(&root, "conversation1", "owner1", 300).unwrap();
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
            inbox_claim(&root, "conversation1", "owner1", 300).unwrap()["delivery_id"],
            id
        );
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
        let first = inbox_claim(&root, "conversation1", "owner1", 300).unwrap();
        let id = first["delivery_id"].as_i64().unwrap();
        let token = first["token"].as_str().unwrap();
        turn_reserve(&root, id, token, "session1").unwrap();
        assert_eq!(
            inbox_claim(&root, "conversation1", "owner1", 300).unwrap()["status"],
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
        let second = inbox_claim(&root, "conversation1", "owner1", 300).unwrap();
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
        let claim = inbox_claim(&root, "conversation1", "owner1", 300).unwrap();
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
        assert!(inbox_claim(&root, "conversation1", "owner2", 300).unwrap()["status"] == "empty");
        turn_record(&root, id, token, "thread1", "turn1").unwrap();
        turn_finish(&root, id, token, TurnEnd::Completed).unwrap();
        let resumed = inbox_claim(&root, "conversation1", "owner2", 300).unwrap();
        assert_eq!(resumed["delivery_id"], id);
        let db = open(&root, false).unwrap();
        let count:i64=db.query_row("SELECT COUNT(*) FROM delivery_turn_history WHERE delivery_id=?1 AND turn_state='completed' AND owner='owner1' AND epoch=1",[id],|r|r.get(0)).unwrap();
        assert_eq!(count, 1);
    }
}
