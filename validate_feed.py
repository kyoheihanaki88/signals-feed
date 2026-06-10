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


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--consistency":
        errs = consistency_errors(sys.argv[2])
        label = "repo consistency"
    elif len(sys.argv) == 2 and not sys.argv[1].startswith("--"):
        errs = validate(sys.argv[1])
        label = sys.argv[1]
    else:
        print("usage: python3 validate_feed.py <feed.json>")
        print("       python3 validate_feed.py --consistency <repo-root>")
        sys.exit(2)

    if errs:
        print(f"❌ REJECTED ({label}) — do not publish:")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)
    print(f"✅ valid ({label}) — safe to publish.")
    sys.exit(0)
