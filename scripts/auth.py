#!/usr/bin/env python3
"""One-time Yahoo OAuth flow. Run locally:

    python scripts/auth.py            # saves YAHOO_REFRESH_TOKEN to .env
    python scripts/auth.py --set-secret   # also stores it as a GitHub Actions secret via `gh`

Reads YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET from the environment or a gitignored .env
(prompts if missing). Never commit the resulting .env.
"""
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from museum import yahoo_auth  # noqa: E402
from museum.config import ROOT, load_dotenv  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "k1kuma/raccoon-museum")


def update_env_file(values: dict[str, str]) -> Path:
    env_path = ROOT / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    env_path.write_text("\n".join(out) + "\n")
    env_path.chmod(0o600)
    return env_path


def _hint(exc: "yahoo_auth.YahooAuthError") -> str:
    e = exc.error.upper()
    redirect = yahoo_auth.redirect_uri()
    if e in {"INVALID_CONSUMER_KEY", "INVALID_CLIENT", "UNAUTHORIZED_CLIENT"}:
        return ("Hint: Yahoo doesn't recognize the Client ID/Secret. Copy both again from "
                "https://developer.yahoo.com/apps/ (no spaces/quotes). If a stale value is in .env, "
                "fix or delete those lines and re-run.")
    if "REDIRECT" in e or "redirect" in exc.description.lower():
        return (f"Hint: the redirect URI must exactly match the one registered on your Yahoo app. "
                f"This script uses {redirect!r}; set YAHOO_REDIRECT_URI to override "
                "(e.g. YAHOO_REDIRECT_URI=https://localhost:8080/).")
    if e in {"INVALID_GRANT", "INVALID_AUTHORIZATION_CODE"} or "code" in exc.description.lower():
        return "Hint: the code was already used, expired, or was copied incompletely."
    return (f"Hint: check the Client ID/Secret and that the app's redirect URI is exactly {redirect!r}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set-secret", action="store_true", help="store the refresh token with `gh secret set`")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    args = parser.parse_args()

    load_dotenv()
    client_id = (os.environ.get("YAHOO_CLIENT_ID") or input("Yahoo Client ID: ")).strip()
    client_secret = (os.environ.get("YAHOO_CLIENT_SECRET") or getpass.getpass("Yahoo Client Secret: ")).strip()
    if os.environ.get("YAHOO_CLIENT_ID"):
        print(f"Using YAHOO_CLIENT_ID from environment/.env (ends in …{client_id[-4:]}).")

    for attempt in range(3):
        url = yahoo_auth.authorize_url(client_id)
        print("\n1. Sign in to Yahoo and click 'Agree' at this URL:\n")
        print(f"   {url}\n")
        if not args.no_browser:
            webbrowser.open(url)
        print(f"2. Yahoo redirects to {yahoo_auth.redirect_uri()}/?code=... — the page will fail to load; that's expected.")
        pasted = input("3. Paste the full URL from the address bar (or just the code): ").strip()

        code = pasted
        if "code=" in pasted:
            code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [""])[0]
        code = code.strip()
        if not code:
            print("No authorization code found.", file=sys.stderr)
            return 1

        try:
            token = yahoo_auth.exchange_code(client_id, client_secret, code)
            break
        except yahoo_auth.YahooAuthError as exc:
            print(f"\n❌ {exc}", file=sys.stderr)
            print(_hint(exc), file=sys.stderr)
            if exc.error.upper() in {"INVALID_CONSUMER_KEY", "INVALID_CLIENT", "UNAUTHORIZED_CLIENT"}:
                return 1
            if attempt < 2:
                print("\nCodes are single-use and expire after a few minutes — let's get a fresh one.", file=sys.stderr)
    else:
        return 1

    refresh_token = token["refresh_token"]
    env_path = update_env_file(
        {"YAHOO_CLIENT_ID": client_id, "YAHOO_CLIENT_SECRET": client_secret, "YAHOO_REFRESH_TOKEN": refresh_token}
    )
    print(f"\n✅ Saved YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET and YAHOO_REFRESH_TOKEN to {env_path} (gitignored).")

    if args.set_secret:
        subprocess.run(
            ["gh", "secret", "set", "YAHOO_REFRESH_TOKEN", "--repo", REPO],
            input=refresh_token.encode(),
            check=True,
        )
        print(f"✅ Stored YAHOO_REFRESH_TOKEN as a GitHub Actions secret on {REPO}.")
    else:
        print("\nNext, store the refresh token as a GitHub secret (value is read from .env, not echoed):")
        print(f"   grep '^YAHOO_REFRESH_TOKEN=' .env | cut -d= -f2- | gh secret set YAHOO_REFRESH_TOKEN --repo {REPO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
