# Signals — Build 1: Signal Scout (plan only)

> Scope: **collect candidate stories every morning → `candidates.json`.** Nothing else. No summaries, no `latest.json`, no validation of a feed, no PR, no publish. The Scout's only job is to hand the next layer a clean, deduped list of *real article URLs* with metadata.

---

## 1. Recommended source list (honmost assessment)

Prefer outlets with **reliable, open-access RSS** (so the later Writer can actually read the article). Reality check on your suggested list:

| Source | RSS status | Use |
|---|---|---|
| **BBC News** | ✅ Reliable open RSS (`/news/world`, `/business`, `/technology`) | **Core** |
| **The Verge** | ✅ Open RSS (`theverge.com/rss/index.xml`) | **Core** (tech) |
| **NHK World (English)** | ✅ RSS available | **Core** (Japan/world) |
| **The Guardian** | ✅ Excellent open RSS + free API | **Core** (add — very reliable) |
| **NPR** | ✅ Open RSS | Add (world/US) |
| **Al Jazeera** | ✅ Open RSS | Add (geopolitics) |
| **Reuters** | ⚠️ **Public RSS was discontinued** (~2020) | Don't rely on direct RSS — use Google News query (below) |
| **Associated Press** | ⚠️ No broad official public RSS | Same — via Google News query |
| **Financial Times** | ⚠️ RSS exists but **articles are paywalled** | Collect URLs only; **flag paywalled** so the Writer avoids unreadable bodies |

**Reuters/AP workaround:** Google News RSS topic search (`news.google.com/rss/search?q=...`) returns their articles — but the links are **Google redirect URLs** that must be **resolved to the real article URL** before storing (or skipped if unresolvable). Treat as a secondary source, not core.

**Recommended core set for 1.0:** BBC, The Guardian, The Verge, NHK World, NPR, Al Jazeera — all reliably open. Add FT (URLs only, flagged) and Google-News-for-Reuters/AP later if needed. **Every feed URL must be verified (200 + valid RSS) before it's committed to `sources.yaml`** — feed endpoints drift, and I can't guarantee exact current URLs from here.

## 2. RSS / API availability — verification step
Before any code, run a one-time check that each candidate feed returns **HTTP 200 + parseable RSS/Atom** and yields entries with a real article `link`. Keep only feeds that pass; record the working URL in `sources.yaml`. This is the first deliverable and it gates the rest.

## 3. File structure
```
signals-feed/
  pipeline/
    sources.yaml          # verified feed list: {name, url, default_category, paywalled: bool}
    scout.py              # the Scout
  candidates/
    candidates-YYYY-MM-DD.json   # output (one per morning)
  .github/workflows/
    scout.yml             # cron → run scout.py → upload candidates.json as an artifact
```
(No `latest.json`, Writer, or PR touched in Build 1.)

## 4. Candidate schema (`candidates.json`)
```json
{
  "generated_at": "2026-06-04T20:05:00Z",
  "source_count": 6,
  "candidates": [
    {
      "title": "…",
      "source": "BBC",
      "url": "https://www.bbc.com/news/world-XXXXXXXX",   // real article URL, never a homepage
      "published": "2026-06-04T18:12:00Z",
      "category_guess": "WORLD",                            // from the feed's section/source
      "snippet": "…RSS description…",                       // kept for the later Writer (not used now)
      "paywalled": false,                                   // from the source's flag
      "cluster_id": 3,                                      // group of the same story across sources
      "cluster_size": 2                                     // # of sources covering it (importance signal)
    }
  ]
}
```
Notes: store the **real article URL** (validated to have a path), the **published timestamp**, a **category guess** (derived from the feed/section), the **RSS snippet** (handed to the Writer later — not summarized now), a **paywalled** flag, and **clustering** info (`cluster_id` + `cluster_size`). `cluster_size > 1` (a story carried by multiple trusted sources) is the cross-source importance signal the Ranker will use later.

## 5. Deduplication & clustering method
Two layers, both lightweight (no embeddings needed for MVP):
- **Exact dedupe:** canonicalize URLs (lowercase host, strip `?utm_*`/tracking params, drop fragments) → drop identical articles.
- **Near-dup clustering (same story, different outlets):** normalize titles (lowercase, strip stopwords/punctuation) and compare with **fuzzy token matching** (`rapidfuzz`, e.g. token-set ratio) above a conservative threshold → assign a `cluster_id`; `cluster_size` = distinct sources in the cluster. Keep all members but mark the cluster, and pick a representative URL (earliest/most-authoritative) for later.
- **Conservative by design:** better to under-merge (two near-identical entries) than over-merge (collapse two genuinely different stories). Embeddings-based clustering is a later upgrade if fuzzy matching proves too coarse.

## 6. Daily GitHub Actions schedule
- **Trigger:** `schedule` cron (UTC — GitHub has no DST handling) set to ~**1 hour before your review window**, plus `workflow_dispatch` for manual runs.
- Example (if your morning is JST, UTC+9): `cron: "0 20 * * *"` → 05:00 JST. **Adjust to your timezone**, and re-check at DST boundaries (or run a little early to be safe).
- **Output for Build 1:** the Action runs `scout.py` and **uploads `candidates.json` as a workflow artifact** (downloadable) and prints a human-readable summary to the log. It does **not** commit to `main`, open a PR, or deploy — that's later builds.

## 7. Failure handling (Scout level)
| Failure | Behavior |
|---|---|
| A feed is down / 404 / invalid XML | Log a warning, **skip it**, continue with the rest — never fail the whole run for one source |
| Per-feed network timeout | Short timeout (e.g. 10s), skip on failure |
| Homepage / non-article URL | **Filter out** (require a URL path) |
| Google-redirect URL unresolved | Resolve to the real article URL, or **skip** |
| Malformed entry (no title/url/date) | Skip |
| Stale item (older than ~24–36h) | Filter out (prefer recent) |
| Zero candidates collected | Exit with a clear error + empty `candidates.json` (downstream would hold last valid) |
Scout never invents or guesses; a source it can't read is simply dropped.

## 8. What to build first (within Scout)
1. **`sources.yaml` + feed verification** — confirm each feed returns valid RSS with real article links.
2. **Fetch + normalize** (`feedparser`) → candidate objects (title, source, url, published, category_guess, snippet, paywalled) with homepage/stale filtering. *Review the raw candidates: real URLs? recent? sane categories?*
3. **Dedupe + cluster** (URL canonicalize + fuzzy title) → `cluster_id`/`cluster_size`. *Review clustering quality on a real morning.*
4. **Wrap in the GitHub Action** (cron + `workflow_dispatch`), upload `candidates.json` artifact + log summary.

## 9. Risks
1. **RSS instability** — Reuters/AP public RSS gone; feed URLs drift; some outlets block/rate-limit. Mitigation: verify feeds, prefer reliably-open ones, keep Google-News-query as a flagged fallback.
2. **Google redirect URLs** — not direct article links; must be resolved or skipped, or they violate the "real URL" rule downstream.
3. **Paywalled sources (FT)** — Scout gets the URL fine, but the later Writer can't read it; the `paywalled` flag lets the Editor prefer readable sources.
4. **Over/under-clustering** — fuzzy matching errs; conservative threshold + early spot-checks.
5. **Category-guess roughness** — derived from feed section; fine for candidates (the Editor refines).
6. **Cron timezone/DST** — UTC only; set for your local morning and adjust at DST.
7. **Freshness/duplication across days** — within-run dedupe + a recency filter handle Build 1; cross-day dedupe is a later concern.

## 10. Review-first implementation plan
Each step is implemented, then you review the *output* before the next:
- **A.** Verified `sources.yaml` (every feed 200 + valid RSS) → you approve the source list.
- **B.** `scout.py` fetch + normalize → `candidates.json` (no clustering) → you review the raw candidates.
- **C.** Add dedupe + clustering + `cluster_size` → you review the grouping on real data.
- **D.** `scout.yml` GitHub Action (cron + manual), candidates uploaded as an artifact → you review a scheduled run.
Only after D is solid do we move to Build 2 (Ranker/Writer).

---

*Plan only. No Scout code, app code, or workflow created by this document. Build 1 ends at a clean `candidates.json`; summaries, ranking, `latest.json`, validation, PR, and publishing are later builds.*
