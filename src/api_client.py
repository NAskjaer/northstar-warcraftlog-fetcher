# src/api_client.py
import os
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

def get_rate_limit() -> dict:
    """
    Return the current Warcraft Logs API points budget:
        {"limit_per_hour": int, "points_spent": float, "points_reset_in": int}
    points_reset_in is seconds until the hourly window resets.
    Raises RuntimeError on failure (including 429).
    """
    q = """
    query {
      rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
    }
    """
    data = run_wcl_query(q)["data"]["rateLimitData"]
    return {
        "limit_per_hour": int(data.get("limitPerHour", 0)),
        "points_spent": float(data.get("pointsSpentThisHour", 0)),
        "points_reset_in": int(data.get("pointsResetIn", 0)),
    }


def run_wcl_query(
    query: str, variables: dict | None = None, max_retries: int = 3
) -> dict:
    """
    Run a GraphQL query against the Warcraft Logs v2 client API and return
    the full JSON response (including the top-level 'data' object).

    Retries on HTTP 429 (Too Many Requests) with backoff, honoring the
    Retry-After header when present, so bursts of concurrent requests don't
    fail outright.

    Raises RuntimeError if the API returns GraphQL errors, or if it keeps
    returning 429 after max_retries.
    """
    import time as _time

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

        resp = requests.post(
            WCL_GRAPHQL_URL,
            json=payload,
            headers=headers,
            timeout=30,
        )

        if resp.status_code == 429:
            if attempt >= max_retries:
                raise RuntimeError(
                    "Warcraft Logs rate limit hit (HTTP 429) and did not clear "
                    "after retries. You've likely exhausted the hourly points "
                    "budget — reduce the rank range or wait before retrying."
                )
            # Fail fast: short, capped waits so the UI doesn't block for minutes.
            # If the hourly points budget is exhausted, no amount of waiting in
            # a single run will help — surface a clear error instead.
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = min(int(retry_after), 10)
            else:
                wait = min(2 ** attempt, 8)  # 1, 2, 4, capped 8
            _time.sleep(wait)
            continue

        resp.raise_for_status()
        result = resp.json()

        if "errors" in result:
            print("Warcraft Logs API returned errors:")
            for err in result["errors"]:
                print(err)
            raise RuntimeError("Warcraft Logs API error — see messages above.")

        return result

    # Unreachable, but keeps type-checkers happy.
    raise RuntimeError("Warcraft Logs query failed unexpectedly.")
