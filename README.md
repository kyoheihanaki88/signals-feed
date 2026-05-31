# signals-feed

The daily content feed for **Signals** — a premium iOS "personal morning operating system" ("Your morning, organized.").

This repository hosts a single file, **`latest.json`**, that the Signals app fetches each morning to populate the day's five signals. It is intentionally tiny, static, and dependency-free.

---

## What this repo is

- A **static JSON feed**. No backend, no database, no server code.
- `latest.json` is the live document the app reads. It is overwritten each day.
- `archive/` keeps a dated copy of every published day for history and rollback.
- `scripts/validate-feed.js` checks a feed file against the app's schema before you deploy.

---

## How `latest.json` is used by the Signals app

- The iOS app's `LiveSignalsService` performs an HTTPS GET of `latest.json` once per day, decodes it into the `SignalsFeed` model, and shows the five signals on the Home screen (Signal 01 = the lead/hero, 02–05 = supporting).
- **Fallback chain (the app is never blank):** today's cached feed → this `latest.json` → the app's bundled `fallback.json` → built-in `SampleData`. If the feed is missing, malformed, or the device is offline, the app silently falls back — no error, no empty state.
- The app enforces **exactly five signals** and the **Free = MIXED** model. `latest.json` must respect both.

> Note: Signal 01's image on Home is replaced by the app's local photography pool; the feed's `imageURL` for signal 1 still matters for the Article view, and 02–05 use their feed `imageURL` directly.

---

## Deployment target (Vercel)

- **Live URL (target):** `https://signals-feed.vercel.app/latest.json`
- Deployment is via Vercel static hosting connected to this repo: a `git push` to the default branch auto-deploys, and the new `latest.json` propagates over Vercel's edge CDN within seconds.
- HTTPS is automatic (required by the app's App Transport Security).
- The host can later be swapped for Cloudflare Pages or a custom domain — only the app's `feedURL` string changes; this repo's structure stays the same.

> Not deployed yet. Once deployed and the URL resolves, the iOS app's `LiveSignalsService.feedURL` is set to it (that's the app-side step "C1").

---

## Daily publishing workflow

1. **Curate** the five signals for the day (1 lead + 4 supporting; mixed categories).
2. **Edit `latest.json`** — update `date`, keep `focus: "MIXED"`, fill all five signals (see schema below).
3. **Validate:** `node scripts/validate-feed.js` (must print `✓ VALID`).
4. **Archive:** copy the validated file to `archive/YYYY-MM-DD.json` (matching `date`).
5. **Publish:** commit + push. Vercel auto-deploys; the app picks it up on its next daily fetch.

---

## Schema rules (`SignalsFeed`)

Top level:

| field | type | rule |
|---|---|---|
| `date` | string | `"YYYY-MM-DD"` |
| `focus` | string | must be `"MIXED"` |
| `version` | integer | schema version (currently `1`) |
| `signals` | array | **exactly 5** items |

Each signal:

| field | type | rule |
|---|---|---|
| `number` | integer | 1–5, unique (the set 1,2,3,4,5) |
| `lead` | boolean | **exactly one** signal is `true` (signal 1) |
| `category` | string | e.g. BUSINESS, AI, SCIENCE, CULTURE, MARKETS |
| `source` | string | e.g. BLOOMBERG, THE VERGE |
| `headline` | string | the signal title |
| `summary` | string | one-line summary |
| `keyTakeaways` | string[] | **exactly 3** items |
| `whyItMatters` | string | one short paragraph |
| `originalURL` | string | non-empty HTTPS link |
| `readTime` | integer | minutes |
| `imageURL` | string | non-empty HTTPS image link |
| `placeTime` | string \| null | optional (lead often `"CITY · 7:14 AM"`) |
| `audioURL` | string \| null | optional; **may be null** (audio is not functional in V1) |

---

## How to validate before deploy

```bash
node scripts/validate-feed.js            # validates ./latest.json
node scripts/validate-feed.js archive/2026-05-30.json   # validate a specific file
```

The script checks: valid JSON · exactly five signals · numbers 1–5 (unique) · exactly one lead · `focus` is MIXED · all required fields present · `keyTakeaways` has exactly 3 items per signal · `imageURL` present · `originalURL` present · `audioURL` may be null. Exit code `0` = valid, `1` = invalid (with a list of problems). No network, no dependencies.

---

## Repository layout

```
signals-feed/
├── latest.json              # the live feed (overwritten daily)
├── archive/
│   └── 2026-05-30.json      # dated copies (history / rollback)
├── README.md                # this file
└── scripts/
    └── validate-feed.js     # pre-deploy schema validation (Node, no deps)
```
