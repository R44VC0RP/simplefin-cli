import json
import sqlite3
import time

from . import client, db

DAY = 86400
OVERLAP_DAYS = 5
WINDOW_DAYS = 45  # Bridge allows 90 but warns above 45 and says it may cap there.
DAILY_BUDGET = 20  # Bridge expects <=24 requests/day and disables tokens that keep exceeding it.


class QuotaExceeded(client.SimpleFINError):
    pass


def last_successful_sync(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sync_runs WHERE ok=1 ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()


def plan_windows(conn: sqlite3.Connection, backfill_days: int | None) -> list[tuple[int, int]]:
    now = int(time.time())
    end = now + DAY
    last = last_successful_sync(conn)
    if backfill_days is not None:
        start = now - backfill_days * DAY
    elif last is not None:
        start = last["finished_at"] - OVERLAP_DAYS * DAY
    else:
        start = now - WINDOW_DAYS * DAY
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
) -> dict:
    windows = plan_windows(conn, backfill_days)
    used = db.requests_last_24h(conn)
    if not force and used + len(windows) > DAILY_BUDGET:
        raise QuotaExceeded(
            f"{used} SimpleFIN requests in the last 24h; this sync needs {len(windows)} more and the "
            f"local budget is {DAILY_BUDGET}/day (the Bridge disables tokens that keep exceeding ~24/day). "
            "Try later, or pass --force."
        )

    started = int(time.time())
    run_id = conn.execute(
        "INSERT INTO sync_runs (started_at, window_start, window_end) VALUES (?, ?, ?)",
        (started, windows[-1][0], windows[0][1]),
    ).lastrowid
    conn.commit()

    totals = {"new": 0, "updated": 0}
    errors: list[str] = []
    accounts: set[str] = set()
    try:
        for window_start, window_end in windows:
            try:
                data = client.fetch_accounts(access_url, start_date=window_start, end_date=window_end)
            except client.SimpleFINError as exc:
                db.log_request(conn, False, type(exc).__name__)
                raise
            db.log_request(conn, True)
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
        """UPDATE sync_runs SET finished_at=?, ok=1, accounts=?, transactions_new=?,
             transactions_updated=?, errors=? WHERE id=?""",
        (finished, len(accounts), totals["new"], totals["updated"], json.dumps(errors), run_id),
    )
    conn.commit()
    return {
        "requests": len(windows),
        "window_start": windows[-1][0],
        "window_end": windows[0][1],
        "accounts": len(accounts),
        "transactions_new": totals["new"],
        "transactions_updated": totals["updated"],
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
        "daily_budget": DAILY_BUDGET,
        "bridge_errors": json.loads(last["errors"]) if last and last["errors"] else [],
    }
