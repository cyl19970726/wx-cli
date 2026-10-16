//! Explicit, metadata-only discovery of chatrooms from a private encrypted copy.
use std::error::Error;
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

use clap::Subcommand;
use serde::Serialize;
use wx_context::{open_encrypted_db_core, AccountContext, ContactResolver, ResolveParams};
use wx_db::{ChatRoomQuery, ContactQuery, WechatDb, MAX_QUERY_LIMIT};

use super::channel_snapshot;

#[derive(Debug, Subcommand)]
pub enum GroupsAction {
    /// Find chatrooms by their preferred display name; all ambiguous matches are returned.
    Find {
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        account: String,
        #[arg(long)]
        source: PathBuf,
        #[arg(long)]
        name: String,
        /// Return only exact display-name matches.
        #[arg(long)]
        exact: bool,
    },
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
struct Match {
    name: String,
    talker: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct FindResult {
    matches: Vec<Match>,
    match_kind: &'static str,
}

fn invalid(message: &str) -> Box<dyn Error> {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, message).into()
}

fn validate_inputs(
    state_dir: &Path,
    account: &str,
    source: &Path,
    name: &str,
) -> Result<(), Box<dyn Error>> {
    if !state_dir.is_absolute() || !source.is_absolute() {
        return Err(invalid("state-dir and source must be absolute"));
    }
    let mut prefix = PathBuf::new();
    for component in state_dir.components() {
        match component {
            Component::RootDir | Component::Prefix(_) => prefix.push(component.as_os_str()),
            Component::Normal(name) => {
                prefix.push(name);
                let metadata = fs::symlink_metadata(&prefix)?;
                if metadata.file_type().is_symlink() || !metadata.is_dir() {
                    return Err(invalid(
                        "state-dir path has a symlink or non-directory parent",
                    ));
                }
            }
            Component::ParentDir => return Err(invalid("state-dir path cannot contain ..")),
            Component::CurDir => {}
        }
    }
    if state_dir.starts_with(source) || source.starts_with(state_dir) {
        return Err(invalid("state-dir and source account cannot overlap"));
    }
    if account.is_empty()
        || Path::new(account).components().count() != 1
        || !matches!(
            Path::new(account).components().next(),
            Some(Component::Normal(_))
        )
    {
        return Err(invalid("account must be one directory name"));
    }
    if source.file_name().and_then(|s| s.to_str()) != Some(account) {
        return Err(invalid("source account directory does not match --account"));
    }
    if name.trim().is_empty() {
        return Err(invalid("nonempty group name required"));
    }
    let metadata = fs::symlink_metadata(state_dir)?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err(invalid(
            "state-dir must be a private owned directory (0700)",
        ));
    }
    Ok(())
}

fn verify_complete_contacts(db: &WechatDb) -> Result<(), Box<dyn Error>> {
    let mut offset = 0;
    loop {
        let page = db.query_contacts(&ContactQuery::new().limit(MAX_QUERY_LIMIT).offset(offset))?;
        let expected = page
            .stats
            .total_rows
            .saturating_sub(offset)
            .min(MAX_QUERY_LIMIT);
        if page.items.len() != expected || page.stats.skipped != 0 {
            return Err(invalid("contact metadata could not be read completely"));
        }
        offset += expected;
        if offset >= page.stats.total_rows {
            return Ok(());
        }
    }
}

fn collect_rooms(db: &WechatDb, resolver: &ContactResolver) -> Result<Vec<Match>, Box<dyn Error>> {
    let mut rooms = Vec::new();
    let mut offset = 0;
    loop {
        let page =
            db.query_chatrooms(&ChatRoomQuery::new().limit(MAX_QUERY_LIMIT).offset(offset))?;
        let expected = page
            .stats
            .total_rows
            .saturating_sub(offset)
            .min(MAX_QUERY_LIMIT);
        if page.items.len() != expected || page.stats.skipped != 0 {
            return Err(invalid("chatroom metadata could not be read completely"));
        }
        rooms.extend(page.items.into_iter().map(|room| Match {
            name: resolver.display_name(&room.username).to_string(),
            talker: room.username,
        }));
        offset += expected;
        if offset >= page.stats.total_rows {
            return Ok(rooms);
        }
    }
}

fn select_matches(rooms: Vec<Match>, name: &str, exact_only: bool) -> FindResult {
    let query = name.trim().to_lowercase();
    let exact: Vec<_> = rooms
        .iter()
        .filter(|room| room.name.to_lowercase() == query)
        .cloned()
        .collect();
    let (mut matches, match_kind) = if !exact.is_empty() {
        (exact, "exact")
    } else if exact_only {
        (Vec::new(), "none")
    } else {
        let partial: Vec<_> = rooms
            .into_iter()
            .filter(|room| room.name.to_lowercase().contains(&query))
            .collect();
        let kind = if partial.is_empty() {
            "none"
        } else {
            "partial"
        };
        (partial, kind)
    };
    matches.sort_by(|a, b| a.name.cmp(&b.name).then_with(|| a.talker.cmp(&b.talker)));
    FindResult {
        matches,
        match_kind,
    }
}

pub fn run(action: GroupsAction) -> Result<(), Box<dyn Error>> {
    match action {
        GroupsAction::Find {
            state_dir,
            account,
            source,
            name,
            exact,
        } => {
            validate_inputs(&state_dir, &account, &source, &name)?;
            let snapshot = channel_snapshot::capture_metadata(&state_dir, &source)?;
            let context = AccountContext::resolve(&ResolveParams {
                account: None,
                data_dir: Some(snapshot.account_dir()),
                key_hex: None,
            })?;
            if context.account_id != account {
                return Err(invalid("snapshot account does not match --account"));
            }
            let db = open_encrypted_db_core(&context)?;
            verify_complete_contacts(&db)?;
            let resolver = ContactResolver::build(&db)?;
            let rooms = collect_rooms(&db, &resolver)?;
            println!(
                "{}",
                serde_json::to_string(&select_matches(rooms, &name, exact))?
            );
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::c_void;

    fn encrypted_fixture(path: &Path, key: &[u8; 32], sql: &str) {
        let conn = rusqlite::Connection::open(path).unwrap();
        unsafe {
            assert_eq!(
                rusqlite::ffi::sqlite3_key(conn.handle(), key.as_ptr() as *const c_void, 32),
                0
            );
        }
        conn.execute_batch(sql).unwrap();
    }

    #[test]
    fn exact_names_take_priority_but_ambiguity_is_retained() {
        let rooms = vec![
            Match {
                name: "Team".into(),
                talker: "2@chatroom".into(),
            },
            Match {
                name: "Team Alpha".into(),
                talker: "3@chatroom".into(),
            },
            Match {
                name: "team".into(),
                talker: "1@chatroom".into(),
            },
        ];
        let found = select_matches(rooms, "TEAM", false);
        assert_eq!(found.match_kind, "exact");
        assert_eq!(found.matches.len(), 2);
        assert_eq!(found.matches[0].talker, "2@chatroom");
        assert_eq!(found.matches[1].talker, "1@chatroom");
    }

    #[test]
    fn partial_and_none_are_unambiguous_contract() {
        let rooms = vec![Match {
            name: "Team Alpha".into(),
            talker: "1@chatroom".into(),
        }];
        assert_eq!(
            select_matches(rooms.clone(), "team", false).match_kind,
            "partial"
        );
        assert_eq!(select_matches(rooms, "team", true).match_kind, "none");
    }

    #[test]
    fn unsafe_paths_and_mismatched_accounts_are_rejected() {
        let state = tempfile::TempDir::new().unwrap();
        let source = state.path().join("wxid_a");
        assert!(validate_inputs(state.path(), "../wxid_a", &source, "team").is_err());
        assert!(validate_inputs(state.path(), "wxid_b", &source, "team").is_err());
        assert!(validate_inputs(state.path(), "wxid_a", &source, " ").is_err());
    }

    #[test]
    fn private_state_and_source_account_must_not_overlap_in_either_direction() {
        let parent = tempfile::TempDir::new().unwrap();
        let parent = parent.path().canonicalize().unwrap();
        let state = parent.join("state");
        fs::create_dir(&state).unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
        let source_inside_state = state.join("wxid_a");
        fs::create_dir(&source_inside_state).unwrap();
        assert!(validate_inputs(&state, "wxid_a", &source_inside_state, "team").is_err());

        let source = parent.join("wxid_a");
        fs::create_dir(&source).unwrap();
        let state_inside_source = source.join("state");
        fs::create_dir(&state_inside_source).unwrap();
        fs::set_permissions(&state_inside_source, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(validate_inputs(&state_inside_source, "wxid_a", &source, "team").is_err());
    }

    #[test]
    fn encrypted_metadata_copy_finds_duplicate_names_without_message_shards() {
        let state = tempfile::TempDir::new().unwrap();
        let source = tempfile::TempDir::new().unwrap();
        let account = source.path().canonicalize().unwrap().join("wxid_synthetic");
        let root = account.join("db_storage");
        fs::create_dir_all(root.join("contact")).unwrap();
        fs::create_dir_all(root.join("session")).unwrap();
        // Deliberately omit message/: discovery must never require it.
        let key = [0xA5_u8; 32];
        encrypted_fixture(&root.join("contact/contact.db"), &key,
            "CREATE TABLE contact (username TEXT PRIMARY KEY, alias TEXT, remark TEXT, nick_name TEXT, description TEXT, extra_buffer BLOB);
             CREATE TABLE chat_room (username TEXT PRIMARY KEY, owner TEXT, ext_buffer BLOB);
             INSERT INTO contact VALUES ('1@chatroom','','Team','','',NULL),('2@chatroom','','team','','',NULL),('3@chatroom','','Team Alpha','','',NULL);
             INSERT INTO chat_room VALUES ('1@chatroom','',NULL),('2@chatroom','',NULL),('3@chatroom','',NULL);");
        encrypted_fixture(
            &root.join("session/session.db"),
            &key,
            "CREATE TABLE SessionTable (username TEXT, sort_timestamp INTEGER, summary TEXT);",
        );
        let snapshot = channel_snapshot::capture_metadata(state.path(), &account).unwrap();
        let db =
            WechatDb::open_encrypted_core(snapshot.account_dir().join("db_storage"), key).unwrap();
        verify_complete_contacts(&db).unwrap();
        let resolver = ContactResolver::build(&db).unwrap();
        let result = select_matches(collect_rooms(&db, &resolver).unwrap(), "TEAM", false);
        assert_eq!(result.match_kind, "exact");
        assert_eq!(result.matches.len(), 2);
        assert!(result
            .matches
            .iter()
            .all(|item| item.talker != "3@chatroom"));
        assert!(!snapshot.account_dir().join("db_storage/message").exists());
    }

    #[test]
    fn row_drop_refuses_incomplete_chatroom_candidates() {
        let storage = tempfile::TempDir::new().unwrap();
        let root = storage.path();
        fs::create_dir(root.join("contact")).unwrap();
        fs::create_dir(root.join("session")).unwrap();
        let conn = rusqlite::Connection::open(root.join("contact/contact.db")).unwrap();
        conn.execute_batch("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, nick_name TEXT, description TEXT, extra_buffer BLOB);
            CREATE TABLE chat_room (username TEXT, owner TEXT, ext_buffer BLOB);
            INSERT INTO chat_room VALUES ('1@chatroom','',NULL),(NULL,'',NULL);").unwrap();
        rusqlite::Connection::open(root.join("session/session.db")).unwrap();
        let db = WechatDb::open_core(root).unwrap();
        assert!(collect_rooms(&db, &ContactResolver::empty()).is_err());
    }
}
