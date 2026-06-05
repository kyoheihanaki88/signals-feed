# Signals — Daily Feed Publishing Workflow

> The single rule: **every published `latest.json` must pass `validate_feed.py` before it reaches users.** The validator is the gate that protects the Lead Signal Rule and the Trust Pass. A feed that fails is never published.
>
> Governing principles: Master Context §8 (Lead Signal Rule) · §"Core Philosophy" ("Read it all. Put it down."). The editor chooses the lead; the validator protects the principle.

---

## The daily ritual

Run this once each morning, **before users wake** (e.g. a fixed early local/UTC time). Publish once per day; do not change the feed again that day (see "Stable content," below).

1. **Curate five stories.** Exactly five — the day's signals. Finite by design.
2. **Assign exactly one `lead: true`.** The single "if you read one thing today" story. Every other story is `lead: false`.
3. **Assign an `importance` tier (1–5) to every story** by the ladder: 1 = global emergency / war / major world event · 2 = geopolitics · 3 = economy / markets · 4 = transformational technology · 5 = other significant. Tier = the *significance of the event*, not its category.
4. **Confirm Signal 01 is the highest tier present.** The `lead: true` story's `importance` must equal the lowest (highest-priority) importance among the five. If a higher-tier story exists, the lead is wrong — re-pick.
5. **Confirm enriched summaries exist** — a real 3–6 sentence briefing on every story (no teaser/clickbait).
6. **Confirm `originalURL` is a real article URL, not a homepage** — a deep link to the actual story (has a path), https.
7. **Confirm `date` is today** (`YYYY-MM-DD`).
8. **Run the validator:**
   ```
   python3 validate_feed.py latest.json
   ```
9. **Publish `latest.json` only if validation passes** (exit code 0 / `✅ feed valid`). If it exits non-zero, do **not** publish — fix and re-run (below).

`number` sets the editorial order of the supporting four (02–05); the app never re-sorts them. `importance` is used only for validation, never as a display sort key.

### Suggested guard (manual or CI)
```
python3 validate_feed.py latest.json && <deploy/commit> || echo "REJECTED — not published"
```
Wire the same check as a **pre-deploy step** (e.g. a CI job or a git pre-push hook on the `signals-feed` repo) so a failing feed can never be deployed even by accident.

---

## When validation fails

- **The publish aborts.** `latest.json` on the host is **not** updated. The validator prints each violation (e.g. "lead not in highest tier", "homepage URL", "stale date").
- **Who fixes it:** the **editor/curator** who assembled the feed corrects the flagged fields — re-pick the lead, fix the URL, add the missing summary, set today's date — and **re-runs `validate_feed.py` until it passes**, then publishes. No one publishes around the validator.
- **What users see meanwhile:** because the publish aborted, the **last valid `latest.json` stays live.** If today's feed failed, users keep yesterday's five (honest, not fake) until the editor fixes and republishes. That is a far better degraded state than a broken, empty, or fabricated feed.

---

## App-side behavior (for context — no app change here)

The app's fetch is **offline-first and date-keyed**, with a fallback chain that backs up the host:

- **Previous valid feed is preserved:** if a new feed isn't published (or is unreachable), the app keeps showing the last good content via its chain: **live cache → bundled `fallback.json` → SampleData.** It never goes blank.
- **Freshness key = the feed's `date`:** the app accepts a fetched feed when its `date` is today; otherwise it keeps what it has.

---

## Stable content within the same day (no mid-day reshuffling)

This is a hard requirement, not a nicety — it protects completion.

- **Publish once per morning, then leave it alone.** Do not re-publish a changed feed later the same day.
- **Same `date` ⇒ same five, same order.** The lead (`lead: true`) and the supporting order (`number`) are fixed for the day, so a user who relaunches at noon sees exactly the morning's five — no shuffle.
- **Daily Completion stays intact.** `ReadStore` is day-keyed; if the five changed after a user finished, their completed morning would silently revert to "incomplete." Stability prevents that.
- **If a same-day correction is unavoidable** (e.g. a broken URL), fix **only the broken field** — never change which five stories appear, their order, or the lead. Reshuffling mid-day is prohibited; a minimal field correction is the only exception.
- **Edge caching must allow same-day swaps to propagate but not serve stale across days:** set `Cache-Control: no-cache` (or short `must-revalidate`) on `latest.json` at the host (Vercel `vercel.json`), so the next morning's feed is picked up promptly while the within-day feed stays consistent.

---

## How this supports "Read it all. Put it down."

- **Finite and stable:** five stories, fixed for the day. The user can read all five, reach the end, and be *done* — the order won't move under them, so completion means completion.
- **A clear lead = permission to stop:** Signal 01 is genuinely the one story that matters most, so even a 20-second morning ("just the lead") feels complete, not like skipping a feed.
- **New each morning, never churning all day:** freshness arrives once, at dawn, then the world is set down. The app doesn't manufacture mid-day updates to pull users back — that would be engagement, not relief.
- **Honest by gate:** the validator guarantees real articles, real summaries, today's date, and a true lead — so "today's Signals" are actually today's, curated, and trustworthy. Trust is what lets a user *put it down*.

The validator is the mechanism; completion is the value. A feed that doesn't pass doesn't ship.
