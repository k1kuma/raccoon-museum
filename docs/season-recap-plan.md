# Season Recap ("Raccoon Wrapped") — implementation plan

Status: **plan only**. Target: build in early December, unlock after the fantasy championship week (week 17, late December).

## Goal

A per-manager, Spotify Wrapped / Instagram Stories style recap of the season. It is a full-screen, vertical (9:16) sequence of slides with tap-to-advance, a segmented progress bar, and animated stat reveals. Every slide can be shared as a 1080×1920 image, so people post them to the group chat.

## User experience

* **Entry points:** a Lobby hero card ("Your 2026 Wrapped is here") from the championship week until the next draft, a link on each manager page, and the "Who are you?" picker, which opens your own recap directly.
* **URL per manager and slide:** `/wrapped/2026/<manager>/` with `#3` (or `/wrapped/2026/<manager>/3/` as a static fallback page per slide) so a shared link opens on that slide.
* **Controls:**
  * Tap the right ⅔ of the screen to go forward and the left ⅓ to go back. Swipe left/right does the same.
  * Press and hold to pause.
  * Arrow keys and Space work on desktop, and **Esc** closes.
  * The progress bar at the top shows one segment per slide and auto-advances after about 6 s. Auto-advance is off under `prefers-reduced-motion`.
* **Desktop:** the 9:16 story is centred, with blurred themed side panels and on-screen ◀ ▶ buttons.
* **Animated reveals:** count-up numbers, bars that grow in, and staggered text. Use CSS transitions only (`@keyframes` plus `animation-delay`), with no animation library. Under `prefers-reduced-motion` everything shows instantly.
* **Share button on every slide:** uses `navigator.share({ files: [png] })` where supported (iOS/Android), and falls back to downloading the PNG plus copying the slide URL.
* **League recap:** a final "league" deck at `/wrapped/2026/` (champion, best and worst teams, biggest blowout, most lopsided trade) for people who aren't in the picker.

## Slides (per manager)

| # | Slide | Content | Data source |
|---|---|---|---|
| 1 | Intro | Team logo, team name, "Your 2026 season" | `managers`, logos |
| 2 | Season in numbers | Regular-season W-L, points for, rank among 12, finish (seed and final place) | `seasons[].standings` |
| 3 | Best week | Highest score, opponent, margin, league rank of that score that week | `weekly` (final weeks only) |
| 4 | Worst week | Lowest score, plus "the league median that week was X" | `weekly`, median per week |
| 5 | MVP player | Top starter by points, with share of team points | carry share |
| 6 | Carry share | One-man-team index plus top-3 share, compared with the league | carry share |
| 7 | Luckiest win | Win with the lowest score, or the biggest all-play gap | all-play, `weekly` |
| 8 | Toughest loss | Loss with the highest score ("You scored 2nd most and still lost") | `weekly`, Hall of Shame |
| 9 | Rivalry | Season result against your most-played or closest rival, plus the all-time H2H after this season | rivalry H2H |
| 10 | Draft grade | Letter grade, best steal, worst bust, and kept value | `draft_grades` |
| 11 | Median record | Record vs median, the true record, and luck (+/− wins) | median/true record, luck |
| 12 | You vs the league | 3 comparisons in percentiles ("More bench points than 10 of 11 managers") | derived |
| 13 | Awards | Weekly awards won this season (Manager of the Week ×3, …) | `awards` |
| 14 | Playoffs | Bracket path, or "Toilet Bowl" result, with the clutch score | playoffs, consolation |
| 15 | Summary card | A grid of 6 headline stats, plus the grade and logo: the one people share | all |

Slides are skipped when their data is missing (e.g. no draft data, or no playoff appearance makes slide 14 a Toilet Bowl slide or skips it). Captions use the existing story-style tone.

## Data requirements

* **New `wrapped` section in `stats.json`, built by `museum/wrapped.py`:** `{season: {manager_id: [slide…]}}`.
  * Each slide is `{kind, title, caption, stats:{…}}`, so the page and the image generator share one source of truth.
  * Built only from **final** regular-season, playoff and consolation weeks; live partial weeks are excluded, as elsewhere.
  * Built only when the season's championship week is final (`season_complete`). Before that the section is empty and the pages aren't generated.
  * Percentile comparisons use that season's 12 teams.
* **Mostly existing data:** the only new computations are the percentiles, the "lowest-score win" and "highest-score loss" (some already exist in the Hall of Shame), and a season-scoped awards tally.
* **Tests:** slide selection and skipping, percentile maths, and the empty section before the season completes.

## Components

* `site/src/pages/wrapped/[season]/[manager].astro`: a static shell that renders every slide as server HTML, so it works without JS as a vertical scroll of cards.
* `site/src/components/wrapped/StoryPlayer.tsx`: a small Preact island (`client:load`) for progress, timers, tap/swipe/keyboard and share. No new dependencies (pointer events only). Budget under 8 kB gzipped.
* `site/src/components/wrapped/Slide*.astro`: one component per slide kind, styled with the existing theme tokens (charcoal/red, condensed headings), with a bolder per-slide background gradient.
* **Share images:** `site/src/pages/og/wrapped/[season]/[manager]/[n].png.ts`, extending the existing satori + resvg OG pipeline at 1080×1920. Reuse the fonts and embedded base64 logos. Cost is about 12 managers × 15 slides = 180 PNGs, about 15 MB and +20 s of build time per season.
  * Generate them only in the Tuesday (`update.yml`) run, and skip them in live runs with an env flag, so live builds stay fast.
* **Meta:** `og:image` for each slide URL points to its PNG, so pasting a link unfurls the right slide.

## Timeline

| When | Work |
|---|---|
| Early Dec (≈ week 13) | `museum/wrapped.py` + tests, built against the 2025 season (complete) as fixture data |
| Week 14 | StoryPlayer island + slide components; review with 2025 data behind an unlisted URL |
| Week 15 (playoffs start) | Share PNGs, mobile audit (375/768/1024, iOS Safari share sheet), performance check |
| Week 16 | Copy polish; Lobby teaser "Wrapped unlocks after the championship" |
| After week 17 is final (Tuesday run) | `season_complete` flips → pages and images build automatically; Lobby hero card appears |

As a bonus, 2024 and 2025 Wrapped pages can ship immediately as "Throwback Wrapped".

## Open questions

1. **Scope:** per-manager only, or also the league deck? Recommendation: both, but the league deck can come later.
2. **Spoilers:** should recaps unlock for everyone at once, or per manager once their season ends (eliminated teams earlier)? Recommendation: all at once.
3. **Roasts:** how savage should the captions be? Do we need a "nice mode"?
4. **Music or sound:** none (autoplay restrictions, and it would add weight). Agree?
5. **Throwback decks for 2024–2025:** build them in December too?
6. **Share target:** a mix of iMessage, Instagram stories and Discord. Is 1080×1920 enough, or do we also want a 1080×1080 summary for Discord embeds?
7. **Privacy:** recaps are public like the rest of the site. Is that OK for everyone in the league?
