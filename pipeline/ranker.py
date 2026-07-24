#!/usr/bin/env python3
"""
Signals — Ranker v1 (deterministic, rule-based auto-selection).

Reads pipeline/candidates.json (from Scout) and writes pipeline/selection.yaml — exactly one
Lead + four Supporting ids — for the Daily Auto Edition pipeline. Deterministic and rule-based:
NO LLM, no randomness; the same candidates always yield the same five. It replaces the human
`selection.yaml` step ONLY in auto mode, and is gated hard so weak mornings produce NO selection
(the workflow then opens no PR — see DAILY_OPS.md).

It is a SELECTION tool only: it never writes copy, never touches latest.json, never publishes.
selection.py / writer.py / build.py / validate_feed.py remain the downstream gates.

Hard fail (exit 1, NO selection.yaml written), printing the reason:
  - fewer than --min-candidates (default 20)
  - fewer than 5 candidates with a real article URL
  - no lead-quality story found
  - not enough eligible supporting stories (need 4)
  - a chosen story has no real article URL, or duplicate URLs/ids slip through

On success it writes selection.yaml with `approved: true` (auto-approved: all selection gates
passed) plus `lead` + `supporting`. selection.py reads only `lead`/`supporting`; the extra keys
are ignored.

Usage:
  python3 pipeline/ranker.py [--candidates candidates.json] [--out selection.yaml]
                             [--min-candidates 20] [--stale-hours 48] [--fresh-hours 36]
                             [--summary-file PATH]   # markdown summary (e.g. $GITHUB_STEP_SUMMARY)
"""
import sys, os, re, json, argparse, hashlib, datetime
from urllib.parse import urlsplit
from editorial import topic_fingerprint, topics_overlap, story_metadata   # v2.1 + Morning Mix

HERE = os.path.dirname(os.path.abspath(__file__))
DEF_CAND = os.path.join(HERE, "candidates.json")
DEF_OUT = os.path.join(HERE, "selection.yaml")

# Reliable outlets we prefer (substring match on the source name, lowercased).
RELIABLE = ("bbc", "npr", "guardian", "financial times", "the verge", "al jazeera")

# Lead-worthiness by category: global urgency / war / geopolitics, then economy, then major tech,
# then regional/institutional. A 0 means "never the Lead" (but still fine as Supporting).
LEAD_CATEGORY_WEIGHT = {
    "WORLD": 4, "ECONOMY": 3, "BUSINESS": 3, "FINANCE": 3,
    "TECH": 2, "AI": 2, "JAPAN": 2, "SCIENCE": 1,
}

LIVE_TITLE_RE = re.compile(r"\b(live|as it happened|live blog|liveblog)\b", re.I)
LIVE_URL_RE = re.compile(r"/live(/|-|$)", re.I)
VIDEO_RE = re.compile(r"/(videos?|watch|av)/", re.I)

# Editorial-quality exclusions (Daily Auto Publish). What Matters Today is civic / geopolitical /
# economic / tech / climate / science / institutional — NOT shopping deals, buying guides, or
# low-impact personal first-person essays. These keep that content out of the top five.
# Price/shopping cues only (NOT the bare word "deal", which appears in "trade/nuclear/peace deal").
DEAL_RE = re.compile(
    r"(\$\s?\d[\d,]*\s*off\b|\b\d+%\s*off\b|\bon sale\b|\bon clearance\b|\bsave \$\d|\bcoupon\b|"
    r"\bprime day\b|\bblack friday\b|\bcyber monday\b|\blowest price\b|\bbest price\b|"
    r"\bprice (drop|cut)\b|\bdrops? to \$|\bunder \$\d|\bbuying guide\b|\bgift guide\b|"
    r"\bdiscounted\b|\bdeal of the (day|week)\b)", re.I)
# Personal first-person essays — specific cues only (NOT a bare leading "I"/"My", which appears in
# quoted news headlines like "I have the right papers").
ESSAY_RE = re.compile(
    r"\bi (made|built|tried|spent|created|wrote|learned|quit|turned|gave up|switched|reviewed)\b|"
    r"\bhow i\b|\bwhy i\b|\bmy (year|life|journey|story|experience|app|kid|dog|cat|yard|garden|"
    r"house|apartment|family|routine|setup|desk)\b", re.I)
REVIEW_RE = re.compile(r"\breview:\s|\bhands[- ]on\b|\bunboxing\b", re.I)

# Low-signal content for a "What Matters Today" morning brief — EXCLUDED, not just demoted (Increment
# G). Judged on the HEADLINE only (snippets legitimately mention these words in real news).
PODCAST_RE = re.compile(r"\b(podcast|this week'?s episode|on the latest episode|episode \d+|"
                        r"listen to (this|the) episode|our (weekly )?show)\b", re.I)
# LOOSE entertainment/franchise words. v2.2: these alone no longer reject — only when there is NO
# civic/public-impact context (see CIVIC_OVERRIDE_RE). So "Backlash over Trump's use of anime" stays
# eligible, while "New anime series announced" does not.
ENTERTAINMENT_RE = re.compile(
    r"\b(x-men|masters of the universe|star wars|marvel|dc (comics|universe)|"
    r"trailer|season (finale|premiere)|box office|spoilers?|"
    r"netflix|disney\+?|hbo max|prime video|anime|comic[- ]con|"
    r"cinematic universe|reboot|spin[- ]?off|fan(dom| service)|"
    r"\bmovie\b|\btv show\b|\bsitcom\b|streaming series|"
    r"celebrity|red carpet|grammys?|oscars?|golden globes?|met gala)\b", re.I)
NOSTALGIA_RE = re.compile(r"\b(nostalgia|throwback|remember when|growing up with|an ode to|"
                          r"in praise of|the magic of|why i still love|a love letter to)\b", re.I)
PRODUCT_RE = re.compile(
    r"(\breview:|\bhands[- ]on\b|\bunboxing\b|buying guide|gift guide|"
    r"\b\d+ best\b|\bbest \w+ (of|for|to buy|under)\b|our favorite|"
    r"\bdeal[s]?:|\bon sale\b|\bdiscount\b|prime day|black friday|cyber monday|"
    r"universal remote|\bearbuds?\b|\bheadphones?\b|smartwatch|\bgadget\b|\bgizmo\b)", re.I)
# Explicit low-signal FORMATS — a review / recap / profile / album-drop is low-signal regardless of
# subject, so these ALWAYS reject (v2.2: format-only; bare 'celebrity'/awards moved to the loose tier
# above so a civic story can override them). NOT a ban on all culture.
ARTS_REVIEW_RE = re.compile(
    r"\b(album review|music review|ep review|track review|single review|song review|"
    r"film review|movie review|tv review|television review|series review|game review|"
    r"book review|art review|gallery review|exhibition review|restaurant review|concert review|"
    r"celebrity profile|\brecaps?\b|best songs|new album|new single|debut album|"
    r"track[- ]by[- ]track)\b", re.I)
# Public-impact context — if any of these is present, a LOOSE entertainment/nostalgia/essay word must
# NOT auto-reject the story (v2.2 civic override). Reviews/recaps/profiles/shopping still reject.
CIVIC_OVERRIDE_RE = re.compile(
    r"\b(backlash|protest\w*|boycott|controvers\w*|government|president|white house|\btrump\b|"
    r"\bbiden\b|election|politic\w*|policy|policies|police|\blaw\b|laws|lawsuit|legislation|"
    r"regulat\w*|antitrust|court|ruling|diplomat\w*|embassy|military|defen[cs]e|security|"
    r"sanction\w*|labor|labour|union|strike|layoffs?|\bai\b|artificial intelligence|platform|"
    r"moderation|copyright|business|market\w*|econom\w*|\bsafety\b|privacy|\bdata\b|deepfake)\b", re.I)
# HIGH-IMPACT tech / government / business policy — ALWAYS eligible (v2.1, req 5). Never misclassified
# as junk even when it names a company. Requires a POLICY/IMPACT cue (or a serious AI lab), so generic
# shopping like "Amazon Prime Day deals" is NOT swept in.
HIGH_IMPACT_RE = re.compile(
    r"\b(antitrust|monopoly|white house|executive order|national security|export controls?|"
    r"sanction\w*|regulat\w*|oversight|supreme court|federal (probe|investigation|lawsuit|trade)|"
    r"data center|semiconductor|chip (ban|export|act|war)|"
    r"anthropic|openai|deepmind|\bai (policy|regulation|safety|act|bill|rules?|oversight|deal|"
    r"partnership|infrastructure)\b|"
    r"government (contract|deal|partnership|funding)|cloud (deal|contract|partnership)|"
    r"acquisition|merger|\bbillion\b)\b", re.I)

# Title cues that signal civic/institutional importance — a positive nudge toward "what matters".
IMPORTANCE_RE = re.compile(
    r"\b(government|parliament|congress|senate|election|vote|court|ruling|lawsuit|sanction\w*|"
    r"treaty|ceasefire|war|peace|diplomacy|summit|military|nuclear|economy|inflation|tariff|"
    r"climate|emissions|environment|wildfire|flood|drought|outbreak|disease|health|pandemic|"
    r"president|minister|policy|regulat\w*|central bank|interest rate|geopolit\w*|"
    r"infrastructure|energy|grid|labor|labour|layoffs|unemployment|wages|strike|gdp|recession|"
    r"trade|supply chain|semiconductor|data breach|cyberattack|surveillance|privacy|"
    r"supreme court|indict\w*|charged|aid|famine|refugee|humanitarian)\b", re.I)

# Per-category cap among the final five, to avoid "all 5 the same kind of story" when possible.
CATEGORY_CAP = 2

# ── Morning Mix composition (2026-07-24) ────────────────────────────────────────────────
# The five-story edition must FEEL varied: not four WORLD stories, not three conflict
# headlines, not the same country three days running. These are deterministic score
# adjustments and caps applied to SUPPORTING selection only — lead selection is preserved
# unchanged. Diversity figures are TARGETS (preferences), never fail-closed gates: a thin
# news day still returns exactly five.
WORLD_CAP = 2                    # hard cap; a 3rd WORLD only via the dominant-news override
DOMINANT_CLUSTER_MIN = 4         # override needs cluster_size >= 4 AND multi-publisher
LAUNCH_BOOST = 3.5               # real consumer product/platform launch (editorial.py gate)
DISCOVERY_BOOST = 1.5            # science/health discovery value
EARNINGS_PENALTY = -1.5          # routine earnings stories are rarely morning-worthy
SAME_COUNTRY_PENALTY = -3.0      # 2nd story about a country already in the five (same family)
CONFLICT_TONE_PENALTY = -4.0     # a THIRD conflict/crisis-toned story
HISTORY_PENALTIES = (-2.5, -3.5, -4.5)  # (country,event_family) seen 1 / 2 / 3 consecutive recent editions
HISTORY_MAX_DAYS = 3             # read at most the previous 3 committed edition dates
CONFLICT_TONES = ("negative_conflict", "negative_crisis")
DISCOVERY_SLOT_MIN_BASE = 4.0    # never force a WEAK story into the discovery slot


def meta(c, now=None):
    """Morning Mix metadata for a candidate, cached on the dict. Optional hints only —
    a fully-neutral result never invalidates the candidate."""
    if "_mm" not in c:
        c["_mm"] = story_metadata(c.get("title", ""), c.get("snippet", ""),
                                  reliability=c.get("source_reliability"),
                                  published_at=c.get("published_at"), now=now)
    return c["_mm"]


def load_history(editions_dir, today, max_days=HISTORY_MAX_DAYS):
    """Read-only recency memory: (country, event_family) pairs from the last committed
    editions strictly before `today` (at most `max_days` dates). Returns
    (pairs → [dates newest-first], [dates newest-first]). Any read problem → empty
    history (neutral), never an error."""
    pairs, dates = {}, []
    try:
        names = sorted(f for f in os.listdir(editions_dir)
                       if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", f))
    except OSError:
        return pairs, dates
    dates = [n[:-5] for n in names if n[:-5] < str(today)][-max_days:][::-1]  # newest first
    for d in dates:
        try:
            ed = json.load(open(os.path.join(editions_dir, d + ".json")))
        except Exception:
            continue
        for s in ed.get("signals", []):
            m = story_metadata(s.get("headline", ""), s.get("summary", ""))
            if m["country"] and m["event_family"] != "other":
                pairs.setdefault((m["country"], m["event_family"]), []).append(d)
    return pairs, dates


def history_run(c, history, now=None):
    """(consecutive_days, matched_dates) — how many CONSECUTIVE most-recent editions
    already carried this candidate's (country, event_family). 0 = no penalty."""
    pairs, dates = history
    m = meta(c, now)
    if not m["country"] or m["event_family"] == "other":
        return 0, []
    hits = pairs.get((m["country"], m["event_family"]), [])
    run = []
    for d in dates:                     # newest first; run must start at the newest edition
        if d in hits:
            run.append(d)
        else:
            break
    return len(run), run


def mix_static(c, history, now=None):
    """Chosen-set-independent Morning Mix adjustments: (delta, notes)."""
    m = meta(c, now)
    delta, notes = 0.0, []
    if m["consumer_launch"]:
        delta += LAUNCH_BOOST
        notes.append(f"launch+{LAUNCH_BOOST}")
    if m["discovery_value"]:
        delta += DISCOVERY_BOOST
        notes.append(f"discovery+{DISCOVERY_BOOST}")
    if m["event_family"] == "earnings":
        delta += EARNINGS_PENALTY
        notes.append(f"earnings{EARNINGS_PENALTY}")
    run, matched = history_run(c, history, now)
    if run:
        p = HISTORY_PENALTIES[min(run, len(HISTORY_PENALTIES)) - 1]
        delta += p
        # log the EXACT history match (which pair, which committed editions)
        notes.append(f"history{p} ({m['country']}/{m['event_family']} in {', '.join(matched)})")
    return delta, notes


def mix_dynamic(c, chosen, now=None):
    """Adjustments that depend on the stories ALREADY selected: (delta, notes)."""
    m = meta(c, now)
    delta, notes = 0.0, []
    dominant = int(c.get("cluster_sources") or 1) >= 3
    if m["country"]:
        for s in chosen:
            sm = meta(s, now)
            if sm["country"] != m["country"]:
                continue
            if sm["event_family"] != m["event_family"]:
                notes.append(f"country-dup waived vs {short_id(s)} (event family materially different)")
            elif dominant:
                notes.append(f"country-dup waived vs {short_id(s)} (dominant multi-publisher story)")
            else:
                delta += SAME_COUNTRY_PENALTY
                notes.append(f"same-country{SAME_COUNTRY_PENALTY} (vs {short_id(s)})")
            break
    if m["tone"] in CONFLICT_TONES and \
            sum(1 for s in chosen if meta(s, now)["tone"] in CONFLICT_TONES) >= 2:
        delta += CONFLICT_TONE_PENALTY
        notes.append(f"third-conflict-tone{CONFLICT_TONE_PENALTY}")
    return delta, notes


def short_id(c):
    """The id the Ranker EMITS — the candidate's own `id` field from candidates.json, used directly
    (never recomputed from url/cluster/title). Falls back to selection.py's formula only if a
    candidate somehow lacks an `id`, so the fallback still resolves."""
    return c.get("id") or selection_id(c)


def selection_id(c):
    """EXACTLY what selection.py.short_id computes — sha1 of the canonical URL, 6 hex. This is the
    key selection.py will look the story up by when it builds `by_id`. We use it only to (a) keep
    the selectable pool to candidates selection.py can actually resolve, and (b) self-check the
    final five before writing selection.yaml — never as the emitted id."""
    return hashlib.sha1((c.get("canonical_url") or "").encode("utf-8")).hexdigest()[:6]


def resolvable(c):
    """True only when the id we'd EMIT for this candidate equals the id selection.py will look it
    up by — i.e. selection.py.build can find it. Candidates whose stored `id` diverges from
    sha1(canonical_url) are unselectable (that exact divergence is the bug this guards against)."""
    return short_id(c) == selection_id(c)


def has_real_url(url):
    p = urlsplit(url or "")
    return p.scheme == "https" and p.netloc != "" and p.path.strip("/") != "" and not VIDEO_RE.search(p.path)


def is_live_blog(c):
    return bool(LIVE_TITLE_RE.search(c.get("title", "")) or LIVE_URL_RE.search(urlsplit(c.get("url", "")).path))


def is_reliable(c):
    s = (c.get("source") or "").lower()
    return any(r in s for r in RELIABLE)


# Thin-source avoidance (Daily Auto Publish reliability) ─────────────────────────────────────────
# The Writer flags thin_source / low confidence / missing takeaways when it can't get the full
# article body and falls back to the RSS snippet — which happens for PAYWALLED sources (the fetch
# is blocked). Reliable outlets fetch dependably even when their RSS blurb is short, so we gate on
# fetchability (paywalled), not snippet length alone.
MIN_SNIPPET = 140   # chars — an UNKNOWN (non-reliable) source needs at least this much RSS text to
                    # be a safe fallback. Reliable outlets bypass it (they fetch full bodies fine).


def snippet_len(c):
    return len((c.get("snippet") or "").strip())


def likely_complete(c):
    """Will this likely produce a COMPLETE draft (full body → real keyTakeaways + whyItMatters),
    not a thin snippet-only one? Paywalled → live fetch blocked → snippet-only → thin, so out.
    Reliable outlet + non-paywalled → fetches dependably (even with a short RSS blurb). Unknown
    source → trust only when the RSS text is already substantial (>= MIN_SNIPPET)."""
    if c.get("paywalled"):
        return False
    if is_reliable(c):
        return True
    return snippet_len(c) >= MIN_SNIPPET


def source_risk(c):
    """0..N thin-source risk — for logging + a tie-breaking score penalty (higher = more likely thin)."""
    r = 0
    if c.get("paywalled"):
        r += 5
    if not is_reliable(c):
        r += 1
    if (c.get("source_reliability") or "").lower() == "low":
        r += 1
    sl = snippet_len(c)
    if sl < 60:
        r += 3
    elif sl < 120:
        r += 2
    elif sl < MIN_SNIPPET:
        r += 1
    return r


def editorial_kind(c):
    """If the story reads as non-core for What Matters Today, name it; else ''. Judged on the HEADLINE
    only (snippets contain 'deal'/'review' etc. in real news). These kinds are EXCLUDED from
    eligibility (Increment G) so the morning five stay civic/timely — fail closed over filling with
    podcasts, franchise entertainment, nostalgia essays, or product/shopping content."""
    title = (c.get("title") or "")
    # 1) High-impact tech/government/business policy is ALWAYS eligible — checked FIRST so a serious
    #    story (Amazon/Anthropic/White House, antitrust, AI policy…) is never misclassified as junk.
    if HIGH_IMPACT_RE.search(title):
        return ""
    # 2) Explicit low-signal FORMATS (review / recap / profile / album-drop, shopping, podcast,
    #    hands-on) — always rejected regardless of subject; a review is a review.
    if ARTS_REVIEW_RE.search(title):
        return "arts/entertainment review"
    if DEAL_RE.search(title) or PRODUCT_RE.search(title):
        return "product/deal"
    if PODCAST_RE.search(title):
        return "podcast"
    if REVIEW_RE.search(title):
        return "review/buying guide"
    # 3) Loose entertainment / nostalgia / personal-essay words reject ONLY without civic context.
    #    v2.2: a public-impact angle (backlash, Trump, policy, labor, AI, platform, business…) keeps
    #    the story eligible — so "Backlash over Trump's use of anime" is NOT entertainment junk.
    if CIVIC_OVERRIDE_RE.search(title):
        return ""
    if ENTERTAINMENT_RE.search(title):
        return "entertainment/franchise"
    if NOSTALGIA_RE.search(title):
        return "nostalgia essay"
    if ESSAY_RE.search(title):
        return "personal essay"
    return ""


def editorial_junk(c):
    return bool(editorial_kind(c))


def editorial_penalty(c):
    """Score penalty for non-core content (so even in a thin-day fallback it sinks to the bottom)."""
    return 6.0 if editorial_junk(c) else 0.0


def importance_bonus(c):
    """Positive nudge for civic/geopolitical/economic/infrastructure/climate/health headline cues —
    so 'what matters today' stories float above merely-available ones. (Increment G: strengthened.)"""
    return 2.0 if IMPORTANCE_RE.search((c.get("title") or "")) else 0.0


def why_selected(c):
    """Short human reason a candidate ranked well (logging)."""
    bits = []
    if is_reliable(c):
        bits.append("reliable")
    if int(c.get("cluster_size") or 1) >= 2:
        bits.append(f"cluster×{c.get('cluster_size')}")
    if snippet_len(c) >= MIN_SNIPPET:
        bits.append("rich-snippet")
    if not c.get("paywalled"):
        bits.append("non-paywalled")
    if importance_bonus(c):
        bits.append("civic/important")
    return ", ".join(bits) or "eligible"


def hours_old(c, now):
    raw = c.get("published_at")
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def cat_weight(c):
    return LEAD_CATEGORY_WEIGHT.get((c.get("category") or "").upper(), 0)


def recency01(c, now, fresh_hours):
    h = hours_old(c, now)
    if h is None:
        return 0.0
    return max(0.0, min(1.0, (fresh_hours - h) / fresh_hours))


def base_score(c, now, fresh_hours):
    """Generic importance/quality score (Supporting ranking)."""
    s = 0.0
    s += 3.0 * min(int(c.get("cluster_size") or 1), 4)   # cross-source corroboration = importance
    s += 2.0 * (1 if is_reliable(c) else 0)
    s += 2.0 * recency01(c, now, fresh_hours)
    s += 1.5 * cat_weight(c)
    if hours_old(c, now) is None:
        s -= 0.5
    if is_live_blog(c):
        s -= 4.0
    # Prefer richer / non-paywalled / reliable sources — so within a cross-source cluster the
    # fetchable version (e.g. BBC/Guardian) outranks a paywalled/thin one (e.g. Financial Times).
    s -= 0.6 * source_risk(c)
    # Editorial quality: deals / personal essays / reviews sink; civic/institutional cues float up.
    s -= editorial_penalty(c)
    s += importance_bonus(c)
    return s


def lead_score(c, now, fresh_hours):
    """Lead ranking weights category urgency more heavily."""
    return base_score(c, now, fresh_hours) + 1.5 * cat_weight(c)


def is_stale(c, now, stale_hours):
    h = hours_old(c, now)
    return h is not None and h > stale_hours


def eligible(c, now, stale_hours):
    # resolvable: selection.py can find it by the emitted id.
    # likely_complete: won't become a thin draft (skip paywalled/thin sources before the Writer).
    # not editorial_junk: podcasts/franchise-entertainment/nostalgia/product-deal/review/essay are
    #   EXCLUDED (Increment G) — we fail closed rather than fill the five with low-signal content.
    return (has_real_url(c.get("url", "")) and not is_stale(c, now, stale_hours)
            and resolvable(c) and likely_complete(c) and not editorial_junk(c))


def dedup_by_cluster(cands, now, fresh_hours):
    """Keep the single best-scoring candidate per cross-source cluster (one story = one pick)."""
    best = {}
    for c in cands:
        cid = c.get("cluster_id", id(c))
        if cid not in best or base_score(c, now, fresh_hours) > base_score(best[cid], now, fresh_hours):
            best[cid] = c
    # deterministic order: score desc, then id asc
    return sorted(best.values(), key=lambda c: (-base_score(c, now, fresh_hours), short_id(c)))


def _fp(c):
    return topic_fingerprint(c.get("title", ""), c.get("snippet", ""))


def _cat(c):
    return (c.get("category") or "OTHER").upper()


def dominant_override(c, chosen):
    """Deterministic dominant-news override for a THIRD WORLD story (never a fourth, never
    unlimited): a big multi-publisher cluster (cluster_size >= DOMINANT_CLUSTER_MIN AND >= 2
    distinct publishers) about a DIFFERENT dominant event than the WORLD stories already chosen."""
    return (int(c.get("cluster_size") or 1) >= DOMINANT_CLUSTER_MIN
            and int(c.get("cluster_sources") or 1) >= 2
            and not any(topics_overlap(_fp(c), _fp(s)) for s in chosen if _cat(s) == "WORLD"))


def pick_supporting(pool, lead, now, fresh_hours, history=None, need=4):
    """Pick `need` supporting stories with Morning Mix balance. Greedy + deterministic: at every
    step take the highest ADJUSTED score (base + static mix + dynamic mix vs already-chosen),
    ties broken by lexically smallest id — same inputs always yield the same five.

    v2.1 duplicate-topic gate preserved: a candidate whose topic fingerprint overlaps the lead's
    or an already-chosen story's is SKIPPED (never relaxed).

    Category caps (the old silent 2→3→unlimited relaxation is REMOVED):
      - WORLD: hard max 2; a 3rd only via the logged dominant-news override; NEVER more than 3.
      - other categories: CATEGORY_CAP, relaxed one explicit LOGGED step at a time only when the
        pool can't otherwise fill the five (thin day) — exactly five is still guaranteed there.

    Discovery slot: right after the lead, ONE supporting slot is offered to the highest-ranked
    qualifying consumer-launch/discovery story — but only a genuinely strong one
    (base >= DISCOVERY_SLOT_MIN_BASE). No qualifying story → no slot, nothing forced."""
    history = history or ({}, [])
    chosen = [lead]                       # constraint accounting includes the lead
    fps = [_fp(lead)]
    picked = []                           # (candidate, tag) in pick order — audit
    static = {short_id(c): mix_static(c, history, now) for c in pool}

    def overlap(c):
        return any(topics_overlap(_fp(c), f) for f in fps)

    def allowed(c, relax):
        cat = _cat(c)
        n = sum(1 for s in chosen if _cat(s) == cat)
        if cat == "WORLD":
            if n < WORLD_CAP:
                return True, None
            if n == WORLD_CAP and dominant_override(c, chosen):
                return True, "world-3rd-override"
            return False, None            # WORLD never exceeds 3, never unlimited
        return n < CATEGORY_CAP + relax, None

    def adjusted(c):
        d1, n1 = static[short_id(c)]
        d2, n2 = mix_dynamic(c, chosen, now)
        return base_score(c, now, fresh_hours) + d1 + d2, n1 + n2

    def take(c, tag, notes):
        chosen.append(c)
        fps.append(_fp(c))
        picked.append((c, tag))
        extra = f" · {'; '.join(notes)}" if notes else ""
        print(f"  mix-pick[{tag}] id={short_id(c)} [{_cat(c)}] {c.get('title','')[:46]}{extra}")

    # ── discovery slot ──
    qual = [c for c in pool
            if (meta(c, now)["consumer_launch"] or meta(c, now)["discovery_value"])
            and base_score(c, now, fresh_hours) >= DISCOVERY_SLOT_MIN_BASE
            and not overlap(c) and allowed(c, 0)[0]]
    if qual:
        best = sorted(qual, key=lambda c: (-adjusted(c)[0], short_id(c)))[0]
        take(best, "discovery-slot", adjusted(best)[1])
    else:
        print("  discovery slot: no qualifying launch/discovery story today — not forcing one")

    # ── greedy fill ──
    relax = 0
    while len(chosen) < need + 1:
        best, best_key, best_notes, best_tag = None, None, None, None
        for c in pool:
            if any(s is c for s in chosen) or overlap(c):
                continue
            ok, tag = allowed(c, relax)
            if not ok:
                continue
            score, notes = adjusted(c)
            key = (-score, short_id(c))
            if best is None or key < best_key:
                best, best_key, best_notes, best_tag = c, key, notes, tag
        if best is None:
            if relax < 2:                 # thin day: explicit, logged, non-WORLD-only relaxation
                relax += 1
                print(f"  category cap relaxed to {CATEGORY_CAP + relax} for non-WORLD categories "
                      f"(thin pool — WORLD stays capped)")
                continue
            break                         # distinct topics ran out → caller fails closed
        if best_tag == "world-3rd-override":
            print(f"  WORLD cap override: 3rd WORLD story allowed — id={short_id(best)} is a "
                  f"dominant multi-publisher event (cluster {best.get('cluster_size')}, "
                  f"{best.get('cluster_sources')} publishers, distinct topic)")
        take(best, best_tag or "rank", best_notes)
    return chosen[1:]


def mix_report(five, now):
    """Diversity TARGET report (preferences, informational — never fail-closed)."""
    cats = {_cat(c) for c in five}
    regions = {meta(c, now)["region"] for c in five if meta(c, now)["region"]}
    countries = [meta(c, now)["country"] for c in five if meta(c, now)["country"]]
    dup_countries = {x for x in countries if countries.count(x) > 1}
    heavy = sum(1 for c in five if meta(c, now)["tone"] in CONFLICT_TONES)
    forward = sum(1 for c in five if meta(c, now)["tone"] in ("forward_looking", "discovery"))
    def t(ok):
        return "met " if ok else "MISS"
    print("  mix targets (preferences, not gates):")
    print(f"    [{t(len(cats) >= 3)}] >=3 categories        ({len(cats)}: {', '.join(sorted(cats))})")
    print(f"    [{t(len(regions) >= 3)}] >=3 regions           ({len(regions)}: {', '.join(sorted(regions)) or '-'})")
    print(f"    [{t(not dup_countries)}] <=1 story per country ({'dups: ' + ', '.join(sorted(dup_countries)) if dup_countries else 'no dups'})")
    print(f"    [{t(heavy <= 2)}] <=2 conflict/crisis   ({heavy})")
    print(f"    [{t(forward >= 1)}] >=1 forward-looking   ({forward})")


def fail(reason, summary_path=None):
    print(f"⏭  RANKER STOP — no selection written.\n   reason: {reason}")
    if summary_path:
        _summary(summary_path, lead=None, supporting=[], total=None, skip=reason)
    sys.exit(1)


def _summary(path, lead, supporting, total, skip=None):
    lines = ["## Ranker (auto-selection)\n"]
    if skip:
        lines.append(f"**SKIPPED** — {skip}\n")
        lines.append("No `selection.yaml` written; the pipeline will not open a PR.\n")
    else:
        lines.append(f"- candidates: **{total}**")
        lines.append(f"- lead: **{lead['title'][:80]}**  · _{lead['source']}_  · `{short_id(lead)}`")
        lines.append("- supporting:")
        for c in supporting:
            lines.append(f"  - {c['title'][:80]}  · _{c['source']}_  · `{short_id(c)}`")
    try:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Signals deterministic Ranker (auto-selection v1).")
    ap.add_argument("--candidates", default=DEF_CAND)
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--min-candidates", type=int, default=20)
    ap.add_argument("--stale-hours", type=int, default=48)
    ap.add_argument("--fresh-hours", type=int, default=36)
    ap.add_argument("--summary-file", default=None)
    ap.add_argument("--now", default=None, help="override 'now' ISO8601 (testing/determinism)")
    ap.add_argument("--editions-dir", default=os.path.join(HERE, "..", "editions"),
                    help="committed editions dir (read-only Morning Mix history; missing = neutral)")
    ap.add_argument("--exclude", default="",
                    help="comma-separated candidate ids to EXCLUDE before selection (publish "
                         "recovery: candidates whose drafts failed strict validation). Empty = "
                         "current behavior, byte-identical output.")
    args = ap.parse_args()

    now = (datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else datetime.datetime.now(datetime.timezone.utc))

    if not os.path.exists(args.candidates):
        fail(f"candidates file not found: {args.candidates}", args.summary_file)
    try:
        data = json.load(open(args.candidates))
    except Exception as e:
        fail(f"candidates.json is not valid JSON: {e}", args.summary_file)

    cands = data.get("candidates", [])

    # Publish-recovery exclusion (Fix 1): drop the named ids BEFORE any gate or ranking, so an
    # excluded candidate can never reappear as lead, supporting, or fallback. Matching covers both
    # the candidate's own `id` and the selection.py lookup id (sha1 of canonical_url) — they are
    # verified equal by GATE 6, but matching both keeps the exclusion robust. Every exclusion is
    # logged (auditable, never silent). All existing gates then apply unchanged to the reduced pool.
    excl = {x.strip() for x in (args.exclude or "").split(",") if x.strip()}
    if excl:
        dropped = [c for c in cands if short_id(c) in excl or selection_id(c) in excl]
        cands = [c for c in cands if short_id(c) not in excl and selection_id(c) not in excl]
        print(f"ranker: excluding {len(dropped)} candidate(s) by id: {', '.join(sorted(excl))}")
        for c in sorted(dropped, key=short_id):
            print(f"    excluded [{short_id(c)}] [{c.get('source','?')}] {c.get('title','')[:48]}")

    total = len(cands)
    print(f"ranker: {total} candidates from {os.path.basename(args.candidates)}")

    # GATE 1 — enough candidates
    if total < args.min_candidates:
        fail(f"too few candidates ({total} < {args.min_candidates}) — a thin/failed Scout morning",
             args.summary_file)

    # GATE 2 — enough real article URLs
    valid_url_count = sum(1 for c in cands if has_real_url(c.get("url", "")))
    if valid_url_count < 5:
        fail(f"too few real article URLs ({valid_url_count} < 5)", args.summary_file)

    # GATE 2b — enough COMPLETE-draft candidates. Skip thin/paywalled stories the Writer can't fully
    # draft (they'd fail writer.py validate --strict), and log what we skip so the morning is auditable.
    complete = [c for c in cands if eligible(c, now, args.stale_hours)]
    skipped_thin = [c for c in cands
                    if has_real_url(c.get("url", "")) and not is_stale(c, now, args.stale_hours)
                    and resolvable(c) and not likely_complete(c)]
    if skipped_thin:
        print(f"skipped {len(skipped_thin)} candidate(s) for thin-source risk (paywalled / too thin):")
        for c in sorted(skipped_thin, key=lambda x: -source_risk(x))[:12]:
            tag = "paywalled" if c.get("paywalled") else f"snippet={snippet_len(c)}"
            print(f"    skip risk={source_risk(c)} {tag:13} [{c.get('source','?')}] {c.get('title','')[:44]}")
    print(f"complete-draft candidates: {len(complete)}/{total}")
    if len(complete) < 5:
        fail(f"fewer than 5 complete-draft candidates ({len(complete)}) — too many thin/paywalled "
             f"sources today; they would become thin drafts. Failing closed before the Writer.",
             args.summary_file)

    # Editorial-quality skip log (Increment G): podcasts / franchise entertainment / nostalgia /
    # product-deal / review / personal essay are EXCLUDED from eligibility — list them with reasons
    # so the morning is auditable (req 5). These never enter the five; we fail closed instead.
    skipped_editorial = [c for c in cands
                         if has_real_url(c.get("url", "")) and not is_stale(c, now, args.stale_hours)
                         and resolvable(c) and likely_complete(c) and editorial_junk(c)]
    if skipped_editorial:
        print(f"excluded {len(skipped_editorial)} candidate(s) for editorial quality (non-core):")
        for c in sorted(skipped_editorial, key=lambda x: short_id(x))[:12]:
            print(f"    exclude [{editorial_kind(c):22}] [{c.get('source','?')}] {c.get('title','')[:48]}")

    # Pool tiers, best first: (1) complete + non-live → (2) allow live blogs (last resort). Editorial
    # junk is already removed by `eligible`, so it can NEVER enter the five — a thin morning fails
    # closed (GATE 4 below) rather than filling with low-signal content.
    base = [c for c in cands if eligible(c, now, args.stale_hours)]
    quality = [c for c in base if not is_live_blog(c)]
    pool = dedup_by_cluster(quality, now, args.fresh_hours)
    if len(pool) < 5:
        pool = dedup_by_cluster(base, now, args.fresh_hours)         # allow live blogs (last resort)

    # GATE 3 — a lead-quality story (serious category, corroborated cross-source OR reliable source)
    lead_pool = [c for c in pool if cat_weight(c) > 0 and (int(c.get("cluster_size") or 1) >= 2 or is_reliable(c))]
    if not lead_pool:
        fail("no lead-quality story found (need a serious-category story, cross-sourced or from a "
             "reliable outlet)", args.summary_file)
    # deterministic: highest lead_score, then lexically smallest id
    lead = sorted(lead_pool, key=lambda c: (-lead_score(c, now, args.fresh_hours), short_id(c)))[0]

    # GATE 4 — four eligible supporting stories from OTHER clusters
    support_pool = [c for c in pool if c.get("cluster_id") != lead.get("cluster_id") and short_id(c) != short_id(lead)]
    if len(support_pool) < 4:
        fail(f"not enough eligible supporting stories ({len(support_pool)} < 4 after removing the "
             f"lead's cluster)", args.summary_file)
    # Morning Mix history: committed recent editions only, read-only, at most 3 previous dates.
    history = load_history(args.editions_dir, now.date())
    print(f"morning-mix history: {len(history[1])} committed edition(s) loaded "
          f"({', '.join(history[1]) or 'none — neutral'})")
    supporting = pick_supporting(support_pool, lead, now, args.fresh_hours, history=history, need=4)

    five = [lead] + supporting
    ids = [short_id(c) for c in five]
    urls = [c.get("url") for c in five]

    # GATE 5 — final integrity: 5 distinct ids, real URLs, no duplicate URLs
    if len(set(ids)) != 5:
        fail(f"selection is not 5 distinct stories (ids={ids})", args.summary_file)
    if any(not has_real_url(u) for u in urls):
        fail("a selected story has no real article URL", args.summary_file)
    if len(set(urls)) != 5:
        fail("duplicate article URLs in the selection", args.summary_file)

    # GATE 6 — the FIX: every emitted id must exist in candidates.json *as selection.py will look
    # it up* (sha1 of canonical_url). This guarantees `selection.py build` can resolve all five —
    # the "id X is not in candidates.json" failure can no longer reach the workflow.
    valid_lookup = {selection_id(c) for c in cands}          # the exact set selection.py builds
    print("verify: selected ids exist in candidates.json (selection.py lookup set)")
    for label, c in [("lead", lead)] + [("supporting", s) for s in supporting]:
        sid = short_id(c)
        present = sid in valid_lookup
        print(f"  {label:10} id={sid}  {'✓ in candidates.json' if present else '✗ MISSING'}  "
              f"| {c.get('source','?')} | {c.get('title','')[:48]}")
        if not present:
            fail(f"selected id {sid} ({label}) is not in candidates.json — selection.py would "
                 f"reject it. source={c.get('source')!r} title={c.get('title','')[:60]!r}",
                 args.summary_file)

    # Write selection.yaml (selection.py reads lead/supporting; the rest is provenance it ignores).
    out_lines = [
        "# Auto-generated by ranker.py (deterministic v1) — do not hand-edit; regenerated each run.",
        "# approved: true means every SELECTION gate passed (downstream writer/build/feed gates still apply).",
        "approved: true",
        "mode: auto",
        f"generated_at: {now.isoformat()}",
        f"lead: {ids[0]}",
        "supporting:",
    ] + [f"  - {i}" for i in ids[1:]]
    with open(args.out, "w") as f:
        f.write("\n".join(out_lines) + "\n")

    print(f"✓ wrote {os.path.relpath(args.out)} (approved: true)")
    for label, c in [("Lead", lead)] + [("Support", s) for s in supporting]:
        print(f"  {label:7}: id={short_id(c)} risk={source_risk(c)} [{c.get('source','?')}] "
              f"{c.get('title','')[:46]}")
        print(f"           {c.get('category','?')} · why: {why_selected(c)}")
    cats = [(c.get('category') or 'OTHER') for c in five]
    print(f"  categories: {', '.join(cats)}  | max source-risk: {max(source_risk(c) for c in five)}")
    mix_report(five, now)
    _summary(args.summary_file, lead, supporting, total) if args.summary_file else None


if __name__ == "__main__":
    main()
