import os
import time
import random
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("API_BASE_URL")

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

def main():
    conn = get_connection()
    records = fetch_resolutions(conn)

    print(f"Found {len(records)} records")

    for r in records:
        anon_id = r["anonId"]
        record_id = r["id"]

        try:
            url = f"{BASE_URL}/{anon_id}"
            res = requests.get(url, timeout=15)

            if res.status_code != 200:
                print(f"Skip {record_id} (status {res.status_code})")
                continue

            data = res.json()

            if data.get("status") == "ok" and data.get("hls"):
                new_url = data["hls"]

                update_url(conn, record_id, new_url)
                print(f"Updated {record_id} -> {new_url}")
            else:
                print(f"No hls for {record_id}")

        except Exception as e:
            print(f"Error {record_id}: {e}")

        # random delay 7–15 seconds
        delay = random.randint(7, 15)
        print(f"Sleeping {delay}s...")
        time.sleep(delay)

    conn.close()

if __name__ == "__main__":
    main()
