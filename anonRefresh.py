import os
import time
import random
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("API_BASE_URL")
BASE_URL2 = os.getenv("API_BASE_URL2")

def get_connection():
    return psycopg2.connect(DATABASE_URL)

def fetch_resolutions(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT id, "anonId"
            FROM "Resolution"
            WHERE "anonId" IS NOT NULL
        """)
        return cur.fetchall()

def update_url(conn, record_id, new_url):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE "Resolution"
            SET url = %s
            WHERE id = %s
        """, (new_url, record_id))
    conn.commit()

def extract_fetch_id(text):
    try:
        return text.split("/load/")[1].split("';")[0]
    except Exception:
        return None

def main():
    print("Entering main()")

    if not DATABASE_URL:
        print("❌ DATABASE_URL is missing")
        return

    if not BASE_URL:
        print("❌ API_BASE_URL is missing")
        return

    if not BASE_URL2:
        print("❌ API_BASE_URL2 is missing")
        return

    print("Connecting to DB...")
    conn = get_connection()

    print("Fetching records...")
    records = fetch_resolutions(conn)

    print(f"Found {len(records)} records")

    for r in records:
        anon_id = r["anonId"]
        record_id = r["id"]

        try:
            # Step 1: Fetch HTML page
            print(f"Init Step 1 of {record_id}")
            url_fetch_anon_id = f"{BASE_URL2}/{anon_id}"

            res2 = requests.get(
                url_fetch_anon_id,
                timeout=15,
                allow_redirects=False
            )
            
            if res2.status_code != 200:
                print(f"Skip {record_id} (status {res2.status_code})")
                continue
            print(f"Step 1 of {record_id} Passed!")
            data2 = res2.text
            print(f"Step 1 of {record_id} Success!")
            print(f"Init Step 2 of {record_id}")
            # Step 2: Extract fetch id
            anon_fetchid = extract_fetch_id(data2)
            
            if not anon_fetchid:
                print(f"Could not extract fetch id for {record_id}")
                continue
            print(f"Step 2 of {record_id} Passed!")
            # Step 3: Fetch API
            url = f"{BASE_URL}/{anon_fetchid}"

            res = requests.get(
                url,
                timeout=15,
                allow_redirects=False
            )

            if res.status_code != 200:
                print(f"Skip {record_id} API (status {res.status_code})")
                continue
            print(f"Step 2 of {record_id} Success!")
            data = res.json()
            print(f"Update HLS record of {record_id}")
            # Step 4: Update DB
            if data.get("status") == "ok" and data.get("hls"):
                new_url = data["hls"]

                update_url(conn, record_id, new_url)

                print(f"Updated {record_id}")
            else:
                print(f"No hls for {record_id}")

        except Exception as e:
            print(f"Error {record_id}: {e}")

        delay = random.randint(7, 15)

        print(f"Sleeping {delay}s...")

        time.sleep(delay)

    conn.close()

if __name__ == "__main__":
    main()
