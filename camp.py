
import os
import json
import time
import hashlib
import mimetypes
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


# ============================================================
# Configuration: GitHub Secrets
# ============================================================

API_URL = os.environ["API_URL"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
NTFY_URL = os.environ["NTFY_URL"]

STATE_FILE = Path("data/uploaded.json")

TIMEZONE = ZoneInfo("Asia/Bangkok")
MAX_RETRIES = 5
REQUEST_TIMEOUT = 60


# ============================================================
# HTTP
# ============================================================

def http_request(url, method="GET", headers=None, body=None):
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
                return response.status, response.headers, response.read()

        except urllib.error.HTTPError as error:
            if (
                error.code == 429 or error.code >= 500
            ) and attempt < MAX_RETRIES:
                retry_after = error.headers.get("Retry-After", "5")

                try:
                    delay = min(float(retry_after), 120)
                except ValueError:
                    delay = 5

                print(f"HTTP {error.code}, retrying in {delay}s")
                time.sleep(delay)
                continue

            raise

        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError
        ):
            if attempt >= MAX_RETRIES:
                raise

            delay = min(2 ** attempt, 60)
            print(f"Network error, retrying in {delay}s")
            time.sleep(delay)

    raise RuntimeError("Maximum retries exceeded")


def http_json(url):
    _, _, body = http_request(
        url,
        headers={"Accept": "application/json"}
    )
    return json.loads(body)


# ============================================================
# State
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {"uploaded": []}

    with STATE_FILE.open("r", encoding="utf-8") as file:
        state = json.load(file)

    if not isinstance(state.get("uploaded"), list):
        raise ValueError("Invalid uploaded.json format")

    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2
        )
        file.write("\n")

    temporary.replace(STATE_FILE)


def make_record_key(image_url):
    parsed = urlparse(image_url)
    key = parse_qs(parsed.query).get("key", [None])[0]

    if not key:
        key = image_url

    return hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()


# ============================================================
# Gallery Pagination
# ============================================================

def get_gallery():
    albums = []
    before = None
    visited = set()

    while True:
        url = API_URL

        if before is not None:
            separator = "&" if "?" in url else "?"
            url += separator + urlencode({"before": before})

        print(f"[Gallery] Fetching: page cursor={before}")

        data = http_json(url)

        page_albums = data.get("albums", [])

        if not isinstance(page_albums, list):
            raise ValueError("Invalid albums response")

        albums.extend(page_albums)

        print(
            f"[Gallery] Received {len(page_albums)} albums "
            f"(total: {len(albums)})"
        )

        if not data.get("hasMore"):
            break

        next_before = data.get("nextBefore")

        if next_before is None:
            raise ValueError("Missing nextBefore cursor")

        cursor = str(next_before)

        if cursor in visited:
            raise ValueError("Repeated pagination cursor")

        visited.add(cursor)
        before = next_before

    return albums


def parse_images(value):
    if isinstance(value, list):
        return value

    if isinstance(value, str):
        result = json.loads(value)

        if not isinstance(result, list):
            raise ValueError("Invalid images JSON")

        return result

    return []


# ============================================================
# Discord Multipart Upload
# ============================================================

def build_multipart(filename, content, album):
    boundary = "----CampBoundary7MA4YWxkTrZu0gW"

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

    add(f"--{boundary}\r\n")
    add(
        f'Content-Disposition: form-data; '
        f'name="files[0]"; filename="{filename}"\r\n'
    )
    add("Content-Type: application/octet-stream\r\n\r\n")

    parts.append(content)
    add("\r\n")
    add(f"--{boundary}--\r\n")

    return boundary, b"".join(parts)


def upload_discord(content, filename, album):
    boundary, body = build_multipart(
        filename,
        content,
        album
    )

    separator = "&" if "?" in DISCORD_WEBHOOK_URL else "?"
    webhook_url = DISCORD_WEBHOOK_URL + separator + "wait=true"

    _, _, response = http_request(
        webhook_url,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body))
        },
        body=body
    )

    result = json.loads(response)

    if not result.get("id"):
        raise RuntimeError("Discord did not return a message ID")

    return result


# ============================================================
# Download
# ============================================================

def download_image(image_url):
    _, headers, content = http_request(image_url)

    content_type = headers.get(
        "Content-Type",
        "application/octet-stream"
    ).split(";")[0].strip()

    extension = mimetypes.guess_extension(content_type) or ".jpg"

    if extension == ".jpe":
        extension = ".jpg"

    return content, extension


# ============================================================
# ntfy Notification
# ============================================================

def notify(message, title="Camp Gallery Backup"):
    _, _, response = http_request(
        NTFY_URL,
        method="POST",
        headers={
            "Title": title,
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
    state = load_state()

    uploaded = {
        item["key"]
        for item in state["uploaded"]
    }

    sent = 0
    skipped = 0
    failed = 0
    total_albums = 0
    status = "success"
    error_message = ""

    print("=" * 50)
    print("Camp Gallery Backup")
    print("=" * 50)

    try:
        albums = get_gallery()
        total_albums = len(albums)

        print(f"[Gallery] Total albums: {total_albums}")

        for album in albums:
            album_id = album.get("id")
            album_name = album.get("name", "Unknown")

            try:
                images = parse_images(album.get("images", []))
            except Exception as error:
                failed += 1
                print(f"[Album {album_id}] Invalid images: {error}")
                continue

            for index, image_url in enumerate(images, start=1):
                try:
                    record_key = make_record_key(image_url)

                    if record_key in uploaded:
                        skipped += 1
                        continue

                    photo_url = urllib.request.urljoin(
                        API_URL,
                        image_url
                    )

                    print(
                        f"[Download] Album {album_id}, "
                        f"image {index}"
                    )

                    content, extension = download_image(photo_url)

                    if not content:
                        raise ValueError("Empty image")

                    filename = (
                        f"album-{album_id}-{index}{extension}"
                    )

                    print(
                        f"[Discord] Uploading {filename} "
                        f"({len(content)} bytes)"
                    )

                    discord_result = upload_discord(
                        content,
                        filename,
                        album
                    )

                    record = {
                        "key": record_key,
                        "albumId": album_id,
                        "albumName": album_name,
                        "filename": filename,
                        "discordMessageId": discord_result["id"],
                        "uploadedAt": datetime.now(
                            TIMEZONE
                        ).isoformat()
                    }

                    state["uploaded"].append(record)
                    uploaded.add(record_key)

                    # Save immediately after successful upload.
                    save_state(state)

                    sent += 1

                    print(
                        f"[Success] {filename} "
                        f"-> {discord_result['id']}"
                    )

                    # Avoid sending requests too rapidly.
                    time.sleep(1)

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
        f"📤 Discord uploaded: {sent} images",
        f"⏭️ Already uploaded: {skipped} images",
        f"❌ Failed: {failed} images",
        f"💾 Total records: {len(state['uploaded'])}",
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
