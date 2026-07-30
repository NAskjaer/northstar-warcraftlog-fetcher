# src/api_client.py
import os
import threading
import time

# Make Python's SSL use the operating-system certificate store *before* requests
# is imported. On machines where antivirus/proxy software (e.g. Norton) performs
# HTTPS/TLS inspection, the presented certificate is signed by a private root CA
# that lives in the Windows cert store but NOT in certifi's bundle. Without this,
# every Warcraft Logs API call fails with CERTIFICATE_VERIFY_FAILED.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    # truststore is optional; if it's unavailable we fall back to certifi.
    pass

import requests
from dotenv import load_dotenv

load_dotenv()

WCL_OAUTH_URL = "https://www.warcraftlogs.com/oauth/token"
WCL_GRAPHQL_URL = "https://www.warcraftlogs.com/api/v2/client"

_token_cache: dict[str, float | str | None] = {
    "access_token": None,
    "expires_at": 0.0,
}

# Last retry/pause event from run_wcl_query, if any — lets the UI show a
# live "currently throttled, retrying..." indicator instead of this only
# ever showing up in the terminal. Plain module state (no Streamlit import
# here) so it's usable from the CLI/overnight paths too.
_retry_status_lock = threading.Lock()
_retry_status: dict[str, object] | None = None


def _set_retry_status(message: str) -> None:
    global _retry_status
    with _retry_status_lock:
        _retry_status = {"message": message, "at": time.time()}


def _clear_retry_status() -> None:
    global _retry_status
    with _retry_status_lock:
        _retry_status = None


def get_retry_status(max_age_seconds: float = 60.0) -> dict[str, object] | None:
    """
    The most recent throttle/retry event, if any and if recent enough.
    Returns None once a request has since succeeded (which clears it) or
    the event is older than ``max_age_seconds`` (a stale blip from a while
    ago isn't "currently happening").
    """
    with _retry_status_lock:
        if _retry_status is None:
            return None
        if time.time() - _retry_status["at"] > max_age_seconds:
            return None
        return dict(_retry_status)


def get_wcl_token() -> str:
    """
    Get (and cache) a Warcraft Logs OAuth2 access token using client credentials.
    Requires WCL_CLIENT_ID and WCL_CLIENT_SECRET in .env
    """
    import time as _time

    client_id = os.getenv("WCL_CLIENT_ID")
    client_secret = os.getenv("WCL_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise ValueError(
            "Missing WCL_CLIENT_ID or WCL_CLIENT_SECRET in your .env file."
        )

    now = _time.time()
    cached = _token_cache.get("access_token")
    exp = _token_cache.get("expires_at", 0.0)

    # Use cached token if still valid
    if cached and now < exp - 60:
        return cached

    try:
        resp = requests.post(
            WCL_OAUTH_URL,
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=15,
        )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        text = exc.response.text if exc.response is not None else ""
        raise RuntimeError(
            f"Failed to get Warcraft Logs API token (HTTP {status}). "
            "This is usually a temporary Warcraft Logs outage or maintenance.\n"
            f"Raw response: {text[:500]}"
        ) from exc
    except requests.RequestException as exc:
        # Other network issues (DNS, timeout, etc.)
        raise RuntimeError(
            f"Network error while contacting Warcraft Logs token endpoint: {exc}"
        ) from exc

    data = resp.json()
    access_token = data["access_token"]
    expires_in = float(data.get("expires_in", 3600))

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + expires_in

    return access_token

_RETRYABLE_ERROR_MARKERS = (
    "rate limit",
    "429",
    "points budget",
    "timing out",
    "failing to connect",
)


def is_retryable_error(message: str) -> bool:
    """
    True if an error message looks like a transient WCL failure (rate limit
    exhaustion, or a network timeout/connection failure after retries) —
    the exact wording run_wcl_query raises for those cases — rather than a
    genuine data result (e.g. "no public logs in range").

    Used by callers that track per-guild "was this fully processed" state
    (the overnight runner) to tell "we don't actually know, try again later"
    apart from "we checked and there's nothing here".
    """
    text = message.lower()
    return any(marker in text for marker in _RETRYABLE_ERROR_MARKERS)


def get_rate_limit(max_retries: int = 3, quiet: bool = False) -> dict:
    """
    Return the current Warcraft Logs API points budget:
        {"limit_per_hour": int, "points_spent": float, "points_reset_in": int}
    points_reset_in is seconds until the hourly window resets.
    Raises RuntimeError on failure (including 429).

    ``max_retries``/``quiet`` let a caller that's just polling "has the
    budget come back yet" (overnight/live runners, while genuinely waiting
    out an exhaustion) use a fast single attempt with no printed retry
    chatter, instead of the full 3-attempt backoff dance every poll — the
    caller already knows to check again shortly either way.
    """
    q = """
    query {
      rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
    }
    """
    data = run_wcl_query(q, max_retries=max_retries, quiet=quiet)["data"]["rateLimitData"]
    return {
        "limit_per_hour": int(data.get("limitPerHour", 0)),
        "points_spent": float(data.get("pointsSpentThisHour", 0)),
        "points_reset_in": int(data.get("pointsResetIn", 0)),
    }


def run_wcl_query(
    query: str, variables: dict | None = None, max_retries: int = 3,
    quiet: bool = False,
) -> dict:
    """
    Run a GraphQL query against the Warcraft Logs v2 client API and return
    the full JSON response (including the top-level 'data' object).

    Retries on HTTP 429 (Too Many Requests) with backoff, honoring the
    Retry-After header when present, so bursts of concurrent requests don't
    fail outright. Also retries on read timeouts / connection errors, since
    warcraftlogs.com occasionally stalls on a single request without
    actually being rate-limited.

    Raises RuntimeError if the API returns GraphQL errors, or if it keeps
    returning 429 or timing out after max_retries.

    ``quiet`` suppresses the per-attempt [RETRY]/[PAUSED]/[FAILED] prints
    (the retry-status tracker used by the UI's rate-limit indicator still
    gets updated either way) — for callers that already know a failure here
    just means "check again later" and don't need the attempt-by-attempt
    play-by-play repeated on every poll.
    """
    import time as _time
    from .log_utils import ts_print

    def _emit(msg: str) -> None:
        if not quiet:
            ts_print(msg)

    headers = {
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "variables": variables or {},
    }

    for attempt in range(max_retries + 1):
        # Fetch (cached) token each attempt; it refreshes if expired.
        headers["Authorization"] = f"Bearer {get_wcl_token()}"

        try:
            resp = requests.post(
                WCL_GRAPHQL_URL,
                json=payload,
                headers=headers,
                timeout=30,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt >= max_retries:
                msg = f"Warcraft Logs API unreachable after {max_retries + 1} attempts — giving up."
                _emit(f"  [FAILED] {msg}")
                _set_retry_status(msg)
                raise RuntimeError(
                    "Warcraft Logs API kept timing out / failing to connect "
                    f"after {max_retries + 1} attempts: {exc}"
                ) from exc
            wait = min(2 ** attempt, 5)  # 1, 2, 4, capped 5
            msg = (f"Network hiccup ({type(exc).__name__}) — retrying in {wait}s "
                   f"(attempt {attempt + 1}/{max_retries})...")
            _emit(f"  [RETRY] {msg}")
            _set_retry_status(msg)
            _time.sleep(wait)
            continue

        if resp.status_code == 429:
            if attempt >= max_retries:
                msg = ("Rate limit (429) did not clear after retries — "
                       "hourly points budget is exhausted.")
                _emit(f"  [PAUSED] {msg}")
                _set_retry_status(msg)
                raise RuntimeError(
                    "Warcraft Logs rate limit hit (HTTP 429) and did not clear "
                    "after retries. The hourly points budget is exhausted. The "
                    "budget is per API client, so an overnight run or a second "
                    "browser tab spends from the same pool. Wait for the hourly "
                    "reset, or reduce the scope, before retrying."
                )
            # Fail fast: short, capped waits so the UI doesn't block for minutes.
            # If the hourly points budget is exhausted, no amount of waiting in
            # a single run will help — surface a clear error instead.
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = min(int(retry_after), 5)
            else:
                wait = min(2 ** attempt, 5)  # 1, 2, 4, capped 5
            msg = (f"Rate limited (429) — pausing {wait}s before retry "
                   f"(attempt {attempt + 1}/{max_retries})...")
            _emit(f"  [PAUSED] {msg}")
            _set_retry_status(msg)
            _time.sleep(wait)
            continue

        resp.raise_for_status()
        result = resp.json()

        if "errors" in result:
            ts_print("Warcraft Logs API returned errors:")
            for err in result["errors"]:
                ts_print(str(err))
            raise RuntimeError("Warcraft Logs API error — see messages above.")

        _clear_retry_status()
        return result

    # Unreachable, but keeps type-checkers happy.
    raise RuntimeError("Warcraft Logs query failed unexpectedly.")
