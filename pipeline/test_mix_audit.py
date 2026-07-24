#!/usr/bin/env python3
"""Tests for the Morning Mix selection audit log (ranker.audit_candidates) — commit 4.

Every serious candidate must get an explainable line: base score, mix components, final
adjusted score, and a selected/rejected reason. No article bodies/snippets may leak."""
import contextlib
import datetime
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ranker  # noqa: E402

NOW = datetime.datetime(2026, 7, 24, 9, 0, tzinfo=datetime.timezone.utc)
HIST = ({("Ukraine", "conflict"): ["2026-07-23"]}, ["2026-07-23", "2026-07-22", "2026-07-21"])
FAILURES = []
SECRET_SNIPPET = "UNIQUE-BODY-MARKER article body text that must never appear in audit logs. " * 3


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def cand(title, category="WORLD", source="BBC News (World)", **kw):
    url = "https://example.com/news/" + "".join(ch if ch.isalnum() else "-" for ch in title.lower())[:60]
    c = {"title": title, "source": source, "category": category,
         "publisher": source.split(" (")[0], "url": url, "canonical_url": url,
         "published_at": "2026-07-24T06:00:00+00:00", "snippet": SECRET_SNIPPET,
         "paywalled": False, "source_reliability": "high",
         "cluster_id": url, "cluster_size": 2, "cluster_sources": 1}
    c.update(kw)
    return c


lead = cand("Summit opens on maritime security cooperation")
launch = cand("Acme launches its first smartphone for the mass market", category="TECH",
              source="The Verge (Mobile)")
ua = cand("Airstrikes pound the eastern front near Kharkiv in Ukraine")
earn = cand("Retailer's quarterly profit rises 12 percent", category="ECONOMY")
pool = [launch, ua, earn,
        cand("Cyclone nears the coast as evacuations begin"),
        cand("Landslide buries villages in the mountain region"),
        cand("Museum returns looted artifacts after a decade-long dispute", category="CULTURE"),
        cand("Officials report record migrant crossings at the border")]

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    supporting = ranker.pick_supporting(pool, lead, NOW, 36, history=HIST, need=4)
    five = [lead] + supporting
    ranker.audit_candidates(pool, lead, five, NOW, 36, HIST)
log = buf.getvalue()

expected = len(pool) + 1              # + the lead, audited by the five-completion pass
check("audit emits one entry per serious candidate (+ the lead)",
      log.count("  audit id=") == expected, f"{log.count('  audit id=')} vs {expected}")
check("every audit line has base, mix, and final adjusted score",
      log.count("base=") == expected and log.count("adj=") == expected)
check("launch boost visible in the audit", "launch+3.5" in log)
check("earnings penalty visible in the audit", "earnings-1.5" in log)
check("recent-edition history penalty visible with the exact match",
      "history-2.5 (Ukraine/conflict in 2026-07-23)" in log)
check("selected stories carry a selected reason (4 supporting + lead)",
      log.count("→ SELECTED") == 5)
check("lead is explained as unchanged by the mix", "lead selection unchanged" in log)
check("rejected stories carry an explicit reason",
      "→ rejected:" in log)
check("WORLD cap rejections name the cap", "rejected: WORLD cap" in log)
check("no article body / snippet text leaks into the audit", "UNIQUE-BODY-MARKER" not in log)
check("neutral candidates say so", "no mix adjustments (neutral metadata)" in log)

# lead audited even though it is not in the support pool? lead IS audited when in pool;
# here it isn't in pool, so the five-completion pass must add it:
check("the five are always audited even beyond the pool/limit",
      f"id={ranker.short_id(lead)}" in log and "SELECTED (lead" in log)

# determinism of the full audit output
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    s2 = ranker.pick_supporting(pool, lead, NOW, 36, history=HIST, need=4)
    ranker.audit_candidates(pool, lead, [lead] + s2, NOW, 36, HIST)
check("audit output is deterministic", buf2.getvalue() == log)

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL PASS")
