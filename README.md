# 🦝 City Slickin' Raccoons — Fantasy Football Museum

A public, static museum for the Yahoo Fantasy Football league
[City Slickin' Raccoons (705220)](https://football.fantasysports.yahoo.com/f1/705220).
It updates itself every Tuesday morning during the season, and shows live scores during NFL games.

**Live site:** https://k1kuma.github.io/raccoon-museum/

| Wing | What's on display |
| --- | --- |
| **Lobby** | Live scoreboard on game days, this week's headlines, records broken, active streaks, "on this week in history", standings, fiercest rivalries, lore |
| **Hall of Champions** | Yearly title banners with the podium, plus the trophy case |
| **Record Book** | Highest and lowest scores, blowouts, closest games, win and loss streaks, playoff explosions, best seasons |
| **Manager Exhibits** | One page per manager: career record, titles, best and worst picks and trades, head-to-head table |
| **Rivalry Room** | Pick any two managers to see their full head-to-head history (every pair has its own URL) |
| **Hall of Shame** | Points left on the bench, losses with the 2nd-best score, heartbreaks, ugly wins, draft busts, lopsided trades, last-place finishes |
| **Archives** | Each season's standings, playoff bracket, draft board (with steals and busts), weekly results, and trades |

Every exhibit and every entry has a shareable URL. On phones, the Share button opens the native share sheet; elsewhere it copies the link.
Pick yourself in **"Who are you?"** (saved in your browser) to highlight your team everywhere.
Entries from the latest week get a **New this week** badge.

## How it works

```
Yahoo Fantasy API ──yfpy──▶ data/league.sqlite ──museum stats──▶ site/src/data/*.json ──Astro──▶ GitHub Pages
        (python -m museum ingest)      (committed)                    (generated at build)
```

* `museum/ingest.py` walks the league's `renew` chain to find every past season. It pulls:
  * standings
  * weekly matchups
  * playoffs
  * draft results with season points
  * transactions
  * every team's weekly roster with player points (used for the bench-points stats)

  Finished seasons are fetched once and cached in SQLite (`fetch_log`). Weekly runs only fetch new weeks. Seasons you can't access are logged and skipped.
* `museum/stats.py` turns the database into JSON with story-style captions. Definitions:
  * **Bench points**: the optimal lineup minus the lineup actually started.
  * **Draft value**: the overall pick number minus the player's points rank among drafted players.
  * **Trade value**: points that the received players scored in the new team's starting lineup, minus the same for the players sent away.
* `site/` is an Astro static site (no client framework, just a little vanilla JS).
* `.github/workflows/update.yml` runs every **Tuesday at 13:00 UTC** (after MNF), on manual dispatch, and on pushes to `main`. It does the following:
  1. Ingests new data (scheduled and manual runs only, and only once the token secret exists).
  2. Commits `data/league.sqlite` and any new team logos.
  3. Recomputes the stats.
  4. Builds and deploys to Pages.

* `.github/workflows/live.yml` shows **live scores** in the Lobby. It follows each week's real NFL schedule:
  * `.github/workflows/nfl-schedule.yml` runs daily. It saves the next ~9 days of kickoff times from ESPN's free public scoreboard (no Yahoo requests) to `data/nfl_schedule.json`, and commits only when kickoffs change.
  * `live.yml` fires every 15 minutes, but its first job is a few-second gate (`python -m museum.nfl_schedule gate`). The gate stops unless a game is on. A game counts as on from 15 minutes before kickoff to 4 hours after (to cover overtime), and overlapping games are merged into one window. One final refresh runs after the last window closes.
  * Early international games, Saturday, Wednesday and Friday games, Thanksgiving and Christmas are all covered automatically. Times are stored in UTC, so daylight saving can't shift them.
  * If the schedule file is missing or more than 8 days old, the gate falls back to fixed windows (Sunday 12:45 PM to midnight ET, and Monday/Thursday 8 PM to midnight ET) and logs a warning.
  * Run `python -m museum.nfl_schedule gate` locally to see the computed windows.

  When the gate opens, the run makes one Yahoo request (`python -m museum scrape --live`), which reads the current week's scoreboard into a gitignored `data/live.json`. The site is then rebuilt and redeployed.
  * Nothing is committed, so the repo doesn't grow every 15 minutes.
  * Live scores appear only in the Lobby's **Live** panel. Records, streaks and standings use final weeks from the database, which only the Tuesday run updates.
  * If the cookies have expired, the run fails before deploying, so the site keeps its last good build.

  Live builds and deploys share a concurrency group with the Tuesday run, so they never overlap. The gate sits outside that group, so it can't bump a queued Tuesday run. You can trigger a live refresh at any time with `gh workflow run live.yml`.

**Team logos:** the scheduled scrape reads each team's Yahoo logo from the standings and teams pages, and copies it into `site/public/logos/<season>/<team_id>-<hash>.<ext>`. A new file is downloaded only when the logo URL changes. The site never hotlinks Yahoo, so the logos keep working if Yahoo's URLs change. Each manager shows their latest logo across the site. Archives (and the Hall of Champions banners) show the logo from that season. Default Yahoo avatars, and missing logos, fall back to initials. Live runs never touch logos.

**Regular season vs playoffs:** at stats time, every game gets exactly one type. Yahoo's own flags mark consolation games as playoff games too, so the type is worked out from the league settings and seeds instead:
* `regular`: weeks before the playoff start week.
* `playoff`: winners'-bracket games, where both teams are seeded within the number of playoff teams. This includes the 3rd- and 5th-place games.
* `consolation`: every other game from the playoff start week on.

Stats default to the regular season. That includes the Basement, standings, streaks, rivalry head-to-head and bench points. Consolation games only appear in the Hall of Shame's **Toilet Bowl** exhibit. The Record Book and Hall of Shame have a Regular / Playoffs / All toggle, which is kept in the URL as `?segment=`. Every stat card shows a segment label.

**Sample data:** until `data/league.sqlite` exists, the site is built from a generated sample league (`python -m museum sample`). A banner on the site says so. The first successful Yahoo ingest swaps it out automatically.

## One-time setup (what you need to do)

`YAHOO_CLIENT_ID` and `YAHOO_CLIENT_SECRET` are already stored as Actions secrets. Only the refresh token is missing.

### 1. Get a Yahoo refresh token (about 2 minutes, on your computer)

Your Yahoo app is a Confidential Client with redirect URI `https://localhost:8080`.

```bash
git clone https://github.com/k1kuma/raccoon-museum.git && cd raccoon-museum
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python scripts/auth.py --set-secret
```

The script:
1. Prompts for your Client ID and Client Secret. The secret's input is hidden, and neither value is printed or committed.
2. Opens Yahoo's consent page. Sign in as a league member and click **Agree**.
3. Yahoo redirects to `https://localhost:8080/?code=...`. The page will fail to load; that's expected. Copy the **whole URL** from the address bar and paste it into the terminal.
4. Saves `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, and `YAHOO_REFRESH_TOKEN` to `.env`. This file is gitignored and set to `chmod 600`.
5. With `--set-secret`, runs `gh secret set YAHOO_REFRESH_TOKEN --repo k1kuma/raccoon-museum` for you. This needs the [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`).

Without `--set-secret`, set the secrets yourself. These commands read from `.env`, so nothing is echoed:

```bash
grep '^YAHOO_REFRESH_TOKEN=' .env | cut -d= -f2- | gh secret set YAHOO_REFRESH_TOKEN --repo k1kuma/raccoon-museum
# Only if you ever need to reset these too:
grep '^YAHOO_CLIENT_ID='     .env | cut -d= -f2- | gh secret set YAHOO_CLIENT_ID     --repo k1kuma/raccoon-museum
grep '^YAHOO_CLIENT_SECRET=' .env | cut -d= -f2- | gh secret set YAHOO_CLIENT_SECRET --repo k1kuma/raccoon-museum
```

> Never commit `.env`, and never paste these values into issues, PRs, or chat.

> **Yahoo Fantasy API approval.** Since mid-2026, Yahoo makes apps apply for Fantasy Sports API access.
> Without approval, logging in works but every API call fails with *"This application is not authorized to perform this action"*.
> Apply with your app's Client ID at <https://sports.yahoo.com/developer/access/>. Approval reportedly takes 1–3 weeks.
> After approval your existing token keeps working; just re-run the workflow.
> Some people report fixing it by revoking the app's access in their Yahoo account and running `scripts/auth.py` again, so that's worth a try first.
> Background: [uberfastman/yfpy#84](https://github.com/uberfastman/yfpy/issues/84).

### 1b. Scraping with browser cookies (works without API approval)

While Yahoo hasn't approved the API app, the workflow can read the league's regular web pages instead. These are the same pages you see when you're logged in. It authenticates with your browser's Yahoo cookies, stored as the `YAHOO_COOKIES` secret. When that secret is set, the workflow scrapes instead of calling the API.

1. In a desktop browser, log in at <https://football.fantasysports.yahoo.com/f1/705220>.
2. Open DevTools (**⌥⌘I** / **F12**) → **Network**, reload the page, and click the first request (`705220`).
3. Under **Request Headers**, copy the whole value of the `Cookie` header. It's one long line.
4. Store it as a secret. `gh` prompts you to paste it, so it never appears in your shell history:

   ```bash
   gh secret set YAHOO_COOKIES --repo k1kuma/raccoon-museum
   gh workflow run update.yml --repo k1kuma/raccoon-museum
   ```

The first run backfills every season (about 450 pages, roughly 20 minutes, with a ~2 s pause between requests). Weekly runs fetch about 50 pages. To run it locally instead, put `YAHOO_COOKIES=...` in `.env` and run `python -m museum scrape`, then `python -m museum stats`.

* **Expiry:** Yahoo cookies usually last weeks to months, and logging out of that browser session can end them early. When they expire, the run shows *"Scrape failed … refresh the YAHOO_COOKIES secret"*, a `scrape-failure` issue is opened (below), and the site keeps its last data. Repeat steps 1–4 to fix it, or let the automatic refresher do it.
* **Seasons:** the scraper reads the seasons in `SEASONS` in [`museum/config.py`](museum/config.py) (`season:league_id` pairs). When the league renews, add the new pair there or set the repository variable `YAHOO_SEASONS`. The new league ID is in the URL.
* **Risk:** this is equivalent to you browsing the site slowly, but automated access may go against Yahoo's terms. The cookies give full access to your Yahoo account, so keep them only in the secret or `.env`. Delete the secret (`gh secret delete YAHOO_COOKIES --repo k1kuma/raccoon-museum`) to switch back to the API once it's approved.

#### Failure alerts

When a live or Tuesday scrape fails (expired cookies, the sign-in page, or anything else), the workflow opens a GitHub issue labelled **`scrape-failure`** with the reason, a link to the run, and how to fix it. GitHub emails you about new issues and comments on your own repository by default (check **Settings → Notifications → Email** if you don't get them). While the issue is open, further failures add a comment at most every 6 hours instead of opening duplicates. The next successful scrape closes the issue automatically. The logic lives in [`scripts/scrape_alert.py`](scripts/scrape_alert.py).

#### Automatic cookie refresh (macOS + Chrome)

If you visit Yahoo Fantasy in Chrome regularly, your Mac can keep the secret fresh. [`scripts/refresh_cookies.py`](scripts/refresh_cookies.py) reads only the yahoo.com cookies Chrome would send to the fantasy site and checks them against the league page. It updates `YAHOO_COOKIES` with `gh secret set` (value passed on stdin, never printed or logged) only if they work and have changed. If a `scrape-failure` issue is open, it also re-runs the live workflow so the site recovers. It stores only a SHA-256 fingerprint of the last upload, in `~/Library/Application Support/raccoon-museum/`.

One-time setup, from the repository folder:

```bash
python3 -m venv .venv                       # if you don't have it yet
.venv/bin/pip install -r requirements.txt -r requirements-cookies.txt
gh auth status                              # must be logged in with access to k1kuma/raccoon-museum
.venv/bin/python scripts/refresh_cookies.py --check-only
#   macOS asks to let Python use "Chrome Safe Storage" in your Keychain: enter your password, then click "Always Allow".
#   Expect "Cookies valid: league page loaded."
scripts/install-cookie-refresher.sh         # runs now, daily at 9:00 and 21:00, and at login
```

* Check it with `tail ~/Library/Logs/raccoon-museum-cookies.log`. A macOS notification appears if the cookies are invalid or the secret can't be updated.
* Run it immediately with `scripts/refresh-cookies-now.sh`, e.g. after an alert email (sign in to Yahoo Fantasy in Chrome first).
* If you use a non-default Chrome profile, add `--cookie-file "~/Library/Application Support/Google/Chrome/Profile 1/Cookies"` to `ProgramArguments` in the plist.
* Remove it with `scripts/uninstall-cookie-refresher.sh`.
* The Mac must be awake: launchd runs a missed 9:00/21:00 job when the Mac wakes up.

### 2. Run the first ingest

```bash
gh workflow run update.yml --repo k1kuma/raccoon-museum
gh run watch --repo k1kuma/raccoon-museum
```

The first run downloads every season, which can take a while. It is capped at about 5 hours. If it runs out of time, it saves its progress and the next run picks up where it left off. You can also run the first ingest locally (`python -m museum ingest`) and commit `data/league.sqlite`.

If the league ID 705220 belongs to an *older* season, not the current one, set the repository variable `YAHOO_GAME_ID` to that season's Yahoo game ID.

### 3. Map managers and add lore (optional, recommended)

Yahoo hides some managers' names (shown as `--hidden--`), and people change team names every year. To fix this:

1. Run `python -m museum managers` to list every season, team, GUID, and nickname.
2. Edit [`data/managers.yaml`](data/managers.yaml) to:
   * give each person a stable id and display name
   * merge hidden profiles (`teams: ["2017:4"]`)
   * add nicknames, bios, and lore

Push the change and the site rebuilds automatically. You can use [`data/sample_managers.yaml`](data/sample_managers.yaml) as an example.

### 4. GitHub Pages

Pages is set to **Settings → Pages → Source: GitHub Actions**. If a deploy ever fails with "Pages not enabled", switch that setting back on.

## Local development

```bash
source .venv/bin/activate
python -m museum sample          # or: python -m museum ingest   (needs .env)
python -m museum stats           # writes site/src/data/*.json
python -m pytest -q

cd site && npm install && npm run dev   # http://localhost:4321/raccoon-museum/
```

CLI reference: `python -m museum {sample|ingest|stats|managers} --help`. Useful `ingest` flags:
* `--season 2019`: only that season
* `--full`: ignore the cache
* `--delay`: seconds between API calls (default 0.35)
* `--time-budget`: minutes before stopping

## Repository layout

```
museum/            Python package: ingest (yfpy), stats, sample generator, OAuth helpers
scripts/auth.py    One-time Yahoo OAuth helper
data/              league.sqlite (committed once ingested), managers.yaml
site/              Astro site (pages in site/src/pages, team logos in site/public/logos)
.github/workflows  update.yml (weekly ingest + deploy), live.yml (game-day live scores), nfl-schedule.yml (daily kickoff times), ci.yml (PR checks)
```
