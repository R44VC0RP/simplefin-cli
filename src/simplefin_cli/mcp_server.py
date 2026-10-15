import time

from mcp.server.fastmcp import FastMCP

from . import client, db, queries, recurring
from . import sync as sync_mod

MIN_SYNC_INTERVAL = 3600

mcp = FastMCP(
    "simplefin",
    instructions=(
        "Read-only access to the user's bank, card and loan accounts, synced from SimpleFIN Bridge "
        "into a local database. Query tools read the local copy and are free to call. sync_now hits "
        "the Bridge, which allows only ~24 requests per day and updates about once a day, so call it "
        "only when the data is stale (see sync_status). "
        + (
            f"The server also syncs by itself in the background when data is older than "
            f"{sync_mod.auto_sync_interval() / 3600:g} hours. "
            if sync_mod.auto_sync_interval()
            else ""
        )
        + "Amounts are strings; negative means money left "
        "the account. Dates accept YYYY-MM-DD or relative values like 30d."
    ),
)


@mcp.tool()
def list_accounts() -> list[dict]:
    """List every synced account with institution, current balance and balance date."""
    with db.session() as conn:
        return queries.accounts(conn)


@mcp.tool()
def list_transactions(
    account: str | None = None,
    since: str | None = "30d",
    until: str | None = None,
    search: str | None = None,
    min_amount: str | None = None,
    max_amount: str | None = None,
    pending: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    """List transactions, newest first.

    account: account id or part of its name. since/until: YYYY-MM-DD or e.g. "90d".
    search: matches description, payee or memo. min_amount/max_amount: signed amounts,
    e.g. max_amount="-100" for charges of $100 or more. pending: only pending (true) or posted (false).
    """
    with db.session() as conn:
        return queries.transactions(
            conn, account=account, since=since, until=until, search=search,
            min_amount=min_amount, max_amount=max_amount, pending=pending, limit=limit,
        )


@mcp.tool()
def get_balance_history(account: str, since: str | None = None) -> list[dict]:
    """Balance snapshots recorded at each sync for an account (id or part of its name)."""
    with db.session() as conn:
        return queries.balance_history(conn, account, since)


@mcp.tool()
def summarize_cash_flow(since: str | None = "30d", until: str | None = None, account: str | None = None) -> dict:
    """Total money in and out per account over a period, from posted transactions."""
    with db.session() as conn:
        return queries.summary(conn, since=since, until=until, account=account)


@mcp.tool()
def find_recurring_charges(
    since: str | None = None,
    include_inactive: bool = False,
    include_income: bool = False,
    min_occurrences: int = 2,
) -> dict:
    """Detect subscriptions, bills and other charges that repeat on a regular cadence.

    Returns each series with merchant, kind (subscription, bill, installment plan fee), cadence,
    typical amount, monthly and yearly cost, last and next expected charge, confidence and status,
    plus the total monthly cost of active outflows. Card payments and transfers are excluded.
    include_inactive adds series that seem to have stopped; include_income adds regular deposits.
    Limited by synced history: yearly charges need over a year of data.
    """
    with db.session() as conn:
        return recurring.find_recurring(
            conn, since=since, min_occurrences=min_occurrences,
            include_inactive=include_inactive, include_income=include_income,
        )


@mcp.tool()
def top_merchants(
    since: str | None = "30d", until: str | None = None, limit: int = 20, include_transfers: bool = False
) -> dict:
    """Spending grouped by merchant (names normalized across card networks and PayPal/Apple Pay
    prefixes), largest first. Excludes pending charges, and transfers/card payments by default."""
    with db.session() as conn:
        return recurring.top_merchants(
            conn, since=since, until=until, limit=limit, include_transfers=include_transfers
        )


@mcp.tool()
def sync_status() -> dict:
    """When data was last synced, request quota used, and any errors the Bridge reported."""
    with db.session() as conn:
        result = sync_mod.status(conn)
    result["last_successful_sync"] = queries.iso(result["last_successful_sync"])
    return result


@mcp.tool()
def sync_now() -> dict:
    """Fetch new data from SimpleFIN Bridge. Refuses if the last sync was under an hour ago."""
    access_url = client.load_access_url()
    if not access_url:
        return {"error": "Not connected. The user must run `simplefin claim` in a terminal."}
    with db.session() as conn:
        if sync_mod.recent_attempt(conn, 600):
            return {"skipped": True, "reason": "Another sync started in the last 10 minutes; check sync_status shortly."}
        last = sync_mod.last_successful_sync(conn)
        if last and time.time() - last["finished_at"] < MIN_SYNC_INTERVAL:
            return {
                "skipped": True,
                "reason": "Last sync was under an hour ago; the Bridge only refreshes about daily.",
                "last_successful_sync": queries.iso(last["finished_at"]),
            }
        try:
            result = sync_mod.sync(conn, access_url)
        except client.SimpleFINError as exc:
            return {"error": client.sanitize(exc)}
    result["window_start"] = queries.iso(result["window_start"])
    result["window_end"] = queries.iso(result["window_end"])
    return result


def run() -> None:
    sync_mod.start_background_sync()
    mcp.run()
