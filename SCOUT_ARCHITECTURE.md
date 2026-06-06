# Signal Scout — Architecture Review (review only, no code)

> Purpose: a single canonical answer to *where Signal Scout lives, what shape it takes, and how a candidate story safely becomes one of the day's five Signals* — under the hard rule that **nothing fake or unapproved ever reaches users.** This consolidates and supersedes the scattered notes in `SCOUT_PLAN.md` and `AUTOMATION_PLAN.md`.
>
> Status note: the **Scout layer itself is already built** (`pipeline/scout.py`, `pipeline/sources.yaml`, `.github/workflows/scout.yml`). This review frames it in the full pipeline and defines the layers that are *not* yet built (selection, writing, publish). No code is written or changed by this document.

---

## Design north star

Signals is a **trust + relief** product — "Read it all. Put it down." Five finite Signals, a morning ritual, not an infinite feed. Every architectural choice below is subordinate to three non-negotiables:

1. **Never fabricate** a fact, a URL, or an attribution.
2. **Never auto-publish** — a human approves before anything goes live.
3. **Stay finite and editorial** — exactly five, chosen with judgment, never algorithmic engagement-bait.

The architecture's job is to remove *labor* (gathering, deduping, drafting) while keeping the *human as the last line* on accuracy and on the editorial call.

---

## 1. Where Signal Scout should live — `signals-feed`

**Decision: `signals-feed`. Definitively. The pipeline must never live in `signals-ios`.**

The two repos are a clean client/backend split, and that boundary is itself a guardrail:

- **`signals-ios`** is the *client*. It only ever **reads** the hosted `latest.json`. It contains no sources, no scraping, no API keys, no editorial logic. If the pipeline lived here, a content bug would require an App Store release to fix — unacceptable for a daily feed.
- **`signals-feed`** is the *content backend*: sources, Scout, the future writer, the validator, `latest.json`, hosting config, and the automation. Content changes ship by updating a JSON file on the host, with **zero app releases**.

This separation means the app can't be the thing that fabricates or mispublishes — it's structurally downstream of the human approval gate. Keep it that way.

## 2. Script, GitHub Action, or manual tool — all three, by layer

It isn't one of the three; each layer uses the right tool:

| Layer | Tool | Why |
|---|---|---|
| **Collection (Scout)** | Python **script** run by a **GitHub Action** (cron + manual `workflow_dispatch`) | Automatable, no human needed to gather; already built. Cron removes the daily labor; manual dispatch is the re-run button. |
| **Selection + approval** | **Human**, via a **Pull Request** | The PR *is* the review surface, the approval mechanism (merge = publish), the audit trail, and the hold-by-default. Reviewable from a phone. |
| **Publish** | **Automatic on merge** (Vercel deploy of `latest.json`) | Only fires *after* the human merge — automation downstream of the gate, never around it. |

So: **automated collection → human selection/approval → automated publish.** The automation brackets the human; it never replaces them.

## 3. How candidate stories are collected — RSS from a vetted allow-list

Already implemented in `scout.py`, and the right model:

- **Trusted sources only**, defined in `sources.yaml` (an explicit allow-list — BBC, NPR, Guardian, Verge, Al Jazeera, etc., each marked `verified`/`known`/`flagged` and `paywalled`). No open-web crawling, no "search the internet" — collection is restricted to feeds a human vetted.
- **RSS, not scraping** — feeds hand us real article URLs and metadata directly, which is both more reliable and legally cleaner than scraping HTML.
- **Normalize → filter → dedupe → cluster** → `candidates.json`: strip tracking params, drop homepage/video/stale items, dedupe by canonical URL, and group the same story across outlets (`cluster_size > 1` = a cross-source importance signal, the input the later selection step uses to spot what actually matters).
- **Output is candidates only** — never a feed. The Scout deliberately does *not* summarize, rank, choose the lead, or write `latest.json`. It hands the next layer a clean, deduped list of real URLs.

The one known coverage gap to close: **Japan (NHK)** has no confirmed feed yet, so the JAPAN category is currently empty.

## 4. How source URLs are validated — two independent layers

Fabricated or junk URLs are the most dangerous failure for a trust product, so validation is enforced **twice, in different places**:

- **At collection (Scout):** a candidate is dropped unless it has a real **article** URL — `https`, a non-empty path (homepages rejected), not a `/video/` link, parseable date, recent. URLs are canonicalized (tracking stripped) and deduped. A source the Scout can't read is simply dropped — never guessed.
- **At publish (`validate_feed.py`):** the existing validator independently re-checks every URL in the assembled `latest.json` — must be `https`, must not be a homepage — and rejects the whole feed if any fail. This already works: it's *why* the current hosted feed (good content, but homepage URLs) correctly fails validation.

Because every candidate originates from a real RSS item, there is **no code path that can invent a URL** — the URL always traces back to a fetched feed entry. The human review is the third check.

## 5. How draft candidates are reviewed by you — a one-screen PR

The review surface is a **Pull Request**, opened automatically once a draft is assembled and passes the validator:

- The PR description renders a **one-screen preview**: the proposed **Lead** + four **Supporting**, each with headline, source, link, and the draft summary, plus warnings (paywall flags, "no clearly dominant story today," low source count) and the **validator status** (green, or the PR isn't opened).
- You open it on your phone, read the five, and either **merge** (~30 seconds = approve and publish), **quick-edit** a headline/summary/lead and merge, or **do nothing** (yesterday's valid feed holds).

This keeps the human cost to about half a minute while leaving the **lead choice and every published word under your control** — the editorial soul stays human.

**The one real open decision (flagged, not decided here):** *how much the machine drafts vs. how much you write.*

- **Option A — auto-draft, you veto:** a Writer drafts all five summaries from article text; you mostly just merge. Lowest effort, highest hallucination surface.
- **Option B — you write, tool assembles:** Scout collects; you select the five and write/approve every summary; the tool only formats `latest.json`. Near-zero fabrication risk, most effort.
- **Option C — hybrid (recommended):** Writer drafts **strictly from the real article text** as a *suggestion*; you review/edit each line and pick the lead before merge. Removes the labor, keeps a human approving every fact. Matches the "no time, but trust is paramount" reality.

Recommendation: **C for 1.0**, with the Writer under a hard "summarize only from provided text, never infer" prompt and paywalled bodies flagged as unwritable. Decide this before the Writer is built.

## 6. How approved items become `latest.json` — assemble → validate → PR → merge → deploy

```
candidates.json → [select 5: 1 Lead + 4 Supporting] → build draft latest.json
                → validate_feed.py (gate) → green → open PR with preview
                → YOU merge → Vercel auto-deploys latest.json → app reads it
```

- The five chosen candidates are mapped into the locked feed schema (`number`, `importance`, `lead`, `category`, `source`, `headline`, `summary`, `keyTakeaways`, `whyItMatters`, `originalURL`, `readTime`, `date`, …), with the curator-selected `lead:true` and the importance tier set.
- `validate_feed.py` is the **hard gate**: wrong count, no/multiple leads, lead not in the highest tier present, missing summaries, homepage URLs, or a stale date → **red, no PR, no publish.**
- **Merge is the only path to live.** Merge → Vercel redeploys `latest.json`. Published **once** per morning, never reshuffled (stability protects the Daily Completion ritual). On any failure at any step, **the last valid feed stays live** — honest (not fresh, but never fake or empty), backed further by the app's offline chain (cache → bundle → sample).

## 7. Risks and guardrails

| Risk | Guardrail |
|---|---|
| **Fabricated facts** (the top risk, once a Writer exists) | Writer summarizes *only* from provided article text; **human reviews every word**; paywalled bodies flagged unwritable, not guessed. |
| **Fabricated / junk URLs** | URLs only ever come from real RSS items; Scout rejects homepages/videos; `validate_feed.py` re-rejects at publish. No code path invents a URL. |
| **Auto-publishing unreviewed content** | No auto-publish in 1.0. Merge-to-publish only; hold-last-valid by default. `scout.yml` runs with `permissions: contents: read` — it *cannot* commit or open a PR. |
| **Wrong lead / manufactured urgency** | Human picks/approves the lead; the machine only *suggests*. Lead = highest tier **present** — a quiet day yields a Tier-4/5 lead, never invented drama. |
| **Duplicate stories** | URL-canonical dedupe + cross-source title clustering; conservative threshold (prefer under-merge to over-merge). |
| **Low-quality sources** | Closed allow-list in `sources.yaml`; each source vetted and reliability-tagged; no open-web discovery. |
| **Too few candidates / source outage** | Skip dead feeds, never fail the run for one; if too few for five → **hold last valid**. Scout exits non-zero on zero candidates. |
| **Paywalled sources (FT)** | `paywalled` flag → Writer avoids the body; bias the list toward open-access outlets. |
| **Losing the "ritual," becoming a news feed** | Structural: exactly five, finite, published once, completion-first; no infinite scroll, no engagement metrics, no reshuffling. |
| **Cron timezone/DST** | GitHub cron is UTC-only; set for your morning (currently 05:00 JST) and re-check at DST. |

## 8. Step-by-step implementation plan

Each step is built, then *you review its output* before the next. Build order is by trust-risk, lowest first.

1. **Scout — DONE.** `scout.py` + `sources.yaml` + `scout.yml`. Collects real, deduped, clustered candidates daily into `candidates.json` (artifact). *Next concrete task: run it live on GitHub once and confirm the candidate quality (and close the NHK/Japan gap).*
2. **Decide the Writer autonomy level** (§5: A/B/C). This gates the design of step 4. *Recommendation: C.*
3. **Selection surface.** The mechanism by which the five are chosen (1 Lead + 4 Supporting, category-balanced) — initially a human pick from `candidates.json`; later a Ranker/Editor that *suggests* and you confirm. Keep the lead human.
4. **Writer** (`write.py`) — the honesty core. Drafts each Signal's fields **only from the real article text**, in the Signals voice, flags anything unreadable. *Build and verify its summaries against sources before trusting it — this is where trust is won or lost.*
5. **Build + validate + PR** (`build.py` + `validate_feed.py` + `morning-feed.yml`) — assemble the draft `latest.json`, gate it through the validator, open the one-screen review PR. End-to-end "draft → review → publish."
6. **Operate the human gate.** Daily: notification → review five on phone → merge or hold. Tune sources, threshold, and timing from real mornings.
7. **(Later, only after a track record)** Optionally let high-confidence days auto-suggest more, but **never remove the human merge in 1.0.**

Everything from step 3 onward is downstream of, and protected by, the human approval gate and the validator.

---

*Architecture review only. No pipeline code, app code, or deployment was written or changed by this document. The Scout, validator, and host config already exist; the selection, writer, build-and-PR, and live operation layers are described here but not yet built.*
