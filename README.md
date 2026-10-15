# simplefin-cli

![simplefin CLI showing accounts, transactions and recurring charges](assets/hero.png)

Read-only access to your bank, card and loan accounts through [SimpleFIN Bridge](https://bridge.simplefin.org), as a CLI and an MCP server for agents.

Data is synced into a local SQLite database, and every query reads that copy. Syncing happens automatically (see [Auto-sync](#auto-sync)), within SimpleFIN's limit of about 24 requests a day; it disables access tokens that keep going over. Works on macOS, Linux and Windows.

## Setup

1. **Install** (needs [uv](https://docs.astral.sh/uv/)):

   ```sh
   uv tool install git+https://github.com/R44VC0RP/simplefin-cli
   ```

2. **Connect (a human does this step).** At [bridge.simplefin.org](https://bridge.simplefin.org), connect your banks and create a setup token for a new app. Then run this in a terminal and paste the token:

   ```sh
   simplefin claim
   ```

   The token works once. The access URL it returns is saved to the system keychain (macOS Keychain, Windows Credential Manager, or Secret Service on Linux) and never printed.

3. **Load history:**

   ```sh
   simplefin sync --days 365   # one request per 45 days of history
   simplefin accounts          # check every account shows up
   ```

4. **Add the MCP server** to your agent:

   | Agent | Config |
   |---|---|
   | OpenCode (`opencode.json`) | `"mcp": { "simplefin": { "type": "local", "command": ["simplefin", "mcp"] } }` |
   | Claude Code | `claude mcp add simplefin -- simplefin mcp` |
   | Codex (`~/.codex/config.toml`) | `[mcp_servers.simplefin]`<br>`command = "simplefin"`<br>`args = ["mcp"]` |
   | Anything else | stdio server, command `simplefin mcp` |

That's it. Data stays fresh on its own; no cron job needed.

## Auto-sync

Built in, on every OS, with no scheduler:

- **MCP server:** while it runs, it checks every 10 minutes and syncs when data is older than 12 hours.
- **CLI:** `accounts`, `transactions`, `summary` and `balances` sync first when data is stale. Pass `--no-sync` to skip.
- **Quota-safe:** every process shares one lease in the database, so several agents or terminals at once still make one request, and failed attempts retry at most every 3 hours. `sync_now` also skips if any sync started in the last 10 minutes.

Set `SIMPLEFIN_AUTO_SYNC_HOURS` to change the 12 hours (values under 3 still wait 3 hours between attempts), or `0` to turn it off. If SimpleFIN rejects the access URL or needs payment, auto-sync pauses for a day; `simplefin claim` resets it.

## Rules for agents

- Never ask the user to paste a setup token or access URL into chat. If `status` says not connected, tell them to run `simplefin claim` themselves.
- Read with the query tools; they're free. Auto-sync keeps data within 12 hours, so you rarely need `sync_now`. Never loop on it.
- Amounts are signed strings: positive means money into the account, negative means money out. Card purchases are usually negative; check a known one before assuming.
- Recurring detection only sees synced history (SimpleFIN provides 90 days, then the local database keeps growing). Monthly charges need two or more hits and yearly ones over a year of data, so say so when it matters. Check `confidence` before calling something a subscription.
- Show Bridge errors (from `sync_status`) to the user. They usually mean a bank needs reconnecting at bridge.simplefin.org.

## MCP tools

| Tool | What it does |
|---|---|
| `list_accounts` | Accounts with institution, balance and balance date |
| `list_transactions` | Newest first. Filters: `account` (id or part of name), `since`/`until` (`YYYY-MM-DD` or `30d`), `search`, `min_amount`/`max_amount`, `pending`, `limit` |
| `summarize_cash_flow` | Money in and out per account over a period |
| `get_balance_history` | Balance recorded at each sync |
| `find_recurring_charges` | Subscriptions, bills and installment fees that repeat: cadence, amount, monthly and yearly cost, next expected charge, confidence, and the total per month |
| `top_merchants` | Spending by merchant, with names normalized across PayPal, Apple Pay and processor prefixes; transfers and card payments excluded |
| `sync_status` | Last sync, requests used today, Bridge errors |
| `sync_now` | Fetch from SimpleFIN. Refuses within an hour of the last sync |

## CLI

Every command accepts `--json`.

```sh
simplefin status                                 # connected? last sync, quota used
simplefin sync [--days N] [--force]              # --force skips the local 20/day budget
simplefin accounts                               # read commands auto-sync stale data; --no-sync to skip
simplefin transactions [--account chase] [--since 30d] [--until 2026-10-01]
                       [--search uber] [--min -500] [--max -100] [--pending] [--limit 100]
simplefin summary [--since 30d] [--account chase]
simplefin balances <account> [--since 90d]
simplefin recurring [--all] [--income] [--since 2026-01-01]   # subscriptions and bills
simplefin merchants [--since 30d] [--limit 20] [--transfers]  # spending by merchant
simplefin logout                                 # removes the stored access URL
simplefin mcp                                    # MCP server over stdio
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SIMPLEFIN_DB` | macOS `~/Library/Application Support/simplefin-cli/`, Linux `~/.local/share/simplefin-cli/`, Windows `%LOCALAPPDATA%\simplefin-cli\` (file `simplefin.db`) | Database location |
| `SIMPLEFIN_AUTO_SYNC_HOURS` | `12` | Sync when data is older than this; `0` turns auto-sync off |
| `SIMPLEFIN_ACCESS_URL` | system keychain | Use this access URL instead of the keychain (for CI or other machines; treat it as a password) |

To revoke access, run `simplefin logout` and disable the app connection at bridge.simplefin.org.
