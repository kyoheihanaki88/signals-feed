# Increment 1 — Selection Surface / Review PR Flow (plan only)

> Goal: make it easy to look at the Scout's `candidates.json` and **choose the day's five** — 1 Lead + 4 Supporting — producing a **human-approved selection**. Nothing else.
>
> Hard boundary for this increment: **no Writer, no editorial copy, no `latest.json`, no publish, no deploy.** The output is *which five stories*, not *what they say*. Decision locked: Writer autonomy **Level C** applies to a *later* increment; this one is pure selection.

---

## What this increment produces

A single artifact: **`selection.json`** — the five candidate records you approved, with the Lead flagged and a display order. It carries only what the Scout already collected (title, source, real URL, snippet, date) — **no AI-written headline, summary, takeaways, or why-it-matters.** It is the clean handoff that a *future* Writer increment will turn into draft copy.

`selection.json` is explicitly **not** `latest.json`. It cannot be published; the app never reads it.

## Smallest safe design

Three small pieces, mirroring how Scout was built (prove it locally, then wire the PR):

1. **`pipeline/select.py`** — reads `candidates.json`, does two jobs:
   - **Render a review view** (`review.md`) — a phone-readable sheet of the candidates, grouped **by cross-source cluster and importance hints** (cluster_size > 1 first), then by category. Each candidate shows a **stable short ID**, headline, **source**, **clickable real URL**, date, snippet, and `paywalled`/reliability flags. This is what you read to decide.
   - **Build + validate the selection** — reads your picks from `selection.yaml`, checks them, and writes `selection.json`.
2. **`pipeline/selection.yaml`** — *your* input, a 5-line file you edit:
   ```yaml
   lead: a1b2c3            # the one Lead Signal (you choose manually)
   supporting:            # exactly four
     - d4e5f6
     - g7h8i9
     - j0k1l2
     - m3n4o5
   ```
   IDs are **deterministic** (a short hash of each candidate's canonical URL), so they're stable across Scout re-runs and unambiguous — no "item #3 moved" confusion.
3. **`review.md`** (generated) — the read surface. Regenerated each run; never hand-edited.

## Data flow

```
candidates.json ──select.py (render)──▶ review.md   (you read on phone)
                                            │
                       you edit selection.yaml  (lead + 4 supporting IDs)
                                            │
candidates.json + selection.yaml ──select.py (build+validate)──▶ selection.json
                                            │
                              (later increment: Writer → latest.json → PR → merge)
```

## Validation rules (in `select.py`)

**Hard fail** (refuse to write `selection.json`, print the reason):
- Not exactly **5** IDs total, or any duplicate ID.
- The `lead` ID is missing, or appears in `supporting` too.
- Any ID is **not present** in `candidates.json` (can't select a story the Scout didn't collect — blocks any out-of-band/fabricated pick).
- Any chosen candidate is missing a real `https` article URL (defense-in-depth; Scout already filters, this re-checks).

**Warn only** (write it, but flag in the output):
- Category coverage is thin (e.g. all five WORLD, or no Japan/tech) — surfaced so you can rebalance, not blocked. A quiet, single-topic day is allowed.
- A chosen candidate is `paywalled` (the future Writer won't be able to read its body).

## Review / PR flow

Local first, then the PR wiring:

- **Step 1 (local):** run `select.py` on a real `candidates.json`, read `review.md`, fill `selection.yaml`, get a clean `selection.json`. Prove the surface is genuinely easy before any automation.
- **Step 2 (PR surface):** the daily Scout run lands `candidates.json` + `review.md` on a branch and opens a **review PR**. You read `review.md` in the PR, **edit `selection.yaml` directly in the PR** (GitHub mobile lets you edit a file in a branch), a check re-runs `select.py --build` to validate and produce `selection.json`, and **your merge records the approved selection.** No merge = no selection that day; nothing is forced.

The PR is the approval record. Even here — at *selection*, before any copy exists — **merge is a human action**, and nothing downstream runs without it.

## Guardrails (maps to your locked rules)

| Your rule | How Increment 1 honors it |
|---|---|
| Real source URL for every candidate | Selection limited to IDs that exist in `candidates.json`, each re-checked for a real `https` URL. |
| No invented URLs / no out-of-allow-list sources | You can only pick what Scout collected from `sources.yaml`; an unknown ID hard-fails. |
| No invented facts | This increment writes **no prose at all** — only copies the Scout's collected metadata. There is nothing to hallucinate. |
| Preserve source attribution | `source` + URL travel with every selected record into `selection.json`. |
| I choose the Lead manually | `lead:` is your single explicit choice; the tool never picks it. |
| I approve every published Signal | Selection is yours; and selection isn't publication — `latest.json` is untouched. |
| Nothing auto-publishes | No `latest.json`, no deploy, no Vercel touch in this increment. The selection PR can run read-only. |

## Out of scope (explicitly NOT in Increment 1)

No Writer, no AI-drafted headline/summary/takeaways/why-it-matters, no `latest.json` assembly, no validator-for-publish run, no Vercel deploy, no NHK/Japan source work (**kept as an open source-gap item**), no Ranker that auto-picks the five or the lead.

## Definition of done

1. `select.py` renders a clear, phone-readable `review.md` from a real `candidates.json`.
2. Editing a 5-line `selection.yaml` and re-running produces a valid `selection.json` (5 records, lead flagged, ordered) — or a clear error if the picks break a rule.
3. You confirm the review surface is genuinely easy to use on a real morning's candidates.
4. (Then, only if you approve) the PR wiring is added so selection happens in a mergeable PR.

---

*Plan only. No code written. Increment 1 ends at a human-approved `selection.json`; drafting copy, assembling `latest.json`, validation-for-publish, and deployment are later increments behind the same human gate.*
