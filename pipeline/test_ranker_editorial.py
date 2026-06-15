#!/usr/bin/env python3
"""
Editorial-quality tests for the Ranker (Increment G). Proves low-signal content is EXCLUDED from
the morning five while real civic/geopolitical/economic stories stay eligible.

Run: python3 pipeline/test_ranker_editorial.py   (stdlib only; exits non-zero on any failure)
"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ranker  # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0
NOW = datetime.datetime(2026, 6, 16, 12, 0, tzinfo=datetime.timezone.utc)


def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


def cand(title, source="Reuters", category="WORLD"):
    url = "https://example.com/news/" + title.lower().replace(" ", "-")[:30]
    # No explicit "id" → short_id falls back to selection_id(sha1(canonical_url)), so resolvable() holds.
    return {
        "title": title, "source": source, "category": category,
        "url": url, "canonical_url": url,
        "snippet": ("This is a substantial news snippet with enough real reporting text to clear the "
                    "unknown-source threshold and read as a genuine article body for the writer. " * 2),
        "published_at": "2026-06-16T08:00:00Z", "cluster_size": 2, "cluster_id": title,
    }


# ── Known-bad headlines must be classified as non-core (excluded) ──
print("Excluded (low-signal) headlines are classified as editorial junk:")
bad = {
    "podcast": "The Vergecast: our universal remote podcast episode on smart homes",
    "entertainment/franchise": "X-Men and Masters of the Universe: the franchise analysis we needed",
    "product/deal": "The best earbuds of 2026: our favorite headphones on sale",
    "review/buying guide": "Review: the new smartwatch is a hands-on disappointment",
    "personal essay": "How I quit my job and built my dream garden shed",
    "nostalgia essay": "An ode to the gadgets we grew up with",
}
for kind, title in bad.items():
    k = ranker.editorial_kind(cand(title))
    check(f"{title[:42]!r} → junk", k != "", f"got {k!r}")
    check(f"  excluded from eligibility", not ranker.eligible(cand(title), NOW, 48), f"kind={k!r}")


# ── Real civic/geopolitical/economic stories stay eligible and are NOT junk ──
print("\nReal 'what matters' headlines stay eligible:")
good = [
    "Ceasefire talks resume as both sides agree to a temporary truce",
    "Central bank holds interest rates steady amid cooling inflation",
    "New climate rules will cut power-plant emissions by 2030",
    "Court rules on landmark privacy lawsuit against data broker",
    "Port strike threatens supply chains across the region",
]
for title in good:
    check(f"{title[:42]!r} → not junk", ranker.editorial_kind(cand(title)) == "")
    check(f"  eligible", ranker.eligible(cand(title), NOW, 48))
    check(f"  importance bonus applied", ranker.importance_bonus(cand(title)) > 0, title)


# ── A feed of only junk fails closed (no 5 eligible) rather than filling with it ──
print("\nA junk-only candidate set yields < 5 eligible (fail-closed, not filled):")
junk_only = [cand(t, source=s) for t, s in
             [(v, "The Verge") for v in bad.values()] + [("Deal: 50% off blenders today", "Wirecutter")]]
n_eligible = sum(1 for c in junk_only if ranker.eligible(c, NOW, 48))
check("fewer than 5 eligible from junk-only set", n_eligible < 5, f"eligible={n_eligible}")


print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
