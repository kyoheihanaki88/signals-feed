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
import sys, os, re, json, argparse, datetime, subprocess
from urllib.parse import urlparse
import urllib.request
from editorial import topic_fingerprint, first_duplicate_pair          # v2.1: duplicate-topic gate
from writer import summary_quality_issues, why_quality_issues          # v2.1: broken-text gate
# NOTE: `yaml` is imported lazily inside main() (only the build run needs it), so this module stays
# importable for the composition-gate helpers/tests without requiring PyYAML.

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
DEF_DRAFTS = os.path.join(HERE, "drafts.json")
DEF_IMAGES = os.path.join(HERE, "images.yaml")
DEF_OUT = os.path.join(HERE, "generated", "latest.draft.json")
VALIDATOR = os.path.join(ROOT, "validate_feed.py")

BLOCKING_FLAGS = {"needs_review", "source_unavailable", "thin_source", "whyItMatters_needs_human"}
FOCUS = "MIXED"
VERSION = 1


def read_time_int(v):
    """Normalize readTime to integer minutes. Accepts 3, '3', '3 min', '' → 0.
    iOS FeedSignal.readTime is an Int, so the feed must never carry a string here."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return int(digits) if digits else 0


def url_ok(url, timeout=8):
    """True if the image URL is reachable (HTTP < 400). Used by --verify-images to drop dead
    images before they can ship. Returns False on any network error (so offline = treat as bad)."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "SignalsBuild/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= getattr(r, "status", r.getcode()) < 400
    except Exception:
        return False


def fail(errors):
    print("❌ BUILD REJECTED — no draft written, production latest.json untouched:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)


def composition_errors(signals):
    """Final set-level editorial gate (Signals Feed v2.1) — run on the assembled five BEFORE writing
    the draft, so the daily edition feels like 'What Matters Today'. Catches anything that slipped
    selection/drafting: wrong count, two signals on the same topic, or a broken/truncated summary or
    whyItMatters. Returns human-readable errors (empty = OK). Fail-closed: any error rejects the build."""
    errors = []
    if len(signals) != 5:
        errors.append(f"expected exactly 5 signals, got {len(signals)}")
    # Distinct topics — the five must not double-cover one story (e.g. two US/Iran peace-deal items).
    fps = [(f"signal {s.get('number','?')}",
            topic_fingerprint(s.get("headline", ""), s.get("summary", ""))) for s in signals]
    dup = first_duplicate_pair(fps)
    if dup:
        errors.append(f"duplicate topic between {dup[0]} and {dup[1]} — the five must cover distinct stories")
    # Clean, complete summary + whyItMatters per signal (reuses the writer's strict checks).
    for s in signals:
        num = s.get("number", "?")
        for issue in summary_quality_issues(s.get("summary", "")):
            errors.append(f"signal {num}: {issue}")
        for issue in why_quality_issues(s.get("whyItMatters", ""), s.get("summary", ""),
                                        s.get("headline", "")):
            errors.append(f"signal {num}: {issue}")
    return errors


# ── Topic-aware image selection (1.1) ────────────────────────────────────────────────────────
def build_topic_matchers(topic_keywords):
    """topic -> list of compiled whole-word regexes for its keywords (case-insensitive)."""
    matchers = {}
    for topic, kws in (topic_keywords or {}).items():
        matchers[topic] = [re.compile(r"\b" + re.escape(str(k).lower()) + r"\b") for k in (kws or [])]
    return matchers


def match_topic(headline, summary, matchers):
    """Best topic for a story. Keyword hits in the HEADLINE count double (it carries the story's
    essence); summary hits count once. Deterministic: highest score wins, ties broken by topic
    name. Returns (topic, score) or (None, 0)."""
    h = (headline or "").lower()
    s = (summary or "").lower()
    scores = {}
    for topic, pats in matchers.items():
        score = 2 * sum(1 for p in pats if p.search(h)) + sum(1 for p in pats if p.search(s))
        if score > 0:
            scores[topic] = score
    if not scores:
        return None, 0
    topic, score = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return topic, score


def recent_image_urls(editions_dir, exclude_date, window=90):
    """Set of imageURLs used by the most recent `window` editions (by date), excluding `exclude_date`.
    This is a COOLDOWN, not a blacklist: an image outside the window may be reused. If fewer than
    `window` editions exist, all available history is used. Missing dir → empty set."""
    import glob
    dated = []
    for f in glob.glob(os.path.join(editions_dir, "*.json")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", os.path.basename(f))
        if m and m.group(1) != exclude_date:
            dated.append((m.group(1), f))
    urls = set()
    for _, f in sorted(dated)[-window:]:
        try:
            for s in json.load(open(f)).get("signals", []):
                if s.get("imageURL"):
                    urls.add(s["imageURL"])
        except Exception:
            pass
    return urls


def _short_img(u):
    """Compact id for logs, e.g. photo-1501339847302 (drops query/host noise)."""
    return (u or "").split("/")[-1].split("?")[0][:28]


def assign_images(items, cat_pools, aliases, default_pool, topic_pools, matchers, yday,
                  img_pool=None, img_cats=None, img_default=None, avoid=None, lead_index=None,
                  seen_ever=None, log=print):
    """One image per item (uses headline/summary/category): topic pool first, else the category pool.
    Deterministic (rotated by day-of-year `yday`), never repeats within the set, and AVOIDS any URL in
    `avoid` (imageURLs inside the reuse COOLDOWN window) when a fresh candidate exists. The LEAD is
    assigned FIRST so it gets the strongest/freshest topic image.

    This is a cooldown, NOT a blacklist: before ever reusing an in-cooldown image, it first tries every
    verified pool for an image that is outside the cooldown. Only if none exists does it reuse an
    in-cooldown image (logged), rather than shipping a blank. When it picks an image that was used
    before but is now OUTSIDE the cooldown, that reuse is logged as allowed. Returns dicts aligned to
    `items`, each carrying internal provenance (source/license/credit) the caller keeps OUT of the feed.

    TODO (curation): as verified Pexels/Wikimedia entries are added to images.yaml with `source`,
    `license`, and `credit`, this selection automatically diversifies across sources — no code change."""
    img_pool = img_pool or []
    img_cats = img_cats or {}
    img_default = img_default or {"imageURL": "", "placeTime": ""}
    avoid = set(avoid or ())            # imageURLs inside the cooldown window (avoid when possible)
    seen_ever = set(seen_ever or ())    # every imageURL ever used (for cooldown-expiry logging)

    def resolve_cat_pool(cat):
        p = cat_pools.get(cat) or cat_pools.get(aliases.get(cat, "")) or default_pool or img_pool
        if not p and cat in img_cats:
            p = [img_cats[cat]]
        return p or [img_default]

    def rotated(pool):
        return (pool[yday % len(pool):] + pool[:yday % len(pool)]) if pool else []

    def pick(pool, used, *, allow_recent):
        """First entry not already used this issue and (unless allow_recent) not recently used."""
        for e in rotated(pool):
            u = e.get("imageURL")
            if not u or u in used:
                continue
            if not allow_recent and u in avoid:
                log(f"      · skip recently-used image {_short_img(u)}")
                continue
            return e
        return None

    # Assign the LEAD first (freshest topic image), then the rest in their given order.
    order = list(range(len(items)))
    if lead_index is not None and 0 <= lead_index < len(items):
        order = [lead_index] + [i for i in order if i != lead_index]

    used, out = set(), [None] * len(items)
    for i in order:
        it = items[i]
        cat = it.get("category", "OTHER")
        topic, score = match_topic(it.get("headline"), it.get("summary"), matchers)
        img, reason = None, None
        # 1) topic pool (most specific) → 2) category pool → 3) any category pool — all recent-avoiding.
        if topic and topic_pools.get(topic):
            img = pick(topic_pools[topic], used, allow_recent=False)
            if img:
                reason = f"topic '{topic}' (score {score})"
        if img is None:
            img = pick(resolve_cat_pool(cat), used, allow_recent=False)
            if img:
                reason = (f"category '{cat}' (no topic match)" if topic is None
                          else f"category '{cat}' (topic '{topic}' pool exhausted)")
        # 3) ANY verified pool (category + topic + default), still cooldown-avoiding — prefer a
        #    different image over reusing one inside the cooldown.
        if img is None:
            all_pools = list(cat_pools.values()) + list(topic_pools.values())
            if default_pool:
                all_pools.append(default_pool)
            for cp in all_pools:
                img = pick(cp, used, allow_recent=False)
                if img:
                    reason = f"cross-pool '{cat}' (cooldown-avoided)"
                    break
        # 4) cooldown exhausted → only now reuse an in-cooldown image (never blank), and log it.
        if img is None:
            img = (pick(resolve_cat_pool(cat), used, allow_recent=True)
                   or next((e for cp in (list(cat_pools.values()) + list(topic_pools.values())
                                         + ([default_pool] if default_pool else []))
                            for e in rotated(cp)
                            if e.get("imageURL") and e.get("imageURL") not in used), None)
                   or img_default)
            reason = f"category '{cat}' — cooldown exhausted, reused an in-window image"
            log(f"    ⚠ #{items[i].get('number', i + 1)}: no image outside cooldown; reusing "
                f"{_short_img(img.get('imageURL'))}")
        # Cooldown-expiry note: chosen image was used before but is now outside the window → allowed.
        chosen = img.get("imageURL")
        if chosen and chosen in seen_ever and chosen not in avoid:
            log(f"      · reuse allowed (outside {_short_img(chosen)}'s cooldown)")
        used.add(chosen)
        out[i] = {"imageURL": img.get("imageURL", ""), "placeTime": img.get("placeTime", ""),
                  "topic": topic, "reason": reason,
                  # internal provenance — NOT written to the public feed (schema unchanged):
                  "source": img.get("source", "unsplash"),
                  "license": img.get("license"), "credit": img.get("credit"),
                  "isLead": (i == lead_index)}
    return out


def main():
    import yaml   # lazy: only the build run needs it (keeps the module importable for tests)
    ap = argparse.ArgumentParser(description="Signals Builder v1 (approved drafts → draft feed).")
    ap.add_argument("--drafts", default=DEF_DRAFTS)
    ap.add_argument("--images", default=DEF_IMAGES)
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--verify-images", action="store_true",
                    help="HTTP-check every pool image and drop any that fail (run on a networked machine)")
    ap.add_argument("--date", default=None,
                    help="edition date YYYY-MM-DD — the morning it serves (default: tomorrow UTC)")
    args = ap.parse_args()

    if not os.path.exists(args.drafts):
        fail([f"drafts file not found: {args.drafts}"])
    drafts = json.load(open(args.drafts))
    images = yaml.safe_load(open(args.images))
    cat_pools = images.get("category_pools", {})       # per-category pools (fallback tier)
    aliases = images.get("aliases", {})                # Scout category → pool category
    default_pool = images.get("default_pool", [])      # fallback pool
    topic_pools = images.get("topic_pools", {})        # story-aware pools (preferred, 1.1)
    topic_matchers = build_topic_matchers(images.get("topic_keywords", {}))
    img_cats = images.get("categories", {})            # legacy category map (optional)
    img_pool = images.get("pool", [])                  # legacy flat mood pool (optional)
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

        # an image source must exist: a category pool (or alias), a flat pool, or a category map.
        cat = s.get("category", "OTHER")
        has_source = (cat_pools.get(aliases.get(cat, cat)) or cat_pools.get(cat) or default_pool
                      or img_pool or (cat in img_cats))
        if not has_source:
            errors.append(f"[{sid}] no curated image for category {cat!r} (no pool/alias/default)")

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
    # Edition date = the morning this edition serves. Default: tomorrow in UTC. The scheduled build
    # runs in the evening-UTC window, so "tomorrow UTC" names the upcoming morning (and equals
    # "today in Tokyo" at that moment). Override with --date. No timezone is pinned — the client
    # decides whose "today" it is (see ADR-0001).
    if args.date:
        try:
            edition = datetime.date.fromisoformat(args.date)
        except ValueError:
            fail([f"--date must be YYYY-MM-DD, got {args.date!r}"])
    else:
        edition = datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=1)
    today = edition.isoformat()             # the edition's date label
    yday = edition.timetuple().tm_yday      # day-of-year → deterministic daily image rotation

    # --verify-images: drop any URL that doesn't load, per pool, so a dead image can never ship.
    if args.verify_images:
        all_urls = sorted({e["imageURL"]
                           for pool in list(cat_pools.values()) + list(topic_pools.values())
                           + [default_pool, img_pool] for e in pool if e.get("imageURL")})
        dead = {u for u in all_urls if not url_ok(u)}
        print(f"  image verify: {len(all_urls) - len(dead)}/{len(all_urls)} reachable"
              + (f" — dropped {len(dead)}" if dead else ""))
        for name, pool in list(cat_pools.items()):
            cat_pools[name] = [e for e in pool if e["imageURL"] not in dead]
        for name, pool in list(topic_pools.items()):
            topic_pools[name] = [e for e in pool if e["imageURL"] not in dead]
        default_pool[:] = [e for e in default_pool if e["imageURL"] not in dead]
        img_pool[:] = [e for e in img_pool if e["imageURL"] not in dead]

    # Story-aware image assignment (topic pool first, category fallback) — deterministic, no repeats.
    # Also avoids any imageURL used by the last 30 editions, and assigns the LEAD first (freshest pick).
    ordered = sorted(signals_in, key=lambda x: x["number"])
    img_items = [{"number": s["number"],
                  "headline": s["draft"].get("headline", ""), "summary": s["draft"].get("summary", ""),
                  "category": s.get("category", "OTHER")} for s in ordered]
    editions_dir = os.path.join(os.path.dirname(HERE), "editions")
    REUSE_WINDOW = 90                                   # cooldown length (editions); configurable
    recent_urls = recent_image_urls(editions_dir, today, window=REUSE_WINDOW)
    seen_ever = recent_image_urls(editions_dir, today, window=10**9)   # all history (for cooldown-expiry logs)
    lead_index = next((i for i, s in enumerate(ordered) if s.get("selectedRole") == "lead"), 0)
    print(f"  image reuse guard: cooldown={REUSE_WINDOW} editions; avoiding {len(recent_urls)} recent "
          f"imageURLs ({len(seen_ever)} seen ever); lead = #{ordered[lead_index]['number']}")
    picks = assign_images(img_items, cat_pools, aliases, default_pool, topic_pools, topic_matchers,
                          yday, img_pool=img_pool, img_cats=img_cats, img_default=img_default,
                          avoid=recent_urls, lead_index=lead_index, seen_ever=seen_ever)

    print("  image assignment:")
    out_signals = []
    for idx, s in enumerate(ordered):
        dr = s["draft"]
        pick = picks[idx]
        print(f"    #{s['number']} [{s.get('category','?'):8}] {dr['headline'][:42]:42} "
              f"→ {pick['reason']:38} → {pick['placeTime']!r} [{pick.get('source','unsplash')}"
              f"{' · LEAD' if pick.get('isLead') else ''}]")
        out_signals.append({
            "number": s["number"],
            "importance": s["number"],                     # human order = importance (Lead=1)
            "lead": s.get("selectedRole") == "lead",
            "category": s.get("category", "OTHER"),
            "source": s["source"],
            "headline": dr["headline"],
            "summary": dr["summary"],
            "keyTakeaways": dr["keyTakeaways"],
            "whyItMatters": dr["whyItMatters"],
            "originalURL": s["originalURL"],
            "readTime": read_time_int(dr.get("readTime")),   # always Int minutes (iOS expects Int)
            "imageURL": pick["imageURL"],                  # story-aware decorative (topic → category)
            "placeTime": pick["placeTime"],
            "audioURL": "",                                 # empty in v1
        })

    # v2.1 FINAL COMPOSITION GATE — distinct topics + clean summary/whyItMatters across the five.
    # Runs before anything is written; on any problem the build is rejected (fail-closed).
    comp = composition_errors(out_signals)
    if comp:
        fail(["final composition gate (v2.1) — the five are not publish-clean:"] + comp)
    print("  ✓ composition gate: 5 distinct topics, clean summaries + whyItMatters\n")

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
