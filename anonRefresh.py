import os
import time
import random
import requests
import psycopg2
import hashlib
import json
from datetime import datetime, timedelta, timezone
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("API_BASE_URL")
BASE_URL2 = os.getenv("API_BASE_URL2")

# New Task Envs
THOST = os.getenv("THOST")
TPATH = os.getenv("TPATH")
NTFY_TTOPIC = os.getenv("NTFY_TTOPIC")

CACHE_FILE = "fetch_cache.json"

def get_connection():
    try:
        return psycopg2.connect(DATABASE_URL)
    except psycopg2.OperationalError:
        print("❌ Database connection failed. (Host details hidden for security)")
        return None
    except Exception as e:
        print(f"❌ Database connection failed with an unexpected error: {type(e).__name__}")
        return None

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

# ==========================================
# New Task: Cache and Pruning Logic
# ==========================================

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"last_fetch_date": "", "last_hash": "", "history": {}}

def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=4)

def prune_cache(history):
    """
    เก็บเฉพาะวันอาทิตย์, วันก่อนเกิดการเปลี่ยนแปลง, และวันที่เกิดการเปลี่ยนแปลง
    """
    sorted_dates = sorted(history.keys())
    if not sorted_dates: 
        return {}

    keep_dates = set()
    for i, date_str in enumerate(sorted_dates):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        
        # 1. ยึดวันอาทิตย์เป็นหลัก (weekday() == 6)
        if dt.weekday() == 6:
            keep_dates.add(date_str)

        # 2. เช็คว่าเปลี่ยนกลางคันหรือไม่
        if i < len(sorted_dates) - 1:
            next_date = sorted_dates[i+1]
            if history[date_str] != history[next_date]:
                keep_dates.add(date_str)  # วันก่อนเปลี่ยน
                keep_dates.add(next_date) # วันหลังเปลี่ยน (วันที่ Hash เริ่มต่าง)
                
        if i > 0:
            prev_date = sorted_dates[i-1]
            if history[date_str] != history[prev_date]:
                keep_dates.add(date_str)
                keep_dates.add(prev_date)

    return {d: history[d] for d in sorted_dates if d in keep_dates}

def run_new_task():
    print("--- Checking New Fetch Task ---")
    if not THOST or not TPATH:
        print("⏭️ THOST or TPATH missing. Skipping new task.")
        return

    tz_th = timezone(timedelta(hours=7))
    now = datetime.now(tz_th)
    today_str = now.strftime("%Y-%m-%d")

    cache = load_cache()

    if cache.get("last_fetch_date") == today_str:
        print("✅ Already fetched today.")
        return

    force_run = (now.hour + 5) >= 24

    if force_run:
        print(f"⚡ Next cron is tomorrow (Current hour: {now.hour}:00 TH). Forcing fetch!")
    else:
        if now.hour < 7:
            print(f"💤 Sleeping hour ({now.hour}:00 TH). Skipping.")
            return

        if random.choice([0, 1]) == 0:
            print("🎲 Randomly decided NOT to fetch this time.")
            return

    print("🚀 Proceeding with fetch task...")
    
    headers = {
        'sec-ch-ua-platform': '"Windows"',
        'Referer': THOST,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
        'sec-ch-ua': '"Not;A=Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
        'sec-ch-ua-mobile': '?0',
    }

    try:
        time.sleep(random.uniform(2.0, 5.0))
        
        response = requests.get(TPATH, headers=headers, timeout=15)
        response.raise_for_status()

        source_code = response.text
        current_hash = hashlib.sha256(source_code.encode('utf-8')).hexdigest()

        last_hash = cache.get("last_hash")
        
        if last_hash and current_hash != last_hash:
            print("🔔 Hash changed! Sending ntfy alert...")
            if NTFY_TTOPIC:
                requests.post(NTFY_TTOPIC, data=f"Hash changed from {last_hash[:8]} to {current_hash[:8]}".encode('utf-8'))

        cache["last_hash"] = current_hash
        cache["last_fetch_date"] = today_str
        
        if "history" not in cache:
            cache["history"] = {}
        cache["history"][today_str] = current_hash

        if now.weekday() == 6:
            cache["history"] = prune_cache(cache["history"])

        save_cache(cache)
        print("✅ New fetch task completed and cached.")

    except Exception as e:
        print(f"❌ Error in new task: {e}")

# ==========================================
# Main Execution
# ==========================================

def main():
    print("Entering main()")

    # 1. รันระบบ DB เดิม
    if not DATABASE_URL or not BASE_URL or not BASE_URL2:
        print("❌ Missing DB/API env vars")
    else:
        print("Connecting to DB...")
        conn = get_connection()
        
        if conn:
            print("Fetching records...")
            records = fetch_resolutions(conn)
            print(f"Found {len(records)} records")

            for r in records:
                anon_id = r["anonId"]
                record_id = r["id"]

                try:
                    print(f"Init Step 1 of {record_id}")
                    url_fetch_anon_id = f"{BASE_URL2}/{anon_id}"

                    res2 = requests.get(url_fetch_anon_id, timeout=15, allow_redirects=False)
                    if res2.status_code != 200:
                        print(f"Skip {record_id} (status {res2.status_code})")
                        continue
                    
                    print(f"Step 1 of {record_id} Passed!")
                    data2 = res2.text
                    
                    print(f"Init Step 2 of {record_id}")
                    anon_fetchid = extract_fetch_id(data2)
                    
                    if not anon_fetchid:
                        print(f"Could not extract fetch id for {record_id}")
                        continue
                        
                    print(f"Step 2 of {record_id} Passed!")
                    
                    url = f"{BASE_URL}/{anon_fetchid}"
                    res = requests.get(url, timeout=15, allow_redirects=False)

                    if res.status_code != 200:
                        print(f"Skip {record_id} API (status {res.status_code})")
                        continue
                        
                    data = res.json()
                    
                    print(f"Update HLS record of {record_id}")
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

    run_new_task()

if __name__ == "__main__":
    main()
