#!/usr/bin/env python3
"""
Signals — Writer v1 (safest first increment): extractive drafting.

Turns an approved selection.json into DRAFT Signals-style copy, grounded ONLY in
real source text. v1 is deliberately *extractive*: it never paraphrases or infers —
it can only copy text that exists in the source, so it cannot invent a fact. (The
LLM paraphrase layer is a later increment that swaps the drafter but keeps this
plumbing, provenance, and validation.)

Source text, in priority order, per selected item:
  1. cached full article  (pipeline/cache/articles/<id>.txt)         -> full_article
  2. live fetch (urllib)   (runner only; --no-fetch disables)         -> full_article
  3. RSS snippet from selection.json                                  -> rss_snippet
  4. nothing                                                          -> none (needs_review)

Hard boundaries: writes ONLY drafts.json. Never latest.json. Never publishes.
Never changes the source or URL. Subcommand `validate` runs validate_drafts.

Usage:
  python3 writer.py draft    [--selection selection.json] [--articles cache/articles]
                             [--no-fetch] [--simulate-unavailable id1,id2] [--out drafts.json]
  python3 writer.py validate [--selection selection.json] [--drafts drafts.json]
"""
import sys, os, re, json, argparse, datetime
from urllib.parse import urlsplit
import urllib.request

HERE = os.path.dirname(__file__)
DEF_SEL = os.path.join(HERE, "selection.json")
DEF_ART = os.path.join(HERE, "cache", "articles")
DEF_OUT = os.path.join(HERE, "drafts.json")

SENT_RE = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"[A-Za-z0-9'$%.-]+")
NUM_RE = re.compile(r"\d")
PROPER_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


# ----------------------------------------------------------------- helpers
def load_selection(path):
    if not os.path.exists(path):
        sys.exit(f"ERROR: selection not found: {path} (run select.py build first).")
    data = json.load(open(path))
    sig = data.get("signals", [])
    if not sig:
        sys.exit("ERROR: selection.json has no signals.")
    return data, sig


def sentences(text):
    return [s.strip() for s in SENT_RE.split((text or "").strip()) if s.strip()]


def word_count(text):
    return len(WORD_RE.findall(text or ""))


def read_time_min(text):
    return max(1, round(word_count(text) / 200))


def source_token_set(text):
    return {w.lower() for w in WORD_RE.findall(text or "")}


def ungrounded_tokens(draft_text, source_text):
    """Numbers / proper nouns in the draft not present in the source (should be empty for extractive)."""
    src = source_token_set(source_text)
    hits = []
    for tok in WORD_RE.findall(draft_text or ""):
        if (NUM_RE.search(tok) or PROPER_RE.fullmatch(tok)) and tok.lower() not in src:
            hits.append(tok)
    return sorted(set(hits))


def get_source_text(item, articles_dir, allow_fetch, unavailable):
    """Return (text, source_text_used). Honors the priority chain; never fabricates."""
    cid = item["id"]
    if cid in unavailable:
        return "", "none"
    # 1. cached full article
    p = os.path.join(articles_dir, f"{cid}.txt")
    if os.path.exists(p):
        t = open(p, encoding="utf-8").read().strip()
        if t:
            return t, "full_article"
    # 2. live fetch (runner only)
    if allow_fetch:
        try:
            req = urllib.request.Request(item["url"], headers={"User-Agent": "SignalsWriter/1.0"})
            html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
            text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", html))
            text = re.sub(r"\s+", " ", text).strip()
            if word_count(text) >= 120:          # crude "did we get a real body?" gate
                return text, "full_article"
        except Exception:
            pass
    # 3. RSS snippet
    snip = (item.get("snippet") or "").strip()
    if snip:
        return snip, "rss_snippet"
    # 4. nothing
    return "", "none"


# ----------------------------------------------------------------- draft
def draft_one(item, source_text, used):
    role = "lead" if item.get("lead") else "supporting"
    base = {
        "id": item["id"], "number": item.get("number"),
        "selectedRole": role, "category": item.get("category", "OTHER"),
        "source": item["source"], "originalURL": item["url"],
        "source_text_used": used, "confidence": "low", "flags": [],
        "draft": {"headline": "", "summary": "", "keyTakeaways": [], "whyItMatters": "", "readTime": ""},
    }

    # --- failure path: no usable source text ---
    if used == "none" or not source_text:
        base["confidence"] = "low"
        base["flags"] = ["source_unavailable", "needs_review"]
        return base

    sents = sentences(source_text)
    headline = item["title"]                              # the outlet's own headline (grounded)
    if used == "full_article":
        summary = " ".join(sents[:2])
        takeaways = sents[2:5] if len(sents) > 2 else sents[:1]
        confidence = "high" if word_count(source_text) >= 250 else "medium"
        flags = ["extractive_draft"]
    else:  # rss_snippet
        summary = source_text                             # the outlet's RSS description (one real summary line)
        takeaways = sents if len(sents) > 1 else []       # honestly can't split independent takeaways from 1 line
        confidence = "low"
        flags = ["extractive_draft", "thin_source", "rss_snippet_only"]
        if not takeaways:
            flags.append("keyTakeaways_needs_human")

    # whyItMatters: conservative by design — v1 never asserts significance; the human writes it.
    why = ""
    flags.append("whyItMatters_needs_human")

    if item.get("paywalled"):
        flags.append("paywalled")

    # grounding check on generated copy (summary + takeaways) against the source text + the
    # outlet's own headline (both authoritative). The verbatim headline isn't re-checked.
    ung = ungrounded_tokens(" ".join([summary] + takeaways), source_text + " " + headline)
    if ung:
        flags.append("ungrounded_tokens:" + ",".join(ung[:6]))
        flags.append("needs_review")
        confidence = "low"

    base["confidence"] = confidence
    base["flags"] = flags
    base["draft"] = {
        "headline": headline,
        "summary": summary,
        "keyTakeaways": takeaways,
        "whyItMatters": why,
        "readTime": f"{read_time_min(source_text)} min",
    }
    return base


def cmd_draft(args):
    _, sig = load_selection(args.selection)
    unavailable = set(x.strip() for x in (args.simulate_unavailable or "").split(",") if x.strip())
    drafts = []
    for item in sig:
        text, used = get_source_text(item, args.articles, allow_fetch=not args.no_fetch, unavailable=unavailable)
        drafts.append(draft_one(item, text, used))

    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "drafter": "extractive-v1",
        "count": len(drafts),
        "note": "DRAFT suggestions only. Extractive (copied from source text). "
                "Not approved, not latest.json, not published. Human edits + approval required.",
        "signals": drafts,
    }
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=2)

    print(f"✓ wrote {os.path.relpath(args.out)} — {len(drafts)} draft(s), drafter=extractive-v1")
    for d in drafts:
        print(f"  #{d['number']} {d['selectedRole']:10} [{d['id']}] src={d['source_text_used']:12} "
              f"conf={d['confidence']:6} flags={d['flags']}")
    print()
    _validate(args.selection, out, source="(just drafted)")


# ----------------------------------------------------------------- validate
def _validate(selection_path, drafts_obj, source=""):
    _, sig = load_selection(selection_path)
    by_id = {s["id"]: s for s in sig}
    drafts = drafts_obj["signals"]

    hard, warn = [], []

    # selection match: same id set, same lead, same role/number
    sel_ids = {s["id"] for s in sig}
    drf_ids = {d["id"] for d in drafts}
    if sel_ids != drf_ids:
        hard.append(f"selection mismatch: draft ids {sorted(drf_ids)} != selection {sorted(sel_ids)}")
    sel_lead = next((s["id"] for s in sig if s.get("lead")), None)
    drf_lead = next((d["id"] for d in drafts if d["selectedRole"] == "lead"), None)
    if sel_lead != drf_lead:
        hard.append(f"lead mismatch: draft lead {drf_lead} != selection lead {sel_lead}")

    for d in drafts:
        s = by_id.get(d["id"])
        if not s:
            continue
        # URL + source immutability
        if d.get("originalURL") != s.get("url"):
            hard.append(f"[{d['id']}] URL changed from selection.")
        if not (urlsplit(d.get("originalURL", "")).scheme == "https" and urlsplit(d["originalURL"]).path.strip("/")):
            hard.append(f"[{d['id']}] originalURL is not a real https article URL.")
        if d.get("source") != s.get("source"):
            hard.append(f"[{d['id']}] source changed from selection.")
        if d.get("selectedRole") not in ("lead", "supporting"):
            hard.append(f"[{d['id']}] invalid selectedRole.")

        # required fields missing WITHOUT a flag = hard fail; with a flag = warn
        dr = d.get("draft", {})
        missing = [k for k in ("headline", "summary") if not dr.get(k)] + \
                  (["keyTakeaways"] if not dr.get("keyTakeaways") else [])
        flagged = any(f in d["flags"] for f in
                      ("needs_review", "source_unavailable", "thin_source",
                       "keyTakeaways_needs_human", "whyItMatters_needs_human"))
        if missing and not flagged:
            hard.append(f"[{d['id']}] missing {missing} with no explaining flag.")
        elif missing:
            warn.append(f"[{d['id']}] missing {missing} (flagged: ok to hand-fill).")

        # warn-level signals
        if d["confidence"] == "low":
            warn.append(f"[{d['id']}] low confidence.")
        if d["source_text_used"] == "rss_snippet":
            warn.append(f"[{d['id']}] snippet-only source (thin).")
        if d["source_text_used"] == "none":
            warn.append(f"[{d['id']}] extraction/source failure — needs a replacement or retry.")
        if any(f.startswith("ungrounded_tokens") for f in d["flags"]):
            warn.append(f"[{d['id']}] possible unsupported numbers/proper nouns — check against source.")
        if "paywalled" in d["flags"]:
            warn.append(f"[{d['id']}] paywalled source.")

    print(f"=== validate_drafts {source} ===")
    if hard:
        print("✗ HARD FAIL:")
        for h in hard:
            print(f"   - {h}")
    else:
        print("✓ no hard failures (structure, URL/source immutability, selection match all OK).")
    if warn:
        print("⚠ flags for your attention:")
        for w in warn:
            print(f"   - {w}")
    print("\nDRAFTS ONLY — nothing here is approved, assembled into latest.json, or published.")
    return 1 if hard else 0


def cmd_validate(args):
    if not os.path.exists(args.drafts):
        sys.exit(f"ERROR: {args.drafts} not found. Run `writer.py draft` first.")
    sys.exit(_validate(args.selection, json.load(open(args.drafts)), source=os.path.basename(args.drafts)))


def main():
    ap = argparse.ArgumentParser(description="Signals Writer v1 (extractive).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--selection", default=DEF_SEL)
    d.add_argument("--articles", default=DEF_ART)
    d.add_argument("--no-fetch", action="store_true", help="disable live fetch (sandbox/offline)")
    d.add_argument("--simulate-unavailable", default="", help="comma ids to force the failure path")
    d.add_argument("--out", default=DEF_OUT)
    v = sub.add_parser("validate")
    v.add_argument("--selection", default=DEF_SEL)
    v.add_argument("--drafts", default=DEF_OUT)
    args = ap.parse_args()
    (cmd_draft if args.cmd == "draft" else cmd_validate)(args)


if __name__ == "__main__":
    main()
