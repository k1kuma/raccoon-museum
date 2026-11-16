"""Open, update or close the `scrape-failure` GitHub issue from a workflow run.

    python scripts/scrape_alert.py fail --code 3 --workflow "Live scores" --run-url URL
    python scripts/scrape_alert.py ok --run-url URL

Uses the `gh` CLI with GH_TOKEN (GITHUB_TOKEN with issues: write) and GITHUB_REPOSITORY. One issue at a time:
a repeat failure adds a comment at most every 6 hours instead of opening a new issue, and the next successful
scrape closes it. GitHub emails repo owners about new issues and comments by default.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

LABEL = "scrape-failure"
COOKIES_EXIT = 3
THROTTLE = timedelta(hours=6)

FIX_COOKIES = """**How to fix:** the Yahoo session cookies in the `YAHOO_COOKIES` secret have expired or been signed out.

- If the cookie refresher is installed on your Mac (README → *Automatic cookie refresh*), open
  https://football.fantasysports.yahoo.com/f1/705220 in Chrome while signed in, then run
  `scripts/refresh-cookies-now.sh` (or wait for its next 9am/9pm run). It updates the secret and re-runs the
  live workflow, and the next successful scrape closes this issue.
- Otherwise copy a fresh `Cookie` header from Chrome DevTools and run
  `gh secret set YAHOO_COOKIES -R {repo}` (paste it, then Ctrl-D).

The site keeps serving its last good build until then; nothing was deleted."""

FIX_OTHER = """**How to fix:** open the run log above. Yahoo may have changed a page layout or been temporarily
unavailable. If the next scheduled run succeeds this issue closes itself. If it keeps failing, re-run the workflow
with *Run workflow* or open the log and look for the first traceback.

The site keeps serving its last good build until then; nothing was deleted."""


def title_for(code: int | None) -> str:
    return "Yahoo scrape failing: cookies expired" if code == COOKIES_EXIT else "Yahoo scrape failing"


def body_for(code: int | None, workflow: str, run_url: str, repo: str) -> str:
    reason = ("Yahoo returned the sign-in page, so the session cookies are no longer valid."
              if code == COOKIES_EXIT else f"The scraper exited with code {code if code is not None else 'unknown'}.")
    fix = FIX_COOKIES.format(repo=repo) if code == COOKIES_EXIT else FIX_OTHER
    return f"**{workflow}** failed to scrape Yahoo.\n\n**Reason:** {reason}\n\n**Run:** {run_url}\n\n{fix}"


def should_comment(last_update: str | None, now: datetime) -> bool:
    if not last_update:
        return True
    then = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
    return now - then >= THROTTLE


def gh(repo: str, *args: str) -> str:
    out = subprocess.run(["gh", *args, "-R", repo], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {out.stderr.strip()}")
    return out.stdout


def open_issue(repo: str) -> dict | None:
    rows = json.loads(gh(repo, "issue", "list", "--label", LABEL, "--state", "open",
                         "--json", "number,updatedAt", "--limit", "1") or "[]")
    return rows[0] if rows else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("status", choices=["fail", "ok"])
    ap.add_argument("--code", default="")
    ap.add_argument("--workflow", default=os.environ.get("GITHUB_WORKFLOW", "Scrape"))
    ap.add_argument("--run-url", default="")
    args = ap.parse_args(argv)
    repo = os.environ["GITHUB_REPOSITORY"]
    code = int(args.code) if str(args.code).strip().lstrip("-").isdigit() else None
    try:
        existing = open_issue(repo)
        if args.status == "ok":
            if existing:
                gh(repo, "issue", "close", str(existing["number"]), "--comment",
                   f"✅ Scraping works again ({args.workflow}: {args.run_url}). Closing.")
                print(f"Closed scrape-failure issue #{existing['number']}.")
            return 0
        if existing:
            if should_comment(existing.get("updatedAt"), datetime.now(timezone.utc)):
                gh(repo, "issue", "comment", str(existing["number"]), "--body",
                   "Still failing.\n\n" + body_for(code, args.workflow, args.run_url, repo))
                print(f"Commented on scrape-failure issue #{existing['number']}.")
            else:
                print(f"Issue #{existing['number']} already open and updated recently; not commenting again.")
            return 0
        gh(repo, "label", "create", LABEL, "--color", "B60205", "--force",
           "--description", "Automated: the Yahoo scrape is failing")
        url = gh(repo, "issue", "create", "--title", title_for(code), "--label", LABEL,
                 "--body", body_for(code, args.workflow, args.run_url, repo)).strip()
        print(f"Opened {url}")
    except RuntimeError as e:
        # Alerting is best effort; never mask the real failure with an alerting error.
        print(f"::warning::{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
