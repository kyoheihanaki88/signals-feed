#!/usr/bin/env python3
"""
Signals Feed Editorial Quality v2.1 — tests for the new gates:
  - duplicate-topic detection (US/Iran peace-deal variants)
  - broken-text validation (dangling 'between the U.S.', mid-sentence summary)
  - arts/entertainment review exclusion (music/album review)
  - high-impact tech exception (Amazon / Anthropic / White House stays eligible)
  - selection replaces duplicate topics rather than publishing < 5
  - a clean five passes the final composition gate

Run: python3 pipeline/test_editorial_v21.py   (stdlib only; exits non-zero on any failure)
"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import editorial, writer, ranker, build  # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0
NOW = datetime.datetime(2026, 6, 17, 12, 0, tzinfo=datetime.timezone.utc)


def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


# ── 1) Duplicate US/Iran peace-deal variants share a topic and are detected ──
print("1) Duplicate US/Iran peace-deal variants are detected:")
variants = [
    "US and Iran reach a peace deal after months of talks",
    "U.S.-Iran peace talks near a breakthrough",
    "Trump says the Iran deal is close",
    "Washington and Tehran resume negotiations",
]
fps = [editorial.topic_fingerprint(v) for v in variants]
for i in range(1, len(variants)):
    check(f"variant {i} overlaps variant 0", editorial.topics_overlap(fps[0], fps[i]),
          f"{set(fps[0])} vs {set(fps[i])}")
# a clearly different story must NOT collide with the Iran topic
amazon_fp = editorial.topic_fingerprint("Amazon and Anthropic deepen AI deal with White House backing")
check("Amazon/Anthropic story is NOT a duplicate of the Iran story",
      not editorial.topics_overlap(fps[0], amazon_fp), f"{set(amazon_fp)} vs {set(fps[0])}")


# ── 2) Broken-text validation ──
print("\n2) Broken text fails validation:")
check("whyItMatters ending 'between the U.S.' fails",
      writer.why_quality_issues("The standoff risks widening the conflict between the U.S.", "x") != [],
      str(writer.why_quality_issues("The standoff risks widening the conflict between the U.S.", "x")))
check("summary ending mid-sentence (dangling 'with the') fails",
      writer.summary_quality_issues("The central bank held rates steady this week amid talks with the") != [],
      "should flag")
check("summary ending mid-sentence (no punctuation) fails",
      "fragment" in " ".join(writer.summary_quality_issues(
          "The council approved a long term concession with private investors covering costs")),
      "should flag fragment")
check("unbalanced quotes fail",
      any("unmatched" in i for i in writer.summary_quality_issues(
          'Officials said the "deal is done and final and complete and ready for everyone today.')),
      "should flag unmatched quotes")
# a clean sentence with a legitimate 'in the U.S.' ending must still PASS (no false positive)
check("legit complete sentence ending 'in the U.S.' passes",
      writer.summary_quality_issues(
          "The new rules will reshape how cloud providers operate across data centers in the U.S.") == [],
      str(writer.summary_quality_issues(
          "The new rules will reshape how cloud providers operate across data centers in the U.S.")))


# ── 3) Arts/entertainment review excluded; high-impact tech stays eligible ──
print("\n3) Editorial classification (exclude reviews, keep high-impact tech):")
def cand(title, source="Reuters", category="WORLD"):
    url = "https://example.com/news/" + title.lower().replace(" ", "-")[:30]
    return {"title": title, "source": source, "category": category, "url": url,
            "canonical_url": url, "cluster_size": 2, "cluster_id": title,
            "published_at": "2026-06-17T08:00:00Z",
            "snippet": ("A substantial news snippet with enough genuine reporting body text to clear "
                        "the unknown-source threshold for the writer to draft from. " * 2)}

for t in ["Album review: the new record is a quiet triumph",
          "Music review: an indie band's comeback single",
          "X-Men and Masters of the Universe: an entertainment analysis"]:
    check(f"{t[:38]!r} excluded", not ranker.eligible(cand(t, source="The Verge"), NOW, 48),
          f"kind={ranker.editorial_kind(cand(t))!r}")

for t in ["Amazon and Anthropic deepen AI partnership with White House backing",
          "White House weighs new antitrust action against a major cloud provider",
          "OpenAI faces federal probe over national security concerns"]:
    check(f"{t[:38]!r} stays eligible", ranker.eligible(cand(t, source="The Verge", category="TECH"), NOW, 48),
          f"kind={ranker.editorial_kind(cand(t))!r}")


# ── 4) Selection replaces duplicate topics rather than dropping below 5 ──
print("\n4) pick_supporting replaces duplicate topics with distinct ones:")
lead = cand("US and Iran reach a peace deal after months of talks", category="WORLD")
support_pool = [
    cand("U.S.-Iran peace talks near a breakthrough", category="WORLD"),      # dup of lead → skip
    cand("Washington and Tehran resume negotiations", category="WORLD"),      # dup of lead → skip
    cand("Central bank holds interest rates steady amid cooling inflation", category="ECONOMY"),
    cand("New climate rules will cut power-plant emissions by 2030", category="SCIENCE"),
    cand("Court rules on a landmark privacy lawsuit against a data broker", category="TECH"),
    cand("Port strike threatens supply chains across the region", category="BUSINESS"),
]
chosen = ranker.pick_supporting(support_pool, lead, NOW, 36, need=4)
check("picked exactly 4 supporting", len(chosen) == 4, f"got {len(chosen)}")
five_fps = [editorial.topic_fingerprint(lead["title"])] + [editorial.topic_fingerprint(c["title"]) for c in chosen]
distinct = all(not editorial.topics_overlap(five_fps[i], five_fps[j])
               for i in range(5) for j in range(i + 1, 5))
check("the final five are all topic-distinct (no duplicate US/Iran)", distinct)
check("no Iran duplicate slipped into supporting",
      not any("iran" in editorial.topic_fingerprint(c["title"]) for c in chosen), str([c["title"] for c in chosen]))


# ── 5) A clean five passes the final composition gate; a broken/duplicate set fails ──
print("\n5) Final composition gate (build.composition_errors):")
def sig(n, headline, summary, why, lead=False):
    return {"number": n, "lead": lead, "headline": headline, "summary": summary, "whyItMatters": why}

clean_five = [
    sig(1, "US and Iran reach a peace deal",
        "After months of negotiation, the two governments agreed to a phased de-escalation that "
        "lifts some sanctions in exchange for verified limits on enrichment.",
        "It reshapes security across the region for years to come.", lead=True),
    sig(2, "Central bank holds rates steady",
        "The central bank left interest rates unchanged this week, citing cooling inflation and a "
        "labor market that is gradually coming back into balance.",
        "Borrowing costs for households and businesses stay put for now."),
    sig(3, "New climate rules target power plants",
        "Regulators finalized rules requiring power plants to cut emissions sharply by 2030, the "
        "most significant climate action from the agency in a decade.",
        "It changes how the country's electricity is generated."),
    sig(4, "Amazon and Anthropic deepen AI partnership",
        "Amazon expanded its partnership with Anthropic, committing new cloud capacity as regulators "
        "in Washington weigh how much influence the deal gives each company.",
        "It concentrates AI infrastructure in a few powerful hands."),
    sig(5, "Port strike threatens supply chains",
        "A strike at several major ports entered its second week, snarling shipments and raising "
        "costs for retailers heading into the season.",
        "Everyday prices could rise if the standoff drags on."),
]
check("a clean, distinct five passes the gate", build.composition_errors(clean_five) == [],
      str(build.composition_errors(clean_five)))

dup_set = [clean_five[0],
           sig(2, "U.S.-Iran peace talks near a breakthrough",   # duplicate topic of #1
               "Negotiators said a U.S.-Iran agreement was within reach after a long round of talks "
               "that narrowed the remaining differences between the two sides.",
               "A deal would ease tensions across the region.")] + clean_five[2:]
check("a duplicate-topic five is rejected", build.composition_errors(dup_set) != [],
      str(build.composition_errors(dup_set)))

broken = clean_five[:4] + [sig(5, "Port strike threatens supply chains",
                               "A strike at several major ports entered its second week amid talks with the",
                               "The conflict could widen between the U.S.")]
errs = build.composition_errors(broken)
check("a broken summary/why five is rejected", errs != [], str(errs))


print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
