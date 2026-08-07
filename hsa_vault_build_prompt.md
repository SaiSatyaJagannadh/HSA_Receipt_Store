# BUILD PROMPT — HSA Receipt Vault ("HSAVault")

Paste everything below into Claude Code as your opening message.

---

Build me a production-quality personal HSA receipt management application. I am a single user, running this locally. Prioritize correctness and durability of my records over features. Read this entire spec before writing any code, then confirm the plan back to me in a short outline before you start building.

## 1. Core Principle

My receipts live in **my own Google Drive**, permanently, in a plain folder structure I can navigate without this app. The app is an **index and a reporting layer over Drive** — never the sole source of truth. If this application is deleted tomorrow, my records must remain complete, organized, and usable by a human or a CPA. Design every decision against that constraint.

## 2. Tech Stack

- **Python 3.11+**
- **Streamlit** — UI
- **Google Drive API v3** — file storage
- **Google Sheets API v4** — the index / ledger
- **Anthropic API (claude-sonnet-4-6)** — vision extraction from receipt images
- **SQLite** — local cache only, rebuildable from Sheets at any time. Never authoritative.
- **reportlab** or **fpdf2** — PDF audit packet generation
- **Pillow, pdf2image** — image normalization
- **python-dotenv** — config

Use a Google **service account** for auth (not OAuth user flow). I will create the service account and share my Drive folder and Sheet with its email address. Document this setup precisely in the README.

## 3. Data Model

### Drive structure (create automatically if missing)
```
HSA_Vault/
  2026/
    2026-03-14__CVS_Pharmacy__42.18__<short_hash>.jpg
  2027/
  _exports/
    HSA_Audit_Packet_2026.pdf
```
Filename format: `YYYY-MM-DD__Provider__Amount__hash.ext`. Provider slugified, no spaces. The hash is the first 8 chars of the file's SHA-256, used for duplicate detection.

### Google Sheet — tab `receipts`
| column | type | notes |
|---|---|---|
| receipt_id | str | UUID4 |
| file_hash | str | SHA-256 of original bytes — duplicate detection key |
| drive_file_id | str | |
| drive_link | str | webViewLink, clickable |
| service_date | date | date of service/purchase, NOT upload date |
| upload_date | datetime | |
| provider | str | |
| amount | decimal | 2dp, store as string to avoid float drift |
| category | enum | see §5 |
| description | str | what the receipt is actually for, plain English |
| payment_method | enum | `hsa_card` \| `out_of_pocket` |
| reimbursed | bool | |
| reimbursement_date | date | nullable |
| reimbursement_amount | decimal | nullable, may be partial |
| patient | str | self / spouse / dependent name |
| tax_year | int | derived from service_date |
| eligibility_confidence | enum | `certain` \| `likely` \| `review` |
| notes | str | |
| extraction_raw | json | full model output, for debugging |

### Tab `reimbursements`
Ledger of withdrawals: `reimbursement_id, date, amount, method, covered_receipt_ids (csv), notes`. One withdrawal may cover several receipts.

### Tab `contributions`
`date, amount, source (payroll/personal), tax_year` — for tracking against IRS annual limits.

## 4. Two Operating Modes

The app must handle both, because I use both:

- **`hsa_card`** — I paid at the register with the HSA card. Already "reimbursed" by definition. These receipts are pure audit documentation. They must NOT count toward the unreimbursed balance.
- **`out_of_pocket`** — I paid with other funds and may reimburse myself later, possibly years later. These accumulate into a claimable balance.

Default the mode in Settings, allow per-receipt override. Make the distinction visually obvious everywhere in the UI — this is the single most important field in the app and getting it wrong causes double-claiming.

## 5. Categories

`Physician`, `Dental`, `Vision`, `Prescription`, `OTC Medication`, `Medical Devices/Supplies`, `Lab/Diagnostic`, `Hospital/Surgery`, `Mental Health`, `Physical Therapy`, `Chiropractic`, `Insurance Premium (limited eligibility)`, `Mileage/Travel`, `Other`.

Mark `Insurance Premium` with an inline warning that eligibility is narrow. Do not give tax advice anywhere in the UI beyond factual IRS-published categories, and include a footer noting the app is not tax advice.

## 6. Upload & Extraction Flow

1. Multi-file uploader accepting JPG, PNG, HEIC, PDF.
2. Normalize: HEIC → JPEG, multi-page PDF → per-page images, auto-rotate, downscale to max 2000px long edge for the API call while **uploading the original bytes to Drive unmodified**.
3. Compute SHA-256. If the hash already exists in the index, block the upload and show the existing record. Never silently create duplicates.
4. Send to Claude vision with a structured-output prompt returning strict JSON:
   `{provider, service_date, total_amount, line_items[], category, description, patient_name_if_visible, is_medical_expense (bool), eligibility_confidence, ambiguities[]}`
   Instruct the model to return `null` for any field it cannot read rather than guessing, and to list what was unclear in `ambiguities`.
5. Show a **confirmation form** pre-filled with the extraction. Fields the model was unsure about are highlighted. Nothing is written until I click Save. Never auto-commit an extraction.
6. On save: upload to Drive with the canonical filename, append the row to Sheets, update the SQLite cache.
7. If the Drive upload succeeds but the Sheets write fails, the app must detect the orphan on next launch and offer to repair. Log every write to a local append-only `audit.log`.

Also provide a **Bulk Import** page that scans an existing Drive folder of loose receipt images and runs them through extraction in a batch with a review queue — I have a backlog.

## 7. Dashboard

Recompute on every load:

- **Unreimbursed claimable balance** — big number, top of page. Sum of `out_of_pocket` AND `reimbursed == False`.
- Receipt count and total by tax year
- Spend by category — bar chart
- Spend over time — line chart, monthly buckets
- Contributions this year vs. the IRS limit, with remaining room
- **Growth projection**: if I leave the unreimbursed balance unclaimed and invested at an assumed annual return (user-configurable, default 7%), show the value at 5 / 10 / 20 years. Label it clearly as an illustration, not a promise.
- Warnings panel: receipts missing amounts, missing dates, flagged `review`, or over 12 months old and still unreimbursed.

## 8. Receipts Browser

Filterable table: tax year, category, provider, reimbursed status, payment method, patient, amount range, free-text search across provider + description. Every row links to the Drive file and to a detail view with the image inline, editable fields, and an edit history trail. Support soft-delete only — a deleted receipt is archived and its Drive file is moved to `HSA_Vault/_archive/`, never hard-deleted.

## 9. Reimbursement Workflow

Select multiple unreimbursed out-of-pocket receipts → "Mark Reimbursed" → enter withdrawal date, total amount, method. Write a row to `reimbursements` and flip each receipt's flag with the reimbursement_date. Support partial reimbursement where the withdrawal is less than the selected total. Guard against marking an already-reimbursed receipt, and against `hsa_card` receipts appearing in the selection list at all.

## 10. Audit Packet Export — the most important feature

Generate a single PDF per tax year, saved to Drive `_exports/` and downloadable:

- **Cover page**: tax year, my name, total qualified expenses, total reimbursed, receipt count, generation date
- **Summary table**: every receipt — date, provider, category, amount, payment method, reimbursed status
- **Category subtotals**
- **One page per receipt**: the full receipt image with its metadata printed beneath it
- Page numbers, and a footer on every page identifying the tax year

Also export CSV and a ZIP of raw images for the year. The PDF must be self-contained and legible printed in black and white.

## 11. Settings

Drive folder ID, Sheet ID, service account JSON path, Anthropic API key, default payment method, default patient, projection return rate, IRS contribution limit for the current year (editable — this changes annually).

## 12. Reliability Requirements

- **Rebuild from Drive**: a command that reconstructs the entire SQLite cache from the Sheet, and a second that detects Drive files missing from the Sheet.
- Exponential backoff and retry on all Google and Anthropic API calls.
- Every destructive action requires confirmation.
- Graceful degradation: if the Anthropic API is unavailable, uploads must still work with fully manual entry. The app must never block me from saving a receipt.
- Amounts handled as `Decimal` throughout. No floats in money paths.

## 13. Project Structure

```
hsa_vault/
  app.py
  pages/
    1_Upload.py
    2_Receipts.py
    3_Reimbursements.py
    4_Contributions.py
    5_Export.py
    6_Bulk_Import.py
    7_Settings.py
  core/
    drive.py
    sheets.py
    extraction.py
    models.py          # dataclasses + validation
    ledger.py          # all balance math, pure functions
    pdf_export.py
    cache.py
    config.py
  tests/
    test_ledger.py
    test_models.py
    test_extraction_parsing.py
  .env.example
  requirements.txt
  README.md
```

## 14. Testing

Write real unit tests for `ledger.py` covering: unreimbursed balance excludes hsa_card receipts, partial reimbursements, multi-receipt reimbursements, tax-year boundary dates, and duplicate hash rejection. Mock the Google and Anthropic clients. I want to trust the balance number.

## 15. Deliverables

1. Complete working code
2. `README.md` with step-by-step Google Cloud setup: creating the project, enabling Drive + Sheets APIs, creating the service account, downloading the JSON key, sharing the folder and Sheet with the service account email
3. A script that creates the Sheet with all three tabs and correct headers on first run
4. `.env.example`
5. Seed data script so I can see the UI populated before I upload anything real

## 16. How to Proceed

Build in this order and stop for my review after each stage:

1. Config, models, Google auth, Sheet bootstrap — prove I can read and write a row
2. Upload + extraction + confirmation form
3. Receipts browser
4. Dashboard and ledger math (with tests)
5. Reimbursement workflow
6. PDF audit packet
7. Bulk import, polish, README

Start with stage 1. Confirm the plan first.
