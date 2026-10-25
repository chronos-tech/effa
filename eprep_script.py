#!/usr/bin/env python3
"""
Eprep v2.0 - Standalone Maintenance & Cron Runner
Executes periodic maintenance directly via PostgreSQL connection:
1. Sweep pending PromptPay invoices via Gateway verification API.
2. Verify wallet ledger invariant (user.stars == sum(wallet_tx.starsDelta)).
3. Prune stale rate-limit and idempotency records.

Designed to run in GitHub Actions as: python -u script.py
"""

import os
import sys
import json
import uuid
import datetime
import urllib.request
import urllib.error
import psycopg2
from psycopg2.extras import RealDictCursor

# Remainder math unit (1/100000 star)
REMAINDER_UNIT = 100_000


def log(msg: str, level: str = "INFO"):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] [{level}] {msg}", flush=True)


def send_discord_alert(webhook_url: str, title: str, description: str, color: int = 15158332):
    if not webhook_url:
        return
    payload = {
        "embeds": [
            {
                "title": f"🚨 [Eprep Cron] {title}",
                "description": description,
                "color": color,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        ]
    }
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "EprepCron/2.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        log(f"Failed to send Discord alert: {e}", level="WARN")


def verify_promptpay_gateway(gateway_url: str, api_key: str, reference: str):
    """Call Payment Gateway to verify PromptPay transfer status."""
    url = f"{gateway_url.rstrip('/')}/api/v1/promptpay/verify?reference={reference}"
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
        "User-Agent": "EprepCron/2.0",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data
            return {"status": "UNKNOWN", "message": f"HTTP {resp.status}"}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "NOT_FOUND"}
        return {"status": "ERROR", "message": str(e)}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


def sweep_pending_invoices(conn, gateway_url: str, api_key: str, default_rate_milli: int):
    """Scan and settle pending PromptPay invoices created within the last 24 hours."""
    log("Starting PromptPay invoice sweep...")
    if not api_key:
        log("No EPREP_PAYMENT_GATEWAY_API_KEY configured; skipping gateway sweep.", level="WARN")
        return {"checked": 0, "settled": 0, "expired": 0}

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, "userId", "providerRef", "amountSatang", "rateMilli", status, "expiredAt", "createdAt"
            FROM topup_invoice
            WHERE status = 'PENDING'
              AND provider = 'GATEWAY'
              AND "providerRef" IS NOT NULL
              AND "createdAt" >= NOW() - INTERVAL '24 hours'
            ORDER BY "createdAt" ASC
            LIMIT 50;
            """
        )
        pending = cur.fetchall()

    log(f"Found {len(pending)} pending invoice(s) to verify.")
    settled_count = 0
    expired_count = 0

    for inv in pending:
        inv_id = inv["id"]
        user_id = inv["userId"]
        ref = inv["providerRef"]
        satang = inv["amountSatang"]
        rate_milli = inv["rateMilli"] or default_rate_milli
        expired_at = inv["expiredAt"]

        res = verify_promptpay_gateway(gateway_url, api_key, ref)
        status = res.get("status")

        if status == "SUCCESS":
            paid_satang = int(float(res.get("amountBaht", 0)) * 100) if res.get("amountBaht") else satang
            with conn.cursor() as cur:
                # 1. Update invoice status atomically
                cur.execute(
                    """
                    UPDATE topup_invoice
                    SET status = 'SUCCESS', "satangPaid" = %s, "settledAt" = NOW(),
                        late = ("expiredAt" IS NOT NULL AND "expiredAt" < NOW())
                    WHERE id = %s AND status = 'PENDING';
                    """,
                    (paid_satang, inv_id),
                )
                if cur.rowcount == 1:
                    # 2. Fetch and lock user for wallet credit
                    cur.execute(
                        """
                        SELECT stars, "starRemainder" FROM "user" WHERE id = %s FOR UPDATE;
                        """,
                        (user_id,),
                    )
                    user_row = cur.fetchone()
                    if user_row:
                        current_stars, remainder = user_row
                        remainder = remainder or 0

                        # Top-up exact integer remainder math
                        total = remainder + (paid_satang * rate_milli)
                        stars_granted = total // REMAINDER_UNIT
                        new_remainder = total % REMAINDER_UNIT

                        # 3. Update user stars and remainder
                        cur.execute(
                            """
                            UPDATE "user"
                            SET stars = stars + %s, "starRemainder" = %s
                            WHERE id = %s;
                            """,
                            (stars_granted, new_remainder, user_id),
                        )

                        # 4. Insert ledger transaction
                        tx_id = f"tx_{uuid.uuid4().hex[:16]}"
                        meta = json.dumps({
                            "source": "cron_sweep",
                            "paidSatang": paid_satang,
                            "rateMilli": rate_milli,
                            "providerRef": ref,
                        })
                        cur.execute(
                            """
                            INSERT INTO wallet_tx (id, "userId", "starsDelta", type, source, "refId", meta, "createdAt")
                            VALUES (%s, %s, %s, 'TOPUP', 'GATEWAY', %s, %s::jsonb, NOW());
                            """,
                            (tx_id, user_id, stars_granted, inv_id, meta),
                        )
                        conn.commit()
                        settled_count += 1
                        log(f"Settled invoice {inv_id} for user {user_id}: +{stars_granted} ★ (satang={paid_satang})")
                else:
                    conn.rollback()

        elif status == "EXPIRED" or (expired_at and expired_at < datetime.datetime.now(datetime.timezone.utc)):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE topup_invoice SET status = 'EXPIRED' WHERE id = %s AND status = 'PENDING';
                    """,
                    (inv_id,),
                )
                conn.commit()
                expired_count += 1
                log(f"Marked invoice {inv_id} as EXPIRED.")

    log(f"Sweep complete: {settled_count} settled, {expired_count} expired.")
    return {"checked": len(pending), "settled": settled_count, "expired": expired_count}


def ledger_invariant_check(conn, discord_url: str):
    """Ensure sum of wallet_tx exactly equals user.stars for all users."""
    log("Running Wallet Ledger Invariant check...")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.stars, COALESCE(SUM(tx."starsDelta"), 0) AS expected_stars
            FROM "user" u
            LEFT JOIN wallet_tx tx ON tx."userId" = u.id
            GROUP BY u.id, u.email, u.stars
            HAVING u.stars != COALESCE(SUM(tx."starsDelta"), 0);
            """
        )
        discrepancies = cur.fetchall()

    if not discrepancies:
        log("✅ Ledger invariant check PASSED: All user balances match transaction history.")
        return True

    msg = f"⚠️ INVARIANT VIOLATION: {len(discrepancies)} user(s) have mismatched star balances!\n"
    for d in discrepancies[:5]:
        msg += f"• User {d['email']} ({d['id']}): Actual={d['stars']}, Expected={d['expected_stars']}\n"
    if len(discrepancies) > 5:
        msg += f"... and {len(discrepancies) - 5} more.\n"

    log(msg, level="ERROR")
    send_discord_alert(discord_url, "Ledger Invariant Violation Detected", msg, color=15158332)
    return False


def prune_stale_records(conn):
    """Clean up expired rate limit rows and stale idempotency keys."""
    log("Pruning stale records...")
    with conn.cursor() as cur:
        cur.execute("""DELETE FROM rate_limit WHERE "resetAt" < NOW() - INTERVAL '1 day';""")
        pruned_rl = cur.rowcount

        cur.execute("""DELETE FROM idempotency_key WHERE "createdAt" < NOW() - INTERVAL '2 days';""")
        pruned_idem = cur.rowcount
        conn.commit()

    log(f"Pruned {pruned_rl} rate-limit rows and {pruned_idem} idempotency keys.")


def main():
    db_url = os.environ.get("EPREP_DATABASE_URL")
    if not db_url:
        log("EPREP_DATABASE_URL environment variable is required.", level="ERROR")
        sys.exit(1)

    gateway_url = os.environ.get("EPREP_PAYMENT_GATEWAY_URL")
    gateway_key = os.environ.get("EPREP_PAYMENT_GATEWAY_API_KEY", "")
    discord_url = os.environ.get("EPREP_DISCORD_WEBHOOK_URL", "")
    rate_topup = float(os.environ.get("EPREP_RATE_TOPUP", "1"))
    default_rate_milli = round(rate_topup * 1000)
  
    if not gateway_url:
      log("GATEWAY_URL environment variable is required.", level="ERROR")
      sys.exit(1)
      
    log("Connecting to PostgreSQL database...")
    try:
        conn = psycopg2.connect(db_url)
    except Exception as e:
        log(f"Failed to connect to database: {e}", level="ERROR")
        send_discord_alert(discord_url, "Cron Database Connection Error", str(e), color=15158332)
        sys.exit(1)

    try:
        sweep_pending_invoices(conn, gateway_url, gateway_key, default_rate_milli)
        ledger_invariant_check(conn, discord_url)
        prune_stale_records(conn)
        log("All periodic maintenance tasks finished successfully.")
    except Exception as e:
        log(f"Error during cron execution: {e}", level="ERROR")
        send_discord_alert(discord_url, "Cron Execution Failure", str(e), color=15158332)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
