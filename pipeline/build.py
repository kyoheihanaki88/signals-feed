#!/usr/bin/env python3
"""
Signals — Builder v1.

Transforms a HUMAN-APPROVED drafts.json into a DRAFT feed file and runs the publish-time
validator against it. It writes ONLY pipeline/generated/latest.draft.json — never the
production latest.json, never publishes, never merges.

Locked rules:
  - input: pipeline/drafts.json (override with --drafts)
  - require top-level `approved: true`
  - hard-fail on unresolved blocking flags: needs_review, source_unavailable, thin_source,
    whyItMatters_needs_human, or confidence == "low"
  - exactly 5 signals, exactly 1 lead
  - lead must be number 1 / importance 1; supporting importance 2..5 in order
  - importance = number (human order); never invented or reordered
  - no duplicate URLs, no invalid URLs
  - curated decorative images from images.yaml; audioURL empty; date = today

Usage:
  python3 build.py [--drafts pipeline/drafts.json] [--images pipeline/images.yaml]
                   [--out pipeline/generated/latest.draft.json]
"""
import sys, os, json, argparse, datetime, subprocess
from urllib.parse import urlparse
import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
DEF_DRAFTS = os.path.join(HERE, "drafts.json")
DEF_IMAGES = os.path.join(HERE, "images.yaml")
DEF_OUT = os.path.join(HERE, "generated", "latest.draft.json")
VALIDATOR = os.path.join(ROOT, "validate_feed.py")

BLOCKING_FLAGS = {"needs_review", "source_unavailable", "thin_source", "whyItMatters_needs_human"}
FOCUS = "MIXED"
VERSION = 1


def fail(errors):
    print("❌ BUILD REJECTED — no draft written, production latest.json untouched:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Signals Builder v1 (approved drafts → draft feed).")
    ap.add_argument("--drafts", default=DEF_DRAFTS)
    ap.add_argument("--images", default=DEF_IMAGES)
    ap.add_argument("--out", default=DEF_OUT)
    args = ap.parse_args()

    if not os.path.exists(args.drafts):
        fail([f"drafts file not found: {args.drafts}"])
    drafts = json.load(open(args.drafts))
    images = yaml.safe_load(open(args.images))
    img_cats = images.get("categories", {})
    img_default = images.get("default", {"imageURL": "", "placeTime": ""})

    signals_in = drafts.get("signals", [])
    errors = []

    # --- approval gate (before anything) ---
    if drafts.get("approved") is not True:
        errors.append("top-level `approved: true` is required (human approval). Refusing to build.")

    # --- structural checks ---
    if len(signals_in) != 5:
        errors.append(f"expected exactly 5 signals, found {len(signals_in)}")
    leads = [s for s in signals_in if s.get("selectedRole") == "lead"]
    if len(leads) != 1:
        errors.append(f"expected exactly 1 lead, found {len(leads)}")

    # --- per-signal checks ---
    seen_urls = {}
    for s in signals_in:
        sid = s.get("id", "?")
        num = s.get("number")
        role = s.get("selectedRole")
        dr = s.get("draft", {})

        # blocking flags / confidence
        bad = sorted(set(s.get("flags", [])) & BLOCKING_FLAGS)
        if bad:
            errors.append(f"[{sid}] unresolved flag(s): {', '.join(bad)} — human must edit/clear first")
        if s.get("confidence") == "low":
            errors.append(f"[{sid}] confidence is low — human must review/approve first")

        # required fields (human-edited copy)
        if not (isinstance(dr.get("headline"), str) and dr["headline"].strip()):
            errors.append(f"[{sid}] missing headline")
        if not (isinstance(dr.get("summary"), str) and dr["summary"].strip()):
            errors.append(f"[{sid}] missing summary")
        if not (isinstance(dr.get("keyTakeaways"), list) and len(dr["keyTakeaways"]) >= 1):
            errors.append(f"[{sid}] needs at least one keyTakeaway")
        if not (isinstance(dr.get("whyItMatters"), str) and dr["whyItMatters"].strip()):
            errors.append(f"[{sid}] missing whyItMatters")

        # role / number / importance coupling (importance = number; never invented)
        if role == "lead" and num != 1:
            errors.append(f"[{sid}] lead must be number 1 (got {num})")
        if role == "supporting" and num not in (2, 3, 4, 5):
            errors.append(f"[{sid}] supporting number must be 2..5 (got {num})")

        # URL validity (https + real article path)
        url = s.get("originalURL", "")
        p = urlparse(url)
        if p.scheme != "https" or not p.netloc:
            errors.append(f"[{sid}] originalURL not https: {url!r}")
        elif p.path.strip("/") == "":
            errors.append(f"[{sid}] originalURL is a homepage, not an article: {url!r}")
        else:
            seen_urls.setdefault(url, []).append(sid)

        # category must have a curated image
        cat = s.get("category", "OTHER")
        if cat not in img_cats:
            errors.append(f"[{sid}] no curated image for category {cat!r} in images.yaml")

    # duplicate URLs
    for url, ids in seen_urls.items():
        if len(ids) > 1:
            errors.append(f"duplicate URL across {', '.join(ids)}: {url}")

    # numbers must be exactly {1,2,3,4,5}
    nums = sorted(s.get("number") for s in signals_in)
    if nums != [1, 2, 3, 4, 5]:
        errors.append(f"signal numbers must be exactly 1..5, got {nums}")

    if errors:
        fail(errors)

    # --- assemble FeedSignal list (importance = number; decorative image; empty audio) ---
    today = datetime.date.today().isoformat()
    out_signals = []
    for s in sorted(signals_in, key=lambda x: x["number"]):
        dr = s["draft"]
        cat = s.get("category", "OTHER")
        img = img_cats.get(cat, img_default)
        out_signals.append({
            "number": s["number"],
            "importance": s["number"],                     # human order = importance (Lead=1)
            "lead": s.get("selectedRole") == "lead",
            "category": cat,
            "source": s["source"],
            "headline": dr["headline"],
            "summary": dr["summary"],
            "keyTakeaways": dr["keyTakeaways"],
            "whyItMatters": dr["whyItMatters"],
            "originalURL": s["originalURL"],
            "readTime": dr.get("readTime", ""),
            "imageURL": img.get("imageURL", ""),           # curated decorative
            "placeTime": img.get("placeTime", ""),
            "audioURL": "",                                 # empty in v1
        })

    feed = {"date": today, "focus": FOCUS, "version": VERSION, "signals": out_signals}

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(feed, open(args.out, "w"), ensure_ascii=False, indent=2)
    print(f"✓ wrote DRAFT feed → {os.path.relpath(args.out)} (date={today}, {len(out_signals)} signals)")
    print(f"  (production latest.json NOT touched; this is a draft only.)\n")

    # --- run the publish-time validator against the DRAFT ---
    print("=== validate_feed.py (against the draft) ===")
    res = subprocess.run([sys.executable, VALIDATOR, args.out])
    if res.returncode != 0:
        print("\n⚠ draft did NOT pass the validator — it is NOT promotable. Fix and rebuild.")
        sys.exit(res.returncode)
    print("\n✓ draft passed the validator. It is ready for human PR review (still NOT published).")


if __name__ == "__main__":
    main()
