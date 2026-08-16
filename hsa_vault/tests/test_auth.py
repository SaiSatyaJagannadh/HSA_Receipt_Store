"""The login gate must fail closed.

A deployed HSAVault holds a refresh token with write access to the owner's Drive,
on a public URL. The one combination that must never serve traffic is
"credentials present, gate absent".
"""

import sys
import types

import pytest

import core


def _forget_auth_module() -> None:
    """Drop core.auth from BOTH caches.

    `from core import auth` resolves the attribute on the already-imported `core`
    package before consulting sys.modules, so popping sys.modules alone leaves
    the stale module (bound to this file's fake streamlit) reachable, and every
    later test that renders a page inherits it.
    """
    sys.modules.pop("core.auth", None)
    if hasattr(core, "auth"):
        delattr(core, "auth")


class Stop(Exception):
    """Stand-in for st.stop(), which halts the script run."""


@pytest.fixture
def st(monkeypatch):
    """Minimal fake streamlit, installed before core.auth is imported."""
    calls = {"error": [], "stopped": False, "buttons": []}

    def stop():
        calls["stopped"] = True
        raise Stop()

    mod = types.ModuleType("streamlit")
    mod.secrets = {}
    mod.user = types.SimpleNamespace(is_logged_in=False, email=None)
    mod.stop = stop
    mod.error = lambda msg, **k: calls["error"].append(msg)
    mod.title = lambda *a, **k: None
    mod.caption = lambda *a, **k: None
    mod.button = lambda label, **k: calls["buttons"].append(label)
    mod.login = lambda *a, **k: calls.setdefault("login_args", []).append(a)
    mod.logout = lambda *a, **k: None
    class _Sidebar:  # dunders resolve on the type, not the instance
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        caption = staticmethod(lambda *a, **k: None)
        button = staticmethod(lambda *a, **k: None)

    mod.sidebar = _Sidebar()
    monkeypatch.setitem(sys.modules, "streamlit", mod)
    # core.auth binds `st` at import time, so re-import it against the fake —
    # and forget it again on teardown so real pages get the real streamlit.
    _forget_auth_module()
    mod._calls = calls
    yield mod
    _forget_auth_module()


def load(st):
    import core.auth as auth

    return auth


# --- local runs are not gated ---------------------------------------------


def test_local_run_is_not_gated(st):
    """No secrets, loopback-bound: the gate must stay out of the way."""
    auth = load(st)
    auth.require_login()  # must not raise
    assert st._calls["error"] == []


# --- fail closed -----------------------------------------------------------


def test_credentials_without_auth_section_refuses_to_start(st):
    st.secrets = {"google_token": {"refresh_token": "x"}}
    auth = load(st)
    with pytest.raises(Stop):
        auth.require_login()
    assert "Refusing to start" in st._calls["error"][0]


def test_auth_configured_but_no_allowed_emails_refuses_to_start(st):
    st.secrets = {"google_token": {"refresh_token": "x"}, "auth": {"google": {"client_id": "x"}}, "hsa": {}}
    st.user = types.SimpleNamespace(is_logged_in=True, email="anyone@gmail.com")
    auth = load(st)
    with pytest.raises(Stop):
        auth.require_login()
    assert "every" in st._calls["error"][0].lower()


def test_logged_out_visitor_is_stopped_at_the_sign_in_screen(st):
    st.secrets = {"google_token": {}, "auth": {"cookie_secret": "s", "google": {"client_id": "x"}}, "hsa": {}}
    auth = load(st)
    with pytest.raises(Stop):
        auth.require_login()
    assert any("Sign in" in b for b in st._calls["buttons"])


def test_wrong_google_account_is_rejected(st):
    st.secrets = {
        "google_token": {},
        "auth": {"cookie_secret": "s", "google": {"client_id": "x"}},
        "hsa": {"allowed_emails": "owner@gmail.com"},
    }
    st.user = types.SimpleNamespace(is_logged_in=True, email="stranger@gmail.com")
    auth = load(st)
    with pytest.raises(Stop):
        auth.require_login()
    assert "not authorized" in st._calls["error"][0]


# --- the owner gets through ------------------------------------------------


def test_the_owner_is_let_through(st):
    st.secrets = {
        "google_token": {},
        "auth": {"cookie_secret": "s", "google": {"client_id": "x"}},
        "hsa": {"allowed_emails": "owner@gmail.com"},
    }
    st.user = types.SimpleNamespace(is_logged_in=True, email="owner@gmail.com")
    auth = load(st)
    auth.require_login()  # must not raise
    assert st._calls["error"] == []


def test_email_match_is_case_insensitive_and_trimmed(st):
    st.secrets = {
        "google_token": {},
        "auth": {"cookie_secret": "s", "google": {"client_id": "x"}},
        "hsa": {"allowed_emails": " Owner@Gmail.com , second@x.com "},
    }
    st.user = types.SimpleNamespace(is_logged_in=True, email="OWNER@gmail.com")
    auth = load(st)
    auth.require_login()
    assert st._calls["error"] == []


def test_a_second_allowed_address_also_works(st):
    st.secrets = {
        "google_token": {},
        "auth": {"cookie_secret": "s", "google": {"client_id": "x"}},
        "hsa": {"allowed_emails": "owner@gmail.com,spouse@gmail.com"},
    }
    st.user = types.SimpleNamespace(is_logged_in=True, email="spouse@gmail.com")
    auth = load(st)
    auth.require_login()
    assert st._calls["error"] == []


# --- provider wiring -------------------------------------------------------


def test_login_names_the_provider_matching_the_secrets_section(st):
    """st.login() with no argument resolves the provider name "default", which
    makes Streamlit look for client_id directly under [auth] and raise
    "credentials are missing for the default authentication provider". Our
    credentials live under [auth.google], so the name must be passed."""
    st.secrets = {
        "google_token": {},
        "auth": {"cookie_secret": "s", "google": {"client_id": "x"}},
        "hsa": {"allowed_emails": "owner@gmail.com"},
    }
    auth = load(st)
    auth._login()  # what the sign-in button calls
    assert st._calls["login_args"] == [("google",)]


def test_auth_section_without_the_provider_subsection_refuses_to_start(st):
    """[auth] present but [auth.google] missing is the exact misconfiguration
    that produced the login error in production."""
    st.secrets = {
        "google_token": {},
        "auth": {"cookie_secret": "s", "redirect_uri": "https://x/oauth2callback"},
        "hsa": {"allowed_emails": "owner@gmail.com"},
    }
    auth = load(st)
    with pytest.raises(Stop):
        auth.require_login()
    assert "auth.google" in st._calls["error"][0]


# --- deployment prerequisites ----------------------------------------------


def test_requirements_declares_authlib():
    """st.login() raises StreamlitMissingAuthlibError before it even validates
    credentials, so a missing Authlib breaks login on the deployed app while
    every local test still passes. google-auth-oauthlib is a DIFFERENT package
    and does not satisfy it."""
    import pathlib

    req = (pathlib.Path(__file__).resolve().parents[2] / "requirements.txt").read_text()
    declared = [
        line.split("#")[0].strip().lower()
        for line in req.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(d.startswith("authlib") for d in declared), (
        "requirements.txt must declare Authlib>=1.3.2 for st.login() to work"
    )


def test_authlib_is_actually_importable():
    """Catches the deployed failure locally: the package must resolve, not just
    be listed."""
    from streamlit.auth_util import is_authlib_installed

    assert is_authlib_installed(), "Authlib is not installed; st.login() will fail"

def test_requirements_declares_httpx():
    """Authlib's Starlette integration imports httpx, but Authlib declares it
    only as the optional [httpx] extra.

    Locally httpx arrives as a transitive dependency of openai, so login worked
    here while the deployed app returned a bare "Internal server error" from
    /auth/login with ModuleNotFoundError: No module named 'httpx'. Nothing in
    the app's own code imports httpx, which is exactly why it went unnoticed.
    """
    import pathlib

    req = (pathlib.Path(__file__).resolve().parents[2] / "requirements.txt").read_text()
    declared = [
        line.split("#")[0].strip().lower()
        for line in req.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(d.startswith("httpx") for d in declared), (
        "requirements.txt must declare httpx: Authlib imports it during st.login() "
        "and does not install it itself"
    )


def test_the_authlib_starlette_client_actually_imports():
    """The listed-vs-resolvable distinction, at the exact import that failed.

    is_authlib_installed() only checks that `authlib` imports; the deployed
    500 came from authlib.integrations.starlette_client, one layer deeper.
    """
    import importlib

    importlib.import_module("authlib.integrations.starlette_client")
