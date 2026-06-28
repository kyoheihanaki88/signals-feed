#!/usr/bin/env python3
"""
Conversational ("dialogue") Listen injection for the publish step (Phase 2).

publish.py calls `inject_listen(feed, date, ...)` after the JA single-voice audio step and before it
writes editions/<date>.json + latest.json. For each signal that has a manifest entry for this
(date, signal number), it builds the optional top-level `signal.listen` block:

    "listen": { "format": "dialogue",
                "en": { "audioURL": "...", "gap": 0.0,
                        "captions": [ { "speaker": "...", "text": "...", "duration": 2.17 } ] } }

Design rules (match LISTEN_DIALOGUE_SCHEMA.md + Phase-1 validation):
  * OPTIONAL: no manifest entry → no `listen` injected → feed unchanged.
  * SEPARATE from JA audio: this never touches `audioURL`, `localized.ja.audioURL`, or article text.
  * PRESERVE: a signal that already has a `listen` block is left untouched.
  * SAFE: an incomplete/malformed track (missing audioURL, empty/invalid captions) is SKIPPED — we
    only ever emit tracks that pass Phase-1 validation (non-empty speaker/text, duration > 0,
    gap >= 0). Any error is swallowed so a publish never breaks.
  * No network: we do NOT verify the file exists on R2 (only list real files in the manifest).

This is a separate module + manifest from audio.py on purpose, so the existing JA audio injection is
provably unchanged.
"""
import os, json

R2_BASE = "https://pub-95a7558772874b48a645ad0c1604d784.r2.dev"
LISTEN_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "listen_manifest.json")


def load_listen_manifest(path=LISTEN_MANIFEST_PATH):
    """Return {date: {signal_no(str): {format?, <lang>: {...}}}}. Missing/invalid → {} (never raises)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        editions = data.get("editions", {})
        return editions if isinstance(editions, dict) else {}
    except Exception:
        return {}


def _audio_url(track, base_url):
    """Resolve a track's audio URL from an explicit `audioURL` or an R2 object `key`."""
    url = track.get("audioURL")
    if isinstance(url, str) and url.strip():
        return url.strip()
    key = track.get("key")
    if isinstance(key, str) and key.strip():
        k = key.strip()
        if k.startswith("http://") or k.startswith("https://"):
            return k
        return f"{base_url.rstrip('/')}/{k.lstrip('/')}"
    return None


def _valid_captions(raw):
    """Return the caption list only if it is non-empty and EVERY caption is well-formed; else None.
    (All-or-nothing per track so we never ship partial/desynced dialogue.)"""
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for c in raw:
        if not isinstance(c, dict):
            return None
        speaker, text, dur = c.get("speaker"), c.get("text"), c.get("duration")
        if not (isinstance(speaker, str) and speaker.strip()):
            return None
        if not (isinstance(text, str) and text.strip()):
            return None
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or dur <= 0:
            return None
        out.append({"speaker": speaker, "text": text, "duration": dur})
    return out


def _build_track(track, base_url):
    """Build one validated language track {audioURL, gap, captions}, or None if incomplete/malformed."""
    if not isinstance(track, dict):
        return None
    url = _audio_url(track, base_url)
    caps = _valid_captions(track.get("captions"))
    if not url or caps is None:
        return None                                     # incomplete → skip (best-effort)
    gap = track.get("gap", 0.0)
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or gap < 0:
        gap = 0.0
    return {"audioURL": url, "gap": gap, "captions": caps}


def _build_listen_block(entry, base_url):
    """Build the `listen` object from a manifest entry, or None if no valid language track."""
    if not isinstance(entry, dict):
        return None
    block = {}
    fmt = entry.get("format")
    block["format"] = fmt if isinstance(fmt, str) and fmt.strip() else "dialogue"
    for lang, track in entry.items():
        if lang == "format" or not isinstance(track, dict):
            continue                                    # skip non-track keys / tolerate extras
        built = _build_track(track, base_url)
        if built is not None:
            block[lang] = built
    # Only return if at least one real language track survived.
    return block if any(k != "format" for k in block) else None


def inject_listen(feed, date, manifest=None, base_url=R2_BASE):
    """Inject `signal.listen` from the dialogue manifest where present. Mutates + returns `feed`.
    Returns (feed, stats). Never raises — on any problem it leaves the feed unchanged."""
    stats = {"injected": 0, "preserved": 0, "skipped": 0, "no_entry": 0}
    try:
        if manifest is None:
            manifest = load_listen_manifest()
        per_date = manifest.get(date, {}) if isinstance(manifest, dict) else {}

        for sig in feed.get("signals", []):
            if isinstance(sig.get("listen"), dict):
                stats["preserved"] += 1                  # never overwrite an existing block
                continue
            entry = per_date.get(str(sig.get("number"))) if isinstance(per_date, dict) else None
            if entry is None:
                stats["no_entry"] += 1                    # no dialogue for this signal → unchanged
                continue
            block = _build_listen_block(entry, base_url)
            if block:
                sig["listen"] = block
                stats["injected"] += 1
            else:
                stats["skipped"] += 1                     # malformed/incomplete → skip, no listen
    except Exception:
        return feed, stats
    return feed, stats
