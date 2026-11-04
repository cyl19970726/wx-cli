use wx_media::key::{derive_v2_key_from_dir, read_uin};
use wx_media::{derive_v2_aes_key, extract_wxid};

// ── V2 key derivation formula ──────────────────────────────────────

#[test]
fn derive_v2_key_known_account() {
    // Verified: MD5("1234567890wxid_example123abc") → hex[:16] = d5b763f94307be38
    let key = derive_v2_aes_key("1234567890", "wxid_example123abc");
    assert_eq!(&key, b"d5b763f94307be38");
}

#[test]
fn v1_fixed_key_same_formula() {
    // V1 fixed key = MD5("0")[:16] = cfcd208495d565ef
    let key = derive_v2_aes_key("", "0");
    assert_eq!(&key, b"cfcd208495d565ef");
}

// ── WXID suffix stripping (shared via wx-keychain AccountId) ───

#[test]
fn extract_wxid_strips_suffix() {
    assert_eq!(
        extract_wxid("wxid_example123abc_ab12"),
        "wxid_example123abc"
    );
}

#[test]
fn extract_wxid_no_suffix() {
    assert_eq!(extract_wxid("wxid_example123abc"), "wxid_example123abc");
}

#[test]
fn extract_wxid_long_suffix_not_stripped() {
    assert_eq!(extract_wxid("wxid_test_abcde"), "wxid_test_abcde");
}

#[test]
fn extract_wxid_non_alnum_suffix_not_stripped() {
    assert_eq!(extract_wxid("wxid_test_ab-c"), "wxid_test_ab-c");
}

#[test]
fn extract_wxid_bare_id_not_shortened() {
    // Regression: wxid_test must NOT be shortened to "wxid"
    assert_eq!(extract_wxid("wxid_test"), "wxid_test");
}

#[test]
fn extract_wxid_legacy_non_wxid_without_signal_stays_raw() {
    assert_eq!(extract_wxid("testuser001_1662"), "testuser001_1662");
}

// ── Base64 UIN parsing ─────────────────────────────────────────────

#[test]
fn read_uin_from_dir() {
    let tmp = tempfile::tempdir().unwrap();
    let data_dir = tmp.path().join("wxid_test1234_ab12");
    let config_dir = data_dir.join("app_data/radium/ilink/somehash/kvcomm");
    std::fs::create_dir_all(&config_dir).unwrap();
    std::fs::write(
        config_dir.join("config.ini"),
        "[General]\nlast_uin=MTIzNDU2Nzg5MA==\n",
    )
    .unwrap();

    let uin = read_uin(&data_dir).unwrap();
    assert_eq!(uin, "1234567890");
}

#[test]
fn read_uin_no_ilink_dir() {
    let tmp = tempfile::tempdir().unwrap();
    let data_dir = tmp.path().join("wxid_test_ab12");
    std::fs::create_dir_all(&data_dir).unwrap();
    assert!(read_uin(&data_dir).is_err());
}

#[test]
fn read_uin_documents_level_path() {
    // Simulate macOS layout: Documents/app_data/ + Documents/xwechat_files/<account>/
    let tmp = tempfile::tempdir().unwrap();
    let docs = tmp.path().join("Documents");
    let data_dir = docs.join("xwechat_files/wxid_example123abc_ab12");
    std::fs::create_dir_all(&data_dir).unwrap();

    let config_dir = docs.join("app_data/radium/ilink/ab12000000000000/kvcomm");
    std::fs::create_dir_all(&config_dir).unwrap();
    std::fs::write(
        config_dir.join("config.ini"),
        "[default]\nlast_uin=MTIzNDU2Nzg5MA==\n",
    )
    .unwrap();

    let uin = read_uin(&data_dir).unwrap();
    assert_eq!(uin, "1234567890");
}

#[test]
fn read_uin_suffix_disambiguation() {
    // Two accounts under ilink — suffix selects the right one
    let tmp = tempfile::tempdir().unwrap();
    let docs = tmp.path().join("Documents");
    let data_dir = docs.join("xwechat_files/wxid_example123abc_ab12");
    std::fs::create_dir_all(&data_dir).unwrap();

    // ab12 account → UIN 1234567890
    let ab12_dir = docs.join("app_data/radium/ilink/ab12000000000000/kvcomm");
    std::fs::create_dir_all(&ab12_dir).unwrap();
    std::fs::write(ab12_dir.join("config.ini"), "last_uin=MTIzNDU2Nzg5MA==\n").unwrap();

    // c3e7 account → UIN 9999999999
    let c3e7_dir = docs.join("app_data/radium/ilink/c3e7000000000000/kvcomm");
    std::fs::create_dir_all(&c3e7_dir).unwrap();
    std::fs::write(
        c3e7_dir.join("config.ini"),
        "last_uin=OTk5OTk5OTk5OQ==\n", // base64("9999999999")
    )
    .unwrap();

    let uin = read_uin(&data_dir).unwrap();
    assert_eq!(uin, "1234567890"); // ab12 suffix → picks ab12 config
}

// ── Full derive_v2_key_from_dir ────────────────────────────────────

#[test]
fn derive_v2_key_from_dir_integration() {
    let tmp = tempfile::tempdir().unwrap();
    let data_dir = tmp.path().join("wxid_example123abc_ab12");
    let config_dir = data_dir.join("app_data/radium/ilink/somehash/kvcomm");
    std::fs::create_dir_all(&config_dir).unwrap();
    std::fs::write(
        config_dir.join("config.ini"),
        "[General]\nlast_uin=MTIzNDU2Nzg5MA==\n",
    )
    .unwrap();

    let key = derive_v2_key_from_dir(&data_dir).unwrap();
    assert_eq!(&key, b"d5b763f94307be38");
}

#[test]
fn derive_v2_key_from_dir_legacy_account_with_login_signal() {
    let tmp = tempfile::tempdir().unwrap();
    let docs = tmp.path().join("Documents");
    let data_dir = docs.join("xwechat_files/testuser001_1662");
    let config_dir = docs.join("app_data/radium/ilink/1662000000000000/kvcomm");
    std::fs::create_dir_all(&config_dir).unwrap();
    std::fs::create_dir_all(docs.join("xwechat_files/all_users/login/testuser001")).unwrap();
    std::fs::create_dir_all(&data_dir).unwrap();
    std::fs::write(
        config_dir.join("config.ini"),
        "[General]\nlast_uin=MTIzNDU2Nzg5MA==\n",
    )
    .unwrap();

    let key = derive_v2_key_from_dir(&data_dir).unwrap();
    let expected = derive_v2_aes_key("1234567890", "testuser001");
    assert_eq!(key, expected);
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod strict_account {
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::path::{Path, PathBuf};

    use wx_media::derive_v2_aes_key;
    use wx_media::key::derive_v2_key_for_account;

    const ACCOUNT: &str = "wxid_example123abc_ab12";
    const SELF: &str = "wxid_example123abc";
    const CONFIG: &str = "[General]\nlast_uin=MTIzNDU2Nzg5MA==\n";

    struct Fixture {
        _temp: tempfile::TempDir,
        documents: PathBuf,
        source: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let temp = tempfile::tempdir().unwrap();
            let documents = temp.path().canonicalize().unwrap().join("Documents");
            let source = documents.join("xwechat_files").join(ACCOUNT);
            fs::create_dir_all(&source).unwrap();
            Self {
                _temp: temp,
                documents,
                source,
            }
        }

        fn config(&self, shared: bool, hash: &str, content: &str) -> PathBuf {
            let root = if shared {
                &self.documents
            } else {
                &self.source
            };
            let path = root
                .join("app_data/radium/ilink")
                .join(hash)
                .join("kvcomm/config.ini");
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, content).unwrap();
            path
        }

        fn derive(&self) -> Result<[u8; 16], wx_media::MediaError> {
            derive_v2_key_for_account(&self.source, ACCOUNT, SELF)
        }
    }

    #[test]
    fn own_config_in_either_supported_layout_derives_exact_key() {
        for shared in [false, true] {
            let fixture = Fixture::new();
            fixture.config(shared, "ab12000000000000", CONFIG);
            assert_eq!(fixture.derive().unwrap(), *b"d5b763f94307be38");
            // A directly matching trusted identity is used verbatim, not stripped.
            assert_eq!(
                derive_v2_key_for_account(&fixture.source, ACCOUNT, ACCOUNT).unwrap(),
                derive_v2_aes_key("1234567890", ACCOUNT)
            );
        }
    }

    #[test]
    fn unmatched_valid_account_never_supplies_missing_or_invalid_own_uin() {
        let fixture = Fixture::new();
        fixture.config(true, "c3e7000000000000", CONFIG);
        assert!(fixture.derive().is_err());
        fixture.config(true, "ab12000000000000", "last_uin=not-base64\n");
        assert!(fixture.derive().is_err());
    }

    #[test]
    fn unmatched_config_is_not_followed_or_validated() {
        let fixture = Fixture::new();
        fixture.config(true, "ab12000000000000", CONFIG);
        let other = fixture.config(true, "c3e7000000000000", CONFIG);
        fs::remove_file(&other).unwrap();
        symlink(fixture.documents.join("missing-other-config"), other).unwrap();
        assert_eq!(fixture.derive().unwrap(), *b"d5b763f94307be38");
    }

    #[test]
    fn multiple_matches_in_one_or_both_roots_refuse_before_selection() {
        for second_shared in [false, true] {
            let fixture = Fixture::new();
            fixture.config(false, "ab12000000000000", CONFIG);
            fixture.config(second_shared, "ab12111111111111", "last_uin=MA==\n");
            assert!(fixture.derive().is_err());
        }
    }

    #[test]
    fn explicit_account_self_and_canonical_source_must_match() {
        let fixture = Fixture::new();
        fixture.config(true, "ab12000000000000", CONFIG);
        for (account, self_wxid) in [
            ("wxid_other_ab12", SELF),
            (ACCOUNT, "wxid_other"),
            (ACCOUNT, "wxid_example123"),
            (ACCOUNT, ""),
        ] {
            assert!(derive_v2_key_for_account(&fixture.source, account, self_wxid).is_err());
        }
        let noncanonical = fixture.source.join("..").join(ACCOUNT);
        assert!(derive_v2_key_for_account(&noncanonical, ACCOUNT, SELF).is_err());
        let link = fixture.documents.join(ACCOUNT);
        symlink(&fixture.source, &link).unwrap();
        assert!(derive_v2_key_for_account(&link, ACCOUNT, SELF).is_err());
    }

    #[test]
    fn no_login_directory_inference_can_override_explicit_self() {
        let fixture = Fixture::new();
        let account = "legacy_user_1662";
        let source = fixture.documents.join("xwechat_files").join(account);
        fs::create_dir_all(&source).unwrap();
        fs::create_dir_all(
            fixture
                .documents
                .join("xwechat_files/all_users/login/legacy_user"),
        )
        .unwrap();
        fixture.config(true, "1662000000000000", CONFIG);
        assert_eq!(
            derive_v2_key_for_account(&source, account, "legacy_user").unwrap(),
            derive_v2_aes_key("1234567890", "legacy_user")
        );
        assert!(derive_v2_key_for_account(&source, account, "another_user").is_err());
    }

    #[test]
    fn malformed_duplicate_and_zero_uin_refuse_without_sensitive_errors() {
        let fixture = Fixture::new();
        for content in [
            "[General]\n",
            "last_uin=\n",
            "last_uin=not-base64\n",
            "last_uin=YWJj\n",
            "last_uin=MA==\n",
            "last_uin=MDAwMA==\n",
            "last_uin=LTI=\n",
            "last_uin=MTIzNDU2Nzg5MA==\nlast_uin=MTIzNDU2Nzg5MA==\n",
            "last_uin=MTIzNDU2Nzg5MA==\nlast_uin = MA==\n",
        ] {
            fixture.config(true, "ab12000000000000", content);
            let error = fixture.derive().unwrap_err().to_string();
            for private in [ACCOUNT, SELF, "1234567890", "MTIzNDU2Nzg5MA=="] {
                assert!(!error.contains(private));
            }
            assert!(!error.contains(fixture.documents.to_str().unwrap()));
        }
    }

    #[test]
    fn symlinked_root_account_directory_or_config_refuses() {
        for link_level in ["config.ini", "kvcomm", "account", "ilink"] {
            let fixture = Fixture::new();
            let config = fixture.config(true, "ab12000000000000", CONFIG);
            let path = match link_level {
                "config.ini" => config,
                "kvcomm" => config.parent().unwrap().to_path_buf(),
                "account" => config.parent().unwrap().parent().unwrap().to_path_buf(),
                _ => config
                    .parent()
                    .unwrap()
                    .parent()
                    .unwrap()
                    .parent()
                    .unwrap()
                    .to_path_buf(),
            };
            let moved = fixture.documents.join("outside-original-layout");
            fs::rename(&path, &moved).unwrap();
            symlink(moved, path).unwrap();
            assert!(
                fixture.derive().is_err(),
                "accepted symlink at {link_level}"
            );
        }
    }

    #[test]
    fn hardlinked_oversized_nonregular_and_writable_config_refuse() {
        let fixture = Fixture::new();
        let path = fixture.config(true, "ab12000000000000", CONFIG);
        let other_link = fixture.documents.join("shared-config");
        fs::hard_link(&path, &other_link).unwrap();
        assert!(fixture.derive().is_err());
        fs::remove_file(other_link).unwrap();
        fs::write(&path, CONFIG.to_owned() + &" ".repeat(65536)).unwrap();
        assert!(fixture.derive().is_err());
        fs::remove_file(&path).unwrap();
        fs::create_dir(&path).unwrap();
        assert!(fixture.derive().is_err());
        fs::remove_dir(&path).unwrap();
        fs::write(&path, CONFIG).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o666)).unwrap();
        assert!(fixture.derive().is_err());
    }

    #[test]
    fn deriving_from_readonly_files_leaves_all_fixture_files_unchanged() {
        fn contents(path: &Path) -> Vec<(PathBuf, Vec<u8>)> {
            let mut files = Vec::new();
            for entry in fs::read_dir(path).unwrap() {
                let entry = entry.unwrap();
                if entry.file_type().unwrap().is_dir() {
                    files.extend(contents(&entry.path()));
                } else {
                    files.push((entry.path(), fs::read(entry.path()).unwrap()));
                }
            }
            files.sort();
            files
        }
        let fixture = Fixture::new();
        let config = fixture.config(true, "ab12000000000000", CONFIG);
        let store = fixture.documents.join("keys.toml");
        fs::write(store, "existing synthetic KeyStore stays unchanged\n").unwrap();
        fs::set_permissions(&config, fs::Permissions::from_mode(0o400)).unwrap();
        let before = contents(&fixture.documents);
        assert_eq!(fixture.derive().unwrap(), *b"d5b763f94307be38");
        assert_eq!(contents(&fixture.documents), before);
    }
}
