import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .client import sanitize

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    conn_id TEXT PRIMARY KEY,
    name TEXT,
    org_id TEXT,
    org_name TEXT,
    org_url TEXT,
    sfin_url TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    conn_id TEXT,
    name TEXT NOT NULL,
    currency TEXT,
    balance TEXT,
    available_balance TEXT,
    balance_date INTEGER,
    extra TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id TEXT NOT NULL,
    balance_date INTEGER NOT NULL,
    balance TEXT NOT NULL,
    available_balance TEXT,
    PRIMARY KEY (account_id, balance_date)
);
CREATE TABLE IF NOT EXISTS transactions (
    account_id TEXT NOT NULL,
    id TEXT NOT NULL,
    posted INTEGER NOT NULL,
    transacted_at INTEGER,
    amount TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    description TEXT NOT NULL,
    payee TEXT,
    memo TEXT,
    pending INTEGER NOT NULL DEFAULT 0,
    extra TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS transactions_by_date ON transactions (COALESCE(NULLIF(posted, 0), transacted_at));
CREATE TABLE IF NOT EXISTS requests (
    at INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    window_start INTEGER,
    window_end INTEGER,
    ok INTEGER NOT NULL DEFAULT 0,
    accounts INTEGER,
    transactions_new INTEGER,
    transactions_updated INTEGER,
    errors TEXT
);
"""


def default_db_path() -> Path:
    override = os.environ.get("SIMPLEFIN_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "simplefin-cli" / "simplefin.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    conn = sqlite3.connect(path)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def to_cents(amount: str) -> int:
    try:
        return int((Decimal(str(amount)) * 100).to_integral_value())
    except InvalidOperation:
        return 0


def log_request(conn: sqlite3.Connection, ok: bool, note: str = "") -> None:
    conn.execute("INSERT INTO requests (at, ok, note) VALUES (?, ?, ?)", (int(time.time()), int(ok), note))
    conn.commit()


def requests_last_24h(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM requests WHERE at > ?", (int(time.time()) - 86400,)).fetchone()
    return row[0]


def store_account_set(conn: sqlite3.Connection, data: dict, window: tuple[int, int] | None) -> dict:
    """Upsert one /accounts response. Returns counts of new and updated transactions."""
    now = int(time.time())
    new = updated = 0

    for c in data.get("connections") or []:
        conn.execute(
            """INSERT INTO connections (conn_id, name, org_id, org_name, org_url, sfin_url, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(conn_id) DO UPDATE SET name=excluded.name, org_id=excluded.org_id,
                 org_name=excluded.org_name, org_url=excluded.org_url, sfin_url=excluded.sfin_url,
                 updated_at=excluded.updated_at""",
            (c.get("conn_id"), c.get("name"), c.get("org_id"), c.get("org_name"), c.get("org_url"), c.get("sfin_url"), now),
        )

    for a in data.get("accounts") or []:
        account_id = a["id"]
        conn_id = a.get("conn_id") or (a.get("org") or {}).get("id")
        conn.execute(
            """INSERT INTO accounts (id, conn_id, name, currency, balance, available_balance, balance_date, extra, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET conn_id=excluded.conn_id, name=excluded.name,
                 currency=excluded.currency, balance=excluded.balance,
                 available_balance=excluded.available_balance, balance_date=excluded.balance_date,
                 extra=excluded.extra, updated_at=excluded.updated_at
               WHERE excluded.balance_date IS NULL OR accounts.balance_date IS NULL
                 OR excluded.balance_date >= accounts.balance_date""",
            (
                account_id, conn_id, a.get("name") or account_id, a.get("currency"), a.get("balance"),
                a.get("available-balance"), a.get("balance-date"),
                json.dumps(a["extra"]) if a.get("extra") else None, now,
            ),
        )
        if a.get("balance") is not None and a.get("balance-date"):
            conn.execute(
                """INSERT OR REPLACE INTO balance_snapshots (account_id, balance_date, balance, available_balance)
                   VALUES (?, ?, ?, ?)""",
                (account_id, a["balance-date"], a["balance"], a.get("available-balance")),
            )

        transactions = a.get("transactions")
        if transactions is None:
            continue
        seen_ids = []
        for t in transactions:
            seen_ids.append(t["id"])
            existing = conn.execute(
                "SELECT 1 FROM transactions WHERE account_id=? AND id=?", (account_id, t["id"])
            ).fetchone()
            values = (
                int(t.get("posted") or 0), t.get("transacted_at"), str(t.get("amount", "0")),
                to_cents(t.get("amount", "0")), t.get("description") or "", t.get("payee"), t.get("memo"),
                int(bool(t.get("pending"))), json.dumps(t["extra"]) if t.get("extra") else None,
            )
            if existing:
                conn.execute(
                    """UPDATE transactions SET posted=?, transacted_at=?, amount=?, amount_cents=?,
                         description=?, payee=?, memo=?, pending=?, extra=?, last_seen=?
                       WHERE account_id=? AND id=?""",
                    (*values, now, account_id, t["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO transactions (posted, transacted_at, amount, amount_cents, description,
                         payee, memo, pending, extra, account_id, id, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*values, account_id, t["id"], now, now),
                )
                new += 1

        # A pending transaction usually reappears under a new id once it posts, so pending rows
        # in the fetched window that the server no longer returns are stale.
        if window is not None:
            placeholders = ",".join("?" * len(seen_ids)) or "NULL"
            conn.execute(
                f"""DELETE FROM transactions WHERE account_id=? AND pending=1
                    AND COALESCE(NULLIF(posted, 0), transacted_at, first_seen) >= ?
                    AND id NOT IN ({placeholders})""",
                (account_id, window[0], *seen_ids),
            )

    conn.commit()
    return {"new": new, "updated": updated}


def collect_errors(data: dict) -> list[str]:
    errors = [sanitize(e.get("msg") or e.get("code") or e) for e in data.get("errlist") or []]
    errors += [sanitize(e) for e in data.get("errors") or [] if sanitize(e) not in errors]
    return errors
