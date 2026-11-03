use std::path::Path;

use base64::Engine;

use crate::error::MediaError;

/// Derive V2 AES key from UIN and WXID.
///
/// Formula: `MD5(format!("{uin}{wxid}")).hex()[:16].as_bytes()` → 16 ASCII bytes.
///
/// The V1 fixed key `cfcd208495d565ef` is a special case: `MD5("0")[:16]`.
pub fn derive_v2_aes_key(uin: &str, wxid: &str) -> [u8; 16] {
    let input = format!("{uin}{wxid}");
    let digest = md5::compute(input.as_bytes());
    let hex_str = format!("{digest:x}"); // 32-char lowercase hex
    let mut key = [0u8; 16];
    key.copy_from_slice(&hex_str.as_bytes()[..16]);
    key
}

/// Extract canonical base account ID from a directory name.
///
/// Example: `wxid_example123abc_ab12` → `"wxid_example123abc"`
/// Example: `testuser001_1662` → `"testuser001_1662"` without extra signal
/// Example: `wxid_test` → `"wxid_test"` (not stripped: would leave bare `wxid`)
pub fn extract_wxid(dir_name: &str) -> String {
    wx_keychain::account_id::canonical_base_for_account_dir(dir_name)
}

fn extract_wxid_from_data_dir(data_dir: &Path) -> Result<String, MediaError> {
    let dir_name = data_dir
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| MediaError::InvalidFormat {
            reason: "cannot determine directory name from data_dir".into(),
        })?;

    Ok(data_dir.parent().map_or_else(
        || extract_wxid(dir_name),
        |root| wx_keychain::process::extract_base_wxid_for_account_dir_under_root(root, dir_name),
    ))
}

/// Read UIN from `config.ini` for the given WeChat account data directory.
///
/// Searches for `last_uin=<base64>` in config.ini under the ilink directory.
///
/// On macOS, the directory layout separates account data from shared app_data:
/// ```text
/// Documents/
/// ├── app_data/radium/ilink/<hash>/kvcomm/config.ini
/// └── xwechat_files/<wxid_xxx_XXXX>/   ← data_dir
/// ```
///
/// This function tries two locations:
/// 1. `<data_dir>/app_data/radium/ilink/` (for tests / alternative layouts)
/// 2. `<data_dir>/../../app_data/radium/ilink/` (standard macOS layout)
///
/// When multiple accounts exist under ilink, the `account_suffix` (e.g. `ab12`
/// from `wxid_xxx_ab12`) is used to match the correct subdirectory.
pub fn read_uin(data_dir: &Path) -> Result<String, MediaError> {
    let suffix =
        extract_account_suffix(data_dir.file_name().and_then(|n| n.to_str()).unwrap_or(""));

    // Candidate ilink directories (in priority order)
    let candidates = [
        data_dir.join("app_data").join("radium").join("ilink"),
        data_dir
            .join("..")
            .join("..")
            .join("app_data")
            .join("radium")
            .join("ilink"),
    ];

    for ilink_dir in &candidates {
        if !ilink_dir.is_dir() {
            continue;
        }
        if let Ok(uin) = read_uin_from_ilink(ilink_dir, suffix.as_deref()) {
            return Ok(uin);
        }
    }

    Err(MediaError::NotFound(format!(
        "no config.ini with last_uin found (searched {} candidate paths)",
        candidates.len()
    )))
}

/// Extract the 4-char account hash suffix from a directory name.
///
/// Delegates to `wx_keychain::AccountId::parse` for consistent suffix detection.
///
/// `wxid_example123abc_ab12` → `Some("ab12")`
fn extract_account_suffix(dir_name: &str) -> Option<String> {
    wx_keychain::AccountId::parse(dir_name)
        .suffix()
        .map(|s| s.to_string())
}

/// Scan an ilink directory for config.ini with last_uin.
///
/// If `account_suffix` is provided, prefer subdirectories whose name starts with it.
fn read_uin_from_ilink(
    ilink_dir: &Path,
    account_suffix: Option<&str>,
) -> Result<String, MediaError> {
    let mut entries: Vec<_> = std::fs::read_dir(ilink_dir)?
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().map(|ft| ft.is_dir()).unwrap_or(false))
        .collect();

    // Sort: matching-suffix directories first
    if let Some(suffix) = account_suffix {
        entries.sort_by_key(|e| {
            let name = e.file_name().to_string_lossy().to_string();
            if name.starts_with(suffix) {
                0
            } else {
                1
            }
        });
    }

    for entry in entries {
        let config_path = entry.path().join("kvcomm").join("config.ini");
        if !config_path.is_file() {
            continue;
        }
        let content = std::fs::read_to_string(&config_path)?;
        if let Some(uin) = parse_uin_from_config(&content) {
            return Ok(uin);
        }
    }

    Err(MediaError::NotFound(
        "no config.ini with last_uin found in ilink directory".into(),
    ))
}

/// Parse `last_uin=<base64>` from config.ini content and return the decoded UIN.
fn parse_uin_from_config(content: &str) -> Option<String> {
    for line in content.lines() {
        let line = line.trim();
        if let Some(value) = line.strip_prefix("last_uin=") {
            let value = value.trim();
            if value.is_empty() {
                continue;
            }
            let decoded = base64::engine::general_purpose::STANDARD
                .decode(value)
                .ok()?;
            let uin = String::from_utf8(decoded).ok()?;
            if !uin.is_empty() && uin.chars().all(|c| c.is_ascii_digit()) {
                return Some(uin);
            }
        }
    }
    None
}

/// Derive V2 AES key automatically from a WeChat account data directory.
///
/// Combines: read UIN from config.ini + extract WXID from directory name + MD5 derivation.
pub fn derive_v2_key_from_dir(data_dir: &Path) -> Result<[u8; 16], MediaError> {
    let wxid = extract_wxid_from_data_dir(data_dir)?;
    let uin = read_uin(data_dir)?;

    Ok(derive_v2_aes_key(&uin, &wxid))
}

/// Derive a key only from the explicitly bound account's unique local config.
///
/// Unlike the legacy directory API, this never infers an identity from login
/// directories or falls back to another account's config. It neither prints
/// secret material nor reads or writes a KeyStore. A four-character account
/// suffix is required to select the config even when `self_wxid == account_id`.
pub fn derive_v2_key_for_account(
    data_dir: &Path,
    account_id: &str,
    self_wxid: &str,
) -> Result<[u8; 16], MediaError> {
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    {
        strict_account::derive(data_dir, account_id, self_wxid)
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = (data_dir, account_id, self_wxid);
        Err(MediaError::InvalidFormat {
            reason: "strict account image-key derivation is unsupported on this platform".into(),
        })
    }
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod strict_account {
    use std::fs::{self, Metadata, OpenOptions};
    use std::io::Read;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::path::{Component, Path};

    use base64::Engine;

    use super::{derive_v2_aes_key, extract_account_suffix, MediaError};

    const MAX_CONFIG_BYTES: u64 = 64 * 1024;
    // O_NOFOLLOW | O_NONBLOCK from the Darwin/Linux open(2) ABI. These keep
    // opening a replaced config from following a symlink or blocking on a FIFO.
    #[cfg(target_os = "macos")]
    const SAFE_OPEN_FLAGS: i32 = 0x0100 | 0x0004;
    #[cfg(target_os = "linux")]
    const SAFE_OPEN_FLAGS: i32 = 0x20000 | 0x0800;

    unsafe extern "C" {
        fn geteuid() -> u32;
    }

    fn invalid(reason: &'static str) -> MediaError {
        MediaError::InvalidFormat {
            reason: reason.into(),
        }
    }

    fn canonical(path: &Path) -> bool {
        path.is_absolute()
            && !path
                .components()
                .any(|part| matches!(part, Component::CurDir | Component::ParentDir))
            && fs::canonicalize(path).is_ok_and(|resolved| resolved == path)
    }

    fn directory(path: &Path, uid: u32) -> Result<Metadata, MediaError> {
        let meta = fs::symlink_metadata(path).map_err(|_| {
            invalid("selected-account image configuration directory is unavailable")
        })?;
        if !meta.is_dir() || meta.uid() != uid || !canonical(path) {
            return Err(invalid(
                "selected-account image configuration directory is unsafe",
            ));
        }
        Ok(meta)
    }

    fn safe_config(meta: &Metadata, uid: u32) -> bool {
        meta.is_file()
            && meta.uid() == uid
            && meta.nlink() == 1
            && meta.mode() & 0o022 == 0
            && meta.len() <= MAX_CONFIG_BYTES
    }

    fn unchanged(a: &Metadata, b: &Metadata) -> bool {
        a.dev() == b.dev()
            && a.ino() == b.ino()
            && a.uid() == b.uid()
            && a.gid() == b.gid()
            && a.mode() == b.mode()
            && a.nlink() == b.nlink()
            && a.len() == b.len()
            && a.mtime() == b.mtime()
            && a.mtime_nsec() == b.mtime_nsec()
            && a.ctime() == b.ctime()
            && a.ctime_nsec() == b.ctime_nsec()
    }

    pub(super) fn derive(
        data_dir: &Path,
        account_id: &str,
        self_wxid: &str,
    ) -> Result<[u8; 16], MediaError> {
        if data_dir.file_name().and_then(|name| name.to_str()) != Some(account_id)
            || self_wxid.is_empty()
        {
            return Err(invalid("selected-account image-key identity mismatch"));
        }
        let suffix = extract_account_suffix(account_id)
            .ok_or_else(|| invalid("selected-account image-key suffix is unavailable"))?;
        if account_id != self_wxid
            && account_id.strip_prefix(self_wxid) != Some(format!("_{suffix}").as_str())
        {
            return Err(invalid("selected-account image-key identity mismatch"));
        }
        // SAFETY: geteuid has no arguments and only reports process ownership.
        let uid = unsafe { geteuid() };
        let mut directories = vec![(data_dir.to_path_buf(), directory(data_dir, uid)?)];
        let mut roots = vec![data_dir.join("app_data/radium/ilink")];
        if let Some(documents) = data_dir.parent().and_then(Path::parent) {
            roots.push(documents.join("app_data/radium/ilink"));
        }
        let mut configs = Vec::new();
        for root in roots {
            match fs::symlink_metadata(&root) {
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(_) => {
                    return Err(invalid(
                        "selected-account image configuration is unavailable",
                    ))
                }
                Ok(_) => {}
            }
            directories.push((root.clone(), directory(&root, uid)?));
            let entries = fs::read_dir(&root)
                .map_err(|_| invalid("selected-account image configuration is unreadable"))?;
            for entry in entries {
                let entry = entry
                    .map_err(|_| invalid("selected-account image configuration is unreadable"))?;
                if !entry
                    .file_name()
                    .as_encoded_bytes()
                    .starts_with(suffix.as_bytes())
                {
                    continue;
                }
                let account_config = entry.path();
                directories.push((account_config.clone(), directory(&account_config, uid)?));
                let kvcomm = account_config.join("kvcomm");
                match fs::symlink_metadata(&kvcomm) {
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                    Err(_) => {
                        return Err(invalid(
                            "selected-account image configuration is unavailable",
                        ))
                    }
                    Ok(_) => {}
                }
                directories.push((kvcomm.clone(), directory(&kvcomm, uid)?));
                let config = kvcomm.join("config.ini");
                let meta = match fs::symlink_metadata(&config) {
                    Ok(meta) => meta,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                    Err(_) => {
                        return Err(invalid(
                            "selected-account image configuration is unavailable",
                        ))
                    }
                };
                if !safe_config(&meta, uid) || !canonical(&config) {
                    return Err(invalid(
                        "selected-account image configuration file is unsafe",
                    ));
                }
                configs.push((config, meta));
            }
        }
        if configs.len() != 1 {
            return Err(invalid(
                "selected-account image configuration is missing or ambiguous",
            ));
        }
        let (path, selected) = &configs[0];
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(SAFE_OPEN_FLAGS)
            .open(path)
            .map_err(|_| invalid("selected-account image configuration cannot be opened safely"))?;
        let before = file
            .metadata()
            .map_err(|_| invalid("selected-account image configuration metadata is unavailable"))?;
        if !safe_config(&before, uid) || !unchanged(selected, &before) {
            return Err(invalid(
                "selected-account image configuration changed before reading",
            ));
        }
        let mut bytes = Vec::new();
        (&file)
            .take(MAX_CONFIG_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| invalid("selected-account image configuration cannot be read safely"))?;
        let after = file
            .metadata()
            .map_err(|_| invalid("selected-account image configuration metadata is unavailable"))?;
        let current = fs::symlink_metadata(path)
            .map_err(|_| invalid("selected-account image configuration changed during reading"))?;
        if bytes.len() as u64 != before.len()
            || !unchanged(&before, &after)
            || !unchanged(&before, &current)
            || !canonical(path)
        {
            return Err(invalid(
                "selected-account image configuration changed during reading",
            ));
        }
        for (path, before) in directories {
            if !unchanged(&before, &directory(&path, uid)?) {
                return Err(invalid(
                    "selected-account image configuration directory changed",
                ));
            }
        }
        let text = std::str::from_utf8(&bytes)
            .map_err(|_| invalid("selected-account image configuration is malformed"))?;
        let values: Vec<_> = text
            .lines()
            .filter_map(|line| line.split_once('='))
            .filter(|(name, _)| name.trim() == "last_uin")
            .map(|(_, value)| value.trim())
            .collect();
        if values.len() != 1 {
            return Err(invalid(
                "selected-account image configuration has missing or duplicate UIN",
            ));
        }
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(values[0])
            .map_err(|_| invalid("selected-account image configuration has malformed UIN"))?;
        if decoded.is_empty()
            || !decoded.iter().all(u8::is_ascii_digit)
            || !decoded.iter().any(|byte| *byte != b'0')
        {
            return Err(invalid(
                "selected-account image configuration has invalid UIN",
            ));
        }
        let uin = std::str::from_utf8(&decoded)
            .map_err(|_| invalid("selected-account image configuration has invalid UIN"))?;
        Ok(derive_v2_aes_key(uin, self_wxid))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_uin_from_config_valid() {
        let config = "[General]\nlast_uin=MTIzNDU2Nzg5MA==\n";
        assert_eq!(
            parse_uin_from_config(config),
            Some("1234567890".to_string())
        );
    }

    #[test]
    fn test_parse_uin_from_config_missing() {
        assert_eq!(parse_uin_from_config("[General]\nsome_key=value\n"), None);
    }

    #[test]
    fn test_parse_uin_from_config_empty_value() {
        assert_eq!(parse_uin_from_config("last_uin=\n"), None);
    }

    #[test]
    fn test_parse_uin_from_config_non_numeric() {
        assert_eq!(parse_uin_from_config("last_uin=YWJj\n"), None); // "abc"
    }

    #[test]
    fn test_extract_account_suffix() {
        assert_eq!(
            extract_account_suffix("wxid_test_ab12"),
            Some("ab12".to_string())
        );
        assert_eq!(
            extract_account_suffix("wxid_test_c3e7"),
            Some("c3e7".to_string())
        );
        assert_eq!(
            extract_account_suffix("wxid_test"),
            Some("test".to_string())
        );
        assert_eq!(extract_account_suffix("wxid_test_abcde"), None);
        // "no_underscore" has rfind('_') at the `_` after `no`, tail = "underscore" (10 chars, not 4)
        assert_eq!(extract_account_suffix("no_underscore"), None);
    }
}
