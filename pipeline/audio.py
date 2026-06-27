#!/usr/bin/env python3
"""
Audio URL injection for the publish step (Japanese only, for now).

publish.py calls `inject_ja_audio(feed, date, ...)` just before it writes editions/<date>.json +
latest.json. For each signal that HAS a `localized.ja` block, if the manifest lists a known R2 audio
key for that (date, signal number), we set `localized.ja.audioURL` to the public R2 URL.

Design rules (match the agreed plan):
  * Japanese ONLY. English `audioURL` and all article text are left untouched.
  * PRESERVE: an existing non-empty `localized.ja.audioURL` is never overwritten.
  * INJECT: only when the field is empty/missing AND the manifest has a key for it.
  * FALLBACK: no manifest entry → leave audioURL empty/missing → iOS uses on-device TTS.
  * Best-effort: a missing/garbled manifest or any error never raises (publish must not break).
  * No network: we do NOT verify the file exists on R2 (only list real files in the manifest).

The manifest is the single source of truth for "known/available" audio. Keys are R2 object keys
under the public base; 2026-06-25 keeps the original root-level keys, new dates use
`audio/YYYY-MM-DD/signal-0N-ja.mp3`.
"""
import os, json

R2_BASE = "https://pub-95a7558772874b48a645ad0c1604d784.r2.dev"
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_manifest.json")


def load_manifest(path=MANIFEST_PATH):
    """Return the {date: {signal_no(str): {lang: key}}} map. Missing/invalid → {} (never raises)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        editions = data.get("editions", {})
        return editions if isinstance(editions, dict) else {}
    except Exception:
        return {}


def _audio_url(key, base_url=R2_BASE):
    """Build a public URL from an R2 object key. Pass-through if a full URL is already given."""
    key = (key or "").strip()
    if not key:
        return None
    if key.startswith("http://") or key.startswith("https://"):
        return key
    return f"{base_url.rstrip('/')}/{key.lstrip('/')}"


def inject_ja_audio(feed, date, manifest=None, base_url=R2_BASE):
    """Set localized.ja.audioURL from the manifest where appropriate. Mutates + returns `feed`.
    Returns (feed, stats). Never raises — on any problem it leaves the feed unchanged."""
    stats = {"injected": 0, "preserved": 0, "no_manifest": 0, "no_ja": 0}
    try:
        if manifest is None:
            manifest = load_manifest()
        per_date = manifest.get(date, {}) if isinstance(manifest, dict) else {}

        for sig in feed.get("signals", []):
            ja = (sig.get("localized") or {}).get("ja")
            if not isinstance(ja, dict):
                stats["no_ja"] += 1                      # no JA block → never add audio (→ TTS)
                continue

            existing = ja.get("audioURL")
            if isinstance(existing, str) and existing.strip():
                stats["preserved"] += 1                  # keep a correction/manual URL untouched
                continue

            entry = per_date.get(str(sig.get("number")), {}) if isinstance(per_date, dict) else {}
            url = _audio_url(entry.get("ja"), base_url) if isinstance(entry, dict) else None
            if url:
                ja["audioURL"] = url
                stats["injected"] += 1
            else:
                stats["no_manifest"] += 1                # no known audio → leave empty (→ TTS)
    except Exception:
        # Best-effort: never let audio wiring break a publish.
        return feed, stats
    return feed, stats
