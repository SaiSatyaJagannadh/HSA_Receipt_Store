"""Print the secrets.toml block to paste into Streamlit Cloud.

    python -m scripts.export_deploy_secrets            # print to stdout
    python -m scripts.export_deploy_secrets --write    # also write .streamlit/secrets.toml

A hosted server cannot open a browser for OAuth consent, so it runs on the
refresh token you already granted locally. This reads ~/.hsavault/token.json plus
your .env and emits the block Streamlit Cloud needs.

The output contains live credentials. Do not paste it anywhere public, and note
that .streamlit/secrets.toml is gitignored for exactly this reason.
"""

import argparse
import json
import sys
from pathlib import Path

from core import config


def toml_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def build() -> str:
    if not config.TOKEN_PATH.exists():
        print(
            f"No token at {config.TOKEN_PATH}. Run this first:\n"
            "    python -m scripts.bootstrap_sheet",
            file=sys.stderr,
        )
        raise SystemExit(1)

    token = json.loads(config.TOKEN_PATH.read_text())
    if not token.get("refresh_token"):
        print(
            "Token has no refresh_token, so the deployed app could not renew "
            "access. Revoke at https://myaccount.google.com/permissions, delete "
            f"{config.TOKEN_PATH}, and re-run scripts/bootstrap_sheet.py.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    s = config.load_settings()
    missing = [k for k in ("drive_folder_id", "sheet_id") if not getattr(s, k)]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)

    lines = [
        "# HSAVault deploy secrets. Live credentials — never commit or share.",
        "",
        "[google_token]",
    ]
    for key in ("token", "refresh_token", "token_uri", "client_id", "client_secret"):
        if token.get(key):
            lines.append(f'{key} = "{toml_escape(token[key])}"')
    lines += [
        'type = "authorized_user"',
        "",
        "[hsa]",
        f'drive_folder_id = "{toml_escape(s.drive_folder_id)}"',
        f'sheet_id = "{toml_escape(s.sheet_id)}"',
        f'nvidia_api_key = "{toml_escape(s.nvidia_api_key)}"',
        f'nvidia_model = "{toml_escape(s.nvidia_model)}"',
        f'default_patient = "{toml_escape(s.default_patient)}"',
        f'default_payment_method = "{toml_escape(s.default_payment_method)}"',
        f'irs_limit = "{toml_escape(s.irs_limit)}"',
        f"irs_limit_year = {s.irs_limit_year}",
        f"projection_rate = {s.projection_rate}",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write .streamlit/secrets.toml")
    args = parser.parse_args()

    block = build()
    if args.write:
        out = Path(".streamlit/secrets.toml")
        out.parent.mkdir(exist_ok=True)
        out.write_text(block)
        out.chmod(0o600)
        print(f"Wrote {out.resolve()} (gitignored, mode 600).", file=sys.stderr)
    else:
        print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
