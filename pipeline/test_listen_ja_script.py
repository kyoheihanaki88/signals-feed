#!/usr/bin/env python3
"""JA Listen generator (PR-1) — quality gate + lang plumbing tests.

Covers: valid JA dialogue passes; literal-translation style, English passthrough,
ungrounded numbers/names, overlong lines, and broken speaker alternation are rejected;
malformed dialogue still raises in parse_dialogue; key_for stays EN-default; and an
end-to-end stubbed generate() proves the EN path is unchanged while a JA run MERGES
`ja` into the manifest without touching `en`.
"""
import json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import listen_generate as lg

failures = 0
def check(name, cond):
    global failures
    print(("✓" if cond else "✗"), name)
    if not cond:
        failures += 1

# ---------------------------------------------------------------- fixtures
SIGNAL = {
    "number": 1,
    "headline": "Apple sues OpenAI, its employees claiming theft of trade secrets",
    "summary": "Apple filed a lawsuit against OpenAI on Tuesday, naming 30 employees.",
    "keyTakeaways": ["The suit was filed in California.", "It names 30 employees."],
    "whyItMatters": "The case could reshape how AI labs hire from rivals.",
    "localized": {"ja": {"headline": "AppleがOpenAIを提訴", "summary": "30人の従業員が対象。"}},
}

GOOD_JA = [
    {"speaker": "listener", "text": "AppleがOpenAIを訴えたそうですね。何があったんですか。"},
    {"speaker": "explainer", "text": "はい。Appleが火曜日に訴訟を起こしました。対象は30人の従業員です。"},
    {"speaker": "listener", "text": "どこで争われるんですか。"},
    {"speaker": "explainer", "text": "カリフォルニアです。企業秘密の持ち出しが争点になっています。"},
    {"speaker": "listener", "text": "これは大きな話なんでしょうか。"},
    {"speaker": "explainer", "text": "AI企業の人材の動き方に影響する可能性があります。"},
    {"speaker": "listener", "text": "なるほど。今後に注目ですね。"},
    {"speaker": "explainer", "text": "ええ。続報が入り次第、また取り上げます。"},
]

# ---------------------------------------------------------------- quality gate
check("valid JA dialogue passes", lg.ja_quality_issues(GOOD_JA, SIGNAL) == [])

bad_literal = [dict(l) for l in GOOD_JA]
bad_literal[1] = {"speaker": "explainer", "text": "Appleは訴訟についての発表をしました。"}
check("literal-translation style rejected",
      any("forbidden phrase" in i for i in lg.ja_quality_issues(bad_literal, SIGNAL)))

bad_english = [dict(l) for l in GOOD_JA]
bad_english[2] = {"speaker": "listener", "text": "Where will the case be heard?"}
check("English passthrough line rejected",
      any("no Japanese characters" in i for i in lg.ja_quality_issues(bad_english, SIGNAL)))

bad_number = [dict(l) for l in GOOD_JA]
bad_number[1] = {"speaker": "explainer", "text": "はい。Appleが訴訟を起こしました。対象は45人の従業員です。"}
check("ungrounded number rejected",
      any("ungrounded number '45'" in i for i in lg.ja_quality_issues(bad_number, SIGNAL)))

bad_name = [dict(l) for l in GOOD_JA]
bad_name[3] = {"speaker": "explainer", "text": "カリフォルニアです。Anthropicも関係しています。"}
check("ungrounded latin name rejected",
      any("ungrounded name" in i for i in lg.ja_quality_issues(bad_name, SIGNAL)))

bad_long = [dict(l) for l in GOOD_JA]
bad_long[5] = {"speaker": "explainer", "text": "この件は" + "とても" * 40 + "重要です。"}
check("overlong line rejected",
      any("too long" in i for i in lg.ja_quality_issues(bad_long, SIGNAL)))

bad_run = [dict(l) for l in GOOD_JA]
bad_run[2] = {"speaker": "explainer", "text": "補足すると、これは民事訴訟です。"}
# lines 2,3,4 all explainer → three in a row
check("triple same-speaker run rejected",
      any("three times in a row" in i for i in lg.ja_quality_issues(bad_run, SIGNAL)))

check("full-width digits ground correctly",
      lg.ja_quality_issues([{"speaker": "listener", "text": "対象は３０人ですか。"},
                            {"speaker": "explainer", "text": "はい、30人です。"}] + GOOD_JA[2:], SIGNAL) == [])

# ---------------------------------------------------------------- parse_dialogue (shared, unchanged)
try:
    lg.parse_dialogue("not json at all")
    check("malformed dialogue raises", False)
except ValueError:
    check("malformed dialogue raises", True)

try:
    lg.parse_dialogue(json.dumps([{"speaker": "narrator", "text": "x"}] * 8))
    check("unknown speaker raises", False)
except ValueError:
    check("unknown speaker raises", True)

# ---------------------------------------------------------------- key_for
check("key_for default stays EN", lg.key_for("2099-01-01", 3) == "audio/2099-01-01/signal-03-dialogue-en.mp3")
check("key_for ja suffix", lg.key_for("2099-01-01", 3, "ja") == "audio/2099-01-01/signal-03-dialogue-ja.mp3")

# ---------------------------------------------------------------- end-to-end (stubbed) generate()
tmp = tempfile.mkdtemp(prefix="ja_listen_test_")
os.makedirs(os.path.join(tmp, "editions"))
DATE = "2099-01-01"
signals = []
for n in range(1, 6):
    s = dict(SIGNAL); s["number"] = n
    signals.append(s)
json.dump({"date": DATE, "signals": signals},
          open(os.path.join(tmp, "editions", f"{DATE}.json"), "w", encoding="utf-8"), ensure_ascii=False)
manifest_path = os.path.join(tmp, "listen_manifest.json")
json.dump({"editions": {}}, open(manifest_path, "w"))

# monkeypatch module paths (kept local to this test process)
lg.ROOT, lg.MANIFEST, lg.OUT_BASE = tmp, manifest_path, os.path.join(tmp, "scratch")

EN_LINES = [{"speaker": "listener", "text": "What happened with Apple and OpenAI?"},
            {"speaker": "explainer", "text": "Apple filed a lawsuit naming 30 employees."}] * 4

def llm_stub_en(sig, *, api_key, model=None):            # legacy signature — no lang kwarg
    return [dict(l) for l in EN_LINES]

def llm_stub_ja(sig, *, api_key, model=None, lang="en"):
    return [dict(l) for l in GOOD_JA]

def synth_stub(text, voice, settings, key):
    return b"x"

def dur_stub(path):
    name = os.path.basename(path)
    return 1.0 if "line-" in name else float(os.path.getsize(path))

uploaded = []
def upload_stub(local, key):
    uploaded.append(key)

def verify_stub(url):
    return True, "stub"

# EN run — legacy call shape (no lang argument at all)
entry_en = lg.generate(DATE, el_key="k", an_key="k", listener_voice="L", explainer_voice="E",
                       llm_fn=llm_stub_en, synth_fn=synth_stub, dur_fn=dur_stub,
                       upload_fn=upload_stub, verify_fn=verify_stub, log=lambda *a: None)
m = json.load(open(manifest_path))
check("EN entry shape unchanged", set(m["editions"][DATE]["1"].keys()) == {"format", "en"})
check("EN key unchanged", m["editions"][DATE]["1"]["en"]["key"].endswith("-dialogue-en.mp3"))
check("EN line filenames unchanged (no lang suffix)",
      os.path.exists(os.path.join(lg.OUT_BASE, DATE, "sig1-line-01.mp3")))
check("EN uploads all -en", all(k.endswith("-en.mp3") for k in uploaded))
en_snapshot = json.dumps(m["editions"][DATE]["1"]["en"], sort_keys=True)

# JA run — merges into the same date entry
uploaded.clear()
entry_ja = lg.generate(DATE, el_key="k", an_key="k", listener_voice="LJ", explainer_voice="EJ",
                       lang="ja", llm_fn=llm_stub_ja, synth_fn=synth_stub, dur_fn=dur_stub,
                       upload_fn=upload_stub, verify_fn=verify_stub, log=lambda *a: None)
m2 = json.load(open(manifest_path))
sig1 = m2["editions"][DATE]["1"]
check("JA merged alongside EN", set(sig1.keys()) == {"format", "en", "ja"})
check("EN block byte-identical after JA merge",
      json.dumps(sig1["en"], sort_keys=True) == en_snapshot)
check("JA key correct", sig1["ja"]["key"].endswith("-dialogue-ja.mp3"))
check("JA uploads all -ja", uploaded and all(k.endswith("-ja.mp3") for k in uploaded))
check("all 5 signals carry ja", all("ja" in m2["editions"][DATE][str(n)] for n in range(1, 6)))

# JA gate wired into generate(): a bad JA script must abort before any upload
uploaded.clear()
def llm_stub_ja_bad(sig, *, api_key, model=None, lang="en"):
    bad = [dict(l) for l in GOOD_JA]
    bad[1] = {"speaker": "explainer", "text": "はい。対象は45人の従業員です。"}
    return bad
try:
    lg.generate(DATE, el_key="k", an_key="k", listener_voice="LJ", explainer_voice="EJ",
                lang="ja", llm_fn=llm_stub_ja_bad, synth_fn=synth_stub, dur_fn=dur_stub,
                upload_fn=upload_stub, verify_fn=verify_stub, log=lambda *a: None)
    check("bad JA script aborts generate()", False)
except ValueError as e:
    check("bad JA script aborts generate()", "JA quality gate failed" in str(e))
check("no uploads after JA gate failure", uploaded == [])
m3 = json.load(open(manifest_path))
check("manifest unchanged after JA gate failure",
      json.dumps(m3["editions"][DATE]["1"], sort_keys=True) == json.dumps(sig1, sort_keys=True))

print("ALL PASS" if failures == 0 else f"{failures} CHECK(S) FAILED")
sys.exit(1 if failures else 0)
