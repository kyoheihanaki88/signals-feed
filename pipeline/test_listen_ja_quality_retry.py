#!/usr/bin/env python3
"""JA quality retry — a gate rejection must retry with feedback, never loop blind.

    python3 pipeline/test_listen_ja_quality_retry.py

Guards the 2026-08-12 outage (run 31615984769): the solo model answered signals 1–3 with
STRUCTURAL COPIES of localized.ja sentences, ja_solo_quality_issues rightly rejected them,
and generate() had no retry for gate failures — so every workflow rerun failed identically,
because the model was never told what was rejected. listen.ja.audioURL was therefore never
published for 2026-08-12, and the iOS app fell back to on-device TTS (the "robot voice").

Pinned here:
  • the gate itself still rejects the exact 2026-08-12 failure shape (a line reproducing a
    localized.ja sentence) — the gate is NOT weakened;
  • generate() retries a rejected signal up to JA_QUALITY_MAX_ATTEMPTS, passing the gate's
    own issue strings to the model via _JA_QUALITY_FEEDBACK, and clears the feedback after
    every call;
  • a signal that cannot pass still aborts the run with the SAME error shape as before
    (no upload, no manifest write);
  • llm_dialogue appends the feedback to the JA request content only — the EN payload and
    a feedback-free JA payload are byte-identical to before. No network.
"""
import io
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listen_generate as lg  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("✓" if cond else "✗"), name)
    if not cond:
        failures += 1


# ---------------------------------------------------------------- 2026-08-12 failure shape
# Signal 1 of the real outage: 「コロンビア地震、政府と救助隊に重い試練」. The rejected
# scripts copied full localized.ja sentences with at most cosmetic edits.
JA_SUMMARY = ("コロンビア中部で発生した地震により、政府と救助隊は復旧作業と"
              "行方不明者の捜索という重い試練に直面しています。")
SIGNAL = {
    "number": 1,
    "headline": "Colombia earthquake tests government and rescuers",
    "summary": "A strong earthquake in central Colombia has left the government and "
               "rescue teams facing a heavy test.",
    "keyTakeaways": ["Rescue teams are searching for missing residents."],
    "whyItMatters": "Recovery capacity shapes how quickly life returns to normal.",
    "localized": {"ja": {
        "headline": "コロンビア地震、政府と救助隊に重い試練",
        "summary": JA_SUMMARY,
    }},
}

CLEAN_TEXTS = [
    "コロンビアの中部で、大きな地震がありました。",
    "被害の全体像は、まだつかめていません。",
    "現地では、連絡が取れない人の捜索が続いています。",
    "政府は、立て直しの体制づくりを急いでいます。",
    "暮らしが元に戻るまでには、時間がかかりそうです。",
    "続報が入り次第、また取り上げます。",
]
CLEAN = [{"speaker": "narrator", "text": t} for t in CLEAN_TEXTS]

# The 2026-08-12 shape: one line reproduces a localized.ja sentence essentially verbatim.
COPIED = [dict(l) for l in CLEAN]
COPIED[2] = {"speaker": "narrator", "text": JA_SUMMARY}

# ---------------------------------------------------------------- gate is NOT weakened
issues_copied = lg.ja_solo_quality_issues(COPIED, SIGNAL)
check("gate still rejects the 2026-08-12 structural copy",
      any("structural copy" in i for i in issues_copied))
check("the clean paraphrase passes the unchanged gate",
      lg.ja_solo_quality_issues(CLEAN, SIGNAL) == [])
_note = lg._ja_quality_retry_note(issues_copied)
check("retry note quotes the gate's own issues and demands a rewrite from meaning",
      "品質再指示" in _note and "structural copy" in _note and "話し言葉" in _note)

# ---------------------------------------------------------------- generate() harness
tmp = tempfile.mkdtemp(prefix="ja_quality_retry_")
os.makedirs(os.path.join(tmp, "editions"), exist_ok=True)
manifest_path = os.path.join(tmp, "listen_manifest.json")
json.dump({"editions": {}}, open(manifest_path, "w", encoding="utf-8"))
lg.ROOT, lg.MANIFEST, lg.OUT_BASE = tmp, manifest_path, os.path.join(tmp, "scratch")
os.environ["LISTEN_JA_RESUME"] = "0"      # deterministic: no checkpoint reuse in this test

DATE = "2026-08-12"
json.dump({"date": DATE, "signals": [dict(SIGNAL, number=n) for n in range(1, 6)]},
          open(os.path.join(tmp, "editions", f"{DATE}.json"), "w", encoding="utf-8"),
          ensure_ascii=False)

uploaded = []


def synth_stub(text, voice, settings, key):
    return b"x"


def dur_stub(path):
    # Per-line files → 1.0s each; the concatenated final → its byte size, which equals the
    # line count (each stub line is 1 byte), so decoded-final == sum(per-line) → drift 0.
    name = os.path.basename(path)
    return 1.0 if "line-" in name else float(os.path.getsize(path))


def upload_stub(local, key):
    uploaded.append(key)
    return f"https://cdn.example/{key}"


def verify_stub(url):
    return True, "ok"


def run(llm):
    return lg.generate(DATE, el_key="k", an_key="k",
                       listener_voice=lg.AZURE_VOICE_JA_LISTENER,
                       explainer_voice=lg.AZURE_VOICE_JA_EXPLAINER,
                       lang="ja", llm_fn=llm, synth_fn=synth_stub, dur_fn=dur_stub,
                       final_dur_fn=dur_stub, upload_fn=upload_stub, verify_fn=verify_stub,
                       log=lambda *a: None)


# ---------------------------------------------------------------- retry-with-feedback cures the run
calls = []                                 # (signal number, feedback seen at call time)


def llm_copy_then_clean(sig, *, api_key, model=None, lang="en"):
    """The 2026-08-12 model, cured by feedback: first attempt per signal copies the
    article; any attempt that carries quality feedback paraphrases properly."""
    fb = lg._JA_QUALITY_FEEDBACK
    calls.append((sig["number"], fb))
    return [dict(l) for l in (CLEAN if fb else COPIED)]


uploaded.clear()
entry = run(llm_copy_then_clean)
check("2026-08-12 shape now completes: all five signals upload",
      len(uploaded) == 5 and all(k.endswith("-ja.mp3") for k in uploaded))
check("each signal took exactly one retry (2 calls per signal)",
      [n for n, _ in calls] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5])
check("first attempt per signal carried NO feedback",
      all(fb is None for (_, fb) in calls[0::2]))
check("the retry carried the gate's issues as feedback",
      all(fb and "structural copy" in fb for (_, fb) in calls[1::2]))
check("feedback is cleared after generate()", lg._JA_QUALITY_FEEDBACK is None)
check("the shipped captions are the paraphrase, not the copy",
      [c["text"] for c in entry["1"]["ja"]["captions"]] == CLEAN_TEXTS)

# rejected scripts are still dumped for inspection
check("rejected script artifact written",
      os.path.exists(os.path.join(tmp, "scratch", f"failed_ja_dialogue_{DATE}_signal1.json")))

# ---------------------------------------------------------------- an incurable signal still aborts
calls_bad = []


def llm_always_copy(sig, *, api_key, model=None, lang="en"):
    calls_bad.append(sig["number"])
    return [dict(l) for l in COPIED]


uploaded.clear()
try:
    run(llm_always_copy)
    check("an incurable gate failure still aborts the run", False)
except ValueError as e:
    check("an incurable gate failure still aborts the run",
          "signal 1 JA quality gate failed" in str(e) and "structural copy" in str(e))
check("the abort is bounded: JA_QUALITY_MAX_ATTEMPTS calls, then stop",
      calls_bad == [1] * lg.JA_QUALITY_MAX_ATTEMPTS)
check("nothing uploaded on abort", uploaded == [])
check("feedback is cleared after the abort", lg._JA_QUALITY_FEEDBACK is None)

# ---------------------------------------------------------------- checkpoints never freeze a rejected script
# A checkpoint is only ever written for a PASSING signal, and _load_ja_checkpoint re-runs
# the CURRENT gate on load — so a script the gate now rejects (e.g. one saved before a gate
# fix, or a stale edition's copy) regenerates instead of being reused forever.
os.environ.pop("LISTEN_JA_RESUME", None)     # resume ON for this block
CK_DATE = "2026-08-15"
json.dump({"date": CK_DATE, "signals": [dict(SIGNAL, number=n) for n in range(1, 6)]},
          open(os.path.join(tmp, "editions", f"{CK_DATE}.json"), "w", encoding="utf-8"),
          ensure_ascii=False)

calls_ck = []


def llm_clean_count(sig, *, api_key, model=None, lang="en"):
    calls_ck.append(sig["number"])
    return [dict(l) for l in CLEAN]


def run_ck(llm):
    return lg.generate(CK_DATE, el_key="k", an_key="k",
                       listener_voice=lg.AZURE_VOICE_JA_LISTENER,
                       explainer_voice=lg.AZURE_VOICE_JA_EXPLAINER,
                       lang="ja", llm_fn=llm, synth_fn=synth_stub, dur_fn=dur_stub,
                       final_dur_fn=dur_stub, upload_fn=upload_stub, verify_fn=verify_stub,
                       log=lambda *a: None)


uploaded.clear()
run_ck(llm_clean_count)
check("checkpoint block: clean run checkpoints all five", calls_ck == [1, 2, 3, 4, 5])

# Poison signal 2's checkpoint with the 2026-08-12 structural copy, as if it had been
# saved under an older gate. The load-time re-verification must reject and regenerate it.
_ck_path = os.path.join(lg.OUT_BASE, CK_DATE, "signal-02-ja.checkpoint.json")
_ck = json.load(open(_ck_path, encoding="utf-8"))
_ck["lines"][2]["text"] = JA_SUMMARY
json.dump(_ck, open(_ck_path, "w", encoding="utf-8"), ensure_ascii=False)

calls_ck.clear()
uploaded.clear()
entry_ck = run_ck(llm_clean_count)
check("a checkpoint holding a gate-rejected script is regenerated, not reused",
      calls_ck == [2])
check("the regenerated captions carry the paraphrase, not the checkpointed copy",
      [c["text"] for c in entry_ck["2"]["ja"]["captions"]] == CLEAN_TEXTS)
check("all five still upload after the checkpoint re-evaluation", len(uploaded) == 5)
os.environ["LISTEN_JA_RESUME"] = "0"

# ---------------------------------------------------------------- llm_dialogue payload proof
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _with_urlopen(reply_lines, fn):
    """Run fn() with urlopen doubled; returns the recorded request bodies."""
    bodies = []
    real = urllib.request.urlopen

    def fake(req, *a, **kw):
        bodies.append(json.loads(req.data.decode("utf-8")))
        return _Resp(json.dumps({
            "content": [{"type": "text",
                         "text": json.dumps(reply_lines, ensure_ascii=False)}],
            "stop_reason": "end_turn",
        }).encode("utf-8"))

    urllib.request.urlopen = fake
    try:
        fn()
    finally:
        urllib.request.urlopen = real
    return bodies


# JA with feedback set → the note reaches the request content (and only then), and the
# output budget starts one rung UP the ladder: on 2026-08-13 the feedback-lengthened
# prompt made thinking alone exhaust the historical 1200, so no JSON body ever came back.
lg._JA_QUALITY_FEEDBACK = lg._ja_quality_retry_note(issues_copied)
try:
    bodies = _with_urlopen(CLEAN_TEXTS,
                           lambda: lg.llm_dialogue(SIGNAL, api_key="k", lang="ja"))
finally:
    lg._JA_QUALITY_FEEDBACK = None
check("llm_dialogue appends the feedback to the JA request content",
      "品質再指示" in bodies[0]["messages"][0]["content"])
check("a quality retry starts at the 2400 budget rung, not 1200",
      bodies[0]["max_tokens"] == lg.JA_BUDGETS[1])

# JA without feedback → payload carries no retry note (byte-identical to before)
bodies_clean = _with_urlopen(CLEAN_TEXTS,
                             lambda: lg.llm_dialogue(SIGNAL, api_key="k", lang="ja"))
check("a feedback-free JA payload carries no quality note",
      "品質再指示" not in bodies_clean[0]["messages"][0]["content"])
check("a first attempt keeps the historical 1200 budget",
      bodies_clean[0]["max_tokens"] == lg.JA_BUDGETS[0])

# EN never sees the feedback, even if it were set
EN_LINES = [{"speaker": "listener", "text": f"Question {i}?"} if i % 2 else
            {"speaker": "explainer", "text": f"Answer number {i}."}
            for i in range(1, 9)]
lg._JA_QUALITY_FEEDBACK = "[品質再指示] must never appear in EN"
try:
    bodies_en = _with_urlopen(EN_LINES,
                              lambda: lg.llm_dialogue(SIGNAL, api_key="k"))
finally:
    lg._JA_QUALITY_FEEDBACK = None
check("the EN payload never carries the JA quality note",
      "品質再指示" not in bodies_en[0]["messages"][0]["content"])

os.environ.pop("LISTEN_JA_RESUME", None)
print()
if failures:
    print(f"{failures} check(s) FAILED")
    sys.exit(1)
print("all checks passed")
