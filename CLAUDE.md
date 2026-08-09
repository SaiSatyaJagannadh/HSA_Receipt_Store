# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` covers setup, Google Cloud config, deployment, and the data model. Read it before changing anything that touches auth, Drive, or the Sheet schema. This file covers what the README doesn't: the invariants that span files.

## Commands

All commands run from `hsa_vault/`, not the repo root. The venv lives at the repo root, so it's `../.venv/bin/...`.

```sh
cd hsa_vault
../.venv/bin/streamlit run app.py                  # run the app
../.venv/bin/python -m pytest tests -q             # full suite (183, no network)
../.venv/bin/python -m pytest tests/test_ledger.py -q          # one file
../.venv/bin/python -m pytest tests/test_edit_flow.py -q -k provider   # one test
../.venv/bin/python -m scripts.bootstrap_sheet --create        # create the Sheet, grant consent
../.venv/bin/python -m scripts.seed_data                       # sample rows
```

Scripts must run as `python -m scripts.x`, not `python scripts/x.py` — they import `core` absolutely, which needs `hsa_vault/` itself on `sys.path` (as a plain script it's `scripts/` instead, and the import fails). `tests/conftest.py` does the equivalent for tests.

There is no linter or formatter configured. Match the surrounding style.

## Architecture

**Drive holds the files, Sheets holds the index, SQLite is a disposable cache.** This ordering is the whole design, not an implementation detail. Sheets is authoritative; `~/.hsavault/cache.sqlite` is rebuilt from it on every load and exists only so the app degrades to read-only instead of blank when Google is unreachable. Never write a code path that treats SQLite as a source of truth, and never make the Sheet unreadable to a human opening it in a browser.

**Only `store.py` and `auth.py` import Streamlit at module level.** `config.py` imports it lazily inside `_secrets()`. Everything else in `core/` — `ledger`, `models`, `sheets`, `drive`, `extraction`, `pdf_export`, `cache`, `util` — is plain Python with no session state and no UI, which is what makes it directly testable. Keep it that way: if a core module needs something from session state, pass it in as an argument.

**Pages never touch Google clients directly.** Every page goes through `store.py`, which owns client construction (`@st.cache_resource`), the session-state read caches, and cache invalidation on write. A page that imports `SheetsClient` or `DriveClient` is a layering break.

**`ledger.py` is pure functions over lists of receipts.** All balance math lives here and nowhere else. The claimable-balance rule (README → "The balance rule") is what `tests/test_ledger.py` exists to protect — treat a change there as changing the product, not refactoring it.

**`models.py` owns the wire format.** `RECEIPT_COLUMNS`, `TABS`, `CATEGORIES`, and `PAYMENT_METHODS` are the single source of truth for the Sheet's shape; `to_row`/`from_row` are the only serialization boundary. Adding a column means updating `RECEIPT_COLUMNS` and both methods together — the Sheet has to stay self-describing.

### Two payment methods, one balance

`hsa_card` receipts are audit documentation only and never count toward the claimable balance; `out_of_pocket` receipts accumulate into it. Confusing the two causes double-claiming, which is the most expensive bug this app can have. `Receipt.claimable` returns 0 for deleted, hsa_card, or fully-reimbursed receipts. Partial reimbursements leave the remainder claimable.

### Money

`Decimal` everywhere, never float. `models.money()` quantizes to 2dp with `ROUND_HALF_UP` (not Decimal's default banker's rounding) because a half-cent on a receipt should round the way a register does. Amounts land in the Sheet as plain 2-decimal strings.

### Session state

All keys are namespaced `_hsa_*`. This is not cosmetic: a bare `st.session_state["settings"]` collided with `st.form("settings")` and took out the Settings page in production. When adding a key, namespace it, and add it to `_CACHES` in `store.py` if a write should invalidate it.

### Confirmations must outlive the rerun

`st.success()` immediately before `st.rerun()` never reaches the browser — the rerun discards the page mid-render, so a successful save looks identical to a no-op. Use `store.flash(msg)` to queue the message and `store.show_flash()` right after the page title to render it. Seven call sites had this bug — six found at once, then Reimbursements a second time, because its save path needs a `data_editor` selection that `AppTest` cannot drive. `tests/test_flash_contract.py` now enforces the pattern by parsing every page's AST instead: no `st.success()` may sit immediately before an `st.rerun()`, and a page that calls `store.flash()` must also call `store.show_flash()`.

### Untrusted text

Provider names come from a vision model reading a stranger's printout, and the Sheet is hand-editable. Three sinks are already defended and must stay that way: `pdf_export.safe()` escapes before reportlab's mini-XML `Paragraph` parser (`<img src="/etc/passwd"/>` in a provider name would otherwise embed a local file), Sheets writes use `valueInputOption="RAW"` so `=IMPORTXML(...)` stays text, and `models.safe_url()` drops any `drive_link` that isn't http(s).

### Failure handling

`util.retry()` wraps every Google and NVIDIA network call with exponential backoff and jitter. Extraction never raises — no API key, a rate limit, or a model replying with prose all degrade to manual entry, and the app never blocks a save. If a Drive upload succeeds and the Sheets write fails, the file becomes an orphan, detected on next launch and repairable from Bulk Import. Nothing is ever hard-deleted; archiving flags the row and moves the file to `_archive/`.

## Auth

`core/auth.py` is **fail-closed and must stay that way**: Google credentials present with no `[auth]` section, no `[auth.google]` provider, or an empty `allowed_emails` all `st.stop()` rather than serve an open door. Credentials without a gate is the one state that must never serve traffic. `tests/test_auth.py` covers each refusal path.

On Community Cloud's free tier the app URL is **public and reachable by anyone**, and repo visibility does not change that. `require_login()` is therefore the only thing protecting the data — treat removing it from a page, even temporarily, as publishing that page to the internet.

Locally there is no login screen at all, which is why `.streamlit/config.toml` binds to `127.0.0.1` — Streamlit's own default is `0.0.0.0`, which would publish full read/write access to your Drive to the local network.

Auth is OAuth-as-you, not a service account: service accounts have no Drive storage on personal accounts (`403 storageQuotaExceeded`, verified against the live API). A service account key is still detected and used if you point at one, for Workspace + Shared Drive setups.

`st.login()` requires **Authlib**, which is not `google-auth-oauthlib`. Removing it from `requirements.txt` breaks login in a way that looks like a provider-config error.

## Testing

Two layers, and the split matters. Unit tests cover `core/`; `tests/test_pages.py` and `tests/test_edit_flow.py` render real pages through `streamlit.testing.v1.AppTest`.

The page layer exists because three bugs reached production while every unit test passed: a slider that collapsed when `min == max` on a single-receipt vault, a session_state/form key collision, and a save whose confirmation was erased by `st.rerun()`. All were invisible until something actually rendered a page.

**Patch the loader, not the accessor.** Patching `store.settings()` meant the real one never ran, so it never wrote session state — which is exactly how the key collision slipped through. Patch `config.load_settings` instead. Same reasoning applies generally: a fixture that replaces the function under test hides the bug you're trying to catch.

`AppTest.from_file()` needs an absolute path (see `test_edit_flow.py`), otherwise it resolves against the working directory and errors.

Test the data states a real vault passes through, not just the happy path: empty on day one, exactly one receipt, several identical amounts, then a mixed set. The first three are where the crashes were.

**Verify a regression test by reverting the fix and confirming it fails.** This is established practice here — it's what distinguished "the save is broken" from "the confirmation is invisible" on the edit bug, where the save tests passed with the bug present and only the confirmation tests failed.

## Secrets

`.env`, `credentials.json`, `service_account.json`, `token.json`, and `**/secrets.toml` are gitignored and must never be committed — verify with `git check-ignore` rather than assuming. Never echo a secret value into the transcript.

Settings precedence is `.env` < `st.secrets[hsa]` < `~/.hsavault/settings.json`. The Settings page writes the last one; your `.env` is never modified.

Don't run `export_deploy_secrets --write` and then start the app locally — the written `secrets.toml` makes the local run look deployed, and the fail-closed gate blocks it.

## Deployment

This repo is source-only; no deployed instance is referenced here, and none should be added. If someone deploys it, the target is Streamlit Community Cloud from `main`, which hot-reloads on push. A hosted instance runs on a refresh token minted locally, since a server has no browser for the consent flow.

Rapid consecutive pushes can trigger hot-reloads mid-import and segfault the app; the fix is Manage app → Reboot, not a code change. Before diagnosing a deploy failure as a code defect, confirm the modules import and the tests pass locally.
