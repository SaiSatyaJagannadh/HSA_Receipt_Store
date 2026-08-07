"""Login gate for hosted deployments.

Streamlit Community Cloud only publishes **public** apps — private ones need a
Snowflake plan. A public URL with no gate would hand any passer-by the owner's
medical expense history and, through the refresh token in secrets, write access
to their Google Drive. So a deployed instance must not serve a single page until
the visitor has proved they are the owner.

The rule this module enforces: **if real Google credentials are present, a login
gate must be present too.** If it isn't, the app refuses to run rather than
quietly serving an open door. Locally there are no secrets and the server is
bound to loopback, so the gate stays out of the way.
"""

import streamlit as st


def _secrets_has(key: str) -> bool:
    try:
        return key in st.secrets
    except Exception:
        return False


def is_hosted() -> bool:
    """True when Google credentials arrive via secrets, i.e. a real deployment."""
    return _secrets_has("google_token")


def auth_configured() -> bool:
    return _secrets_has("auth")


def allowed_emails() -> set[str]:
    try:
        raw = st.secrets["hsa"].get("allowed_emails", "")
    except Exception:
        raw = ""
    return {e.strip().lower() for e in str(raw).split(",") if e.strip()}


def require_login() -> None:
    """Call at the top of every page, before anything is rendered."""
    if not is_hosted():
        return  # local run: no secrets, loopback-bound

    if not auth_configured():
        # Fail closed. Credentials without a gate is the one combination that
        # must never serve traffic.
        st.error(
            "**Refusing to start.** This instance has Google credentials but no "
            "`[auth]` section in secrets, so anyone with the URL could read your "
            "records and write to your Drive. Add the `[auth]` block (see "
            "README → Deploying) or remove `google_token` from secrets."
        )
        st.stop()

    if not st.user.is_logged_in:
        st.title("🧾 HSAVault")
        st.caption("Your HSA receipt vault. Sign in to continue.")
        st.button("Sign in with Google", type="primary", on_click=st.login)
        st.stop()

    allowed = allowed_emails()
    email = (getattr(st.user, "email", "") or "").lower()
    if not allowed:
        st.error(
            "**Refusing to start.** No `allowed_emails` configured, so every "
            "Google account on earth would be let in. Set it in secrets under "
            "`[hsa]`."
        )
        st.stop()
    if email not in allowed:
        st.error(f"`{email}` is not authorized for this vault.")
        st.button("Sign out", on_click=st.logout)
        st.stop()

    with st.sidebar:
        st.caption(f"Signed in as {email}")
        st.button("Sign out", on_click=st.logout, width="stretch")
