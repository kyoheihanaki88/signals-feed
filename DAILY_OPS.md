# Signals — Daily Feed Operations (solo creator)

> The biggest launch blocker is content operations, not code. This is the simplest sustainable workflow for **one person** to publish a real, honest, changing feed every morning in **under 10 minutes**.
>
> Signals is a **morning ritual product, not a news company.** The workflow stays light: five stories, curated by judgment, drafted with assistance, verified by a human, gated by the validator. Companion to `PUBLISHING.md` (the rules) and `validate_feed.py` (the gate).

---

## The 10-minute morning flow (at a glance)

| # | Step | Who | ~time |
|---|---|---|---|
| 1 | Skim 2–3 trusted sources, pick 5 stories, copy real article URLs | **You** | 3 min |
| 2 | Pick the Lead, assign an importance tier to each | **You** | 1 min |
| 3 | Draft summaries + takeaways + why-it-matters (assisted) into `latest.json` | LLM | 1 min |
| 4 | **Verify each summary against the source; edit voice** | **You** | 3 min |
| 5 | `python3 validate_feed.py latest.json` | script | 30 s |
| 6 | Deploy (commit + push → Vercel) | script | 30 s |

**Total ≈ 9 min.** The human owns *judgment and verification*; tools own *drafting, validation, deployment*. That split is what keeps it sustainable and honest.

---

## 1. Sourcing the five (≈3 min) — manual

Don't scan the world; scan a **small, trusted set**. Pick 2–3 sources you already read and stop there:

- An RSS reader (NetNewsWire / Feedly) with one folder of ~10 trusted feeds, **or** 2–3 bookmarked front pages (a wire/aggregator + a tech/business source + one you trust for the world).
- Skim the top items, pick the **five that matter most** by the ladder (below). Resist the urge to over-source — five is the product.
- **Copy the real article URL** for each as you go (deep link, not the homepage). Doing it now means no separate step later, and it's what the validator requires.

## 2. Selecting the Lead Signal (≈1 min) — manual

Ask one question: **"What is the single most important thing happening in the world today?"** Mark that story `lead: true`. Then tier each story:

1 = global emergency / war / major world event · 2 = geopolitics · 3 = economy / markets · 4 = transformational technology · 5 = other significant.

The Lead must be in the **highest tier present**. Apply the **swap test**: if the Lead could trade places with #3 and no one would notice, re-pick. On a genuinely quiet day, a Tier-4/5 Lead is honest — don't manufacture urgency (that's the "Quiet Day," and it's on-brand).

## 3. Creating summaries efficiently (≈1 min draft) — assisted

The slowest part by hand; make it a **draft, not a write-from-scratch.** Keep a saved prompt and run it once each morning:

> *"For each story below, write a Signals briefing in this exact JSON shape: `summary` (3–6 calm, factual sentences — no teaser, no clickbait, no hype), `keyTakeaways` (3 concrete points), `whyItMatters` (one quiet sentence). Use only the facts I provide; do not invent. Output the full `latest.json` with my `number`, `lead`, `importance`, `category`, `source`, `originalURL`, and today's `date`."*

Paste the five (your one-line gist of each + the URL + your lead/tier decisions). The LLM returns a complete `latest.json` in the schema. **It drafts; it never decides the lead or the tiers — you do.**

## 4. Collecting real article URLs — manual (done in step 1)

No separate step. You copied each story's real article URL while sourcing. The validator rejects homepages, so this is enforced, not optional.

## 5. Generating `latest.json` — assisted

The LLM in step 3 emits the whole file. Alternatively, copy yesterday's `latest.json` and replace fields. Either way, confirm: exactly one `lead: true`, an `importance` on each, today's `date`, enriched `summary` and real `originalURL` per story.

## 6. Validation (≈30 s) — automated

```
python3 validate_feed.py latest.json
```
Must print `✅ feed valid`. If it rejects (no/multiple lead, lead not highest tier, missing importance/summary, homepage URL, stale date), fix the flagged field and re-run. **Nothing publishes around the validator.**

## 7. Deployment (≈30 s) — automated

`signals-feed` repo → Vercel. One guarded command:
```
python3 validate_feed.py latest.json && git commit -am "feed $(date +%F)" && git push || echo "REJECTED — not published"
```
The push triggers Vercel's auto-deploy. The `&& … || abort` means a failing feed can never reach users, even by accident. (Ensure `Cache-Control: no-cache` on `latest.json` so the morning's feed propagates promptly — see `PUBLISHING.md`.)

---

## 8. What stays manual (the human core — never automate)

- **Story selection** — which five matter.
- **Lead choice + importance tiers** — editorial judgment.
- **Verification** — checking each drafted summary against the real article for accuracy and voice. *This is non-negotiable:* the product sells trust, and one hallucinated fact destroys it. The LLM drafts; you are accountable for every published word.

## 9. What can be automated (the mechanics)

- Summary/takeaway **drafting** (LLM, human-verified).
- `latest.json` **assembly** (LLM or template).
- **Validation** (`validate_feed.py`).
- **Deployment** (guarded git push → Vercel).
- **Date stamping** and the validate-then-deploy guard.

---

## Reliability for one person

- **Miss a morning?** The app keeps the **last valid feed** (honest, finite, not broken) and falls back to cache → bundle if needed — it never goes blank or fake. An occasional skip is survivable; chronic misses erode the "daily" promise.
- **Pre-batch on a strong day:** when you have time, prepare 2–3 days ahead and publish each at its date. Trade-off: same-morning curation is freshest and most honest; pre-batching buys resilience. Use it as a buffer, not the default.
- **Stable within the day:** publish **once** each morning, then leave it. Same `date` ⇒ same five, same order — no mid-day reshuffle (which would un-complete a finished morning). Fix only a broken field if a same-day correction is truly needed.
- **Quiet Day is allowed:** a calm news day means a calmer Lead. Honesty over manufactured importance.

---

## Why this stays true to the product

Five finite stories, curated by a person, verified for honesty, and stable for the day — published once at dawn and then set down. That is **"Read it all. Put it down."** as an operation: the reader can trust today's Signals are today's, read all five, and be done. The validator guarantees the honesty; the 10-minute ceiling guarantees the ritual is sustainable for the one person who keeps it. A briefing you can't maintain daily isn't a ritual — so the workflow's real job is to make *showing up every morning* effortless.

---

## Daily Auto Edition (v1)

A GitHub Actions pipeline that does the 10-minute flow automatically and **opens a PR for you to review** — it never merges or deploys, and it produces nothing at all on a weak/failed morning. Workflow: `.github/workflows/daily-auto-edition.yml`.

### How it works

Runs at **17:00 UTC** daily (before the 20:30 UTC Tokyo-5:30 deadline, with buffer) and builds the edition dated **tomorrow-UTC** (the morning it serves):

```
Scout (live RSS)   →  candidates.json
Ranker             →  selection.yaml   (deterministic, rule-based — 1 Lead + 4 Supporting)
selection.py build →  selection.json   (validates picks, metadata only)
writer.py draft    →  drafts.json      (extractive — copies real source text, never invents)
writer.py validate →  drafts gate
build.py --date    →  generated/latest.draft.json  (+ validate_feed.py on the draft)
publish.py --write →  editions/<date>.json + latest.json  (+ --consistency, regression guard)
create-pull-request→  review PR  (NOT merged)
```

The **Ranker** (`pipeline/ranker.py`) is deterministic — no LLM. It prefers cross-source clusters (importance), reliable outlets (BBC, NPR, Guardian, FT, The Verge, Al Jazeera), and recent stories; it requires real article URLs (no homepages/videos), avoids live blogs unless nothing better exists, takes one story per cluster, and spreads categories so the five aren't all the same kind. The Lead prefers global-urgency categories (WORLD → economy → major tech → institutional). Category labels come from the *source feed*, so a human still confirms the Lead's editorial fit in the PR.

### Run it manually

Trigger the whole pipeline from the Actions tab → **Daily Auto Edition** → *Run workflow* (optional `date` input). Or run the stages locally:

```
python3 pipeline/scout.py  --sources pipeline/sources.yaml --out pipeline/candidates.json --max-age-hours 36
python3 pipeline/ranker.py --candidates pipeline/candidates.json --out pipeline/selection.yaml
python3 pipeline/selection.py build
python3 pipeline/writer.py draft        # add --no-fetch when offline (uses RSS snippets)
python3 pipeline/writer.py validate
python3 pipeline/build.py   --date $(date -u -d '+1 day' +%F)
python3 pipeline/publish.py              # DRY-RUN; --apply for a local branch+commit, --write for files only
```

(`build-edition.yml` is the older **manual-only** helper that builds from committed drafts; the scheduled automation is now `daily-auto-edition.yml`.)

### Safety gates

The job **stops before the PR step** (and touches nothing) if any of these fail:

- fewer than **20** candidates, or fewer than **5** real article URLs *(Ranker)*
- **no lead-quality story** found, or fewer than 4 eligible supporting stories *(Ranker)*
- a selected story has **no real article URL**, or **duplicate URLs** *(Ranker / selection.py)*
- drafts fail validation — `needs_review` / `source_unavailable` / `thin_source` *(writer.py validate)*
- the built **edition date doesn't match** the target *(date-match gate)*
- **draft or feed validation fails** *(build.py / validate_feed.py)*, or the consistency/regression guard trips *(publish.py)*

Because every step is a hard gate and the PR is the last step, a failure leaves `latest.json` and existing editions untouched. A weak morning simply produces no PR — never a stale or fabricated edition.

### What to do when it fails

1. Open the failed run → read the step summary (candidate count, the chosen Lead/Supporting, or the **skip reason**).
2. If it stopped at the **Ranker** (thin Scout / no lead-quality story) → it's an honest quiet/dry morning. Either let it skip (the app keeps the last valid edition) or curate by hand: fill `pipeline/selection.yaml` yourself and run `selection.py build → writer → build → publish.py --apply`, then open the PR.
3. If it stopped at **Writer/validate** → a source body couldn't be extracted cleanly. Re-run, or hand-edit the flagged draft, then continue.
4. If it stopped at **feed/consistency** → fix the offending field and rebuild; never bypass the validator.
5. Never merge a PR you haven't read — confirm the Lead is right and the five are today's.
