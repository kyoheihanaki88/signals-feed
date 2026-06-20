#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import writer

failures = 0
def check(name, cond):
    global failures
    print(("✓" if cond else "✗"), name)
    if not cond:
        failures += 1

bio1 = "Stevie started out at Laptop Mag writing news and reviews on hardware, gaming, and AI."
bio2 = "An AI beat reporter for more than five years, her work has also appeared in CNBC, MIT Technology Review, Wired UK, and other outlets."
cap = "Italy's Prime Minister Giorgia Meloni arrives to a round table meeting at the EU summit in Brussels on Thursday."
stitched = 'A rift widens between Trump and Italy’s Giorgia Meloni "Italy and I do not beg," Meloni said.'

check("bio1 metadata", writer.looks_like_metadata(bio1))
check("bio2 metadata", writer.looks_like_metadata(bio2))
check("caption metadata", writer.looks_like_metadata(cap))
check("stitched quote metadata", writer.looks_like_metadata(stitched))

check("bio1 summary blocked", writer.summary_quality_issues(bio1) != [])
check("bio2 why blocked", writer.why_quality_issues(bio2, "x") != [])
check("caption takeaway blocked", not writer._well_formed_bullet(cap))

clean_why = "Rising RAM prices could push up the cost of budget phones launching later this year."
clean_sum = "OpenAI named a new head of enterprise to expand its corporate software push."
check("clean why passes", writer.why_quality_issues(clean_why, "x") == [])
check("clean summary passes", writer.summary_quality_issues(clean_sum) == [])
check("based in Berlin not flagged", not writer.looks_like_metadata("A startup based in Berlin raised forty million dollars."))
check("contributed to not flagged", not writer.looks_like_metadata("Several factors contributed to the sharp rise in energy prices."))
check("normal quote not flagged", not writer.looks_like_metadata('The minister said, "We will not accept these terms," before leaving.'))

print("ALL PASS" if failures == 0 else f"{failures} CHECK(S) FAILED")
sys.exit(1 if failures else 0)
