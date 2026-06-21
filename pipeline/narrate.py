#!/usr/bin/env python3
"""
Signals Listen — narration generator (FIRST STEP: non-publishing / draft-only).

Reads an existing edition (or draft) JSON, builds a calm CONVERSATIONAL Listen script per signal in
English and Japanese, synthesizes MP3s with ElevenLabs (locked winning voices), and writes:

  * audio  → scratch/narration_prod_spike/audio/<DATE>/signal-<NN>-<lang>.mp3
  * a DRAFT json copy → scratch/narration_prod_spike/editions/<DATE>.with-audio.json
        with signal.audioURL / signal.localized.ja.audioURL populated (and optional listenScript).

SAFETY: this never touches editions/, latest.json, build.py, the workflow, or git. It only writes
under the gitignored scratch/ tree. It is best-effort: any narration failure leaves that signal's
audioURL empty and the run still completes. It never narrates raw article fields directly — it uses
the approved conversational script (a prepared script file if present, else an LLM rewrite).

Script source per (signal, language), first that works:
  1) scratch/listen_scripts/signal<N>-<lang>.txt      (approved hand-written spike scripts)
  2) an LLM rewrite of the fields into the calm spoken style   (needs ANTHROPIC_API_KEY)
  3) none → that clip is skipped (audioURL stays empty)

Stdlib only. Requires ELEVENLABS_API_KEY to actually synthesize (missing key → empty audioURLs, no
hard failure). Run from the signals-feed repo root.

Usage:
  python3 pipeline/narrate.py --edition editions/2026-06-17.json
  python3 pipeline/narrate.py --edition editions/2026-06-17.json --signal 1
"""
import os, sys, json, argparse, datetime, urllib.request, urllib.error

OUT_ROOT = os.path.join("scratch", "narration_prod_spike")
SCRIPT_DIR = os.path.join("scratch", "listen_scripts")
BASE_URL = "https://signals-feed.vercel.app"

# Locked winning voices + settings (from the narration spike).
EN_VOICE, EN_SETTINGS = "DXFkLCBUTmvXpp2QwZjA", {"stability": 0.40, "similarity_boost": 0.85, "style": 0.12}
JA_VOICE, JA_SETTINGS = "HrSkFxfPhljjtQifnw1n", {"stability": 0.45, "similarity_boost": 0.85, "style": 0.10}
EL_MODEL = "eleven_multilingual_v2"
EL_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# A pinned, known-good model id. The "-latest" aliases 404 on some accounts; override via env if needed.
SCRIPT_MODEL = os.environ.get("SIGNALS_SCRIPT_MODEL", "claude-3-5-sonnet-20241022")

SCRIPT_SYSTEM = (
    "You write the spoken script for 'Signals Listen', a calm morning-ritual app. Rewrite the given "
    "story fields into a short, warm, CONVERSATIONAL narration that a thoughtful person would say "
    "aloud. Rules: warm and calm; simple sentences; natural pauses with commas and periods; NO "
    "headings (never say 'Key points' or 'Why it matters'); NO 'Signal N' prefix; NO news-anchor, "
    "corporate, dramatic, or movie-trailer phrasing; NO short dramatic sentence fragments; grounded "
    "ONLY in the provided fields — do not invent facts, names, or numbers, and do not omit the key "
    "point; about 55 to 70 seconds when spoken (~130-160 words for English, ~320-400 characters for "
    "Japanese). For Japanese, write natural spoken Japanese, not a stiff translation. Output ONLY the "
    "narration text — no quotes, no labels, no preamble."
)


# ── script sources ────────────────────────────────────────────────────────────────────────────────
def prepared_script_path(signal_no, lang, script_dir=SCRIPT_DIR):
    return os.path.join(script_dir, f"signal{signal_no}-{lang}.txt")


def load_prepared_script(signal_no, lang, script_dir=SCRIPT_DIR):
    p = prepared_script_path(signal_no, lang, script_dir)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
            return t or None
    return None


def fields_for(signal, lang):
    src = (signal.get("localized") or {}).get("ja", {}) if lang == "ja" else signal
    return {k: src.get(k) for k in ("headline", "summary", "keyTakeaways", "whyItMatters")
            if src.get(k)}


def llm_script(signal, lang, *, api_key, model=SCRIPT_MODEL):
    """LLM rewrite of the fields into the calm spoken style. Returns text or None (never raises)."""
    if not api_key:
        return None
    fields = fields_for(signal, lang)
    if not fields.get("headline"):
        return None
    user = (f"Language: {'Japanese' if lang == 'ja' else 'English'}.\n"
            f"Story fields (JSON):\n{json.dumps(fields, ensure_ascii=False, indent=2)}")
    body = json.dumps({"model": model, "max_tokens": 700, "temperature": 0.6,
                       "system": SCRIPT_SYSTEM,
                       "messages": [{"role": "user", "content": user}]}).encode("utf-8")
    req = urllib.request.Request(ANTHROPIC_URL, data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text").strip()
        return text or None
    except urllib.error.HTTPError as e:
        # Surface the API error body (e.g. an unknown model_id) so failures are diagnosable.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        print(f"    · LLM script ({lang}) failed: HTTP {e.code} (model={model}) {detail}")
        return None
    except Exception as e:
        print(f"    · LLM script ({lang}) failed: {e}")
        return None


def build_listen_script(signal, lang, *, anthropic_key, script_dir=SCRIPT_DIR, use_llm=True):
    """Approved conversational script for one (signal, language). prepared file → LLM → None.
    NEVER returns raw article fields concatenated."""
    prepared = load_prepared_script(signal.get("number"), lang, script_dir)
    if prepared:
        return prepared, "prepared-file"
    if use_llm:
        text = llm_script(signal, lang, api_key=anthropic_key)
        if text:
            return text, "llm"
    return None, "none"


# ── ElevenLabs synthesis (injectable for tests) ─────────────────────────────────────────────────────
def synth_elevenlabs(text, *, voice, settings, api_key, model=EL_MODEL):
    body = json.dumps({"text": text, "model_id": model,
                       "voice_settings": {**settings, "use_speaker_boost": True}}).encode("utf-8")
    req = urllib.request.Request(EL_URL.format(voice=voice), data=body, method="POST", headers={
        "xi-api-key": api_key, "accept": "audio/mpeg", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def audio_rel_path(date, number, lang):
    return f"audio/{date}/signal-{int(number):02d}-{lang}.mp3"


# ── core (testable) ─────────────────────────────────────────────────────────────────────────────────
def narrate_edition(feed, *, date, out_root=OUT_ROOT, base_url=BASE_URL, el_key=None,
                    anthropic_key=None, store_script=True, only_signal=None,
                    synth_fn=synth_elevenlabs, script_dir=SCRIPT_DIR, use_llm=True, log=print):
    """Return (feed_copy, stats). Best-effort: failures leave audioURL empty; never raises for a clip.
    Writes mp3s under out_root/<audio_rel_path>; sets audioURL to base_url + that path."""
    feed = json.loads(json.dumps(feed))   # deep copy — never mutate the caller's edition
    stats = {"clips": 0, "skipped_existing": 0, "failed": 0, "no_script": 0}

    for sig in feed.get("signals", []):
        if only_signal is not None and sig.get("number") != only_signal:
            continue
        langs = [("en", EN_VOICE, EN_SETTINGS)]
        if (sig.get("localized") or {}).get("ja"):
            langs.append(("ja", JA_VOICE, JA_SETTINGS))

        for lang, voice, settings in langs:
            script, src = build_listen_script(sig, lang, anthropic_key=anthropic_key,
                                              script_dir=script_dir, use_llm=use_llm)
            if not script:
                stats["no_script"] += 1
                log(f"  #{sig.get('number')} {lang}: no script ({src}) — audioURL left empty")
                continue
            if store_script:
                if lang == "ja":
                    sig.setdefault("localized", {}).setdefault("ja", {})["listenScript"] = script
                else:
                    sig["listenScript"] = script

            rel = audio_rel_path(date, sig["number"], lang)
            abspath = os.path.join(out_root, rel)
            os.makedirs(os.path.dirname(abspath), exist_ok=True)
            url = f"{base_url}/{rel}"

            if os.path.exists(abspath) and os.path.getsize(abspath) > 0:
                _set_audio_url(sig, lang, url)
                stats["skipped_existing"] += 1
                log(f"  #{sig.get('number')} {lang}: reuse existing clip ({src} script) → {rel}")
                continue
            if not el_key:
                stats["failed"] += 1
                log(f"  #{sig.get('number')} {lang}: no ELEVENLABS_API_KEY — audioURL left empty")
                continue
            try:
                audio = synth_fn(script, voice=voice, settings=settings, api_key=el_key)
                with open(abspath, "wb") as f:
                    f.write(audio)
                _set_audio_url(sig, lang, url)
                stats["clips"] += 1
                log(f"  ✓ #{sig.get('number')} {lang}: {len(audio):,} bytes ({src} script) → {rel}")
            except Exception as e:
                stats["failed"] += 1
                log(f"  ✗ #{sig.get('number')} {lang}: synth failed ({e}) — audioURL left empty")
    return feed, stats


def _set_audio_url(signal, lang, url):
    if lang == "ja":
        signal.setdefault("localized", {}).setdefault("ja", {})["audioURL"] = url
    else:
        signal["audioURL"] = url


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Signals Listen narration — draft-only (non-publishing).")
    ap.add_argument("--edition", required=True, help="path to an edition/draft JSON")
    ap.add_argument("--date", default=None, help="override DATE (default: feed.date or filename)")
    ap.add_argument("--signal", type=int, default=None, help="limit to one signal number (testing)")
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--out-root", default=OUT_ROOT)
    ap.add_argument("--no-store-script", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="only use prepared script files (no Anthropic)")
    args = ap.parse_args()

    if not os.path.exists(args.edition):
        sys.exit(f"❌ edition not found: {args.edition} (run from the signals-feed repo root)")
    feed = json.load(open(args.edition))
    date = args.date or feed.get("date") or os.path.splitext(os.path.basename(args.edition))[0]

    el_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    an_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    print(f"=== narrate (draft-only) · {args.edition} · date={date} ===")
    if not el_key:
        print("⚠ ELEVENLABS_API_KEY not set — scripts/wiring still produced, but no audio (audioURL empty).")

    feed, stats = narrate_edition(feed, date=date, out_root=args.out_root, base_url=args.base_url,
                                  el_key=el_key, anthropic_key=an_key,
                                  store_script=not args.no_store_script, only_signal=args.signal,
                                  use_llm=not args.no_llm)

    out_json = os.path.join(args.out_root, "editions", f"{date}.with-audio.json")
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print(f"\n  clips={stats['clips']} reused={stats['skipped_existing']} "
          f"failed/empty={stats['failed']} no-script={stats['no_script']}")
    print(f"  draft JSON → {out_json}")
    print(f"  audio dir  → {os.path.join(args.out_root, 'audio', date)}/")
    print("  (Draft only — editions/, latest.json, build.py, workflow, and git are untouched.)")


if __name__ == "__main__":
    main()
