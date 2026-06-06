# Writer v1 — Architecture Review (review only, no code)

> Goal: turn an **approved `selection.json`** (the five stories you chose) into **draft** Signals-style copy — headline, summary, key takeaways, why it matters — grounded **only** in the real source article, for you to edit and approve.
>
> Governing principle: **the Writer is an editorial assistant, not an autonomous journalist.** It drafts from a single approved article, shows its work, flags what it can't support, and never decides, attributes, or publishes on its own. Writer autonomy **Level C**: it may suggest copy, but every fact traces to the article and every published word is yours.
>
> Hard boundary: the Writer's output is a **draft file**, never `latest.json`. Assembling and publishing `latest.json` is a *separate, later* increment behind the human gate. This document writes no code and changes nothing.

---

## 1. Inputs to the Writer

The Writer runs **after** selection and consumes, per Signal:

| Input | Role | Notes |
|---|---|---|
| **`selection.json`** | The five approved stories (1 Lead + 4 Supporting) | **Primary driver.** Source of `id`, `source`, `category`, `lead`, real `url`/`canonical_url`, `number`, date. The Writer never re-selects or reorders. |
| **Source article text** | The **only** factual basis for drafting | Fetched live from each `url` and extracted to clean text. This — not the model's memory — is what the copy is written from. |
| **RSS snippet** (from selection) | Labeled fallback only | Used *only* when the full article can't be read (paywall/extraction failure), and the draft is flagged "thin source." Never preferred over full text. |
| **Metadata** (source name, URL, date, category, lead/importance) | Carried through untouched | The Writer formats copy around it; it does not invent or alter any of it. |
| `candidates.json` | **Not** a Writer input | The Writer works only from the *approved* selection, never the raw candidate pool — that boundary keeps unapproved stories out of drafting. |

Crucially, the Writer is handed **one article's text at a time**, in isolation — no cross-article context — so facts can't bleed between stories.

## 2. Output format

A **draft file** (`drafts.json`) — one record per selected Signal, each with AI-drafted fields plus carried-through metadata plus a provenance/confidence block:

- **`headline`** — short, calm, factual; the article's own framing, not a hotter take.
- **`summary`** — 2–4 sentences, original-language paraphrase of the article's core, in the Signals voice (plain, unhurried, no hype).
- **`keyTakeaways`** — 2–4 bullet points, each a single fact **present in the article**.
- **`whyItMatters`** — 1–2 sentences. *The most interpretive field and the highest-risk one* (see §4): grounded in significance the **article itself** states or directly implies; conservative or flagged when the article doesn't support it.
- **`readTime`** — derived mechanically from length (not generated prose).
- **Carried, untouched:** `number`, `lead`, `importance`, `category`, `source`, `originalURL`, `date`.
- **Not the Writer's job:** `imageURL`, `placeTime` (hero photography is app/PhotographyPool side), `audioURL` (later).
- **Provenance/confidence block** (per draft): `source_text_used` (`full_article` | `rss_snippet`), `confidence` (`ok` | `low`), and `flags` (e.g. `paywalled`, `thin_source`, `needs_review`, `unsupported_claim_removed`).

Every field is a **suggestion**. The draft is explicitly labeled *"DRAFT — not approved, not latest.json."*

## 3. Fact-grounding strategy

- **Single-article grounding.** Each Signal is drafted from exactly one fetched article's extracted text. The prompt provides *only* that text and instructs: *write nothing that isn't supported by it.*
- **No outside knowledge.** The model is told to ignore anything it "knows" — no added dates, figures, names, context, or background not in the supplied text. If the article doesn't say it, it doesn't get written.
- **Numbers and names are copied, not recalled.** Any figure, date, place, or proper noun in the draft must appear in the source text; the model is instructed to lift them, not reconstruct from memory.
- **Paraphrase, not transcribe.** Original wording (avoids copyright and keeps the Signals voice), but semantically faithful — no embellishment, no sharpening, no implied causation the article didn't make.
- **Low randomness.** Drafting runs at low temperature for faithful, repeatable output, not creative variation.

## 4. Hallucination-prevention strategy

Defense in depth — prompt, isolation, machine check, human gate:

1. **Constrained prompt** — "summarize *only* from the provided article text; if it isn't there, don't write it; if you're unsure, say so." Explicit permission to **under-write** and to emit a "can't confidently summarize" flag instead of guessing.
2. **Isolation** — one article per call; no other stories, no web access during drafting, so nothing external can leak in.
3. **Post-generation grounding check** — a programmatic pass flags draft tokens that look like un-sourced specifics: numbers, dates, %/$ figures, and capitalized proper nouns **not present in the source text** are surfaced for human attention (and, for the worst cases, the offending claim is dropped and flagged `unsupported_claim_removed`). This is a *flagging* aid, not a guarantee.
4. **`whyItMatters` gets extra caution** — because it's interpretive, the model may only state significance the article asserts; otherwise it stays generic-but-true ("a developing story in the region") or is flagged `needs_review`. Never manufactured stakes or urgency.
5. **Human review is the final and authoritative line.** Drafts are never trusted blindly; you read each against its source link before anything proceeds. The machine's job is to make that review fast, not to remove it.

## 5. Citation / source-preservation strategy

- **URL is immutable.** `originalURL` is copied verbatim from `selection.json`; the Writer can read the article *at* that URL but can never change, shorten, or substitute it. No code path lets the Writer mint a URL.
- **Attribution travels with the copy.** Every draft keeps `source` (e.g. "BBC News") and `originalURL`; the output renders a clear "Source: <name> — <link>" so attribution can't be separated from the claim.
- **One article, one source.** A draft is grounded in a single article from a single approved outlet; the Writer doesn't blend two outlets' facts into one summary (clustered duplicates inform *selection*, not drafting).
- **Provenance recorded** — `source_text_used` makes explicit whether the copy came from the full article or only the RSS snippet, so a "thin source" draft is never mistaken for a fully-read one.
- **Summarize, link, attribute** — original-language paraphrase + a link back + correct source name is the clean, standard, legally-sound pattern; no long verbatim quotes.

## 6. Validation rules (draft-time)

A `validate_drafts` pass before a draft reaches your review (separate from the existing publish-time `validate_feed.py`, which still gates `latest.json` later):

**Hard fail (won't present the draft as ready):**
- Any required field empty (`headline`, `summary`, ≥2 `keyTakeaways`, `whyItMatters`).
- `originalURL` missing, altered from selection, or not a real `https` article URL.
- `source` missing, or changed from the selection.
- Field lengths out of bounds (e.g. summary too long, headline absurdly long).
- `number` / `lead` / `category` don't match the approved selection.

**Flag, don't block (surface for human attention):**
- Grounding check found un-sourced numbers/proper nouns → `needs_review`.
- `source_text_used = rss_snippet` → `thin_source`.
- `confidence = low` or any `whyItMatters` the model couldn't ground.

Validation never *fixes* copy — it either passes it to you clean or hands you a flagged draft to edit. It does **not** publish.

## 7. Failure behavior

The golden rule holds: **never fabricate; when in doubt, flag or drop — never guess.**

| Failure | Behavior |
|---|---|
| **Article unavailable** (fetch fails, 404, timeout) | Don't draft from memory. Mark the Signal `source_unavailable`, leave its copy blank, and tell you to pick a replacement candidate (or retry). |
| **Paywall** | If the RSS snippet is substantive, draft a short summary from it only, flagged `paywalled` + `thin_source`; if the snippet is too thin to summarize honestly, **drop** and ask for a replacement. Never infer the body. |
| **Extraction failure** (HTML fetched but no clean text) | Treat like paywall: fall back to the snippet (flagged) or drop. Never draft from boilerplate/nav text. |
| **Model uncertainty / conflicting facts** | Emit the draft with `confidence: low` + `needs_review`, write only what's clearly supported, and leave the rest for you. Under-writing beats over-claiming. |
| **A field can't be grounded** (esp. `whyItMatters`) | Leave it minimal or empty and flag it — never pad with invented significance. |
| **Too many Signals fail to draft** | Surface that the day's set is incomplete; **hold** — the later assembly step won't build `latest.json` from a partial/flagged set without your edits. |

## 8. Incremental implementation plan

Lowest-trust-risk first; you review each step's output before the next:

1. **Article fetch + extraction** — given the approved URLs, fetch and extract clean article text (readability-style), with paywall/extraction-failure detection. *Verify on the real BBC/NPR URLs: does the extracted text match the article?* No LLM yet. (Note: extraction needs network — runs on the GitHub runner; some domains are blocked in the review sandbox.)
2. **Single-field drafting — `summary` only** — grounded prompt, one article in isolation, low temperature. *Verify each draft summary against its source by hand; this is where trust is won.*
3. **Add the grounding check** — flag un-sourced numbers/proper nouns; tune until it's a useful (not noisy) aid.
4. **Full field set** — `headline`, `keyTakeaways`, `whyItMatters` (with its extra caution), `readTime`, plus the provenance/confidence block → `drafts.json` + `validate_drafts`.
5. **Failure handling** — wire in §7 (unavailable / paywall / extraction / uncertainty) end to end on a real morning.
6. **Human review surface** — extend the selection PR so drafts render for read-and-edit, with flags visible; **your edits + approval are mandatory.**
7. **(Separate later increment, not Writer)** — assemble approved drafts into `latest.json`, run `validate_feed.py`, open the publish PR, merge → deploy.

Everything stays downstream of, and protected by, your edits, the draft validator, and — at publish — the human merge.

---

*Architecture review only. No Writer code, app code, `latest.json`, or deployment written or changed. The Writer drafts; you edit and approve; nothing publishes without you.*
