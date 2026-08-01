#!/usr/bin/env python3
"""Regression tests for the cross-day story cooldown in ranker.py.

Locks down the production failure where the same Samsung Unpacked article appeared in
four consecutive editions even though within-edition duplicate protection was working.
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ranker  # noqa: E402

NOW = datetime.datetime(2026, 7, 29, 9, 0, tzinfo=datetime.timezone.utc)
FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def candidate(title, url, snippet=""):
    return {
        "id": url.rsplit("/", 1)[-1],
        "title": title,
        "snippet": snippet,
        "url": url,
        "canonical_url": url,
        "source": "The Verge (Mobile)",
        "source_reliability": "high",
    }


def prior(date, rank, headline, url, summary=""):
    return {"edition_date": date, "edition_rank": rank, "headline": headline,
            "summary": summary, "url": url}


roundup = candidate(
    "Samsung Galaxy Unpacked 2026: The 6 biggest announcements",
    "https://www.theverge.com/tech/831-unpacked",
    "Samsung announced its new Galaxy foldables at Unpacked.",
)

# Exact URL survives query changes but never a consecutive edition.
p = [prior("2026-07-28", 1, "A different display headline",
           "https://www.theverge.com/tech/831-unpacked?utm_source=rss")]
match, _, rule = ranker.recent_repeat_of(roundup, p, NOW)
check("same URL on previous edition is rejected", match is not None and rule == "recent-exact-url", rule)

# A publisher toggling its cosmetic www subdomain must not bypass URL identity.
www_roundup = dict(roundup, url="https://www.theverge.com/tech/831-unpacked",
                   canonical_url="https://www.theverge.com/tech/831-unpacked")
p = [prior("2026-07-28", 1, "A different display headline",
           "https://theverge.com/tech/831-unpacked")]
match, _, rule = ranker.recent_repeat_of(www_roundup, p, NOW)
check("www and bare-domain forms are the same article URL",
      match is not None and rule == "recent-exact-url", rule)

# Exact headline is caught even when the publisher URL changed.
p = [prior("2026-07-28", 1, roundup["title"], "https://mirror.example.com/story")]
match, _, rule = ranker.recent_repeat_of(roundup, p, NOW)
check("same headline on previous edition is rejected",
      match is not None and rule == "recent-exact-headline", rule)

# A hands-on is a different URL/headline but the same product event.
hands_on = candidate(
    "Samsung's wider Z Fold 8 feels just right",
    "https://www.theverge.com/tech/z-fold-8-hands-on",
    "The foldable shown at Samsung Unpacked has a wider display.",
)
p = [prior("2026-07-28", 1, roundup["title"], roundup["url"], roundup["snippet"])]
match, _, rule = ranker.recent_repeat_of(hands_on, p, NOW)
check("same Samsung product event under another article is rejected",
      match is not None and rule.startswith("recent-"), rule)

# Same company, materially different news remains eligible.
earnings = candidate(
    "Samsung's quarterly profit rises on strong chip demand",
    "https://www.bbc.com/news/business/samsung-profit",
    "The semiconductor division reported higher quarterly earnings.",
)
match, _, rule = ranker.recent_repeat_of(earnings, p, NOW)
check("unrelated Samsung earnings story remains eligible", match is None, rule)

# Product-event cooldown ends after three editions, while exact-copy cooldown stays seven.
p_old_product = [prior("2026-07-25", 4, roundup["title"],
                       "https://other.example.com/unpacked", roundup["snippet"])]
match, _, rule = ranker.recent_repeat_of(hands_on, p_old_product, NOW)
check("different article about product event is allowed after three editions", match is None, rule)

p_old_exact = [prior("2026-07-25", 4, roundup["title"], roundup["url"], roundup["snippet"])]
match, _, rule = ranker.recent_repeat_of(roundup, p_old_exact, NOW)
check("exact article remains blocked throughout seven-edition cooldown",
      match is not None and rule == "recent-exact-url", rule)

# The filter is hard and leaves no path for emergency selection to add the repeat back.
fresh = candidate("Astronomers map a newly found galaxy", "https://bbc.com/news/science/galaxy")
logs = []
kept, rejected = ranker.exclude_recent_repeats([roundup, fresh], p_old_exact, NOW, logs.append)
check("filter removes repeat and preserves distinct story",
      kept == [fresh] and rejected == [roundup], f"kept={len(kept)} rejected={len(rejected)}")
check("rejection is auditable", any("recent repeat rejected" in line for line in logs), logs)

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL PASS")
