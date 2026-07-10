#!/usr/bin/env python3
"""
Signals — publish-time feed validator (Lead Signal Rule, Master Context §8 + Trust Pass).

Run this in the daily publishing pipeline BEFORE deploying. It is the strict gate: a feed that
fails ANY check must NOT be published. The app's runtime check is lenient (logs + graceful
fallback); this is where the principle is actually enforced.

Two modes (see ADR-0001 — local-time morning delivery):

  python3 validate_feed.py <feed.json>
      Validate a single feed file: structure + Lead Signal Rule + a valid YYYY-MM-DD date.
      If the path is editions/<DATE>.json, the file's `date` must equal that filename DATE.
      A LOOSE UTC sanity check rejects only absurd far-future dates (a typo guard); it does
      NOT pin Asia/Tokyo or any other timezone. The client decides whose "today" it is.

  python3 validate_feed.py --consistency <repo-root>
      Repo-level invariants for the date-keyed model:
        - every editions/<DATE>.json has internal `date` == DATE
        - latest.json equals the NEWEST editions/<DATE>.json (by date)
        - latest.json is not older than the newest edition (no stale regression)

Exit code 0 = valid (safe to publish); non-zero = rejected (do not publish).
"""
import sys, os, re, json, glob, datetime
from urllib.parse import urlparse

# How far in the future a publish date may be before we treat it as a typo. Lenient on purpose:
# an edition is normally "tomorrow (UTC)" at build time, and we never pin a single timezone.
FUTURE_SANITY_DAYS = 2

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.json$")


def localized_errors(s):
    """Optional `localized` content (Increment F). ABSENT = fine — English-only editions, including
    every edition published before this, stay valid. This only catches a present-but-MALFORMED block
    so the iOS optional `localized.ja` decoder never chokes; it never requires Japanese to exist and
    never weakens an existing check.

    Shape (all fields optional): localized.ja.{ headline:str, summary:str,
                                                keyTakeaways:[str], whyItMatters:str }"""
    num = s.get("number", "?")
    loc = s.get("localized")
    if loc is None:
        return []                                   # no localization → nothing to check
    if not isinstance(loc, dict):
        return [f"signal {num} `localized` must be an object"]
    ja = loc.get("ja")
    if ja is None:
        return []                                   # `localized` present but no `ja` yet → allowed
    if not isinstance(ja, dict):
        return [f"signal {num} `localized.ja` must be an object"]
    errs = []
    for k in ("headline", "summary", "whyItMatters"):
        if k in ja and not (isinstance(ja[k], str) and ja[k].strip()):
            errs.append(f"signal {num} localized.ja.{k} must be a non-empty string")
    if "keyTakeaways" in ja:
        kt = ja["keyTakeaways"]
        if not (isinstance(kt, list) and kt and all(isinstance(x, str) and x.strip() for x in kt)):
            errs.append(f"signal {num} localized.ja.keyTakeaways must be a non-empty list of strings")
    return errs


# Known dialogue languages. Unknown language keys are TOLERATED (forward-compat) but, if they are
# objects, still shape-checked so a malformed track can't ship.
LISTEN_LANG_KEYS = ("en", "ja")


def _listen_track_errors(num, lang, track):
    """Validate one `listen.<lang>` track (audioURL / gap / captions). All fields optional."""
    if not isinstance(track, dict):
        return [f"signal {num} listen.{lang} must be an object"]
    errs = []
    if "audioURL" in track and not (isinstance(track["audioURL"], str) and track["audioURL"].strip()):
        errs.append(f"signal {num} listen.{lang}.audioURL must be a non-empty string")
    if "gap" in track:
        g = track["gap"]
        if isinstance(g, bool) or not isinstance(g, (int, float)) or g < 0:
            errs.append(f"signal {num} listen.{lang}.gap must be a number >= 0")
    if "captions" in track:
        caps = track["captions"]
        if not isinstance(caps, list):
            errs.append(f"signal {num} listen.{lang}.captions must be a list")
        else:
            for j, c in enumerate(caps):
                where = f"signal {num} listen.{lang}.captions[{j}]"
                if not isinstance(c, dict):
                    errs.append(f"{where} must be an object")
                    continue
                if not (isinstance(c.get("speaker"), str) and c["speaker"].strip()):
                    errs.append(f"{where}.speaker must be a non-empty string")
                if not (isinstance(c.get("text"), str) and c["text"].strip()):
                    errs.append(f"{where}.text must be a non-empty string")
                d = c.get("duration")
                if isinstance(d, bool) or not isinstance(d, (int, float)) or d <= 0:
                    errs.append(f"{where}.duration must be a number > 0")
    return errs


def listen_errors(s):
    """Optional conversational `listen` block (Phase 1). ABSENT = fine — every existing edition stays
    valid and the iOS optional decoder is unaffected. This only catches a present-but-MALFORMED block;
    it is never required and never weakens an existing check. Unknown extra fields are tolerated.

    Shape (all optional): listen.{ format:str,
                                   <lang>:{ audioURL:str, gap:number>=0,
                                            captions:[{ speaker:str, text:str, duration:number>0 }] } }"""
    num = s.get("number", "?")
    listen = s.get("listen")
    if listen is None:
        return []                                   # no conversational data → nothing to check
    if not isinstance(listen, dict):
        return [f"signal {num} `listen` must be an object"]
    errs = []
    if "format" in listen and not isinstance(listen["format"], str):
        errs.append(f"signal {num} listen.format must be a string")
    for k, v in listen.items():
        if k == "format":
            continue
        if k in LISTEN_LANG_KEYS:
            errs += _listen_track_errors(num, k, v)            # known language → must be a valid track
        elif isinstance(v, dict):
            errs += _listen_track_errors(num, k, v)            # unknown language → tolerated, but shape-checked
        # non-dict unknown extra fields → tolerated (ignored) for forward compatibility
    return errs


# Expected shape of a generated EN Listen clip URL: an .mp3 under the edition's OWN date folder,
# e.g. https://<r2-host>/audio/2026-07-09/signal-03-dialogue-en.mp3
def listen_ready_errors(feed, date=None):
    """FAIL-CLOSED promotion gate. A *promoted* pointer (latest.json — the edition the app actually
    serves) must carry a structurally valid `listen.en.audioURL` for ALL 5 signals, in this edition's
    own `/audio/<date>/` folder. This is intentionally STRICTER than the optional `listen_errors`
    shape check: individual editions/<date>.json may be transiently audio-less ("pending" — built by
    Daily Auto Publish, awaiting Auto Generate Listen), but latest.json must NEVER be served audio-less.

    Structural only — non-empty, https, `.mp3`, correct date folder. Network reachability is verified
    UPSTREAM at generation time (listen_generate publicly verifies every clip before its PR), so it is
    deliberately NOT re-checked here: that keeps this a fast, offline, non-flaky required CI gate."""
    errs = []
    d = date if date is not None else feed.get("date")
    sigs = feed.get("signals", [])
    if len(sigs) != 5:
        errs.append(f"a promoted pointer must have 5 signals, found {len(sigs)}")
    for s in sigs:
        num = s.get("number", "?")
        en = ((s.get("listen") or {}).get("en") or {})
        url = en.get("audioURL")
        if not (isinstance(url, str) and url.strip()):
            errs.append(f"signal {num} is missing listen.en.audioURL — latest.json must have EN "
                        f"Listen audio for all 5 signals before it is served (fail-closed)")
            continue
        p = urlparse(url)
        if p.scheme != "https" or not p.netloc:
            errs.append(f"signal {num} listen.en.audioURL is not https: {url!r}")
        if not url.endswith(".mp3"):
            errs.append(f"signal {num} listen.en.audioURL is not an .mp3: {url!r}")
        if d and f"/audio/{d}/" not in url:
            errs.append(f"signal {num} listen.en.audioURL is not in this edition's audio folder "
                        f"(/audio/{d}/): {url!r}")
    return errs


def structural_errors(feed):
    """Lead Signal Rule + content checks — independent of any clock."""
    errors = []
    signals = feed.get("signals", [])

    if len(signals) != 5:
        errors.append(f"expected 5 signals, found {len(signals)}")

    for s in signals:
        imp = s.get("importance")
        if imp is None:
            errors.append(f"signal {s.get('number','?')} missing `importance`")
        elif not (isinstance(imp, int) and 1 <= imp <= 5):
            errors.append(f"signal {s.get('number','?')} importance {imp!r} not in 1..5")

    leads = [s for s in signals if s.get("lead") is True]
    if len(leads) == 0:
        errors.append("no signal has `lead: true`")
    elif len(leads) > 1:
        errors.append(f"multiple leads ({len(leads)}) — exactly one required")

    imps = [s["importance"] for s in signals if isinstance(s.get("importance"), int)]
    if len(leads) == 1 and imps:
        lead_imp = leads[0].get("importance")
        if isinstance(lead_imp, int) and lead_imp != min(imps):
            errors.append(
                f"lead is importance {lead_imp} but the highest tier present is {min(imps)} "
                f"— the lead must be the most important story")

    for s in signals:
        if not (isinstance(s.get("summary"), str) and s["summary"].strip()):
            errors.append(f"signal {s.get('number','?')} has an empty/missing summary")

    # readTime must be an integer (iOS FeedSignal.readTime is Int — a string like "3 min" breaks
    # the whole feed decode and silently drops the app back to bundled fallback.json).
    for s in signals:
        rt = s.get("readTime")
        if not isinstance(rt, int) or isinstance(rt, bool):
            errors.append(f"signal {s.get('number','?')} readTime must be an integer, got {rt!r}")

    for s in signals:
        url = s.get("originalURL", "")
        p = urlparse(url)
        if p.scheme != "https" or not p.netloc:
            errors.append(f"signal {s.get('number','?')} originalURL not https: {url!r}")
        elif p.path.strip("/") == "":
            errors.append(f"signal {s.get('number','?')} originalURL is a homepage, not an article: {url!r}")

    # Optional localized content (Increment F) — only flagged if present-but-malformed.
    for s in signals:
        errors += localized_errors(s)

    # Optional conversational `listen` block (Phase 1) — only flagged if present-but-malformed.
    for s in signals:
        errors += listen_errors(s)

    return errors


# ---------------------------------------------------------------------------
# Editorial gate (v2.6) — defense in depth behind pipeline/writer.py.
#
# The writer already refuses to DRAFT fragments, captions, bios, roundup lines, and
# headline-glued summaries; these checks re-detect the same failure classes at the final
# validate step, so a writer regression (or a hand-edited edition) can never publish
# broken prose. DELIBERATELY self-contained (no import of pipeline.writer): an
# independent implementation can't share the writer's bugs.
# Scope: summary / keyTakeaways / whyItMatters only. Does NOT touch listen-ready,
# consistency, or any promotion/fail-closed logic.

# "…between the U.S." — a compound whose second half never arrives.
_ED_DANGLE_RE = re.compile(
    r"\b(between|among|amongst|amid)\s+(the\s+)?[A-Z][\w.&'’-]*\.?[\"')\]”’]?\s*$")
# Sentence ending at a TITLE-style abbreviation (…said Dr.) → split fragment. Initialisms
# (U.S., E.U.) may legitimately end a sentence; their fragment case is _ED_DANGLE_RE's job.
_ED_ABBREV_END_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Gen|Sen|Rep|Gov|St|vs|No)\.[\"')\]”’]?\s*$")
# Media-credit caption as prose ("This image made from video provided by INFOCA shows…").
_ED_CAPTION_RE = re.compile(
    r"\b(image|photo|photograph|picture|video|footage)\s+"
    r"(made|taken|provided|obtained|released|distributed|courtesy)\b", re.I)
# Career-history author bio ("He joined The Verge in 2019 …" / "…years at Techmeme.").
_ED_BIO_RE = re.compile(
    r"^\s*(he|she|they)\s+joined\s+[A-Z][\w.&'’-]*(?:\s+[A-Z][\w.&'’-]*){0,4}\s+in\s+(19|20)\d\d\b",
    re.I)
_ED_BIO_TAIL_RE = re.compile(r"\byears?\s+at\s+[A-Z][\w.&'’-]+\s*[.\"'”’]*\s*$")
# Context-free connective opener ("And, a look at…").
_ED_CONNECTIVE_RE = re.compile(r"^\s*(And|But|Also|Meanwhile|However)\b[\s,]", re.I)
# Newsletter/roundup table-of-contents line ("Up First briefing: Iran-US; TPS; …").
_ED_ROUNDUP_RE = re.compile(r"^[^.!?]{0,60}\b(briefing|newsletter|roundup|rundown)\b[^.!?]{0,40}:", re.I)
# Scrape artifact: whitespace before punctuation ("strikes , fighting" / "the form .").
_ED_SPACE_PUNCT_RE = re.compile(r"\s[,.;:!?]")


def _editorial_issues(text):
    """Failure-class labels for one prose field ('' / non-str → no issues)."""
    s = (text or "").strip() if isinstance(text, str) else ""
    if not s:
        return []
    issues = []
    if _ED_DANGLE_RE.search(s):
        issues.append("dangling compound ending (e.g. 'between the U.S.')")
    if _ED_ABBREV_END_RE.search(s):
        issues.append("ends at an abbreviation (split fragment)")
    if _ED_CAPTION_RE.search(s):
        issues.append("photo/video credit caption text")
    if _ED_BIO_RE.search(s) or _ED_BIO_TAIL_RE.search(s):
        issues.append("author-bio text")
    if _ED_CONNECTIVE_RE.search(s):
        issues.append("starts with a context-free connective (e.g. 'And,')")
    if _ED_ROUNDUP_RE.search(s) or s.count(";") >= 2:
        issues.append("newsletter/roundup table-of-contents line")
    if _ED_SPACE_PUNCT_RE.search(s):
        issues.append("whitespace before punctuation (scrape artifact)")
    return issues


def _norm_alnum(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def editorial_errors(feed):
    """Prose-quality errors for every signal's summary / keyTakeaways / whyItMatters."""
    errors = []
    for s in feed.get("signals", []):
        num = s.get("number", "?")
        fields = [("summary", s.get("summary")), ("whyItMatters", s.get("whyItMatters"))]
        kts = s.get("keyTakeaways")
        if isinstance(kts, list):
            fields += [(f"keyTakeaways[{i}]", t) for i, t in enumerate(kts)]
        for name, value in fields:
            for issue in _editorial_issues(value):
                errors.append(f"signal {num} {name}: {issue}")
        # Headline glue: the summary must not begin with the (normalized) headline and keep going.
        h, sm = _norm_alnum(s.get("headline")), _norm_alnum(s.get("summary"))
        if h and sm and sm != h and sm.startswith(h + " "):
            errors.append(f"signal {num} summary: begins with the headline glued to body text")
    return errors


def validate(path):
    """Single-file validation: structure + editorial quality + a valid date
    (+ filename match + loose future guard)."""
    errors = []
    try:
        feed = json.load(open(path))
    except Exception as e:
        return [f"feed is not valid JSON: {e}"]

    errors += structural_errors(feed)
    errors += editorial_errors(feed)   # v2.6 prose-quality gate (defense in depth behind writer)

    # date must be a valid YYYY-MM-DD label (timezone-agnostic — it names the morning it serves).
    raw = feed.get("date")
    feed_date = None
    try:
        feed_date = datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        errors.append(f"invalid or missing feed date: {raw!r}")

    if feed_date is not None:
        # editions/<DATE>.json — the internal date must equal the filename date.
        m = DATE_RE.search(os.path.basename(path))
        if m and m.group(1) != raw:
            errors.append(
                f"edition filename date {m.group(1)} != internal date {raw!r} "
                f"(editions/<DATE>.json must be self-consistent)")

        # LOOSE UTC sanity (typo guard only): reject an absurd far-future date. Not a timezone
        # rule — the client decides whose "today" it is; this only catches obvious mistakes.
        utc_today = datetime.datetime.now(datetime.timezone.utc).date()
        if feed_date > utc_today + datetime.timedelta(days=FUTURE_SANITY_DAYS):
            errors.append(
                f"implausible future date: {raw} is more than {FUTURE_SANITY_DAYS} days "
                f"ahead of UTC today ({utc_today}) — likely a typo")

    return errors


def _is_listen_complete(data, date):
    """True iff this edition carries valid EN Listen audio for all 5 signals (i.e. promotable)."""
    return not listen_ready_errors(data, date)


def consistency_errors(root):
    """Repo-level invariants for the date-keyed editions model — FAIL-CLOSED on Listen.

    latest.json (the pointer the app serves) must equal the NEWEST edition that is Listen-complete
    (EN audio for all 5). Editions that are newer but still awaiting audio ("pending") are allowed
    to EXIST — Daily Auto Publish writes them and Auto Generate Listen fills the audio — but they
    must NEVER be what latest.json points at. This is what guarantees the app is never served an
    audio-less edition (no TTS-as-default)."""
    errors = []
    edir = os.path.join(root, "editions")
    files = sorted(glob.glob(os.path.join(edir, "*.json")))
    if not files:
        return [f"no editions/*.json found under {root!r} — nothing to check"]

    dated = []   # (date, path, parsed)
    for f in files:
        m = DATE_RE.search(os.path.basename(f))
        if not m:
            errors.append(f"edition file not named editions/<YYYY-MM-DD>.json: {os.path.basename(f)}")
            continue
        try:
            data = json.load(open(f))
        except Exception as e:
            errors.append(f"{os.path.basename(f)} is not valid JSON: {e}")
            continue
        if data.get("date") != m.group(1):
            errors.append(f"{os.path.basename(f)} internal date {data.get('date')!r} != filename {m.group(1)}")
        dated.append((m.group(1), f, data))

    if not dated:
        return errors or ["no valid editions found"]

    latest_path = os.path.join(root, "latest.json")
    if not os.path.exists(latest_path):
        errors.append("latest.json is missing")
        return errors
    try:
        latest = json.load(open(latest_path))
    except Exception as e:
        return errors + [f"latest.json is not valid JSON: {e}"]

    # (1) The served pointer must ITSELF be Listen-complete (fail-closed).
    errors += [f"latest.json: {e}" for e in listen_ready_errors(latest, latest.get("date"))]

    # (2) latest.json must equal the NEWEST Listen-complete edition. Newer pending (audio-less)
    #     editions are permitted to exist and are skipped for the pointer comparison.
    complete = [(dt, f, data) for (dt, f, data) in dated if _is_listen_complete(data, dt)]
    if not complete:
        errors.append("no Listen-complete edition exists yet (every edition is awaiting EN audio) "
                      "— latest.json cannot be validated")
        return errors
    newest_date, newest_path, newest_data = max(complete, key=lambda t: t[0])

    if latest.get("date") != newest_date:
        errors.append(
            f"latest.json date {latest.get('date')!r} != newest Listen-complete edition {newest_date} "
            f"({os.path.basename(newest_path)}) — latest.json must point at the newest edition that "
            f"has EN Listen for all 5 (an audio-less edition must not be promoted; stale-date regression)")
    elif latest != newest_data:
        errors.append(
            f"latest.json content differs from editions/{newest_date}.json — "
            f"latest.json must be an exact copy of the newest Listen-complete edition")

    return errors


def image_reuse_errors(root, window=90):
    """Opt-in image-variety check: the NEWEST edition must not reuse an imageURL from the previous
    `window` editions (the reuse COOLDOWN; default 90). Reuse OUTSIDE the window is fine — this is a
    cooldown, not a blacklist. Kept SEPARATE from the default gates so it never breaks existing history;
    it is a forward-looking guard for freshly built editions (build.py already avoids reuse at build)."""
    edir = os.path.join(root, "editions")
    dated = []
    for f in glob.glob(os.path.join(edir, "*.json")):
        m = DATE_RE.search(os.path.basename(f))
        if not m:
            continue
        try:
            dated.append((m.group(1), json.load(open(f))))
        except Exception as e:
            return [f"{os.path.basename(f)} is not valid JSON: {e}"]
    if len(dated) < 2:
        return []                                   # nothing to compare against yet
    dated.sort(key=lambda t: t[0])
    newest_date, newest = dated[-1]
    prior = dated[-(window + 1):-1]                 # up to `window` editions before the newest
    recent = {s["imageURL"] for _, d in prior for s in d.get("signals", []) if s.get("imageURL")}
    errs = []
    for s in newest.get("signals", []):
        u = s.get("imageURL")
        if u and u in recent:
            errs.append(f"[{newest_date}] signal {s.get('number','?')} reuses an imageURL from the "
                        f"last {window} editions: {u}")
    return errs


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--consistency":
        errs = consistency_errors(sys.argv[2])
        label = "repo consistency"
    elif len(sys.argv) == 3 and sys.argv[1] == "--image-reuse":
        errs = image_reuse_errors(sys.argv[2])
        label = "image reuse (newest edition vs last 90 — cooldown)"
    elif len(sys.argv) == 3 and sys.argv[1] == "--listen-ready":
        # Fail-closed promotion gate: a pointer file (latest.json) must carry EN Listen audio for
        # all 5 signals in its own date folder before it may be served.
        try:
            data = json.load(open(sys.argv[2]))
            errs = listen_ready_errors(data, data.get("date"))
        except Exception as e:
            errs = [f"{sys.argv[2]} is not valid JSON: {e}"]
        label = f"listen-ready ({sys.argv[2]})"
    elif len(sys.argv) == 2 and not sys.argv[1].startswith("--"):
        errs = validate(sys.argv[1])
        label = sys.argv[1]
    else:
        print("usage: python3 validate_feed.py <feed.json>")
        print("       python3 validate_feed.py --consistency <repo-root>")
        print("       python3 validate_feed.py --image-reuse <repo-root>")
        print("       python3 validate_feed.py --listen-ready <pointer.json>")
        sys.exit(2)

    if errs:
        print(f"❌ REJECTED ({label}) — do not publish:")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)
    print(f"✅ valid ({label}) — safe to publish.")
    sys.exit(0)
