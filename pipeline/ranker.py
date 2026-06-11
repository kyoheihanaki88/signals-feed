#!/usr/bin/env python3
"""
Signals — Ranker v1 (deterministic, rule-based auto-selection).

Reads pipeline/candidates.json (from Scout) and writes pipeline/selection.yaml — exactly one
Lead + four Supporting ids — for the Daily Auto Edition pipeline. Deterministic and rule-based:
NO LLM, no randomness; the same candidates always yield the same five. It replaces the human
`selection.yaml` step ONLY in auto mode, and is gated hard so weak mornings produce NO selection
(the workflow then opens no PR — see DAILY_OPS.md).

It is a SELECTION tool only: it never writes copy, never touches latest.json, never publishes.
selection.py / writer.py / build.py / validate_feed.py remain the downstream gates.

Hard fail (exit 1, NO selection.yaml written), printing the reason:
  - fewer than --min-candidates (default 20)
  - fewer than 5 candidates with a real article URL
  - no lead-quality story found
  - not enough eligible supporting stories (need 4)
  - a chosen story has no real article URL, or duplicate URLs/ids slip through

On success it writes selection.yaml with `approved: true` (auto-approved: all selection gates
passed) plus `lead` + `supporting`. selection.py reads only `lead`/`supporting`; the extra keys
are ignored.

Usage:
  python3 pipeline/ranker.py [--candidates candidates.json] [--out selection.yaml]
                             [--min-candidates 20] [--stale-hours 48] [--fresh-hours 36]
                             [--summary-file PATH]   # markdown summary (e.g. $GITHUB_STEP_SUMMARY)
"""
import sys, os, re, json, argparse, hashlib, datetime
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEF_CAND = os.path.join(HERE, "candidates.json")
DEF_OUT = os.path.join(HERE, "selection.yaml")

# Reliable outlets we prefer (substring match on the source name, lowercased).
RELIABLE = ("bbc", "npr", "guardian", "financial times", "the verge", "al jazeera")

# Lead-worthiness by category: global urgency / war / geopolitics, then economy, then major tech,
# then regional/institutional. A 0 means "never the Lead" (but still fine as Supporting).
LEAD_CATEGORY_WEIGHT = {
    "WORLD": 4, "ECONOMY": 3, "BUSINESS": 3, "FINANCE": 3,
    "TECH": 2, "AI": 2, "JAPAN": 2, "SCIENCE": 1,
}

LIVE_TITLE_RE = re.compile(r"\b(live|as it happened|live blog|liveblog)\b", re.I)
LIVE_URL_RE = re.compile(r"/live(/|-|$)", re.I)
VIDEO_RE = re.compile(r"/(videos?|watch|av)/", re.I)

# Per-category cap among the final five, to avoid "all 5 the same kind of story" when possible.
CATEGORY_CAP = 2


def short_id(c):
    """The candidate's stable id (matches Scout / selection.py.short_id)."""
    if c.get("id"):
        return c["id"]
    return hashlib.sha1((c.get("canonical_url") or c.get("url") or "").encode("utf-8")).hexdigest()[:6]


def has_real_url(url):
    p = urlsplit(url or "")
    return p.scheme == "https" and p.netloc != "" and p.path.strip("/") != "" and not VIDEO_RE.search(p.path)


def is_live_blog(c):
    return bool(LIVE_TITLE_RE.search(c.get("title", "")) or LIVE_URL_RE.search(urlsplit(c.get("url", "")).path))


def is_reliable(c):
    s = (c.get("source") or "").lower()
    return any(r in s for r in RELIABLE)


def hours_old(c, now):
    raw = c.get("published_at")
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def cat_weight(c):
    return LEAD_CATEGORY_WEIGHT.get((c.get("category") or "").upper(), 0)


def recency01(c, now, fresh_hours):
    h = hours_old(c, now)
    if h is None:
        return 0.0
    return max(0.0, min(1.0, (fresh_hours - h) / fresh_hours))


def base_score(c, now, fresh_hours):
    """Generic importance/quality score (Supporting ranking)."""
    s = 0.0
    s += 3.0 * min(int(c.get("cluster_size") or 1), 4)   # cross-source corroboration = importance
    s += 2.0 * (1 if is_reliable(c) else 0)
    s += 2.0 * recency01(c, now, fresh_hours)
    s += 1.5 * cat_weight(c)
    if c.get("paywalled"):
        s -= 1.0
    if hours_old(c, now) is None:
        s -= 0.5
    if is_live_blog(c):
        s -= 4.0
    return s


def lead_score(c, now, fresh_hours):
    """Lead ranking weights category urgency more heavily."""
    return base_score(c, now, fresh_hours) + 1.5 * cat_weight(c)


def is_stale(c, now, stale_hours):
    h = hours_old(c, now)
    return h is not None and h > stale_hours


def eligible(c, now, stale_hours):
    return has_real_url(c.get("url", "")) and not is_stale(c, now, stale_hours)


def dedup_by_cluster(cands, now, fresh_hours):
    """Keep the single best-scoring candidate per cross-source cluster (one story = one pick)."""
    best = {}
    for c in cands:
        cid = c.get("cluster_id", id(c))
        if cid not in best or base_score(c, now, fresh_hours) > base_score(best[cid], now, fresh_hours):
            best[cid] = c
    # deterministic order: score desc, then id asc
    return sorted(best.values(), key=lambda c: (-base_score(c, now, fresh_hours), short_id(c)))


def pick_supporting(pool, lead, now, fresh_hours, need=4):
    """Pick `need` supporting from pool (already excludes the lead's cluster), preferring category
    diversity (<= CATEGORY_CAP per category incl. the lead), relaxing the cap only if needed."""
    ranked = sorted(pool, key=lambda c: (-base_score(c, now, fresh_hours), short_id(c)))
    for cap in (CATEGORY_CAP, CATEGORY_CAP + 1, 999):
        chosen, counts = [], {(lead.get("category") or "OTHER").upper(): 1}
        for c in ranked:
            cat = (c.get("category") or "OTHER").upper()
            if counts.get(cat, 0) < cap:
                chosen.append(c)
                counts[cat] = counts.get(cat, 0) + 1
            if len(chosen) == need:
                return chosen
    return ranked[:need]   # fallback (won't reach diversity, but fills the five)


def fail(reason, summary_path=None):
    print(f"⏭  RANKER STOP — no selection written.\n   reason: {reason}")
    if summary_path:
        _summary(summary_path, lead=None, supporting=[], total=None, skip=reason)
    sys.exit(1)


def _summary(path, lead, supporting, total, skip=None):
    lines = ["## Ranker (auto-selection)\n"]
    if skip:
        lines.append(f"**SKIPPED** — {skip}\n")
        lines.append("No `selection.yaml` written; the pipeline will not open a PR.\n")
    else:
        lines.append(f"- candidates: **{total}**")
        lines.append(f"- lead: **{lead['title'][:80]}**  · _{lead['source']}_  · `{short_id(lead)}`")
        lines.append("- supporting:")
        for c in supporting:
            lines.append(f"  - {c['title'][:80]}  · _{c['source']}_  · `{short_id(c)}`")
    try:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Signals deterministic Ranker (auto-selection v1).")
    ap.add_argument("--candidates", default=DEF_CAND)
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--min-candidates", type=int, default=20)
    ap.add_argument("--stale-hours", type=int, default=48)
    ap.add_argument("--fresh-hours", type=int, default=36)
    ap.add_argument("--summary-file", default=None)
    ap.add_argument("--now", default=None, help="override 'now' ISO8601 (testing/determinism)")
    args = ap.parse_args()

    now = (datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else datetime.datetime.now(datetime.timezone.utc))

    if not os.path.exists(args.candidates):
        fail(f"candidates file not found: {args.candidates}", args.summary_file)
    try:
        data = json.load(open(args.candidates))
    except Exception as e:
        fail(f"candidates.json is not valid JSON: {e}", args.summary_file)

    cands = data.get("candidates", [])
    total = len(cands)
    print(f"ranker: {total} candidates from {os.path.basename(args.candidates)}")

    # GATE 1 — enough candidates
    if total < args.min_candidates:
        fail(f"too few candidates ({total} < {args.min_candidates}) — a thin/failed Scout morning",
             args.summary_file)

    # GATE 2 — enough real article URLs
    valid_url_count = sum(1 for c in cands if has_real_url(c.get("url", "")))
    if valid_url_count < 5:
        fail(f"too few real article URLs ({valid_url_count} < 5)", args.summary_file)

    # Eligible (real URL + not stale), strict (no live blogs) then with live blogs as a fallback.
    elig_strict = [c for c in cands if eligible(c, now, args.stale_hours) and not is_live_blog(c)]
    elig_loose = [c for c in cands if eligible(c, now, args.stale_hours)]
    pool = dedup_by_cluster(elig_strict, now, args.fresh_hours)
    if len(pool) < 5:   # not enough non-live stories — allow live blogs as a last resort
        pool = dedup_by_cluster(elig_loose, now, args.fresh_hours)

    # GATE 3 — a lead-quality story (serious category, corroborated cross-source OR reliable source)
    lead_pool = [c for c in pool if cat_weight(c) > 0 and (int(c.get("cluster_size") or 1) >= 2 or is_reliable(c))]
    if not lead_pool:
        fail("no lead-quality story found (need a serious-category story, cross-sourced or from a "
             "reliable outlet)", args.summary_file)
    # deterministic: highest lead_score, then lexically smallest id
    lead = sorted(lead_pool, key=lambda c: (-lead_score(c, now, args.fresh_hours), short_id(c)))[0]

    # GATE 4 — four eligible supporting stories from OTHER clusters
    support_pool = [c for c in pool if c.get("cluster_id") != lead.get("cluster_id") and short_id(c) != short_id(lead)]
    if len(support_pool) < 4:
        fail(f"not enough eligible supporting stories ({len(support_pool)} < 4 after removing the "
             f"lead's cluster)", args.summary_file)
    supporting = pick_supporting(support_pool, lead, now, args.fresh_hours, need=4)

    five = [lead] + supporting
    ids = [short_id(c) for c in five]
    urls = [c.get("url") for c in five]

    # GATE 5 — final integrity: 5 distinct ids, real URLs, no duplicate URLs
    if len(set(ids)) != 5:
        fail(f"selection is not 5 distinct stories (ids={ids})", args.summary_file)
    if any(not has_real_url(u) for u in urls):
        fail("a selected story has no real article URL", args.summary_file)
    if len(set(urls)) != 5:
        fail("duplicate article URLs in the selection", args.summary_file)

    # Write selection.yaml (selection.py reads lead/supporting; the rest is provenance it ignores).
    out_lines = [
        "# Auto-generated by ranker.py (deterministic v1) — do not hand-edit; regenerated each run.",
        "# approved: true means every SELECTION gate passed (downstream writer/build/feed gates still apply).",
        "approved: true",
        "mode: auto",
        f"generated_at: {now.isoformat()}",
        f"lead: {ids[0]}",
        "supporting:",
    ] + [f"  - {i}" for i in ids[1:]]
    with open(args.out, "w") as f:
        f.write("\n".join(out_lines) + "\n")

    print(f"✓ wrote {os.path.relpath(args.out)} (approved: true)")
    print(f"  Lead    : {lead['title'][:64]}  [{lead['source']}]  ({ids[0]}, "
          f"cluster_size={lead.get('cluster_size')}, {lead.get('category')})")
    for c in supporting:
        print(f"  Support : {c['title'][:64]}  [{c['source']}]  ({short_id(c)}, {c.get('category')})")
    cats = [(c.get('category') or 'OTHER') for c in five]
    print(f"  categories: {', '.join(cats)}")
    _summary(args.summary_file, lead, supporting, total) if args.summary_file else None


if __name__ == "__main__":
    main()
