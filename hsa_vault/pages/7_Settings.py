"""Settings + reliability tools (bootstrap, cache rebuild, Drive/Sheet diff)."""

from dataclasses import asdict, replace

import streamlit as st

from core import auth, config, store
from core.models import PAYMENT_LABELS, PAYMENT_METHODS

st.set_page_config(page_title="Settings — HSAVault", page_icon="⚙️", layout="wide")

auth.require_login()
st.title("⚙️ Settings")
store.show_flash()

settings = store.settings()

st.caption(
    f"Values come from `.env`, overridden by `{config.SETTINGS_PATH}`. "
    "Saving here writes the JSON file; your `.env` is never modified."
)

with st.form("settings"):
    st.subheader("Google")
    google_credentials_json = st.text_input(
        "Google credentials JSON path",
        value=settings.google_credentials_json,
        help="OAuth client file (recommended) or a service account key.",
    )
    c1, c2 = st.columns(2)
    drive_folder_id = c1.text_input("Drive folder ID", value=settings.drive_folder_id)
    sheet_id = c2.text_input("Sheet ID", value=settings.sheet_id)

    st.subheader("NVIDIA (receipt extraction)")
    c3, c4 = st.columns(2)
    nvidia_api_key = c3.text_input(
        "API key", value=settings.nvidia_api_key, type="password",
        help="Leave blank to disable extraction. Manual entry always works.",
    )
    nvidia_model = c4.text_input(
        "Model", value=settings.nvidia_model,
        help="Any NVIDIA NIM vision model, e.g. meta/llama-3.2-90b-vision-instruct.",
    )

    st.subheader("Defaults")
    c5, c6 = st.columns(2)
    default_payment_method = c5.radio(
        "Default payment method",
        PAYMENT_METHODS,
        index=PAYMENT_METHODS.index(settings.default_payment_method)
        if settings.default_payment_method in PAYMENT_METHODS
        else 0,
        format_func=lambda m: PAYMENT_LABELS[m],
    )
    default_patient = c6.text_input("Default patient", value=settings.default_patient)

    st.subheader("Dashboard")
    c7, c8, c9 = st.columns(3)
    projection_rate = c7.number_input(
        "Projection return rate", 0.0, 0.30, float(settings.projection_rate), step=0.005,
        format="%.3f", help="Illustration only.",
    )
    irs_limit = c8.text_input("IRS contribution limit", value=str(settings.irs_limit))
    irs_limit_year = c9.number_input(
        "Limit applies to tax year", 2000, 2100, int(settings.irs_limit_year)
    )

    if st.form_submit_button("Save settings", type="primary"):
        config.save_settings(
            replace(
                settings,
                google_credentials_json=google_credentials_json.strip(),
                drive_folder_id=drive_folder_id.strip(),
                sheet_id=sheet_id.strip(),
                nvidia_api_key=nvidia_api_key.strip(),
                nvidia_model=nvidia_model.strip(),
                default_payment_method=default_payment_method,
                default_patient=default_patient.strip(),
                projection_rate=float(projection_rate),
                irs_limit=irs_limit.strip(),
                irs_limit_year=int(irs_limit_year),
            )
        )
        st.cache_resource.clear()
        store.reload_settings()
        store.flash("Saved.")
        st.rerun()

st.divider()
st.subheader("Connection")

missing = settings.ready()
if missing:
    st.error("Missing: " + ", ".join(missing))
else:
    if st.button("Test connection"):
        try:
            sheets, drive = store.clients()
            rows = sheets.read_tab("receipts")
            root = drive.metadata(settings.drive_folder_id)
            st.success(
                f"Connected. Sheet has {len(rows)} receipt row(s); "
                f"Drive folder is `{root.get('name')}`."
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed: {exc}")
            st.caption(
                "Most common cause: the folder or sheet is not shared with the service "
                "account's email address. See the README."
            )

    if st.button("Create/repair the three Sheet tabs"):
        try:
            created = store.clients()[0].ensure_tabs()
            st.success(f"Tabs verified. Created: {created or 'none — all present'}.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed: {exc}")

st.divider()
st.subheader("Reliability")

col_a, col_b = st.columns(2)

with col_a:
    st.markdown("**Rebuild the local cache from Sheets**")
    st.caption(
        "The SQLite cache is never authoritative. This wipes and repopulates it from "
        "the Sheet — safe to run at any time."
    )
    if st.button("Rebuild cache"):
        try:
            count = store.rebuild_cache()
            st.success(f"Rebuilt from {count} row(s).")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed: {exc}")

with col_b:
    st.markdown("**Find Drive files missing from the Sheet**")
    st.caption("Detects half-finished saves. Repair them in Bulk Import.")
    if st.button("Scan Drive"):
        try:
            orphans = store.find_orphans()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed: {exc}")
        else:
            if orphans:
                st.error(f"{len(orphans)} orphan(s):")
                for f in orphans:
                    st.write(f"- `{f['name']}`")
            else:
                st.success("None — Drive and Sheet agree.")

st.divider()
st.subheader("Local state")
st.code(
    f"settings  {config.SETTINGS_PATH}\n"
    f"cache     {config.CACHE_PATH}\n"
    f"audit log {config.AUDIT_PATH}",
    language="text",
)
if config.AUDIT_PATH.exists():
    with st.expander("Last 40 audit log entries"):
        lines = config.AUDIT_PATH.read_text().splitlines()[-40:]
        st.code("\n".join(lines), language="json")

with st.expander("Current effective settings"):
    redacted = asdict(settings)
    if redacted.get("nvidia_api_key"):
        redacted["nvidia_api_key"] = "***"
    st.json(redacted)

st.caption("Not tax advice.")
