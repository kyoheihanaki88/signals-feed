#!/usr/bin/env python3
"""
Signals — publish-time feed validator (Lead Signal Rule, Master Context §8 + Trust Pass).

Run this in the daily publishing pipeline BEFORE deploying latest.json. It is the strict gate:
a feed that fails ANY check must NOT be published. The app's runtime check is lenient (logs +
graceful fallback); this is where the principle is actually enforced.

Usage:
    python3 validate_feed.py latest.json
Exit code 0 = valid (safe to publish); non-zero = rejected (do not publish).

Rejects:
  - no lead                          (exactly one signal must have lead: true)
  - multiple leads
  - lead not in highest importance tier   (lead.importance must equal the min importance present)
  - missing importance               (every signal needs importance in 1..5)
  - missing summaries                (every signal needs a non-empty summary)
  - invalid article URLs             (must be https AND a real article path, not a homepage)
  - stale date                       (feed date must be today)
"""
import sys, json, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

TOKYO = ZoneInfo("Asia/Tokyo")   # publish timezone — all date checks use this, not the host's TZ

def validate(path):
    errors = []
    try:
        feed = json.load(open(path))
    except Exception as e:
        return [f"feed is not valid JSON: {e}"]

    signals = feed.get("signals", [])

    # exactly five signals
    if len(signals) != 5:
        errors.append(f"expected 5 signals, found {len(signals)}")

    # importance present + in range
    for s in signals:
        imp = s.get("importance")
        if imp is None:
            errors.append(f"signal {s.get('number','?')} missing `importance`")
        elif not (isinstance(imp, int) and 1 <= imp <= 5):
            errors.append(f"signal {s.get('number','?')} importance {imp!r} not in 1..5")

    # exactly one lead
    leads = [s for s in signals if s.get("lead") is True]
    if len(leads) == 0:
        errors.append("no signal has `lead: true`")
    elif len(leads) > 1:
        errors.append(f"multiple leads ({len(leads)}) — exactly one required")

    # lead in highest tier present (lowest importance number)
    imps = [s["importance"] for s in signals if isinstance(s.get("importance"), int)]
    if len(leads) == 1 and imps:
        lead_imp = leads[0].get("importance")
        if isinstance(lead_imp, int) and lead_imp != min(imps):
            errors.append(
                f"lead is importance {lead_imp} but the highest tier present is {min(imps)} "
                f"— the lead must be the most important story")

    # summaries present
    for s in signals:
        if not (isinstance(s.get("summary"), str) and s["summary"].strip()):
            errors.append(f"signal {s.get('number','?')} has an empty/missing summary")

    # readTime must be an integer (the iOS FeedSignal.readTime is Int — a string like "3 min"
    # breaks the whole feed decode and silently drops the app back to bundled fallback.json).
    for s in signals:
        rt = s.get("readTime")
        if not isinstance(rt, int) or isinstance(rt, bool):
            errors.append(f"signal {s.get('number','?')} readTime must be an integer, got {rt!r}")

    # article URLs: https + a real path (not a bare homepage)
    for s in signals:
        url = s.get("originalURL", "")
        p = urlparse(url)
        if p.scheme != "https" or not p.netloc:
            errors.append(f"signal {s.get('number','?')} originalURL not https: {url!r}")
        elif p.path.strip("/") == "":
            errors.append(f"signal {s.get('number','?')} originalURL is a homepage, not an article: {url!r}")

    # date freshness (live feed only; the bundled fallback.json is exempt): the feed date must be
    # TODAY or YESTERDAY in the publish timezone (Asia/Tokyo). This grace window tolerates a
    # build/review that crosses local midnight, while still rejecting future dates and any feed
    # older than yesterday (e.g. the May-30 seed).
    tokyo_today = datetime.datetime.now(TOKYO).date()
    raw = feed.get("date")
    try:
        feed_date = datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        errors.append(f"invalid or missing feed date: {raw!r}")
    else:
        if feed_date > tokyo_today:
            errors.append(f"future-dated feed: {raw} is ahead of today ({tokyo_today}, Asia/Tokyo)")
        elif feed_date < tokyo_today - datetime.timedelta(days=1):
            errors.append(f"stale date: feed date {raw} is older than yesterday ({tokyo_today}, Asia/Tokyo)")

    return errors

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 validate_feed.py <feed.json>"); sys.exit(2)
    errs = validate(sys.argv[1])
    if errs:
        print("❌ FEED REJECTED — do not publish:")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)
    print("✅ feed valid — safe to publish.")
    sys.exit(0)
