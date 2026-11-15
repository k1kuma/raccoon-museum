# 🦝 City Slickin' Raccoons — Fantasy Football Museum

A public, static museum for the Yahoo Fantasy Football league
[City Slickin' Raccoons (705220)](https://football.fantasysports.yahoo.com/f1/705220).
It updates itself every Tuesday morning during the season.

**Live site:** https://k1kuma.github.io/raccoon-museum/

| Wing | What's on display |
| --- | --- |
| **Lobby** | This week's headlines, records broken, active streaks, "on this week in history", standings, fiercest rivalries, lore |
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
  2. Commits `data/league.sqlite`.
  3. Recomputes the stats.
  4. Builds and deploys to Pages.

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
site/              Astro site (pages in site/src/pages)
.github/workflows  update.yml (weekly ingest + deploy), ci.yml (PR checks)
```
