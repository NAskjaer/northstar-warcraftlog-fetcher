# src/api_client.py

import os
import base64  # (not strictly needed with the grant_type flow, but harmless)
from typing import Any, Dict

import requests
from dotenv import load_dotenv

# Load environment variables from .env in project root
load_dotenv()

WCL_TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
WCL_GRAPHQL_URL = "https://www.warcraftlogs.com/api/v2/client"

_token_cache: str | None = None


def get_wcl_token() -> str:
    """
    Request an OAuth2 client-credentials token from Warcraft Logs.

    Uses WCL_CLIENT_ID and WCL_CLIENT_SECRET from the environment.
    Caches the token in-process so we don't request it on every query.
    """
    global _token_cache
    if _token_cache:
        return _token_cache

    client_id = os.getenv("WCL_CLIENT_ID")
    client_secret = os.getenv("WCL_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise ValueError("Missing WCL_CLIENT_ID or WCL_CLIENT_SECRET in your .env file.")

    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    resp = requests.post(WCL_TOKEN_URL, data=data)
    resp.raise_for_status()

    token = resp.json()["access_token"]
    _token_cache = token
    return token


def run_wcl_query(query: str, variables: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Run a GraphQL query against the Warcraft Logs v2 API and return the full JSON.
    """
    token = get_wcl_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload: Dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    response = requests.post(WCL_GRAPHQL_URL, headers=headers, json=payload)

    # Debug print so we can see raw responses while developing
    # print("WCL RESPONSE:", response.text)

    if response.status_code != 200:
        raise RuntimeError(
            f"GraphQL request failed: {response.status_code} - {response.text}"
        )

    result: Dict[str, Any] = response.json()

    if "errors" in result:
        print("Warcraft Logs API returned errors:")
        for err in result["errors"]:
            print(err)
        raise RuntimeError("Warcraft Logs API error — see messages above.")

    # IMPORTANT: return the full JSON, not just result["data"]
    return result
