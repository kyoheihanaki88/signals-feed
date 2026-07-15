#!/usr/bin/env python3
"""
Listen automation — generate EN dialogue (audio + captions) for ALL five signals of one edition,
upload to R2 (date-scoped), and write the listen_manifest.json entry. Edition/​latest injection is a
separate step (listen_inject_edition.py), so this script never edits the feed if anything fails.

Per signal: LLM writes a calm two-speaker dialogue grounded ONLY in that signal's article fields →
ElevenLabs synthesizes each line with two voices → lines are concatenated directly (no gaps → gap 0.0)
→ ffprobe measures each line + the final clip → drift gate (|sum − final| ≤ 0.25s).

ATOMIC / FAIL-CLOSED: every signal must succeed (scripts, audio, drift, upload, HTTP 200) before the
manifest is written. Any failure raises → no manifest change → no listen.en is ever injected. So the
text feed (published separately) is never blocked, and mismatched/partial Listen data can't ship.

Audio is written under scratch/ (gitignored) and pushed to R2 remote-only — never committed to git.

Env: ELEVENLABS_API_KEY, ANTHROPIC_API_KEY (required); EXPLAINER_VOICE, LISTENER_VOICE;
     SIGNALS_LISTEN_MODEL (optional). R2 upload uses `wrangler` (CLOUDFLARE_API_TOKEN in CI).
     Japanese (--lang ja) additionally requires EXPLAINER_VOICE_JA, LISTENER_VOICE_JA.

Usage: python3 pipeline/listen_generate.py 2026-06-30 [--lang en|ja]

LANGUAGES: `--lang en` (default) is the production path and is behavior-identical to the
historical EN-only script. `--lang ja` generates natural Japanese narration (NOT literal
translation — see SCRIPT_SYSTEM_JA) into audio/<date>/signal-0N-dialogue-ja.mp3 and MERGES
a `ja` track into the existing manifest entry, never touching `en`. JA is optional
everywhere downstream: promotion (listen-ready) remains EN-only.
"""
import os, re, sys, json, subprocess, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(HERE, "listen_manifest.json")
OUT_BASE = os.path.join(ROOT, "scratch", "listen_audio_test", "dialogue_proto")   # gitignored

R2_BASE = "https://pub-95a7558772874b48a645ad0c1604d784.r2.dev"
R2_BUCKET = "signals-audio"
# Caption-vs-audio drift gate. MP3 duration probing (ffprobe) and direct MP3 concatenation
# both round at frame/container granularity, so a few hundredths of a second of drift is
# normal encoder noise, not a content mismatch (a real mismatch — wrong/missing line —
# drifts by whole seconds). 0.50s stays strict enough to catch those while not aborting
# runs over rounding (seen: 0.260s on a perfectly good clip).
DRIFT_THRESHOLD = 0.50

EL_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
EL_MODEL = "eleven_multilingual_v2"
# .strip() + `or`: a secret pasted with a trailing newline/space, or set-but-empty, must
# never reach the URL — whitespace in EL_URL.format(voice=…) makes http.client reject the
# request at putrequest with a cryptic traceback before any network call.
EXPLAINER_VOICE = os.environ.get("EXPLAINER_VOICE", "").strip() or "DXFkLCBUTmvXpp2QwZjA"
LISTENER_VOICE = os.environ.get("LISTENER_VOICE", "").strip()
EXPLAINER_SETTINGS = {"stability": 0.50, "similarity_boost": 0.85, "style": 0.12}
LISTENER_SETTINGS = {"stability": 0.38, "similarity_boost": 0.85, "style": 0.12}

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# `or` (not the .get default) so an EMPTY env var — e.g. the workflow passing an unset
# repo secret — still falls back instead of calling the API with model="".
SCRIPT_MODEL = os.environ.get("SIGNALS_LISTEN_MODEL") or "claude-3-5-sonnet-20241022"
SCRIPT_SYSTEM = (
    "You write a short, calm two-person Listen dialogue for a morning news app. Two speakers: "
    "'listener' (curious, asks what a regular person would ask) and 'explainer' (calm, concise, "
    "informed). Rules: 8–12 short spoken lines; warm and quiet, never dramatic or comedic; ground "
    "EVERY line ONLY in the provided story fields — invent no facts, names, or numbers; handle "
    "sensitive topics (violence, death) soberly and without graphic detail; no headings, no 'Signal "
    "N'. Output ONLY a JSON array of objects: [{\"speaker\":\"listener|explainer\",\"text\":\"...\"}]."
)

# Japanese narration (--lang ja) v2 — a REAL two-person conversation, not narration.
# NOT a translation prompt: the model speaks naturally in Japanese, grounded in the English
# fields plus the edition's own localized.ja text (japanese_reference) so terminology matches
# what JP-mode readers see in the app. The conversation target: two intelligent friends
# discussing the morning's news (Apple-Podcast morning-briefing feel), never an AI anchor.
SCRIPT_SYSTEM_JA = (
    "あなたは朝のニュースアプリ「Signals」のための、日本語の朝の会話番組の台本作家です。"
    "知的な友人2人が今日のニュースを一緒に話す、本物の会話を書きます。ニュース番組の"
    "アナウンサー原稿ではありません。"
    "役割 — listener: まだ記事を読んでいない、好奇心のある聞き手。会話を進める側。"
    "listenerの発言は**すべて質問で終える**こと。相槌・感想・要約だけの行は書かない。"
    "驚きや納得を入れる場合も、同じ行の中で必ず次の質問につなげる"
    "(例:「30人も。それって転職しただけで訴えられるものなんですか?」)。"
    "質問は「なぜ重要?」「私たちの生活にどう関係する?」「次に何が起きる?」の方向で、"
    "記事の事実を自分から先回りして述べない。"
    "explainer: 質問に答える側。記事や japanese_reference の文を、そのままでも部分的にも"
    "使わない。語順を入れ替えただけの言い換えも不可 — 友人に口頭で説明するときの"
    "**完全に自分の言葉**に置き換える(事実・名前・数字だけを保ち、文はゼロから作る)。"
    "1回の発言は1〜2文まで。"
    "構造(この流れに従う): ①listenerが好奇心のある質問で切り出す → ②explainerが核心を短く答える"
    " → ③listenerが踏み込んだ質問か身近な視点の質問で返す → ④explainerが背景や「なぜ重要か」を足す"
    " → ⑤(任意)短い自然な締めの往復(締めもlistenerは質問形で: 例「これは今後も追ったほうがいい話ですか?」)。"
    "listenerの発言は最低2回、全て質問で終えること。"
    "8〜12行。ですます調で、1文は短く(目安35文字以内)、通勤中に聞いて理解できる平易な言葉。"
    "固有名詞(企業名・人名・地名)は提供された表記のまま、英語名は英語のまま(例: Apple, OpenAI)。"
    "数字・日付・金額は正確に保持。提供されたstory fields(英語原文と japanese_reference)にある"
    "事実だけを使い、事実・名前・数字を発明しない。深刻な話題(暴力・死)は淡々と扱う。"
    "禁止: 一人語り/explainerの長い段落/記事文の貼り付け/「今日は〜についてお話しします」型の司会進行/"
    "「〜が発表されました」の繰り返し/「今日のSignalsでした」「お届けしました」「また次回」などの"
    "番組風の締め(会話は友人同士の自然な一言で終える)/煽り・感嘆・ドラマ化・冗談・「速報」的言い回し/"
    "直訳調(例:「〜についての発表をしました」)/見出しや「シグナルN」などのラベル。"
    "出力はJSON配列のみ: [{\"speaker\":\"listener|explainer\",\"text\":\"...\"}]"
)


# ── external calls (injectable for tests) ───────────────────────────────────────────────────────
def llm_dialogue(signal, *, api_key, model=SCRIPT_MODEL, lang="en"):
    fields = {k: signal.get(k) for k in ("headline", "summary", "keyTakeaways", "whyItMatters") if signal.get(k)}
    if lang == "ja":
        ja_ref = (signal.get("localized") or {}).get("ja")
        if ja_ref:
            fields["japanese_reference"] = ja_ref     # ground JA terminology in the app's own JP text
    system = SCRIPT_SYSTEM_JA if lang == "ja" else SCRIPT_SYSTEM
    body = json.dumps({"model": model, "max_tokens": 1200 if lang == "ja" else 900,
                       "temperature": 0.5, "system": system,
                       "messages": [{"role": "user", "content": json.dumps(fields, ensure_ascii=False)}]}).encode()
    req = urllib.request.Request(ANTHROPIC_URL, data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode())
    text = "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text").strip()
    return parse_dialogue(text)


def synth_line(text, voice, settings, api_key):
    body = json.dumps({"text": text, "model_id": EL_MODEL,
                       "voice_settings": {**settings, "use_speaker_boost": True}}).encode()
    req = urllib.request.Request(EL_URL.format(voice=voice), data=body, method="POST", headers={
        "xi-api-key": api_key, "accept": "audio/mpeg", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def ffprobe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nokey=1:noprint_wrappers=1", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return round(float(out), 6)


def decoded_duration(path):
    """Duration of the ACTUAL decoded audio of `path`, from the decoded PCM sample
    count — independent of container size/bitrate estimation.

    The Listen final clip is a RAW byte concatenation of per-line MP3s, each of which
    keeps its own ID3v2 tag + LAME/"Info" header frame. ffprobe's format=duration on
    that blob is a size*8/bitrate ESTIMATE that counts those embedded header bytes as
    audio and OVER-reports (the historical false-positive drift). Decoding to signed
    16-bit mono PCM and dividing the byte count by (sample_rate * 2) yields the true
    audio length, which matches the sum of the per-line durations.

    Fail-closed: raises if ffmpeg cannot decode the file, so the drift gate aborts
    rather than silently skipping the check."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0",
             "-ac", "1", "-ar", "44100", "-f", "s16le", "pipe:1"],
            capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = getattr(e, "stderr", b"") or b""
        raise RuntimeError(f"decoded-duration probe failed for {path}: "
                           f"{detail.decode('utf-8', 'replace')[:200]}") from e
    nbytes = len(proc.stdout)
    if nbytes == 0:
        raise RuntimeError(f"decoded-duration probe produced no audio for {path}")
    return round(nbytes / (44100 * 2), 6)   # mono s16le → 2 bytes/sample


def r2_upload(local_path, key):
    subprocess.run(["wrangler", "r2", "object", "put", f"{R2_BUCKET}/{key}",
                    "--file", local_path, "--content-type", "audio/mpeg", "--remote"], check=True)


# Cloudflare fronts r2.dev and can 403 "bot-looking" requests (python-urllib UA) while
# serving browsers fine — send a browser-ish UA on verification requests only.
_VERIFY_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Signals-ListenVerify/1.0"


def _status(url, method="GET", headers=None, read_byte=False):
    h = {"User-Agent": _VERIFY_UA, **(headers or {})}
    req = urllib.request.Request(url, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if read_byte:
                r.read(1)          # urllib streams lazily — 1 byte proves the body serves
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def verify_public(url):
    """Is the uploaded object publicly retrievable?

    R2 public (r2.dev) endpoints can 403 both HEAD and Range GETs while serving a plain
    browser GET perfectly (browser-confirmed). Ladder, cheapest first:
      1. HEAD                     → 200 = success
      2. GET with Range: bytes=0-0 → 200/206 = success
      3. plain GET, read 1 byte    → 200 = success (urllib streams lazily, so reading a
         single byte verifies the body without downloading the whole MP3)
    Fail-closed if all three fail.

    Returns (ok: bool, detail: str) — detail carries every attempted status for the logs,
    e.g. "HEAD 403 → GET/range 403 → GET 200".
    """
    head = _status(url, method="HEAD")
    if head == 200:
        return True, "HEAD 200"
    ranged = _status(url, method="GET", headers={"Range": "bytes=0-0"})
    if ranged in (200, 206):
        return True, f"HEAD {head} → GET/range {ranged}"
    plain = _status(url, method="GET", read_byte=True)
    ok = plain == 200
    return ok, f"HEAD {head} → GET/range {ranged} → GET {plain}"


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────
def parse_dialogue(text):
    """Parse the LLM output into a validated [{speaker,text}] list, or raise."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1].lstrip("json").strip() if "```" in s else s
    start, end = s.find("["), s.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("no JSON array in LLM output")
    arr = json.loads(s[start:end + 1])
    out = []
    for c in arr:
        sp = str(c.get("speaker", "")).lower().strip()
        tx = str(c.get("text", "")).strip()
        if sp not in ("listener", "explainer") or not tx:
            raise ValueError(f"bad caption: {c!r}")
        out.append({"speaker": sp, "text": tx})
    if not (6 <= len(out) <= 14):
        raise ValueError(f"unexpected line count {len(out)}")
    return out


def key_for(date, number, lang="en"):
    return f"audio/{date}/signal-{int(number):02d}-dialogue-{lang}.mp3"


# ── JA quality gate (isolated: never runs on the EN path) ───────────────────────────────────────
# Fail-closed like everything else here: any issue raises upstream → no upload, no manifest.

_JA_CHARS_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")   # hiragana/katakana/kanji
_JA_FORBIDDEN = (
    "についての発表をしました",   # literal-translation tell
    "についての報告をしました",
    "による発表によると",
    "することが可能です",
    "であるということです",
    "速報", "衝撃", "驚愕", "必見", "！！",
    # presenter-style hosting / closing (v2 — this is a conversation, not a program)
    "についてお話しします", "お伝えします", "お届けします", "お届けしました",
    "今日のSignalsでした", "また次回", "ご清聴", "お相手は",
)
_JA_MAX_LINE_CHARS = 90
# v2 conversation-structure knobs
_JA_QUESTION_RE = re.compile(r"[？?]|か(?:ね|な)?[。」]?\s*$")   # spoken-question detection
_JA_MIN_LISTENER_TURNS = 2
_JA_MIN_QUESTIONS = 2
_JA_PASTE_MIN = 15            # shared-substring length that marks article text pasted into a line
_JA_AIZUCHI_MAX = 9           # a non-question listener line this short is a bare aizuchi
_JA_LISTENER_STMT_MAX = 40    # non-question listener lines must stay short (reactions, not summaries)
_REPEAT_ANNOUNCE = "発表されました"


def _ja_source_strings(signal):
    """All localized.ja strings of the signal (headline/summary/takeaways/why) as one blob —
    the reference text a pasted-not-spoken line would share long substrings with."""
    ja = (signal.get("localized") or {}).get("ja") or {}
    parts = []
    for v in ja.values():
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts += [x for x in v if isinstance(x, str)]
    return "\n".join(parts)


def _shares_long_substring(text, source, n=_JA_PASTE_MIN):
    if len(text) < n or len(source) < n:
        return False
    return any(text[i:i + n] in source for i in range(len(text) - n + 1))
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _latin_tokens(text):
    """Proper-noun-ish latin tokens (Apple, OpenAI, iOS…) that must exist in the source."""
    return re.findall(r"[A-Za-z][A-Za-z0-9.&'\-]{2,}", text)


def _digit_runs(text):
    return re.findall(r"\d+", text.translate(_FW_DIGITS))


# ── numeric grounding across notation systems ────────────────────────────────────────────────────
# The source states figures in mixed notations: ASCII/full-width digits ("60-day"), English
# number words ("two tankers", "third night"), and Japanese kanji numerals ("二隻", "八名",
# "六十日間"). The JA dialogue naturally verbalizes the SAME figures in Arabic digits ("2隻",
# "8名", "60日"). Comparing only ASCII digit runs made the grounded set collapse to whatever the
# source happened to spell with digits (often just one value), so correctly-grounded Arabic digits
# in the narration were wrongly flagged "ungrounded". We fix that by normalizing the source's own
# number words / kanji to canonical Arabic strings and ADDING them to the grounded set — the
# fail-closed rule (a spoken number must appear in the source set) is unchanged, so invented
# numbers are still caught. Support is intentionally narrow + auditable: fixed word/kanji
# dictionaries and a small kanji unit-combiner, NOT a general natural-language number parser.
_EN_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    # ordinals that name a count in headlines/takeaways ("third consecutive night")
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_KANJI_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNIT = {"十": 10, "百": 100, "千": 1000}
_KANJI_NUM_RE = re.compile(r"[〇零一二三四五六七八九十百千]+")


def _kanji_numbers(text):
    """Canonical Arabic strings for kanji numerals in `text`. Handles single digits
    (二→'2', 八→'8') and unit compounds (十八→'18', 六十→'60', 二百→'200') by combining
    unit chars — so a compound grounds only its combined value, never its parts (六十→'60'
    only, not '6'/'10'). A bare multi-character digit run with no unit (e.g. 二〇二六) is
    positional and outside the observed source formats, so it is skipped rather than
    mis-valued."""
    out = set()
    for span in _KANJI_NUM_RE.findall(text):
        if not any(ch in _KANJI_UNIT for ch in span) and len(span) > 1:
            continue
        total, cur = 0, 0
        for ch in span:
            if ch in _KANJI_DIGIT:
                cur = _KANJI_DIGIT[ch]
            else:  # a unit char (十/百/千) — every span char is digit-or-unit by the regex
                total += (cur or 1) * _KANJI_UNIT[ch]
                cur = 0
        total += cur
        if total:
            out.add(str(total))
    return out


def _grounded_source_numbers(src):
    """Every number the source expresses, in canonical Arabic-digit form, across ASCII/
    full-width digits, simple English number words, and kanji numerals. A superset of the
    old digit-only set — never smaller — so numeric grounding stays fail-closed."""
    g = set(_digit_runs(src))
    low = src.lower()
    for word, val in _EN_NUM_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            g.add(str(val))
    return g | _kanji_numbers(src)


def ja_quality_issues(lines, signal):
    """Issue strings for a JA dialogue (empty list = clean). Checks are JA-only by design:
    format/speakers/line-count are already enforced by parse_dialogue for both languages.

    v2 adds CONVERSATION-STRUCTURE checks: this must read as two intelligent friends
    discussing the news — a curious listener who has NOT read the article driving with
    questions, an explainer answering in their own words — never a narrator with aizuchi,
    never article text chopped into turns, never a presenter-style closing."""
    issues = []
    src = json.dumps({k: signal.get(k) for k in
                      ("headline", "summary", "keyTakeaways", "whyItMatters", "localized")},
                     ensure_ascii=False).translate(_FW_DIGITS)
    src_lower = src.lower()
    src_digits = _grounded_source_numbers(src)   # digits + English words + kanji numerals
    ja_src = _ja_source_strings(signal)

    # ── conversation shape (v2) ──────────────────────────────────────────────
    turns = []
    for line in lines:
        if not turns or turns[-1] != line["speaker"]:
            turns.append(line["speaker"])
    if turns.count("listener") < _JA_MIN_LISTENER_TURNS:
        issues.append(f"only {turns.count('listener')} listener turn(s) — a conversation needs ≥{_JA_MIN_LISTENER_TURNS}")
    if turns.count("explainer") < 2:
        issues.append(f"only {turns.count('explainer')} explainer turn(s) — a conversation needs ≥2")
    questions = sum(1 for l in lines
                    if l["speaker"] == "listener" and _JA_QUESTION_RE.search(l["text"]))
    if questions < _JA_MIN_QUESTIONS:
        issues.append(f"listener asks only {questions} question(s) — needs ≥{_JA_MIN_QUESTIONS} (curiosity drives the talk)")
    announce = sum(l["text"].count(_REPEAT_ANNOUNCE) for l in lines)
    if announce >= 2:
        issues.append(f"'{_REPEAT_ANNOUNCE}' repeated {announce}× — news-anchor cadence, not conversation")

    run_speaker, run_len = None, 0
    for i, line in enumerate(lines, 1):
        text = line["text"]
        # ── per-line conversation checks (v2) ────────────────────────────────
        is_q = bool(_JA_QUESTION_RE.search(text))
        if line["speaker"] == "listener" and not is_q:
            # The listener hasn't read the article: reactions stay short and fact-free.
            if len(text) <= _JA_AIZUCHI_MAX:
                issues.append(f"line {i}: bare aizuchi listener line ({text!r})")
            elif (len(text) > _JA_LISTENER_STMT_MAX or _digit_runs(text)
                  or len(_latin_tokens(text)) >= 2):
                issues.append(f"line {i}: listener states article facts / summarizes instead of asking ({text[:30]!r})")
        if _shares_long_substring(text, ja_src):
            issues.append(f"line {i}: article text pasted into dialogue (≥{_JA_PASTE_MIN}-char overlap with localized.ja)")
        # natural Japanese, not English passthrough
        if not _JA_CHARS_RE.search(text):
            issues.append(f"line {i}: no Japanese characters ({text[:30]!r})")
        # spoken-length ceiling (short sentences, listenable while commuting)
        if len(text) > _JA_MAX_LINE_CHARS:
            issues.append(f"line {i}: too long ({len(text)} chars > {_JA_MAX_LINE_CHARS})")
        # literal-translation / sensational tells
        for pat in _JA_FORBIDDEN:
            if pat in text:
                issues.append(f"line {i}: forbidden phrase {pat!r}")
        # grounding — numbers must exist in the source fields
        for d in _digit_runs(text):
            if d not in src_digits:
                issues.append(f"line {i}: ungrounded number {d!r}")
        # grounding — latin proper nouns must exist in the source fields
        for tok in _latin_tokens(text):
            if tok.lower() not in src_lower:
                issues.append(f"line {i}: ungrounded name {tok!r}")
        # speaker alternation — never the same voice three times in a row
        if line["speaker"] == run_speaker:
            run_len += 1
            if run_len >= 3:
                issues.append(f"line {i}: speaker {run_speaker!r} three times in a row")
        else:
            run_speaker, run_len = line["speaker"], 1
    return issues


# ── orchestration ───────────────────────────────────────────────────────────────────────────────
def generate(date, *, el_key, an_key, listener_voice, explainer_voice, lang="en",
             llm_fn=llm_dialogue, synth_fn=synth_line, dur_fn=ffprobe_duration,
             final_dur_fn=decoded_duration,
             upload_fn=r2_upload, verify_fn=verify_public, log=print):
    """Generate+upload all 5 dialogue clips for `date`, then write the manifest. Raises on any failure
    BEFORE writing the manifest (atomic). Returns the manifest entry dict.

    lang="en" (default) is byte-for-byte the historical behavior (same filenames, same manifest
    replace-write). lang="ja" adds the JA quality gate and MERGES `ja` tracks into the existing
    manifest entry so a previously generated `en` is never touched."""
    edition = os.path.join(ROOT, "editions", f"{date}.json")
    feed = json.load(open(edition, encoding="utf-8"))
    if feed.get("date") != date:
        raise ValueError(f"{edition} internal date {feed.get('date')!r} != {date}")
    signals = feed.get("signals", [])
    if len(signals) != 5:
        raise ValueError(f"expected 5 signals, found {len(signals)}")

    outdir = os.path.join(OUT_BASE, date)
    os.makedirs(outdir, exist_ok=True)
    entry, to_upload = {}, []

    # 1) scripts + audio + drift — all must pass before any upload.
    for sig in signals:
        num = sig["number"]
        lines = llm_fn(sig, api_key=an_key, lang=lang) if lang != "en" else llm_fn(sig, api_key=an_key)
        if lang == "ja":
            issues = ja_quality_issues(lines, sig)
            if issues:
                raise ValueError(f"signal {num} JA quality gate failed:\n  " + "\n  ".join(issues))
        parts, durs = [], []
        for i, c in enumerate(lines, 1):
            voice = explainer_voice if c["speaker"] == "explainer" else listener_voice
            settings = EXPLAINER_SETTINGS if c["speaker"] == "explainer" else LISTENER_SETTINGS
            line_name = f"sig{num}-line-{i:02d}.mp3" if lang == "en" else f"sig{num}-line-{i:02d}-{lang}.mp3"
            p = os.path.join(outdir, line_name)
            open(p, "wb").write(synth_fn(c["text"], voice, settings, el_key))
            durs.append(dur_fn(p))
            parts.append(p)
        final = os.path.join(outdir, f"signal-{int(num):02d}-dialogue-{lang}.mp3")
        with open(final, "wb") as o:
            for p in parts:
                o.write(open(p, "rb").read())
        # Measure the concatenated clip by DECODED audio (sample count), not ffprobe's
        # size/bitrate estimate: the raw concat embeds each segment's ID3/Info header
        # mid-stream, which inflates the estimate but not the real audio. Sum of per-line
        # durations stays the reference; final_dur_fn raises on decode failure (fail-closed).
        drift = abs(final_dur_fn(final) - sum(durs))
        if drift > DRIFT_THRESHOLD:
            raise ValueError(f"signal {num} drift {drift:.3f}s > {DRIFT_THRESHOLD}s — aborting")
        caps = [{"speaker": c["speaker"], "text": c["text"], "duration": round(d, 6)}
                for c, d in zip(lines, durs)]
        entry[str(num)] = {"format": "dialogue",
                           lang: {"key": key_for(date, num, lang), "gap": 0.0, "captions": caps}}
        to_upload.append((final, key_for(date, num, lang)))
        log(f"  signal {num} [{lang}]: {len(caps)} lines, drift {drift:.3f}s OK")

    # 2) upload all, then require every object to be publicly retrievable (fail-closed).
    #    HEAD 200, or GET/range 200/206 when HEAD is refused (r2.dev quirk) — see verify_public.
    for final, key in to_upload:
        upload_fn(final, key)
    for _, key in to_upload:
        url = f"{R2_BASE}/{key}"
        ok, detail = verify_fn(url)
        if not ok:
            raise RuntimeError(f"R2 object not publicly retrievable ({detail}): {url} — "
                               "aborting, manifest unchanged")
        log(f"  OK ({detail}) {url}")

    # 3) only now write the manifest entry.
    #    en: replace the whole date entry — byte-identical to the historical behavior.
    #    ja (any non-en): MERGE per-signal tracks into the existing entry so `en` (already
    #    generated, uploaded, and possibly injected/promoted) is never modified or lost.
    data = json.load(open(MANIFEST, encoding="utf-8"))
    editions = data.setdefault("editions", {})
    if lang == "en" or not isinstance(editions.get(date), dict):
        editions[date] = entry
    else:
        existing = editions[date]
        for num, tracks in entry.items():
            cur = existing.get(num)
            if not isinstance(cur, dict):
                existing[num] = tracks
                continue
            for k, v in tracks.items():
                if k != "format":
                    cur[k] = v          # add/refresh this language only; en untouched
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log(f"✓ wrote listen_manifest.json entry for {date} (5 signals, lang={lang})")
    return entry


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    lang = "en"
    if "--lang" in sys.argv:
        i = sys.argv.index("--lang")
        lang = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        args = [a for a in args if a != lang]
    if len(args) != 1 or lang not in ("en", "ja"):
        sys.exit("usage: python3 pipeline/listen_generate.py <YYYY-MM-DD> [--lang en|ja]")
    date = args[0]
    el = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    an = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not el or not an:
        sys.exit("❌ ELEVENLABS_API_KEY and ANTHROPIC_API_KEY are required")

    if lang == "ja":
        # JA voices come ONLY from env — no hardcoded fallbacks (fail loudly if unset).
        explainer = os.environ.get("EXPLAINER_VOICE_JA", "").strip()
        listener = os.environ.get("LISTENER_VOICE_JA", "").strip()
        if not explainer or not listener:
            sys.exit("❌ EXPLAINER_VOICE_JA and LISTENER_VOICE_JA are required for --lang ja")
        checks = (("LISTENER_VOICE_JA", listener), ("EXPLAINER_VOICE_JA", explainer))
    else:
        explainer, listener = EXPLAINER_VOICE, LISTENER_VOICE
        if not listener:
            sys.exit("❌ LISTENER_VOICE required (EXPLAINER_VOICE defaults to %s)" % EXPLAINER_VOICE)
        checks = (("LISTENER_VOICE", listener), ("EXPLAINER_VOICE", explainer))
    # Fail with a READABLE message on malformed voice IDs (ElevenLabs IDs are alphanumeric).
    # Without this, a bad character reaches the request URL and http.client dies in
    # putrequest with an opaque traceback.
    for name, v in checks:
        if not v.isalnum():
            sys.exit(f"❌ {name} is not a valid ElevenLabs voice ID ({v!r}) — "
                     "re-paste the repo secret without spaces/newlines/quotes")
    generate(date, el_key=el, an_key=an, listener_voice=listener, explainer_voice=explainer, lang=lang)
    if lang == "en":
        print("Next: python3 pipeline/listen_inject_edition.py", date)
    else:
        print(f"JA clips uploaded + manifest merged for {date}. "
              "(Injection of listen.ja into editions comes in a later phase — evaluate audio quality first.)")


if __name__ == "__main__":
    main()
