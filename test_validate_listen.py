#!/usr/bin/env python3
"""
Tests for the optional `signal.listen` validation in validate_feed.py (Phase 1).
Stdlib only. Run: python3 test_validate_listen.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate_feed as V  # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0


def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


def sig(listen="__absent__"):
    s = {"number": 1}
    if listen != "__absent__":
        s["listen"] = listen
    return s


VALID_TRACK = {
    "audioURL": "https://pub.example.r2.dev/audio/2026-06-25/signal-01-dialogue-en.mp3",
    "gap": 0.0,
    "captions": [
        {"speaker": "listener",  "text": "So there's real news on Iran this morning?", "duration": 2.168163},
        {"speaker": "explainer", "text": "Yeah. The U.S. and Iran reached an early peace deal.", "duration": 3.004082},
    ],
}


# ── 1) no `listen` → valid ──
print("1) no listen:")
check("absent listen → no errors", V.listen_errors(sig()) == [])
check("structural ignores absent listen", "listen" not in str(V.listen_errors(sig())))

# ── 2) valid dialogue listen → valid ──
print("\n2) valid dialogue listen:")
ok = {"format": "dialogue", "en": VALID_TRACK, "ja": {"audioURL": "https://x/y.mp3", "gap": 0.0, "captions": []}}
check("valid listen → no errors", V.listen_errors(sig(ok)) == [], str(V.listen_errors(sig(ok))))
check("empty captions list allowed", V.listen_errors(sig({"ja": {"captions": []}})) == [])
check("track with only audioURL allowed", V.listen_errors(sig({"en": {"audioURL": "https://x/y.mp3"}})) == [])

# ── 3) malformed listen object ──
print("\n3) malformed listen object:")
check("listen as string → error", V.listen_errors(sig("nope")) == ["signal 1 `listen` must be an object"])
check("known lang as string → error", any("listen.en must be an object" in e for e in V.listen_errors(sig({"en": "x"}))))
check("format non-string → error", any("listen.format must be a string" in e for e in V.listen_errors(sig({"format": 3}))))

# ── 4) invalid caption duration ──
print("\n4) invalid caption duration:")
bad_dur = {"en": {"captions": [{"speaker": "explainer", "text": "hi", "duration": 0}]}}
check("duration 0 → error", any("duration must be a number > 0" in e for e in V.listen_errors(sig(bad_dur))))
neg = {"en": {"captions": [{"speaker": "explainer", "text": "hi", "duration": -1.0}]}}
check("negative duration → error", any("duration must be a number > 0" in e for e in V.listen_errors(sig(neg))))
missing = {"en": {"captions": [{"speaker": "explainer", "text": "hi"}]}}
check("missing duration → error", any("duration must be a number > 0" in e for e in V.listen_errors(sig(missing))))
boolean = {"en": {"captions": [{"speaker": "explainer", "text": "hi", "duration": True}]}}
check("bool duration → error", any("duration must be a number > 0" in e for e in V.listen_errors(sig(boolean))))

# ── 5) missing caption text ──
print("\n5) missing / empty caption text:")
no_text = {"en": {"captions": [{"speaker": "explainer", "duration": 2.0}]}}
check("missing text → error", any("text must be a non-empty string" in e for e in V.listen_errors(sig(no_text))))
empty_text = {"en": {"captions": [{"speaker": "explainer", "text": "   ", "duration": 2.0}]}}
check("blank text → error", any("text must be a non-empty string" in e for e in V.listen_errors(sig(empty_text))))
no_speaker = {"en": {"captions": [{"text": "hi", "duration": 2.0}]}}
check("missing speaker → error", any("speaker must be a non-empty string" in e for e in V.listen_errors(sig(no_speaker))))

# ── 6) unknown language key tolerated ──
print("\n6) unknown language key + extras tolerated:")
unknown_lang = {"format": "dialogue", "fr": VALID_TRACK}        # unknown but well-formed track
check("unknown well-formed lang → no errors", V.listen_errors(sig(unknown_lang)) == [], str(V.listen_errors(sig(unknown_lang))))
extra_scalar = {"en": VALID_TRACK, "version": 2, "note": "x"}    # unknown non-dict extras
check("unknown scalar extras tolerated", V.listen_errors(sig(extra_scalar)) == [], str(V.listen_errors(sig(extra_scalar))))
extra_in_caption = {"en": {"captions": [{"speaker": "explainer", "text": "hi", "duration": 2.0, "startMs": 0}]}}
check("unknown caption field tolerated", V.listen_errors(sig(extra_in_caption)) == [])

# ── extra shape checks ──
print("\n+) gap / captions shape:")
check("negative gap → error", any("gap must be a number >= 0" in e for e in V.listen_errors(sig({"en": {"gap": -0.1}}))))
check("gap 0 allowed", V.listen_errors(sig({"en": {"gap": 0}})) == [])
check("captions not a list → error", any("captions must be a list" in e for e in V.listen_errors(sig({"en": {"captions": "x"}}))))
check("empty audioURL → error", any("audioURL must be a non-empty string" in e for e in V.listen_errors(sig({"en": {"audioURL": ""}}))))

# ── full-feed integration: a valid 5-signal feed WITH listen on signal 1 passes structural_errors ──
print("\n*) full feed integration:")
def full_signal(n):
    return {"number": n, "importance": n, "lead": n == 1, "category": "WORLD", "source": "X",
            "headline": f"H{n}", "summary": f"S{n}.", "keyTakeaways": [f"k{n}"], "whyItMatters": f"W{n}.",
            "originalURL": f"https://example.com/a{n}", "readTime": 3, "imageURL": "https://img/x.jpg",
            "placeTime": "p", "audioURL": ""}
feed = {"date": "2026-06-25", "focus": "MIXED", "version": 1, "signals": [full_signal(n) for n in range(1, 6)]}
check("valid feed, no listen → structural clean", V.structural_errors(feed) == [], str(V.structural_errors(feed)))
feed["signals"][0]["listen"] = {"format": "dialogue", "en": VALID_TRACK}
check("valid feed + valid listen → structural clean", V.structural_errors(feed) == [], str(V.structural_errors(feed)))
feed["signals"][0]["listen"] = {"en": {"captions": [{"speaker": "x", "text": "y", "duration": 0}]}}
check("valid feed + bad listen → structural flags it", any("duration must be a number > 0" in e for e in V.structural_errors(feed)))

print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
