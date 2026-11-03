import os
import re
import json
import time
import uuid
import mimetypes
import urllib.request
import urllib.error
import urllib.parse

from pathlib import Path
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo


# ============================================================
# Configuration
# ============================================================

API_URL = os.environ.get("API_URL", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL", ""
).strip()
NTFY_URL = os.environ.get("NTFY_URL", "").strip()
GALLERY_COOKIE = os.environ.get("GALLERY_COOKIE", "").strip()

CACHE_FILE = Path("cache.json")

TIMEZONE = ZoneInfo("Asia/Bangkok")

REQUEST_TIMEOUT = 60
MAX_RETRIES = 5
UPLOAD_DELAY = 1

BROWSER_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en,th;q=0.9,en-US;q=0.8,en-GB;q=0.7",
    "Cookie": GALLERY_COOKIE,
    "Priority": "u=1, i",
    "Referer": "https://m2talk.akkawit.com/",
    "Sec-CH-UA": (
        '"Not:A-Brand";v="8", '
        '"Chromium";v="156", '
        '"Microsoft Edge";v="156"'
    ),
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
    ),
}

UUID_PATTERN = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12})$",
    re.IGNORECASE
)


# ============================================================
# Exceptions
# ============================================================

class CookieAuthenticationError(Exception):
    """Gallery authentication failed (401/403)."""
    pass


# ============================================================
# HTTP
# ============================================================

def retry_delay(headers, attempt):
    value = headers.get("Retry-After", "")

    if value:
        try:
            return min(float(value), 120)

        except ValueError:
            try:
                retry_date = parsedate_to_datetime(value)

                return min(
                    max(
                        (
                            retry_date - datetime.now(
                                retry_date.tzinfo
                            )
                        ).total_seconds(),
                        1
                    ),
                    120
                )

            except (ValueError, TypeError):
                pass

    return min(2 ** attempt, 60)


def request(
    url,
    method="GET",
    headers=None,
    body=None,
    gallery_request=False
):
    headers = headers or {}

    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method
        )

        try:
            with urllib.request.urlopen(
                req,
                timeout=REQUEST_TIMEOUT
            ) as response:
                return (
                    response.status,
                    response.headers,
                    response.read()
                )

        except urllib.error.HTTPError as error:

            # Only classify 401/403 as gallery authentication
            # failures when the request is to the gallery.
            if gallery_request and error.code in (401, 403):
                raise CookieAuthenticationError(
                    f"Gallery authentication rejected "
                    f"(HTTP {error.code}). "
                    "Cookie may be expired, invalid, or blocked "
                    "by Cloudflare."
                ) from error

            # Retry HTTP 429 and GET 5xx responses.
            # Do not automatically retry Discord POST 5xx.
            retryable = (
                error.code == 429
                or (
                    method == "GET"
                    and error.code >= 500
                )
            )

            if not retryable or attempt >= MAX_RETRIES:
                raise

            delay = retry_delay(error.headers, attempt)

            print(
                f"[HTTP] {error.code}, retrying in {delay}s"
            )

            time.sleep(delay)

        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError
        ) as error:

            # Retrying POST after a connection failure can
            # produce duplicate Discord messages.
            if method != "GET" or attempt >= MAX_RETRIES:
                raise

            delay = min(2 ** attempt, 60)

            print(
                f"[HTTP] Network error, retrying in {delay}s"
            )

            time.sleep(delay)

    raise RuntimeError("Maximum retries exceeded")


def get_json(url):
    headers = dict(BROWSER_HEADERS)
    headers["Accept"] = "application/json"

    _, _, body = request(
        url,
        headers=headers,
        gallery_request=True
    )

    return json.loads(body)


# ============================================================
# Cache
# ============================================================

def load_cache():
    if not CACHE_FILE.exists():
        return {
            "resetVersion": None,
            "uploaded": {}
        }

    with CACHE_FILE.open("r", encoding="utf-8") as file:
        cache = json.load(file)

    if not isinstance(cache, dict):
        raise ValueError("Invalid cache.json")

    if not isinstance(cache.get("uploaded"), dict):
        raise ValueError("Invalid cache.json: uploaded must be an object")

    cache.setdefault("resetVersion", None)

    return cache


def save_cache(cache):
    temporary = CACHE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(
            cache,
            file,
            ensure_ascii=False,
            indent=2
        )

        file.write("\n")

    temporary.replace(CACHE_FILE)


def get_image_id(image_url):
    parsed = urllib.parse.urlparse(image_url)

    query = urllib.parse.parse_qs(parsed.query)
    keys = query.get("key", [])

    if not keys:
        raise ValueError("Photo key not found")

    key = keys[0]

    match = UUID_PATTERN.search(key)

    if not match:
        raise ValueError(
            "Cannot extract UUID from photo key"
        )

    return match.group(1).lower()


# ============================================================
# Gallery Pagination
# ============================================================

def build_gallery_url(before=None):
    if before is None:
        return API_URL

    parsed = urllib.parse.urlsplit(API_URL)

    query = urllib.parse.parse_qsl(
        parsed.query,
        keep_blank_values=True
    )

    query = [
        (key, value)
        for key, value in query
        if key != "before"
    ]

    query.append(("before", str(before)))

    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urllib.parse.urlencode(query),
        parsed.fragment
    ))


def get_gallery():
    albums = []
    visited_cursors = set()
    before = None
    metadata = {}

    while True:
        url = build_gallery_url(before)

        print(
            f"[Gallery] Fetching page: "
            f"{before if before is not None else 'first'}"
        )

        data = get_json(url)

        if not isinstance(data, dict):
            raise ValueError("Invalid gallery response")

        page_albums = data.get("albums")

        if not isinstance(page_albums, list):
            raise ValueError("Invalid gallery response: albums")

        albums.extend(page_albums)

        # Preserve metadata from the latest response.
        metadata = data

        print(
            f"[Gallery] Received {len(page_albums)} albums "
            f"(total: {len(albums)})"
        )

        if not data.get("hasMore"):
            break

        next_before = data.get("nextBefore")

        if next_before is None:
            raise ValueError("Missing nextBefore")

        cursor = str(next_before)

        if cursor in visited_cursors:
            raise ValueError("Repeated pagination cursor")

        visited_cursors.add(cursor)
        before = next_before

    return metadata, albums


def parse_images(value):
    if isinstance(value, list):
        return value

    if isinstance(value, str):
        images = json.loads(value)

        if not isinstance(images, list):
            raise ValueError("Invalid images JSON")

        return images

    return []


# ============================================================
# Photo Download
# ============================================================

def build_photo_url(image_path):
    url = urllib.parse.urljoin(
        API_URL,
        image_path
    )

    # Prevent an unexpected external URL from being fetched.
    api_origin = urllib.parse.urlsplit(API_URL)
    photo_origin = urllib.parse.urlsplit(url)

    if (
        photo_origin.scheme != api_origin.scheme
        or photo_origin.netloc != api_origin.netloc
    ):
        raise ValueError(
            "Photo URL has an unexpected origin"
        )

    return url


def download_photo(image_path):
    url = build_photo_url(image_path)

    # Cookie is included in BROWSER_HEADERS.
    _, headers, content = request(
        url,
        headers=dict(BROWSER_HEADERS),
        gallery_request=True
    )

    if not content:
        raise ValueError("Empty image response")

    content_type = headers.get(
        "Content-Type", ""
    ).split(";")[0].strip().lower()

    if not content_type.startswith("image/"):
        raise ValueError(
            f"Expected image but received "
            f"{content_type or 'unknown'}"
        )

    extension = mimetypes.guess_extension(content_type)

    if extension == ".jpe":
        extension = ".jpg"

    if not extension:
        extension = ".jpg"

    return content, extension


# ============================================================
# Discord Multipart Upload
# ============================================================

def upload_discord(content, filename, album):
    boundary = uuid.uuid4().hex
    payload = json.dumps({"content": f"Album {album}"})

    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        payload.encode(), b"\r\n",
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        content, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])

    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL + "?wait=true",   # wait=true → ได้ id กลับมา
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "DiscordBot (camp-gallery-backup, 1.0)",  # สำคัญสุด
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Discord HTTP {e.code}: {detail}") from e


# ============================================================
# Notifications
# ============================================================

def notify_ntfy(message):
    if not NTFY_URL:
        print("[ntfy] NTFY_URL is missing")
        return False

    request(
        NTFY_URL,
        method="POST",
        headers={
            "Title": "Camp Gallery Backup",
            "Priority": "high" if "Cookie" in message else "default",
            "Tags": "warning,camera"
        },
        body=message.encode("utf-8")
    )

    print("[ntfy] Notification sent")

    return True


def notify_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("[Discord] DISCORD_WEBHOOK_URL is missing")
        return False

    payload = {
        "content": message
    }

    body = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    _, _, _ = request(
        DISCORD_WEBHOOK_URL,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "CampGalleryBackup/1.0"
        },
        body=body
    )

    print("[Discord] Notification sent")

    return True


def notify_authentication_failure(error):
    message = (
        "🚨 Camp Gallery Backup: Authentication Failed\n\n"
        "ไม่สามารถเข้าถึง Gallery หรือดาวน์โหลดรูปได้\n\n"
        f"⚠️ Error: {error}\n\n"
        "สาเหตุที่เป็นไปได้:\n"
        "• GALLERY_COOKIE หมดอายุหรือไม่ถูกต้อง\n"
        "• Cloudflare ปฏิเสธ GitHub Actions runner\n"
        "• Session ถูกยกเลิกจากเว็บไซต์\n\n"
        "กรุณาเข้าสู่เว็บไซต์ m2talk ใหม่ "
        "และอัปเดต GALLERY_COOKIE ใน GitHub Secrets"
    )

    print("[Authentication] Sending alerts...")

    try:
        notify_discord(message)

    except Exception as notify_error:
        print(
            f"[Discord Alert Failed] {notify_error}"
        )

    try:
        notify_ntfy(message)

    except Exception as notify_error:
        print(
            f"[ntfy Alert Failed] {notify_error}"
        )


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 55)
    print("Camp Gallery Backup")
    print("=" * 55)

    cache = load_cache()
    uploaded = cache["uploaded"]

    sent = 0
    skipped = 0
    failed = 0

    total_albums = 0
    total_images = 0

    status = "success"
    error_message = ""
    authentication_error = False

    try:
        # Validate required configuration.
        if not API_URL:
            raise RuntimeError("API_URL secret is missing")

        if not DISCORD_WEBHOOK_URL:
            raise RuntimeError(
                "DISCORD_WEBHOOK_URL secret is missing"
            )

        if not NTFY_URL:
            raise RuntimeError("NTFY_URL secret is missing")

        if not GALLERY_COOKIE:
            raise CookieAuthenticationError(
                "GALLERY_COOKIE secret is missing"
            )

        # ----------------------------------------------------
        # Fetch Gallery
        # ----------------------------------------------------

        metadata, albums = get_gallery()

        total_albums = len(albums)

        reset_version = metadata.get("resetVersion")
        old_version = cache.get("resetVersion")

        if old_version != reset_version:
            print(
                f"[Gallery] resetVersion changed: "
                f"{old_version} -> {reset_version}"
            )

            # Preserve upload history even if source resets.
            cache["resetVersion"] = reset_version
            save_cache(cache)

        print(f"[Gallery] Albums: {total_albums}")

        # ----------------------------------------------------
        # Process Albums
        # ----------------------------------------------------

        for album in albums:
            album_id = album.get("id")
            album_name = album.get("name", "Unknown")

            try:
                images = parse_images(
                    album.get("images", [])
                )

            except Exception as error:
                failed += 1

                print(
                    f"[Album {album_id}] "
                    f"Invalid images: {error}"
                )

                continue

            for index, image_path in enumerate(
                images,
                start=1
            ):
                total_images += 1

                try:
                    image_id = get_image_id(image_path)

                    # Skip before downloading.
                    if image_id in uploaded:
                        skipped += 1

                        print(f"[Skip] {image_id}")

                        continue

                    print(
                        f"[Download] Album {album_id}, "
                        f"image {index}: {image_id}"
                    )

                    content, extension = download_photo(
                        image_path
                    )

                    filename = f"{image_id}{extension}"

                    print(
                        f"[Upload] {filename} "
                        f"({len(content)} bytes)"
                    )

                    result = upload_discord(
                        content,
                        filename,
                        album
                    )

                    # Only record after confirmed Discord success.
                    uploaded[image_id] = {
                        "albumId": album_id,
                        "albumName": album_name,
                        "filename": filename,
                        "discordMessageId": result["id"],
                        "uploadedAt": datetime.now(
                            TIMEZONE
                        ).isoformat()
                    }

                    # Persist immediately.
                    save_cache(cache)

                    sent += 1

                    print(
                        f"[Success] {image_id} "
                        f"-> {result['id']}"
                    )

                    time.sleep(UPLOAD_DELAY)

                except CookieAuthenticationError:
                    # Stop immediately if Gallery rejects Cookie.
                    raise

                except Exception as error:
                    failed += 1

                    print(
                        f"[Failed] Album {album_id}, "
                        f"image {index}: {error}"
                    )

        if failed:
            status = "partial"

    except CookieAuthenticationError as error:
        status = "error"
        authentication_error = True
        error_message = str(error)

        print(f"[Authentication Error] {error}")

    except Exception as error:
        status = "error"
        error_message = str(error)

        print(f"[Fatal] {error}")

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    timestamp = datetime.now(TIMEZONE).strftime(
        "%d/%m/%Y %H:%M:%S"
    )

    summary_lines = [
        "=======================================================",
        "Camp Gallery Backup",
        "=======================================================",
        f"🕒 Updated: {timestamp}",
        f"📚 Albums scanned: {total_albums}",
        f"🖼️ Images scanned: {total_images}",
        f"📤 Discord uploaded: {sent} images",
        f"⏭️ Already uploaded: {skipped} images",
        f"❌ Failed: {failed} images",
        f"💾 Total cached: {len(uploaded)} images",
        f"🔄 Reset version: {cache.get('resetVersion')}",
        f"📌 Status: {status}",
    ]

    if error_message:
        summary_lines.append(
            f"⚠️ Error: {error_message}"
        )

    if authentication_error:
        summary_lines.extend([
            "",
            "🚨 Authentication failed.",
            "Please refresh GALLERY_COOKIE in GitHub Secrets."
        ])

    message = "\n".join(summary_lines)

    print("\n" + message)

    # --------------------------------------------------------
    # Authentication Alerts
    # --------------------------------------------------------

    if authentication_error:
        notify_authentication_failure(error_message)

    # --------------------------------------------------------
    # ntfy Summary
    # --------------------------------------------------------

    try:
        notify_ntfy(message)

    except Exception as error:
        print(f"[ntfy] Failed: {error}")

        if status == "success":
            status = "error"

    # --------------------------------------------------------
    # Exit
    # --------------------------------------------------------

    if status != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
