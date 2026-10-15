import datetime as dt
import sqlite3
from decimal import Decimal, InvalidOperation

from .client import SimpleFINError

TX_DATE = "COALESCE(NULLIF(t.posted, 0), t.transacted_at, t.first_seen)"


def parse_date(value: str | None) -> int | None:
    """Accepts YYYY-MM-DD, an ISO datetime, or a relative 'Nd' (N days ago)."""
    if not value:
        return None
    value = value.strip()
    if value.endswith("d") and value[:-1].isdigit():
        return int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(value[:-1]))).timestamp())
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise SimpleFINError(f"Invalid date {value!r}: use YYYY-MM-DD or e.g. 30d") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp())


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def accounts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT a.id, a.name, a.currency, a.balance, a.available_balance, a.balance_date,
                  c.name AS connection, COALESCE(c.org_name, c.org_url) AS institution
           FROM accounts a LEFT JOIN connections c ON c.conn_id = a.conn_id
           ORDER BY institution, a.name"""
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "institution": r["institution"],
            "connection": r["connection"],
            "currency": r["currency"],
            "balance": r["balance"],
            "available_balance": r["available_balance"],
            "balance_date": iso(r["balance_date"]),
        }
        for r in rows
    ]


def to_cents(value: str) -> int:
    try:
        return int(Decimal(value) * 100)
    except InvalidOperation:
        raise SimpleFINError(f"Invalid amount {value!r}") from None


def resolve_account_ids(conn: sqlite3.Connection, account: str | None) -> list[str] | None:
    """Match an account by exact id or case-insensitive name substring."""
    if not account:
        return None
    rows = conn.execute(
        "SELECT id FROM accounts WHERE id = ? OR name LIKE ?", (account, f"%{account}%")
    ).fetchall()
    return [r["id"] for r in rows]


def transactions(
    conn: sqlite3.Connection,
    *,
    account: str | None = None,
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    min_amount: str | None = None,
    max_amount: str | None = None,
    pending: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    where, params = [], []
    ids = resolve_account_ids(conn, account)
    if ids is not None:
        if not ids:
            return []
        where.append(f"t.account_id IN ({','.join('?' * len(ids))})")
        params += ids
    if (start := parse_date(since)) is not None:
        where.append(f"{TX_DATE} >= ?")
        params.append(start)
    if (end := parse_date(until)) is not None:
        where.append(f"{TX_DATE} < ?")
        params.append(end)
    if search:
        where.append("(t.description LIKE ? OR t.payee LIKE ? OR t.memo LIKE ?)")
        params += [f"%{search}%"] * 3
    if min_amount is not None:
        where.append("t.amount_cents >= ?")
        params.append(to_cents(min_amount))
    if max_amount is not None:
        where.append("t.amount_cents <= ?")
        params.append(to_cents(max_amount))
    if pending is not None:
        where.append("t.pending = ?")
        params.append(int(pending))
    sql = f"""SELECT t.*, a.name AS account_name, a.currency, {TX_DATE} AS date
              FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
              {'WHERE ' + ' AND '.join(where) if where else ''}
              ORDER BY date DESC, t.id DESC LIMIT ?"""
    params.append(max(1, min(int(limit), 5000)))
    return [
        {
            "date": iso(r["date"]),
            "account": r["account_name"],
            "account_id": r["account_id"],
            "id": r["id"],
            "amount": r["amount"],
            "currency": r["currency"],
            "description": r["description"],
            "payee": r["payee"],
            "memo": r["memo"],
            "pending": bool(r["pending"]),
        }
        for r in conn.execute(sql, params).fetchall()
    ]


def balance_history(conn: sqlite3.Connection, account: str, since: str | None = None) -> list[dict]:
    ids = resolve_account_ids(conn, account) or []
    if not ids:
        return []
    start = parse_date(since) or 0
    rows = conn.execute(
        f"""SELECT s.*, a.name FROM balance_snapshots s JOIN accounts a ON a.id = s.account_id
            WHERE s.account_id IN ({','.join('?' * len(ids))}) AND s.balance_date >= ?
            ORDER BY s.balance_date DESC""",
        (*ids, start),
    ).fetchall()
    return [
        {"account": r["name"], "date": iso(r["balance_date"]), "balance": r["balance"], "available_balance": r["available_balance"]}
        for r in rows
    ]


def summary(conn: sqlite3.Connection, *, since: str | None = "30d", until: str | None = None, account: str | None = None) -> dict:
    """Money in/out per account over a period, from posted (non-pending) transactions."""
    where, params = ["t.pending = 0"], []
    ids = resolve_account_ids(conn, account)
    if ids is not None:
        if not ids:
            return {"accounts": []}
        where.append(f"t.account_id IN ({','.join('?' * len(ids))})")
        params += ids
    if (start := parse_date(since)) is not None:
        where.append(f"{TX_DATE} >= ?")
        params.append(start)
    if (end := parse_date(until)) is not None:
        where.append(f"{TX_DATE} < ?")
        params.append(end)
    rows = conn.execute(
        f"""SELECT a.name, a.currency, COUNT(*) AS n,
                   SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents ELSE 0 END) AS money_in,
                   SUM(CASE WHEN t.amount_cents < 0 THEN -t.amount_cents ELSE 0 END) AS money_out
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE {' AND '.join(where)} GROUP BY t.account_id ORDER BY money_out DESC""",
        params,
    ).fetchall()
    fmt = lambda cents: f"{Decimal(cents or 0) / 100:.2f}"
    return {
        "since": iso(start),
        "until": iso(end),
        "accounts": [
            {"account": r["name"], "currency": r["currency"], "transactions": r["n"], "money_in": fmt(r["money_in"]), "money_out": fmt(r["money_out"])}
            for r in rows
        ],
    }
