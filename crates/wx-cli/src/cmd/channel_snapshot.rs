//! Bounded, byte-only capture of live encrypted WeChat databases.
//! SQLite is allowed to open only the owned copy returned by this module.
use std::error::Error;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::{Component, Path, PathBuf};

use sha2::{Digest, Sha256};

const MAX_BYTES: u64 = 512 * 1024 * 1024;
const MAX_FILES: usize = 64;
const ATTEMPTS: usize = 2;
const MAX_RETAINED_SNAPSHOTS: usize = 4;

fn invalid(message: &str) -> Box<dyn Error> {
    std::io::Error::new(std::io::ErrorKind::InvalidData, message).into()
}

fn owned_dir(path: &Path) -> Result<(), Box<dyn Error>> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || meta.file_type().is_symlink() || meta.uid() != unsafe { libc::geteuid() } {
        return Err(invalid(
            "snapshot source directory must be owned and free of symlinks",
        ));
    }
    Ok(())
}

/// Reject symlinks in the supplied path, including parents. Ownership is checked
/// for the account subtree separately; system ancestors may belong to root.
pub(super) fn validate_account_path(account: &Path) -> Result<(), Box<dyn Error>> {
    let absolute = if account.is_absolute() {
        account.to_path_buf()
    } else {
        std::env::current_dir()?.join(account)
    };
    let mut parent = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::RootDir | Component::Prefix(_) => parent.push(component.as_os_str()),
            Component::Normal(name) => {
                parent.push(name);
                let meta = fs::symlink_metadata(&parent)?;
                if meta.file_type().is_symlink() || !meta.is_dir() {
                    return Err(invalid(
                        "snapshot source path has a symlink or non-directory parent",
                    ));
                }
            }
            Component::CurDir => {}
            Component::ParentDir => return Err(invalid("snapshot source path cannot contain ..")),
        }
    }
    owned_dir(account)?;
    owned_dir(&account.join("db_storage"))?;
    for dir in ["contact", "session", "message"] {
        owned_dir(&account.join("db_storage").join(dir))?;
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Fingerprint {
    relative: PathBuf,
    size: u64,
    dev: u64,
    ino: u64,
    mtime: i64,
    mtime_nsec: i64,
    digest: [u8; 32],
}

fn source_file(path: &Path) -> Result<File, Box<dyn Error>> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file()
        || meta.file_type().is_symlink()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.nlink() != 1
    {
        return Err(invalid(
            "snapshot source file must be owned, regular, and unlinked",
        ));
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let opened = file.metadata()?;
    if opened.dev() != meta.dev() || opened.ino() != meta.ino() {
        return Err(invalid("snapshot source changed while opening"));
    }
    Ok(file)
}

fn inventory(root: &Path) -> Result<Vec<PathBuf>, Box<dyn Error>> {
    let mut files = vec![
        PathBuf::from("contact/contact.db"),
        PathBuf::from("session/session.db"),
    ];
    for dir in ["contact", "session", "message"] {
        for entry in fs::read_dir(root.join(dir))? {
            let entry = entry?;
            let name = entry.file_name();
            let name = name
                .to_str()
                .ok_or_else(|| invalid("non-UTF8 database entry"))?;
            if name.ends_with("-journal") {
                return Err(invalid("rollback journal present; snapshot refused"));
            }
            let path = entry.path();
            if fs::symlink_metadata(&path)?.file_type().is_symlink() {
                return Err(invalid("symlink in snapshot source directory"));
            }
            if dir == "message" && numbered_db(name) {
                files.push(PathBuf::from(dir).join(name));
            }
        }
    }
    if files.len() == 2 {
        return Err(invalid("no numeric message shard found"));
    }
    let dbs = files.clone();
    for db in dbs {
        let wal = root.join(format!("{}-wal", db.display()));
        if fs::symlink_metadata(&wal).is_ok() {
            files.push(PathBuf::from(format!("{}-wal", db.display())));
        }
    }
    files.sort();
    if files.len() > MAX_FILES {
        return Err(invalid("too many snapshot files"));
    }
    Ok(files)
}

fn numbered_db(name: &str) -> bool {
    name.strip_prefix("message_")
        .and_then(|rest| rest.strip_suffix(".db"))
        .is_some_and(|digits| !digits.is_empty() && digits.bytes().all(|c| c.is_ascii_digit()))
}

fn measure(root: &Path, files: &[PathBuf]) -> Result<Vec<Fingerprint>, Box<dyn Error>> {
    let mut result = Vec::with_capacity(files.len());
    let mut total = 0_u64;
    for relative in files {
        let mut file = source_file(&root.join(relative))?;
        let before = file.metadata()?;
        total = total
            .checked_add(before.len())
            .ok_or_else(|| invalid("snapshot size overflow"))?;
        if total > MAX_BYTES {
            return Err(invalid("snapshot exceeds 512 MiB limit"));
        }
        let mut hash = Sha256::new();
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let n = file.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            hash.update(&buffer[..n]);
        }
        let after = file.metadata()?;
        if before.len() != after.len()
            || before.mtime() != after.mtime()
            || before.mtime_nsec() != after.mtime_nsec()
        {
            return Err(invalid("snapshot source changed while hashing"));
        }
        result.push(Fingerprint {
            relative: relative.clone(),
            size: before.len(),
            dev: before.dev(),
            ino: before.ino(),
            mtime: before.mtime(),
            mtime_nsec: before.mtime_nsec(),
            digest: hash.finalize().into(),
        });
    }
    Ok(result)
}

pub(super) struct Snapshot {
    root: PathBuf,
    account: PathBuf,
    dev: u64,
    ino: u64,
}

impl Snapshot {
    pub(super) fn account_dir(&self) -> &Path {
        &self.account
    }
}

impl Drop for Snapshot {
    fn drop(&mut self) {
        if let Ok(meta) = fs::symlink_metadata(&self.root) {
            if meta.is_dir()
                && !meta.file_type().is_symlink()
                && meta.dev() == self.dev
                && meta.ino() == self.ino
            {
                let _ = fs::remove_dir_all(&self.root);
            }
        }
    }
}

fn new_snapshot(state_dir: &Path, account_name: &str) -> Result<Snapshot, Box<dyn Error>> {
    let mut retained = 0;
    for entry in fs::read_dir(state_dir)? {
        let entry = entry?;
        let name = entry.file_name();
        let Some(suffix) = name
            .to_str()
            .and_then(|name| name.strip_prefix(".snapshot-"))
        else {
            continue;
        };
        if suffix.len() != 32 || !suffix.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            continue;
        }
        let meta = fs::symlink_metadata(entry.path())?;
        if !meta.is_dir()
            || meta.file_type().is_symlink()
            || meta.uid() != unsafe { libc::geteuid() }
        {
            return Err(invalid("unexpected entry in snapshot namespace"));
        }
        retained += 1;
    }
    if retained >= MAX_RETAINED_SNAPSHOTS {
        return Err(invalid(
            "too many retained private snapshots; inspect state directory",
        ));
    }
    let mut random = [0_u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut random)?;
    let root = state_dir.join(format!(".snapshot-{}", hex::encode(random)));
    fs::DirBuilder::new().mode(0o700).create(&root)?;
    let meta = fs::symlink_metadata(&root)?;
    // The temporary account basename must match the KeyStore account id.
    let account = root.join(account_name);
    let snapshot = Snapshot {
        root,
        account: account.clone(),
        dev: meta.dev(),
        ino: meta.ino(),
    };
    fs::DirBuilder::new().mode(0o700).create(&account)?;
    for dir in [
        "db_storage",
        "db_storage/contact",
        "db_storage/session",
        "db_storage/message",
    ] {
        fs::DirBuilder::new()
            .mode(0o700)
            .create(account.join(dir))?;
    }
    Ok(snapshot)
}

fn copy_files(root: &Path, files: &[Fingerprint], target: &Path) -> Result<(), Box<dyn Error>> {
    for fingerprint in files {
        let source = source_file(&root.join(&fingerprint.relative))?;
        let mut dest = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(target.join(&fingerprint.relative))?;
        let copied = std::io::copy(&mut source.take(fingerprint.size + 1), &mut dest)?;
        if copied != fingerprint.size {
            return Err(invalid("snapshot source changed while copying"));
        }
        dest.flush()?;
    }
    Ok(())
}

fn capture_once<F>(
    state_dir: &Path,
    account: &Path,
    after_copy: &mut F,
) -> Result<Snapshot, Box<dyn Error>>
where
    F: FnMut(),
{
    validate_account_path(account)?;
    let source_root = account.join("db_storage");
    let files = inventory(&source_root)?;
    let before = measure(&source_root, &files)?;
    let name = account
        .file_name()
        .and_then(|s| s.to_str())
        .ok_or_else(|| invalid("invalid account basename"))?;
    let snapshot = new_snapshot(state_dir, name)?;
    let target = snapshot.account_dir().join("db_storage");
    copy_files(&source_root, &before, &target)?;
    after_copy();
    if inventory(&source_root)? != files
        || measure(&source_root, &files)? != before
        || measure(&target, &files)?
            .iter()
            .map(|f| (f.relative.clone(), f.size, f.digest))
            .collect::<Vec<_>>()
            != before
                .iter()
                .map(|f| (f.relative.clone(), f.size, f.digest))
                .collect::<Vec<_>>()
    {
        return Err(invalid("snapshot source changed during capture"));
    }
    Ok(snapshot)
}

pub(super) fn capture(state_dir: &Path, account: &Path) -> Result<Snapshot, Box<dyn Error>> {
    capture_with_hook(state_dir, account, &mut || {})
}

fn capture_with_hook<F>(
    state_dir: &Path,
    account: &Path,
    after_copy: &mut F,
) -> Result<Snapshot, Box<dyn Error>>
where
    F: FnMut(),
{
    let mut last = None;
    for _ in 0..ATTEMPTS {
        match capture_once(state_dir, account, after_copy) {
            Ok(snapshot) => return Ok(snapshot),
            Err(error) => last = Some(error),
        }
    }
    Err(last.expect("at least one capture attempt"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::c_void;
    use std::os::unix::fs::symlink;
    use tempfile::TempDir;

    fn fixture() -> (TempDir, PathBuf, TempDir) {
        let state = TempDir::new().unwrap();
        let source = TempDir::new().unwrap();
        let account = source.path().canonicalize().unwrap().join("wxid_test_123");
        for dir in ["contact", "session", "message"] {
            fs::create_dir_all(account.join("db_storage").join(dir)).unwrap();
        }
        for (name, content) in [
            ("contact/contact.db", b"contact".as_slice()),
            ("session/session.db", b"session"),
            ("message/message_0.db", b"message"),
            ("message/message_0.db-wal", b"wal"),
        ] {
            fs::write(account.join("db_storage").join(name), content).unwrap();
        }
        (state, account, source)
    }

    #[test]
    fn capture_preserves_source_and_excludes_shm() {
        let (state, account, _source) = fixture();
        let root = account.join("db_storage");
        let shm = root.join("message/message_0.db-shm");
        fs::write(&shm, b"shm untouched").unwrap();
        let originals = measure(&root, &inventory(&root).unwrap()).unwrap();
        let snapshot = capture(state.path(), &account).unwrap();
        let copied = snapshot.account_dir().join("db_storage");
        assert!(!copied.join("message/message_0.db-shm").exists());
        assert_eq!(fs::read(&shm).unwrap(), b"shm untouched");
        assert_eq!(
            measure(&root, &inventory(&root).unwrap())
                .unwrap()
                .iter()
                .map(|f| (&f.relative, f.digest))
                .collect::<Vec<_>>(),
            originals
                .iter()
                .map(|f| (&f.relative, f.digest))
                .collect::<Vec<_>>()
        );
        let path = snapshot.account_dir().to_path_buf();
        drop(snapshot);
        assert!(!path.exists());
    }

    #[test]
    fn rejects_symlink_hardlink_and_journal() {
        let (state, account, _source) = fixture();
        let root = account.join("db_storage");
        fs::write(root.join("contact/contact.db-journal"), b"j").unwrap();
        assert!(capture(state.path(), &account).is_err());
        fs::remove_file(root.join("contact/contact.db-journal")).unwrap();
        let original = root.join("contact/contact.db");
        let link = root.join("contact/contact-link");
        fs::hard_link(&original, &link).unwrap();
        assert!(capture(state.path(), &account).is_err());
        fs::remove_file(link).unwrap();
        fs::remove_file(&original).unwrap();
        symlink(root.join("session/session.db"), &original).unwrap();
        assert!(capture(state.path(), &account).is_err());
    }

    #[test]
    fn live_change_aborts_and_removes_copy() {
        let (state, account, _source) = fixture();
        let root = account.join("db_storage");
        let mut revision = 0;
        let mut change = || {
            revision += 1;
            fs::write(
                root.join("message/message_0.db-wal"),
                format!("updated-{revision}"),
            )
            .unwrap();
        };
        assert!(capture_with_hook(state.path(), &account, &mut change).is_err());
        assert_eq!(revision, ATTEMPTS);
        assert_eq!(fs::read_dir(state.path()).unwrap().count(), 0);
    }

    #[test]
    fn retained_snapshot_limit_fails_closed_without_deleting_dirs() {
        let (state, account, _source) = fixture();
        for index in 0..MAX_RETAINED_SNAPSHOTS {
            fs::create_dir(state.path().join(format!(".snapshot-{index:032x}"))).unwrap();
        }
        assert!(capture(state.path(), &account).is_err());
        assert_eq!(
            fs::read_dir(state.path()).unwrap().count(),
            MAX_RETAINED_SNAPSHOTS
        );
    }

    fn encrypted_db(path: &Path, key: &[u8; 32], sql: &str) {
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
    fn encrypted_wal_row_is_visible_only_through_copy_without_touching_source() {
        let (state, account, _source) = fixture();
        let root = account.join("db_storage");
        let key = [0xAB_u8; 32];
        for (name, sql) in [
            (
                "contact/contact.db",
                "CREATE TABLE contact (username TEXT);",
            ),
            (
                "session/session.db",
                "CREATE TABLE SessionTable (username TEXT);",
            ),
            (
                "message/message_0.db",
                "CREATE TABLE Timestamp (timestamp INTEGER); INSERT INTO Timestamp VALUES (1700000000); CREATE TABLE wal_probe (message TEXT);",
            ),
        ] {
            fs::remove_file(root.join(name)).unwrap();
            encrypted_db(&root.join(name), &key, sql);
        }
        let _ = fs::remove_file(root.join("message/message_0.db-wal"));
        let msg = root.join("message/message_0.db");
        let writer = rusqlite::Connection::open(&msg).unwrap();
        unsafe {
            assert_eq!(
                rusqlite::ffi::sqlite3_key(writer.handle(), key.as_ptr() as *const c_void, 32),
                0
            );
        }
        writer.execute_batch("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; INSERT INTO wal_probe VALUES ('committed only in WAL');").unwrap();
        let shm = root.join("message/message_0.db-shm");
        assert!(shm.exists());
        let before = measure(&root, &inventory(&root).unwrap()).unwrap();
        let shm_before = fs::read(&shm).unwrap();
        let snapshot = capture(state.path(), &account).unwrap();
        let copied = snapshot.account_dir().join("db_storage");
        let db = wx_db::WechatDb::open_encrypted(&copied, key).unwrap();
        let reader =
            wx_db::open_readonly_connection(&copied.join("message/message_0.db"), Some(&key))
                .unwrap();
        let value: String = reader
            .query_row("SELECT message FROM wal_probe", [], |row| row.get(0))
            .unwrap();
        assert_eq!(value, "committed only in WAL");
        drop(reader);
        drop(db);
        assert_eq!(measure(&root, &inventory(&root).unwrap()).unwrap(), before);
        assert_eq!(fs::read(&shm).unwrap(), shm_before);
        let snapshot_path = snapshot.account_dir().to_path_buf();
        drop(snapshot);
        assert!(!snapshot_path.exists());
        drop(writer);
    }
}
