"""Merchant normalization, recurring-charge detection and merchant spending rollups."""

import re
import sqlite3
import statistics
import time
from collections import defaultdict
from decimal import Decimal

from .queries import TX_DATE, iso, parse_date

DAY = 86400

# Payment wrappers and processors that prefix the real merchant name.
_PREFIXES = re.compile(
    r"^(?:APLPAY|APPLE PAY|GOOGLE \*|GOOGLE|PAYPAL \*|PAYPAL|PP\*|SQ \*|SQ\*|TST\* ?|TST\*|SP \*|SP |"
    r"DD \*|DOORDASH\*|IC\* ?|PY \*|CKO\*|BT\*|FS \*|WPY\*|POS |ACH |DEBIT |RECURRING |PURCHASE )\s*",
    re.I,
)
_US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)

# Money moving between the user's own accounts or paying off a card, not spending.
_TRANSFER = re.compile(
    r"ONLINE TRANSFER|TRANSFER (TO|FROM)|AUTOPAY|AUTO PAY|PAYMENT[ -]*THANK YOU|MOBILE PAYMENT|"
    r"ONLINE PAYMENT|PAYMENT RECEIVED|AMERICAN EXPRESS (CREDIT|TRA)|CREDIT CARD PAYMENT|EPAYMENT|EPAY ",
    re.I,
)
# Card-present (tap to pay) purchases are almost never subscriptions.
_CARD_PRESENT = re.compile(r"^(APLPAY|APPLE PAY)\b", re.I)

# Cadence name, nominal days, accepted interval range.
CADENCES = [
    ("weekly", 7, (5, 9)),
    ("biweekly", 14, (12, 17)),
    ("monthly", 30.44, (25, 36)),
    ("quarterly", 91.3, (80, 100)),
    ("yearly", 365.25, (340, 390)),
]


def merchant_key(description: str) -> str:
    """Reduce a raw bank description to a stable merchant name, e.g.
    'PAYPAL *AUTODESK 4029357733 CA' -> 'AUTODESK', 'AplPay CHICK-FIL-A #JACKSONVILLE FL' -> 'CHICK-FIL-A'.
    """
    text = description.upper().strip()
    for _ in range(3):
        stripped = _PREFIXES.sub("", text)
        if stripped == text:
            break
        text = stripped
    tokens = []
    for raw in re.split(r"\s+", text):
        token = raw.strip(".,;:*")
        if not token or token.startswith("#"):
            continue
        if sum(ch.isdigit() for ch in token) >= 3:  # phone numbers, store and reference ids
            continue
        tokens.append(token)
    while len(tokens) > 1 and tokens[-1] in _US_STATES:
        tokens.pop()
    # Domains like APPLE.COM/BILL keep only the domain.
    tokens = [re.sub(r"(\.COM|\.NET|\.IO|\.AI|\.CO)/.*$", r"\1", t) for t in tokens]
    key = " ".join(tokens[:3]) if tokens else description.upper().strip()
    return key or "UNKNOWN"


def _rows(conn: sqlite3.Connection, start: int | None, end: int | None) -> list[sqlite3.Row]:
    where, params = ["t.pending = 0"], []
    if start is not None:
        where.append(f"{TX_DATE} >= ?")
        params.append(start)
    if end is not None:
        where.append(f"{TX_DATE} < ?")
        params.append(end)
    return conn.execute(
        f"""SELECT t.description, t.payee, t.memo, t.amount_cents, {TX_DATE} AS date, a.name AS account
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE {' AND '.join(where)} ORDER BY date""",
        params,
    ).fetchall()


def is_transfer(row: sqlite3.Row) -> bool:
    return bool(_TRANSFER.search(row["description"] or "") or _TRANSFER.search(row["payee"] or ""))


def _money(cents: float) -> str:
    return f"{Decimal(round(cents)) / 100:.2f}"


def _cadence_for(intervals: list[float]) -> tuple[str, float] | None:
    median = statistics.median(intervals)
    for name, nominal, (low, high) in CADENCES:
        if low <= median <= high:
            in_band = sum(1 for d in intervals if low <= d <= high)
            # Allow one skipped or doubled period when there's enough data.
            if in_band >= max(1, len(intervals) - 1) or in_band / len(intervals) >= 0.75:
                return name, nominal
    return None


def _fixed_buckets(charges: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Group charges whose amounts are within 2% (min 50 cents) of each other."""
    ordered = sorted(charges, key=lambda c: abs(c["amount_cents"]))
    buckets: list[list[sqlite3.Row]] = []
    for c in ordered:
        amount = abs(c["amount_cents"])
        if buckets:
            anchor = abs(buckets[-1][0]["amount_cents"])
            if amount - anchor <= max(50, anchor * 0.02):
                buckets[-1].append(c)
                continue
        buckets.append([c])
    return [b for b in buckets if len(b) >= 2]


def _evaluate(
    matched: list[sqlite3.Row], charges: list[sqlite3.Row], now: float, min_occurrences: int, *, fixed: bool
) -> dict | None:
    if len(matched) < max(2, min_occurrences):
        return None
    amounts = [abs(c["amount_cents"]) for c in matched]
    mean = statistics.mean(amounts)
    cv = statistics.pstdev(amounts) / mean if mean else 1.0
    # Collapse same-day duplicates (split charges) before measuring intervals.
    days = sorted({int(c["date"] // DAY) for c in matched})
    occurrences = len(days)
    if occurrences < max(2, min_occurrences):
        return None
    cadence = _cadence_for([b - a for a, b in zip(days, days[1:])])
    if cadence is None:
        return None
    name, nominal = cadence
    frequent = name in ("weekly", "biweekly")
    # Two visits a week apart is a habit, not a subscription.
    if frequent and (occurrences < 3 or cv > 0.12):
        return None
    # Tap-to-pay purchases are shopping unless identical and monthly or longer (a membership
    # paid in person). A loose cluster at a merchant that mostly charges other amounts is too.
    card_present = all(_CARD_PRESENT.match(c["description"] or "") for c in matched)
    mostly_other = not fixed and len(matched) / len(charges) < 0.6
    if (card_present or mostly_other) and not (occurrences >= 3 and cv <= 0.02 and not frequent):
        return None
    last = max(c["date"] for c in matched)
    next_expected = last + nominal * DAY
    active = (now - next_expected) / DAY <= max(7, nominal * 0.5)
    typical = statistics.median(amounts)
    if occurrences >= 3 and cv <= 0.05:
        confidence = "high"
    elif occurrences >= 3 or cv <= 0.02:
        confidence = "medium"
    else:
        confidence = "low"
    example = max(matched, key=lambda c: c["date"])["description"]
    if example.upper().startswith("PLAN FEE"):
        kind = "installment plan fee"
    elif cv > 0.05:
        kind = "bill"
    else:
        kind = "subscription"
    return {
        "kind": kind,
        "cadence": name,
        "typical_amount": _money(typical),
        "amount_varies": cv > 0.05,
        "monthly_cost": _money(typical * 30.44 / nominal),
        "yearly_cost": _money(typical * 365.25 / nominal),
        "occurrences": occurrences,
        "first_seen": iso(int(min(c["date"] for c in matched)))[:10],
        "last_charged": iso(int(last))[:10],
        "next_expected": iso(int(next_expected))[:10],
        "status": "active" if active else "possibly cancelled",
        "confidence": confidence,
        "accounts": sorted({c["account"] for c in matched}),
        "example": example,
    }


def find_recurring(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    min_occurrences: int = 2,
    include_inactive: bool = False,
    include_income: bool = False,
) -> dict:
    """Detect charges that repeat on a regular cadence with a consistent amount."""
    now = time.time()
    rows = _rows(conn, parse_date(since), None)
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["amount_cents"] == 0 or (r["amount_cents"] > 0 and not include_income) or is_transfer(r):
            continue
        direction = "out" if r["amount_cents"] < 0 else "in"
        groups[(merchant_key(r["payee"] or r["description"]), direction)].append(r)

    results = []
    for (merchant, direction), charges in groups.items():
        if len(charges) < max(2, min_occurrences):
            continue
        # First look for exact-price series (one merchant can bill several subscriptions, e.g.
        # Apple or Google); fall back to a looser cluster for bills that vary (utilities, phone).
        found = []
        for bucket in _fixed_buckets(charges):
            hit = _evaluate(bucket, charges, now, min_occurrences, fixed=True)
            if hit:
                found.append(hit)
        if not found:
            amounts = [abs(c["amount_cents"]) for c in charges]
            median_amount = statistics.median(amounts)
            tolerance = max(200, median_amount * 0.15)
            cluster = [c for c in charges if abs(abs(c["amount_cents"]) - median_amount) <= tolerance]
            hit = _evaluate(cluster, charges, now, min_occurrences, fixed=False)
            if hit:
                found.append(hit)
        for hit in found:
            if hit["status"] != "active" and not include_inactive:
                continue
            hit["merchant"] = merchant if len(found) == 1 else f"{merchant} ({hit['typical_amount']})"
            hit["direction"] = direction
            results.append(hit)

    results.sort(key=lambda r: -float(r["monthly_cost"]))
    outflows = [r for r in results if r["direction"] == "out" and r["status"] == "active"]
    data_start = conn.execute(f"SELECT MIN({TX_DATE}) FROM transactions t").fetchone()[0]
    return {
        "recurring": results,
        "active_monthly_total": _money(sum(float(r["monthly_cost"]) * 100 for r in outflows)),
        "active_yearly_total": _money(sum(float(r["yearly_cost"]) * 100 for r in outflows)),
        "history_from": iso(data_start)[:10] if data_start else None,
        "note": "Yearly and quarterly charges need history longer than one period to be detected.",
    }


def top_merchants(
    conn: sqlite3.Connection,
    *,
    since: str | None = "30d",
    until: str | None = None,
    limit: int = 20,
    include_transfers: bool = False,
) -> dict:
    """Spending (outflows, excluding pending and transfers) grouped by normalized merchant."""
    start, end = parse_date(since), parse_date(until)
    totals: dict[str, dict] = {}
    for r in _rows(conn, start, end):
        if r["amount_cents"] >= 0 or (not include_transfers and is_transfer(r)):
            continue
        key = merchant_key(r["payee"] or r["description"])
        entry = totals.setdefault(key, {"merchant": key, "cents": 0, "transactions": 0, "accounts": set()})
        entry["cents"] += -r["amount_cents"]
        entry["transactions"] += 1
        entry["accounts"].add(r["account"])
    ranked = sorted(totals.values(), key=lambda e: -e["cents"])[: max(1, limit)]
    return {
        "since": iso(start),
        "until": iso(end),
        "merchants": [
            {
                "merchant": e["merchant"],
                "spent": _money(e["cents"]),
                "transactions": e["transactions"],
                "average": _money(e["cents"] / e["transactions"]),
                "accounts": sorted(e["accounts"]),
            }
            for e in ranked
        ],
    }
