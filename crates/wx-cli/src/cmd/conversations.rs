//! Metadata-only discovery of group and private conversations.
use std::collections::BTreeMap;
use std::error::Error;
use std::path::PathBuf;

use clap::{Subcommand, ValueEnum};
use serde::Serialize;
use sha2::{Digest, Sha256};
use wx_context::{open_encrypted_db_core, AccountContext, ContactResolver, ResolveParams};
use wx_db::{ChatRoomQuery, Contact, ContactQuery, WechatDb, MAX_QUERY_LIMIT};

use super::{channel_snapshot, groups};

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Kind {
    Group,
    Private,
    All,
}

#[derive(Debug, Subcommand)]
pub enum ConversationsAction {
    /// Find conversations by display name or stable conversation ID.
    Find {
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        account: String,
        #[arg(long)]
        source: PathBuf,
        #[arg(long, conflicts_with = "id", required_unless_present = "id")]
        name: Option<String>,
        #[arg(long, conflicts_with = "name", required_unless_present = "name")]
        id: Option<String>,
        #[arg(long, requires = "name", conflicts_with = "all_matches")]
        exact: bool,
        /// Include partial name matches even when exact matches exist.
        #[arg(long, requires = "name", conflicts_with = "exact")]
        all_matches: bool,
        #[arg(long, value_enum, default_value = "all")]
        kind: Kind,
    },
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
struct Match {
    conversation_id: String,
    talker: String,
    name: String,
    kind: &'static str,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct FindResult {
    matches: Vec<Match>,
    match_kind: &'static str,
}

fn invalid(message: &str) -> Box<dyn Error> {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, message).into()
}

fn conversation_id(account: &str, talker: &str) -> String {
    let mut hash = Sha256::new();
    hash.update(account.as_bytes());
    hash.update([0]);
    hash.update(talker.as_bytes());
    hex::encode(hash.finalize())[..32].to_owned()
}

fn private_name(contact: &Contact) -> Option<&str> {
    let talker = contact.user_name.as_str();
    if talker.trim().is_empty()
        || talker.contains(char::is_whitespace)
        || talker.contains('@')
        || talker.starts_with("gh_")
        || matches!(
            talker,
            "filehelper"
                | "weixin"
                | "fmessage"
                | "newsapp"
                | "brandsessionholder"
                | "medianote"
                | "floatbottle"
                | "qmessage"
                | "qqmail"
                | "tmessage"
        )
    {
        return None;
    }
    let name = [&contact.remark, &contact.nick_name, &contact.alias]
        .into_iter()
        .map(String::as_str)
        .find(|value| !value.trim().is_empty() && *value != talker)?;
    Some(name)
}

fn collect_contacts(db: &WechatDb) -> Result<Vec<Contact>, Box<dyn Error>> {
    let mut contacts = Vec::new();
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
        contacts.extend(page.items);
        offset += expected;
        if offset >= page.stats.total_rows {
            return Ok(contacts);
        }
    }
}

pub(crate) fn has_private_contact(db: &WechatDb, talker: &str) -> Result<bool, Box<dyn Error>> {
    Ok(collect_contacts(db)?
        .iter()
        .any(|contact| contact.user_name == talker && private_name(contact).is_some()))
}

fn collect(db: &WechatDb, account: &str, kind: Kind) -> Result<Vec<Match>, Box<dyn Error>> {
    let mut found = BTreeMap::new();
    if kind != Kind::Private {
        groups::verify_complete_contacts(db)?;
        let resolver = ContactResolver::build(db)?;
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
            for room in page.items {
                if room.username.ends_with("@chatroom") {
                    found.insert(
                        room.username.clone(),
                        Match {
                            conversation_id: conversation_id(account, &room.username),
                            name: resolver.display_name(&room.username).to_owned(),
                            talker: room.username,
                            kind: "group",
                        },
                    );
                }
            }
            offset += expected;
            if offset >= page.stats.total_rows {
                break;
            }
        }
    }
    if kind != Kind::Group {
        for contact in collect_contacts(db)? {
            if let Some(name) = private_name(&contact) {
                found.insert(
                    contact.user_name.clone(),
                    Match {
                        conversation_id: conversation_id(account, &contact.user_name),
                        talker: contact.user_name.clone(),
                        name: name.to_owned(),
                        kind: "private",
                    },
                );
            }
        }
    }
    Ok(found.into_values().collect())
}

fn select_matches(
    candidates: Vec<Match>,
    name: Option<&str>,
    id: Option<&str>,
    exact_only: bool,
    all_matches: bool,
) -> FindResult {
    let (mut matches, match_kind) = if let Some(id) = id {
        let matches: Vec<_> = candidates
            .into_iter()
            .filter(|item| item.conversation_id == id)
            .collect();
        let kind = if matches.is_empty() { "none" } else { "id" };
        (matches, kind)
    } else {
        let query = name.unwrap_or_default().trim().to_lowercase();
        let exact: Vec<_> = candidates
            .iter()
            .filter(|item| item.name.to_lowercase() == query)
            .cloned()
            .collect();
        if all_matches {
            let matches: Vec<_> = candidates
                .into_iter()
                .filter(|item| item.name.to_lowercase().contains(&query))
                .collect();
            let kind = if matches.is_empty() {
                "none"
            } else if matches.len() == exact.len() {
                "exact"
            } else {
                "partial"
            };
            (matches, kind)
        } else if !exact.is_empty() {
            (exact, "exact")
        } else if exact_only {
            (Vec::new(), "none")
        } else {
            let matches: Vec<_> = candidates
                .into_iter()
                .filter(|item| item.name.to_lowercase().contains(&query))
                .collect();
            let kind = if matches.is_empty() {
                "none"
            } else {
                "partial"
            };
            (matches, kind)
        }
    };
    matches.sort_by(|a, b| a.name.cmp(&b.name).then_with(|| a.talker.cmp(&b.talker)));
    FindResult {
        matches,
        match_kind,
    }
}

pub fn run(action: ConversationsAction) -> Result<(), Box<dyn Error>> {
    let ConversationsAction::Find {
        state_dir,
        account,
        source,
        name,
        id,
        exact,
        all_matches,
        kind,
    } = action;
    let query = name
        .as_deref()
        .or(id.as_deref())
        .ok_or_else(|| invalid("name or id required"))?;
    groups::validate_inputs(&state_dir, &account, &source, query)?;
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
    let candidates = collect(&db, &account, kind)?;
    println!(
        "{}",
        serde_json::to_string(&select_matches(
            candidates,
            name.as_deref(),
            id.as_deref(),
            exact,
            all_matches,
        ))?
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn fixture() -> (tempfile::TempDir, WechatDb) {
        let storage = tempfile::TempDir::new().unwrap();
        for part in ["contact", "session"] {
            fs::create_dir(storage.path().join(part)).unwrap();
        }
        let contact =
            rusqlite::Connection::open(storage.path().join("contact/contact.db")).unwrap();
        contact.execute_batch(
            "CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, nick_name TEXT, description TEXT, extra_buffer BLOB);
             CREATE TABLE chat_room (username TEXT, owner TEXT, ext_buffer BLOB);
             INSERT INTO contact VALUES
               ('room@chatroom','','Team','','',NULL),
               ('wxid_alice','','Alex','','',NULL),
               ('ordinary_bob','','Alex','','',NULL),
               ('wxid_empty','','wxid_empty','','',NULL),
               ('gh_public','','Alex','','',NULL),
               ('filehelper','','Alex','','',NULL),
               ('wxid_other','','Morgan','','',NULL);
             INSERT INTO chat_room VALUES ('room@chatroom','',NULL);",
        ).unwrap();
        rusqlite::Connection::open(storage.path().join("session/session.db")).unwrap();
        let db = WechatDb::open_core(storage.path()).unwrap();
        (storage, db)
    }

    #[test]
    fn mixed_kinds_duplicate_names_and_exact_id() {
        let (_storage, db) = fixture();
        let candidates = collect(&db, "account-a", Kind::All).unwrap();
        assert_eq!(candidates.len(), 4);
        let found = select_matches(candidates.clone(), Some("alex"), None, false, false);
        assert_eq!(found.match_kind, "exact");
        assert_eq!(found.matches.len(), 2);
        assert!(found.matches.iter().all(|item| item.kind == "private"));
        let id = found.matches[0].conversation_id.clone();
        let by_id = select_matches(candidates, None, Some(&id), false, false);
        assert_eq!(by_id.match_kind, "id");
        assert_eq!(by_id.matches.len(), 1);
        assert_eq!(by_id.matches[0].conversation_id, id);
        assert_eq!(collect(&db, "account-a", Kind::Group).unwrap().len(), 1);
        assert_eq!(collect(&db, "account-a", Kind::Private).unwrap().len(), 3);
        assert!(has_private_contact(&db, "ordinary_bob").unwrap());
        assert!(!has_private_contact(&db, "gh_public").unwrap());
    }

    #[test]
    fn stable_id_uses_account_and_talker_only() {
        let first = conversation_id("account-a", "ordinary_bob");
        assert_eq!(first.len(), 32);
        assert_eq!(first, conversation_id("account-a", "ordinary_bob"));
        assert_ne!(first, conversation_id("account-b", "ordinary_bob"));
        assert_ne!(first, conversation_id("account-a", "wxid_alice"));
    }

    #[test]
    fn all_matches_keeps_exact_name_and_native_member_count_suffixes() {
        let candidates = ["Alice", "Alice (2)", "Alice（２）", "Bob"]
            .into_iter()
            .enumerate()
            .map(|(index, name)| Match {
                conversation_id: index.to_string(),
                talker: format!("peer-{index}"),
                name: name.to_owned(),
                kind: "private",
            })
            .collect::<Vec<_>>();
        let default = select_matches(candidates.clone(), Some("Alice"), None, false, false);
        assert_eq!(default.match_kind, "exact");
        assert_eq!(default.matches.len(), 1);
        let all = select_matches(candidates.clone(), Some("Alice"), None, false, true);
        assert_eq!(all.match_kind, "partial");
        assert_eq!(all.matches.len(), 3);
        assert!(all.matches.iter().any(|item| item.name == "Alice"));
        assert!(all.matches.iter().any(|item| item.name == "Alice (2)"));
        assert!(all.matches.iter().any(|item| item.name == "Alice（２）"));
        assert!(!all.matches.iter().any(|item| item.name == "Bob"));
        let exact = select_matches(candidates, Some("Alice"), None, true, false);
        assert_eq!(exact.matches.len(), 1);
    }

    #[test]
    fn unsafe_paths_and_empty_queries_are_rejected() {
        let state = tempfile::TempDir::new().unwrap();
        let source = state.path().join("wxid_test");
        assert!(groups::validate_inputs(state.path(), "wxid_test", &source, "alex").is_err());
        assert!(groups::validate_inputs(state.path(), "../wxid_test", &source, "alex").is_err());
        assert!(groups::validate_inputs(state.path(), "wxid_test", &source, " ").is_err());
    }
}
