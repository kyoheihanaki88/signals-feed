# Signals — Automated Morning Feed Pipeline (technical plan)

> Plan only — no implementation. Goal: produce **one valid `latest.json` every morning** with minimal human time, **without ever risking fake or hallucinated content.**
>
> The hard constraint shapes everything: Signals is a *trust* product. Fully auto-publishing AI-written summaries is the single biggest risk (one hallucinated fact destroys the brand). So the recommended 1.0 is **automated draft + a 30-second human approval gate, hold-last-valid by default.** Automation removes the *labor*; the human removes the *risk*.

---

## 1. Recommended architecture

A daily scheduled pipeline, with a **Pull Request as the review-and-publish surface**:

```
[cron ~05:00] →  Scout  →  Ranker  →  Editor  →  Writer  →  validate_feed.py
                                                                  │ green
                                                                  ▼
                                              opens a PR with draft latest.json + preview
                                                                  │
                                   you merge (PUBLISH) ───────────┤
                                   you ignore (HOLD last valid) ──┘
                                                                  │ merge → Vercel auto-deploys
```

- **Scout** — pull candidate stories from trusted **RSS feeds** (real article URLs, no scraping). Dedupe + cluster similar stories; cross-source overlap = an importance signal.
- **Ranker** — an LLM assigns the importance tier (the locked ladder) and *suggests* the Lead; prefers multi-source, global significance; rejects hype/outrage/gossip.
- **Editor** — selects exactly five (1 Lead + 4 Supporting, category-balanced: world · economy · tech · Japan/culture · other). Lead = highest tier present.
- **Writer** — drafts each Signal's fields **only from the real article text** (or the RSS description when the article is paywalled), in the calm Signals voice. Flags anything unreadable.
- **Validator** — `validate_feed.py` (already built) is the gate. Red = no PR / no publish.
- **Publisher** — a merged PR triggers Vercel's auto-deploy of `latest.json`. Once per morning; never reshuffled; on any failure the last valid feed stays live.

The **PR is the elegant part**: it's the review surface (preview in the description), the approval mechanism (merge = publish), the audit trail, and the hold-by-default (no merge = yesterday's feed stays) — all with zero custom server or email infra, reviewable from GitHub mobile.

---

## 2. Minimum viable version for 1.0

Don't build full autonomy first. The MVP:

1. **Scout** fetches RSS from ~5 open-access sources, dedupes, clusters → a candidate list with real URLs.
2. **Writer** (LLM) drafts summaries/takeaways/why-it-matters from each article's real text.
3. The pipeline **assembles a complete draft `latest.json`** with the LLM's *suggested* lead + tiers, runs the **validator**, and **opens a PR** with a one-screen preview (Lead + 4 Supporting + warnings + validator status).
4. **You review on your phone in ~30 sec and merge to publish** — or do nothing, and the last valid feed holds.

This automates the two time-sinks (gathering + summarizing) and keeps the human as a fast veto on the lead and on accuracy. The Ranker/Editor can start as *suggestions you approve*; fuller automation comes later once the pipeline earns trust.

---

## 3. Tools / libraries

- **Python**: `feedparser` (RSS), `httpx`/`requests` (fetch readable articles), `trafilatura` or `readability-lxml` (extract clean article text from HTML), plus the existing `validate_feed.py`.
- **LLM**: Claude API (Anthropic) for ranking + writing, under strict "summarize **only** from the provided text; never invent" prompts.
- **Orchestration**: **GitHub Actions** (cron schedule, repo secrets for the API key, PR creation).
- **Hosting/deploy**: the existing **Vercel** static deploy on `latest.json` (auto-deploys on merge to `main`).
- No database, no server — everything lives in the `signals-feed` repo.

---

## 4. File structure (`signals-feed` repo)

```
signals-feed/
  latest.json                # the live feed (deployed)
  validate_feed.py           # the gate (built)
  vercel.json                # cache headers (built)
  PUBLISHING.md / DAILY_OPS.md
  pipeline/
    sources.yaml             # trusted RSS feed list
    scout.py                 # fetch + dedupe + cluster candidates
    rank.py                  # LLM importance + lead suggestion
    write.py                 # LLM summaries (only-from-text)
    build.py                 # assemble draft latest.json
    preview.py               # render the PR preview
  .github/workflows/
    morning-feed.yml         # cron → run pipeline → validate → open PR
```

---

## 5. Daily schedule

- **~05:00 local** — cron triggers the pipeline (before family/work).
- **~05:05** — draft built, validated, **PR opened** with the preview; you get a GitHub notification.
- **Cutoff ~07:00** — if you've **merged**, today's feed is live before users wake; if **not**, the **last valid feed holds** (honest, finite — never fake or empty).
- Published **once**; not touched again that day (stability protects Daily Completion).

---

## 6. Human review flow

The PR notification → open on phone → one screen:
- **Lead Signal** (headline + tier) and **the four Supporting**.
- **Confidence / warnings** (paywall flags, low-source-count, "no clearly dominant story today").
- **Validator status** (must be green or the PR isn't opened).

Approve = **merge** (~30 sec). Disagree with the lead? Reject or quick-edit, re-run. Do nothing = hold.

## 7. Recommended review mode for 1.0 — *approval-required, hold-last-valid*

**Recommendation: do NOT auto-publish unreviewed content in 1.0.** Require the human merge; if missed by cutoff, **hold the last valid feed.**

Rationale: the product is trust, and an unreviewed AI feed can carry a hallucinated fact or a wrong lead straight to users — irreversible reputational damage for a one-person brand. A 30-second daily merge is cheap insurance, and holding yesterday's feed is *honest* (not fake, just not fresh). "Auto-publish if confidence is high" is a tempting Phase-2 once the pipeline has a track record (e.g., weeks of human-approved feeds with zero corrections) — but for launch, **trust > freshness.** Hold-last-valid is the safe default.

---

## 8. Delivery options — recommendation

| Option | Verdict |
|---|---|
| **GitHub Actions (cron) + PR review + Vercel auto-deploy** | ✅ **Recommended** — free, reliable, no server, mobile review, audit trail |
| Vercel Cron | Better for serverless functions; awkward for "build a file + open a PR" |
| Local Mac scheduled script (launchd/cron) | Works, but needs the Mac awake + maintained; least reliable |
| Claude Code scheduled workflow | Possible, but GitHub Actions is the standard, robust choice here |
| Gmail notification | Use GitHub's PR notification (or add an email step) — don't build custom email |
| Manual fallback button | Yes — GitHub Actions `workflow_dispatch` lets you re-run the pipeline on demand |

**Simplest reliable setup for a solo creator: GitHub Actions cron → pipeline → PR → you merge → Vercel deploys.** One repo, one daily notification, one tap.

---

## 9. Failure handling (the golden rule: never publish fake; hold last valid)

| Failure | Behavior |
|---|---|
| A source is down | Skip it; use the rest. Too few candidates for 5 → **hold last valid**. |
| Article paywalled/blocked | Use the RSS description if sufficient + flag it; if not enough to summarize honestly → drop the candidate, pick another. Can't honestly fill 5 → **hold last valid**. |
| AI can't read the article | Flag + drop that candidate; never guess. |
| Validation fails | No PR / no publish; alert you; **hold last valid**. |
| Deployment fails | Retry; if it still fails, nothing was overwritten → **last valid stays live**. |
| No clearly important story (quiet day) | Honest — the Lead is the highest tier *present* (may be Tier 4/5). Never manufacture urgency. |
| Feed is late | Past cutoff → **hold last valid**; publish when ready (ideally before wake). |

**If today's feed cannot be generated honestly and validated, the last valid feed stays. The app never shows fake content; the offline chain (cache → bundle → sample) backs the host.**

---

## 10. Safety rules — how each is enforced

- **Never fabricate news / invent URLs / fake attribution** → Writer prompt ("only from provided text"), real RSS URLs, **human review**, and the validator's homepage-URL + schema checks.
- **No homepage URLs** → validator rejects (built).
- **No mid-day reshuffle** → publish once; same `date` ⇒ same five.
- **No engagement-chasing / no infinite feed** → fixed five, finite, completion-first — structural.
- **Honesty backstop** → the human merge gate is the last line against hallucination; hold-last-valid on any doubt.

---

## Risks (ranked)

1. **LLM hallucination in summaries** — the top risk. Mitigation: only-from-text prompts + human review + paywall flagging. Never remove the human gate in 1.0.
2. **Paywalled sources** (esp. FT) — many articles unreadable. Bias the source list toward **open-access** outlets (Reuters, AP, BBC, The Verge, NHK World); use RSS descriptions where the body is blocked; skip what can't be read honestly.
3. **Wrong lead / tier misjudgment** — human review catches it; the LLM only *suggests*.
4. **Too few candidates / source outages** — hold last valid.
5. **Editorial soul erosion** — over-automation could make it feel algorithmic; keep the human approving the lead, which is the product's value.
6. **Legal** — summarizing (not copying) + linking + correct attribution is standard practice; the rules (original language, no long quotes, real URLs, real source) keep it clean.

---

## What to build first (order)

1. **Scout (`scout.py`)** — RSS fetch + dedupe + cluster. Proves you can gather real candidate stories with real URLs automatically. Lowest risk, foundational.
2. **Writer (`write.py`) + its prompt** — the honesty core. Test on real articles and *verify the summaries against the sources* before trusting it. This is where trust is won or lost.
3. **PR + GitHub Action wiring (`morning-feed.yml`)** — the delivery and the human gate. Now you have an end-to-end "draft → review → publish."
4. **Ranker/Editor automation (`rank.py`)** — last. Until then, let the LLM *suggest* lead/tiers and you confirm in the PR; keep the editorial call human while the pipeline earns trust.

---

*Plan only. No pipeline code, app code, or deployment created by this document. The validator and host config already exist; this is the automation layer on top.*
