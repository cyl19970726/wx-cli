//! One explicitly bound group, backed by a private local event journal.
use std::collections::BTreeMap;
use std::error::Error;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

use clap::Subcommand;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use wx_db::{
    ChatRoomQuery, MessageQuery, MessageQueryResult, SortOrder, WechatDb, MAX_QUERY_LIMIT,
};

use super::channel_snapshot as snapshot;

const DB_NAME: &str = "channel.sqlite3";

#[derive(Debug, Subcommand)]
pub enum ChannelAction {
    /// Permanently bind this state directory to exactly one local group.
    Bind {
        #[arg(long)]
        account: String,
        #[arg(long)]
        talker: String,
        #[arg(long)]
        session: String,
        #[arg(long)]
        source: PathBuf,
        /// Copy encrypted account databases into private state before reading.
        #[arg(long)]
        encrypted_snapshot: bool,
    },
    /// Show the binding and local journal position.
    Status {
        /// Include this consumer's durable cursor in the status response.
        #[arg(long)]
        consumer: Option<String>,
    },
    /// Rescan the bounded, decrypted local database and append changes.
    Sync,
    /// Read journal events without acknowledging them.
    Events {
        #[arg(long, default_value_t = 0)]
        after: i64,
        #[arg(long, default_value_t = 100)]
        limit: usize,
    },
    /// Explicitly acknowledge an event after a consumer has processed it.
    Ack {
        #[arg(long)]
        consumer: String,
        #[arg(long)]
        cursor: i64,
        /// Refuse the update unless the consumer is still at this cursor.
        #[arg(long)]
        expected_cursor: Option<i64>,
    },
    /// Poll and print NDJSON. Output alone never acknowledges an event.
    Subscribe {
        #[arg(long)]
        consumer: String,
        #[arg(long, default_value_t = 1000)]
        poll_ms: u64,
        #[arg(long)]
        once: bool,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BindingView {
    pub channel_id: String,
    pub session_id: String,
    pub account: String,
    pub talker: String,
    pub source: String,
    #[serde(default)]
    pub encrypted_snapshot: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub self_wxid: Option<String>,
}

fn invalid(message: &str) -> Box<dyn Error> {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, message).into()
}

fn ensure_private(path: &Path, directory: bool) -> Result<(), Box<dyn Error>> {
    let meta = fs::symlink_metadata(path)?;
    if meta.file_type().is_symlink()
        || (directory && !meta.is_dir())
        || (!directory && !meta.is_file())
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o777 != if directory { 0o700 } else { 0o600 }
        || (!directory && meta.nlink() != 1)
    {
        return Err(invalid(
            "state path must be private, owned, regular, and never a symlink or hard-linked database",
        ));
    }
    Ok(())
}

fn open_state(state_dir: &Path, create: bool) -> Result<Connection, Box<dyn Error>> {
    if fs::symlink_metadata(state_dir).is_err() && create {
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700).create(state_dir)?;
    }
    ensure_private(state_dir, true)?;
    let db_path = state_dir.join(DB_NAME);
    if fs::symlink_metadata(&db_path).is_ok() {
        ensure_private(&db_path, false)?;
    } else if create {
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&db_path)?;
        ensure_private(&db_path, false)?;
    } else {
        return Err(invalid("channel is not bound"));
    }
    let flags = if create {
        OpenFlags::SQLITE_OPEN_READ_WRITE
    } else {
        OpenFlags::SQLITE_OPEN_READ_ONLY
    };
    let conn = Connection::open_with_flags(&db_path, flags)?;
    if create {
        conn.execute_batch(
            "PRAGMA busy_timeout=5000; PRAGMA journal_mode=DELETE;
            CREATE TABLE IF NOT EXISTS channel_binding (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                channel_id TEXT NOT NULL, session_id TEXT NOT NULL,
                account TEXT NOT NULL, talker TEXT NOT NULL, source TEXT NOT NULL,
                encrypted_snapshot INTEGER NOT NULL DEFAULT 0,
                self_wxid TEXT
            );
            CREATE TABLE IF NOT EXISTS channel_messages (
                server_id INTEGER PRIMARY KEY CHECK(server_id > 0),
                snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, server_id INTEGER NOT NULL,
                message_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_consumers (
                consumer TEXT PRIMARY KEY, cursor INTEGER NOT NULL CHECK(cursor >= 0)
            );",
        )?;
        let columns: Vec<String> = conn
            .prepare("PRAGMA table_info(channel_binding)")?
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<Result<_, _>>()?;
        if !columns.iter().any(|name| name == "encrypted_snapshot") {
            conn.execute_batch("ALTER TABLE channel_binding ADD COLUMN encrypted_snapshot INTEGER NOT NULL DEFAULT 0")?;
        }
        if !columns.iter().any(|name| name == "self_wxid") {
            conn.execute_batch("ALTER TABLE channel_binding ADD COLUMN self_wxid TEXT")?;
        }
    }
    Ok(conn)
}

fn read_binding(conn: &Connection) -> Result<BindingView, Box<dyn Error>> {
    let columns: Vec<String> = conn
        .prepare("PRAGMA table_info(channel_binding)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<_, _>>()?;
    let mode = if columns.iter().any(|name| name == "encrypted_snapshot") {
        "encrypted_snapshot"
    } else {
        "0"
    };
    let identity = if columns.iter().any(|name| name == "self_wxid") {
        "self_wxid"
    } else {
        "NULL"
    };
    let query = format!(
        "SELECT channel_id,session_id,account,talker,source,{mode},{identity} FROM channel_binding WHERE singleton=1"
    );
    conn.query_row(&query, [], |row| {
        Ok(BindingView {
            channel_id: row.get(0)?,
            session_id: row.get(1)?,
            account: row.get(2)?,
            talker: row.get(3)?,
            source: row.get(4)?,
            encrypted_snapshot: row.get::<_, i64>(5)? != 0,
            self_wxid: row.get(6)?,
        })
    })
    .optional()?
    .ok_or_else(|| invalid("channel is not bound"))
}

/// Read the immutable binding without creating state or selecting an account.
pub fn load_binding(state_dir: &Path) -> Result<BindingView, Box<dyn Error>> {
    read_binding(&open_state(state_dir, false)?)
}

fn random_channel_id() -> Result<String, Box<dyn Error>> {
    let mut bytes = [0_u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(hex::encode(bytes))
}

fn ensure_disjoint_state(state_dir: &Path, account: &Path) -> Result<(), Box<dyn Error>> {
    let state = if state_dir.exists() {
        state_dir.canonicalize()?
    } else {
        let parent = state_dir
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        let name = state_dir
            .file_name()
            .ok_or_else(|| invalid("invalid state directory"))?;
        parent.canonicalize()?.join(name)
    };
    let account = account.canonicalize()?;
    if state.starts_with(&account) || account.starts_with(&state) {
        return Err(invalid(
            "state directory and encrypted source account must be separate",
        ));
    }
    Ok(())
}

fn bind(
    state_dir: &Path,
    account: String,
    talker: String,
    session_id: String,
    source: PathBuf,
    encrypted_snapshot: bool,
) -> Result<BindingView, Box<dyn Error>> {
    if account.trim().is_empty()
        || session_id.trim().is_empty()
        || talker.trim().is_empty()
        || !talker.ends_with("@chatroom")
        || talker.contains(char::is_whitespace)
    {
        return Err(invalid(
            "account, exact @chatroom talker, and session are required",
        ));
    }
    if encrypted_snapshot {
        snapshot::validate_account_path(&source)?;
        if source.file_name().and_then(|s| s.to_str()) != Some(account.as_str()) {
            return Err(invalid(
                "encrypted account must match the source account directory basename",
            ));
        }
    } else if !source.is_dir() {
        return Err(invalid("source must be a decrypted db_storage directory"));
    }
    let source = source.canonicalize()?;
    if encrypted_snapshot {
        ensure_disjoint_state(state_dir, &source)?;
    }
    let source = source.to_string_lossy().into_owned();
    let mut conn = open_state(state_dir, true)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if let Ok(existing) = read_binding(&tx) {
        if existing.account == account
            && existing.talker == talker
            && existing.session_id == session_id
            && existing.source == source
            && existing.encrypted_snapshot == encrypted_snapshot
        {
            tx.commit()?;
            return Ok(existing);
        }
        return Err(invalid("channel is already bound; replacement is refused"));
    }
    let view = BindingView {
        channel_id: random_channel_id()?,
        session_id,
        account,
        talker,
        source,
        encrypted_snapshot,
        self_wxid: None,
    };
    tx.execute("INSERT INTO channel_binding(singleton,channel_id,session_id,account,talker,source,encrypted_snapshot) VALUES(1,?1,?2,?3,?4,?5,?6)",
        params![view.channel_id, view.session_id, view.account, view.talker, view.source, encrypted_snapshot as i64])?;
    tx.commit()?;
    Ok(view)
}

fn trusted_self_wxid(
    account_id: &str,
    copied_base: &str,
    stored_base: Option<&str>,
) -> Option<String> {
    let parsed = wx_keychain::AccountId::parse(account_id);
    // A legacy account directory may end in a four-character hash suffix.
    // A stored copy of the raw directory name is not independent confirmation.
    if parsed.alias_candidate().is_some() && !parsed.has_stripped_suffix() {
        return stored_base
            .filter(|stored| Some(*stored) == parsed.alias_candidate())
            .map(str::to_owned);
    }
    if let Some(stored) = stored_base.filter(|value| !value.is_empty() && parsed.matches(value)) {
        return Some(stored.to_owned());
    }
    (copied_base == parsed.base()).then(|| copied_base.to_owned())
}

fn read_source(
    state_dir: &Path,
    binding: &BindingView,
) -> Result<(MessageQueryResult, Option<String>), Box<dyn Error>> {
    let captured = if binding.encrypted_snapshot {
        Some(snapshot::capture(state_dir, Path::new(&binding.source))?)
    } else {
        None
    };
    let (db, self_wxid) = if let Some(ref copy) = captured {
        let account = wx_context::AccountContext::resolve(&wx_context::ResolveParams {
            account: None,
            data_dir: Some(copy.account_dir()),
            key_hex: None,
        })?;
        if account.account_id != binding.account || account.raw_key.is_none() {
            return Err(invalid("bound account has no matching raw key"));
        }
        let store = wx_keychain::KeyStore::load_default()?;
        let stored_base = store
            .get(&account.account_id)
            .and_then(|key| key.base_wxid.as_deref());
        let self_wxid = trusted_self_wxid(&account.account_id, &account.base_wxid, stored_base);
        (wx_context::open_encrypted_db(&account)?, self_wxid)
    } else {
        // A legacy decrypted binding must not accidentally open a live,
        // encrypted WeChat database through SQLite's shared-memory path.
        for relative in ["contact/contact.db", "session/session.db"] {
            let mut header = [0_u8; 16];
            File::open(Path::new(&binding.source).join(relative))?.read_exact(&mut header)?;
            if &header != b"SQLite format 3\0" {
                return Err(invalid(
                    "source is not a decrypted SQLite database; use --encrypted-snapshot",
                ));
            }
        }
        (WechatDb::open(&binding.source)?, None)
    };
    let q = MessageQuery::for_talker(&binding.talker)
        .order(SortOrder::Asc)
        .limit(MAX_QUERY_LIMIT)
        .with_filtered_count(true);
    let result = db.query_messages(&q)?;
    if result.items.is_empty() && result.stats.filtered_count == Some(0) {
        // A missing per-group Msg table also produces an empty result. Require
        // independent evidence that this exact group exists before accepting
        // an empty scan. An absent chat_room schema is likewise inconclusive.
        let rooms = db.query_chatrooms(&ChatRoomQuery::new().username(&binding.talker).limit(1))?;
        if rooms.items.len() != 1 {
            return Err(invalid(
                "empty group scan cannot be verified against chat_room",
            ));
        }
    }
    Ok((result, self_wxid))
}

/// Validate the entire scan before mutating the journal. A capped or partially decoded
/// query cannot establish that every local group message was observed.
fn validate_batch(
    binding: &BindingView,
    result: &MessageQueryResult,
) -> Result<Vec<(i64, String)>, Box<dyn Error>> {
    if result.stats.skipped != 0
        || !result.shard_warnings.is_empty()
        || result.stats.filtered_count.is_none()
        || result.stats.filtered_count.unwrap_or(0) > MAX_QUERY_LIMIT
        || result.stats.filtered_count != Some(result.items.len())
    {
        return Err(invalid(
            "incomplete local scan; journal and cursor unchanged",
        ));
    }
    let mut batch = BTreeMap::new();
    for message in &result.items {
        if message.server_id <= 0 || message.talker != binding.talker {
            return Err(invalid(
                "message lacks a positive server_id or has wrong talker; journal unchanged",
            ));
        }
        let json = serde_json::to_string(message)?;
        let ordered = (
            message.sort_seq,
            message.create_time,
            message.server_id,
            json.clone(),
        );
        if let Some(previous) = batch.insert(message.server_id, ordered) {
            if previous.3 != json {
                return Err(invalid(
                    "contradictory rows for one server_id; journal unchanged",
                ));
            }
        }
    }
    let mut ordered: Vec<_> = batch.into_values().collect();
    ordered.sort_unstable_by_key(|row| (row.0, row.1, row.2));
    Ok(ordered
        .into_iter()
        .map(|(_, _, id, json)| (id, json))
        .collect())
}

fn apply_batch(
    tx: &rusqlite::Transaction<'_>,
    batch: Vec<(i64, String)>,
) -> Result<usize, Box<dyn Error>> {
    let mut appended = 0;
    for (server_id, snapshot) in batch {
        let existing: Option<String> = tx
            .query_row(
                "SELECT snapshot FROM channel_messages WHERE server_id=?1",
                [server_id],
                |r| r.get(0),
            )
            .optional()?;
        let kind = match existing {
            None => "message_observed",
            Some(ref old) if old == &snapshot => continue,
            Some(_) => "message_updated",
        };
        tx.execute(
            "INSERT INTO channel_messages(server_id,snapshot) VALUES(?1,?2)
            ON CONFLICT(server_id) DO UPDATE SET snapshot=excluded.snapshot",
            params![server_id, snapshot],
        )?;
        tx.execute(
            "INSERT INTO channel_events(kind,server_id,message_json) VALUES(?1,?2,?3)",
            params![kind, server_id, snapshot],
        )?;
        appended += 1;
    }
    Ok(appended)
}

#[cfg(test)]
fn sync_result(
    conn: &mut Connection,
    binding: &BindingView,
    result: &MessageQueryResult,
) -> Result<usize, Box<dyn Error>> {
    let batch = validate_batch(binding, result)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let appended = apply_batch(&tx, batch)?;
    tx.commit()?;
    Ok(appended)
}

fn sync(state_dir: &Path) -> Result<usize, Box<dyn Error>> {
    let loaded = load_binding(state_dir)?;
    if loaded.encrypted_snapshot {
        ensure_disjoint_state(state_dir, Path::new(&loaded.source))?;
    }
    let mut conn = open_state(state_dir, true)?;
    // SQLite's write reservation is taken before reading the source. Another
    // sync cannot read a newer source and then be overwritten by an older scan.
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let binding = read_binding(&tx)?;
    let (result, self_wxid) = read_source(state_dir, &binding)?;
    if let Some(ref self_wxid) = self_wxid {
        if binding
            .self_wxid
            .as_ref()
            .is_some_and(|saved| saved != self_wxid)
        {
            return Err(invalid("bound self identity changed; journal unchanged"));
        }
    }
    let batch = validate_batch(&binding, &result)?;
    let appended = apply_batch(&tx, batch)?;
    if let Some(self_wxid) = self_wxid {
        tx.execute(
            "UPDATE channel_binding SET self_wxid=?1 WHERE singleton=1 AND self_wxid IS NULL",
            [self_wxid],
        )?;
    }
    tx.commit()?;
    Ok(appended)
}

#[derive(Serialize)]
struct EventLine<'a> {
    cursor: i64,
    kind: &'a str,
    channel_id: &'a str,
    session_id: &'a str,
    account: &'a str,
    talker: &'a str,
    server_id: i64,
    direction: &'static str,
    coverage: &'static str,
    history_complete: bool,
    message: serde_json::Value,
}

fn event_page(
    conn: &Connection,
    binding: &BindingView,
    after: i64,
    limit: usize,
) -> Result<Vec<(i64, String)>, Box<dyn Error>> {
    if after < 0 || limit == 0 || limit > 1000 {
        return Err(invalid("after must be nonnegative and limit 1..1000"));
    }
    let mut stmt = conn.prepare("SELECT cursor,kind,server_id,message_json FROM channel_events WHERE cursor>?1 ORDER BY cursor ASC LIMIT ?2")?;
    let rows = stmt.query_map(params![after, limit as i64], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, i64>(2)?,
            r.get::<_, String>(3)?,
        ))
    })?;
    let mut lines = Vec::new();
    for row in rows {
        let (cursor, kind, server_id, json) = row?;
        let message: serde_json::Value = serde_json::from_str(&json)?;
        let direction = match (
            binding.self_wxid.as_deref(),
            message["sender"]
                .as_str()
                .filter(|sender| !sender.is_empty()),
        ) {
            (Some(self_wxid), Some(sender)) if sender == self_wxid => "outgoing",
            (Some(_), Some(_)) => "incoming",
            _ => "unknown",
        };
        lines.push((
            cursor,
            serde_json::to_string(&EventLine {
                cursor,
                kind: &kind,
                channel_id: &binding.channel_id,
                session_id: &binding.session_id,
                account: &binding.account,
                talker: &binding.talker,
                server_id,
                direction,
                coverage: "bounded_local_database",
                history_complete: false,
                message,
            })?,
        ));
    }
    Ok(lines)
}

fn event_lines(
    conn: &Connection,
    binding: &BindingView,
    after: i64,
    limit: usize,
) -> Result<Vec<String>, Box<dyn Error>> {
    Ok(event_page(conn, binding, after, limit)?
        .into_iter()
        .map(|(_, line)| line)
        .collect())
}

fn consumer_cursor(conn: &Connection, consumer: &str) -> Result<i64, Box<dyn Error>> {
    Ok(conn
        .query_row(
            "SELECT cursor FROM channel_consumers WHERE consumer=?1",
            [consumer],
            |r| r.get(0),
        )
        .optional()?
        .unwrap_or(0))
}

/// The in-process cursor only suppresses duplicate output during this run.
/// Recovery starts from the durable, explicitly acknowledged consumer cursor.
fn drain_events<W: Write>(
    conn: &Connection,
    binding: &BindingView,
    emitted_cursor: &mut i64,
    out: &mut W,
) -> Result<usize, Box<dyn Error>> {
    let mut emitted = 0;
    loop {
        let page = event_page(conn, binding, *emitted_cursor, 1000)?;
        if page.is_empty() {
            break;
        }
        let page_len = page.len();
        for (cursor, line) in page {
            writeln!(out, "{line}")?;
            out.flush()?;
            *emitted_cursor = cursor;
            emitted += 1;
        }
        if page_len < 1000 {
            break;
        }
    }
    Ok(emitted)
}

fn ack(
    conn: &mut Connection,
    consumer: &str,
    cursor: i64,
    expected_cursor: Option<i64>,
) -> Result<(), Box<dyn Error>> {
    if consumer.trim().is_empty() || cursor < 0 {
        return Err(invalid("consumer and nonnegative cursor required"));
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let previous: i64 = tx
        .query_row(
            "SELECT cursor FROM channel_consumers WHERE consumer=?1",
            [consumer],
            |r| r.get(0),
        )
        .optional()?
        .unwrap_or(0);
    if expected_cursor.is_some_and(|expected| expected != previous) {
        return Err(invalid("consumer cursor changed; expected cursor mismatch"));
    }
    let latest: i64 = tx.query_row(
        "SELECT COALESCE(MAX(cursor),0) FROM channel_events",
        [],
        |r| r.get(0),
    )?;
    if cursor < previous || cursor > latest {
        return Err(invalid("ack regression or future cursor refused"));
    }
    tx.execute(
        "INSERT INTO channel_consumers(consumer,cursor) VALUES(?1,?2)
        ON CONFLICT(consumer) DO UPDATE SET cursor=excluded.cursor",
        params![consumer, cursor],
    )?;
    tx.commit()?;
    Ok(())
}

fn print_lines(lines: Vec<String>) -> Result<(), Box<dyn Error>> {
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for line in lines {
        writeln!(out, "{line}")?;
        out.flush()?;
    }
    Ok(())
}

pub fn run(state_dir: &Path, action: ChannelAction) -> Result<(), Box<dyn Error>> {
    match action {
        ChannelAction::Bind {
            account,
            talker,
            session,
            source,
            encrypted_snapshot,
        } => {
            let view = bind(
                state_dir,
                account,
                talker,
                session,
                source,
                encrypted_snapshot,
            )?;
            println!("{}", serde_json::to_string(&view)?);
        }
        ChannelAction::Status { consumer } => {
            let conn = open_state(state_dir, false)?;
            let binding = read_binding(&conn)?;
            let latest: i64 = conn.query_row(
                "SELECT COALESCE(MAX(cursor),0) FROM channel_events",
                [],
                |r| r.get(0),
            )?;
            let mut result = serde_json::json!({"binding":binding,"latest_cursor":latest,"coverage":"bounded_local_database","history_complete":false});
            if let Some(consumer) = consumer {
                if consumer.trim().is_empty() {
                    return Err(invalid("consumer required"));
                }
                result["consumer_cursor"] = serde_json::json!(consumer_cursor(&conn, &consumer)?);
            }
            println!("{result}");
        }
        ChannelAction::Sync => {
            let count = sync(state_dir)?;
            println!(
                "{}",
                serde_json::json!({"events_appended":count,"coverage":"bounded_local_database","history_complete":false})
            );
        }
        ChannelAction::Events { after, limit } => {
            let conn = open_state(state_dir, false)?;
            print_lines(event_lines(&conn, &read_binding(&conn)?, after, limit)?)?;
        }
        ChannelAction::Ack {
            consumer,
            cursor,
            expected_cursor,
        } => {
            let binding = load_binding(state_dir)?;
            let mut conn = open_state(state_dir, true)?;
            ack(&mut conn, &consumer, cursor, expected_cursor)?;
            println!(
                "{}",
                serde_json::json!({"channel_id":binding.channel_id,"session_id":binding.session_id,"consumer":consumer,"cursor":cursor})
            );
        }
        ChannelAction::Subscribe {
            consumer,
            poll_ms,
            once,
        } => {
            if consumer.trim().is_empty() || poll_ms < 1000 {
                return Err(invalid("consumer required and poll-ms must be >=1000"));
            }
            let mut emitted_cursor = {
                let conn = open_state(state_dir, false)?;
                read_binding(&conn)?;
                consumer_cursor(&conn, &consumer)?
            };
            loop {
                sync(state_dir)?;
                let conn = open_state(state_dir, false)?;
                let binding = read_binding(&conn)?;
                emitted_cursor = emitted_cursor.max(consumer_cursor(&conn, &consumer)?);
                let stdout = std::io::stdout();
                drain_events(&conn, &binding, &mut emitted_cursor, &mut stdout.lock())?;
                if once {
                    break;
                }
                thread::sleep(Duration::from_millis(poll_ms));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;
    use wx_db::{Message, MessageContent, QueryStats};

    fn setup() -> (TempDir, TempDir, BindingView) {
        let state = TempDir::new().unwrap();
        fs::set_permissions(state.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let source = TempDir::new().unwrap();
        let binding = bind(
            state.path(),
            "local-a".into(),
            "group@chatroom".into(),
            "agent-1".into(),
            source.path().to_path_buf(),
            false,
        )
        .unwrap();
        (state, source, binding)
    }

    fn msg(id: i64, seq: i64, text: &str) -> Message {
        Message {
            sort_seq: seq,
            server_id: id,
            msg_type: 1,
            sub_type: 0,
            sender: "member".into(),
            talker: "group@chatroom".into(),
            create_time: 100,
            content: MessageContent::Text(text.into()),
            status: 0,
        }
    }

    fn batch(items: Vec<Message>) -> MessageQueryResult {
        let count = items.len();
        MessageQueryResult {
            items,
            stats: QueryStats {
                total_rows: count,
                filtered_count: Some(count),
                skipped: 0,
            },
            shard_warnings: vec![],
        }
    }

    fn cursor(conn: &Connection) -> i64 {
        conn.query_row(
            "SELECT COALESCE(MAX(cursor),0) FROM channel_events",
            [],
            |r| r.get(0),
        )
        .unwrap()
    }

    #[test]
    fn immutable_binding_and_private_files() {
        let (state, source, binding) = setup();
        let same = bind(
            state.path(),
            "local-a".into(),
            "group@chatroom".into(),
            "agent-1".into(),
            source.path().to_path_buf(),
            false,
        )
        .unwrap();
        assert_eq!(binding.channel_id, same.channel_id);
        assert!(bind(
            state.path(),
            "local-b".into(),
            "group@chatroom".into(),
            "agent-1".into(),
            source.path().to_path_buf(),
            false,
        )
        .is_err());
        assert_eq!(
            load_binding(state.path()).unwrap().channel_id,
            binding.channel_id
        );
        assert_eq!(
            fs::metadata(state.path()).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(state.path().join(DB_NAME))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }

    #[test]
    fn old_binding_remains_decrypted_after_schema_migration() {
        let state = TempDir::new().unwrap();
        fs::set_permissions(state.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let db_path = state.path().join(DB_NAME);
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("CREATE TABLE channel_binding (singleton INTEGER PRIMARY KEY, channel_id TEXT NOT NULL, session_id TEXT NOT NULL, account TEXT NOT NULL, talker TEXT NOT NULL, source TEXT NOT NULL); INSERT INTO channel_binding VALUES (1,'old','session','account','group@chatroom','/decrypted');").unwrap();
        drop(conn);
        fs::set_permissions(&db_path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(!load_binding(state.path()).unwrap().encrypted_snapshot);
        let conn = open_state(state.path(), true).unwrap();
        let binding = read_binding(&conn).unwrap();
        assert_eq!(binding.channel_id, "old");
        assert!(!binding.encrypted_snapshot);
        assert!(binding.self_wxid.is_none());
    }

    #[test]
    fn legacy_account_direction_waits_for_confirmed_identity() {
        assert_eq!(
            trusted_self_wxid("testuser001_1662", "testuser001_1662", None),
            None
        );
        assert_eq!(
            trusted_self_wxid("testuser001_1662", "testuser001_1662", Some("testuser001")),
            Some("testuser001".to_owned())
        );
        assert_eq!(
            trusted_self_wxid("wxid_me_ab12", "wxid_me", None),
            Some("wxid_me".to_owned())
        );
        assert_eq!(
            trusted_self_wxid("testuser001_1662", "testuser001_1662", Some("unrelated")),
            None
        );
        assert_eq!(
            trusted_self_wxid(
                "testuser001_1662",
                "testuser001_1662",
                Some("testuser001_1662")
            ),
            None
        );
    }

    #[test]
    fn encrypted_binding_refuses_state_inside_source_account() {
        let source = TempDir::new().unwrap();
        let account = source.path().canonicalize().unwrap().join("wxid_test_123");
        for dir in ["contact", "session", "message"] {
            fs::create_dir_all(account.join("db_storage").join(dir)).unwrap();
        }
        let state = account.join("channel-state");
        assert!(bind(
            &state,
            "wxid_test_123".into(),
            "group@chatroom".into(),
            "session".into(),
            account.clone(),
            true
        )
        .is_err());
        assert!(!state.exists());
        assert!(ensure_disjoint_state(source.path(), &account).is_err());
    }

    #[test]
    fn dedup_uses_server_id_and_rescan_finds_late_same_sequence() {
        let (state, _source, binding) = setup();
        let mut conn = open_state(state.path(), true).unwrap();
        assert_eq!(
            sync_result(
                &mut conn,
                &binding,
                &batch(vec![
                    msg(1, 10, "same"),
                    msg(1, 10, "same"),
                    msg(2, 10, "same")
                ])
            )
            .unwrap(),
            2
        );
        assert_eq!(
            sync_result(
                &mut conn,
                &binding,
                &batch(vec![msg(1, 10, "same"), msg(2, 10, "same")])
            )
            .unwrap(),
            0
        );
        assert_eq!(
            sync_result(
                &mut conn,
                &binding,
                &batch(vec![
                    msg(1, 10, "same"),
                    msg(2, 10, "same"),
                    msg(3, 9, "late")
                ])
            )
            .unwrap(),
            1
        );
        assert_eq!(cursor(&conn), 3);
    }

    #[test]
    fn journal_cursors_follow_message_chronology_not_server_id() {
        let (state, _source, binding) = setup();
        let mut conn = open_state(state.path(), true).unwrap();
        sync_result(
            &mut conn,
            &binding,
            &batch(vec![msg(900, 1, "first"), msg(10, 2, "second")]),
        )
        .unwrap();
        let ids: Vec<i64> = conn
            .prepare("SELECT server_id FROM channel_events ORDER BY cursor")
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        assert_eq!(ids, vec![900, 10]);
    }

    #[test]
    fn read_rejects_permissive_state_without_changing_it() {
        let (state, _source, _binding) = setup();
        fs::set_permissions(state.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(load_binding(state.path()).is_err());
        assert_eq!(
            fs::metadata(state.path()).unwrap().permissions().mode() & 0o777,
            0o755
        );
        fs::set_permissions(state.path(), fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(
            state.path().join(DB_NAME),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        assert!(load_binding(state.path()).is_err());
        assert_eq!(
            fs::metadata(state.path().join(DB_NAME))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o644
        );
    }

    #[test]
    fn update_is_distinct_event_and_latest_snapshot() {
        let (state, _source, binding) = setup();
        let mut conn = open_state(state.path(), true).unwrap();
        sync_result(&mut conn, &binding, &batch(vec![msg(1, 1, "before")])).unwrap();
        sync_result(&mut conn, &binding, &batch(vec![msg(1, 1, "after")])).unwrap();
        let lines = event_lines(&conn, &binding, 0, 10).unwrap();
        assert_eq!(lines.len(), 2);
        assert!(lines[1].contains("message_updated"));
        assert!(lines[1].contains("after"));
        assert!(lines[1].contains("\"direction\":\"unknown\""));
        let snapshot: String = conn
            .query_row(
                "SELECT snapshot FROM channel_messages WHERE server_id=1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(snapshot.contains("after"));
    }

    #[test]
    fn invalid_or_partial_batch_is_atomic() {
        let (state, _source, binding) = setup();
        let mut conn = open_state(state.path(), true).unwrap();
        let invalid_id = batch(vec![msg(1, 1, "okay"), msg(0, 2, "bad")]);
        assert!(sync_result(&mut conn, &binding, &invalid_id).is_err());
        let contradictory = batch(vec![msg(1, 1, "a"), msg(1, 1, "b")]);
        assert!(sync_result(&mut conn, &binding, &contradictory).is_err());
        let mut partial = batch(vec![msg(1, 1, "a")]);
        partial.stats.skipped = 1;
        assert!(sync_result(&mut conn, &binding, &partial).is_err());
        partial.stats.skipped = 0;
        partial.stats.filtered_count = Some(MAX_QUERY_LIMIT + 1);
        assert!(sync_result(&mut conn, &binding, &partial).is_err());
        assert_eq!(cursor(&conn), 0);
    }

    #[test]
    fn restart_replays_until_explicit_ack_and_rejects_bad_ack() {
        let (state, _source, binding) = setup();
        let mut conn = open_state(state.path(), true).unwrap();
        sync_result(
            &mut conn,
            &binding,
            &batch(vec![msg(1, 1, "one"), msg(2, 2, "two")]),
        )
        .unwrap();
        drop(conn);
        let mut conn = open_state(state.path(), true).unwrap();
        assert_eq!(event_lines(&conn, &binding, 0, 10).unwrap().len(), 2);
        ack(&mut conn, "worker", 1, Some(0)).unwrap();
        assert_eq!(event_lines(&conn, &binding, 1, 10).unwrap().len(), 1);
        assert!(ack(&mut conn, "worker", 0, None).is_err());
        assert!(ack(&mut conn, "worker", 3, None).is_err());
        assert!(ack(&mut conn, "worker", 2, Some(0)).is_err());
        assert_eq!(consumer_cursor(&conn, "worker").unwrap(), 1);
        ack(&mut conn, "worker", 2, Some(1)).unwrap();
        assert_eq!(consumer_cursor(&conn, "worker").unwrap(), 2);
        assert_eq!(
            conn.query_row(
                "SELECT cursor FROM channel_consumers WHERE consumer='worker'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            2
        );
    }

    #[test]
    fn subscription_drains_all_pages_once_per_process_and_replays_on_restart() {
        let (state, _source, binding) = setup();
        let mut conn = open_state(state.path(), true).unwrap();
        let messages = (1..=1001).map(|id| msg(id, id, "item")).collect();
        sync_result(&mut conn, &binding, &batch(messages)).unwrap();
        let mut cursor = consumer_cursor(&conn, "worker").unwrap();
        let mut output = Vec::new();
        assert_eq!(
            drain_events(&conn, &binding, &mut cursor, &mut output).unwrap(),
            1001
        );
        assert_eq!(output.iter().filter(|&&byte| byte == b'\n').count(), 1001);
        assert_eq!(
            drain_events(&conn, &binding, &mut cursor, &mut output).unwrap(),
            0
        );
        assert_eq!(consumer_cursor(&conn, "worker").unwrap(), 0);
        drop(conn);
        let conn = open_state(state.path(), false).unwrap();
        let mut restarted = consumer_cursor(&conn, "worker").unwrap();
        let mut replay = Vec::new();
        assert_eq!(
            drain_events(&conn, &binding, &mut restarted, &mut replay).unwrap(),
            1001
        );
    }
}
