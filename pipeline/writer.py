#!/usr/bin/env python3
"""
Signals — Writer v1 (safest first increment): extractive drafting.

Turns an approved selection.json into DRAFT Signals-style copy, grounded ONLY in
real source text. v1 is deliberately *extractive*: it never paraphrases or infers —
it can only copy text that exists in the source, so it cannot invent a fact. (The
LLM paraphrase layer is a later increment that swaps the drafter but keeps this
plumbing, provenance, and validation.)

Source text, in priority order, per selected item:
  1. cached full article  (pipeline/cache/articles/<id>.txt)         -> full_article
  2. live fetch (urllib)   (runner only; --no-fetch disables)         -> full_article
  3. RSS snippet from selection.json                                  -> rss_snippet
  4. nothing                                                          -> none (needs_review)

Hard boundaries: writes ONLY drafts.json. Never latest.json. Never publishes.
Never changes the source or URL. Subcommand `validate` runs validate_drafts.

Usage:
  python3 writer.py draft    [--selection selection.json] [--articles cache/articles]
                             [--no-fetch] [--simulate-unavailable id1,id2] [--out drafts.json]
  python3 writer.py validate [--selection selection.json] [--drafts drafts.json]
"""
import sys, os, re, json, html, argparse, datetime
from urllib.parse import urlsplit
import urllib.request

HERE = os.path.dirname(__file__)
DEF_SEL = os.path.join(HERE, "selection.json")
DEF_ART = os.path.join(HERE, "cache", "articles")
DEF_OUT = os.path.join(HERE, "drafts.json")

SENT_RE = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"[A-Za-z0-9'$%.-]+")
NUM_RE = re.compile(r"\d")
PROPER_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")

# Website boilerplate to drop before extraction (BBC / NPR / The Verge etc.).
BOILERPLATE = (
    "skip to content", "skip to main", "accessibility", "hide caption", "toggle caption",
    "image caption", "media caption", "getty images", "read more", "sign up", "sign in",
    "log in", "subscribe", "newsletter", "follow us", "share this", "share on",
    "advertisement", "cookie", "privacy policy", "terms of", "all rights reserved",
    "copyright", "back to top", "more on this story", "related topics", "watch:",
    "listen:", "play video", "click here", "download the app", "supported browser",
    "enable javascript", "view comments", "most read", "in pictures", "tap here",
    "bbc is not responsible", "external sites", "this video can not be played",
    "logo", "homepage", "weekend editor", "vox media", "comments", "more from the verge",
)
BYLINE_RE = re.compile(r"^(by|source|photo|photograph|credit|reporting by|updated|published|"
                       r"editor's note|getty|reuters|associated press)\b", re.I)
# Words that signal stakes/consequence — used to pick a cautious whyItMatters from the article.
STAKES_RE = re.compile(r"\b(could|may|might|risk|warn(?:s|ed)?|threat|expected|means?|impact|"
                       r"because|lead(?:s)? to|consequence|raising|fears?|concerns?|"
                       r"significant|major|first time|unprecedented|escalat\w*|ceasefire|"
                       r"agreement|deal|sanction\w*)\b", re.I)

# Affiliate/disclosure, author-or-editor bio, photo-credit endings, photo-caption scenes.
AFFILIATE_RE = re.compile(r"(may earn (an? )?commission|if you (buy|purchase) something|"
                          r"vox media|affiliate link|our ethics (policy|statement))", re.I)
AUTHOR_BIO_RE = re.compile(r"\bis\s+(a|an|the)\b[^.]{0,70}\b(editor|reporter|writer|journalist|"
                           r"correspondent|contributor|columnist|anchor|host)\b", re.I)
CREDIT_END_RE = re.compile(r"(getty images|/\s*ap\b|/\s*afp\b|/\s*npr\b|/\s*getty|reuters|"
                           r"associated press|\bafp\b|bloomberg|pool|handout|via getty)"
                           r"\s*[.\)\"'”]*\s*$", re.I)
CAPTION_SCENE_RE = re.compile(r"^(a|an|the)\s+[\w'’]+(?:\s+[\w'’,]+){0,6}?\s+"
                              r"(holds?|carries|carry|waves?|stands?|sits?|walks?|marche?s?|"
                              r"gathers?|wears?|raises?|displays?|protests?|cheers?|poses?|"
                              r"attends?|hugs?|embraces?|celebrat\w+|reacts?)\b", re.I)

# Broader photo-caption / credit detection (issue: captions becoming the summary). Captions usually
# carry a file/credit marker, a "pictured/seen here" cue, or a subject doing a visual action —
# regardless of whether they start with a/an/the.
CAPTION_LEAD_RE = re.compile(
    r"^\s*(file ?photo|file ?-|pictured|photo|photograph|image|illustration|video|graphic|caption)\b"
    r"\s*[:\-—]", re.I)
CAPTION_CUE_RE = re.compile(
    r"\b(pictured (above|below|here|right|left)|seen (here|above|below)|from left( to right)?|"
    r"left to right|is seen|are seen|looks on|gestures|holds up|poses for|walk(s)? past|"
    r"during a (rally|protest|ceremony|match|game|parade|news conference|press conference))\b", re.I)
CAPTION_VERB_RE = re.compile(
    r"^\s*[\w'’.,\- ]{0,55}?\b(holds?|carries|carry|waves?|stands?|sits?|walks?|marche?s?|gathers?|"
    r"wears?|raises?|displays?|protests?|cheers?|poses?|attends?|hugs?|embraces?|celebrat\w+|"
    r"reacts?|gestures?|looks on)\b", re.I)
# RSS/snippet truncation: a sentence ending in an ellipsis is CUT OFF (issue: whyItMatters cut off).
ELLIPSIS_END_RE = re.compile(r"(\.\.\.|…|…)\s*$")


def looks_like_caption(s):
    """A photo caption / credit line, not article prose — must never become summary/takeaway/why."""
    return bool(CAPTION_LEAD_RE.search(s) or CAPTION_CUE_RE.search(s)
                or CAPTION_SCENE_RE.match(s) or CAPTION_VERB_RE.match(s))


# v2.5 — author/reporter bios + publication metadata that scrapers leave in the body.
METADATA_BIO_RE = re.compile(
    r"(\bwork has (also )?appeared\b|"
    r"\bstarted out at\b|"
    r"\b(beat reporter|staff (writer|reporter)|senior (writer|reporter|editor)|"
    r"freelance (writer|journalist)|contributing (writer|editor|reporter))\b|"
    r"\b(reporter|writer|editor|journalist|correspondent|columnist|contributor)s?\s+"
    r"for\s+(more than|over|nearly|almost)\b|"
    r"\bcovers\s+[\w\s,'-]{1,40}\s+for\s+[A-Z]|"
    r"\bwrites about\s+[\w\s,'-]{1,40}\s+for\s+[A-Z]|"
    r"\b(is|was)\s+(a|an)\s+[\w\s,'’-]{0,40}?\b"
    r"(reporter|writer|editor|journalist|correspondent|columnist|contributor|critic|blogger|anchor|host)\s+"
    r"(for|at|covering|based|who|with)\b|"
    r"\bfollow (her|him|them|us) on\b|"
    r"\byou can (reach|email|follow) (her|him|them)\b)", re.I)

METADATA_CAPTION_RE = re.compile(
    r"(\b(photo|image|picture) (by|credit)\b|\bgetty images\b|\bap photo\b|\bvia getty\b|"
    r"\breuters\s*/|/\s*afp\b|\bpool photo\b|"
    r"\barrives (to|for|at)\b|\bspeaks (during|at|to)\b|\bstands? near\b|\bsits? (in|near|beside)\b|"
    r"\bwalks? (past|through|along)\b|\battends? (a|an|the)\b|\bis seen\b|\bare seen\b|\bpictured\b|"
    r"\bduring a (round table|round-table|meeting|summit|session|hearing|signing))\b", re.I)

STITCHED_QUOTE_RE = re.compile(
    r"[\w’'][\s]+[\"“][^\"”]{1,180}[,.\!?][\"”]\s+\w+\s+(said|says|told|added|noted|argued|wrote)\b", re.I)

def looks_like_metadata(s):
    """Author/reporter bio, publication metadata, photo credit/caption, or stitched quote."""
    return bool(METADATA_BIO_RE.search(s) or METADATA_CAPTION_RE.search(s)
                or STITCHED_QUOTE_RE.search(s))

STOPWORDS = {"the","a","an","and","or","but","of","to","in","on","for","with","from","at","by",
             "as","is","are","was","were","be","been","that","this","it","its","his","her","their",
             "they","we","you","has","have","had","will","would","could","said","after","over",
             "into","out","new","news","world","daily","times","post","media","online","about"}


def keywords(text):
    """Significant lowercase tokens (≥4 chars, non-stopword) — for headline-relevance scoring."""
    return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower()) if w not in STOPWORDS}


def brand_key(source_name):
    """Most distinctive word of a source name (e.g. 'verge','npr','bbc') for nav-repetition checks."""
    toks = [t for t in re.findall(r"[a-z]{3,}", (source_name or "").lower()) if t not in STOPWORDS]
    return max(toks, key=len) if toks else ""


def clean_sentences(text, source_name=""):
    """Article-body sentences only. Decodes HTML entities (&nbsp;, &#x27;, …) and drops nav, site
    title/logo, image captions, photo credits, affiliate disclosures, author/editor bios, bylines,
    and newsletter/footer boilerplate. NEVER invents — only filters what's already in the source."""
    text = html.unescape(text or "").replace("\xa0", " ")
    bkey = brand_key(source_name)
    out, seen = [], set()
    for raw in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = re.sub(r"\s+", " ", raw).strip()
        if not s:
            continue
        low = s.lower()
        if any(b in low for b in BOILERPLATE):
            continue
        if BYLINE_RE.match(s) or AUTHOR_BIO_RE.search(s) or AFFILIATE_RE.search(s):
            continue
        if CREDIT_END_RE.search(s) or looks_like_caption(s):        # photo credit / caption / scene
            continue
        if looks_like_metadata(s):                                  # author bio / metadata / stitched quote
            continue
        if ELLIPSIS_END_RE.search(s):                               # snippet cut off mid-thought
            continue
        if bkey and low.count(bkey) >= 2:                          # site-title/logo nav repetition
            continue
        wc = len(s.split())
        if wc < 6 or wc > 60:                                      # UI labels / overly long blobs
            continue
        if not re.search(r"[.!?][\"')”]?$", s):                # must end like a real sentence
            continue
        if not re.search(r"[a-z]", s):                             # drop ALL-CAPS nav labels
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ----------------------------------------------------------------- helpers
def load_selection(path):
    if not os.path.exists(path):
        sys.exit(f"ERROR: selection not found: {path} (run selection.py build first).")
    data = json.load(open(path))
    sig = data.get("signals", [])
    if not sig:
        sys.exit("ERROR: selection.json has no signals.")
    return data, sig


def sentences(text):
    return [s.strip() for s in SENT_RE.split((text or "").strip()) if s.strip()]


def word_count(text):
    return len(WORD_RE.findall(text or ""))


def read_time_min(text):
    return max(1, round(word_count(text) / 200))


def source_token_set(text):
    return {w.lower() for w in WORD_RE.findall(text or "")}


def ungrounded_tokens(draft_text, source_text):
    """Numbers / proper nouns in the draft not present in the source (should be empty for extractive)."""
    src = source_token_set(source_text)
    hits = []
    for tok in WORD_RE.findall(draft_text or ""):
        if (NUM_RE.search(tok) or PROPER_RE.fullmatch(tok)) and tok.lower() not in src:
            hits.append(tok)
    return sorted(set(hits))


def get_source_text(item, articles_dir, allow_fetch, unavailable):
    """Return (text, source_text_used). Honors the priority chain; never fabricates."""
    cid = item["id"]
    if cid in unavailable:
        return "", "none"
    # 1. cached full article
    p = os.path.join(articles_dir, f"{cid}.txt")
    if os.path.exists(p):
        t = open(p, encoding="utf-8").read().strip()
        if t:
            return t, "full_article"
    # 2. live fetch (runner only)
    if allow_fetch:
        try:
            req = urllib.request.Request(item["url"], headers={"User-Agent": "SignalsWriter/1.0"})
            doc = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
            # remove non-content regions entirely, then turn block tags into line breaks so
            # sentences separate cleanly (clean_sentences does the boilerplate filtering later).
            doc = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside|figure|figcaption|form)\b.*?</\1>", " ", doc)
            doc = re.sub(r"(?is)<(br|/p|/div|/li|/h[1-6]|/section|/article)\s*/?>", "\n", doc)
            text = re.sub(r"<[^>]+>", " ", doc)
            text = html.unescape(text).replace("\xa0", " ")
            text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
            if word_count(text) >= 120:          # crude "did we get a real body?" gate
                return text, "full_article"
        except Exception:
            pass
    # 3. RSS snippet
    snip = (item.get("snippet") or "").strip()
    if snip:
        return snip, "rss_snippet"
    # 4. nothing
    return "", "none"


# ----------------------------------------------------------------- summary/why composition
# These build copy that satisfies the SAME strict editorial checks the validator enforces
# (summary_quality_issues / why_quality_issues, defined below). Repairing extraction here — rather
# than weakening the gate — is what keeps a too-short or mid-sentence summary from ever reaching
# `writer.py validate --strict`. Still grounded: they only ever SELECT or JOIN real source sentences,
# never invent text. If no quality summary can be formed, the caller flags the draft → fail-closed.

def _compose_quality_summary(clean, headline):
    """A summary that passes summary_quality_issues, drawn from clean body sentences. Prefer a single
    headline-relevant, well-formed sentence; else JOIN consecutive sentences (from a capitalized
    start) until the bar is met (fixes 'too short'). Returns '' if no quality summary is possible."""
    if not clean:
        return ""
    hk = keywords(headline)
    score = {s: len(keywords(s) & hk) for s in clean}
    order = sorted(range(len(clean)), key=lambda i: (score[clean[i]] == 0, i))  # headline-relevant first
    # 1) a single already-well-formed sentence (starts uppercase, ends clean, >=12 words, no dangling)
    for i in order:
        if not summary_quality_issues(clean[i]):
            return clean[i]
    # 2) combine consecutive sentences, beginning at a real (capitalized) sentence start
    for start in order:
        if not clean[start][:1].isupper():       # never begin a summary mid-sentence
            continue
        combo, j = clean[start], start + 1
        while summary_quality_issues(combo) and j < len(clean):
            combo = (combo + " " + clean[j]).strip()
            j += 1
        if not summary_quality_issues(combo):
            return combo
    return ""


def _compose_quality_why(clean, headline, summary):
    """A whyItMatters that passes why_quality_issues and is NOT part of the summary. Prefer a
    headline-relevant stakes sentence, then any stakes sentence, then any clean sentence. '' if none."""
    sl = summary.lower()
    hk = keywords(headline)
    score = {s: len(keywords(s) & hk) for s in clean}

    def usable(s):
        return s.lower() not in sl and not why_quality_issues(s, summary, headline)

    for pred in (lambda s: STAKES_RE.search(s) and score[s] > 0,
                 lambda s: STAKES_RE.search(s),
                 lambda s: True):
        for s in clean:
            if usable(s) and pred(s):
                return s
    return ""


def _overlap(a, b):
    """Jaccard token overlap of two keyword sets (0..1). Used to drop near-duplicate takeaways."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _well_formed_bullet(s):
    """A takeaway must read as ONE complete, standalone sentence — not a fragment, caption, bare
    quote, or an overlong blob. (Issue: keyTakeaways fragments/duplicates/captions.)"""
    words = re.findall(r"[A-Za-z0-9']+", s)
    if not (7 <= len(words) <= 40):                 # too short (fragment) or too long
        return False
    if not s[:1].isupper():                         # begins mid-sentence
        return False
    if s[:1] in ('"', "“", "'", "‘"):               # bare quote without context
        return False
    if not _ENDS_OK.search(s) or ELLIPSIS_END_RE.search(s):   # no clean end / cut off
        return False
    if words[-1].lower() in _DANGLING_END:          # ends mid-clause
        return False
    if looks_like_caption(s):                       # photo caption / scene
        return False
    if looks_like_metadata(s):                      # author bio / metadata / stitched quote (v2.5)
        return False
    return True


def _dedupe_takeaways(cands, against, limit=3):
    """Up to `limit` distinct bullets: drop any that overlap (Jaccard ≥ 0.6) the summary/why in
    `against` OR an already-chosen bullet — so takeaways never duplicate each other or the summary."""
    chosen, used = [], [keywords(a) for a in against if a]
    for s in cands:
        t = keywords(s)
        if not t or any(_overlap(t, u) >= 0.6 for u in used):
            continue
        chosen.append(s)
        used.append(t)
        if len(chosen) == limit:
            break
    return chosen


# ----------------------------------------------------------------- draft
def draft_one(item, source_text, used):
    role = "lead" if item.get("lead") else "supporting"
    base = {
        "id": item["id"], "number": item.get("number"),
        "selectedRole": role, "category": item.get("category", "OTHER"),
        "source": item["source"], "originalURL": item["url"],
        "source_text_used": used, "confidence": "low", "flags": [],
        "draft": {"headline": "", "summary": "", "keyTakeaways": [], "whyItMatters": "", "readTime": 0},
    }

    # --- failure path: no usable source text ---
    if used == "none" or not source_text:
        base["confidence"] = "low"
        base["flags"] = ["source_unavailable", "needs_review"]
        return base

    # Clean the source first: strip boilerplate/nav/captions/credits/bios, decode entities, keep prose.
    clean = clean_sentences(source_text, source_name=item.get("source", ""))
    headline = item["title"]                              # the outlet's own headline (grounded)
    flags = ["extractive_draft"]
    if used == "rss_snippet":
        flags += ["thin_source", "rss_snippet_only"]

    if not clean:
        # Cleaning removed everything (pure boilerplate / too thin). Use the raw snippet ONLY if it is
        # itself a quality summary; otherwise emit NO summary (never ship a fragment) and flag. Either
        # way this draft fails closed via the flags below — we just never emit malformed copy.
        raw = html.unescape(source_text).strip() if used == "rss_snippet" else ""
        summary = raw if (raw and not summary_quality_issues(raw)) else ""
        takeaways, why, confidence = [], "", "low"
        if not summary:
            flags += ["summary_needs_human", "needs_review"]
        flags += ["keyTakeaways_needs_human", "whyItMatters_needs_human"]
    else:
        # Prefer sentences that share terms with the headline — this pushes any stray boilerplate
        # (which never shares headline terms) out of the summary/takeaways.
        hk = keywords(headline)
        score = {s: len(keywords(s) & hk) for s in clean}
        # summary: an editorially well-formed line (repairs 'too short' / 'mid-sentence' by selecting
        # a capitalized, full, >=12-word sentence — joining consecutive sentences when needed).
        summary = _compose_quality_summary(clean, headline)
        if not summary:
            # The source has prose but none of it can form a quality summary — DON'T ship a fragment.
            # Flag for fail-closed: strict validation + build.py will reject (no PR), per requirement 7.
            takeaways, why, confidence = [], "", "low"
            flags += ["summary_needs_human", "needs_review",
                      "keyTakeaways_needs_human", "whyItMatters_needs_human"]
        else:
            # Sentences consumed by the (possibly multi-sentence) summary — excluded from why/takeaways
            # so whyItMatters is never a verbatim slice of the summary.
            consumed = {s for s in clean if s in summary}
            # whyItMatters: a cautious, NON-fabricated stakes sentence, quality-checked + de-duped.
            why = _compose_quality_why(clean, headline, summary)
            # takeaways: well-formed standalone sentences, headline-relevant first, de-duplicated
            # against the summary + why and each other (no fragments, captions, or repeats).
            pool = [s for s in clean if s not in consumed and s != why and _well_formed_bullet(s)]
            pool.sort(key=lambda s: (score[s] == 0, clean.index(s)))
            takeaways = _dedupe_takeaways(pool, against=[summary, why])
            if not why:                                   # no quality stakes line → closing line / takeaway
                why = next((s for s in reversed(clean) if s not in consumed and s not in takeaways),
                           takeaways[-1] if takeaways else summary)
            if why_quality_issues(why, summary, headline):   # even the fallback isn't editorial → fail-closed
                flags.append("whyItMatters_needs_human")
            if len(takeaways) < 3:
                flags.append("keyTakeaways_thin")
            confidence = "low" if used == "rss_snippet" else \
                         ("high" if (word_count(source_text) >= 250 and len(clean) >= 4) else "medium")

    if item.get("paywalled"):
        flags.append("paywalled")

    # grounding check on generated copy (summary + takeaways + whyItMatters) against the
    # entity-decoded source + the outlet's own headline. Extractive ⇒ should be empty.
    ung = ungrounded_tokens(" ".join([summary] + takeaways + [why]),
                            html.unescape(source_text) + " " + headline)
    if ung:
        flags.append("ungrounded_tokens:" + ",".join(ung[:6]))
        flags.append("needs_review")
        confidence = "low"

    base["confidence"] = confidence
    base["flags"] = flags
    base["draft"] = {
        "headline": headline,
        "summary": summary,
        "keyTakeaways": takeaways,
        "whyItMatters": why,
        "readTime": read_time_min(source_text),   # integer minutes — the app appends its own "min"
    }
    return base


def cmd_draft(args):
    _, sig = load_selection(args.selection)
    unavailable = set(x.strip() for x in (args.simulate_unavailable or "").split(",") if x.strip())
    drafts = []
    for item in sig:
        text, used = get_source_text(item, args.articles, allow_fetch=not args.no_fetch, unavailable=unavailable)
        drafts.append(draft_one(item, text, used))

    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "drafter": "extractive-v1",
        "count": len(drafts),
        "note": "DRAFT suggestions only. Extractive (copied from source text). "
                "Not approved, not latest.json, not published. Human edits + approval required.",
        "signals": drafts,
    }
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=2)

    print(f"✓ wrote {os.path.relpath(args.out)} — {len(drafts)} draft(s), drafter=extractive-v1")
    for d in drafts:
        print(f"  #{d['number']} {d['selectedRole']:10} [{d['id']}] src={d['source_text_used']:12} "
              f"conf={d['confidence']:6} flags={d['flags']}")
    print()
    _validate(args.selection, out, source="(just drafted)")


# ----------------------------------------------------------------- validate
# Auto-publish hard-blockers (mirror build.py): a draft carrying any of these (or low confidence,
# or a missing keyTakeaway / whyItMatters) must NOT auto-publish.
STRICT_BLOCKING_FLAGS = {"needs_review", "source_unavailable", "thin_source", "whyItMatters_needs_human"}

# Editorial quality (auto-publish strict). Catches broken extraction the user reported: summaries
# that begin mid-sentence / are headline fragments / are cut off, and whyItMatters that is a bare
# quote, a fragment, or a line copied verbatim from the summary (not an editorial explanation).
_ENDS_OK = re.compile(r"[.!?][\"')\]”’]?\s*$")
# Single-word endings that mean a sentence was cut off — bare articles, coordinating/subordinating
# conjunctions, and object-requiring prepositions. Phrasal particles that DO validly end a sentence
# ("drags on", "not over", "moved in", "carry on") are deliberately excluded to avoid false positives.
_DANGLING_END = {"the", "a", "an", "of", "to", "and", "or", "that", "this", "with", "for", "from",
                 "as", "but", "said", "its", "his", "her", "their", "than",
                 "between", "among", "amongst", "amid", "while", "because", "after", "before",
                 "against", "despite", "since", "although", "though", "unless", "whereas",
                 "toward", "towards", "without", "within", "whether", "nor"}
# A compound preposition left with ONE capitalized object at the very end = truncated, e.g.
# "negotiations between the U.S." (no "… and Iran" follows). v2.1 broken-text gate.
_COMPOUND_DANGLE = re.compile(
    r"\b(between|among|amongst|amid)\s+(the\s+)?[A-Z][\w.&'’-]*\.?[\"')\]”’]?\s*$")


def _unbalanced_issue(s):
    """Reason string if quotes/brackets are unmatched (scrape garbage), else None.

    v2.3: single quotes / apostrophes (straight ' and curly ‘ ’) are NOT pair-checked — they appear
    in possessives, contractions, names, and years ("Iran's", "Mattel's", "X-Men ’97"), so balancing
    them produced false "unmatched quotation marks" on ordinary text. Only DOUBLE quotes (straight "
    and curly “ ”) and brackets are balanced — those signal real scrape garbage."""
    t = s or ""
    if t.count('"') % 2 or t.count("“") != t.count("”"):
        return "unmatched quotation marks"
    for o, c in (("(", ")"), ("[", "]"), ("{", "}")):
        if t.count(o) != t.count(c):
            return "unmatched brackets/parentheses"
    return None


def summary_quality_issues(summary):
    s = (summary or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", s)
    issues = []
    if len(words) < 12:
        issues.append("summary too short (<12 words)")
    if s and not s[:1].isupper():
        issues.append("summary begins mid-sentence (not capitalized)")
    if s and not _ENDS_OK.search(s):
        issues.append("summary is a headline fragment (no sentence-ending punctuation)")
    if ELLIPSIS_END_RE.search(s):
        issues.append("summary is cut off (ends in an ellipsis)")
    if words and words[-1].lower() in _DANGLING_END:
        issues.append("summary ends mid-clause (cut off)")
    if _COMPOUND_DANGLE.search(s):
        issues.append("summary ends with a dangling phrase (e.g. 'between the U.S.')")
    if _unbalanced_issue(s):
        issues.append("summary has " + _unbalanced_issue(s))
    if looks_like_caption(s):
        issues.append("summary is a photo caption / scene description")
    if looks_like_metadata(s):
        issues.append("summary is author bio / publication metadata / stitched-quote text")
    return issues


def why_quality_issues(why, summary, headline=""):
    w = (why or "").strip()
    s = (summary or "").strip().lower()
    if not w:
        return ["whyItMatters empty"]
    issues = []
    if w[:1] in ('"', "“", "'", "‘"):
        issues.append("whyItMatters is a quote, not an explanation")
    if len(re.findall(r"[A-Za-z0-9']+", w)) < 6:
        issues.append("whyItMatters too short (<6 words)")
    if not w[:1].isupper():
        issues.append("whyItMatters begins mid-sentence")
    if not _ENDS_OK.search(w):
        issues.append("whyItMatters is a fragment (no sentence end)")
    if ELLIPSIS_END_RE.search(w):
        issues.append("whyItMatters is cut off (ends in an ellipsis)")
    if re.findall(r"[A-Za-z0-9']+", w) and re.findall(r"[A-Za-z0-9']+", w)[-1].lower() in _DANGLING_END:
        issues.append("whyItMatters ends mid-clause (cut off)")
    if _COMPOUND_DANGLE.search(w):
        issues.append("whyItMatters ends with a dangling phrase (e.g. 'between the U.S.')")
    if _unbalanced_issue(w):
        issues.append("whyItMatters has " + _unbalanced_issue(w))
    if w.lower() in s:
        issues.append("whyItMatters is copied verbatim from the summary")
    if looks_like_caption(w):
        issues.append("whyItMatters is a photo caption, not an explanation")
    if looks_like_metadata(w):
        issues.append("whyItMatters is author bio / publication metadata, not an explanation")
    if headline:
        ht, wt = keywords(headline), keywords(w)
        if wt and len(wt & ht) / len(wt) >= 0.85:
            issues.append("whyItMatters just repeats the headline")
    return issues


def _validate(selection_path, drafts_obj, source="", strict=False):
    _, sig = load_selection(selection_path)
    by_id = {s["id"]: s for s in sig}
    drafts = drafts_obj["signals"]

    hard, warn = [], []

    # selection match: same id set, same lead, same role/number
    sel_ids = {s["id"] for s in sig}
    drf_ids = {d["id"] for d in drafts}
    if sel_ids != drf_ids:
        hard.append(f"selection mismatch: draft ids {sorted(drf_ids)} != selection {sorted(sel_ids)}")
    sel_lead = next((s["id"] for s in sig if s.get("lead")), None)
    drf_lead = next((d["id"] for d in drafts if d["selectedRole"] == "lead"), None)
    if sel_lead != drf_lead:
        hard.append(f"lead mismatch: draft lead {drf_lead} != selection lead {sel_lead}")

    for d in drafts:
        s = by_id.get(d["id"])
        if not s:
            continue
        # URL + source immutability
        if d.get("originalURL") != s.get("url"):
            hard.append(f"[{d['id']}] URL changed from selection.")
        if not (urlsplit(d.get("originalURL", "")).scheme == "https" and urlsplit(d["originalURL"]).path.strip("/")):
            hard.append(f"[{d['id']}] originalURL is not a real https article URL.")
        if d.get("source") != s.get("source"):
            hard.append(f"[{d['id']}] source changed from selection.")
        if d.get("selectedRole") not in ("lead", "supporting"):
            hard.append(f"[{d['id']}] invalid selectedRole.")

        # required fields missing WITHOUT a flag = hard fail; with a flag = warn
        dr = d.get("draft", {})
        missing = [k for k in ("headline", "summary") if not dr.get(k)] + \
                  (["keyTakeaways"] if not dr.get("keyTakeaways") else [])
        flagged = any(f in d["flags"] for f in
                      ("needs_review", "source_unavailable", "thin_source",
                       "keyTakeaways_needs_human", "whyItMatters_needs_human"))
        if missing and not flagged:
            hard.append(f"[{d['id']}] missing {missing} with no explaining flag.")
        elif missing:
            warn.append(f"[{d['id']}] missing {missing} (flagged: ok to hand-fill).")

        # warn-level signals
        if d["confidence"] == "low":
            warn.append(f"[{d['id']}] low confidence.")
        if d["source_text_used"] == "rss_snippet":
            warn.append(f"[{d['id']}] snippet-only source (thin).")
        if d["source_text_used"] == "none":
            warn.append(f"[{d['id']}] extraction/source failure — needs a replacement or retry.")
        if any(f.startswith("ungrounded_tokens") for f in d["flags"]):
            warn.append(f"[{d['id']}] possible unsupported numbers/proper nouns — check against source.")
        if "paywalled" in d["flags"]:
            warn.append(f"[{d['id']}] paywalled source.")

        # STRICT (auto-publish): the same conditions build.py blocks become HARD here, so the
        # "Validate drafts" gate stops the workflow BEFORE build.py and BEFORE any auto-approval.
        # Manual mode keeps these as warns (a human may hand-fill flagged drafts).
        if strict:
            bad = sorted(set(d.get("flags", [])) & STRICT_BLOCKING_FLAGS)
            if bad:
                hard.append(f"[{d['id']}] unresolved blocking flag(s): {', '.join(bad)} — too thin/uncertain to auto-publish.")
            if d.get("confidence") == "low":
                hard.append(f"[{d['id']}] low confidence — too uncertain to auto-publish.")
            if not (isinstance(dr.get("keyTakeaways"), list) and len(dr.get("keyTakeaways") or []) >= 1):
                hard.append(f"[{d['id']}] needs at least one keyTakeaway to auto-publish.")
            if not (isinstance(dr.get("whyItMatters"), str) and dr.get("whyItMatters", "").strip()):
                hard.append(f"[{d['id']}] missing whyItMatters — required to auto-publish.")
            # Editorial quality of the drafted copy (broken summaries / non-editorial whyItMatters).
            if dr.get("summary"):
                for issue in summary_quality_issues(dr.get("summary")):
                    hard.append(f"[{d['id']}] {issue}.")
            if dr.get("whyItMatters"):
                for issue in why_quality_issues(dr.get("whyItMatters"), dr.get("summary"), dr.get("headline", "")):
                    hard.append(f"[{d['id']}] {issue}.")

    print(f"=== validate_drafts {source}{' [STRICT]' if strict else ''} ===")
    if hard:
        print("✗ HARD FAIL:")
        for h in hard:
            print(f"   - {h}")
    else:
        print("✓ no hard failures (structure, URL/source immutability, selection match all OK).")
    if warn:
        print("⚠ flags for your attention:")
        for w in warn:
            print(f"   - {w}")
    print("\nDRAFTS ONLY — nothing here is approved, assembled into latest.json, or published.")
    return 1 if hard else 0


def cmd_validate(args):
    if not os.path.exists(args.drafts):
        sys.exit(f"ERROR: {args.drafts} not found. Run `writer.py draft` first.")
    sys.exit(_validate(args.selection, json.load(open(args.drafts)),
                       source=os.path.basename(args.drafts), strict=getattr(args, "strict", False)))


def main():
    ap = argparse.ArgumentParser(description="Signals Writer v1 (extractive).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--selection", default=DEF_SEL)
    d.add_argument("--articles", default=DEF_ART)
    d.add_argument("--no-fetch", action="store_true", help="disable live fetch (sandbox/offline)")
    d.add_argument("--simulate-unavailable", default="", help="comma ids to force the failure path")
    d.add_argument("--out", default=DEF_OUT)
    v = sub.add_parser("validate")
    v.add_argument("--selection", default=DEF_SEL)
    v.add_argument("--drafts", default=DEF_OUT)
    v.add_argument("--strict", action="store_true",
                   help="auto-publish mode: hard-fail on thin/incomplete/low-confidence drafts (no hand-fill)")
    args = ap.parse_args()
    (cmd_draft if args.cmd == "draft" else cmd_validate)(args)


if __name__ == "__main__":
    main()
