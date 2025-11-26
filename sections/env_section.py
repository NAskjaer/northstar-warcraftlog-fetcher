from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from src.api_client import get_wcl_token


def render_env_section(env_path: Path) -> None:
    """Render the Warcraft Logs credentials section.

    This reads from st.session_state['env_validated'] and updates it when
    credentials are successfully validated.
    """
    wcl_id = os.getenv("WCL_CLIENT_ID", "")
    wcl_secret = os.getenv("WCL_CLIENT_SECRET", "")

    env_ok = st.session_state.get("env_validated", False)

    with st.expander(
        "1. Warcraft Logs API credentials",
        expanded=not env_ok,
    ):
        if env_ok:
            st.success("Warcraft Logs credentials found and marked as valid.")
        else:
            st.warning(
                "No valid Warcraft Logs API credentials found.\n\n"
                "Before you can query reports, configure your Client ID and Secret."
            )

        st.markdown(
            """
To create a client:

1. Go to **https://www.warcraftlogs.com/api/clients/**  
2. Click **+ Create Client**  
3. **Name:** e.g. `northstar-log-fetcher`  
4. **Redirect URL:** `http://localhost`  
5. Leave **Public Client** unchecked (client should be private)  
6. Click **Create** and copy the **Client ID** and **Client Secret** below.
"""
        )

        client_id = st.text_input(
            "WCL_CLIENT_ID",
            value=wcl_id,
            placeholder="Paste your Warcraft Logs Client ID",
            key="wcl_client_id",
        )

        client_secret = st.text_input(
            "WCL_CLIENT_SECRET",
            value=wcl_secret,
            placeholder="Paste your Warcraft Logs Client Secret",
            type="password",
            key="wcl_client_secret",
        )

        def save_and_validate() -> None:
            cid = client_id.strip()
            csecret = client_secret.strip()

            if not cid or not csecret:
                st.error("Both `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET` are required.")
                return

            # Write .env file
            try:
                env_path.write_text(
                    f"WCL_CLIENT_ID={cid}\nWCL_CLIENT_SECRET={csecret}\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                st.error(f"Could not write `.env` file: {exc}")
                return

            # Update process env and reload dotenv
            os.environ["WCL_CLIENT_ID"] = cid
            os.environ["WCL_CLIENT_SECRET"] = csecret
            load_dotenv(env_path, override=True)

            st.info("Validating credentials with Warcraft Logs…")

            try:
                get_wcl_token()
            except Exception as exc:  # noqa: BLE001
                st.session_state["env_validated"] = False
                st.error(
                    "❌ Validation failed.\n\n"
                    "Warcraft Logs did not accept your credentials or there was a "
                    "network error."
                )
                st.code(str(exc))
            else:
                st.session_state["env_validated"] = True
                st.success(
                    "✅ Credentials saved and validated successfully! "
                    "You now have a working `.env` file in your project root."
                )
                # Rerun so the rest of the UI sees the updated state/env
                st.rerun()

        if st.button("Save & Validate", key="save_validate_env"):
            save_and_validate()
