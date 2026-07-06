#!/usr/bin/env python3
"""
Listen automation — generate EN dialogue (audio + captions) for ALL five signals of one edition,
upload to R2 (date-scoped), and write the listen_manifest.json entry. Edition/​latest injection is a
separate step (listen_inject_edition.py), so this script never edits the feed if anything fails.

Per signal: LLM writes a calm two-speaker dialogue grounded ONLY in that signal's article fields →
ElevenLabs synthesizes each line with two voices → lines are concatenated directly (no gaps → gap 0.0)
→ ffprobe measures each line + the final clip → drift gate (|sum − final| ≤ 0.25s).

ATOMIC / FAIL-CLOSED: every signal must succeed (scripts, audio, drift, upload, HTTP 200) before the
manifest is written. Any failure raises → no manifest change → no listen.en is ever injected. So the
text feed (published separately) is never blocked, and mismatched/partial Listen data can't ship.

Audio is written under scratch/ (gitignored) and pushed to R2 remote-only — never committed to git.

Env: ELEVENLABS_API_KEY, ANTHROPIC_API_KEY (required); EXPLAINER_VOICE, LISTENER_VOICE;
     SIGNALS_LISTEN_MODEL (optional). R2 upload uses `wrangler` (CLOUDFLARE_API_TOKEN in CI).

Usage: python3 pipeline/listen_generate.py 2026-06-30
"""
import os, sys, json, subprocess, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(HERE, "listen_manifest.json")
OUT_BASE = os.path.join(ROOT, "scratch", "listen_audio_test", "dialogue_proto")   # gitignored

R2_BASE = "https://pub-95a7558772874b48a645ad0c1604d784.r2.dev"
R2_BUCKET = "signals-audio"
# Caption-vs-audio drift gate. MP3 duration probing (ffprobe) and direct MP3 concatenation
# both round at frame/container granularity, so a few hundredths of a second of drift is
# normal encoder noise, not a content mismatch (a real mismatch — wrong/missing line —
# drifts by whole seconds). 0.50s stays strict enough to catch those while not aborting
# runs over rounding (seen: 0.260s on a perfectly good clip).
DRIFT_THRESHOLD = 0.50

EL_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
EL_MODEL = "eleven_multilingual_v2"
# .strip() + `or`: a secret pasted with a trailing newline/space, or set-but-empty, must
# never reach the URL — whitespace in EL_URL.format(voice=…) makes http.client reject the
# request at putrequest with a cryptic traceback before any network call.
EXPLAINER_VOICE = os.environ.get("EXPLAINER_VOICE", "").strip() or "DXFkLCBUTmvXpp2QwZjA"
LISTENER_VOICE = os.environ.get("LISTENER_VOICE", "").strip()
EXPLAINER_SETTINGS = {"stability": 0.50, "similarity_boost": 0.85, "style": 0.12}
LISTENER_SETTINGS = {"stability": 0.38, "similarity_boost": 0.85, "style": 0.12}

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# `or` (not the .get default) so an EMPTY env var — e.g. the workflow passing an unset
# repo secret — still falls back instead of calling the API with model="".
SCRIPT_MODEL = os.environ.get("SIGNALS_LISTEN_MODEL") or "claude-3-5-sonnet-20241022"
SCRIPT_SYSTEM = (
    "You write a short, calm two-person Listen dialogue for a morning news app. Two speakers: "
    "'listener' (curious, asks what a regular person would ask) and 'explainer' (calm, concise, "
    "informed). Rules: 8–12 short spoken lines; warm and quiet, never dramatic or comedic; ground "
    "EVERY line ONLY in the provided story fields — invent no facts, names, or numbers; handle "
    "sensitive topics (violence, death) soberly and without graphic detail; no headings, no 'Signal "
    "N'. Output ONLY a JSON array of objects: [{\"speaker\":\"listener|explainer\",\"text\":\"...\"}]."
)


# ── external calls (injectable for tests) ───────────────────────────────────────────────────────
def llm_dialogue(signal, *, api_key, model=SCRIPT_MODEL):
    fields = {k: signal.get(k) for k in ("headline", "summary", "keyTakeaways", "whyItMatters") if signal.get(k)}
    body = json.dumps({"model": model, "max_tokens": 900, "temperature": 0.5, "system": SCRIPT_SYSTEM,
                       "messages": [{"role": "user", "content": json.dumps(fields, ensure_ascii=False)}]}).encode()
    req = urllib.request.Request(ANTHROPIC_URL, data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode())
    text = "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text").strip()
    return parse_dialogue(text)


def synth_line(text, voice, settings, api_key):
    body = json.dumps({"text": text, "model_id": EL_MODEL,
                       "voice_settings": {**settings, "use_speaker_boost": True}}).encode()
    req = urllib.request.Request(EL_URL.format(voice=voice), data=body, method="POST", headers={
        "xi-api-key": api_key, "accept": "audio/mpeg", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def ffprobe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nokey=1:noprint_wrappers=1", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return round(float(out), 6)


def r2_upload(local_path, key):
    subprocess.run(["wrangler", "r2", "object", "put", f"{R2_BUCKET}/{key}",
                    "--file", local_path, "--content-type", "audio/mpeg", "--remote"], check=True)


# Cloudflare fronts r2.dev and can 403 "bot-looking" requests (python-urllib UA) while
# serving browsers fine — send a browser-ish UA on verification requests only.
_VERIFY_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Signals-ListenVerify/1.0"


def _status(url, method="GET", headers=None, read_byte=False):
    h = {"User-Agent": _VERIFY_UA, **(headers or {})}
    req = urllib.request.Request(url, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if read_byte:
                r.read(1)          # urllib streams lazily — 1 byte proves the body serves
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def verify_public(url):
    """Is the uploaded object publicly retrievable?

    R2 public (r2.dev) endpoints can 403 both HEAD and Range GETs while serving a plain
    browser GET perfectly (browser-confirmed). Ladder, cheapest first:
      1. HEAD                     → 200 = success
      2. GET with Range: bytes=0-0 → 200/206 = success
      3. plain GET, read 1 byte    → 200 = success (urllib streams lazily, so reading a
         single byte verifies the body without downloading the whole MP3)
    Fail-closed if all three fail.

    Returns (ok: bool, detail: str) — detail carries every attempted status for the logs,
    e.g. "HEAD 403 → GET/range 403 → GET 200".
    """
    head = _status(url, method="HEAD")
    if head == 200:
        return True, "HEAD 200"
    ranged = _status(url, method="GET", headers={"Range": "bytes=0-0"})
    if ranged in (200, 206):
        return True, f"HEAD {head} → GET/range {ranged}"
    plain = _status(url, method="GET", read_byte=True)
    ok = plain == 200
    return ok, f"HEAD {head} → GET/range {ranged} → GET {plain}"


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────
def parse_dialogue(text):
    """Parse the LLM output into a validated [{speaker,text}] list, or raise."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1].lstrip("json").strip() if "```" in s else s
    start, end = s.find("["), s.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("no JSON array in LLM output")
    arr = json.loads(s[start:end + 1])
    out = []
    for c in arr:
        sp = str(c.get("speaker", "")).lower().strip()
        tx = str(c.get("text", "")).strip()
        if sp not in ("listener", "explainer") or not tx:
            raise ValueError(f"bad caption: {c!r}")
        out.append({"speaker": sp, "text": tx})
    if not (6 <= len(out) <= 14):
        raise ValueError(f"unexpected line count {len(out)}")
    return out


def key_for(date, number):
    return f"audio/{date}/signal-{int(number):02d}-dialogue-en.mp3"


# ── orchestration ───────────────────────────────────────────────────────────────────────────────
def generate(date, *, el_key, an_key, listener_voice, explainer_voice,
             llm_fn=llm_dialogue, synth_fn=synth_line, dur_fn=ffprobe_duration,
             upload_fn=r2_upload, verify_fn=verify_public, log=print):
    """Generate+upload all 5 dialogue clips for `date`, then write the manifest. Raises on any failure
    BEFORE writing the manifest (atomic). Returns the manifest entry dict."""
    edition = os.path.join(ROOT, "editions", f"{date}.json")
    feed = json.load(open(edition, encoding="utf-8"))
    if feed.get("date") != date:
        raise ValueError(f"{edition} internal date {feed.get('date')!r} != {date}")
    signals = feed.get("signals", [])
    if len(signals) != 5:
        raise ValueError(f"expected 5 signals, found {len(signals)}")

    outdir = os.path.join(OUT_BASE, date)
    os.makedirs(outdir, exist_ok=True)
    entry, to_upload = {}, []

    # 1) scripts + audio + drift — all must pass before any upload.
    for sig in signals:
        num = sig["number"]
        lines = llm_fn(sig, api_key=an_key)
        parts, durs = [], []
        for i, c in enumerate(lines, 1):
            voice = explainer_voice if c["speaker"] == "explainer" else listener_voice
            settings = EXPLAINER_SETTINGS if c["speaker"] == "explainer" else LISTENER_SETTINGS
            p = os.path.join(outdir, f"sig{num}-line-{i:02d}.mp3")
            open(p, "wb").write(synth_fn(c["text"], voice, settings, el_key))
            durs.append(dur_fn(p))
            parts.append(p)
        final = os.path.join(outdir, f"signal-{int(num):02d}-dialogue-en.mp3")
        with open(final, "wb") as o:
            for p in parts:
                o.write(open(p, "rb").read())
        drift = abs(dur_fn(final) - sum(durs))
        if drift > DRIFT_THRESHOLD:
            raise ValueError(f"signal {num} drift {drift:.3f}s > {DRIFT_THRESHOLD}s — aborting")
        caps = [{"speaker": c["speaker"], "text": c["text"], "duration": round(d, 6)}
                for c, d in zip(lines, durs)]
        entry[str(num)] = {"format": "dialogue",
                           "en": {"key": key_for(date, num), "gap": 0.0, "captions": caps}}
        to_upload.append((final, key_for(date, num)))
        log(f"  signal {num}: {len(caps)} lines, drift {drift:.3f}s OK")

    # 2) upload all, then require every object to be publicly retrievable (fail-closed).
    #    HEAD 200, or GET/range 200/206 when HEAD is refused (r2.dev quirk) — see verify_public.
    for final, key in to_upload:
        upload_fn(final, key)
    for _, key in to_upload:
        url = f"{R2_BASE}/{key}"
        ok, detail = verify_fn(url)
        if not ok:
            raise RuntimeError(f"R2 object not publicly retrievable ({detail}): {url} — "
                               "aborting, manifest unchanged")
        log(f"  OK ({detail}) {url}")

    # 3) only now write the manifest entry.
    data = json.load(open(MANIFEST, encoding="utf-8"))
    data.setdefault("editions", {})[date] = entry
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log(f"✓ wrote listen_manifest.json entry for {date} (5 signals)")
    return entry


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python3 pipeline/listen_generate.py <YYYY-MM-DD>")
    date = sys.argv[1]
    el = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    an = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not el or not an:
        sys.exit("❌ ELEVENLABS_API_KEY and ANTHROPIC_API_KEY are required")
    if not LISTENER_VOICE:
        sys.exit("❌ LISTENER_VOICE required (EXPLAINER_VOICE defaults to %s)" % EXPLAINER_VOICE)
    # Fail with a READABLE message on malformed voice IDs (ElevenLabs IDs are alphanumeric).
    # Without this, a bad character reaches the request URL and http.client dies in
    # putrequest with an opaque traceback.
    for name, v in (("LISTENER_VOICE", LISTENER_VOICE), ("EXPLAINER_VOICE", EXPLAINER_VOICE)):
        if not v.isalnum():
            sys.exit(f"❌ {name} is not a valid ElevenLabs voice ID ({v!r}) — "
                     "re-paste the repo secret without spaces/newlines/quotes")
    generate(date, el_key=el, an_key=an, listener_voice=LISTENER_VOICE, explainer_voice=EXPLAINER_VOICE)
    print("Next: python3 pipeline/listen_inject_edition.py", date)


if __name__ == "__main__":
    main()
