
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

API_URL = os.environ["API_URL"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
NTFY_URL = os.environ["NTFY_URL"]

CACHE_FILE = Path("cache.json")
GALLERY_COOKIE = os.environ.get("GALLERY_COOKIE", "").strip()

if not GALLERY_COOKIE:
    raise RuntimeError("GALLERY_COOKIE secret is missing")


TIMEZONE = ZoneInfo("Asia/Bangkok")
REQUEST_TIMEOUT = 60
MAX_RETRIES = 5
UPLOAD_DELAY = 1

BROWSER_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en,th;q=0.9,en-US;q=0.8,en-GB;q=0.7",
    "Cookie": GALLERY_COOKIE,
    "Referer": "https://m2talk.akkawit.com/",
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
                        (retry_date - datetime.now(
                            retry_date.tzinfo
                        )).total_seconds(),
                        1
                    ),
                    120
                )
            except (ValueError, TypeError):
                pass

    return min(2 ** attempt, 60)


def request(url, method="GET", headers=None, body=None):
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
            # POST is not automatically retried on 5xx.
            # Discord may have accepted the upload already.
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
    _, _, body = request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": BROWSER_HEADERS["User-Agent"]
        }
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

    if not isinstance(cache.get("uploaded"), dict):
        raise ValueError("Invalid cache.json")

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
        raise ValueError("Cannot extract UUID from photo key")

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

    while True:
        url = build_gallery_url(before)

        print(f"[Gallery] Fetching page: {before or 'first'}")

        data = get_json(url)

        page_albums = data.get("albums")

        if not isinstance(page_albums, list):
            raise ValueError("Invalid gallery response")

        albums.extend(page_albums)

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

    return data, albums


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
    url = urllib.parse.urljoin(API_URL, image_path)

    # Prevent an unexpected external URL from being fetched.
    api_origin = urllib.parse.urlsplit(API_URL)
    photo_origin = urllib.parse.urlsplit(url)

    if (
        photo_origin.scheme != api_origin.scheme
        or photo_origin.netloc != api_origin.netloc
    ):
        raise ValueError("Photo URL has an unexpected origin")

    return url


def download_photo(image_path):
    url = build_photo_url(image_path)

    _, headers, content = request(
        url,
        headers=BROWSER_HEADERS
    )

    if not content:
        raise ValueError("Empty image response")

    content_type = headers.get(
        "Content-Type", ""
    ).split(";")[0].strip().lower()

    if not content_type.startswith("image/"):
        raise ValueError(
            f"Expected image but received {content_type or 'unknown'}"
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
    boundary = "----CampBoundary" + uuid.uuid4().hex

    payload = {
        "content": (
            f"📸 Gallery Backup\n"
            f"Album: {album.get('name', 'Unknown')}\n"
            f"Album ID: {album.get('id', 'Unknown')}"
        )
    }

    parts = []

    def add(value):
        parts.append(value.encode("utf-8"))

    add(f"--{boundary}\r\n")
    add('Content-Disposition: form-data; name="payload_json"\r\n')
    add("Content-Type: application/json\r\n\r\n")
    add(json.dumps(payload, ensure_ascii=False))
    add("\r\n")

    safe_filename = filename.replace('"', "_")

    add(f"--{boundary}\r\n")
    add(
        'Content-Disposition: form-data; '
        f'name="files[0]"; filename="{safe_filename}"\r\n'
    )
    add("Content-Type: application/octet-stream\r\n\r\n")

    parts.append(content)

    add("\r\n")
    add(f"--{boundary}--\r\n")

    body = b"".join(parts)

    parsed = urllib.parse.urlsplit(DISCORD_WEBHOOK_URL)

    query = urllib.parse.parse_qsl(
        parsed.query,
        keep_blank_values=True
    )

    query = [
        (key, value)
        for key, value in query
        if key != "wait"
    ]

    query.append(("wait", "true"))

    webhook_url = urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urllib.parse.urlencode(query),
        parsed.fragment
    ))

    _, _, response = request(
        webhook_url,
        method="POST",
        headers={
            "Content-Type": (
                f"multipart/form-data; boundary={boundary}"
            ),
            "Content-Length": str(len(body))
        },
        body=body
    )

    result = json.loads(response)

    if not result.get("id"):
        raise RuntimeError("Discord did not return message ID")

    return result


# ============================================================
# ntfy
# ============================================================

def notify(message):
    request(
        NTFY_URL,
        method="POST",
        headers={
            "Title": "Camp Gallery Backup",
            "Priority": "default",
            "Tags": "camera,floppy_disk"
        },
        body=message.encode("utf-8")
    )

    print("[ntfy] Notification sent")


# ============================================================
# Main
# ============================================================

def main():
    cache = load_cache()
    uploaded = cache["uploaded"]

    sent = 0
    skipped = 0
    failed = 0
    total_albums = 0
    total_images = 0

    status = "success"
    error_message = ""

    print("=" * 55)
    print("Camp Gallery Backup")
    print("=" * 55)

    try:
        metadata, albums = get_gallery()

        total_albums = len(albums)

        reset_version = metadata.get("resetVersion")
        old_version = cache.get("resetVersion")

        if old_version != reset_version:
            print(
                f"[Gallery] resetVersion changed: "
                f"{old_version} -> {reset_version}"
            )

            # Preserve upload history even if the source resets.
            cache["resetVersion"] = reset_version
            save_cache(cache)

        print(f"[Gallery] Albums: {total_albums}")

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
                    f"[Album {album_id}] Invalid images: {error}"
                )
                continue

            for index, image_path in enumerate(images, start=1):
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

                    filename = (
                        f"{image_id}{extension}"
                    )

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

                except Exception as error:
                    failed += 1

                    print(
                        f"[Failed] Album {album_id}, "
                        f"image {index}: {error}"
                    )

        if failed:
            status = "partial"

    except Exception as error:
        status = "error"
        error_message = str(error)

        print(f"[Fatal] {error}")

    timestamp = datetime.now(TIMEZONE).strftime(
        "%d/%m/%Y %H:%M:%S"
    )

    message = "\n".join([
        f"🕒 Updated: {timestamp}",
        f"📚 Albums scanned: {total_albums}",
        f"🖼️ Images scanned: {total_images}",
        f"📤 Discord uploaded: {sent} images",
        f"⏭️ Already uploaded: {skipped} images",
        f"❌ Failed: {failed} images",
        f"💾 Total cached: {len(uploaded)} images",
        f"🔄 Reset version: {cache.get('resetVersion')}",
        f"📌 Status: {status}",
        f"⚠️ Error: {error_message}" if error_message else ""
    ])

    print("\n" + message)

    try:
        notify(message)
    except Exception as error:
        print(f"[ntfy] Failed: {error}")
        status = "error"

    if status != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
