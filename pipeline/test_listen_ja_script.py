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

# ---------------------------------------------------------------- numeric grounding across notations
# Regression for the 2026-07-14 signal-1 false positive: the source stated every figure as
# English words ("two", "eight", "third") and kanji numerals ("二隻", "八名", "六十日間"), so the
# old digit-only grounded set was essentially just {"60"} and correctly-spoken Arabic digits
# (2隻 / 8名 / 2月) were wrongly flagged "ungrounded". Grounding must now normalize source
# words/kanji to Arabic — while STILL rejecting numbers the source never states.

# English number words in source → Arabic
check("english words ground to Arabic (two/one/eight/four/third → 2/1/8/4/3)",
      {"2", "1", "8", "4", "3"} <= lg._grounded_source_numbers(
          "two national tankers, one crew member, eight others, four seriously, third night"))

# kanji numerals → Arabic (single digits and unit compounds)
check("kanji 二隻/一名/八名/四名/三夜 ground 2/1/8/4/3",
      {"2", "1", "8", "4", "3"} <= (lg._kanji_numbers("二隻") | lg._kanji_numbers("一名")
       | lg._kanji_numbers("八名") | lg._kanji_numbers("四名") | lg._kanji_numbers("三夜")))
check("kanji 二月 grounds 2", "2" in lg._kanji_numbers("二月に始まった"))
check("kanji 六十日間 grounds 60", lg._kanji_numbers("六十日間") == {"60"})
check("kanji 六十 does not independently ground 6 or 10",
      "6" not in lg._kanji_numbers("六十") and "10" not in lg._kanji_numbers("六十"))
check("kanji 十八 grounds 18 only (not 1/8/10)", lg._kanji_numbers("十八") == {"18"})
check("bare positional kanji run is not mis-valued (二〇二六 skipped)",
      lg._kanji_numbers("二〇二六年") == set())

# whole-signal grounded set for a 2026-07-14-style Hormuz signal (English words + kanji localized)
HORMUZ_SIGNAL = {
    "number": 1,
    "headline": "Trump threatens US tolls on Hormuz shipping as strikes on Iran continue",
    "summary": "The US launched its third consecutive night of strikes on Iran.",
    "keyTakeaways": [
        "Two national tankers were targeted by two Iranian cruise missiles, killing one "
        "Indian crew member and wounding eight others, including four seriously.",
        "The interim ceasefire is near total collapse.",
    ],
    "whyItMatters": "They are nearly halfway through the 60-day interim deal, in a war that began in February.",
    "localized": {"ja": {
        "headline": "ホルムズ海峡に通行料——米国、イランへの攻撃を三夜連続で継続",
        "summary": "米国がイランへの攻撃を三夜連続で実施した。",
        "keyTakeaways": [
            "自国タンカー二隻がイランの巡航ミサイルに攻撃され、インド人乗組員一名が死亡し、八名が負傷、うち四名は重傷。",
            "暫定停戦は事実上崩壊に近い状態となっている。",
        ],
        "whyItMatters": "二月に始まったこの戦争の、六十日間の暫定合意のほぼ折り返し地点にある。",
    }},
}
_hz_src = json.dumps({k: HORMUZ_SIGNAL.get(k) for k in
                     ("headline", "summary", "keyTakeaways", "whyItMatters", "localized")},
                     ensure_ascii=False).translate(lg._FW_DIGITS)
_hz = lg._grounded_source_numbers(_hz_src)
check("Hormuz source grounds the real facts 1,2,3,4,8,60", {"1", "2", "3", "4", "8", "60"} <= _hz)
check("Hormuz grounding still rejects invented 7/9/30/45/100",
      all(bad not in _hz for bad in ("7", "9", "30", "45", "100")))

# a valid JA dialogue stating those facts in Arabic digits must pass the number gate cleanly
HORMUZ_JA = [
    {"speaker": "listener", "text": "ホルムズ海峡で何が起きているんですか?"},
    {"speaker": "explainer", "text": "米国がイランへの攻撃を3夜続けています。通行料の話も出ています。"},
    {"speaker": "listener", "text": "船が狙われたというのは本当ですか?"},
    {"speaker": "explainer", "text": "タンカー2隻がミサイルで攻撃されました。1名が亡くなり、8名が負傷しています。"},
    {"speaker": "listener", "text": "重傷の人もいるんですか?"},
    {"speaker": "explainer", "text": "4名が重傷と伝えられています。"},
    {"speaker": "listener", "text": "戦争はいつからなんですか?"},
    {"speaker": "explainer", "text": "2月に始まりました。60日間の暫定合意の途中です。"},
]
check("valid JA dialogue using 2,1,8,4,3,60 passes (no ungrounded-number issue)",
      not any("ungrounded number" in i for i in lg.ja_quality_issues(HORMUZ_JA, HORMUZ_SIGNAL)))

# fail-closed preserved end-to-end: an invented casualty number is still caught
_hz_bad = [dict(l) for l in HORMUZ_JA]
_hz_bad[3] = {"speaker": "explainer", "text": "タンカー2隻が攻撃され、45名が負傷しました。"}
check("invented number in JA dialogue still rejected (fail-closed intact)",
      any("ungrounded number '45'" in i for i in lg.ja_quality_issues(_hz_bad, HORMUZ_SIGNAL)))

# ---------------------------------------------------------------- v2: conversation structure
# The failure mode from the first real generation: a narrator reading the news with a
# listener that only nods. Every one of these must now be rejected.

NARRATOR_STYLE = [
    {"speaker": "listener", "text": "今日のニュースを教えてください。"},
    {"speaker": "explainer", "text": "Appleは火曜日、OpenAIと従業員30人を提訴しました。"},
    {"speaker": "listener", "text": "なるほど。"},
    {"speaker": "explainer", "text": "訴訟はカリフォルニア州で提起されています。"},
    {"speaker": "listener", "text": "そうですね。"},
    {"speaker": "explainer", "text": "AI業界の採用に影響する可能性があります。"},
]
_narr = lg.ja_quality_issues(NARRATOR_STYLE, SIGNAL)
check("narrator+aizuchi style rejected", any("aizuchi" in i for i in _narr))
check("narrator style fails question minimum", any("question" in i for i in _narr))

summarizing = [dict(l) for l in GOOD_JA]
summarizing[2] = {"speaker": "listener", "text": "AppleがOpenAIと30人を火曜日に提訴したんですよね。"}
check("listener summarizing article facts rejected",
      any("summarizes instead of asking" in i for i in lg.ja_quality_issues(summarizing, SIGNAL)))

one_question = [
    {"speaker": "listener", "text": "AppleがOpenAIを訴えたって本当ですか。"},
    {"speaker": "explainer", "text": "本当です。火曜日に訴訟を起こしました。"},
    {"speaker": "listener", "text": "続きが気になる話ですね。"},
    {"speaker": "explainer", "text": "ええ、また動きがあれば話しましょう。"},
    {"speaker": "listener", "text": "朝から勉強になりました。"},
    {"speaker": "explainer", "text": "こちらこそ。良い一日を。"},
]
check("only one question rejected",
      any("question" in i for i in lg.ja_quality_issues(one_question, SIGNAL)))

SIGNAL_PASTE = dict(SIGNAL)
SIGNAL_PASTE["localized"] = {"ja": {
    "headline": "AppleがOpenAIを提訴",
    "summary": "30人の従業員が企業秘密を持ち出した疑いで提訴の対象になっています。"}}
pasted = [dict(l) for l in GOOD_JA]
pasted[1] = {"speaker": "explainer",
             "text": "はい。30人の従業員が企業秘密を持ち出した疑いで提訴の対象になっています。"}
check("article text pasted into dialogue rejected",
      any("pasted into dialogue" in i for i in lg.ja_quality_issues(pasted, SIGNAL_PASTE)))

# ---- paste detector: one unavoidable fact phrase must PASS; a copied sentence must FAIL ----
# Regression for the 2026-07-14 signal-1 false positive: an explainer paraphrase sharing the
# single entity/event phrase 「…隻がイランの巡航ミサイルに攻撃され…」 (17ch) with localized.ja was
# wrongly rejected. Detector now fails only on structural copies (dominant single run, or ≥2
# independent runs), not on one unavoidable fact phrase inside a genuine paraphrase.
PASTE_SIG = {
    "number": 1,
    "headline": "Trump threatens US tolls on Hormuz shipping as strikes on Iran continue",
    "summary": "The US launched its third consecutive night of strikes on Iran.",
    "keyTakeaways": ["Two national tankers were hit by two Iranian cruise missiles, "
                     "killing one Indian crew member and wounding eight."],
    "whyItMatters": "They are nearly halfway through the 60-day interim deal.",
    "localized": {"ja": {
        "headline": "ホルムズ海峡に通行料——米国、イランへの攻撃を三夜連続で継続",
        "summary": "米国がイランへの攻撃を三夜連続で実施した。トランプ大統領がホルムズ海峡を通過する船舶に通行料を課す方針を示した直後のことだ。",
        "keyTakeaways": [
            "アラブ首長国連邦は、オマーン領海内のホルムズ海峡南航路で自国タンカー二隻がイランの巡航ミサイルに攻撃されたと発表。"
            "インド人乗組員一名が死亡し、八名が負傷、うち四名は重傷。"],
        "whyItMatters": "二月に始まったこの戦争の、六十日間の暫定合意のほぼ折り返し地点にある。",
    }},
}
_paste_src = lg._ja_source_strings(PASTE_SIG)

# PASS: genuine paraphrase carrying the unavoidable fact phrase (the exact confirmed line)
_para = "UAEのタンカー2隻がイランの巡航ミサイルに攻撃されて、インド人乗組員1人が亡くなっています。"
check("paraphrase with a single unavoidable fact phrase passes paste gate",
      not lg._is_article_paste(_para, _paste_src))
check("...and it carries a real ≥15-char overlap (so the gate is genuinely tested, not vacuous)",
      len(lg._paste_overlaps(_para, _paste_src)) == 1 and
      len(lg._paste_overlaps(_para, _paste_src)[0]) >= lg._JA_PASTE_MIN)
_para_full = [dict(l) for l in GOOD_JA]
_para_full[3] = {"speaker": "explainer", "text": _para}
check("paraphrase line not flagged inside a full dialogue run",
      not any("pasted into dialogue" in i for i in lg.ja_quality_issues(_para_full, PASTE_SIG)))

# FAIL: a localized.ja sentence copied with only cosmetic punctuation changes (dominant run)
_copied = "米国がイランへの攻撃を三夜連続で実施した!"   # summary sentence 1, 。→!
check("copied localized.ja sentence with minor punctuation change is rejected",
      lg._is_article_paste(_copied, _paste_src))

# FAIL: two independent long article runs stitched together (multiple-overlap branch)
_stitched = "米国がイランへの攻撃を三夜連続で実施した、そしてホルムズ海峡を通過する船舶に通行料を課す。"
check("two independent long article overlaps stitched together are rejected",
      len(lg._paste_overlaps(_stitched, _paste_src)) >= 2 and lg._is_article_paste(_stitched, _paste_src))

# ---- paste detector: a short factual summary of a LONG keyTakeaway must PASS (sentence-ratio) ----
# Regression for the 2026-07-14 signal-4 false positive: a summary sharing a 23-char sub-phrase of a
# 60-char keyTakeaway had 68% line-coverage and was wrongly rejected. It only reproduces ~39% of the
# source sentence, so it now passes; a near-full copy of that sentence still fails.
SAT_SIG = {"number": 4, "localized": {"ja": {"keyTakeaways": [
    "計画が成功した場合、2035年までに5万機の衛星群を展開し、地上で最大約5キロメートルの範囲を照らすことを想定している。"]}}}
_sat_src = lg._ja_source_strings(SAT_SIG)
_summary = "将来的には地上で約5キロメートルの範囲を照らすことを想定しています。"   # the exact rejected line
check("short factual summary of a long keyTakeaway passes paste gate",
      not lg._is_article_paste(_summary, _sat_src))
check("...and it really shares a >=15-char run (so the gate is genuinely tested)",
      len(lg._paste_overlaps(_summary, _sat_src)) == 1 and
      len(lg._paste_overlaps(_summary, _sat_src)[0]) >= lg._JA_PASTE_MIN)
# full keyTakeaway copied with only a cosmetic verb ending change (している→しています) still fails
_sat_copy = "計画が成功した場合、2035年までに5万機の衛星群を展開し、地上で最大約5キロメートルの範囲を照らすことを想定しています。"
check("full keyTakeaway copied with minor edits still rejected",
      lg._is_article_paste(_sat_copy, _sat_src))

hosting = [dict(l) for l in GOOD_JA]
hosting[0] = {"speaker": "listener", "text": "今日はAppleの訴訟についてお話しします。"}
check("presenter-style hosting rejected",
      any("forbidden phrase" in i for i in lg.ja_quality_issues(hosting, SIGNAL)))

closing = [dict(l) for l in GOOD_JA]
closing[7] = {"speaker": "explainer", "text": "以上、今日のSignalsでした。また次回。"}
check("presenter-style closing rejected",
      any("forbidden phrase" in i for i in lg.ja_quality_issues(closing, SIGNAL)))

anchor = [dict(l) for l in GOOD_JA]
anchor[1] = {"speaker": "explainer", "text": "はい。Appleから訴訟が発表されました。"}
anchor[3] = {"speaker": "explainer", "text": "同じ日に人事も発表されました。"}
check("repeated 発表されました rejected",
      any("news-anchor cadence" in i for i in lg.ja_quality_issues(anchor, SIGNAL)))

check("good conversation still passes v2 gates", lg.ja_quality_issues(GOOD_JA, SIGNAL) == [])

# ---------------------------------------------------------------- v3: prompt directives + question-only style
# v3 changes the PROMPT only (listener: every line ends in a question; explainer: full
# paraphrase, never article wording). Gates are unchanged — these tests pin (a) the prompt
# carries the directives and (b) the stricter v3 style passes the existing gates cleanly.

check("prompt: listener must end every line with a question",
      "すべて質問で終える" in lg.SCRIPT_SYSTEM_JA)
check("prompt: explainer must fully paraphrase (no partial reuse)",
      "そのままでも部分的にも" in lg.SCRIPT_SYSTEM_JA and "完全に自分の言葉" in lg.SCRIPT_SYSTEM_JA)
check("prompt: question-form closing example present",
      "追ったほうがいい話ですか" in lg.SCRIPT_SYSTEM_JA)

# v3.1: keep each utterance concise for audio; don't compress multiple independent facts into one
# over-long line (regression for signal-4 「line 4: too long (93 chars > 90)」). Prompt-only change.
check("prompt: each utterance stays concise / within the line limit",
      "音声で自然に聞こえるよう簡潔" in lg.SCRIPT_SYSTEM_JA and "90文字以内" in lg.SCRIPT_SYSTEM_JA)
check("prompt: do not compress multiple independent facts into one line",
      "複数の独立した事実を1つの発話に詰め込まない" in lg.SCRIPT_SYSTEM_JA)
check("prompt: split facts across sentences or a listener follow-up",
      "文を分ける" in lg.SCRIPT_SYSTEM_JA and "listenerの短い追加質問を挟んで" in lg.SCRIPT_SYSTEM_JA)
check("prompt: preserve facts — do not drop them to satisfy length",
      "文字数のために事実" in lg.SCRIPT_SYSTEM_JA and "省かない" in lg.SCRIPT_SYSTEM_JA)

GOOD_JA_V3 = [
    {"speaker": "listener", "text": "AppleがOpenAIを訴えたって本当ですか?"},
    {"speaker": "explainer", "text": "本当です。火曜日に裁判を起こしました。相手は30人の元社員も含みます。"},
    {"speaker": "listener", "text": "30人も。転職しただけで訴えられるものなんですか?"},
    {"speaker": "explainer", "text": "転職自体ではなく、秘密情報を持ち出した疑いが問題になっています。"},
    {"speaker": "listener", "text": "これ、AI業界全体には何か影響がありますか?"},
    {"speaker": "explainer", "text": "採用のやり方が変わるかもしれません。ライバルからの引き抜きに慎重になりそうです。"},
    {"speaker": "listener", "text": "今後も追ったほうがいい話ですか?"},
    {"speaker": "explainer", "text": "ええ。動きがあればまた話しましょう。"},
]
check("question-only v3 conversation passes existing gates",
      lg.ja_quality_issues(GOOD_JA_V3, SIGNAL) == [])
check("v3 fixture: every listener line is a question",
      all(lg._JA_QUESTION_RE.search(l["text"]) for l in GOOD_JA_V3 if l["speaker"] == "listener"))

# gates are UNCHANGED: the v2-style short non-question listener reaction must still pass
check("gates unchanged: v2-style closing reaction still accepted",
      lg.ja_quality_issues(GOOD_JA, SIGNAL) == [])

# ---------------------------------------------------------------- Azure JA synthesis backend
check("confirmed voice constants", lg.AZURE_VOICE_JA_LISTENER == "ja-JP-NanamiNeural"
      and lg.AZURE_VOICE_JA_EXPLAINER == "ja-JP-KeitaNeural")
_ssml = lg._azure_ssml("30人が対象です <&> テスト", "ja-JP-KeitaNeural")
check("SSML carries the voice name", "name='ja-JP-KeitaNeural'" in _ssml)
check("SSML escapes XML specials", "&lt;&amp;&gt;" in _ssml and "<&>" not in _ssml)
check("SSML is ja-JP single-voice", _ssml.startswith("<speak version='1.0' xml:lang='ja-JP'>")
      and _ssml.count("<voice") == 1)
check("azure synth has synth_line signature parity",
      lg.synth_line_azure.__code__.co_varnames[:4] == lg.synth_line.__code__.co_varnames[:4])

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
    # per-line files -> 1.0s each; the concatenated final -> its byte size, which equals
    # the line count (each stub line is 1 byte), so decoded-final == sum(per-line) -> drift 0.
    # Passed as BOTH dur_fn and final_dur_fn so the real ffmpeg decoded_duration() (which
    # would choke on the 1-byte stub audio) is never invoked in this stubbed e2e.
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
                       final_dur_fn=dur_stub,
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
                       final_dur_fn=dur_stub,
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
                final_dur_fn=dur_stub,
                upload_fn=upload_stub, verify_fn=verify_stub, log=lambda *a: None)
    check("bad JA script aborts generate()", False)
except ValueError as e:
    check("bad JA script aborts generate()", "JA quality gate failed" in str(e))
check("no uploads after JA gate failure", uploaded == [])
m3 = json.load(open(manifest_path))
check("manifest unchanged after JA gate failure",
      json.dumps(m3["editions"][DATE]["1"], sort_keys=True) == json.dumps(sig1, sort_keys=True))

# ---------------------------------------------------------------- speaker → Azure voice mapping
# Prove that in a JA run every listener line synthesizes with Nanami and every explainer
# line with Keita — no third voice, no crossover.
voice_by_speaker = {}
def synth_capture(text, voice, settings, key):
    for l in GOOD_JA:
        if l["text"] == text:
            voice_by_speaker.setdefault(l["speaker"], set()).add(voice)
    return b"x"

lg.generate(DATE, el_key="azure-key", an_key="k",
            listener_voice=lg.AZURE_VOICE_JA_LISTENER,
            explainer_voice=lg.AZURE_VOICE_JA_EXPLAINER,
            lang="ja", llm_fn=llm_stub_ja, synth_fn=synth_capture, dur_fn=dur_stub,
            final_dur_fn=dur_stub,
            upload_fn=upload_stub, verify_fn=verify_stub, log=lambda *a: None)
check("listener lines all synthesize with Nanami",
      voice_by_speaker.get("listener") == {"ja-JP-NanamiNeural"})
check("explainer lines all synthesize with Keita",
      voice_by_speaker.get("explainer") == {"ja-JP-KeitaNeural"})
check("exactly two voices used (no third voice)",
      len(set().union(*voice_by_speaker.values())) == 2)
uploaded.clear()   # restore the post-gate-failure state the next section asserts on

# debug-only failure artifact: a rejected JA dialogue is snapshotted to scratch/ (gitignored)
_art = os.path.join(lg.ROOT, "scratch", f"failed_ja_dialogue_{DATE}_signal1.json")
check("gate failure writes a debug artifact under scratch/", os.path.exists(_art))
if os.path.exists(_art):
    _a = json.load(open(_art, encoding="utf-8"))
    check("artifact records signal id, issues, and all lines with speakers",
          _a.get("signal") == 1 and _a.get("issues") and
          len(_a.get("lines", [])) == len(GOOD_JA) and
          all("speaker" in ln and "text" in ln and "overlap_coverage" in ln for ln in _a["lines"]))
check("debug artifact did not change the abort/upload/manifest behavior",
      uploaded == [] and json.dumps(m3["editions"][DATE]["1"], sort_keys=True) == json.dumps(sig1, sort_keys=True))

print("ALL PASS" if failures == 0 else f"{failures} CHECK(S) FAILED")
sys.exit(1 if failures else 0)
