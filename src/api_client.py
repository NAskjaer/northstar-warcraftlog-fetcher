# src/api_client.py
import os
import time
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

def run_wcl_query(query: str, variables: dict | None = None) -> dict:
    """
    Run a GraphQL query against the Warcraft Logs v2 client API and return
    the full JSON response (including the top-level 'data' object).

    Raises RuntimeError if the API returns GraphQL errors.
    """
    token = get_wcl_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "variables": variables or {},
    }

    resp = requests.post(
        WCL_GRAPHQL_URL,
        json=payload,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()

    if "errors" in result:
        print("Warcraft Logs API returned errors:")
        for err in result["errors"]:
            print(err)
        raise RuntimeError("Warcraft Logs API error — see messages above.")

    return result
