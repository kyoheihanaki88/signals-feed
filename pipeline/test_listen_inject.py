#!/usr/bin/env python3
"""
Tests for pipeline/listen.py (conversational `listen` injection, Phase 2). Stdlib only, no network.
Run: python3 pipeline/test_listen_inject.py
"""
import os, sys, json
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import listen as L          # noqa: E402
import audio as A           # noqa: E402  (to confirm JA injection is unchanged)
import validate_feed as V   # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0


def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


BASE = "https://pub-95a7558772874b48a645ad0c1604d784.r2.dev"


def full_signal(n, *, ja=False):
    s = {"number": n, "importance": n, "lead": n == 1, "category": "WORLD", "source": "X",
         "headline": f"H{n}", "summary": f"S{n}.", "keyTakeaways": [f"k{n}"], "whyItMatters": f"W{n}.",
         "originalURL": f"https://example.com/a{n}", "readTime": 3, "imageURL": "https://img/x.jpg",
         "placeTime": "p", "audioURL": ""}
    if ja:
        s["localized"] = {"ja": {"headline": f"見出し{n}", "summary": f"要約{n}。", "whyItMatters": f"重要{n}。"}}
    return s


def feed5(ja=False):
    return {"date": "2026-06-25", "focus": "MIXED", "version": 1,
            "signals": [full_signal(n, ja=ja) for n in range(1, 6)]}


GOOD_ENTRY = {
    "format": "dialogue",
    "en": {
        "key": "signal-01-dialogue-en.mp3",
        "gap": 0.0,
        "captions": [
            {"speaker": "listener",  "text": "So there's real news on Iran this morning?", "duration": 2.168163},
            {"speaker": "explainer", "text": "Yeah. The U.S. and Iran reached an early peace deal.", "duration": 3.004082},
        ],
    },
}
MANIFEST = {"2026-06-25": {"1": GOOD_ENTRY}}


# ── 1) no manifest entry → no signal.listen injected ──
print("1) no entry → unchanged:")
feed = feed5()
before = json.dumps(feed, sort_keys=True)
feed, st = L.inject_listen(feed, "2099-01-01", MANIFEST, BASE)     # date not in manifest
check("no listen added to any signal", all("listen" not in s for s in feed["signals"]))
check("feed structurally unchanged", json.dumps(feed, sort_keys=True) == before)
check("stats no_entry == 5", st["no_entry"] == 5, str(st))


# ── 2) valid entry → injects signal.listen.en ──
print("\n2) valid entry → injects:")
feed = feed5()
feed, st = L.inject_listen(feed, "2026-06-25", MANIFEST, BASE)
s1 = feed["signals"][0]
check("signal 1 has listen", isinstance(s1.get("listen"), dict))
check("format dialogue", s1["listen"].get("format") == "dialogue")
check("en.audioURL built from key", s1["listen"]["en"]["audioURL"] == f"{BASE}/signal-01-dialogue-en.mp3",
      s1["listen"]["en"].get("audioURL"))
check("en.gap 0.0", s1["listen"]["en"]["gap"] == 0.0)
check("captions 2", len(s1["listen"]["en"]["captions"]) == 2)
check("signals 2-5 untouched", all("listen" not in s for s in feed["signals"][1:]))
check("stats injected==1", st["injected"] == 1, str(st))
check("audioURL (EN single) untouched", s1.get("audioURL") == "")


# ── 3) injected feed passes validate_feed ──
print("\n3) injected feed validates:")
feed = feed5()
feed, _ = L.inject_listen(feed, "2026-06-25", MANIFEST, BASE)
errs = V.structural_errors(feed)
check("structural_errors clean", errs == [], str(errs))
check("listen_errors clean on signal 1", V.listen_errors(feed["signals"][0]) == [])


# ── 4) malformed entries do not break; skipped, no listen ──
print("\n4) malformed → skipped (no break, no listen):")
def inject_one(entry):
    f = feed5()
    f, s = L.inject_listen(f, "2026-06-25", {"2026-06-25": {"1": entry}}, BASE)
    return f["signals"][0], s

s1, st = inject_one({"en": {"key": "x.mp3", "captions": []}})            # empty captions
check("empty captions → skipped", "listen" not in s1 and st["skipped"] == 1, str(st))
s1, _ = inject_one({"en": {"captions": [{"speaker": "a", "text": "b", "duration": 1}]}})  # no audio
check("missing audioURL → skipped", "listen" not in s1)
s1, _ = inject_one({"en": {"key": "x.mp3", "captions": [{"speaker": "a", "text": "b", "duration": 0}]}})  # bad dur
check("bad duration → skipped (all-or-nothing)", "listen" not in s1)
s1, _ = inject_one("not-an-object")                                      # entry not dict
check("entry not object → skipped", "listen" not in s1)
# garbage manifest never raises
f, st = L.inject_listen(feed5(), "2026-06-25", "totally broken", BASE)
check("garbage manifest → no raise, nothing injected", all("listen" not in s for s in f["signals"]))


# ── preserve existing listen ──
print("\n+) preserve existing listen:")
f = feed5()
f["signals"][0]["listen"] = {"format": "dialogue", "en": {"audioURL": "https://keep/me.mp3", "gap": 0, "captions": [{"speaker": "x", "text": "y", "duration": 1}]}}
f, st = L.inject_listen(f, "2026-06-25", MANIFEST, BASE)
check("existing listen preserved", f["signals"][0]["listen"]["en"]["audioURL"] == "https://keep/me.mp3")
check("stats preserved>=1", st["preserved"] >= 1, str(st))


# ── 5) existing JA audio injection behavior unchanged ──
print("\n5) JA audio injection unchanged:")
ja_manifest = {"2026-06-25": {"1": {"ja": "signal-01-ja-v2.mp3"}}}
f = feed5(ja=True)
f, ast = A.inject_ja_audio(f, "2026-06-25", ja_manifest, BASE)
check("JA audioURL injected as before", f["signals"][0]["localized"]["ja"].get("audioURL") == f"{BASE}/signal-01-ja-v2.mp3")
# and listen injection on the SAME feed doesn't disturb the JA audio
f, _ = L.inject_listen(f, "2026-06-25", MANIFEST, BASE)
check("JA audioURL still intact after listen inject", f["signals"][0]["localized"]["ja"].get("audioURL") == f"{BASE}/signal-01-ja-v2.mp3")
check("listen added alongside JA", isinstance(f["signals"][0].get("listen"), dict))


# ── 6) real manifest file loads + matches prototype ──
print("\n6) real listen_manifest.json:")
real = L.load_listen_manifest()
check("manifest loads (non-empty)", isinstance(real, dict) and "2026-06-25" in real, str(list(real)[:3]))
f = feed5()
f, st = L.inject_listen(f, "2026-06-25", real, BASE)
caps = f["signals"][0].get("listen", {}).get("en", {}).get("captions", [])
check("prototype injects 16 captions", len(caps) == 16, str(len(caps)))
check("real-injected feed validates", V.structural_errors(f) == [], str(V.structural_errors(f)))


print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
