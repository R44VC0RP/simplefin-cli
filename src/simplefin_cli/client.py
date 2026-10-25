import base64
import binascii
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import keyring

KEYRING_SERVICE = "simplefin-cli"
KEYRING_ACCOUNT = "access-url"
ENV_ACCESS_URL = "SIMPLEFIN_ACCESS_URL"
TIMEOUT_SECONDS = 120
MAX_WINDOW_SECONDS = 90 * 86400
# Cloudflare in front of the Bridge blocks the default Python-urllib user agent (error 1010).
USER_AGENT = "simplefin-cli/0.1 (+https://www.simplefin.org/protocol.html)"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SimpleFINError(Exception):
    pass


class TokenRejected(SimpleFINError):
    pass


class AccessRevoked(SimpleFINError):
    pass


class PaymentRequired(SimpleFINError):
    pass


class KeychainError(SimpleFINError):
    """The stored access URL exists or may exist but couldn't be read or written."""


def sanitize(text: object) -> str:
    return _CONTROL_CHARS.sub(" ", str(text)).strip()


def _require_https(url: str) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SimpleFINError("SimpleFIN URLs must use HTTPS")
    return parts


def _raise_if_edge_block(exc: urllib.error.HTTPError) -> None:
    try:
        body = exc.read(200).decode(errors="replace")
    except Exception:
        return
    if body.startswith("error code:"):
        raise SimpleFINError(f"Request blocked by the Bridge's CDN ({sanitize(body)}), not by SimpleFIN auth")


def decode_setup_token(token: str) -> str:
    cleaned = "".join(token.split())
    if not cleaned:
        raise SimpleFINError("Setup token is empty")
    try:
        claim_url = base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise SimpleFINError("Setup token is not valid base64") from exc
    _require_https(claim_url)
    return claim_url


def claim_access_url(token: str) -> str:
    claim_url = decode_setup_token(token)
    request = urllib.request.Request(
        claim_url, data=b"", method="POST", headers={"Content-Length": "0", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
        ) as response:
            access_url = response.read().decode().strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            _raise_if_edge_block(exc)
            raise TokenRejected(
                "SimpleFIN rejected the setup token: it does not exist or was already claimed. "
                "If you did not claim it yourself, your data may be compromised: disable the token "
                "at https://bridge.simplefin.org and create a new one."
            ) from None
        raise SimpleFINError(f"Claim failed with HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise SimpleFINError(f"Claim failed: {sanitize(exc.reason)}") from None
    parts = _require_https(access_url)
    if not parts.username or not parts.password:
        raise SimpleFINError("SimpleFIN returned an access URL without credentials")
    return access_url


# On macOS the Keychain ACL pins the calling binary. uv's ad-hoc signed Python changes on every
# patch upgrade, which re-triggers the access prompt. Apple's signed `security` tool stays stable,
# so "Always Allow" sticks. Other platforms use keyring.
_SECURITY = "/usr/bin/security"
_USE_SECURITY_CLI = sys.platform == "darwin" and os.path.exists(_SECURITY)
_SECURITY_TIMEOUT = 60  # a Keychain prompt nobody can see (e.g. a background MCP server) must not hang forever
_ITEM_NOT_FOUND = 44  # errSecItemNotFound, as returned by `security`


def _security(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [_SECURITY, *args], input=stdin, capture_output=True, text=True, timeout=_SECURITY_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise KeychainError(
            "Timed out waiting for Keychain (an access prompt may be waiting, or the keychain is locked)"
        ) from None


def _security_add(access_url: str) -> None:
    # `security -i` reads the command from stdin, so the secret never appears in the process list.
    # It exits 0 even when the command fails, so any stderr output counts as failure.
    command = (
        f'add-generic-password -s "{KEYRING_SERVICE}" -a "{KEYRING_ACCOUNT}" '
        f'-T "{_SECURITY}" -w "{access_url}"\n'
    )
    result = _security(["-i"], stdin=command)
    if result.returncode != 0 or result.stderr.strip():
        raise KeychainError(f"Saving to Keychain failed: {sanitize(result.stderr) or result.returncode}")


def save_access_url(access_url: str) -> None:
    _require_https(access_url)
    if not _USE_SECURITY_CLI:
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, access_url)
        return
    if any(c in access_url for c in '"\\') or _CONTROL_CHARS.search(access_url):
        raise KeychainError("Access URL contains characters the Keychain tool cannot store")
    previous = _keychain_read()
    # Replace rather than update: `-U` with `-T` rewrites the ACL, which needs user approval.
    _security(["delete-generic-password", "-s", KEYRING_SERVICE, "-a", KEYRING_ACCOUNT])
    try:
        _security_add(access_url)
    except KeychainError:
        if previous:
            try:
                _security_add(previous)  # don't leave the user with nothing stored
            except KeychainError:
                pass
        raise


def _keychain_read() -> str | None:
    result = _security(["find-generic-password", "-s", KEYRING_SERVICE, "-a", KEYRING_ACCOUNT, "-w"])
    if result.returncode == _ITEM_NOT_FOUND:
        return None
    if result.returncode != 0:
        raise KeychainError(
            f"Couldn't read the access URL from Keychain (security exit {result.returncode}: "
            f"{sanitize(result.stderr) or 'no details'}). Unlock the keychain or allow access, then retry."
        )
    return result.stdout.strip() or None


def load_access_url() -> str | None:
    """The stored access URL, or None when none is stored. Raises KeychainError if it can't be read."""
    if os.environ.get(ENV_ACCESS_URL):
        return os.environ[ENV_ACCESS_URL]
    if _USE_SECURITY_CLI:
        return _keychain_read()
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except keyring.errors.KeyringError:
        return None  # e.g. headless Linux without Secret Service: treat as not connected


def delete_access_url() -> bool:
    if _USE_SECURITY_CLI:
        result = _security(["delete-generic-password", "-s", KEYRING_SERVICE, "-a", KEYRING_ACCOUNT])
        if result.returncode == _ITEM_NOT_FOUND:
            return False
        if result.returncode != 0:
            raise KeychainError(f"Couldn't delete the Keychain item: {sanitize(result.stderr) or result.returncode}")
        return True
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        return False
    return True


def fetch_accounts(
    access_url: str,
    *,
    start_date: int | None = None,
    end_date: int | None = None,
    pending: bool = True,
    account_ids: list[str] | None = None,
    balances_only: bool = False,
) -> dict:
    """One GET /accounts request. Each call counts against the Bridge's ~24/day quota."""
    parts = _require_https(access_url)
    if not parts.username or not parts.password:
        raise SimpleFINError("Stored access URL has no credentials; run `simplefin claim` again")
    if start_date is not None and end_date is not None and end_date - start_date > MAX_WINDOW_SECONDS:
        raise SimpleFINError("SimpleFIN limits each request to a 90-day window")

    params: list[tuple[str, str]] = [("version", "2")]
    if start_date is not None:
        params.append(("start-date", str(int(start_date))))
    if end_date is not None:
        params.append(("end-date", str(int(end_date))))
    if pending:
        params.append(("pending", "1"))
    if balances_only:
        params.append(("balances-only", "1"))
    for account_id in account_ids or []:
        params.append(("account", account_id))

    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    base = urllib.parse.urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
    url = f"{base}/accounts?{urllib.parse.urlencode(params)}"
    credentials = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password)}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    # Unredirected so a redirect can never carry the credentials to another host or to plain HTTP.
    request.add_unredirected_header(
        "Authorization", "Basic " + base64.b64encode(credentials.encode()).decode()
    )
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
        ) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            _raise_if_edge_block(exc)
            raise AccessRevoked(
                "SimpleFIN rejected the access credentials (revoked or invalid). "
                "Create a new setup token and run `simplefin claim`."
            ) from None
        if exc.code == 402:
            raise PaymentRequired("SimpleFIN Bridge subscription payment is required") from None
        raise SimpleFINError(f"GET /accounts failed with HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise SimpleFINError(f"GET /accounts failed: {sanitize(exc.reason)}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SimpleFINError("SimpleFIN returned invalid JSON") from None
    except (OSError, http.client.HTTPException) as exc:
        raise SimpleFINError(f"GET /accounts failed: {type(exc).__name__}") from None
