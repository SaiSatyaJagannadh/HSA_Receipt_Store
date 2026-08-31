# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` covers setup, Google Cloud config, deployment, and the data model. Read it before changing anything that touches auth, Drive, or the Sheet schema. This file covers what the README doesn't: the invariants that span files.

## Commands

Two projects live here: the Streamlit app in `hsa_vault/` and a SwiftUI client in `ios/`. Neither shares code with the other — see "One balance rule, two languages" below for the one thing they do share.

Python commands run from `hsa_vault/`, not the repo root. The venv lives at the repo root, so it's `../.venv/bin/...`.

```sh
cd hsa_vault
../.venv/bin/streamlit run app.py                  # run the app
../.venv/bin/python -m pytest tests -q             # full suite (275, no network)
../.venv/bin/python -m pytest tests/test_ledger.py -q          # one file
../.venv/bin/python -m pytest tests/test_edit_flow.py -q -k provider   # one test
../.venv/bin/python -m scripts.bootstrap_sheet --create        # create the Sheet, grant consent
../.venv/bin/python -m scripts.seed_data                       # sample rows
```

Scripts must run as `python -m scripts.x`, not `python scripts/x.py` — they import `core` absolutely, which needs `hsa_vault/` itself on `sys.path` (as a plain script it's `scripts/` instead, and the import fails). `tests/conftest.py` does the equivalent for tests.

Swift commands run from `ios/HSAVaultCore/`:

```sh
cd ios/HSAVaultCore
swift build                    # compiles the package, SwiftUI views included
swift run HSAVaultCoreChecks   # the balance-rule checks
```

There is no Xcode on this machine, so no iOS SDK and no simulator. Building for the macOS host is deliberate and is what makes the view code verifiable rather than merely written — SwiftUI is identical on both platforms for everything used here. `HSAVaultCoreChecks` is an executable target with `assert`s, not XCTest, so it needs no Xcode at all; run it after any change to `Ledger.swift`.

There is no linter or formatter configured for either project. Match the surrounding style.

## Architecture

**Drive holds the files, Sheets holds the index, SQLite is a disposable cache.** This ordering is the whole design, not an implementation detail. Sheets is authoritative; `~/.hsavault/cache.sqlite` is rebuilt from it on every load and exists only so the app degrades to read-only instead of blank when Google is unreachable. Never write a code path that treats SQLite as a source of truth, and never make the Sheet unreadable to a human opening it in a browser.

**Only `store.py` and `auth.py` import Streamlit at module level.** `config.py` imports it lazily inside `_secrets()`. Everything else in `core/` — `ledger`, `models`, `sheets`, `drive`, `extraction`, `pdf_export`, `cache`, `util` — is plain Python with no session state and no UI, which is what makes it directly testable. Keep it that way: if a core module needs something from session state, pass it in as an argument.

**Pages never touch Google clients directly.** Every page goes through `store.py`, which owns client construction (`@st.cache_resource`), the session-state read caches, and cache invalidation on write. A page that imports `SheetsClient` or `DriveClient` is a layering break.

**`ledger.py` is pure functions over lists of receipts.** All balance math lives here and nowhere else. The claimable-balance rule (README → "The balance rule") is what `tests/test_ledger.py` exists to protect — treat a change there as changing the product, not refactoring it.

**`models.py` owns the wire format.** `RECEIPT_COLUMNS`, `TABS`, `CATEGORIES`, and `PAYMENT_METHODS` are the single source of truth for the Sheet's shape; `to_row`/`from_row` are the only serialization boundary. Adding a column means updating `RECEIPT_COLUMNS` and both methods together — the Sheet has to stay self-describing.

New columns go on the **end**, never inserted. `read_tab` maps values positionally from `RECEIPT_COLUMNS` and pads short rows, so an existing row simply reads back `""` for a column added later and no migration is needed. The Sheet's own header row is cosmetic — nothing reads it — but it is what a human sees, so run `python -m scripts.bootstrap_sheet` once after adding a column to rewrite the headers. `extra_file_ids` (the extra pages of a receipt photographed in parts) was added this way.

**The SQLite cache is dropped, not migrated.** `CREATE TABLE IF NOT EXISTS` keeps an outdated table, so adding a column left every insert failing with `table receipts has 21 columns but 22 values were supplied` — and because that raised inside `store.receipts()`, the app reported Sheets as unreachable and served stale rows, hiding a receipt that had saved perfectly. `cache._match_schema` now compares `PRAGMA table_info` against `RECEIPT_COLUMNS` (order included, since inserts are positional) and recreates the table on any difference. Relatedly, `cache.rebuild` is called outside that try block: Sheets has already answered by then, so a failure writing the disposable mirror must never discard live rows or claim the app is offline.

**A receipt can own more than one Drive file.** `drive_file_id` is page one; `extra_file_ids` is the rest. `store.detach_page` removes one page: it archives the file rather than deleting it, and if the page removed is `drive_file_id` it promotes the next one and refreshes `drive_link`, because that field backs the Drive link, the packet's first image and the ZIP filename. Detaching the last remaining page raises `store.LastPage` — a receipt with no document is the one state this vault exists to prevent, and archiving the receipt is the operation actually intended. Anything that touches a receipt's files must use `store._all_file_ids`, not `drive_file_id` alone — `archive_receipt` and `restore_receipt` would otherwise strand half the pages in the wrong folder, and `find_orphans` would report page two as unindexed on every launch, forever.

### A hand-entered receipt may have no file, and that is deliberate

`pages/2_Manual_Entry.py` takes typed values with no extraction call. A document is
optional: attach one and it goes through `store.commit_receipt` exactly as an upload
does, save without one and `store.save_receipt` writes the row alone. That is the one
sanctioned way to create a row with an empty `drive_file_id`, and it does not weaken
`store.detach_page`'s refusal to strip a receipt down to nothing — that guard is about
not *destroying* the only evidence there is.

**The hash depends on which route was taken, and must.** With files in hand the receipt
is keyed by `sha256` of the bytes, the same as Upload, so the identical photo cannot be
filed once from each page. With nothing attached it falls back to a synthesized
`manual|service_date|provider|amount|patient` digest rather than a blank: `file_hash` is
required by `Receipt.validate()` and is what `ledger.is_duplicate` matches on, so a blank
would silently opt hand-entry out of duplicate detection — on exactly the route
duplicates come from, since typing the same expense twice leaves no identical bytes to
catch.

**`store.attach_pages` promotes the first file attached to a receipt that has none** into
`drive_file_id`/`drive_link` instead of filing it under `extra_file_ids`. Those two fields
back the Drive link, the packet's first image, the ZIP filename, and `ledger.packet_gaps`'
"no image" check, so the original would otherwise upload and the receipt would still read
as undocumented.

**Provider, patient and a non-zero amount are required here and nowhere else.** The
checks live on the page, not in `Receipt.validate()`: the Sheet is hand-editable and
rows already exist with a blank provider — an extraction that could not read one saves
that way by design — so tightening the shared validator would make those rows unsavable
from the Receipts editor, breaking editing for the receipts most in need of it. The
amount is an `st.number_input` that opens at `0.00`, which is why 0.00 is refused: a
defaulted figure is only safe when forgetting to change it cannot file a wrong number.
`money()` still runs on the way out, so the float the widget returns never reaches the
Sheet. `tests/test_manual_entry.py` pins all of it.

### One balance rule, two languages

`ios/` is a native SwiftUI client, currently screens plus the balance rule with no Google auth behind them yet. Nothing in `hsa_vault/` ports — Streamlit is server-rendered Python with no iOS target, so every page is throwaway. What crosses the boundary is the **contract**, and only one part of it is dangerous to approximate.

`ios/.../Sources/HSAVaultCore/Ledger.swift` is a hand port of `core/ledger.py` plus `Receipt.claimable` from `core/models.py`, and `Sources/HSAVaultCoreChecks/main.swift` mirrors `tests/test_ledger.py` case for case. **Changing the balance rule means changing it in both places in the same commit.** A client that disagrees with the Python app about the claimable balance does not look broken — it shows a confident number for the same Sheet, and the difference between the two is medical dollars claimed from the HSA twice. That is the same failure as confusing `hsa_card` with `out_of_pocket`, reached from a different direction.

The port keeps the money rules too: `Decimal` with `NSDecimalRound(..., 2, .plain)`, Foundation's equivalent of `money()`'s ROUND_HALF_UP. Never `Double` on either side.

### Two payment methods, one balance

`hsa_card` receipts are audit documentation only and never count toward the claimable balance; `out_of_pocket` receipts accumulate into it. Confusing the two causes double-claiming, which is the most expensive bug this app can have. `Receipt.claimable` returns 0 for deleted, hsa_card, or fully-reimbursed receipts. Partial reimbursements leave the remainder claimable.

**Recording a withdrawal is N+1 writes with no transaction, so the order is load-bearing.** `store.record_reimbursement` writes the `reimbursements` row *before* marking any receipt, and it is the only thing that may do this — a page must not hand-roll the loop. If a write fails part-way, the money is on record and some receipts are unmarked, so the balance errs **high** and the gap shows in the withdrawal history. The reverse order leaves receipts marked with no record of where the money went, which is how the same dollars get claimed twice. `apply_allocation` mutates in place, so a failed save also rolls the receipt back and calls `clear()`; otherwise the session cache would serve an in-memory receipt that claims to be reimbursed when the Sheet says it is not. `tests/test_reimbursement_write_order.py` pins all of this.

### Money

`Decimal` everywhere, never float. `models.money()` quantizes to 2dp with `ROUND_HALF_UP` (not Decimal's default banker's rounding) because a half-cent on a receipt should round the way a register does. Amounts land in the Sheet as plain 2-decimal strings.

### Session state

All keys are namespaced `_hsa_*`. This is not cosmetic: a bare `st.session_state["settings"]` collided with `st.form("settings")` and took out the Settings page in production. When adding a key, namespace it, and add it to `_CACHES` in `store.py` if a write should invalidate it.

### Confirmations must outlive the rerun

`st.success()` immediately before `st.rerun()` never reaches the browser — the rerun discards the page mid-render, so a successful save looks identical to a no-op. Use `store.flash(msg)` to queue the message and `store.show_flash()` right after the page title to render it. Seven call sites had this bug — six found at once, then Reimbursements a second time, because its save path needs a `data_editor` selection that `AppTest` cannot drive. `tests/test_flash_contract.py` now enforces the pattern by parsing every page's AST instead: no `st.success()` may sit immediately before an `st.rerun()`, and a page that calls `store.flash()` must also call `store.show_flash()`.

### Untrusted text

Provider names come from a vision model reading a stranger's printout, and the Sheet is hand-editable. Four sinks are already defended and must stay that way: `assistant.build_context` fences every receipt row between `assistant.FENCE` markers and the system prompt tells the model that region is data and must never be obeyed (a provider named "ignore previous instructions..." is a realistic input, not a hypothetical), and pipes are stripped from provider and patient so a name cannot forge a column in the delimited rows; `pdf_export.safe()` escapes before reportlab's mini-XML `Paragraph` parser (`<img src="/etc/passwd"/>` in a provider name would otherwise embed a local file), Sheets writes use `valueInputOption="RAW"` so `=IMPORTXML(...)` stays text, and `models.safe_url()` drops any `drive_link` that isn't http(s).

### Failure handling

**Retry budgets multiply, they do not add.** The openai SDK defaults to a 600s timeout *and* its own `max_retries=2`; wrapped in `retry()`, one extraction became up to twelve requests of up to ten minutes each, which presented as the Upload page hanging on "Reading the receipt…" — measured at over six minutes for a single 150KB image. `extraction.extract` now passes `timeout=REQUEST_TIMEOUT, max_retries=0` so `retry()` alone owns retries, and the worst case is `ATTEMPTS × REQUEST_TIMEOUT`. Any client wrapped in `retry()` must disable that client's own retries; `tests/test_extraction_parsing.py` pins both.

`util.retry()` wraps every Google and NVIDIA network call with exponential backoff and jitter. Extraction never raises — no API key, a rate limit, or a model replying with prose all degrade to manual entry, and the app never blocks a save. If a Drive upload succeeds and the Sheets write fails, the file becomes an orphan, detected on next launch and repairable from Bulk Import. Nothing is ever hard-deleted; archiving flags the row and moves the file to `_archive/`, and `store.restore_receipt` is the exact inverse — it must keep putting the file back in the same folder `commit_receipt` would have chosen, which is why both go through `_home_folder`.

## Auth

`core/auth.py` is **fail-closed and must stay that way**: Google credentials present with no `[auth]` section, no `[auth.google]` provider, or an empty `allowed_emails` all `st.stop()` rather than serve an open door. Credentials without a gate is the one state that must never serve traffic. `tests/test_auth.py` covers each refusal path.

On Community Cloud's free tier the app URL is **public and reachable by anyone**, and repo visibility does not change that. `require_login()` is therefore the only thing protecting the data — treat removing it from a page, even temporarily, as publishing that page to the internet.

Locally there is no login screen at all, which is why `.streamlit/config.toml` binds to `127.0.0.1` — Streamlit's own default is `0.0.0.0`, which would publish full read/write access to your Drive to the local network.

Auth is OAuth-as-you, not a service account: service accounts have no Drive storage on personal accounts (`403 storageQuotaExceeded`, verified against the live API). A service account key is still detected and used if you point at one, for Workspace + Shared Drive setups.

`st.login()` requires **Authlib**, which is not `google-auth-oauthlib`. Removing it from `requirements.txt` breaks login in a way that looks like a provider-config error.

It also requires **httpx**, one layer deeper and easier to miss: `authlib.integrations.starlette_client` imports it, but Authlib declares it only as the optional `[httpx]` extra. Locally it arrives free as a transitive dependency of `openai`, so login works on your machine and the deployed app answers `/auth/login` with a bare `Internal server error` — the traceback ends in `ModuleNotFoundError: No module named 'httpx'`, visible only in the Cloud logs. Nothing in this codebase imports httpx directly, which is why nothing caught it. Both are pinned in `requirements.txt` and both are covered in `tests/test_auth.py`; a "listed" test is not enough, so there is also a test that actually imports `authlib.integrations.starlette_client`.

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
