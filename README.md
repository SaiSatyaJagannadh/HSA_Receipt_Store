# HSAVault

A personal HSA receipt vault. Your receipts live permanently in **your own Google
Drive**, in a plain folder structure you can navigate without this app. A Google
Sheet is the index. This application is a reporting layer over both.

If you delete this app tomorrow, your records remain complete, organized, and
usable by a human or a CPA.

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
- **Export** a self-contained audit packet PDF per tax year — cover page, summary
  table, category subtotals, and one page per receipt with the image and its
  metadata printed beneath it. Legible in black and white. Plus CSV and a ZIP of
  the raw images.
- **Repair** itself: rebuild the local cache from Sheets, and detect Drive files
  that have no index row (a Drive upload that succeeded when the Sheets write
  failed).

---

## Google Cloud setup

You need a service account. It has its own email address; you share your Drive
folder and Sheet with it, exactly as you would with a colleague. There is no
OAuth consent screen and no token to refresh.

### 1. Create a project

1. Go to <https://console.cloud.google.com/projectcreate>.
2. Name it something like `hsa-vault`. Click **Create**.
3. Make sure it is the selected project in the top bar before continuing.

### 2. Enable the two APIs

1. <https://console.cloud.google.com/apis/library/drive.googleapis.com> →
   **Enable**.
2. <https://console.cloud.google.com/apis/library/sheets.googleapis.com> →
   **Enable**.

### 3. Create the service account

1. Go to <https://console.cloud.google.com/iam-admin/serviceaccounts> →
   **+ Create service account**.
2. Name: `hsa-vault`. Click **Create and continue**.
3. Skip the optional role and user-access steps — **Done**.

### 4. Download the JSON key

1. Click the service account you just made → **Keys** tab.
2. **Add key → Create new key → JSON → Create**. A `.json` file downloads.
3. Move it next to this README as `service_account.json`.

   ```sh
   mv ~/Downloads/hsa-vault-*.json ./service_account.json
   chmod 600 service_account.json
   ```

   This file is a credential. It is already covered by `.gitignore` — keep it
   out of version control and out of backups you share.

### 5. Copy the service account email

On the service account's **Details** tab, copy the email. It looks like:

```
hsa-vault@hsa-vault-123456.iam.gserviceaccount.com
```

### 6. Create and share the Drive folder

1. In Google Drive, create a folder named `HSA_Vault`.
2. Right-click it → **Share**, paste the service account email, set it to
   **Editor**, uncheck "Notify people", **Share**.
3. Open the folder and copy its ID from the URL:

   ```
   https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
                                          ^^^^^^^^^^^^^^^^^^^^^^^^^^ this
   ```

### 7. Create and share the Sheet

You can let the bootstrap script create it (step 3 of Running below), or do it
by hand:

1. Create a Google Sheet named `HSA Vault Index`.
2. Share it with the same service account email as **Editor**.
3. Copy its ID from the URL:

   ```
   https://docs.google.com/spreadsheets/d/1XyZ.../edit
                                         ^^^^^^^ this
   ```

> **If you get a 403 or "file not found"**, the cause is almost always that the
> folder or sheet was not shared with the service account email. Re-check step 6
> and step 7.

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
$EDITOR .env          # paste the folder ID, sheet ID, and API key

cd hsa_vault

# 3. Create the Sheet tabs with the right headers
#    Add --create to also create the spreadsheet itself in your Drive folder.
../.venv/bin/python -m scripts.bootstrap_sheet

# 4. (Optional) Sample data so the UI isn't empty
../.venv/bin/python -m scripts.seed_data

# 5. Run
../.venv/bin/streamlit run app.py
```

Everything in `.env` is also editable in the app's **Settings** page, which
writes `~/.hsavault/settings.json`. That file wins over `.env`; your `.env` is
never modified.

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

75 tests, no network. `test_ledger.py` covers the balance math: HSA-card
exclusion, partial reimbursements, multi-receipt withdrawals, tax-year
boundaries, and duplicate rejection. `test_extraction_parsing.py` mocks the
OpenAI SDK entirely and covers the tolerant reply parsing — fenced JSON, chatty
preambles, invented categories, and currency symbols in the amount.

---

## Layout

```
hsa_vault/
  app.py                  Dashboard
  pages/                  Upload, Receipts, Reimbursements, Contributions,
                          Export, Bulk Import, Settings
  core/
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
  scripts/                bootstrap_sheet.py, seed_data.py
  tests/
```

## Note on the model

The build spec named Claude. Extraction now runs on NVIDIA NIM instead, at the
owner's request. Because NIM models honour `response_format` inconsistently, the
JSON contract is stated in the prompt and the reply is parsed defensively —
markdown fences, a chatty preamble, an invented category, or `$1,042.18` in the
amount field are all handled rather than trusted. Swap models with
`NVIDIA_MODEL`; nothing else in the codebase knows which model is in use.
