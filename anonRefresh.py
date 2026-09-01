import os
import sys
import time
import random
import argparse
import requests
import psycopg2
import hashlib
import json

from datetime import datetime, timezone, timedelta
from psycopg2.extras import RealDictCursor
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ============================================================
# ENV
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL")

API_BASE_URL = os.getenv("API_BASE_URL")
API_BASE_URL2 = os.getenv("API_BASE_URL2")

THOST = os.getenv("THOST")
TPATH = os.getenv("TPATH")

NTFY_TTOPIC = os.getenv("NTFY_TTOPIC")

# Task 002
OHOST = os.getenv("ENV_OHOST")
OAPIKEY = os.getenv("ENV_OAPIKEY")
OREFERER = os.getenv("ENV_OREFERER")


# ============================================================
# CONSTANTS
# ============================================================

CACHE_FILE = "fetch_cache.json"

THAI_TZ = timezone(timedelta(hours=7))


# ============================================================
# CACHE
# ============================================================

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {}

        return data

    except (json.JSONDecodeError, OSError):
        print("[WARN] Cannot read cache. Starting with empty cache.")
        return {}


def save_cache(cache):
    temp_file = f"{CACHE_FILE}.tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(
            cache,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temp_file, CACHE_FILE)


# ============================================================
# NTFY
# ============================================================

def send_ntfy(message):
    if not NTFY_TTOPIC:
        print("[WARN] NTFY_TTOPIC is not configured.")
        return

    try:
        response = requests.post(
            NTFY_TTOPIC,
            data=message.encode("utf-8"),
            timeout=20
        )

        if response.status_code >= 400:
            print(
                f"[WARN] ntfy failed: "
                f"{response.status_code} {response.text}"
            )
        else:
            print("[INFO] ntfy sent.")

    except requests.RequestException as e:
        print(f"[WARN] ntfy error: {e}")


# ============================================================
# TASK 0
# Original Resolution refresh
# ============================================================

def get_connection():
    return psycopg2.connect(DATABASE_URL)


def fetch_resolutions():
    conn = get_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                '''
                SELECT id, "anonId"
                FROM "Resolution"
                WHERE "anonId" IS NOT NULL
                '''
            )

            return cursor.fetchall()

    finally:
        conn.close()


def update_url(resolution_id, url):
    conn = get_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                UPDATE "Resolution"
                SET url = %s
                WHERE id = %s
                ''',
                (url, resolution_id)
            )

        conn.commit()

    finally:
        conn.close()


def extract_fetch_id(html):
    try:
        return html.split("/load/")[1].split("';")[0]
    except (IndexError, AttributeError):
        return None


def run_task_0():
    print("=" * 60)
    print("TASK 0 - Refresh Resolution")
    print("=" * 60)

    if not DATABASE_URL:
        print("[ERROR] DATABASE_URL is not configured.")
        return

    if not API_BASE_URL or not API_BASE_URL2:
        print("[ERROR] API_BASE_URL / API_BASE_URL2 is not configured.")
        return

    try:
        resolutions = fetch_resolutions()
    except Exception as e:
        print(f"[ERROR] Cannot fetch resolutions: {e}")
        return

    print(f"[INFO] Found {len(resolutions)} resolutions.")

    session = requests.Session()

    for index, resolution in enumerate(resolutions, start=1):
        resolution_id = resolution["id"]
        anon_id = resolution["anonId"]

        print(
            f"[{index}/{len(resolutions)}] "
            f"Processing {resolution_id}"
        )

        try:
            # ------------------------------------------------
            # Step 1
            # ------------------------------------------------

            url1 = f"{API_BASE_URL2}/{anon_id}"

            response1 = session.get(
                url1,
                timeout=20
            )

            if response1.status_code != 200:
                print(
                    f"[WARN] Step 1 failed: "
                    f"{response1.status_code}"
                )
                continue

            fetch_id = extract_fetch_id(response1.text)

            if not fetch_id:
                print("[WARN] Cannot extract fetch ID.")
                continue

            # ------------------------------------------------
            # Step 2
            # ------------------------------------------------

            url2 = f"{API_BASE_URL}/{fetch_id}"

            response2 = session.get(
                url2,
                timeout=20
            )

            if response2.status_code != 200:
                print(
                    f"[WARN] Step 2 failed: "
                    f"{response2.status_code}"
                )
                continue

            data = response2.json()

            if (
                isinstance(data, dict)
                and data.get("status") == "ok"
                and data.get("hls")
            ):
                update_url(
                    resolution_id,
                    data["hls"]
                )

                print("[OK] URL updated.")

            else:
                print("[WARN] Invalid API response.")

        except requests.RequestException as e:
            print(f"[WARN] Request error: {e}")

        except Exception as e:
            print(f"[WARN] Unexpected error: {e}")

        # Avoid hammering API
        sleep_time = random.uniform(7, 15)
        print(f"[INFO] Sleeping {sleep_time:.2f}s")
        time.sleep(sleep_time)

    print("[INFO] Task 0 completed.")


# ============================================================
# TASK 001
# Original TPATH checking
# ============================================================

def run_task_001():
    print("=" * 60)
    print("TASK 001")
    print("=" * 60)

    if not TPATH:
        print("[ERROR] TPATH is not configured.")
        return

    cache = load_cache()

    task_cache = cache.setdefault(
        "task001",
        {}
    )

    now = datetime.now(THAI_TZ)
    today = now.strftime("%Y-%m-%d")

    last_fetch_date = task_cache.get("last_fetch_date")

    # Already checked today
    if last_fetch_date == today:
        print("[INFO] Task001 already ran today.")
        return

    # Don't run too early
    if now.hour < 7:
        print("[INFO] Task001 skipped because current hour < 07:00.")
        return

    # Force run near midnight
    force_run = now.hour + 3 >= 24

    if not force_run:
        # 50% chance to skip
        if random.choice([0, 1]) == 0:
            print("[INFO] Task001 randomly skipped.")
            return

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }

    try:
        response = requests.get(
            TPATH,
            headers=headers,
            timeout=30
        )

        if response.status_code != 200:
            print(
                f"[ERROR] Task001 HTTP "
                f"{response.status_code}"
            )
            return

        content = response.content

        current_hash = hashlib.sha256(
            content
        ).hexdigest()

        previous_hash = task_cache.get("last_hash")

        print(f"[INFO] Current hash:  {current_hash}")
        print(f"[INFO] Previous hash: {previous_hash}")

        if previous_hash is None:
            print("[INFO] First Task001 run.")
            task_cache["last_hash"] = current_hash
            task_cache["last_fetch_date"] = today

            save_cache(cache)

            send_ntfy(
                "🆕 Task001 initial snapshot saved."
            )

            return

        if current_hash != previous_hash:
            print("[INFO] Task001 content changed.")

            send_ntfy(
                "🔄 Task001 content changed."
            )

            task_cache["last_hash"] = current_hash

            history = task_cache.setdefault(
                "history",
                []
            )

            history.append({
                "date": today,
                "hash": current_hash
            })

        else:
            print("[INFO] Task001 content unchanged.")

        task_cache["last_fetch_date"] = today

        save_cache(cache)

        # ----------------------------------------------------
        # Weekly history pruning
        # ----------------------------------------------------

        if now.weekday() == 6:
            prune_cache()

    except requests.RequestException as e:
        print(f"[ERROR] Task001 request failed: {e}")

    except Exception as e:
        print(f"[ERROR] Task001 failed: {e}")


def prune_cache():
    cache = load_cache()

    task_cache = cache.get("task001")

    if not task_cache:
        return

    history = task_cache.get("history")

    if not history:
        return

    # Keep latest 30 records
    task_cache["history"] = history[-30:]

    save_cache(cache)

    print("[INFO] Task001 history pruned.")


# ============================================================
# TASK 002 - AES + SHA256 + ETag
# ============================================================

def derive_aes_key(api_key):
    """
    Derive a valid AES-256 key from ENV_OAPIKEY.

    AES-256 requires exactly 32 bytes.
    SHA-256 gives exactly 32 bytes.
    """

    if not api_key:
        raise ValueError(
            "ENV_OAPIKEY is not configured."
        )

    return hashlib.sha256(
        api_key.encode("utf-8")
    ).digest()


def encrypt_title(title):
    """
    AES-256-GCM encryption.

    Returns Base64 string containing:
        nonce + ciphertext + authentication tag
    """

    key = derive_aes_key(OAPIKEY)

    aesgcm = AESGCM(key)

    # GCM nonce must be unique.
    nonce = os.urandom(12)

    plaintext = title.encode("utf-8")

    ciphertext = aesgcm.encrypt(
        nonce,
        plaintext,
        None
    )

    payload = nonce + ciphertext

    return __import__("base64").urlsafe_b64encode(
        payload
    ).decode("ascii")


def decrypt_title(encrypted_title):
    """
    Decrypt AES-256-GCM encrypted title.
    """

    key = derive_aes_key(OAPIKEY)

    aesgcm = AESGCM(key)

    payload = __import__("base64").urlsafe_b64decode(
        encrypted_title.encode("ascii")
    )

    nonce = payload[:12]
    ciphertext = payload[12:]

    plaintext = aesgcm.decrypt(
        nonce,
        ciphertext,
        None
    )

    return plaintext.decode("utf-8")


def hash_title(title):
    """
    SHA-256 is ONLY used for comparison.

    The plaintext title is UTF-8 encoded,
    so Thai language works normally.
    """

    return hashlib.sha256(
        title.encode("utf-8")
    ).hexdigest()


# ============================================================
# Task002 helpers
# ============================================================

def extract_collection_id(collection):
    """
    User specified that collection ID is:

        data[].data.id
    """

    if not isinstance(collection, dict):
        return None

    data = collection.get("data")

    if not isinstance(data, dict):
        return None

    return data.get("id")


def extract_collection_title(collection):
    """
    User specified title path:

        data[0].contentInCollection[0].content.title

    Each collection is expected to contain:
        data.contentInCollection[].content.title
    """

    if not isinstance(collection, dict):
        return []

    data = collection.get("data")

    if not isinstance(data, dict):
        return []

    contents = data.get("contentInCollection")

    if not isinstance(contents, list):
        return []

    titles = []

    for item in contents:
        if not isinstance(item, dict):
            continue

        content = item.get("content")

        if not isinstance(content, dict):
            continue

        title = content.get("title")

        if isinstance(title, str) and title:
            titles.append(title)

    return titles


def build_task002_snapshot(response_data):
    """
    Expected API structure:

    {
        "success": true,
        "data": [
            {
                "data": {
                    "id": "collection-id",
                    ...
                },
                "contentInCollection": [
                    {
                        "content": {
                            "id": "content-id",
                            "title": "ชื่อเรื่อง"
                        }
                    }
                ]
            }
        ]
    }
    """

    if not isinstance(response_data, dict):
        return {}

    collections = response_data.get("data", [])

    if not isinstance(collections, list):
        return {}

    snapshot = {}

    for collection in collections:
        if not isinstance(collection, dict):
            continue

        # Collection ID = data[].data.id
        collection_data = collection.get("data")

        if not isinstance(collection_data, dict):
            continue

        collection_id = collection_data.get("id")

        if not collection_id:
            continue

        collection_id = str(collection_id)

        content_items = collection.get("contentInCollection", [])

        if not isinstance(content_items, list):
            continue

        contents = []

        for item in content_items:
            if not isinstance(item, dict):
                continue

            content = item.get("content")

            if not isinstance(content, dict):
                continue

            title = content.get("title")

            if not isinstance(title, str) or not title:
                continue

            contents.append({
                "hash": hash_title(title),
                "encrypted": encrypt_title(title),
            })

        snapshot[collection_id] = contents

    return snapshot


def normalize_cached_entries(entries):
    """
    Supports both:

    New format:
        {
            "hash": "...",
            "encrypted": "..."
        }

    And old/legacy plaintext format:
        "Some title"

    Legacy plaintext will be migrated to AES.
    """

    if not isinstance(entries, list):
        return []

    normalized = []

    for entry in entries:

        # New format
        if isinstance(entry, dict):
            title_hash = entry.get("hash")
            encrypted = entry.get("encrypted")

            if title_hash and encrypted:
                normalized.append({
                    "hash": title_hash,
                    "encrypted": encrypted
                })

        # Legacy plaintext
        elif isinstance(entry, str):
            try:
                normalized.append({
                    "hash": hash_title(entry),
                    "encrypted": encrypt_title(entry)
                })

            except Exception as e:
                print(
                    f"[WARN] Cannot migrate legacy title: {e}"
                )

    return normalized


def normalize_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return {}

    result = {}

    for collection_id, entries in snapshot.items():
        result[str(collection_id)] = (
            normalize_cached_entries(entries)
        )

    return result


def decrypt_cached_title(entry):
    """
    Safely decrypt title.

    If decryption fails, return a readable fallback
    rather than crashing the whole task.
    """

    try:
        encrypted = entry.get("encrypted")

        if not encrypted:
            return "[Unable to decrypt title]"

        return decrypt_title(encrypted)

    except Exception as e:
        print(
            f"[WARN] Cannot decrypt title: {e}"
        )

        return "[Unable to decrypt title]"


# ============================================================
# Task002 diff
# ============================================================

def diff_collections(old_snapshot, new_snapshot):
    """
    Compare collection IDs and title SHA-256 hashes.

    Returns:

        added_collections
        removed_collections
        added_contents
        removed_contents
    """

    old_ids = set(old_snapshot.keys())
    new_ids = set(new_snapshot.keys())

    added_collection_ids = new_ids - old_ids
    removed_collection_ids = old_ids - new_ids

    added_collections = sorted(
        added_collection_ids
    )

    removed_collections = sorted(
        removed_collection_ids
    )

    added_contents = []
    removed_contents = []

    # --------------------------------------------------------
    # Existing collections
    # --------------------------------------------------------

    for collection_id in sorted(
        old_ids & new_ids
    ):
        old_entries = normalize_cached_entries(
            old_snapshot.get(collection_id, [])
        )

        new_entries = normalize_cached_entries(
            new_snapshot.get(collection_id, [])
        )

        old_by_hash = {
            entry["hash"]: entry
            for entry in old_entries
            if entry.get("hash")
        }

        new_by_hash = {
            entry["hash"]: entry
            for entry in new_entries
            if entry.get("hash")
        }

        old_hashes = set(old_by_hash.keys())
        new_hashes = set(new_by_hash.keys())

        # ----------------------------------------------------
        # Added
        # ----------------------------------------------------

        for title_hash in sorted(
            new_hashes - old_hashes
        ):
            added_contents.append({
                "collection_id": collection_id,
                "entry": new_by_hash[title_hash]
            })

        # ----------------------------------------------------
        # Removed
        # ----------------------------------------------------

        for title_hash in sorted(
            old_hashes - new_hashes
        ):
            removed_contents.append({
                "collection_id": collection_id,
                "entry": old_by_hash[title_hash]
            })

    return (
        added_collections,
        removed_collections,
        added_contents,
        removed_contents
    )


# ============================================================
# Task002 notifications
# ============================================================

def notify_task002_changes(
    added_collections,
    removed_collections,
    added_contents,
    removed_contents,
    new_snapshot
):
    """
    Send notifications.

    IMPORTANT:
    Added content notification decrypts the title,
    so user receives the real Thai title instead of SHA256.
    """

    # --------------------------------------------------------
    # Added collections
    # --------------------------------------------------------

    for collection_id in added_collections:
        lines = [
            "📦 Added Collection",
            "",
            f"Collection: {collection_id}"
        ]

        entries = new_snapshot.get(
            collection_id,
            []
        )

        for entry in entries:
            title = decrypt_cached_title(entry)

            lines.append(
                f"Title: {title}"
            )

        send_ntfy(
            "\n".join(lines)
        )

    # --------------------------------------------------------
    # Removed collections
    # --------------------------------------------------------

    for collection_id in removed_collections:
        send_ntfy(
            "\n".join([
                "🗑️ Removed Collection",
                "",
                f"Collection: {collection_id}"
            ])
        )

    # --------------------------------------------------------
    # Added content
    # --------------------------------------------------------

    for item in added_contents:
        collection_id = item["collection_id"]
        entry = item["entry"]

        title = decrypt_cached_title(
            entry
        )

        send_ntfy(
            "\n".join([
                "🆕 Added Content",
                "",
                f"Collection: {collection_id}",
                f"Title: {title}"
            ])
        )

    # --------------------------------------------------------
    # Removed content
    # --------------------------------------------------------

    for item in removed_contents:
        collection_id = item["collection_id"]
        entry = item["entry"]

        title = decrypt_cached_title(
            entry
        )

        send_ntfy(
            "\n".join([
                "❌ Removed Content",
                "",
                f"Collection: {collection_id}",
                f"Title: {title}"
            ])
        )


# ============================================================
# TASK 002
# ============================================================

def run_task_002():
    print("=" * 60)
    print("TASK 002 - ETag + AES Cache + SHA256")
    print("=" * 60)

    if not OHOST:
        print("[ERROR] ENV_OHOST is not configured.")
        return

    if not OAPIKEY:
        print("[ERROR] ENV_OAPIKEY is not configured.")
        return

    cache = load_cache()

    task_cache = cache.setdefault(
        "task002",
        {}
    )

    previous_etag = task_cache.get(
        "etag"
    )

    previous_snapshot = task_cache.get(
        "collections",
        {}
    )

    # --------------------------------------------------------
    # Normalize old cache
    # --------------------------------------------------------

    previous_snapshot = normalize_snapshot(
        previous_snapshot
    )

    headers = {
        "accept": "*/*",
        "accept-language": (
            "en,th;q=0.9,"
            "en-US;q=0.8,"
            "en-GB;q=0.7"
        ),
        "x-api-key": OAPIKEY,
    }

    if OREFERER:
        headers["referer"] = OREFERER

    # --------------------------------------------------------
    # Send previous ETag
    # --------------------------------------------------------

    if previous_etag:
        headers["If-None-Match"] = previous_etag

        print(
            f"[INFO] Sending If-None-Match: "
            f"{previous_etag}"
        )
    else:
        print(
            "[INFO] No previous ETag. "
            "This is probably the first run."
        )

    try:
        response = requests.get(
            OHOST,
            headers=headers,
            timeout=30
        )

    except requests.RequestException as e:
        print(
            f"[ERROR] Task002 request failed: {e}"
        )
        return

    # --------------------------------------------------------
    # 304
    # --------------------------------------------------------

    if response.status_code == 304:
        print(
            "[INFO] API returned 304 Not Modified."
        )

        task_cache["last_fetch_date"] = (
            datetime.now(THAI_TZ).isoformat()
        )

        save_cache(cache)

        return

    # --------------------------------------------------------
    # Non-200
    # --------------------------------------------------------

    if response.status_code != 200:
        print(
            f"[ERROR] Task002 HTTP "
            f"{response.status_code}"
        )

        print(
            response.text[:1000]
        )

        return

    # --------------------------------------------------------
    # Get new ETag
    # --------------------------------------------------------

    new_etag = response.headers.get(
        "ETag"
    )

    if new_etag:
        print(
            f"[INFO] New ETag: {new_etag}"
        )

        task_cache["etag"] = new_etag

    else:
        print(
            "[WARN] API did not return ETag."
        )

    # --------------------------------------------------------
    # Parse JSON
    # --------------------------------------------------------

    try:
        data = response.json()

    except ValueError as e:
        print(
            f"[ERROR] Task002 invalid JSON: {e}"
        )
        return

    # --------------------------------------------------------
    # Build encrypted snapshot
    # --------------------------------------------------------

    try:
        new_snapshot = build_task002_snapshot(
            data
        )

    except Exception as e:
        print(
            f"[ERROR] Cannot build Task002 snapshot: {e}"
        )
        return

    print(
        f"[INFO] Collections received: "
        f"{len(new_snapshot)}"
    )

    # --------------------------------------------------------
    # FIRST RUN
    # --------------------------------------------------------

    is_first_run = (
        "collections" not in task_cache
    )

    if is_first_run:
        print(
            "[INFO] Task002 first run."
        )

        print(
            "[INFO] Saving initial snapshot. "
            "Nothing will be reported as Added."
        )

        task_cache["collections"] = (
            new_snapshot
        )

        task_cache["last_fetch_date"] = (
            datetime.now(THAI_TZ).isoformat()
        )

        save_cache(cache)

        send_ntfy(
            "\n".join([
                "📸 Task002 Initial Snapshot",
                "",
                f"Collections: {len(new_snapshot)}",
                "No content changes reported."
            ])
        )

        return

    # --------------------------------------------------------
    # Compare
    # --------------------------------------------------------

    (
        added_collections,
        removed_collections,
        added_contents,
        removed_contents
    ) = diff_collections(
        previous_snapshot,
        new_snapshot
    )

    print(
        f"[INFO] Added collections: "
        f"{len(added_collections)}"
    )

    print(
        f"[INFO] Removed collections: "
        f"{len(removed_collections)}"
    )

    print(
        f"[INFO] Added contents: "
        f"{len(added_contents)}"
    )

    print(
        f"[INFO] Removed contents: "
        f"{len(removed_contents)}"
    )

    # --------------------------------------------------------
    # Notify
    # --------------------------------------------------------

    if (
        added_collections
        or removed_collections
        or added_contents
        or removed_contents
    ):
        notify_task002_changes(
            added_collections,
            removed_collections,
            added_contents,
            removed_contents,
            new_snapshot
        )

    else:
        print(
            "[INFO] No collection/content changes."
        )

    # --------------------------------------------------------
    # Save new snapshot
    # --------------------------------------------------------

    task_cache["collections"] = (
        new_snapshot
    )

    task_cache["last_fetch_date"] = (
        datetime.now(THAI_TZ).isoformat()
    )

    save_cache(cache)

    print(
        "[INFO] Task002 completed."
    )


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Anonymous refresh tasks"
    )

    group = parser.add_mutually_exclusive_group()

    group.add_argument(
        "--task0",
        action="store_true",
        help="Run Task 0 only"
    )

    group.add_argument(
        "--task001",
        action="store_true",
        help="Run Task 001 only"
    )

    group.add_argument(
        "--task002",
        action="store_true",
        help="Run Task 002 only"
    )

    group.add_argument(
        "--all",
        action="store_true",
        help="Run all tasks"
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # No argument => --all
    # --------------------------------------------------------

    if not any([
        args.task0,
        args.task001,
        args.task002,
        args.all
    ]):
        args.all = True

    # --------------------------------------------------------
    # Execute
    # --------------------------------------------------------

    if args.all:
        run_task_0()
        run_task_001()
        run_task_002()

    elif args.task0:
        run_task_0()

    elif args.task001:
        run_task_001()

    elif args.task002:
        run_task_002()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
