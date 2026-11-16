# Analytics & heatmaps — plan

Status: **plan only**. Goal: see which pages, sections and controls people actually use (heatmaps, scroll depth, clicks), so later iterations can remove or simplify what nobody touches.

Context: the site is static (Astro on GitHub Pages) with no login and a small, known audience (a 12-person league plus friends). That means tens to a few hundred visits a week, nowhere near any free-tier limit.

## Options

| | PostHog Cloud (free) | Microsoft Clarity | Plausible / Umami |
|---|---|---|---|
| Cost | Free up to 1M events/mo (plus 5k replays) | Free, unlimited | Plausible from about $9/mo; Umami Cloud free tier, or self-host |
| Heatmaps (click / scroll) | ✅ via toolbar | ✅ the core feature | ❌ |
| Session replay | ✅ (input masking) | ✅ | ❌ |
| Autocapture clicks | ✅ | ✅ (no event querying) | ❌ |
| Custom events with properties | ✅ rich: funnels, trends, breakdowns | ⚠️ basic (custom tags) | ✅ simple goals |
| Cookieless mode | ✅ `persistence: 'memory'` | ❌ sets cookies by default (consent API exists) | ✅ by design |
| Script weight | ~50 kB gz (lazy loaded) | ~25 kB | <2 kB |
| Setup effort | Low | Lowest (paste one tag) | Lowest |

**Recommendation: PostHog Cloud (US or EU), free tier.**
* It's the only option that gives heatmaps **and** queryable custom events (e.g. "which Record Book segment do people pick?") in one place.
* Cookieless memory persistence means no consent banner is needed.

**Zero-effort alternative: Microsoft Clarity.**
* Paste one tag and you get unlimited heatmaps and recordings, but shallow event analysis.
* It sets cookies, so it would need its consent mode or a small notice.

Plausible or Umami only count pageviews, so they don't meet the heatmap goal.

## Integration (PostHog)

1. **Config:** create a PostHog project and copy the **public** project key (`phc_…`). It's designed to be public, so it's fine to keep as a GitHub Actions **variable** `PUBLIC_POSTHOG_KEY` (plus `PUBLIC_POSTHOG_HOST`, e.g. `https://us.i.posthog.com`). Pass both to `npm run build` in `update.yml` and `live.yml`. No key means no script, so local and dev builds stay clean.
2. **Snippet:** one snippet in `site/src/layouts/Base.astro`, inside `<head>`, rendered only when the key is set.
   * Use PostHog's standard async loader snippet: it queues calls and loads `array.js` asynchronously.
   * Initialise with:
     ```js
     posthog.init(KEY, {
       api_host: HOST,
       persistence: 'memory',          // cookieless: no cookies/localStorage, no banner
       autocapture: true,              // clicks on links/buttons/inputs, incl. inside islands
       capture_pageview: true,
       capture_pageleave: true,        // enables scroll depth on leave
       enable_heatmaps: true,
       session_recording: { maskAllInputs: true },
       respect_dnt: true,
     });
     ```
   * Load it after `requestIdleCallback` (fallback: `load`) so it never blocks rendering or LCP.
3. **Islands and nav:**
   * Autocapture works on the DOM, so it covers the Preact/visx chart islands and the hamburger nav as long as they use real `<button>`/`<a>` elements, which they do.
   * Add `data-ph` attributes (e.g. `data-ph="nav-toggle"`) for stable selectors, so heatmaps survive CSS changes.
   * For chart hovers (not clicks), fire explicit events (below), throttled to one per chart per page view.
4. **Owner opt-out:** visiting any page with `?notrack=1` sets `localStorage.museumNoTrack = '1'`; `?notrack=0` clears it. The snippet checks the flag *before* loading the script, so opted-out visits make no network requests. Bookmark the site once with `?notrack=1` on each of your devices.
5. **Base path:** the site lives under `/raccoon-museum/`. Set up URL grouping by path patterns (`/managers/*`, `/records/*`, `/fun/week/*`) so dashboards group dynamic pages.

## What to measure

Use the automatic data: pageviews per page or path group, scroll depth (`$pageleave` → max scroll percentage), click heatmaps per template, and outbound link clicks.

### Event naming

Use the `object_action` scheme in snake_case, past tense: `segment_toggled`, `tooltip_opened`. Properties are snake_case. Every event carries `page_type` (lobby / season / records / manager / rivalry / shame / archive / fun) from a `data-page-type` attribute on `<body>`.

### Custom events (small list; add with a tiny `track(name, props)` helper that does nothing when PostHog is absent)

| Event | Properties | Why |
|---|---|---|
| `segment_toggled` | `page_type`, `segment` (regular/playoffs/all) | Is the Regular/Playoffs/All toggle used? |
| `columns_toggled` | `table`, `columns` (median/avg/true) | Are optional standings columns worth keeping? |
| `season_picked` | `component` (trend chart, archives…), `season` | Do people look at past seasons? |
| `odds_metric_selected` | `metric` (playoff/bye/title/last) | Which odds view matters |
| `tooltip_opened` | `tip` (true_record, luck, all_play, clutch, power_weights…) | Are explanations needed or ignored? |
| `chart_interacted` | `chart` (trend, carry, odds, leaderboard), `interaction` (hover/tap) | Throttled once per chart per view |
| `rivalry_compared` | `pair` (manager ids) | Rivalry Room usage |
| `whoami_set` | `manager` | Is the "Who are you?" picker used? |
| `leaderboard_viewed` | `slug`, `segment` | Fired by IntersectionObserver when a Record Book / Hall of Shame leaderboard is 50% visible for 1 s |
| `share_clicked` | `target` (copy link / native share), `page_type` | Are share links used? |
| `nav_clicked` | `item`, `menu` (top/hamburger) | Autocapture covers this, but an explicit event makes the dashboards cleaner |

## Privacy

* There are no accounts, logins, names or emails collected. Session recordings mask all inputs (the only input is the manager picker). Page text stays visible because it's public stats anyway.
* **Cookie banner:** none needed with `persistence: 'memory'`, because PostHog sets no cookies and no localStorage, and each page view is anonymous. The trade-off is that cross-page sessions and returning-visitor counts are approximate (each full page load is a new anonymous id). For a small static site this is acceptable.
  * If cross-page journeys turn out to matter, switch to `localStorage` persistence and add a one-line notice in the footer.
* `respect_dnt: true`. Add a short "Privacy" line in the footer: "Anonymous usage stats via PostHog; add `?notrack=1` to opt out."
* **Undercounting:** ad-blockers (uBlock and the Brave shield) block PostHog and Clarity. With a small, tech-savvy audience, expect 20–40% of visits missing. Treat the numbers as relative (which page beats which), not absolute. A PostHog reverse proxy would fix this but needs a server, so it's out of scope.
* With about 12 regular users, one person can skew results. Look at counts of distinct sessions, not raw clicks.

## Review cadence and decisions

1. **Ship it and wait about 4 weeks.** Ideally include a Sunday live window and a Tuesday awards drop.
2. **Week 4 review (≈ 30 min):** build a PostHog dashboard with pageviews per path group, median scroll depth per template, the custom event counts above, and click heatmaps for the Lobby, Record Book, a manager page and This Season.
3. **Decision rules (guidelines, not hard thresholds):**
   * **Page with fewer than 3 distinct sessions in 4 weeks, and not linked from shares:** candidate to merge into a parent page or remove.
   * **Section below the 25th-percentile scroll depth on its page, with no clicks:** move it lower or collapse it behind a "More" disclosure.
   * **Toggle or tooltip never used (0–1 events):** remove the control and keep the default view.
   * **Heavily used features:** promote them in the Lobby and nav, and consider deeper versions.
   * **Before removing anything:** check it isn't seasonal (e.g. playoff odds matter in weeks 10–14, Wrapped in December).
4. **Re-review:** after the playoffs, then once in the off-season. Turn session replay off if nobody watches recordings.
5. **Removal:** remove the tracking entirely if it isn't providing value after 2 reviews.
