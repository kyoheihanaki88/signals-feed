# ADR-0001 — Local-time morning delivery

Status: Accepted · Date: 2026-06-10

## Context

Signals is a morning-ritual app. The promise is simple:

> **Today's edition is available by 5:30 AM in the user's own local time.**

A reader in New York should have today's edition by 5:30 AM ET; a reader in Tokyo by
5:30 AM JST. The old pipeline dated the feed in **Asia/Tokyo** (`build.py`, `publish.py`,
`validate_feed.py` all read "today" in JST) and shipped a single `latest.json`. That hard-codes
one timezone's notion of "today" into the server and the validator, which is wrong for a global
audience and brittle across the date line.

## Decision

**"Today" is a client decision, not a server decision.** The server publishes immutable,
date-labelled editions early enough; each device selects the edition that matches *its own*
local date.

### 1. Date-keyed editions + a `latest.json` pointer

```
/editions/2026-06-10.json     # immutable once published; "date" is a plain label
/editions/2026-06-09.json     # yesterday stays available (fallback for users still in "yesterday")
/latest.json                  # a copy of the newest edition — pointer + fallback
```

The edition's `date` field is a plain `YYYY-MM-DD` **label** for the morning it serves. It carries
no timezone. The server never decides whose "today" it is. `latest.json` always equals the newest
edition by date.

### 2. One global edition per day

Same editorial content for everyone. The client chooses *which date's* edition to show based on the
device's local date. (No per-region content, no per-timezone editions.)

### 3. The client owns local-date selection (iOS)

On launch / foreground:

```
localToday = YYYY-MM-DD in TimeZone.current        # device truth, never hard-coded
GET /editions/<localToday>.json
  → 200 + date == localToday   → today's edition. cache it.
  → 404 / not published yet      → GET /latest.json (newest available) or cached edition.
```

Offline-first and always-refresh-on-launch are preserved: the cache (or bundled fallback) paints
the UI instantly, and the network fetch revalidates in the background. The app is never blank, and
`Asia/Tokyo` does not appear anywhere in the app.

### 4. The 5:30 guarantee is a publish-*deadline*, not code

The earliest 5:30 AM for date `D` happens in the easternmost supported timezone. We commit to
**Tokyo (UTC+9)** as the easternmost morning:

```
5:30 AM Tokyo (UTC+9)  =  20:30 UTC the previous day
```

So edition `D` must be merged + deployed **before ~20:30 UTC on D−1**. The scheduled workflow
generates the edition and opens the PR at **17:00 UTC** (D−1), leaving a buffer for human review
and merge before the deadline. Every timezone west of Tokyo then receives edition `D` before *their*
5:30. This deadline is enforced by **CI timing + monitoring**, not by any timezone literal in code.

Edition date label = **tomorrow in UTC** at the 17:00 UTC build time (which equals "today in Tokyo"
during the evening-UTC publish window) — overridable with `--date`. No `Asia/Tokyo` import.

## Validation (timezone-agnostic)

`validate_feed.py` enforces internal consistency and recency, never a single clock:

- `date` is a valid `YYYY-MM-DD`.
- For `editions/<D>.json`, the file's `date` must equal the filename `D`.
- `--consistency`: `latest.json` equals the newest `editions/*.json` (by date), and is not older
  than it (no stale regression).
- Loose UTC sanity (optional, lenient): the publish date is within a few days of UTC `today`
  — catches a wildly stale or far-future build without pinning a timezone.
- All existing structural rules stay: exactly 5 signals, one lead in the highest tier, importance
  1–5, non-empty summaries, integer `readTime`, real `https` article URLs.

The Asia/Tokyo grace window is removed.

## Publish flow

```
draft (build.py, date = tomorrow-UTC or --date)
  → publish.py validates + writes BOTH editions/<date>.json AND latest.json
      · DRY-RUN by default
      · --apply : local publish/<date> branch + commit (no push/PR) — manual Mac flow
      · --write : write the two files in place, no git — used by CI, which opens the PR
  → PR to main → validate-feed CI (per-file + --consistency) → human merge → Vercel deploys
```

`publish.py` refuses to publish a date **older** than the current `latest.json` (regression guard).
Re-publishing the *same* day (a correction) is allowed.

## Caching (iOS)

The feed is cached by its edition `date`:

- cached `date == localToday` → show immediately, revalidate in background.
- cached `date <  localToday` → fetch today's edition; on failure keep the cached one (shows
  yesterday — honest, never blank).
- Network requests bypass the local/CDN cache (`reloadIgnoringLocalCacheData`) so a same-day
  re-publish actually reaches the app.

## Consequences

**Good:** the server stays dumb static JSON (date-keyed files + a pointer) — no edge functions, no
`?tz=` params, no geo, no per-user logic. The client owns "what is today," which is where timezone
truth actually lives. One edition/day keeps editorial + build cost flat. Travel "just works" (each
launch recomputes the local date). The ritual is never blank.

**Honest edge cases:** a user east of the easternmost deadline, or awake before 5:30 local, may
briefly see *yesterday's* edition until today's lands — correct behaviour for a "by 5:30" promise,
and the fallback means it's never blank.

## Alternatives considered

- **Per-timezone editions** — multiplies editorial, build, and validation cost for a single global
  brief. Rejected for complexity.
- **Server-side selection by client timezone** (`?tz=` / geo edge function) — adds a moving server
  target the client could decide better itself. Rejected; keep the server static.
- **Rolling `latest.json` only, no date keys** — simplest, but loses the local-date match near the
  date line and can't tell whether a user is even seeing the right day. The date-keyed model is
  barely more complex and far more correct.
