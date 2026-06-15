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
from editorial import topic_fingerprint, topics_overlap   # v2.1: duplicate-topic detection

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


def pick_supporting(pool, lead, now, fresh_hours, need=4):
    """Pick `need` supporting from pool (already excludes the lead's cluster), preferring category
    diversity (<= CATEGORY_CAP per category incl. the lead), relaxing the cap only if needed.

    v2.1 duplicate-topic gate: a candidate whose topic fingerprint overlaps the lead's or an
    already-chosen supporting's is SKIPPED — so the stronger (higher-ranked, picked first) story is
    kept and the weaker duplicate is replaced by the next distinct one. Never fills with a duplicate;
    if not enough distinct topics exist, returns fewer (the caller's 5-distinct gate then fails closed)."""
    ranked = sorted(pool, key=lambda c: (-base_score(c, now, fresh_hours), short_id(c)))
    lead_fp = _fp(lead)
    best = []
    for cap in (CATEGORY_CAP, CATEGORY_CAP + 1, 999):
        chosen, counts, fps = [], {(lead.get("category") or "OTHER").upper(): 1}, [lead_fp]
        for c in ranked:
            fp = _fp(c)
            if any(topics_overlap(fp, f) for f in fps):     # same/near-identical topic → skip
                continue
            cat = (c.get("category") or "OTHER").upper()
            if counts.get(cat, 0) < cap:
                chosen.append(c)
                counts[cat] = counts.get(cat, 0) + 1
                fps.append(fp)
            if len(chosen) == need:
                return chosen
        if len(chosen) > len(best):
            best = chosen
    return best   # fewer than `need` only when distinct topics run out → caller fails closed


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
    supporting = pick_supporting(support_pool, lead, now, args.fresh_hours, need=4)

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
    _summary(args.summary_file, lead, supporting, total) if args.summary_file else None


if __name__ == "__main__":
    main()
