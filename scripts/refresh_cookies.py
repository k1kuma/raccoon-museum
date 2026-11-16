"""Keep the YAHOO_COOKIES GitHub secret fresh from your everyday Chrome session (macOS).

You log in to Yahoo Fantasy in Chrome anyway; this reads Chrome's yahoo.com cookies for the fantasy site,
checks them against the league page, and only when they work and have changed runs
`gh secret set YAHOO_COOKIES` (value passed on stdin, never printed or logged).
If a `scrape-failure` issue is open it also re-runs the live workflow so the site recovers.

    .venv/bin/python scripts/refresh_cookies.py              # refresh if changed
    .venv/bin/python scripts/refresh_cookies.py --check-only # read + validate, don't touch the secret
    .venv/bin/python scripts/refresh_cookies.py --force      # push even if unchanged

Run it once by hand first: macOS asks to let Python read "Chrome Safe Storage" from your Keychain;
choose "Always Allow". Then scripts/install-cookie-refresher.sh schedules it (9am, 9pm, and at login).
Privacy: only cookies that Chrome would send to football.fantasysports.yahoo.com are read; nothing else.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = os.environ.get("MUSEUM_REPO", "k1kuma/raccoon-museum")
LEAGUE_ID = os.environ.get("YAHOO_LEAGUE_ID", "705220")
HOST = "football.fantasysports.yahoo.com"
SECRET = "YAHOO_COOKIES"
STATE_DIR = Path.home() / "Library" / "Application Support" / "raccoon-museum"
STATE_FILE = STATE_DIR / "cookies.sha256"
ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("refresh_cookies")

EXIT_OK, EXIT_ERROR, EXIT_NO_COOKIES, EXIT_INVALID = 0, 1, 2, 3


def domain_matches(cookie_domain: str, host: str = HOST) -> bool:
    """RFC 6265 domain-match: would a browser send a cookie set for `cookie_domain` to `host`?"""
    d = (cookie_domain or "").lstrip(".").lower()
    return bool(d) and d.endswith("yahoo.com") and (host == d or host.endswith("." + d))


def select_cookies(cookies, host: str = HOST, now: float | None = None) -> dict[str, str]:
    """name -> value for unexpired cookies a browser would send to `host`. When a name is set on several
    matching domains, the most specific domain wins (as the browser would list it first)."""
    now = time.time() if now is None else now
    chosen: dict[str, tuple[int, str]] = {}
    for c in cookies:
        if not domain_matches(c.domain, host) or not c.value:
            continue
        if c.expires and c.expires < now:
            continue
        spec = len(c.domain.lstrip("."))
        if c.name not in chosen or spec > chosen[c.name][0]:
            chosen[c.name] = (spec, c.value)
    return {name: v for name, (_, v) in chosen.items()}


def cookie_header(selected: dict[str, str]) -> str:
    return "; ".join(f"{k}={selected[k]}" for k in sorted(selected))


def fingerprint(header: str) -> str:
    return hashlib.sha256(header.encode()).hexdigest()


def read_chrome(cookie_file: str | None):
    try:
        import browser_cookie3
    except ImportError:
        sys.exit("browser_cookie3 is not installed: .venv/bin/pip install -r requirements-cookies.txt")
    return browser_cookie3.chrome(domain_name="yahoo.com", cookie_file=cookie_file)


def validate(header: str) -> tuple[bool, str]:
    """True when the league home page loads with these cookies (no redirect to the sign-in page)."""
    sys.path.insert(0, str(ROOT))
    from museum.scrape import CookiesExpired, Fetcher, ScrapeError

    try:
        html = Fetcher(header, 0, time.time() + 60).get(f"/f1/{LEAGUE_ID}")
    except CookiesExpired:
        return False, "Yahoo redirected to the sign-in page"
    except (ScrapeError, OSError) as e:
        return False, f"league page check failed: {type(e).__name__}"
    if f"/f1/{LEAGUE_ID}/" not in html:
        return False, "league page loaded but didn't look like the league (not a member, or layout change?)"
    return True, "league page loaded"


def gh(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)


def failure_issue_open() -> bool:
    out = gh("issue", "list", "-R", REPO, "--label", "scrape-failure", "--state", "open", "--json", "number")
    return out.returncode == 0 and bool(json.loads(out.stdout or "[]"))


def notify(message: str) -> None:
    """Best-effort macOS notification (no cookie data in it)."""
    script = f'display notification "{message}" with title "Raccoon Museum cookies"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check-only", "--dry-run", action="store_true", help="read and validate only")
    ap.add_argument("--force", action="store_true", help="update the secret even if unchanged")
    ap.add_argument("--cookie-file", help="Chrome Cookies DB for a non-default profile, e.g. "
                    "'~/Library/Application Support/Google/Chrome/Profile 1/Cookies'")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    alert = (lambda m: None) if args.no_notify else notify

    selected = select_cookies(read_chrome(os.path.expanduser(args.cookie_file) if args.cookie_file else None))
    if not selected:
        log.warning("No Yahoo cookies found in Chrome. Sign in at https://%s/f1/%s in Chrome.", HOST, LEAGUE_ID)
        alert("No Yahoo cookies in Chrome. Sign in to Yahoo Fantasy.")
        return EXIT_NO_COOKIES
    header = cookie_header(selected)
    log.info("Read %d Yahoo cookies from Chrome.", len(selected))

    ok, why = validate(header)
    if not ok:
        log.warning("Cookies not valid (%s). Open Yahoo Fantasy in Chrome and sign in, then re-run.", why)
        alert("Yahoo cookies invalid. Open Yahoo Fantasy in Chrome and sign in.")
        return EXIT_INVALID
    log.info("Cookies valid: %s.", why)

    digest = fingerprint(header)
    previous = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""
    if args.check_only:
        log.info("Check only: secret %s.", "would be updated" if digest != previous else "already up to date")
        return EXIT_OK
    if digest == previous and not args.force:
        log.info("Unchanged since the last update; secret left alone.")
    else:
        res = gh("secret", "set", SECRET, "-R", REPO, stdin=header)
        if res.returncode != 0:
            log.error("gh secret set failed (is `gh auth status` logged in with repo access?).")
            alert("Couldn't update the GitHub secret. Run gh auth status.")
            return EXIT_ERROR
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(digest + "\n")
        os.chmod(STATE_FILE, 0o600)
        log.info("Updated the %s secret on %s.", SECRET, REPO)

    if failure_issue_open():
        res = gh("workflow", "run", "live.yml", "-R", REPO)
        log.info("A scrape-failure issue is open; %s.",
                 "re-ran the live workflow to recover" if res.returncode == 0 else "couldn't trigger the live workflow")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
