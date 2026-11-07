//! Explicit, epoch-bound exceptions to the historical dedicated Session scope.
//!
//! While two bindings share a real Session ref, coordinator operations require
//! this runtime. Older binaries try to recreate the dedicated unique index and
//! fail closed on those duplicate refs.
use std::error::Error;
use std::io;

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};

fn invalid(message: &str) -> Box<dyn Error> {
    io::Error::new(io::ErrorKind::InvalidInput, message).into()
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

/// Called after the coordinator's core tables and binding.session_ref exist.
pub(super) fn ensure_schema(conn: &Connection) -> Result<(), Box<dyn Error>> {
    // The coordinator opens this database for every queue operation. Trust the
    // schema objects created by this private database's atomic migration and
    // avoid taking a write lock or replacing triggers on steady-state opens.
    let (installed, old_unique_index): (i64, bool) = conn.query_row(
        "SELECT
          (SELECT count(*) FROM sqlite_master WHERE
            (type='table' AND name IN ('session_scope','shared_session_scope'))
            OR (type='index' AND name='session_binding_ref')
            OR (type='trigger' AND name IN (
              'session_scope_immutable','session_scope_retained',
              'shared_session_scope_immutable','shared_session_scope_retained',
              'shared_binding_insert_fence','shared_binding_update_fence',
              'dedicated_session_scope','shared_session_reservation_fence',
              'shared_session_slot_fence'))),
          EXISTS(SELECT 1 FROM sqlite_master WHERE type='index'
            AND name='dedicated_session_per_conversation')",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if installed == 12 {
        if old_unique_index {
            // An older binary may have recreated this index while no duplicate
            // binding existed. Its own open fails closed once sharing is live.
            conn.execute_batch("DROP INDEX dedicated_session_per_conversation;")?;
        }
        return Ok(());
    }
    conn.execute_batch(
        "BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS shared_session_scope (
          session_ref TEXT NOT NULL, conversation_id TEXT NOT NULL,
          owner TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch > 0),
          recorded_at INTEGER NOT NULL,
          PRIMARY KEY(session_ref,conversation_id,owner,epoch)
        );
        CREATE TRIGGER IF NOT EXISTS shared_session_scope_immutable
          BEFORE UPDATE ON shared_session_scope BEGIN
          SELECT RAISE(ABORT,'Shared Session grant is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS shared_session_scope_retained
          BEFORE DELETE ON shared_session_scope BEGIN
          SELECT RAISE(ABORT,'Shared Session grant history must be retained'); END;
        CREATE TABLE IF NOT EXISTS session_scope (
          session_ref TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
          created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        INSERT OR IGNORE INTO session_scope(session_ref,conversation_id)
          SELECT b.session_ref,b.conversation_id FROM binding b
          WHERE b.session_ref IS NOT NULL
            AND (SELECT count(*) FROM binding other WHERE other.session_ref=b.session_ref)=1
            AND NOT EXISTS (SELECT 1 FROM shared_session_scope g
              WHERE g.session_ref=b.session_ref AND g.conversation_id=b.conversation_id);
        CREATE TRIGGER IF NOT EXISTS session_scope_immutable
          BEFORE UPDATE ON session_scope BEGIN
          SELECT RAISE(ABORT,'Session conversation scope is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS session_scope_retained
          BEFORE DELETE ON session_scope BEGIN
          SELECT RAISE(ABORT,'Session conversation scope history must be retained'); END;
        DROP INDEX IF EXISTS dedicated_session_per_conversation;
        CREATE INDEX IF NOT EXISTS session_binding_ref ON binding(session_ref)
          WHERE session_ref IS NOT NULL;
        CREATE TRIGGER IF NOT EXISTS shared_binding_insert_fence
          BEFORE INSERT ON binding WHEN NEW.session_ref IS NOT NULL BEGIN
          SELECT CASE WHEN EXISTS (
            SELECT 1 FROM binding other WHERE other.session_ref=NEW.session_ref
          ) AND (
            NOT EXISTS (SELECT 1 FROM shared_session_scope g
              WHERE g.session_ref=NEW.session_ref AND g.conversation_id=NEW.conversation_id
                AND g.owner=NEW.owner AND g.epoch=NEW.epoch AND NEW.control='active')
            OR EXISTS (SELECT 1 FROM binding other
              WHERE other.session_ref=NEW.session_ref AND NOT EXISTS (
                SELECT 1 FROM shared_session_scope g
                WHERE g.session_ref=other.session_ref AND g.conversation_id=other.conversation_id
                  AND g.owner=other.owner AND g.epoch=other.epoch AND other.control='active'))
          ) THEN RAISE(ABORT,'Session already belongs to another binding') END;
        END;
        CREATE TRIGGER IF NOT EXISTS shared_binding_update_fence
          BEFORE UPDATE OF session_ref ON binding WHEN NEW.session_ref IS NOT NULL BEGIN
          SELECT CASE WHEN EXISTS (
            SELECT 1 FROM binding other WHERE other.session_ref=NEW.session_ref
              AND other.conversation_id<>NEW.conversation_id
          ) AND (
            NOT EXISTS (SELECT 1 FROM shared_session_scope g
              WHERE g.session_ref=NEW.session_ref AND g.conversation_id=NEW.conversation_id
                AND g.owner=NEW.owner AND g.epoch=NEW.epoch AND NEW.control='active')
            OR EXISTS (SELECT 1 FROM binding other
              WHERE other.session_ref=NEW.session_ref
                AND other.conversation_id<>NEW.conversation_id AND NOT EXISTS (
                SELECT 1 FROM shared_session_scope g
                WHERE g.session_ref=other.session_ref AND g.conversation_id=other.conversation_id
                  AND g.owner=other.owner AND g.epoch=other.epoch AND other.control='active'))
          ) THEN RAISE(ABORT,'Session already belongs to another binding') END;
        END;
        DROP TRIGGER IF EXISTS dedicated_session_scope;
        CREATE TRIGGER dedicated_session_scope AFTER UPDATE OF session_ref ON binding
          WHEN NEW.session_ref IS NOT NULL BEGIN
          INSERT OR IGNORE INTO session_scope(session_ref,conversation_id)
            SELECT NEW.session_ref,NEW.conversation_id WHERE NOT EXISTS (
              SELECT 1 FROM shared_session_scope g
              WHERE g.session_ref=NEW.session_ref AND g.conversation_id=NEW.conversation_id
                AND g.owner=NEW.owner AND g.epoch=NEW.epoch AND NEW.control='active');
          SELECT CASE WHEN
            (SELECT conversation_id FROM session_scope WHERE session_ref=NEW.session_ref)<>NEW.conversation_id
            AND NOT EXISTS (
              SELECT 1 FROM shared_session_scope g
              WHERE g.session_ref=NEW.session_ref AND g.conversation_id=NEW.conversation_id
                AND g.owner=NEW.owner AND g.epoch=NEW.epoch AND NEW.control='active'
            ) THEN RAISE(ABORT,'Session belongs to another conversation') END;
        END;
        CREATE TRIGGER IF NOT EXISTS shared_session_reservation_fence
          BEFORE UPDATE OF turn_state,session_ref ON delivery
          WHEN NEW.turn_state='reserved' AND NEW.session_ref IS NOT NULL
            AND EXISTS (SELECT 1 FROM shared_session_scope history
              WHERE history.session_ref=NEW.session_ref)
          BEGIN
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM shared_session_scope g
            JOIN binding b ON b.conversation_id=NEW.conversation_id
            WHERE g.session_ref=NEW.session_ref AND g.conversation_id=NEW.conversation_id
              AND g.owner=NEW.owner AND g.epoch=NEW.epoch
              AND b.owner=NEW.owner AND b.epoch=NEW.epoch
              AND b.control='active' AND b.session_ref=NEW.session_ref
          ) THEN RAISE(ABORT,'Shared Session grant is absent or stale') END;
        END;
        CREATE TRIGGER IF NOT EXISTS shared_session_slot_fence
          BEFORE UPDATE OF turn_state,session_ref ON delivery
          WHEN NEW.turn_state='reserved' AND NEW.session_ref IS NOT NULL
            AND (OLD.turn_state IS NOT 'reserved' OR OLD.session_ref IS NOT NEW.session_ref)
            AND EXISTS (SELECT 1 FROM shared_session_scope history
              WHERE history.session_ref=NEW.session_ref)
          BEGIN
          SELECT CASE WHEN EXISTS (
            SELECT 1 FROM delivery other
            LEFT JOIN binding b ON b.conversation_id=other.conversation_id
            WHERE other.id<>NEW.id AND (
              (other.session_ref=NEW.session_ref
                AND other.turn_state IN ('reserved','recorded'))
              OR (other.status='claimed' AND other.turn_state IS NULL
                AND other.lease_until>strftime('%s','now')
                AND b.owner=other.owner AND b.epoch=other.epoch AND b.control='active'
                AND (other.session_ref=NEW.session_ref OR b.session_ref=NEW.session_ref
                  OR EXISTS (SELECT 1 FROM shared_session_scope g
                    WHERE g.session_ref=NEW.session_ref
                      AND g.conversation_id=b.conversation_id
                      AND g.owner=b.owner AND g.epoch=b.epoch)))
            )
          ) THEN RAISE(ABORT,'Shared Session already has unfinished notification') END;
        END;
        COMMIT;",
    )?;
    Ok(())
}

pub(super) fn has_history(conn: &Connection, session: &str) -> Result<bool, Box<dyn Error>> {
    check_id(session)?;
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM shared_session_scope WHERE session_ref=?1)",
        [session],
        |r| r.get::<_, bool>(0),
    )?)
}

pub(super) fn allowed(
    conn: &Connection,
    session: &str,
    conversation: &str,
    owner: &str,
    epoch: i64,
) -> Result<bool, Box<dyn Error>> {
    check_id(session)?;
    check_id(conversation)?;
    check_id(owner)?;
    if epoch <= 0 {
        return Err(invalid("epoch must be positive"));
    }
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM shared_session_scope g
          JOIN binding b ON b.conversation_id=g.conversation_id
          WHERE g.session_ref=?1 AND g.conversation_id=?2 AND g.owner=?3 AND g.epoch=?4
            AND b.owner=g.owner AND b.epoch=g.epoch AND b.control='active'
            AND (b.session_ref IS NULL OR b.session_ref=g.session_ref))",
        params![session, conversation, owner, epoch],
        |r| r.get::<_, bool>(0),
    )?)
}

pub(super) fn authorize(
    conn: &mut Connection,
    session: &str,
    conversation: &str,
    owner: &str,
    expected_epoch: i64,
    reason: &str,
) -> Result<Value, Box<dyn Error>> {
    check_id(session)?;
    check_id(conversation)?;
    check_id(owner)?;
    if expected_epoch <= 0 {
        return Err(invalid("expected epoch must be positive"));
    }
    if reason.trim().is_empty() || reason.len() > 4096 || reason.chars().any(|c| c.is_control()) {
        return Err(invalid(
            "reason must be nonempty printable text within 4096 bytes",
        ));
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let binding: Option<(String, i64, String, Option<String>)> = tx
        .query_row(
            "SELECT owner,epoch,control,session_ref FROM binding WHERE conversation_id=?1",
            [conversation],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let Some((current_owner, current_epoch, control, bound_session)) = binding else {
        return Err(invalid("conversation is not registered"));
    };
    if current_owner != owner
        || current_epoch != expected_epoch
        || control != "active"
        || bound_session
            .as_deref()
            .is_some_and(|bound| bound != session)
    {
        return Err(invalid(
            "conversation owner, epoch, control, or Session binding changed",
        ));
    }
    let inserted = tx.execute(
        "INSERT OR IGNORE INTO shared_session_scope
          (session_ref,conversation_id,owner,epoch,recorded_at)
          VALUES(?1,?2,?3,?4,strftime('%s','now'))",
        params![session, conversation, owner, expected_epoch],
    )? == 1;
    if inserted {
        tx.execute(
            "INSERT INTO control_event(conversation_id,epoch,action,reason,recorded_at)
              VALUES(?1,?2,'authorize_shared_session',?3,strftime('%s','now'))",
            params![conversation, expected_epoch, reason],
        )?;
    }
    tx.commit()?;
    Ok(
        json!({"session_ref":session,"conversation_id":conversation,"owner":owner,
        "epoch":expected_epoch,"authorized":true,"already_authorized":!inserted}),
    )
}
