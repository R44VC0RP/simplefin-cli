import argparse
import getpass
import json
import sys

from . import client, db, queries
from .client import sanitize
from . import sync as sync_mod


def emit(data, as_json: bool, render) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        render(data)


def table(rows: list[dict], columns: list[tuple[str, str]]) -> None:
    if not rows:
        print("(none)")
        return
    cells = [[sanitize(r.get(key) if r.get(key) is not None else "") for key, _ in columns] for r in rows]
    widths = [max(len(title), *(len(c[i]) for c in cells)) for i, (_, title) in enumerate(columns)]
    widths = [min(w, 48) for w in widths]
    print("  ".join(title.ljust(widths[i]) for i, (_, title) in enumerate(columns)))
    for c in cells:
        print("  ".join(v[: widths[i]].ljust(widths[i]) for i, v in enumerate(c)))


def require_access_url() -> str:
    access_url = client.load_access_url()
    if not access_url:
        raise client.SimpleFINError("Not connected. Run `simplefin claim` first.")
    return access_url


def cmd_claim(args) -> int:
    if args.stdin:
        token = sys.stdin.read()
    else:
        token = getpass.getpass("SimpleFIN setup token (input hidden): ")
    if client.load_access_url() and not args.replace:
        raise client.SimpleFINError("An access URL is already stored. Pass --replace to overwrite it.")
    access_url = client.claim_access_url(token)
    client.save_access_url(access_url)
    print("Claimed. Access URL saved to the macOS Keychain (service: simplefin-cli).")
    print("Next: `simplefin sync` to pull accounts and transactions.")
    return 0


def cmd_sync(args) -> int:
    with db.session() as conn:
        result = sync_mod.sync(conn, require_access_url(), backfill_days=args.days, force=args.force)

    def render(r):
        print(
            f"Synced {r['accounts']} accounts: {r['transactions_new']} new, "
            f"{r['transactions_updated']} updated transactions "
            f"({queries.iso(r['window_start'])[:10]} to {queries.iso(r['window_end'])[:10]}, "
            f"{r['requests']} request{'s' if r['requests'] != 1 else ''})."
        )
        for message in r["errors"]:
            print(f"Bridge: {message}", file=sys.stderr)
        if not r["complete"]:
            print("Some accounts reported errors; the next sync will re-fetch this period.", file=sys.stderr)

    emit(result, args.json, render)
    return 0


def cmd_accounts(args) -> int:
    with db.session() as conn:
        rows = queries.accounts(conn)
    emit(rows, args.json, lambda r: table(r, [
        ("institution", "Institution"), ("name", "Account"), ("balance", "Balance"),
        ("available_balance", "Available"), ("currency", "Cur"), ("balance_date", "As of"),
    ]))
    return 0


def cmd_transactions(args) -> int:
    with db.session() as conn:
        rows = queries.transactions(
            conn, account=args.account, since=args.since, until=args.until, search=args.search,
            min_amount=args.min, max_amount=args.max, pending=True if args.pending else None, limit=args.limit,
        )
    if not args.json:
        for r in rows:
            r["date"] = (r["date"] or "")[:10]
            r["flag"] = "pending" if r["pending"] else ""
    emit(rows, args.json, lambda r: table(r, [
        ("date", "Date"), ("account", "Account"), ("amount", "Amount"), ("description", "Description"), ("flag", ""),
    ]))
    return 0


def cmd_balances(args) -> int:
    with db.session() as conn:
        rows = queries.balance_history(conn, args.account, args.since)
    emit(rows, args.json, lambda r: table(r, [
        ("date", "Date"), ("account", "Account"), ("balance", "Balance"), ("available_balance", "Available"),
    ]))
    return 0


def cmd_summary(args) -> int:
    with db.session() as conn:
        result = queries.summary(conn, since=args.since, until=args.until, account=args.account)
    emit(result, args.json, lambda r: table(r["accounts"], [
        ("account", "Account"), ("transactions", "Count"), ("money_in", "In"), ("money_out", "Out"), ("currency", "Cur"),
    ]))
    return 0


def cmd_status(args) -> int:
    with db.session() as conn:
        result = sync_mod.status(conn)

    def render(r):
        print(f"Connected:        {'yes' if r['access_configured'] else 'no (run `simplefin claim`)'}")
        print(f"Database:         {r['database']}")
        print(f"Accounts:         {r['accounts']}")
        print(f"Transactions:     {r['transactions']}")
        print(f"Last sync:        {queries.iso(r['last_successful_sync']) or 'never'}")
        print(f"Requests (24h):   {r['requests_last_24h']} of {r['daily_budget']}")
        for message in r["bridge_errors"]:
            print(f"Bridge error:     {message}")

    emit(result, args.json, render)
    return 0


def cmd_logout(args) -> int:
    removed = client.delete_access_url()
    print("Removed the stored access URL." if removed else "No access URL was stored.")
    print("To fully revoke access, disable this app connection at https://bridge.simplefin.org.")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run

    run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simplefin", description="Sync and query SimpleFIN Bridge data locally.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    p = add("claim", cmd_claim, "exchange a setup token for an access URL (stored in Keychain)")
    p.add_argument("--stdin", action="store_true", help="read the token from stdin instead of a hidden prompt")
    p.add_argument("--replace", action="store_true", help="overwrite an existing stored access URL")

    p = add("sync", cmd_sync, "fetch new data from SimpleFIN Bridge (uses the ~24/day quota)")
    p.add_argument("--days", type=int, help="backfill this many days (one request per 45 days)")
    p.add_argument("--force", action="store_true", help="ignore the local daily request budget")

    add("accounts", cmd_accounts, "list accounts and balances")

    p = add("transactions", cmd_transactions, "list transactions, newest first")
    p.add_argument("--account", help="account id or part of its name")
    p.add_argument("--since", default="30d", help="YYYY-MM-DD or e.g. 90d (default 30d)")
    p.add_argument("--until", help="YYYY-MM-DD, exclusive")
    p.add_argument("--search", help="match description, payee or memo")
    p.add_argument("--min", help="minimum signed amount")
    p.add_argument("--max", help="maximum signed amount (e.g. -100 for charges of $100+)")
    p.add_argument("--pending", action="store_true", help="only pending transactions")
    p.add_argument("--limit", type=int, default=100)

    p = add("balances", cmd_balances, "balance history recorded at each sync")
    p.add_argument("account", help="account id or part of its name")
    p.add_argument("--since")

    p = add("summary", cmd_summary, "money in and out per account")
    p.add_argument("--since", default="30d")
    p.add_argument("--until")
    p.add_argument("--account")

    add("status", cmd_status, "connection, last sync and quota")
    add("logout", cmd_logout, "delete the stored access URL")
    add("mcp", cmd_mcp, "run the MCP server over stdio")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except client.SimpleFINError as exc:
        print(f"error: {client.sanitize(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
