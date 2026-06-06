#!/usr/bin/env python3
"""
Signals — Selection Surface (Increment 1).

Two jobs, no more:
  render : read candidates.json -> write a phone-readable review.md (and a
           selection.yaml template if one doesn't exist yet).
  build  : read your picks from selection.yaml -> validate -> write selection.json.

Scope guard: this selects WHICH five stories (1 Lead + 4 Supporting). It writes
NO editorial copy, never assembles or touches latest.json, and never publishes.
selection.json carries only metadata the Scout already collected.

Usage:
  python3 select.py render [--candidates candidates.json] [--review review.md]
  python3 select.py build  [--candidates candidates.json] [--selection selection.yaml] [--out selection.json]
"""
import sys, os, re, json, argparse, hashlib
from urllib.parse import urlsplit
import yaml

HERE = os.path.dirname(__file__)
DEF_CAND = os.path.join(HERE, "candidates.json")
DEF_REVIEW = os.path.join(HERE, "review.md")
DEF_SEL = os.path.join(HERE, "selection.yaml")
DEF_OUT = os.path.join(HERE, "selection.json")


def short_id(canonical_url: str) -> str:
    """Deterministic 6-char id from the canonical URL (stable across Scout re-runs)."""
    return hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:6]


def load_candidates(path):
    if not os.path.exists(path):
        sys.exit(f"ERROR: candidates file not found: {path}\n(Run the Scout first.)")
    data = json.load(open(path))
    cands = data.get("candidates", [])
    if not cands:
        sys.exit("ERROR: candidates.json has zero candidates — nothing to select.")
    # assign deterministic ids; detect (and fail loudly on) any collision
    by_id = {}
    for c in cands:
        cid = short_id(c["canonical_url"])
        if cid in by_id and by_id[cid]["canonical_url"] != c["canonical_url"]:
            sys.exit(f"ERROR: id collision on '{cid}' — widen short_id length.")
        c["id"] = cid
        by_id[cid] = c
    return data, cands, by_id


def has_real_url(url: str) -> bool:
    p = urlsplit(url or "")
    return p.scheme == "https" and p.netloc != "" and p.path.strip("/") != ""


def trunc(s, n=200):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- render
def render(args):
    data, cands, _ = load_candidates(args.candidates)
    date = (data.get("generated_at", "") or "")[:10]
    sources = sorted({c["source"] for c in cands})

    # group: multi-source clusters first (importance signal), then singletons by category
    clusters = {}
    for c in cands:
        clusters.setdefault(c.get("cluster_id"), []).append(c)
    multi = [grp for grp in clusters.values() if len(grp) > 1]
    multi.sort(key=lambda g: (-len(g), g[0]["title"]))
    singles = [grp[0] for grp in clusters.values() if len(grp) == 1]

    L = []
    L.append(f"# Signals — Candidate Review · {date}")
    L.append("")
    L.append(f"{len(cands)} candidates · {len(sources)} source(s) · "
             f"{len(multi)} cross-source cluster(s)")
    L.append("")
    L.append("**How to choose:** pick 1 Lead + 4 Supporting from below, paste their "
             "`id`s into the template, save as `selection.yaml`, then run `python3 select.py build`.")
    L.append("")
    L.append("## ✍️ Suggested selection template")
    L.append("_Copy this, fill in five `id`s (Lead first), paste into `selection.yaml`._")
    L.append("")
    L.append("```yaml")
    L.append("lead:        # one id")
    L.append("supporting:  # four ids")
    L.append("  - ")
    L.append("  - ")
    L.append("  - ")
    L.append("  - ")
    L.append("```")
    L.append("")
    L.append("---")
    L.append("")

    def card(c, show_title=True):
        flags = []
        if c.get("paywalled"):
            flags.append("🔒 paywalled")
        if c.get("source_reliability") and c["source_reliability"] != "high":
            flags.append(f"⚠ {c['source_reliability']}")
        flagstr = ("  ·  " + " · ".join(flags)) if flags else ""
        date_s = (c.get("published_at") or "")[:10]
        title_line = f"{c['title']}\n" if show_title else ""  # clusters already show the title as the header
        return (
            f"**`{c['id']}`**  ·  {c['source'].split('(')[0].strip()}  ·  "
            f"{c.get('category','OTHER')}  ·  {date_s}{flagstr}\n"
            f"{title_line}"
            f"{trunc(c.get('snippet'), 130)}\n"
            f"[{urlsplit(c['url']).netloc}]({c['url']})\n"
        )

    if multi:
        L.append("## ⭐ Top stories — covered by multiple outlets")
        L.append("_More than one trusted source ran it — usually the day's most important news. Good Lead candidates._")
        L.append("")
        for n, grp in enumerate(multi, 1):
            grp_sorted = sorted(grp, key=lambda x: x["source"])
            L.append(f"### {n}. {grp_sorted[0]['title']}  ·  {len(grp)} outlets")
            L.append("")
            for c in grp_sorted:
                L.append(card(c, show_title=False))
        L.append("---")
        L.append("")

    L.append("## Other candidates")
    L.append("")
    for cat in sorted({c.get("category", "OTHER") for c in singles}):
        L.append(f"### {cat}")
        L.append("")
        for c in sorted((x for x in singles if x.get("category", "OTHER") == cat),
                        key=lambda x: x.get("published_at") or "", reverse=True):
            L.append(card(c))
        L.append("")

    with open(args.review, "w") as f:
        f.write("\n".join(L) + "\n")

    # drop a selection.yaml template only if one isn't already there (never overwrite your picks)
    made_template = False
    if not os.path.exists(args.selection):
        tmpl = [
            "# Signals — your selection. Copy ids from review.md.",
            "# Exactly one lead + exactly four supporting.",
            "",
            "lead:        # <-- one candidate id, e.g. a1b2c3",
            "supporting:  # <-- exactly four ids",
            "  - ",
            "  - ",
            "  - ",
            "  - ",
        ]
        with open(args.selection, "w") as f:
            f.write("\n".join(tmpl) + "\n")
        made_template = True

    print(f"✓ rendered {os.path.relpath(args.review)} — {len(cands)} candidates "
          f"({len(multi)} cross-source cluster(s)).")
    if made_template:
        print(f"✓ wrote a blank {os.path.relpath(args.selection)} for you to fill in.")
    else:
        print(f"• kept your existing {os.path.relpath(args.selection)} (not overwritten).")
    print("Next: open review.md, choose 1 Lead + 4 Supporting in selection.yaml, then: python3 select.py build")


# ---------------------------------------------------------------- build
def build(args):
    _, _, by_id = load_candidates(args.candidates)
    if not os.path.exists(args.selection):
        sys.exit(f"ERROR: {args.selection} not found. Run `select.py render` first, then fill it in.")
    sel = yaml.safe_load(open(args.selection)) or {}

    errors = []
    lead = sel.get("lead")
    supporting = sel.get("supporting") or []
    if isinstance(lead, str):
        lead = lead.strip() or None
    if not isinstance(supporting, list):
        supporting = []
    supporting = [str(s).strip() for s in supporting if str(s).strip()]

    # --- hard validation ---
    if not lead:
        errors.append("no `lead` id set (you choose the Lead manually).")
    if len(supporting) != 4:
        errors.append(f"`supporting` must list exactly 4 ids (found {len(supporting)}).")

    all_ids = ([lead] if lead else []) + supporting
    seen, dups = set(), set()
    for i in all_ids:
        if i in seen:
            dups.add(i)
        seen.add(i)
    if dups:
        errors.append(f"duplicate id(s): {', '.join(sorted(dups))} (a story can't appear twice).")
    if lead and lead in supporting:
        errors.append(f"lead `{lead}` is also listed under supporting — it must be only the Lead.")
    if len(seen) != 5:
        errors.append(f"need exactly 5 distinct ids total (lead + 4); got {len(seen)}.")

    for i in all_ids:
        if i not in by_id:
            errors.append(f"id `{i}` is not in candidates.json — you can only select stories the Scout collected.")
        elif not has_real_url(by_id[i]["url"]):
            errors.append(f"id `{i}` has no real https article URL — refusing to select it.")

    if errors:
        print("✗ selection rejected:")
        for e in errors:
            print(f"   - {e}")
        sys.exit(1)

    # --- assemble selection.json (metadata only — NO editorial copy) ---
    def record(i, number, is_lead):
        c = by_id[i]
        return {
            "id": c["id"], "number": number, "lead": is_lead,
            "category": c.get("category", "OTHER"),
            "source": c["source"], "title": c["title"],
            "url": c["url"], "canonical_url": c["canonical_url"],
            "published_at": c.get("published_at"),
            "snippet": c.get("snippet"),
            "paywalled": bool(c.get("paywalled")),
            "source_reliability": c.get("source_reliability"),
            "cluster_id": c.get("cluster_id"), "cluster_size": c.get("cluster_size"),
        }

    chosen = [record(lead, 1, True)] + [record(s, n + 2, False) for n, s in enumerate(supporting)]
    out = {
        "selected_at_source": os.path.basename(args.candidates),
        "count": len(chosen),
        "note": "Human-approved SELECTION only. No editorial copy. Not latest.json. Not published.",
        "signals": chosen,
    }
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=2)

    # warn-only signals (do not block)
    warns = []
    cats = [r["category"] for r in chosen]
    if len(set(cats)) <= 2:
        warns.append(f"thin category coverage: {', '.join(cats)} — consider rebalancing.")
    pay = [r["id"] for r in chosen if r["paywalled"]]
    if pay:
        warns.append(f"paywalled pick(s): {', '.join(pay)} — a future Writer may not read the body.")

    print(f"✓ selection valid → wrote {os.path.relpath(args.out)}")
    print(f"  Lead    : {by_id[lead]['title'][:64]}  [{by_id[lead]['source']}]")
    for s in supporting:
        print(f"  Support : {by_id[s]['title'][:64]}  [{by_id[s]['source']}]")
    for w in warns:
        print(f"  ⚠ {w}")
    print("\nThis is a selection only — no copy written, latest.json untouched, nothing published.")


def main():
    ap = argparse.ArgumentParser(description="Signals selection surface (Increment 1).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("render", "build"):
        p = sub.add_parser(name)
        p.add_argument("--candidates", default=DEF_CAND)
        p.add_argument("--selection", default=DEF_SEL)
        if name == "render":
            p.add_argument("--review", default=DEF_REVIEW)
        else:
            p.add_argument("--out", default=DEF_OUT)
    args = ap.parse_args()
    (render if args.cmd == "render" else build)(args)


if __name__ == "__main__":
    main()
