#!/usr/bin/env python3
"""
Tests for pipeline/narrate.py (draft-only narration). No network, no API keys — the ElevenLabs call
is stubbed and scripts come from a temp prepared-script dir. Run: python3 pipeline/test_narrate.py
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import narrate  # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0


def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


def sample_feed():
    return {"date": "2026-06-17", "signals": [
        {"number": 1, "headline": "A peace deal", "summary": "Some summary.",
         "keyTakeaways": ["k1"], "whyItMatters": "It matters.",
         "localized": {"ja": {"headline": "和平", "summary": "要約。", "whyItMatters": "重要。"}}},
        {"number": 2, "headline": "Markets steady", "summary": "Rates held.",
         "whyItMatters": "Stays put."},   # EN only (no localized.ja)
    ]}


def stub_synth(text, *, voice, settings, api_key, model=narrate.EL_MODEL):
    # pretend-render: a few bytes proportional to the text, no network
    return ("MP3" + text[:8]).encode("utf-8")


# ── 1) script builder: prepared file wins; absent → none (no raw-field fallback) ──
print("1) Listen script source:")
with tempfile.TemporaryDirectory() as d:
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "signal1-en.txt"), "w", encoding="utf-8").write("Prepared EN script.\n")
    sig = sample_feed()["signals"][0]
    text, src = narrate.build_listen_script(sig, "en", anthropic_key=None, script_dir=d, use_llm=False)
    check("prepared file used when present", text == "Prepared EN script." and src == "prepared-file", f"{src}:{text!r}")
    # no prepared JA file + no LLM key → None (never raw fields)
    text_ja, src_ja = narrate.build_listen_script(sig, "ja", anthropic_key=None, script_dir=d, use_llm=False)
    check("no file + no LLM → None (no raw-field narration)", text_ja is None and src_ja == "none", f"{src_ja}")


# ── 2) missing ELEVENLABS key must NOT fail hard; audioURLs stay empty ──
print("\n2) Missing ELEVENLABS_API_KEY is graceful:")
with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as sd:
    open(os.path.join(sd, "signal1-en.txt"), "w", encoding="utf-8").write("Prepared EN.\n")
    feed, stats = narrate.narrate_edition(sample_feed(), date="2026-06-17", out_root=out, el_key=None,
                                          anthropic_key=None, script_dir=sd, use_llm=False, log=lambda *_: None)
    s1 = feed["signals"][0]
    check("did not raise; produced stats", isinstance(stats, dict))
    check("audioURL stays empty without key", s1.get("audioURL", "") == "")
    check("listenScript still stored from prepared file", s1.get("listenScript") == "Prepared EN.")


# ── 3) audioURL shape + skip-if-exists, with a stubbed synth (no network) ──
print("\n3) audioURL shape + skip-if-exists (stubbed synth):")
with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as sd:
    open(os.path.join(sd, "signal1-en.txt"), "w", encoding="utf-8").write("Prepared EN.\n")
    open(os.path.join(sd, "signal1-ja.txt"), "w", encoding="utf-8").write("用意した日本語。\n")
    open(os.path.join(sd, "signal2-en.txt"), "w", encoding="utf-8").write("Prepared EN two.\n")
    feed, stats = narrate.narrate_edition(sample_feed(), date="2026-06-17", out_root=out,
                                          base_url="https://example.com", el_key="fake",
                                          anthropic_key=None, script_dir=sd, use_llm=False,
                                          synth_fn=stub_synth, log=lambda *_: None)
    s1, s2 = feed["signals"]
    check("EN audioURL is absolute https .mp3",
          s1.get("audioURL") == "https://example.com/audio/2026-06-17/signal-01-en.mp3", s1.get("audioURL"))
    check("JA audioURL under localized.ja",
          s1["localized"]["ja"].get("audioURL") == "https://example.com/audio/2026-06-17/signal-01-ja.mp3",
          s1["localized"]["ja"].get("audioURL"))
    check("EN-only signal #2 has audioURL, no JA", s2.get("audioURL", "").endswith("signal-02-en.mp3")
          and "ja" not in (s2.get("localized") or {}))
    check("mp3 files written to out tree",
          os.path.exists(os.path.join(out, "audio/2026-06-17/signal-01-en.mp3")))
    check("clips counted (3: s1 en+ja, s2 en)", stats["clips"] == 3, str(stats))

    # run again → should SKIP existing files (no new clips)
    calls = {"n": 0}
    def counting_synth(*a, **k):
        calls["n"] += 1
        return stub_synth(*a, **k)
    _, stats2 = narrate.narrate_edition(sample_feed(), date="2026-06-17", out_root=out,
                                        base_url="https://example.com", el_key="fake",
                                        anthropic_key=None, script_dir=sd, use_llm=False,
                                        synth_fn=counting_synth, log=lambda *_: None)
    check("skip-if-exists: synth not called on 2nd run", calls["n"] == 0, f"calls={calls['n']}")
    check("skip-if-exists: counted as reused", stats2["skipped_existing"] == 3, str(stats2))


print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
