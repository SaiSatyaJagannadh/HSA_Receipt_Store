# HSAVault

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.x-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![Tests](https://img.shields.io/badge/tests-208%20passing-3fb950)](#tests)
[![No network in tests](https://img.shields.io/badge/test%20suite-no%20network-8957e5)](#tests)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**A personal HSA receipt vault built so that deleting the app costs you nothing.**

Your receipts live permanently in **your own Google Drive**, in a plain folder
structure you can navigate without this app. A Google Sheet is the index. This
application is a reporting layer over both.

If you delete this app tomorrow, your records remain complete, organized, and
usable by a human or a CPA.

> **Why this exists.** An HSA lets you pay a medical expense out of pocket today
> and reimburse yourself from the account *years* later — but only if you can
> still produce the receipt. That turns a shoebox of thermal paper into a
> multi-decade record-keeping problem, and it makes one distinction expensive to
> get wrong: a receipt already paid with the HSA card must never be claimed
> again. The whole design falls out of those two facts.

```
HSA_Vault/                                 <- your Drive folder
  2026/
    2026-03-14__CVS_Pharmacy__42.18__a1b2c3d4.jpg
  2027/
  _exports/
    HSA_Audit_Packet_2026.pdf
  _archive/                                <- soft-deleted receipts
```

**This is not tax advice.** Categories follow IRS-published expense categories.
Confirm your own eligibility with a qualified tax professional.

---

## What it does

- **Upload** receipts (JPG/PNG/HEIC/PDF). Originals go to Drive **unmodified**;
  only a downscaled copy is sent to the vision model for extraction.
- **Extract** provider, date, amount, category, and line items with an NVIDIA NIM
  vision model, returning `null` rather than guessing, and listing what was unclear.
- **Confirm** every extraction in a form before anything is written. Nothing
  auto-commits.
- **Track** two kinds of receipt separately, because confusing them causes
  double-claiming:
  - `hsa_card` — paid at the register with the HSA card. Audit documentation
    only. Never counts toward the claimable balance.
  - `out_of_pocket` — paid with other money. Accumulates into a balance you can
    reimburse yourself for later, even years later.
- **Reimburse** yourself: select receipts, record the withdrawal, support partial
  amounts. HSA-card receipts never appear in the selection list.
- **Archive and restore.** Deleting is a soft delete: the row is flagged and the
  file moves to `_archive/`. Nothing is ever hard-deleted from Drive, and a
  mis-click is one click to undo.
- **Export** a self-contained audit packet PDF per tax year — cover page, summary
  table, category subtotals, and one page per receipt with the image and its
  metadata printed beneath it. Legible in black and white. Plus CSV and a ZIP of
  the raw images.
- **Repair** itself: rebuild the local cache from Sheets, and detect Drive files
  that have no index row (a Drive upload that succeeded when the Sheets write
  failed).

---

## Google Cloud setup

You authenticate as **yourself** over OAuth. Files created by the app are owned
by you, which is the point: your records have to outlive both this app and the
Cloud project it was built in.

> **Why not a service account?** The original design used one. It cannot work on
> a personal Google account: a service account has no Drive storage of its own,
> so every file it creates fails with `403 storageQuotaExceeded` — verified
> against the live API. The usual fix is to put the files in a Shared Drive, but
> Shared Drives are a Google Workspace feature. If you *are* on Workspace, point
> `GOOGLE_CREDENTIALS_JSON` at a service account key instead and it will be
> detected and used automatically.

### 1. Create a project

1. Go to <https://console.cloud.google.com/projectcreate>.
2. Name it `hsa-vault`. Click **Create**, then select it in the top bar.

### 2. Enable the two APIs

1. <https://console.cloud.google.com/apis/library/drive.googleapis.com> → **Enable**
2. <https://console.cloud.google.com/apis/library/sheets.googleapis.com> → **Enable**

### 3. Configure the OAuth consent screen

Under **Google Auth Platform → Overview → Get started**:

1. **App name** `HSAVault`, user support email = your address.
2. **Audience** → **External**. (Internal needs Workspace.)
3. **Contact information** → your address.
4. Agree to the API services user data policy → **Create**.

Then **Audience → Test users → Add users** and add your own Gmail address.
Without this, consent is refused with `access_denied`.

> **Testing vs production.** While the app is in *Testing*, refresh tokens expire
> after **7 days** and you re-consent weekly. To stop that, hit **Publish app**
> on the Audience page. Publishing an unverified app with the Drive scope shows a
> "Google hasn't verified this app" interstitial at consent — click **Advanced →
> Go to HSAVault (unsafe)**. That warning is expected: the "app" is this code
> running on your own machine, and no one else can reach it.

### 4. Create a Desktop OAuth client

**Clients → Create client → Application type: Desktop app**, name it
`HSAVault desktop`, **Create**, then **Download JSON**.

Save it next to this README as `credentials.json`:

```sh
mv ~/Downloads/client_secret_*.json ./credentials.json
chmod 600 credentials.json
```

It is gitignored. A desktop-app client secret is not treated as confidential by
OAuth (it cannot be kept secret in a distributed binary), but the access token it
mints is — that lands in `~/.hsavault/token.json`, mode 600.

### 5. Create the Drive folder

Create a folder named `HSA_Vault` in your Drive. No sharing needed — you own it
and you are the one authenticating. Copy its ID from the URL:

```
https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
                                       ^^^^^^^^^^^^^^^^^^^^^^^^^^ this
```

### 6. First run grants consent

`scripts/bootstrap_sheet.py --create` opens a browser once, you click **Allow**,
and the token is cached. Everything after that is silent until the token is
revoked. Run it from a terminal, not from inside Streamlit.

### 7. Revoking access

<https://myaccount.google.com/permissions> → HSAVault → Remove access. Then
`rm ~/.hsavault/token.json`.

If the app shows **"Google sign-in has expired"** and the underlying error is
`invalid_grant: Token has been expired or revoked`, the refresh token is dead —
Google was reached and refused it, so retrying never helps. Re-run
`python -m scripts.bootstrap_sheet` to mint a new one (and
`python -m scripts.export_deploy_secrets` if a hosted copy is running on it).
If it dies again about a week later, the consent screen is still in *Testing* —
see the note in step 3. Publishing is the fix; re-minting only restarts the
7-day clock.

### 8. NVIDIA API key

Get one at <https://build.nvidia.com> — open any model and click **Get API Key**.
It starts with `nvapi-`. Optional: leave it blank and the app still works with
fully manual entry.

Extraction goes through NVIDIA's OpenAI-compatible endpoint
(`https://integrate.api.nvidia.com/v1`), so the model is a config value rather
than a code change. Free-endpoint vision models that work here:

| Model | Notes |
|---|---|
| `meta/llama-3.2-90b-vision-instruct` | Default. Strongest general reading. |
| `meta/llama-3.2-11b-vision-instruct` | Faster and cheaper, less accurate on faint print. |
| `nvidia/nemotron-nano-12b-v2-vl` | Multi-image and document Q&A. |
| `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` | Tuned for document intelligence. |

Point `NVIDIA_BASE_URL` at your own host to run a self-hosted NIM container
instead.

---

## Running

```sh
# 1. Install
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configure
cp .env.example .env
$EDITOR .env          # paste the Drive folder ID and (optionally) the NVIDIA key

cd hsa_vault

# 3. Grant consent and create the Sheet. Opens a browser once; click Allow.
#    Prints the new sheet ID — paste it into .env as HSA_SHEET_ID.
../.venv/bin/python -m scripts.bootstrap_sheet --create

# 4. (Optional) Sample data so the UI isn't empty
../.venv/bin/python -m scripts.seed_data

# 5. Run
../.venv/bin/streamlit run app.py
```

Everything in `.env` is also editable in the app's **Settings** page, which
writes `~/.hsavault/settings.json`. That file wins over `.env`; your `.env` is
never modified.

### Security posture

**HSAVault has no login screen.** Whoever reaches the port gets full read/write
on your records and, through the cached token, on your Google Drive.
`.streamlit/config.toml` therefore binds the server to `127.0.0.1` — Streamlit's
own default is `0.0.0.0`, which would publish all of that to your local network.

### Deploying

This repository is the source only — no instance is published here. If you deploy
your own to Streamlit Community Cloud, read this section first, because the
hosting model is not what most people assume.

**The URL is public and reachable by anyone.** Community Cloud on the free tier
only publishes public apps — private ones need a Snowflake plan. Repo visibility
does not gate the app either: the repo can be public or private and the app URL
stays open to the internet. Assume the URL is known.

So exactly one thing stands between a passer-by and your medical expense history
(plus, through the refresh token in secrets, write access to your Drive):

- **In-app Google login** (`core/auth.py`), called at the top of all 8 pages
  before anything renders, and restricted to `allowed_emails`. An anonymous
  visitor gets a sign-in button and `st.stop()` — no receipt is ever read,
  computed, or sent.
- **Fail-closed**, because that gate is load-bearing and alone. If `google_token`
  is in secrets but `[auth]` is missing, or `[auth.google]` is absent, or
  `allowed_emails` is empty, the app refuses to start rather than serving an open
  door. Credentials without a gate is the one state that must never serve
  traffic — `tests/test_auth.py` covers each refusal path.

Verify after any deploy: load the app in a logged-out browser and confirm you get
the sign-in screen rather than the dashboard. Never remove `require_login()` from
a page "temporarily" — on this hosting that publishes the page to everyone.

To redeploy from scratch:

```sh
# 1. A Web OAuth client in the same GCP project, redirect URI:
#    https://<your-subdomain>.streamlit.app/oauth2callback

# 2. Generate the secrets block (never commit it)
cd hsa_vault
../.venv/bin/python -m scripts.export_deploy_secrets \
  --web-client ~/Downloads/client_secret_*.json \
  --allowed-emails you@gmail.com \
  --app-url https://<your-subdomain>.streamlit.app

# 3. share.streamlit.io -> Deploy, main file hsa_vault/app.py, Python 3.12,
#    paste the block into Advanced settings -> Secrets. The subdomain MUST
#    match the redirect URI exactly or login fails.
```

The hosted app runs on the refresh token you granted locally — a server has no
browser for the consent flow. `packages.txt` installs poppler-utils so PDF
rasterization works there too.

> Do **not** run `export_deploy_secrets --write` and then start the app locally:
> the written `secrets.toml` makes the local run look deployed, so the
> fail-closed gate blocks it.

### Optional system dependency

Multi-page PDFs are rasterized into per-page images using `poppler`. Vision
models take images only, so without it a PDF upload still saves to Drive but
extraction is skipped and you fill the fields in by hand.

```sh
brew install poppler        # macOS
```

---

## Data model

The Sheet has three tabs. Both this app and a human can read them.

**`receipts`** — `receipt_id`, `file_hash`, `drive_file_id`, `drive_link`,
`service_date`, `upload_date`, `provider`, `amount`, `category`, `description`,
`payment_method`, `reimbursed`, `reimbursement_date`, `reimbursement_amount`,
`patient`, `tax_year`, `eligibility_confidence`, `notes`, `extraction_raw`,
`deleted`, `edit_history`

> `deleted` and `edit_history` are additions to the spec: soft delete and the
> per-receipt audit trail need somewhere to live, and the Sheet has to stay
> self-describing.

**`reimbursements`** — `reimbursement_id`, `date`, `amount`, `method`,
`covered_receipt_ids`, `notes`. One withdrawal may cover several receipts.

**`contributions`** — `date`, `amount`, `source`, `tax_year`.

Amounts are stored as plain 2-decimal strings and handled as `Decimal` in Python.
No float ever touches a dollar amount.

### The balance rule

```
unreimbursed balance = sum, over receipts where
    payment_method == out_of_pocket
    AND not reimbursed
    AND not deleted
  of (amount - already-reimbursed amount)
```

A partial reimbursement leaves the remainder claimable. This is the number the
tests exist to protect — see `tests/test_ledger.py`.

---

## Durability

- **Drive is the record.** Original bytes, never re-encoded, in dated folders
  with human-readable filenames.
- **Sheets is the index.** Authoritative. Readable and editable without this app.
- **SQLite is a cache.** `~/.hsavault/cache.sqlite` is rebuilt from Sheets on
  every load and is never authoritative. Delete it freely.
- **`~/.hsavault/audit.log`** is append-only JSONL: every Drive upload, Sheets
  write, retry, and archive.
- **Nothing is hard-deleted.** Archiving flags the row and moves the file to
  `_archive/`.
- **Duplicates are blocked by SHA-256**, including against archived receipts.
- **Retries** with exponential backoff wrap every Google and NVIDIA call.
- **Untrusted text is escaped**, not trusted. Provider names come from a model
  reading a stranger's printout; they are escaped before reaching the PDF's
  markup parser, stored with `valueInputOption=RAW` so `=IMPORTXML(...)` is text
  rather than a live formula, and `drive_link` is dropped unless it is http(s).
- **Graceful degradation.** No API key, a rate limit, an outage, or a model that
  replies with prose instead of JSON — extraction fails softly and manual entry
  always works. The app never blocks a save.
- **Orphan detection.** If a Drive upload succeeds and the Sheets write fails,
  the file is flagged on next launch and repairable from **Bulk Import**.

---

## Tests

```sh
cd hsa_vault
../.venv/bin/python -m pytest tests -q
```

**208 tests, no network.** Every Google and model call is mocked, so the suite
runs offline and deterministically.

| File | Tests | Covers |
|---|---:|---|
| `test_ledger.py` | 34 | The balance math: HSA-card exclusion, partial reimbursements, multi-receipt withdrawals, tax-year boundaries, duplicate rejection, amount-filter bounds. |
| `test_pages.py` | 34 | Renders all 8 pages through Streamlit's `AppTest` in four data states: empty vault, one receipt, several identical amounts, a mixed set. Also pins that an expired sign-in is not reported as a network blip. |
| `test_extraction_parsing.py` | 25 | Mocks the OpenAI SDK entirely; tolerant reply parsing — fenced JSON, chatty preambles, invented categories, `$1,042.18` in the amount field. |
| `test_models.py` | 22 | Row (de)serialization, validation, `Decimal` money quantization. |
| `test_edit_flow.py` | 21 | Every editable field on the receipt form saves **and** confirms itself. |
| `test_pdf_export.py` | 19 | reportlab markup injection and audit-packet contents. |
| `test_flash_contract.py` | 18 | Static guards: the confirmation-after-rerun bug (below), and that every page reading receipts also warns when they are stale. |
| `test_auth.py` | 14 | The fail-closed login gate, plus the Authlib and httpx dependencies `st.login()` requires. |
| `test_archive_flow.py` | 9 | Archive → restore round trip, and that the money comes back with it. |
| `test_upload_flow.py` | 6 | The uploader empties itself once a batch is filed, and not before. |
| `test_reimbursement_write_order.py` | 6 | A withdrawal is on record before any receipt is marked paid by it. |

### Why there is a page-rendering layer at all

Three bugs reached production while every unit test passed: a slider that
collapsed when `min == max` on a single-receipt vault, a `session_state` key that
collided with a form key, and a save whose confirmation was erased by the
`st.rerun()` that followed it. All three were invisible until something actually
rendered a page — and two were hidden further by fixtures that patched the very
function under test. **Patch the _loader_, not the accessor.**

The last of those three came back. `st.success()` immediately before `st.rerun()`
never reaches the browser: the rerun discards the page mid-render, so a
successful save looks identical to a no-op. It was fixed at six call sites, then
reappeared at a seventh — on the reimbursements page, where a silent write is
most expensive, and which `AppTest` cannot reach because that path requires a
`data_editor` row selection.

So `test_flash_contract.py` stopped testing behaviour and started testing shape:
it parses every page's AST and fails the build if any `st.success()` sits
immediately before an `st.rerun()`, or if a page queues a confirmation without
rendering one. Verified the only way a regression test is worth anything — by
reverting the fix and confirming it failed on exactly that call site.

---

## Layout

```
hsa_vault/
  app.py                  Dashboard
  .streamlit/config.toml  Binds to loopback; disables telemetry
  pages/                  Upload, Receipts, Reimbursements, Contributions,
                          Export, Bulk Import, Settings
  core/
    auth.py               Fail-closed login gate for hosted deployments
    config.py             .env + settings.json, Google credentials
    models.py             Dataclasses, validation, row (de)serialization
    ledger.py             All balance math. Pure functions.
    drive.py              Folder tree, upload, move, download
    sheets.py             Tab bootstrap, read, append, upsert
    extraction.py         Normalization + NVIDIA NIM vision
    cache.py              SQLite mirror
    pdf_export.py         Audit packet, CSV, ZIP
    store.py              App-facing layer over the above
    util.py               Hashing, slugs, retry, audit log
  scripts/                bootstrap_sheet.py, seed_data.py,
                          export_deploy_secrets.py
  tests/                  183 tests, no network
```

Two rules hold this shape together:

- **Only `store.py` and `auth.py` import Streamlit at module level.** Everything
  else in `core/` is plain Python with no session state and no UI, which is what
  makes it directly testable.
- **Pages never touch Google clients.** Every page goes through `store.py`, which
  owns client construction, the read caches, and cache invalidation on write.

## Where the build diverged from the spec

The original specification is checked in at
[`hsa_vault_build_prompt.md`](hsa_vault_build_prompt.md). Two of its decisions
did not survive contact with the actual APIs, and the reasoning is worth more
than the spec was:

**Auth: service account → OAuth-as-you.** The spec called for a Google service
account with the Drive folder shared to it. That cannot work on a personal Google
account — a service account has no Drive storage of its own, so every file it
creates fails with `403 storageQuotaExceeded`, verified against the live API. The
documented workaround is a Shared Drive, which is a Workspace-only feature. The
app now authenticates as *you*, so the files are owned by you, which serves the
core principle better than the spec's own mechanism did. A service account key is
still detected and used if you point at one, for Workspace setups.

**Extraction: a single named model → any OpenAI-compatible vision endpoint.**
Extraction runs on NVIDIA NIM. Because NIM models honour `response_format`
inconsistently, the JSON contract is stated in the prompt and the reply is parsed
defensively — markdown fences, a chatty preamble, an invented category, or
`$1,042.18` in the amount field are all handled rather than trusted. Swap models
with `NVIDIA_MODEL`, or point `NVIDIA_BASE_URL` at a self-hosted container;
nothing else in the codebase knows which model is in use.

The general lesson both share: extraction is the least trustworthy part of the
system, so no extracted value is ever written without a human confirming it, and
the app degrades to manual entry rather than blocking a save.
