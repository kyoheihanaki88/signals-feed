# Builder v1 — Implementation Plan (plan only, no code)

> Goal: transform an **approved, human-edited `drafts.json`** into a **draft feed file** (`pipeline/generated/latest.draft.json`), gated by `validate_feed.py`. **Never** overwrite production `latest.json`, never publish, never merge.
>
> Locked decisions: importance is **human-assigned** (Lead = 1; Supporting = 2,3,4,5 in approved order; Builder must not invent or reorder). Images are **curated/decorative** (no scraping; atmosphere, not journalism). `audioURL` empty in v1. `date` = today.

---

## 1. Exact files to add / change

| File | Action | Purpose |
|---|---|---|
| `pipeline/build.py` | **ADD** | The Builder: approved drafts → `latest.draft.json`, then run the validator. |
| `pipeline/images.yaml` | **ADD** | Curated decorative image + place·time per category (the v1 image strategy). |
| `pipeline/generated/latest.draft.json` | **ADD (output, gitignored)** | The draft feed. Written here only — never the repo-root `latest.json`. |
| `.gitignore` | **CHANGE** | Ignore `pipeline/generated/` (draft output is a working file, not source). |
| `validate_feed.py` | **REUSE** (verify it accepts a path arg) | The hard gate, unchanged logic. If it's hardcoded to `latest.json`, the only change is an optional path argument — validation rules untouched. |
| `latest.json` (repo root, production) | **DO NOT TOUCH** | The live feed. The Builder never writes here. |
| `.github/workflows/*` | **NOT in v1** | PR automation is a later step. |

No `signals-ios` changes. No Scout/Writer/Selection changes.

## 2. Schema mapping (`drafts.json` item → `FeedSignal`)

| `FeedSignal` field | Source | Rule |
|---|---|---|
| `number` | `draft.number` | 1–5, the approved order (Lead = 1). Builder verifies, never reorders. |
| `importance` | **= `draft.number`** | Human order *is* importance: Lead → 1, Supporting → 2,3,4,5. Builder asserts Lead = 1 and importances are 1–5 distinct; **hard-fail if not** (does not "fix"). |
| `lead` | `draft.selectedRole == "lead"` | Exactly one `true`. |
| `category`, `source`, `originalURL` | carried from draft | Immutable; URL copied verbatim. |
| `headline`, `summary`, `keyTakeaways`, `whyItMatters`, `readTime` | `draft.draft.*` (human-edited) | Used as-is from the approved draft. |
| `imageURL`, `placeTime` | **`images.yaml` by `category`** | Curated decorative; never an article image, never a factual claim. |
| `audioURL` | `""` | Empty in v1 (TTS later). |
| `date` | **today** (`yyyy-MM-dd`, publish locale) | `validate_feed.py` requires `date == today`. Match the existing `latest.json` structure (top-level `date` + `signals[]`, and per-signal `date` if the current schema carries it). |

Output object shape mirrors the current `latest.json` / `fallback.json` exactly (`{ "date": …, "signals": [ … ] }`) so the app's `SignalsFeed`/`FeedSignal` decoder and the validator both accept it unchanged. **Confirm field placement against the live `latest.json` before finalizing.**

## 3. Validation flow

```
approved drafts.json
  → Builder PRE-CHECKS (below)            ── fail → STOP, no artifact, exit 1
  → map → assemble latest.draft.json
  → write to pipeline/generated/          (draft path only; never live)
  → run validate_feed.py <draft path>     ── red → STOP (artifact marked invalid), exit 1
  → green → print "draft ready for human PR" (still NOT published)
```

Builder pre-checks (mirror the validator, fail fast with clear messages):
- exactly **5** signals; exactly **1** `lead`;
- `importance` = `number`, values are 1–5 distinct, **Lead's importance = 1** (= min present);
- required fields non-empty: `headline`, `summary`, ≥1 `keyTakeaway`, `whyItMatters`;
- every `originalURL` is `https` and **not a homepage**; **no duplicate URLs**;
- **no unresolved blocking flags** and confidence is approved-level (see §4);
- `category` has an entry in `images.yaml`.

`validate_feed.py` is then the authoritative second gate — the Builder can't widen it.

## 4. Human approval / blocking flags (nothing auto-publishes)

The Builder runs **only on an explicitly approved input** and refuses otherwise:

- **Approval marker:** each signal carries `"approved": true` (set by the human after editing), or a top-level `"approved": true`. No marker → **refuse to build.**
- **Blocking flags:** if any signal still carries `needs_review`, `source_unavailable`, `thin_source`, `rss_snippet_only`, `keyTakeaways_needs_human`, `whyItMatters_needs_human`, or `confidence: low`, the Builder **hard-fails** — these must be cleared by the human (by editing the copy / filling the field), not by the tool.
- The Builder is **human-invoked**; it has no schedule and cannot merge or deploy.

## 5. Failure behavior (golden rule: never publish fake; hold last valid)

| Failure | Behavior |
|---|---|
| Missing required field | Hard-fail, exit 1, no ready artifact. |
| Unresolved `needs_review` / low confidence | Hard-fail — human must edit + approve first. |
| Invalid URL (non-https / homepage / altered) | Hard-fail. |
| Duplicate URL | Hard-fail. |
| Wrong signal count (≠ 5) | Hard-fail. |
| Wrong lead count (≠ 1) | Hard-fail. |
| Importance not 1–5 / Lead ≠ 1 | Hard-fail (never reordered or invented). |
| `validate_feed.py` red | Hard-fail; draft is **not** promotable. |

On **any** failure: production `latest.json` is **never touched**; the last valid live feed holds. The draft path may keep the rejected file for inspection, clearly non-publishable, but the Builder exits non-zero and prints exactly what failed.

## 6. Smallest safe implementation steps

1. **`images.yaml`** — a small category → `{imageURL, placeTime}` map (curated, decorative). Add `pipeline/generated/` to `.gitignore`.
2. **`build.py` core** — read approved `drafts.json`, run pre-checks, map → `FeedSignal`, set `date`/images/empty `audioURL`, write `pipeline/generated/latest.draft.json`. Never write the live file. *Review the draft by hand.*
3. **Wire `validate_feed.py`** against the draft path (confirm/extend it to accept a path arg). Build → validate; red = stop. *Prove red and green on the current edited drafts.*
4. **Approval gate** — require the `approved` marker + zero blocking flags; refuse otherwise. Confirm it can't be bypassed.
5. **Local verification** — run end-to-end on a hand-approved `drafts.json`: a clean green draft, plus forced failures (missing field, duplicate URL, unapproved, Lead ≠ 1). Confirm production `latest.json` is untouched throughout.
6. **(Later, not v1)** PR automation: build+validate on a branch → preview PR → human merge → Vercel deploy. And (separately) unify Home/Signals/Audio onto one shared store when flipping to live.

---

*Plan only. No Builder code, app code, or `latest.json` written or changed. Builder v1 ends at a validated `pipeline/generated/latest.draft.json`; promoting it to the live feed (PR + merge + deploy) is a later step behind the human gate.*
