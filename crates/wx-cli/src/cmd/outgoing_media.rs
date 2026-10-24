//! Private, immutable image bytes owned by the outgoing journal.

use std::error::Error;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use image::ImageFormat;
use serde_json::json;
use sha2::{Digest, Sha256};

const MAX_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Media {
    pub path: PathBuf,
    pub sha256: String,
    pub mime_type: &'static str,
    pub size_bytes: u64,
}

impl Media {
    pub fn descriptor(&self) -> serde_json::Value {
        json!({"path": self.path, "sha256": self.sha256, "mime_type": self.mime_type, "size_bytes": self.size_bytes})
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn private(path: &Path, directory: bool) -> Result<(), Box<dyn Error>> {
    let meta = fs::symlink_metadata(path)?;
    if meta.file_type().is_symlink()
        || meta.uid() != unsafe { libc::geteuid() }
        || (directory && (!meta.is_dir() || meta.permissions().mode() & 0o777 != 0o700))
        || (!directory
            && (!meta.is_file() || meta.nlink() != 1 || meta.permissions().mode() & 0o777 != 0o600))
    {
        return Err(invalid("outgoing image storage must be private and owned").into());
    }
    Ok(())
}

fn image_bytes(path: &Path) -> Result<Vec<u8>, Box<dyn Error>> {
    let file = OpenOptions::new()
        .read(true)
        // Opening a FIFO without O_NONBLOCK can hang before metadata rejects it.
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let before = file.metadata()?;
    if !before.file_type().is_file() || before.len() == 0 || before.len() > MAX_BYTES as u64 {
        return Err(invalid("image must be a regular file of at most 16 MiB").into());
    }
    let mut bytes = Vec::new();
    (&file).take(MAX_BYTES as u64 + 1).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    if bytes.len() as u64 != before.len()
        || bytes.len() > MAX_BYTES
        || before.dev() != after.dev()
        || before.ino() != after.ino()
        || before.len() != after.len()
        || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || before.ctime() != after.ctime()
        || before.ctime_nsec() != after.ctime_nsec()
    {
        return Err(invalid("image changed while being read").into());
    }
    Ok(bytes)
}

fn png_single_frame(bytes: &[u8]) -> bool {
    if !bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        return false;
    }
    let mut offset = 8usize;
    loop {
        if offset + 12 > bytes.len() {
            return false;
        }
        let length = u32::from_be_bytes(bytes[offset..offset + 4].try_into().unwrap()) as usize;
        let Some(end) = offset.checked_add(12).and_then(|n| n.checked_add(length)) else {
            return false;
        };
        if end > bytes.len() {
            return false;
        }
        let kind = &bytes[offset + 4..offset + 8];
        if kind == b"acTL" {
            return false;
        }
        if kind == b"IEND" {
            return length == 0 && end == bytes.len();
        }
        offset = end;
    }
}

fn jpeg_single_frame(bytes: &[u8]) -> bool {
    if !bytes.starts_with(b"\xff\xd8") {
        return false;
    }
    let mut offset = 2usize;
    let mut in_scan = false;
    loop {
        if offset >= bytes.len() || bytes[offset] != 0xff {
            return false;
        }
        while offset < bytes.len() && bytes[offset] == 0xff {
            offset += 1;
        }
        if offset >= bytes.len() {
            return false;
        }
        let marker = bytes[offset];
        offset += 1;
        match marker {
            0xd9 => return in_scan && offset == bytes.len(),
            0xd8 | 0x00 => return false,
            0xd0..=0xd7 if in_scan => {}
            0x01 => {}
            _ => {
                if offset + 2 > bytes.len() {
                    return false;
                }
                let length = u16::from_be_bytes([bytes[offset], bytes[offset + 1]]) as usize;
                if length < 2 || offset + length > bytes.len() {
                    return false;
                }
                if marker == 0xe2 && bytes[offset + 2..offset + length].starts_with(b"MPF\0") {
                    return false;
                }
                offset += length;
                if marker == 0xda {
                    in_scan = true;
                }
            }
        }
        if in_scan {
            // Within entropy data, only unstuffed marker bytes have meaning.
            while offset < bytes.len() {
                if bytes[offset] != 0xff {
                    offset += 1;
                    continue;
                }
                if offset + 1 >= bytes.len() {
                    return false;
                }
                if bytes[offset + 1] == 0x00 {
                    offset += 2;
                    continue;
                }
                if (0xd0..=0xd7).contains(&bytes[offset + 1]) {
                    offset += 2;
                    continue;
                }
                break;
            }
        }
    }
}

pub(super) fn inspect(path: &Path) -> Result<(Vec<u8>, Media), Box<dyn Error>> {
    let bytes = image_bytes(path)?;
    let (format, mime_type) = if png_single_frame(&bytes) {
        (ImageFormat::Png, "image/png")
    } else if jpeg_single_frame(&bytes) {
        (ImageFormat::Jpeg, "image/jpeg")
    } else {
        return Err(invalid("only single-frame PNG or JPEG images are supported").into());
    };
    let decoder = image::ImageReader::with_format(io::Cursor::new(&bytes), format);
    let (width, height) = decoder.into_dimensions()?;
    if width == 0
        || height == 0
        || width > 8192
        || height > 8192
        || u64::from(width) * u64::from(height) > 40_000_000
    {
        return Err(invalid("image dimensions exceed limit").into());
    }
    // A full decode catches truncated or invalid compressed data before an intent is persisted.
    image::load_from_memory_with_format(&bytes, format)?;
    let media = Media {
        path: PathBuf::new(),
        sha256: hex::encode(Sha256::digest(&bytes)),
        mime_type,
        size_bytes: bytes.len() as u64,
    };
    Ok((bytes, media))
}

pub(super) fn frozen_path(state_dir: &Path, request_id: &str) -> Result<PathBuf, Box<dyn Error>> {
    private(state_dir, true)?;
    let directory = state_dir.join("outgoing-media");
    if !directory.exists() {
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700).create(&directory)?;
    }
    private(&directory, true)?;
    Ok(directory.canonicalize()?.join(request_id))
}

pub(super) fn freeze(
    state_dir: &Path,
    request_id: &str,
    bytes: &[u8],
    media: &mut Media,
) -> Result<(), Box<dyn Error>> {
    let path = frozen_path(state_dir, request_id)?;
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&path)
    {
        Ok(mut file) => {
            file.write_all(bytes)?;
            file.sync_all()?;
        }
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    media.path = path;
    verify(media)?;
    Ok(())
}

pub(super) fn verify(media: &Media) -> Result<(), Box<dyn Error>> {
    read_verified(media).map(|_| ())
}

pub(super) fn read_verified(media: &Media) -> Result<Vec<u8>, Box<dyn Error>> {
    private(
        media
            .path
            .parent()
            .ok_or_else(|| invalid("frozen image has no parent"))?,
        true,
    )?;
    private(&media.path, false)?;
    let bytes = image_bytes(&media.path)?;
    if bytes.len() as u64 != media.size_bytes || hex::encode(Sha256::digest(&bytes)) != media.sha256
    {
        return Err(invalid("frozen image is missing or changed").into());
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;

    #[test]
    fn fifo_image_input_is_rejected_without_a_writer() {
        let temp = tempfile::tempdir().unwrap();
        let fifo = temp.path().join("image-fifo");
        let name = CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let error = inspect(&fifo).unwrap_err().to_string();
        assert!(error.contains("regular file"), "{error}");
    }
}
