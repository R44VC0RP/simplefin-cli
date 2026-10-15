import json
import os
import sqlite3
import sys
import threading
import time

from . import client, db

DAY = 86400
OVERLAP_DAYS = 5
WINDOW_DAYS = 45  # Bridge allows 90 but warns above 45 and says it may cap there.
DAILY_BUDGET = 20  # Bridge expects <=24 requests/day and disables tokens that keep exceeding it.
DEFAULT_AUTO_SYNC_HOURS = 12
# Minimum gap between automatic attempts across every process sharing the database, so failures
# (or several MCP servers running at once) can't burn the quota: at most 8 auto attempts a day.
AUTO_SYNC_RETRY_SECONDS = 3 * 3600


class QuotaExceeded(client.SimpleFINError):
    pass


def last_successful_sync(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sync_runs WHERE ok=1 ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()


def last_complete_sync(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sync_runs WHERE ok=1 AND complete=1 ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()


def auto_sync_interval() -> int:
    """Seconds after which data counts as stale; 0 disables auto-sync. Env: SIMPLEFIN_AUTO_SYNC_HOURS."""
    raw = os.environ.get("SIMPLEFIN_AUTO_SYNC_HOURS", "")
    try:
        hours = float(raw) if raw.strip() else DEFAULT_AUTO_SYNC_HOURS
    except ValueError:
        hours = DEFAULT_AUTO_SYNC_HOURS
    return max(0, int(hours * 3600))


def claim_sync_lease(conn: sqlite3.Connection, min_gap: int) -> bool:
    """Atomically claim the right to sync if nobody claimed it in the last `min_gap` seconds."""
    now = int(time.time())
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('sync_lease', 0)")
    claimed = conn.execute(
        "UPDATE meta SET value=? WHERE key='sync_lease' AND value <= ?", (now, now - min_gap)
    ).rowcount == 1
    conn.commit()
    return claimed


def is_stale(conn: sqlite3.Connection, interval: int | None = None) -> bool:
    interval = auto_sync_interval() if interval is None else interval
    if interval <= 0:
        return False
    last = last_successful_sync(conn)
    return last is None or time.time() - last["finished_at"] >= interval


def maybe_sync(conn: sqlite3.Connection) -> dict | None:
    """Sync if auto-sync is on, credentials exist, data is stale, and no recent attempt anywhere.

    Returns the sync result, {"error": ...} on failure, or None when nothing was attempted.
    """
    if auto_sync_interval() <= 0 or not is_stale(conn):
        return None
    access_url = client.load_access_url()
    if not access_url or not claim_sync_lease(conn, AUTO_SYNC_RETRY_SECONDS):
        return None
    try:
        return sync(conn, access_url, _lease_held=True)
    except (client.AccessRevoked, client.PaymentRequired) as exc:
        # Won't fix itself: back off a day instead of retrying every few hours. `claim` resets this.
        next_try = int(time.time()) + DAY
        conn.execute("UPDATE meta SET value=? WHERE key='sync_lease'", (next_try - AUTO_SYNC_RETRY_SECONDS,))
        conn.commit()
        return {"error": client.sanitize(exc)}
    except client.SimpleFINError as exc:
        return {"error": client.sanitize(exc)}


def recent_attempt(conn: sqlite3.Connection, within: int) -> bool:
    """True if any process started (or is running) a sync in the last `within` seconds."""
    row = conn.execute("SELECT value FROM meta WHERE key='sync_lease'").fetchone()
    return bool(row and row[0] and 0 <= time.time() - row[0] < within)


def reset_lease(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sync_lease', 0)")
    conn.commit()


def start_background_sync(check_every: int = 600) -> threading.Thread | None:
    """Keep data fresh while a long-running process (the MCP server) is alive."""
    if auto_sync_interval() <= 0:
        return None

    def loop() -> None:
        while True:
            try:
                with db.session() as conn:
                    result = maybe_sync(conn)
                if result and "error" in result:
                    print(f"simplefin auto-sync failed: {result['error']}", file=sys.stderr)
            except Exception as exc:  # never let the thread die; retry on the next tick
                print(f"simplefin auto-sync error: {client.sanitize(exc)}", file=sys.stderr)
            time.sleep(check_every)

    thread = threading.Thread(target=loop, name="simplefin-auto-sync", daemon=True)
    thread.start()
    return thread


def plan_windows(conn: sqlite3.Connection, backfill_days: int | None) -> list[tuple[int, int]]:
    now = int(time.time())
    end = now + DAY
    if backfill_days is not None:
        if backfill_days < 1:
            raise client.SimpleFINError("--days must be at least 1")
        start = now - backfill_days * DAY
    elif (last := last_complete_sync(conn)) is not None:
        # Resume from the last sync with no account/connection errors, so gaps get re-fetched,
        # but never reach back more than two windows on a routine sync.
        start = max(last["finished_at"] - OVERLAP_DAYS * DAY, end - 2 * WINDOW_DAYS * DAY)
    else:
        start = end - WINDOW_DAYS * DAY
    windows = []
    cursor = end
    while cursor > start:
        window_start = max(start, cursor - WINDOW_DAYS * DAY)
        windows.append((window_start, cursor))
        cursor = window_start
    return windows


def sync(
    conn: sqlite3.Connection,
    access_url: str,
    *,
    backfill_days: int | None = None,
    force: bool = False,
    _lease_held: bool = False,
) -> dict:
    windows = plan_windows(conn, backfill_days)
    used = db.requests_last_24h(conn)
    if not force and used + len(windows) > DAILY_BUDGET:
        raise QuotaExceeded(
            f"{used} SimpleFIN requests in the last 24h; this sync needs {len(windows)} more and the "
            f"local budget is {DAILY_BUDGET}/day (the Bridge disables tokens that keep exceeding ~24/day). "
            "Try later, or pass --force."
        )

    if not _lease_held:
        # Manual syncs always run, but they push back automatic ones in other processes.
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sync_lease', ?)", (int(time.time()),))
    started = int(time.time())
    run_id = conn.execute(
        "INSERT INTO sync_runs (started_at, window_start, window_end) VALUES (?, ?, ?)",
        (started, windows[-1][0], windows[0][1]),
    ).lastrowid
    conn.commit()

    totals = {"new": 0, "updated": 0}
    errors: list[str] = []
    complete = True
    accounts: set[str] = set()
    try:
        for window_start, window_end in windows:
            request_id = db.begin_request(conn)
            try:
                data = client.fetch_accounts(access_url, start_date=window_start, end_date=window_end)
            except BaseException as exc:
                db.finish_request(conn, request_id, False, type(exc).__name__)
                raise
            db.finish_request(conn, request_id, True)
            complete = complete and not db.has_data_errors(data)
            counts = db.store_account_set(conn, data, (window_start, window_end))
            totals["new"] += counts["new"]
            totals["updated"] += counts["updated"]
            accounts.update(a["id"] for a in data.get("accounts") or [])
            for message in db.collect_errors(data):
                if message not in errors:
                    errors.append(message)
    except Exception as exc:
        conn.execute(
            "UPDATE sync_runs SET finished_at=?, ok=0, errors=? WHERE id=?",
            (int(time.time()), json.dumps(errors + [client.sanitize(exc)]), run_id),
        )
        conn.commit()
        raise

    finished = int(time.time())
    conn.execute(
        """UPDATE sync_runs SET finished_at=?, ok=1, complete=?, accounts=?, transactions_new=?,
             transactions_updated=?, errors=? WHERE id=?""",
        (finished, int(complete), len(accounts), totals["new"], totals["updated"], json.dumps(errors), run_id),
    )
    conn.commit()
    return {
        "requests": len(windows),
        "window_start": windows[-1][0],
        "window_end": windows[0][1],
        "accounts": len(accounts),
        "transactions_new": totals["new"],
        "transactions_updated": totals["updated"],
        "complete": complete,
        "errors": errors,
    }


def status(conn: sqlite3.Connection) -> dict:
    last = last_successful_sync(conn)
    last_attempt = conn.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM accounts), (SELECT COUNT(*) FROM transactions)"
    ).fetchone()
    return {
        "access_configured": client.load_access_url() is not None,
        "database": str(db.default_db_path()),
        "accounts": counts[0],
        "transactions": counts[1],
        "last_successful_sync": last["finished_at"] if last else None,
        "last_attempt": dict(last_attempt) if last_attempt else None,
        "requests_last_24h": db.requests_last_24h(conn),
        "auto_sync_hours": auto_sync_interval() / 3600,
        "daily_budget": DAILY_BUDGET,
        "bridge_errors": json.loads(last["errors"]) if last and last["errors"] else [],
    }
