"""The login gate must fail closed.

A deployed HSAVault holds a refresh token with write access to the owner's Drive,
on a public URL. The one combination that must never serve traffic is
"credentials present, gate absent".
"""

import sys
import types

import pytest


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
    sys.modules.pop("core.auth", None)
    mod._calls = calls
    return mod


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
