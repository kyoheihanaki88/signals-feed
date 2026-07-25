#!/usr/bin/env python3
"""
Shared editorial helpers — Signals Feed Editorial Quality v2.1.

Topic fingerprinting + duplicate-topic detection, kept in ONE place so the Ranker (selection) and
build.py (final composition gate) judge "same story" identically. Deterministic, stdlib-only — NO
LLM. The point: two signals about the same event (e.g. a U.S.–Iran peace deal) must not both make
the daily five, even when their URLs and headlines differ.
"""
import re

# Canonical tokens collapse synonyms so near-identical topics share a fingerprint.
#   ENTITY = a specific actor (country / company / government).
#   THEME  = the kind of story.
# A story's fingerprint is the set of canon tokens its title (+summary) triggers.
_SYNONYMS = [
    # ── entities ───────────────────────────────────────────────────────────────────────────────
    (re.compile(r"\b(u\.?\s?s\.?\s?a?\.?|united states|america|american|washington|white house|"
                r"trump administration|the administration|president trump|\btrump\b|\bbiden\b|"
                r"pentagon|congress)\b", re.I), "usa"),
    (re.compile(r"\b(iran|iranian|tehran)\b", re.I), "iran"),
    (re.compile(r"\b(israel|israeli|tel aviv|netanyahu)\b", re.I), "israel"),
    (re.compile(r"\b(pakistan|pakistani|islamabad)\b", re.I), "pakistan"),
    (re.compile(r"\b(china|chinese|beijing|xi jinping)\b", re.I), "china"),
    (re.compile(r"\b(russia|russian|moscow|kremlin|putin)\b", re.I), "russia"),
    (re.compile(r"\b(ukraine|ukrainian|kyiv|kiev|zelensky)\b", re.I), "ukraine"),
    (re.compile(r"\b(gaza|hamas|palestin\w+)\b", re.I), "gaza"),
    (re.compile(r"\b(european union|\beu\b|brussels)\b", re.I), "eu"),
    (re.compile(r"\b(amazon|\baws\b)\b", re.I), "amazon"),
    (re.compile(r"\b(anthropic|claude)\b", re.I), "anthropic"),
    (re.compile(r"\b(openai|chatgpt|sam altman)\b", re.I), "openai"),
    (re.compile(r"\b(google|alphabet|deepmind|gemini)\b", re.I), "google"),
    (re.compile(r"\b(microsoft|azure)\b", re.I), "microsoft"),
    (re.compile(r"\b(meta|facebook|instagram)\b", re.I), "meta"),
    (re.compile(r"\b(apple|iphone)\b", re.I), "apple"),
    (re.compile(r"\b(nvidia)\b", re.I), "nvidia"),
    # ── themes ─────────────────────────────────────────────────────────────────────────────────
    (re.compile(r"\b(peace|ceasefire|truce|talks?|negotiat\w+|\bdeal\b|agreement|accord|diplomacy|"
                r"summit)\b", re.I), "peace_talks"),
    (re.compile(r"\b(tariff\w*|trade war|trade deal|import\w*|export\w*|supply chain)\b", re.I), "trade"),
    (re.compile(r"\b(antitrust|monopoly|competition probe)\b", re.I), "antitrust"),
    (re.compile(r"\b(\bai\b|a\.i\.|artificial intelligence)\b", re.I), "ai"),
    (re.compile(r"\b(climate|emissions|warming|carbon|wildfire|flood\w*|drought)\b", re.I), "climate"),
    (re.compile(r"\b(election|\bvote\b|ballot|primary|campaign)\b", re.I), "election"),
    (re.compile(r"\b(\bwar\b|military|missile|airstrike|troops|nuclear)\b", re.I), "military"),
    (re.compile(r"\b(inflation|interest rate|recession|\bgdp\b|markets?|stocks?|\bbond\b)\b", re.I), "markets"),
]

ENTITY_CANON = {"usa", "iran", "israel", "pakistan", "china", "russia", "ukraine", "gaza", "eu",
                "amazon", "anthropic", "openai", "google", "microsoft", "meta", "apple", "nvidia"}
THEME_CANON = {"peace_talks", "trade", "antitrust", "ai", "climate", "election", "military", "markets"}


def topic_fingerprint(*texts):
    """Canonical topic tokens for one story (pass title, optionally + summary). A frozenset so the
    caller can do set algebra; empty when nothing canonical is recognized."""
    blob = " ".join(t or "" for t in texts)
    return frozenset(canon for rx, canon in _SYNONYMS if rx.search(blob))


def _jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def topics_overlap(a, b):
    """True when two fingerprints describe the same / near-identical story (duplicate coverage).
    Same two actors → same story; one shared actor + same theme + high overlap → same story;
    otherwise a high Jaccard. Tuned so US/Iran peace-deal variants collide but distinct stories
    (e.g. an Amazon/Anthropic policy story vs an Iran story) do not."""
    if not a or not b:
        return False
    shared = a & b
    if len(shared & ENTITY_CANON) >= 2:
        return True
    if (shared & ENTITY_CANON) and (shared & THEME_CANON) and _jaccard(a, b) >= 0.5:
        return True
    return _jaccard(a, b) >= 0.6


def first_duplicate_pair(fingerprints):
    """Given an ordered list of (label, fingerprint), return the first (i, j) pair that overlaps,
    or None. Used by the final composition gate to report which two signals collide."""
    for i in range(len(fingerprints)):
        for j in range(i + 1, len(fingerprints)):
            if topics_overlap(fingerprints[i][1], fingerprints[j][1]):
                return fingerprints[i][0], fingerprints[j][0]
    return None


# ══════════════════════════════════════════════════════════════════════════════════════
# Morning Mix story metadata (2026-07-24). Deterministic, stdlib-only, keyword-based.
# ALL fields are OPTIONAL composition hints: a missing/None value means "neutral" and
# must NEVER invalidate a candidate. tone/region/event_family feed the Ranker's variety
# scoring only — they make NO factual or safety decisions about a story.
# ══════════════════════════════════════════════════════════════════════════════════════

# country/region from STORY TEXT only — never inferred from the publisher's home country
# (a BBC story about Nigeria is an Africa story, not a Europe story).
_REGIONS = {
    "North America": {"United States": r"\b(u\.?s\.?a?\.?|united states|america\b|american\b|washington|white house|pentagon|congress)\b",
                      "Canada": r"\b(canada|canadian|ottawa|toronto)\b",
                      "Mexico": r"\b(mexico|mexican|mexico city)\b"},
    "Latin America": {"Brazil": r"\b(brazil|brazilian|brasilia|sao paulo)\b",
                      "Argentina": r"\b(argentina|argentine|buenos aires)\b",
                      "Colombia": r"\b(colombia|colombian|bogota)\b",
                      "Venezuela": r"\b(venezuela|venezuelan|caracas)\b",
                      "Chile": r"\b(chile|chilean|santiago)\b"},
    "Europe": {"United Kingdom": r"\b(\buk\b|britain|british|england|scotland|wales|london|downing street)\b",
               "France": r"\b(france|french|paris)\b",
               "Germany": r"\b(germany|german|berlin)\b",
               "Italy": r"\b(italy|italian|rome)\b",
               "Spain": r"\b(spain|spanish|madrid)\b",
               "Ukraine": r"\b(ukraine|ukrainian|kyiv|kiev|zelensky)\b",
               "Russia": r"\b(russia|russian|moscow|kremlin|putin)\b",
               "Poland": r"\b(poland|polish|warsaw)\b"},
    "Middle East": {"Israel": r"\b(israel|israeli|tel aviv|jerusalem|netanyahu)\b",
                    "Palestinian territories": r"\b(gaza|hamas|palestin\w+|west bank)\b",
                    "Iran": r"\b(iran|iranian|tehran)\b",
                    "Saudi Arabia": r"\b(saudi|riyadh)\b",
                    "Syria": r"\b(syria|syrian|damascus)\b",
                    "Lebanon": r"\b(lebanon|lebanese|beirut|hezbollah)\b",
                    "Yemen": r"\b(yemen|yemeni|houthi)\b",
                    "Iraq": r"\b(iraq|iraqi|baghdad)\b",
                    "Turkey": r"\b(turkey|turkish|ankara|istanbul|erdogan)\b"},
    "Africa": {"Nigeria": r"\b(nigeria|nigerian|lagos|abuja)\b",
               "South Africa": r"\b(south africa|johannesburg|pretoria)\b",
               "Egypt": r"\b(egypt|egyptian|cairo)\b",
               "Kenya": r"\b(kenya|kenyan|nairobi)\b",
               "Ethiopia": r"\b(ethiopia|ethiopian|addis ababa)\b",
               "Sudan": r"\b(sudan|sudanese|khartoum)\b",
               "DR Congo": r"\b(congo|congolese|kinshasa)\b"},
    "Asia-Pacific": {"China": r"\b(china|chinese|beijing|shanghai|xi jinping)\b",
                     "Japan": r"\b(japan|japanese|tokyo)\b",
                     "India": r"\b(india|indian|delhi|mumbai|modi)\b",
                     "Pakistan": r"\b(pakistan|pakistani|islamabad|karachi)\b",
                     "South Korea": r"\b(south korea|korean\b|seoul)\b",
                     "North Korea": r"\b(north korea|pyongyang)\b",
                     "Australia": r"\b(australia|australian|canberra|sydney)\b",
                     "Indonesia": r"\b(indonesia|indonesian|jakarta)\b",
                     "Philippines": r"\b(philippine\w*|filipino|manila)\b",
                     "Taiwan": r"\b(taiwan|taiwanese|taipei)\b"},
}
_COUNTRY_RX = [(re.compile(rx, re.I), country, region)
               for region, countries in _REGIONS.items()
               for country, rx in countries.items()]

EVENT_FAMILIES = ("conflict", "protest", "election", "political_scandal", "disaster",
                  "policy", "earnings", "markets", "consumer_launch",
                  "science_discovery", "health_breakthrough", "culture", "other")

# checked in order; first match wins (consumer_launch is decided separately, first)
_EVENT_RX = [
    ("conflict", r"\b(\bwar\b|airstrike\w*|missile\w*|shell\w+|invasion|offensiv\w+|troops|"
                 r"ceasefire|hostage\w*|militant\w*|insurgen\w+|drone strike\w*|artillery|frontline)\b"),
    ("protest", r"\b(protest\w*|demonstrat\w+|riot\w*|unrest|marche[sd]|rally|rallies|strike action|walkout)\b"),
    ("election", r"\b(election\w*|ballot\w*|\bvote\b|voters?|polls? open|primary|runoff|referendum)\b"),
    ("political_scandal", r"\b(scandal|impeach\w+|corruption|bribery|indict\w+|resign\w+ over|cover-?up)\b"),
    ("disaster", r"\b(earthquake|hurricane|typhoon|cyclone|wildfire\w*|flood\w*|landslide|tsunami|"
                 r"eruption|heatwave|storm\w* kill|death toll|derail\w*|plane crash|collapse\w* kill)\b"),
    ("earnings", r"\b(earnings|quarterly (results|profit|revenue)|q[1-4] (results|profit|revenue)|"
                 r"profit (rise|fall|jump|drop)\w*|revenue (beat|miss)\w*|forecast\w* (cut|raise)\w*)\b"),
    ("markets", r"\b(stocks?\b|shares?\b|markets? (rally|slide|fall|rise)|inflation|interest rates?|"
                 r"central bank|\bfed\b|bond yields?|\bgdp\b|recession)\b"),
    ("science_discovery", r"\b(discover\w+|breakthrough|telescope|astronomer\w*|fossil\w*|"
                          r"spacecraft|\bnasa\b|space mission|quantum|physicist\w*|new species|researchers? (found|find))\b"),
    ("health_breakthrough", r"\b(vaccine\w*|clinical trial\w*|cancer treatment|new drug|therapy shows|"
                            r"cure\w*|transplant\w*|alzheimer\w*|obesity drug\w*)\b"),
    ("policy", r"\b(\bbill\b|legislation|regulat\w+|\bban\b|sanction\w*|tariff\w*|policy|lawmaker\w*|"
               r"court rul\w+|supreme court|minister\w* announc\w+|government plan\w*)\b"),
    ("culture", r"\b(film\b|movie\w*|box office|album\w*|concert\w*|festival\w*|museum\w*|exhibition\w*|"
                r"\bnovel\b|booker|oscars?|grammy\w*|premiere\w*|theatre|theater\b)\b"),
]
_EVENT_RX = [(f, re.compile(rx, re.I)) for f, rx in _EVENT_RX]

# ── consumer launch (deterministic, brand-neutral) ─────────────────────────────────────
# Qualifies ONLY when ALL hold: launch verb + recognizable consumer product/platform +
# credible source + fresh. Brand names are NOT scored and get no preference — a launch by
# an unknown maker of a mainstream product class qualifies exactly like a Samsung one.
_LAUNCH_VERB_RX = re.compile(r"\b(announc\w+|launch\w+|unveil\w+|introduc\w+|releas\w+|debut\w+)\b", re.I)
_CONSUMER_PRODUCT_RX = re.compile(
    r"\b(smart ?phones?|phones?|handsets?|foldables?|tablets?|laptops?|notebooks?|"
    r"consoles?|headsets?|smart ?watch\w*|smart ?glasses|earbuds?|e-?readers?|cameras?|"
    r"televisions?|\btvs?\b|operating system|android \d+|ios \d+|windows \d+|macos \w+|"
    r"browsers?|apps?\b|platforms?|chatbots?|assistants?)\b", re.I)
_LAUNCH_EXCLUDE_RX = re.compile(
    r"\b(rumou?r\w*|leak\w*|reportedly|expected to|may\b|could\b|might\b|tipped to|"
    r"deals?\b|discount\w*|% off|price (cut|drop)|flash sale|clearance|best \w+ to buy|buying guide|"
    r"benchmark\w*|geekbench|antutu|earnings|revenue|profit|stocks?\b|shares?\b|"
    r"affiliate|preview\b|hands-?on)\b", re.I)
_ACCESSORY_RX = re.compile(
    r"\b(cases?\b|covers?\b|cables?\b|chargers?\b|adapters?\b|dongles?\b|straps?\b|"
    r"bands?\b|stands?\b|mounts?\b|styl(us|i)\b|screen protectors?|keychains?|skins?\b)\b", re.I)

_TONES = ("negative_conflict", "negative_crisis", "neutral", "forward_looking", "discovery")
_CRISIS_RX = re.compile(r"\b(kill\w*|death\w*|dead\b|dies?\b|died\b|casualt\w*|famine|outbreak|"
                        r"epidemic|crisis|collaps\w*|missing\b|injur\w*|victims?)\b", re.I)
_FORWARD_RX = re.compile(r"\b(plans? to|will (open|build|invest|expand|create)|to invest|pledg\w+|"
                         r"unveil\w+ plan|aims? to|set to (open|build|expand))\b", re.I)


def _is_fresh(published_at, now=None, max_hours=72):
    """Freshness for the launch gate. Unknown timestamps are treated as fresh — missing
    metadata is neutral and must never invalidate a candidate."""
    if not published_at:
        return True
    import datetime as _dt
    try:
        ts = _dt.datetime.fromisoformat(published_at)
        ref = now or _dt.datetime.now(_dt.timezone.utc)
        return (ref - ts) <= _dt.timedelta(hours=max_hours)
    except Exception:
        return True


def is_consumer_launch(title, snippet="", reliability=None, published_at=None, now=None):
    """True only for a REAL, current, credibly-sourced consumer product/platform launch.
    Rumors, leaks, hedged reports, accessories, deals/discounts, buying guides,
    benchmarks, earnings/stock stories and affiliate content never qualify.
    Brand-neutral by construction: no brand list exists here."""
    blob = f"{title or ''} {snippet or ''}"
    title_blob = title or ""
    if _LAUNCH_EXCLUDE_RX.search(blob):
        return False
    if _ACCESSORY_RX.search(title_blob):
        return False              # accessory refreshes are not platform-level launches
    # The launch VERB must be the story's headline claim (title), not a stray "announced"
    # buried in the snippet — otherwise acquisitions and opinion pieces that merely
    # mention a past launch would qualify.
    if not _LAUNCH_VERB_RX.search(title_blob):
        return False
    if not _CONSUMER_PRODUCT_RX.search(blob):
        return False
    if reliability == "low":
        return False              # credible source required
    return _is_fresh(published_at, now)


def story_metadata(title, snippet="", reliability=None, published_at=None, now=None):
    """Deterministic Morning Mix metadata for one candidate. Every field is an optional
    composition hint; None/False means neutral. Never raises on odd input."""
    blob = f"{title or ''} {snippet or ''}"

    country = region = None
    for rx, c, r in _COUNTRY_RX:
        if rx.search(blob):
            country, region = c, r
            break                 # first (deterministic) match wins

    launch = is_consumer_launch(title, snippet, reliability, published_at, now)
    event_family = "other"
    if launch:
        event_family = "consumer_launch"
    else:
        for fam, rx in _EVENT_RX:
            if rx.search(blob):
                # "her body was discovered" is a death story, not a science discovery:
                # generic discovery verbs must not classify a crisis story as science.
                if fam == "science_discovery" and _CRISIS_RX.search(blob):
                    continue
                event_family = fam
                break

    if event_family == "conflict":
        tone = "negative_conflict"
    elif event_family in ("disaster",) or (_CRISIS_RX.search(blob) and event_family not in
                                           ("science_discovery", "health_breakthrough", "consumer_launch")):
        tone = "negative_crisis"
    elif event_family in ("science_discovery", "health_breakthrough"):
        tone = "discovery"
    elif event_family == "consumer_launch" or _FORWARD_RX.search(blob):
        tone = "forward_looking"
    else:
        tone = "neutral"

    discovery_value = event_family in ("science_discovery", "health_breakthrough") or tone == "discovery"

    return {
        "country": country,
        "region": region,
        "event_family": event_family,
        "tone": tone,
        "consumer_launch": launch,
        "discovery_value": discovery_value,
    }


# ══════════════════════════════════════════════════════════════════════════════════════
# Underlying-story identity (2026-07-25 regression fix)
#
# WHY: `topic_fingerprint` only recognizes a fixed canon vocabulary. A story it does not
# recognize fingerprints to the EMPTY set, and `topics_overlap` returns False whenever
# either side is empty — so two articles about the SAME product launch passed every gate:
#
#   "Samsung Galaxy Unpacked 2026: The 6 biggest announcements"   (roundup)
#   "Samsung's wider Z Fold 8 feels just right"                   (hands-on)
#
# Both fingerprinted to {} , and their title-token Jaccard was 0.09 (< the 0.30 clustering
# threshold), so scout clustering ALSO kept them apart. One event took two TECH slots.
#
# This adds a second, independent identity for CONSUMER-PRODUCT news: brand + product
# family + launch event. It deliberately does NOT block everything from one company —
# a phone launch and a chip-factory or earnings story stay independent, because they are
# different event families and only one of them is a product story.
# ══════════════════════════════════════════════════════════════════════════════════════

# Consumer-tech brands we can normalize. Unknown brands simply yield no identity (the
# existing fingerprint gate still applies) — this list is safe to extend.
_BRAND_RX = [
    (re.compile(r"\bsamsung\b", re.I), "samsung"),
    (re.compile(r"\b(apple|iphone|ipad|macbook|airpods|apple watch|vision pro)\b", re.I), "apple"),
    (re.compile(r"\b(google|pixel|android)\b", re.I), "google"),
    (re.compile(r"\bmicrosoft|xbox|surface\b", re.I), "microsoft"),
    (re.compile(r"\b(meta|quest|oculus)\b", re.I), "meta"),
    (re.compile(r"\b(sony|playstation|\bps5\b)\b", re.I), "sony"),
    (re.compile(r"\bnintendo|switch 2\b", re.I), "nintendo"),
    (re.compile(r"\b(openai|chatgpt)\b", re.I), "openai"),
    (re.compile(r"\banthropic|claude\b", re.I), "anthropic"),
    (re.compile(r"\bnothing phone\b", re.I), "nothing"),
    (re.compile(r"\bmotorola|moto g\b", re.I), "motorola"),
    (re.compile(r"\bxiaomi|redmi\b", re.I), "xiaomi"),
    (re.compile(r"\boneplus\b", re.I), "oneplus"),
    (re.compile(r"\btesla\b", re.I), "tesla"),
]

# Product families, MOST SPECIFIC FIRST. Values are space-separated hierarchies:
# "galaxy z fold" is inside "galaxy", so a broad roundup and a specific model match by
# prefix. Two DIFFERENT families of one brand (iphone vs mac) are materially distinct.
_FAMILY_RX = [
    (re.compile(r"\b(galaxy\s+)?z\s*fold\b", re.I), "galaxy z fold"),
    (re.compile(r"\b(galaxy\s+)?z\s*flip\b", re.I), "galaxy z flip"),
    (re.compile(r"\bgalaxy\s+s\d+\b", re.I), "galaxy s"),
    (re.compile(r"\bgalaxy\s+watch\b", re.I), "galaxy watch"),
    (re.compile(r"\bgalaxy\s+buds\b", re.I), "galaxy buds"),
    (re.compile(r"\bgalaxy\b", re.I), "galaxy"),
    (re.compile(r"\biphone\b", re.I), "iphone"),
    (re.compile(r"\bapple\s+watch\b", re.I), "apple watch"),
    (re.compile(r"\bairpods\b", re.I), "airpods"),
    (re.compile(r"\bvision\s+pro\b", re.I), "vision pro"),
    (re.compile(r"\bmac(book)?\b", re.I), "mac"),
    (re.compile(r"\bipad\b", re.I), "ipad"),
    (re.compile(r"\bpixel\s+watch\b", re.I), "pixel watch"),
    (re.compile(r"\bpixel\b", re.I), "pixel"),
    (re.compile(r"\bplaystation|\bps5\b", re.I), "playstation"),
    (re.compile(r"\bxbox\b", re.I), "xbox"),
    (re.compile(r"\bswitch\s*2\b", re.I), "switch"),
    (re.compile(r"\bquest\s*\d?\b", re.I), "quest"),
]

# Named launch events — the strongest same-story signal when two articles share one.
_LAUNCH_EVENT_RX = [
    (re.compile(r"\bunpacked\b", re.I), "unpacked"),
    (re.compile(r"\bwwdc\b", re.I), "wwdc"),
    (re.compile(r"\bmade by google\b", re.I), "made by google"),
    (re.compile(r"\bgalaxy ai\b", re.I), "galaxy ai"),
    (re.compile(r"\b(ces|mwc|ifa)\b", re.I), "trade show"),
    (re.compile(r"\bkeynote\b", re.I), "keynote"),
]

# Event families that are PRODUCT news (a launch, a hands-on, a roundup of one event).
# Anything else about the same brand — earnings, markets, policy, a factory, a lawsuit —
# is materially distinct news value and must stay independently selectable.
_PRODUCT_EVENT_FAMILIES = {"consumer_launch", "other"}

# Article formats that cover a whole event rather than one device. A roundup and a
# hands-on from the same brand+event are the same underlying story.
_ROUNDUP_RX = re.compile(
    r"\b(everything|all the news|announcements?|roundup|round-up|biggest|recap|"
    r"what was announced|highlights|liveblog|as it happened|how to watch)\b", re.I)


def story_identity(title, snippet="", metadata=None):
    """Deterministic identity for CONSUMER-PRODUCT news, or None when the story is not
    recognizably about a consumer brand/product. Never raises; unknown → None (the
    existing fingerprint gate still applies, so nothing is weakened)."""
    blob = f"{title or ''} {snippet or ''}"
    brand = next((b for rx, b in _BRAND_RX if rx.search(blob)), None)
    if not brand:
        return None
    meta = metadata or story_metadata(title, snippet)
    family = next((f for rx, f in _FAMILY_RX if rx.search(blob)), None)
    event = next((e for rx, e in _LAUNCH_EVENT_RX if rx.search(blob)), None)
    # EVERY product line named anywhere in the story. For a roundup this is its actual
    # coverage ("…from the iPhone 18 to the new MacBook Pro" covers iphone AND mac), which
    # is what lets a roundup absorb a specific story WITHOUT absorbing unrelated lines.
    #
    # Specificity pruning is essential: "Galaxy Watch 8" matches BOTH "galaxy watch" and the
    # broad "galaxy", and keeping the broad parent would make every Samsung roundup appear to
    # cover every Galaxy line (a Watch recap would swallow a Z Fold story). A parent is
    # dropped whenever a more specific descendant of it was also matched.
    matched = {f for rx, f in _FAMILY_RX if rx.search(blob)}
    covered = {f for f in matched
               if not any(g != f and g.startswith(f + " ") for g in matched)}
    return {
        "brand": brand,
        "product_family": family,
        "covered_families": covered,
        "event": event,
        "event_family": meta.get("event_family"),
        "is_roundup": bool(_ROUNDUP_RX.search(title or "")),
        "is_product_story": (meta.get("consumer_launch")
                             or bool(family) or bool(event)) and
                            meta.get("event_family") in _PRODUCT_EVENT_FAMILIES,
    }


def _family_related(a, b):
    """True when two product families describe the same line — equal, or one is a
    hierarchical prefix of the other ("galaxy" covers "galaxy z fold")."""
    if a == b:
        return True
    if not a or not b:
        return False
    return a.startswith(b + " ") or b.startswith(a + " ")


def same_underlying_story(id_a, id_b):
    """(is_duplicate, reason, matched_rule) for two `story_identity` results.

    RULE ORDER (first match wins). Ordering matters: the specific evidence rules run
    BEFORE any generic roundup containment, so a roundup headline can never merge two
    materially different product lines of one brand.

      1. no-identity / different-brand      → not duplicate
      2. not-product-story (either side)    → not duplicate  (launch vs earnings stays split)
      3. same-launch-event                  → DUPLICATE      (strongest same-event evidence)
      4. same-product-family                → DUPLICATE      (equal or hierarchical: galaxy ⊃ galaxy z fold)
      5. roundup-covers-family              → DUPLICATE      (the roundup NAMES the other line)
      6. distinct-product-families          → not duplicate  (iphone vs mac — before any generic fallback)
      7. roundup-contains-broad             → DUPLICATE      (roundup + a side with no identified line)
      8. brand-product-news-no-line         → DUPLICATE      (neither side names a line)
    """
    if not id_a or not id_b:
        return False, "", "no-identity"
    if id_a["brand"] != id_b["brand"]:
        return False, "", "different-brand"
    if not (id_a["is_product_story"] and id_b["is_product_story"]):
        return False, "", "not-product-story"

    brand = id_a["brand"]
    fa, fb = id_a["product_family"], id_b["product_family"]

    if id_a["event"] and id_a["event"] == id_b["event"]:
        return True, f"same brand ({brand}) + same launch event ({id_a['event']})", \
               "same-launch-event"

    if (fa or fb) and _family_related(fa, fb):
        return True, f"same brand ({brand}) + same product family ({fa or fb})", \
               "same-product-family"

    # A roundup absorbs a specific story ONLY when it actually names that product line.
    # "Apple's iPhone 18 event: the 5 biggest announcements" does not cover a MacBook story.
    for round_id, other_id, other_fam in ((id_a, id_b, fb), (id_b, id_a, fa)):
        if round_id["is_roundup"] and other_fam and \
                any(_family_related(other_fam, f) for f in round_id["covered_families"]):
            return True, (f"same brand ({brand}) + roundup covers that product line "
                          f"({other_fam})"), "roundup-covers-family"

    if fa and fb:
        # Both lines identified and materially unrelated — genuinely distinct news value.
        return False, "", "distinct-product-families"

    if id_a["is_roundup"] or id_b["is_roundup"]:
        return True, (f"same brand ({brand}) + event roundup and a story with no distinct "
                      f"product line"), "roundup-contains-broad"

    if not fa and not fb:
        return True, f"same brand ({brand}) product news with no distinguishing product line", \
               "brand-product-news-no-line"

    return False, "", "distinct-product-families"


def duplicate_story(a_title, a_snippet, b_title, b_snippet,
                    a_meta=None, b_meta=None):
    """Convenience wrapper: (is_duplicate, reason, matched_rule) for two raw stories."""
    return same_underlying_story(story_identity(a_title, a_snippet, a_meta),
                                 story_identity(b_title, b_snippet, b_meta))
