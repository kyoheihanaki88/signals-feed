#!/usr/bin/env python3
"""Editorial gate v2.6 — regression fixtures from the 2026-07-10 edition.

Covers: abbreviation-split fragments ("…between the U.S."), newsletter roundup lines,
media-credit captions as prose (INFOCA), career-history author bios (The Verge),
headline-glued summaries (Spain wildfire), whitespace-before-punctuation artifacts,
connective openers ("And, …") — plus pass-through checks for normal copy, and the
validate_feed.editorial_errors defense-in-depth layer.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import writer
import validate_feed

failures = 0
def check(name, cond):
    global failures
    print(("✓" if cond else "✗"), name)
    if not cond:
        failures += 1

# ---------------------------------------------------------------- 2026-07-10 fixtures
FRAG1 = "Fighting between the U.S."
FRAG2 = "After two days of intense strikes , fighting between the U.S."
ROUNDUP = "Up First briefing: Iran-US; TPS; Election Assistance Commission; Gaza Fighting between the U.S."
CAPTION = ("This image made from video provided by INFOCA shows firefighters battling a wildfire "
           "near Los Gallardos, Almeria, Spain, on Thursday, July 9, 2026.")
BIO = "He joined The Verge in 2019 after nearly two years at Techmeme."
GLUE_HEADLINE = "One of Spain's deadliest wildfires has killed at least 11 people"
GLUE_SUMMARY = ("One of Spain's deadliest wildfires has killed at least 11 people A wildfire in "
                "southern Spain has killed at least 11 people, making it one of the country's "
                "deadliest on record, as soaring temperatures grip much of the country.")
AND_OPEN = "And, a look at what life is like inside Israel's expanding zone of control in Gaza."

# ---------------------------------------------------------------- bullets must fail
check("fragment 'between the U.S.' bullet rejected", not writer._well_formed_bullet(FRAG1))
check("truncated strikes bullet rejected", not writer._well_formed_bullet(FRAG2))
check("roundup TOC bullet rejected", not writer._well_formed_bullet(ROUNDUP))
check("INFOCA caption bullet rejected", not writer._well_formed_bullet(CAPTION))
check("Verge author bio bullet rejected", not writer._well_formed_bullet(BIO))
check("'And,' opener bullet rejected", not writer._well_formed_bullet(AND_OPEN))

# classification helpers
check("INFOCA caption detected as caption", writer.looks_like_caption(CAPTION))
check("'video released by' detected as caption",
      writer.looks_like_caption("Video released by the coastguard shows the vessel listing heavily."))
check("Verge bio detected as metadata", writer.looks_like_metadata(BIO))
check("'She joined X in 2020' detected as metadata",
      writer.looks_like_metadata("She joined Wired in 2020 after five years covering startups."))

# ---------------------------------------------------------------- summary / why gates
check("'And,' summary blocked", writer.summary_quality_issues(AND_OPEN) != [])
check("roundup summary blocked", writer.summary_quality_issues(ROUNDUP) != [])
check("abbrev-end why blocked", writer.why_quality_issues(FRAG2, "x") != [])

# ---------------------------------------------------------------- headline glue
stripped = writer._strip_headline_glue(GLUE_SUMMARY, GLUE_HEADLINE)
check("glued summary stripped to body sentence",
      stripped.startswith("A wildfire in southern Spain") and "11 people A wildfire" not in stripped)
check("stripped remainder is a quality summary", writer.summary_quality_issues(stripped) == [])
check("non-glued sentence untouched",
      writer._strip_headline_glue("A separate storm system moved north on Friday, forecasters said.",
                                  GLUE_HEADLINE)
      == "A separate storm system moved north on Friday, forecasters said.")
check("glue with broken remainder dropped",
      writer._strip_headline_glue(GLUE_HEADLINE + " and more.", GLUE_HEADLINE) == "")

# ---------------------------------------------------------------- splitter & normalization
sents = writer.clean_sentences(
    "Officials said the U.S. will respond to the strikes within days, according to the ministry. "
    "Talks continued in Geneva on Friday between both delegations, mediators said.")
check("no split at 'U.S.' (one sentence kept whole)",
      any("U.S. will respond" in s for s in sents) and not any(s.endswith("the U.S.") for s in sents))
sents2 = writer.clean_sentences(
    "It's worth filling out the form because registration costs nothing at all . "
    "After two days of intense strikes , officials urged calm across the whole region.")
check("' .' and ' ,' normalized", any("nothing at all." in s for s in sents2)
      and any("strikes, officials" in s for s in sents2))
check("INFOCA caption filtered from source text",
      all("INFOCA" not in s for s in writer.clean_sentences(
          CAPTION + " Meanwhile rescue crews searched nearby villages overnight for missing residents.")))

# ---------------------------------------------------------------- normal copy still passes
GOOD1 = "Spanish authorities reported earlier that 12 people had died, but revised the death toll Friday morning."
GOOD2 = "The commission said the addictive design of Facebook and Instagram breaches EU law."
GOOD3 = "Samsung will apply a thirty dollar credit at checkout when customers preorder the new phones."
check("good bullet 1 passes", writer._well_formed_bullet(GOOD1))
check("good bullet 2 passes", writer._well_formed_bullet(GOOD2))
check("good bullet 3 passes", writer._well_formed_bullet(GOOD3))
check("good summary passes", writer.summary_quality_issues(
    "EU regulators accused Meta of failing to tackle the mental-health risks of its addictive design.") == [])
check("mid-sentence 'U.S.' bullet passes", writer._well_formed_bullet(
    "The U.S. and Iran traded their most intense strikes since the ceasefire was extended last month."))
check("'Sweden joined NATO in 2024' NOT flagged as bio", not writer.looks_like_metadata(
    "Sweden joined NATO in 2024 after decades of neutrality, reshaping Baltic security."))

# ---------------------------------------------------------------- validate_feed defense in depth
def sig(n, summary=GOOD2, takeaways=None, why=GOOD3, headline="A clean headline about policy"):
    return {"number": n, "importance": n, "lead": n == 1, "category": "WORLD", "source": "X",
            "headline": headline, "summary": summary,
            "keyTakeaways": takeaways if takeaways is not None else [GOOD1],
            "whyItMatters": why, "originalURL": "https://example.com/story/one", "readTime": 3}

BROKEN_FEED = {"date": "2026-07-10", "focus": "MIXED", "version": 1, "signals": [
    sig(1, summary=GLUE_SUMMARY, headline=GLUE_HEADLINE,
        takeaways=[CAPTION]),
    sig(2, summary=AND_OPEN, takeaways=[ROUNDUP, FRAG2]),
    sig(3),
    sig(4, why="There are no downsides to registering, so it's worth filling out the form ."),
    sig(5, takeaways=[BIO]),
]}
errs = validate_feed.editorial_errors(BROKEN_FEED)
def has(sub):
    return any(sub in e for e in errs)
check("validator: headline glue detected", has("signal 1 summary: begins with the headline"))
check("validator: caption detected", has("signal 1 keyTakeaways[0]: photo/video credit"))
check("validator: 'And,' opener detected", has("signal 2 summary: starts with a context-free connective"))
check("validator: roundup detected", has("signal 2 keyTakeaways[0]: newsletter/roundup"))
check("validator: dangling compound detected", has("signal 2 keyTakeaways[1]: dangling compound")
      or has("signal 2 keyTakeaways[1]: ends at an abbreviation"))
check("validator: space-before-punctuation detected", has("signal 4 whyItMatters: whitespace before punctuation"))
check("validator: author bio detected", has("signal 5 keyTakeaways[0]: author-bio"))

CLEAN_FEED = {"date": "2026-07-10", "focus": "MIXED", "version": 1,
              "signals": [sig(i) for i in range(1, 6)]}
check("validator: clean feed has no editorial errors", validate_feed.editorial_errors(CLEAN_FEED) == [])

print("ALL PASS" if failures == 0 else f"{failures} CHECK(S) FAILED")
sys.exit(1 if failures else 0)
