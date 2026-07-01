#!/usr/bin/env python3
"""
Signals — publish-time feed validator (Lead Signal Rule, Master Context §8 + Trust Pass).

Run this in the daily publishing pipeline BEFORE deploying. It is the strict gate: a feed that
fails ANY check must NOT be published. The app's runtime check is lenient (logs + graceful
fallback); this is where the principle is actually enforced.

Two modes (see ADR-0001 — local-time morning delivery):

  python3 validate_feed.py <feed.json>
      Validate a single feed file: structure + Lead Signal Rule + a valid YYYY-MM-DD date.
      If the path is editions/<DATE>.json, the file's `date` must equal that filename DATE.
      A LOOSE UTC sanity check rejects only absurd far-future dates (a typo guard); it does
      NOT pin Asia/Tokyo or any other timezone. The client decides whose "today" it is.

  python3 validate_feed.py --consistency <repo-root>
      Repo-level invariants for the date-keyed model:
        - every editions/<DATE>.json has internal `date` == DATE
        - latest.json equals the NEWEST editions/<DATE>.json (by date)
        - latest.json is not older than the newest edition (no stale regression)

Exit code 0 = valid (safe to publish); non-zero = rejected (do not publish).
"""
import sys, os, re, json, glob, datetime
from urllib.parse import urlparse

# How far in the future a publish date may be before we treat it as a typo. Lenient on purpose:
# an edition is normally "tomorrow (UTC)" at build time, and we never pin a single timezone.
FUTURE_SANITY_DAYS = 2

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.json$")


def localized_errors(s):
    """Optional `localized` content (Increment F). ABSENT = fine — English-only editions, including
    every edition published before this, stay valid. This only catches a present-but-MALFORMED block
    so the iOS optional `localized.ja` decoder never chokes; it never requires Japanese to exist and
    never weakens an existing check.

    Shape (all fields optional): localized.ja.{ headline:str, summary:str,
                                                keyTakeaways:[str], whyItMatters:str }"""
    num = s.get("number", "?")
    loc = s.get("localized")
    if loc is None:
        return []                                   # no localization → nothing to check
    if not isinstance(loc, dict):
        return [f"signal {num} `localized` must be an object"]
    ja = loc.get("ja")
    if ja is None:
        return []                                   # `localized` present but no `ja` yet → allowed
    if not isinstance(ja, dict):
        return [f"signal {num} `localized.ja` must be an object"]
    errs = []
    for k in ("headline", "summary", "whyItMatters"):
        if k in ja and not (isinstance(ja[k], str) and ja[k].strip()):
            errs.append(f"signal {num} localized.ja.{k} must be a non-empty string")
    if "keyTakeaways" in ja:
        kt = ja["keyTakeaways"]
        if not (isinstance(kt, list) and kt and all(isinstance(x, str) and x.strip() for x in kt)):
            errs.append(f"signal {num} localized.ja.keyTakeaways must be a non-empty list of strings")
    return errs


# Known dialogue languages. Unknown language keys are TOLERATED (forward-compat) but, if they are
# objects, still shape-checked so a malformed track can't ship.
LISTEN_LANG_KEYS = ("en", "ja")


def _listen_track_errors(num, lang, track):
    """Validate one `listen.<lang>` track (audioURL / gap / captions). All fields optional."""
    if not isinstance(track, dict):
        return [f"signal {num} listen.{lang} must be an object"]
    errs = []
    if "audioURL" in track and not (isinstance(track["audioURL"], str) and track["audioURL"].strip()):
        errs.append(f"signal {num} listen.{lang}.audioURL must be a non-empty string")
    if "gap" in track:
        g = track["gap"]
        if isinstance(g, bool) or not isinstance(g, (int, float)) or g < 0:
            errs.append(f"signal {num} listen.{lang}.gap must be a number >= 0")
    if "captions" in track:
        caps = track["captions"]
        if not isinstance(caps, list):
            errs.append(f"signal {num} listen.{lang}.captions must be a list")
        else:
            for j, c in enumerate(caps):
                where = f"signal {num} listen.{lang}.captions[{j}]"
                if not isinstance(c, dict):
                    errs.append(f"{where} must be an object")
                    continue
                if not (isinstance(c.get("speaker"), str) and c["speaker"].strip()):
                    errs.append(f"{where}.speaker must be a non-empty string")
                if not (isinstance(c.get("text"), str) and c["text"].strip()):
                    errs.append(f"{where}.text must be a non-empty string")
                d = c.get("duration")
                if isinstance(d, bool) or not isinstance(d, (int, float)) or d <= 0:
                    errs.append(f"{where}.duration must be a number > 0")
    return errs


def listen_errors(s):
    """Optional conversational `listen` block (Phase 1). ABSENT = fine — every existing edition stays
    valid and the iOS optional decoder is unaffected. This only catches a present-but-MALFORMED block;
    it is never required and never weakens an existing check. Unknown extra fields are tolerated.

    Shape (all optional): listen.{ format:str,
                                   <lang>:{ audioURL:str, gap:number>=0,
                                            captions:[{ speaker:str, text:str, duration:number>0 }] } }"""
    num = s.get("number", "?")
    listen = s.get("listen")
    if listen is None:
        return []                                   # no conversational data → nothing to check
    if not isinstance(listen, dict):
        return [f"signal {num} `listen` must be an object"]
    errs = []
    if "format" in listen and not isinstance(listen["format"], str):
        errs.append(f"signal {num} listen.format must be a string")
    for k, v in listen.items():
        if k == "format":
            continue
        if k in LISTEN_LANG_KEYS:
            errs += _listen_track_errors(num, k, v)            # known language → must be a valid track
        elif isinstance(v, dict):
            errs += _listen_track_errors(num, k, v)            # unknown language → tolerated, but shape-checked
        # non-dict unknown extra fields → tolerated (ignored) for forward compatibility
    return errs


def structural_errors(feed):
    """Lead Signal Rule + content checks — independent of any clock."""
    errors = []
    signals = feed.get("signals", [])

    if len(signals) != 5:
        errors.append(f"expected 5 signals, found {len(signals)}")

    for s in signals:
        imp = s.get("importance")
        if imp is None:
            errors.append(f"signal {s.get('number','?')} missing `importance`")
        elif not (isinstance(imp, int) and 1 <= imp <= 5):
            errors.append(f"signal {s.get('number','?')} importance {imp!r} not in 1..5")

    leads = [s for s in signals if s.get("lead") is True]
    if len(leads) == 0:
        errors.append("no signal has `lead: true`")
    elif len(leads) > 1:
        errors.append(f"multiple leads ({len(leads)}) — exactly one required")

    imps = [s["importance"] for s in signals if isinstance(s.get("importance"), int)]
    if len(leads) == 1 and imps:
        lead_imp = leads[0].get("importance")
        if isinstance(lead_imp, int) and lead_imp != min(imps):
            errors.append(
                f"lead is importance {lead_imp} but the highest tier present is {min(imps)} "
                f"— the lead must be the most important story")

    for s in signals:
        if not (isinstance(s.get("summary"), str) and s["summary"].strip()):
            errors.append(f"signal {s.get('number','?')} has an empty/missing summary")

    # readTime must be an integer (iOS FeedSignal.readTime is Int — a string like "3 min" breaks
    # the whole feed decode and silently drops the app back to bundled fallback.json).
    for s in signals:
        rt = s.get("readTime")
        if not isinstance(rt, int) or isinstance(rt, bool):
            errors.append(f"signal {s.get('number','?')} readTime must be an integer, got {rt!r}")

    for s in signals:
        url = s.get("originalURL", "")
        p = urlparse(url)
        if p.scheme != "https" or not p.netloc:
            errors.append(f"signal {s.get('number','?')} originalURL not https: {url!r}")
        elif p.path.strip("/") == "":
            errors.append(f"signal {s.get('number','?')} originalURL is a homepage, not an article: {url!r}")

    # Optional localized content (Increment F) — only flagged if present-but-malformed.
    for s in signals:
        errors += localized_errors(s)

    # Optional conversational `listen` block (Phase 1) — only flagged if present-but-malformed.
    for s in signals:
        errors += listen_errors(s)

    return errors


def validate(path):
    """Single-file validation: structure + a valid date (+ filename match + loose future guard)."""
    errors = []
    try:
        feed = json.load(open(path))
    except Exception as e:
        return [f"feed is not valid JSON: {e}"]

    errors += structural_errors(feed)

    # date must be a valid YYYY-MM-DD label (timezone-agnostic — it names the morning it serves).
    raw = feed.get("date")
    feed_date = None
    try:
        feed_date = datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        errors.append(f"invalid or missing feed date: {raw!r}")

    if feed_date is not None:
        # editions/<DATE>.json — the internal date must equal the filename date.
        m = DATE_RE.search(os.path.basename(path))
        if m and m.group(1) != raw:
            errors.append(
                f"edition filename date {m.group(1)} != internal date {raw!r} "
                f"(editions/<DATE>.json must be self-consistent)")

        # LOOSE UTC sanity (typo guard only): reject an absurd far-future date. Not a timezone
        # rule — the client decides whose "today" it is; this only catches obvious mistakes.
        utc_today = datetime.datetime.now(datetime.timezone.utc).date()
        if feed_date > utc_today + datetime.timedelta(days=FUTURE_SANITY_DAYS):
            errors.append(
                f"implausible future date: {raw} is more than {FUTURE_SANITY_DAYS} days "
                f"ahead of UTC today ({utc_today}) — likely a typo")

    return errors


def consistency_errors(root):
    """Repo-level invariants for the date-keyed editions model."""
    errors = []
    edir = os.path.join(root, "editions")
    files = sorted(glob.glob(os.path.join(edir, "*.json")))
    if not files:
        return [f"no editions/*.json found under {root!r} — nothing to check"]

    dated = []   # (date, path, parsed)
    for f in files:
        m = DATE_RE.search(os.path.basename(f))
        if not m:
            errors.append(f"edition file not named editions/<YYYY-MM-DD>.json: {os.path.basename(f)}")
            continue
        try:
            data = json.load(open(f))
        except Exception as e:
            errors.append(f"{os.path.basename(f)} is not valid JSON: {e}")
            continue
        if data.get("date") != m.group(1):
            errors.append(f"{os.path.basename(f)} internal date {data.get('date')!r} != filename {m.group(1)}")
        dated.append((m.group(1), f, data))

    if not dated:
        return errors or ["no valid editions found"]

    newest_date, newest_path, newest_data = max(dated, key=lambda t: t[0])

    latest_path = os.path.join(root, "latest.json")
    if not os.path.exists(latest_path):
        errors.append("latest.json is missing")
        return errors
    try:
        latest = json.load(open(latest_path))
    except Exception as e:
        return errors + [f"latest.json is not valid JSON: {e}"]

    if latest.get("date") != newest_date:
        errors.append(
            f"latest.json date {latest.get('date')!r} != newest edition {newest_date} "
            f"({os.path.basename(newest_path)}) — latest.json must point at the newest edition "
            f"(stale-date regression)")
    elif latest != newest_data:
        errors.append(
            f"latest.json content differs from editions/{newest_date}.json — "
            f"latest.json must be an exact copy of the newest edition")

    return errors


def image_reuse_errors(root, window=90):
    """Opt-in image-variety check: the NEWEST edition must not reuse an imageURL from the previous
    `window` editions (the reuse COOLDOWN; default 90). Reuse OUTSIDE the window is fine — this is a
    cooldown, not a blacklist. Kept SEPARATE from the default gates so it never breaks existing history;
    it is a forward-looking guard for freshly built editions (build.py already avoids reuse at build)."""
    edir = os.path.join(root, "editions")
    dated = []
    for f in glob.glob(os.path.join(edir, "*.json")):
        m = DATE_RE.search(os.path.basename(f))
        if not m:
            continue
        try:
            dated.append((m.group(1), json.load(open(f))))
        except Exception as e:
            return [f"{os.path.basename(f)} is not valid JSON: {e}"]
    if len(dated) < 2:
        return []                                   # nothing to compare against yet
    dated.sort(key=lambda t: t[0])
    newest_date, newest = dated[-1]
    prior = dated[-(window + 1):-1]                 # up to `window` editions before the newest
    recent = {s["imageURL"] for _, d in prior for s in d.get("signals", []) if s.get("imageURL")}
    errs = []
    for s in newest.get("signals", []):
        u = s.get("imageURL")
        if u and u in recent:
            errs.append(f"[{newest_date}] signal {s.get('number','?')} reuses an imageURL from the "
                        f"last {window} editions: {u}")
    return errs


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--consistency":
        errs = consistency_errors(sys.argv[2])
        label = "repo consistency"
    elif len(sys.argv) == 3 and sys.argv[1] == "--image-reuse":
        errs = image_reuse_errors(sys.argv[2])
        label = "image reuse (newest edition vs last 90 — cooldown)"
    elif len(sys.argv) == 2 and not sys.argv[1].startswith("--"):
        errs = validate(sys.argv[1])
        label = sys.argv[1]
    else:
        print("usage: python3 validate_feed.py <feed.json>")
        print("       python3 validate_feed.py --consistency <repo-root>")
        print("       python3 validate_feed.py --image-reuse <repo-root>")
        sys.exit(2)

    if errs:
        print(f"❌ REJECTED ({label}) — do not publish:")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)
    print(f"✅ valid ({label}) — safe to publish.")
    sys.exit(0)
