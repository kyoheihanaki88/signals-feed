#!/usr/bin/env python3
"""
Tests for pipeline/audio.py (JA audioURL injection). Stdlib only, no network.
Run: python3 pipeline/test_audio.py
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audio  # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0


def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


BASE = "https://pub-95a7558772874b48a645ad0c1604d784.r2.dev"


def feed_with(signals):
    return {"date": "2026-06-25", "focus": "MIXED", "version": 1, "signals": signals}


def sig(n, *, ja=True, ja_audio=None, en_audio=""):
    s = {"number": n, "headline": f"H{n}", "summary": f"S{n}.", "keyTakeaways": [f"k{n}"],
         "whyItMatters": f"W{n}.", "audioURL": en_audio}
    if ja:
        block = {"headline": f"見出し{n}", "summary": f"要約{n}。", "whyItMatters": f"重要{n}。"}
        if ja_audio is not None:
            block["audioURL"] = ja_audio
        s["localized"] = {"ja": block}
    return s


MANIFEST = {
    "2026-06-25": {
        "1": {"ja": "signal-01-ja-v2.mp3"},
        "2": {"ja": "signal-02-ja.mp3"},
    },
    "2026-06-26": {
        "1": {"ja": "audio/2026-06-26/signal-01-ja.mp3"},
    },
}


# ── 1) inject: manifest entry → localized.ja.audioURL set to full R2 URL ──
print("1) inject from manifest:")
feed = feed_with([sig(1), sig(2)])
feed, st = audio.inject_ja_audio(feed, "2026-06-25", MANIFEST, BASE)
check("signal 1 JA url injected",
      feed["signals"][0]["localized"]["ja"]["audioURL"] == f"{BASE}/signal-01-ja-v2.mp3",
      feed["signals"][0]["localized"]["ja"].get("audioURL"))
check("signal 2 JA url injected",
      feed["signals"][1]["localized"]["ja"]["audioURL"] == f"{BASE}/signal-02-ja.mp3")
check("EN audioURL untouched (empty)", feed["signals"][0]["audioURL"] == "")
check("stats injected==2", st["injected"] == 2, str(st))

# date-scoped key for a future date builds the nested path
print("\n   date-scoped future key:")
f2 = feed_with([sig(1)])
f2, _ = audio.inject_ja_audio(f2, "2026-06-26", MANIFEST, BASE)
check("date-scoped url",
      f2["signals"][0]["localized"]["ja"]["audioURL"] == f"{BASE}/audio/2026-06-26/signal-01-ja.mp3",
      f2["signals"][0]["localized"]["ja"].get("audioURL"))


# ── 2) fallback: no manifest entry → audioURL stays empty/missing (→ iOS TTS) ──
print("\n2) fallback when no manifest entry:")
feed = feed_with([sig(3)])                       # #3 not in manifest
feed, st = audio.inject_ja_audio(feed, "2026-06-25", MANIFEST, BASE)
check("no audioURL key added", "audioURL" not in feed["signals"][0]["localized"]["ja"])
check("stats no_manifest==1", st["no_manifest"] == 1, str(st))

print("   no localized.ja at all → skipped:")
feed = feed_with([sig(1, ja=False)])
feed, st = audio.inject_ja_audio(feed, "2026-06-25", MANIFEST, BASE)
check("no localized block created", "localized" not in feed["signals"][0])
check("stats no_ja==1", st["no_ja"] == 1, str(st))

print("   unknown date → nothing injected:")
feed = feed_with([sig(1)])
feed, st = audio.inject_ja_audio(feed, "2099-01-01", MANIFEST, BASE)
check("unknown date leaves JA empty", "audioURL" not in feed["signals"][0]["localized"]["ja"])


# ── 3) preserve: existing non-empty JA audioURL is never overwritten ──
print("\n3) preserve existing JA audioURL:")
custom = "https://example.com/custom-ja.mp3"
feed = feed_with([sig(1, ja_audio=custom)])      # #1 has a manifest entry, but already set
feed, st = audio.inject_ja_audio(feed, "2026-06-25", MANIFEST, BASE)
check("existing url preserved", feed["signals"][0]["localized"]["ja"]["audioURL"] == custom)
check("stats preserved==1", st["preserved"] == 1, str(st))


# ── 4) byte-identical: latest.json and editions/<date>.json serialize identically ──
print("\n4) byte-identical output:")
feed = feed_with([sig(1), sig(2), sig(3)])
feed, _ = audio.inject_ja_audio(feed, "2026-06-25", MANIFEST, BASE)
text = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"   # exactly what publish.write_edition does
latest_bytes = text.encode("utf-8")
edition_bytes = text.encode("utf-8")
check("latest == edition (byte-identical)", latest_bytes == edition_bytes)
check("round-trips as valid JSON", json.loads(text)["signals"][0]["localized"]["ja"]["audioURL"]
      == f"{BASE}/signal-01-ja-v2.mp3")


# ── 5) robustness: missing/garbled manifest never raises ──
print("\n5) robustness:")
feed = feed_with([sig(1)])
feed, st = audio.inject_ja_audio(feed, "2026-06-25", {}, BASE)   # empty manifest
check("empty manifest → no inject, no raise", "audioURL" not in feed["signals"][0]["localized"]["ja"])
check("load_manifest on missing path → {}", audio.load_manifest("/no/such/file.json") == {})


print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
