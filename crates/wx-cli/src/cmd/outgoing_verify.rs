//! Read-only correlation of one outbox image and one exact channel event.

use std::error::Error;
use std::fs::{self, OpenOptions};
use std::io::{self, Cursor, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use image::{imageops::FilterType, DynamicImage, ImageDecoder, ImageReader};
use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Value};

use super::channel::{self, BindingView};
use super::outgoing_media::{self, Media};

const MAX_DAT_BYTES: u64 = 64 * 1024 * 1024;

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

pub(super) fn verify(
    state_dir: &Path,
    binding: &BindingView,
    conn: &Connection,
    request_id: &str,
    cursor: i64,
) -> Result<Value, Box<dyn Error>> {
    if cursor <= 0 || !binding.encrypted_snapshot {
        return Err(invalid(
            "image verification requires an encrypted channel and positive cursor",
        )
        .into());
    }
    let row: (String, String, String, String, String, String, i64, i64) = conn.query_row(
        "SELECT content_kind,status,channel_id,session_id,media_path,media_sha256,media_size_bytes,attempt_number
         FROM outgoing_intent WHERE request_id=?1",
        [request_id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?)),
    ).optional()?.ok_or_else(|| invalid("image intent is absent"))?;
    if row.0 != "image"
        || row.2 != binding.channel_id
        || row.3 != binding.session_id
        || row.1 == "media_queued"
        || row.7 != 1
    {
        return Err(invalid("image intent is not an attempted request for this channel").into());
    }
    let mime: String = conn.query_row(
        "SELECT media_mime_type FROM outgoing_intent WHERE request_id=?1",
        [request_id],
        |r| r.get(0),
    )?;
    let mime_type = match mime.as_str() {
        "image/png" => "image/png",
        "image/jpeg" => "image/jpeg",
        _ => return Err(invalid("image intent has invalid MIME type").into()),
    };
    let frozen = Media {
        path: PathBuf::from(row.4),
        sha256: row.5,
        mime_type,
        size_bytes: u64::try_from(row.6)?,
    };
    if frozen.path != outgoing_media::frozen_path(state_dir, request_id)? {
        return Err(invalid("image intent has an invalid frozen path").into());
    }
    let frozen_bytes = outgoing_media::read_verified(&frozen)?;
    let event = channel::event_at_cursor(state_dir, cursor)?;
    let md5 = event_md5(&event, binding)?.to_ascii_lowercase();
    let attach_root = Path::new(&binding.source).join("msg/attach");
    let lookup = wx_media::resolve_image_by_md5(&binding.talker, &attach_root, &md5)
        .map_err(|_| invalid("event image asset is unavailable"))?;
    let selected = lookup
        .recommended
        .ok_or_else(|| invalid("event has no image asset"))?;
    let exact_name = format!("{md5}.dat");
    let high_quality_name = format!("{md5}_h.dat");
    if !selected
        .file_name()
        .is_some_and(|name| name == exact_name.as_str() || name == high_quality_name.as_str())
    {
        return Err(invalid("event has no exact full image asset").into());
    }
    let source = decode_image(&frozen_bytes)?;
    let options = decrypt_options(binding, &attach_root)?;
    let dat = copy_encrypted_dat(state_dir, &attach_root, &selected)?;
    let decoded = wx_media::decrypt_dat(&dat, &options)
        .map_err(|_| invalid("event image could not be decrypted"))?;
    let image_data = if decoded.ext == "wxgf" {
        let result = wx_media::transcode_wxgf(&decoded.data)
            .map_err(|_| invalid("event image could not be decoded"))?;
        if !result.transcoded {
            return Err(invalid("event image has unsupported format").into());
        }
        result.data
    } else {
        decoded.data
    };
    let observed = decode_image(&image_data)?;
    let (comparison, mean_abs_error, max_abs_error) = compare_images(&source, &observed)
        .ok_or_else(|| invalid("event image does not match frozen intent"))?;
    Ok(
        json!({"verified": true, "request_id": request_id, "cursor": cursor,
        "media_sha256": frozen.sha256, "event_media_md5": md5,
        "comparison": comparison, "mean_abs_error": mean_abs_error,
        "max_abs_error": max_abs_error}),
    )
}

fn event_md5<'a>(event: &'a Value, binding: &BindingView) -> Result<&'a str, Box<dyn Error>> {
    let message = &event["message"];
    let md5 = message["content"]["Image"]["md5"]
        .as_str()
        .ok_or_else(|| invalid("event image has no MD5"))?;
    if event["kind"] != "message_observed"
        || event["direction"] != "outgoing"
        || event["channel_id"] != binding.channel_id
        || event["session_id"] != binding.session_id
        || event["account"] != binding.account
        || event["talker"] != binding.talker
        || message["talker"] != binding.talker
        || message["sender"].as_str() != binding.self_wxid.as_deref()
        || message["msg_type"] != 3
        || md5.len() != 32
        || !md5.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return Err(invalid("event is not an exact outgoing image for this binding").into());
    }
    Ok(md5)
}

fn decrypt_options(
    binding: &BindingView,
    attach_root: &Path,
) -> Result<wx_media::DatDecryptOptions, Box<dyn Error>> {
    let store = wx_keychain::KeyStore::load_default()?;
    let key = store
        .get(&binding.account)
        .and_then(|entry| entry.image_aes_key.as_deref())
        .and_then(|hex_key| hex::decode(hex_key).ok())
        .filter(|raw| raw.len() == 16);
    let v2_aes_key = key.map(|raw| {
        let mut fixed = [0_u8; 16];
        fixed.copy_from_slice(&raw);
        fixed
    });
    let username_hash = format!("{:x}", wx_media::md5_hash(binding.talker.as_bytes()));
    let xor_key = wx_media::detect_xor_key(&attach_root.join(username_hash));
    Ok(wx_media::DatDecryptOptions {
        v2_aes_key,
        xor_key,
    })
}

fn copy_encrypted_dat(
    state_dir: &Path,
    attach_root: &Path,
    path: &Path,
) -> Result<Vec<u8>, Box<dyn Error>> {
    let root = attach_root.canonicalize()?;
    let canonical = path.canonicalize()?;
    if canonical != path || !canonical.starts_with(&root) {
        return Err(invalid("event image path is outside its attachment root").into());
    }
    let source = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let before = source.metadata()?;
    if !before.is_file() || before.len() == 0 || before.len() > MAX_DAT_BYTES {
        return Err(invalid("event image asset has invalid size or type").into());
    }
    let mut temp = tempfile::NamedTempFile::new_in(state_dir)?;
    let copied = io::copy(&mut (&source).take(MAX_DAT_BYTES + 1), &mut temp)?;
    let after = source.metadata()?;
    if copied != before.len()
        || before.len() != after.len()
        || before.dev() != after.dev()
        || before.ino() != after.ino()
        || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || before.ctime() != after.ctime()
        || before.ctime_nsec() != after.ctime_nsec()
    {
        return Err(invalid("event image asset changed while copying").into());
    }
    temp.flush()?;
    Ok(fs::read(temp.path())?)
}

fn decode_image(bytes: &[u8]) -> Result<DynamicImage, Box<dyn Error>> {
    let reader = ImageReader::new(Cursor::new(bytes)).with_guessed_format()?;
    let mut decoder = reader.into_decoder()?;
    let (width, height) = decoder.dimensions();
    if width == 0
        || height == 0
        || width > 8192
        || height > 8192
        || u64::from(width) * u64::from(height) > 40_000_000
    {
        return Err(invalid("decoded image dimensions exceed limit").into());
    }
    let orientation = decoder.orientation()?;
    let mut image = DynamicImage::from_decoder(decoder)?;
    image.apply_orientation(orientation);
    Ok(image)
}

/// Strict fallback for JPEG recompression/downscale: same aspect ratio within 1%,
/// opaque pixels, 64x64 triangular resampling, mean channel error <=3 and max <=48.
fn compare_images(left: &DynamicImage, right: &DynamicImage) -> Option<(&'static str, f64, u8)> {
    let a = left.to_rgba8();
    let b = right.to_rgba8();
    if a.dimensions() == b.dimensions() && a.as_raw() == b.as_raw() {
        return Some(("exact-pixels", 0.0, 0));
    }
    if a.pixels().any(|pixel| pixel[3] != 255) || b.pixels().any(|pixel| pixel[3] != 255) {
        return None;
    }
    let (aw, ah) = a.dimensions();
    let (bw, bh) = b.dimensions();
    if aw.min(ah).min(bw).min(bh) < 64 {
        return None;
    }
    let cross_a = u64::from(aw) * u64::from(bh);
    let cross_b = u64::from(bw) * u64::from(ah);
    if cross_a.abs_diff(cross_b) * 100 > cross_a.max(cross_b) {
        return None;
    }
    let resized_a = image::imageops::resize(&left.to_rgb8(), 64, 64, FilterType::Triangle);
    let resized_b = image::imageops::resize(&right.to_rgb8(), 64, 64, FilterType::Triangle);
    let mut total = 0_u64;
    let mut maximum = 0_u8;
    for (&x, &y) in resized_a.as_raw().iter().zip(resized_b.as_raw()) {
        let diff = x.abs_diff(y);
        total += u64::from(diff);
        maximum = maximum.max(diff);
    }
    let mean = total as f64 / (64.0 * 64.0 * 3.0);
    (mean <= 3.0 && maximum <= 48).then_some(("bounded-reencode", mean, maximum))
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{GenericImageView, ImageFormat, Rgb, RgbImage};

    fn sample() -> DynamicImage {
        DynamicImage::ImageRgb8(RgbImage::from_fn(128, 96, |x, y| {
            Rgb([((x / 32) * 40 + 20) as u8, ((y / 32) * 40 + 30) as u8, 100])
        }))
    }

    #[test]
    fn exact_reencode_and_unrelated_pixels_have_distinct_results() {
        let source = sample();
        assert_eq!(compare_images(&source, &source).unwrap().0, "exact-pixels");
        let mut encoded = Cursor::new(Vec::new());
        source.write_to(&mut encoded, ImageFormat::Jpeg).unwrap();
        let jpeg = decode_image(&encoded.into_inner()).unwrap();
        assert_eq!(
            compare_images(&source, &jpeg).unwrap().0,
            "bounded-reencode"
        );
        let unrelated = DynamicImage::ImageRgb8(RgbImage::from_pixel(128, 96, Rgb([255, 0, 0])));
        assert!(compare_images(&source, &unrelated).is_none());
    }

    #[test]
    fn jpeg_exif_orientation_is_normalized_before_comparison() {
        let mut encoded = Cursor::new(Vec::new());
        sample().write_to(&mut encoded, ImageFormat::Jpeg).unwrap();
        let original = encoded.into_inner();
        // APP1 Exif with one little-endian Orientation=6 (rotate 90 degrees) tag.
        let mut with_exif = vec![0xff, 0xd8, 0xff, 0xe1, 0x00, 0x22];
        with_exif.extend_from_slice(
            b"Exif\0\0II\x2a\0\x08\0\0\0\x01\0\x12\x01\x03\0\x01\0\0\0\x06\0\0\0\0\0\0\0",
        );
        with_exif.extend_from_slice(&original[2..]);
        let oriented = decode_image(&with_exif).unwrap();
        let expected = decode_image(&original).unwrap().rotate90();
        assert_eq!(oriented.dimensions(), (96, 128));
        assert_eq!(
            compare_images(&oriented, &expected).unwrap().0,
            "exact-pixels"
        );
    }

    #[test]
    fn event_identity_and_image_md5_are_required() {
        let binding = BindingView {
            channel_id: "channel".into(),
            session_id: "session".into(),
            account: "account".into(),
            talker: "room@chatroom".into(),
            source: "unused".into(),
            encrypted_snapshot: true,
            self_wxid: Some("self".into()),
        };
        let event = json!({"kind":"message_observed","direction":"outgoing",
            "channel_id":"channel","session_id":"session","account":"account",
            "talker":"room@chatroom","message":{"talker":"room@chatroom",
                "sender":"self","msg_type":3,"content":{"Image":{"md5":"0123456789abcdef0123456789abcdef"}}}});
        assert!(event_md5(&event, &binding).is_ok());
        let mut wrong = event.clone();
        wrong["message"]["sender"] = json!("other");
        assert!(event_md5(&wrong, &binding).is_err());
        let mut wrong = event.clone();
        wrong["channel_id"] = json!("other-channel");
        assert!(event_md5(&wrong, &binding).is_err());
        let mut missing = event;
        missing["message"]["content"]["Image"]["md5"] = Value::Null;
        assert!(event_md5(&missing, &binding).is_err());
    }
}
