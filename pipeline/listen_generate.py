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
     Japanese (--lang ja) uses AZURE SPEECH instead of ElevenLabs: requires AZURE_SPEECH_KEY,
     AZURE_SPEECH_REGION (voices default to ja-JP-NanamiNeural listener / ja-JP-KeitaNeural
     explainer; override via LISTENER_VOICE_JA / EXPLAINER_VOICE_JA). No ElevenLabs key needed.
     Azure rate-limit tuning (optional): AZURE_TTS_DELAY (seconds between requests, default 2.0 —
     F0-friendly pacing) and AZURE_TTS_MAX_RETRIES (429 retries per line, default 5).
     JA runs resume per signal: passed signals checkpoint to scratch/ and are reused (after
     gate+drift re-verification) on rerun; LISTEN_JA_RESUME=0 forces full regeneration.

Usage: python3 pipeline/listen_generate.py 2026-06-30 [--lang en|ja]

LANGUAGES: `--lang en` (default) is the production path and is behavior-identical to the
historical EN-only script. `--lang ja` generates natural Japanese narration (NOT literal
translation — see SCRIPT_SYSTEM_JA) into audio/<date>/signal-0N-dialogue-ja.mp3 and MERGES
a `ja` track into the existing manifest entry, never touching `en`. JA is optional
everywhere downstream: promotion (listen-ready) remains EN-only.
"""
import os, re, sys, json, time, subprocess, urllib.request, urllib.error

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
    "1つの発話は音声で自然に聞こえるよう簡潔にし、必ず90文字以内に収める。"
    "複数の独立した事実を1つの発話に詰め込まない — 事実が複数あるときは、explainerが文を分ける(1〜2文)か、"
    "listenerの短い追加質問を挟んで次の発話に回す。"
    "ただし文字数のために事実(数字・固有名詞・出来事)を省かない — 情報は保ったまま発話を分割して短くする。"
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


# ── Azure Speech backend (JA only) ───────────────────────────────────────────────────────────────
# Confirmed voices: listener = ja-JP-NanamiNeural, explainer = ja-JP-KeitaNeural. Same signature
# as synth_line (ElevenLabs) so generate() stays backend-agnostic — the ONLY difference for a JA
# run is which synth_fn main() passes in. EN keeps ElevenLabs untouched.
AZURE_TTS_FORMAT = "audio-24khz-96kbitrate-mono-mp3"
AZURE_VOICE_JA_LISTENER = "ja-JP-NanamiNeural"    # curious listener — calm morning tone
AZURE_VOICE_JA_EXPLAINER = "ja-JP-KeitaNeural"    # calm explainer


def _azure_ssml(text, voice):
    """Minimal SSML for one dialogue line — XML-escaped, single voice, ja-JP."""
    esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (f"<speak version='1.0' xml:lang='ja-JP'>"
            f"<voice name='{voice}'>{esc}</voice></speak>")


# Rate-limit resilience (JA/Azure ONLY — the F0 free tier throttles bursts of per-line requests).
# Tuning knobs are env vars so production behavior is adjustable without code changes; invalid or
# unset values fall back to the defaults below. EN/ElevenLabs synthesis is intentionally untouched.
AZURE_TTS_DELAY_DEFAULT = 2.0        # seconds between consecutive Azure requests (AZURE_TTS_DELAY)
AZURE_TTS_MAX_RETRIES_DEFAULT = 5    # extra attempts after a 429, per line (AZURE_TTS_MAX_RETRIES)
AZURE_TTS_BACKOFF_BASE = 2.0         # backoff when no Retry-After header: 2s, 4s, 8s, 16s, 32s …
AZURE_TTS_BACKOFF_CAP = 60.0         # … capped here
_azure_last_request_ts = None        # module state for inter-request pacing


def _env_num(name, default, cast=float):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = cast(raw)
    except ValueError:
        return default
    return v if v >= 0 else default


def synth_line_azure(text, voice, settings, api_key,
                     _urlopen=urllib.request.urlopen, _sleep=time.sleep, _clock=time.monotonic):
    """Azure Speech REST TTS → MP3 bytes. `settings` is accepted for signature parity with
    synth_line but unused (Azure neural voices need no stability/similarity knobs).

    F0 rate-limit resilience (this backend only):
      • paces requests ≥ AZURE_TTS_DELAY seconds apart (default 2.0; 0 disables);
      • on HTTP 429 waits per the Retry-After header when present, else bounded exponential
        backoff (2s·2^attempt, capped 60s), for at most AZURE_TTS_MAX_RETRIES retries;
      • after the final attempt fails clearly with a RuntimeError naming the attempt count;
      • any non-429 HTTPError raises IMMEDIATELY (unchanged fail-closed behavior).
    The trailing _urlopen/_sleep/_clock parameters are test seams only — never passed in
    production, so positional signature parity with synth_line is preserved."""
    global _azure_last_request_ts
    region = os.environ.get("AZURE_SPEECH_REGION", "").strip()
    if not region:
        raise RuntimeError("AZURE_SPEECH_REGION is required for Azure synthesis")
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    delay = _env_num("AZURE_TTS_DELAY", AZURE_TTS_DELAY_DEFAULT)
    max_retries = _env_num("AZURE_TTS_MAX_RETRIES", AZURE_TTS_MAX_RETRIES_DEFAULT, cast=int)

    for attempt in range(max_retries + 1):
        if delay > 0 and _azure_last_request_ts is not None:
            gap = delay - (_clock() - _azure_last_request_ts)
            if gap > 0:
                _sleep(gap)
        req = urllib.request.Request(url, data=_azure_ssml(text, voice).encode("utf-8"),
                                     method="POST", headers={
            "Ocp-Apim-Subscription-Key": api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": AZURE_TTS_FORMAT,
            "User-Agent": "SignalsListen/1.0"})
        try:
            _azure_last_request_ts = _clock()
            with _urlopen(req, timeout=180) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise                                   # non-429 → fail immediately, unchanged
            if attempt == max_retries:
                raise RuntimeError(
                    f"Azure TTS rate limit (HTTP 429) persisted after {max_retries + 1} attempts — "
                    f"aborting (raise AZURE_TTS_DELAY / wait for the F0 quota window)") from e
            retry_after = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
            try:
                wait = float(retry_after)
            except ValueError:
                wait = 0.0
            if wait <= 0:
                wait = min(AZURE_TTS_BACKOFF_CAP, AZURE_TTS_BACKOFF_BASE * (2 ** attempt))
            print(f"    Azure TTS 429 — retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{max_retries + 1})", file=sys.stderr)
            _sleep(wait)


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
_JA_PASTE_MIN = 15            # min verbatim run (chars) shared with localized.ja to count as a copy
_JA_SENT_COPY_RATIO = 0.80    # a SINGLE long run fails only if it reproduces ≥ this fraction of a
                             # localized.ja SENTENCE (a full-sentence copy). A short factual summary
                             # quotes only a sub-phrase of a long sentence, so its ratio stays low.
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


def _paste_overlaps(text, source, n=_JA_PASTE_MIN):
    """Maximal, NON-overlapping runs of `text` (≥ n chars) that appear verbatim in `source`.
    Each entry is one independent stretch copied from localized.ja."""
    out, i, L = [], 0, len(text)
    while i <= L - n:
        if text[i:i + n] in source:
            j = i + n
            while j <= L and text[i:j] in source:
                j += 1
            out.append(text[i:j - 1])
            i = j - 1                 # resume after this maximal run → runs stay independent
        else:
            i += 1
    return out


def _ja_sentences(source):
    """localized.ja split into individual sentences — the unit a structural copy reproduces.
    Split on Japanese/ASCII sentence terminators and newlines; keep only substantial ones."""
    return [p for p in (s.strip() for s in re.split(r"[。．！？!?\n]", source)) if len(p) >= _JA_PASTE_MIN]


def _is_article_paste(text, source, n=_JA_PASTE_MIN):
    """True when a line is copied from localized.ja rather than paraphrased.

    A single unavoidable factual phrase or a short factual summary quotes only a SUB-PHRASE of a
    localized.ja sentence and must NOT trip the gate — even when that phrase fills much of a short
    line. We flag a paste only when the copy is STRUCTURAL: either the line stitches together two
    or more independent long article runs, or a single run reproduces most of one source SENTENCE
    (≥ _JA_SENT_COPY_RATIO of it — a full sentence copied with at most cosmetic edits)."""
    if len(text) < n or len(source) < n:
        return False
    overlaps = _paste_overlaps(text, source, n)
    if not overlaps:
        return False
    if len(overlaps) >= 2:                                  # stitched from ≥2 article fragments
        return True
    run = overlaps[0].strip("。．！？!?\n 　")               # one run: paste only if it ≈ a full sentence
    if len(run) < n:                                       # left with only punctuation/space → not a copy
        return False
    best = max((len(run) / len(s) for s in _ja_sentences(source) if run in s), default=0.0)
    return best >= _JA_SENT_COPY_RATIO
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
        if _is_article_paste(text, ja_src):
            issues.append(f"line {i}: article text pasted into dialogue "
                          f"(structural copy of localized.ja, not a single fact phrase)")
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
def _dump_failed_ja(date, num, signal, lines, issues):
    """DEBUG-ONLY: best-effort snapshot of a rejected JA dialogue to scratch/ (gitignored) so a
    non-deterministic gate failure can be inspected after the fact. Writes
    scratch/failed_ja_dialogue_<date>_signal<num>.json with the signal id, generated lines +
    speakers, the gate issues, and per-line verbatim-overlap details against localized.ja.

    NEVER raises and changes NO pipeline behavior: it runs only on a failure path that already
    aborts, writes only under scratch/, and swallows any error so it can't mask or alter the
    gate's own ValueError. Returns the path written, or None."""
    try:
        ja_src = _ja_source_strings(signal)
        recs = []
        for i, ln in enumerate(lines, 1):
            text = ln.get("text", "") if isinstance(ln, dict) else ""
            ovs = _paste_overlaps(text, ja_src) if ja_src else []
            covered = sum(len(s) for s in ovs)
            recs.append({
                "index": i,
                "speaker": ln.get("speaker") if isinstance(ln, dict) else None,
                "text": text,
                "overlaps": [{"substring": s, "chars": len(s)} for s in ovs],
                "overlap_runs": len(ovs),
                "overlap_coverage": round(covered / len(text), 4) if text else 0.0,
            })
        artifact = {
            "date": date,
            "signal": num,
            "lang": "ja",
            "issues": issues,
            "localized_ja": (signal.get("localized") or {}).get("ja"),
            "lines": recs,
        }
        scratch = os.path.join(ROOT, "scratch")
        os.makedirs(scratch, exist_ok=True)
        path = os.path.join(scratch, f"failed_ja_dialogue_{date}_signal{num}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return path
    except Exception:
        return None   # debug aid must never break the pipeline


# ── JA per-signal resume checkpoints (scratch-only, gitignored — never committed) ────────────────
# Why: a run is atomic (nothing uploads until 5/5 pass), so one gate-failed signal used to force
# regenerating four perfectly good signals — wasted LLM+TTS calls and more F0 429 exposure.
# A checkpoint snapshots a signal that FULLY passed (gate + synthesis + drift). Reuse is
# fail-closed: every original gate is re-verified at load time, and any doubt → regenerate.
def _ja_resume_enabled():
    return os.environ.get("LISTEN_JA_RESUME", "").strip().lower() not in ("0", "false", "off")


def _ckpt_path(outdir, num, lang):
    return os.path.join(outdir, f"signal-{int(num):02d}-{lang}.checkpoint.json")


def _save_ja_checkpoint(outdir, num, lang, lines, durs, final):
    """Best-effort atomic snapshot after a signal fully passes. Failure to save never
    breaks the run (it only costs a future regeneration)."""
    try:
        path = _ckpt_path(outdir, num, lang)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"lines": [{"speaker": l["speaker"], "text": l["text"]} for l in lines],
                       "durs": [float(d) for d in durs], "final": os.path.basename(final)},
                      f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _load_ja_checkpoint(outdir, num, lang, sig, final_dur_fn):
    """Return (lines, durs, final_path) for an already-passed signal, or None to regenerate.

    Fail-closed re-verification on every load:
      • shape: speaker/text lines, one duration per line;
      • the saved dialogue must STILL pass ja_quality_issues against the CURRENT signal
        fields (a checkpoint from an edited edition regenerates instead of shipping stale);
      • the concatenated MP3 must exist and still pass the drift gate via final_dur_fn.
    Any missing/corrupt/failing condition → None. Never raises."""
    try:
        path = _ckpt_path(outdir, num, lang)
        if not os.path.exists(path):
            return None
        d = json.load(open(path, encoding="utf-8"))
        lines, durs = d.get("lines"), d.get("durs")
        if not (isinstance(lines, list) and isinstance(durs, list) and lines
                and len(lines) == len(durs)
                and all(isinstance(l, dict) and l.get("speaker") in ("listener", "explainer")
                        and isinstance(l.get("text"), str) and l["text"] for l in lines)):
            return None
        if ja_quality_issues(lines, sig):
            return None
        final = os.path.join(outdir, os.path.basename(str(d.get("final", ""))))
        if not os.path.isfile(final):
            return None
        durs = [float(x) for x in durs]
        if abs(final_dur_fn(final) - sum(durs)) > DRIFT_THRESHOLD:
            return None
        return lines, durs, final
    except Exception:
        return None


def generate(date, *, el_key, an_key, listener_voice, explainer_voice, lang="en",
             llm_fn=llm_dialogue, synth_fn=synth_line, dur_fn=ffprobe_duration,
             final_dur_fn=decoded_duration,
             upload_fn=r2_upload, verify_fn=verify_public, log=print):
    """Generate+upload all 5 dialogue clips for `date`, then write the manifest. Raises on any failure
    BEFORE writing the manifest (atomic). Returns the manifest entry dict.

    lang="en" (default) is byte-for-byte the historical behavior (same filenames, same manifest
    replace-write). lang="ja" adds the JA quality gate and MERGES `ja` tracks into the existing
    manifest entry so a previously generated `en` is never touched.

    JA PER-SIGNAL RESUME: each JA signal that fully passes (gate + synthesis + drift) writes a
    scratch checkpoint. If one signal later fails its gate, rerunning the SAME command reuses the
    passed signals' checkpoints (after re-verifying gate + drift at load time) and calls the
    LLM/TTS only for the missing signal — then uploads and writes the manifest for all five as
    usual. Atomicity is unchanged: nothing uploads and no manifest is written until 5/5 pass in
    one run. Set LISTEN_JA_RESUME=0 to force full regeneration. EN never reads or writes
    checkpoints."""
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
        if lang == "ja" and _ja_resume_enabled():
            reused = _load_ja_checkpoint(outdir, num, lang, sig, final_dur_fn)
            if reused:
                lines, durs, final = reused
                caps = [{"speaker": c["speaker"], "text": c["text"], "duration": round(d, 6)}
                        for c, d in zip(lines, durs)]
                entry[str(num)] = {"format": "dialogue",
                                   lang: {"key": key_for(date, num, lang), "gap": 0.0, "captions": caps}}
                to_upload.append((final, key_for(date, num, lang)))
                log(f"  signal {num} [{lang}]: reused checkpoint ({len(caps)} lines, "
                    f"gate+drift re-verified) — no LLM/TTS calls")
                continue
        lines = llm_fn(sig, api_key=an_key, lang=lang) if lang != "en" else llm_fn(sig, api_key=an_key)
        if lang == "ja":
            issues = ja_quality_issues(lines, sig)
            if issues:
                _dump_failed_ja(date, num, sig, lines, issues)   # debug-only; never alters the gate
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
        if lang == "ja":
            _save_ja_checkpoint(outdir, num, lang, lines, durs, final)
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
    an = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not an:
        sys.exit("❌ ANTHROPIC_API_KEY is required")

    if lang == "ja":
        # JA synthesis = Azure Speech (Nanami = listener, Keita = explainer). ElevenLabs is
        # NOT used or required for JA. Voices default to the confirmed pair and may be
        # overridden via env; Azure names are letters/digits/hyphens (ja-JP-NanamiNeural).
        az_key = os.environ.get("AZURE_SPEECH_KEY", "").strip()
        az_region = os.environ.get("AZURE_SPEECH_REGION", "").strip()
        if not az_key or not az_region:
            sys.exit("❌ AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are required for --lang ja")
        listener = os.environ.get("LISTENER_VOICE_JA", "").strip() or AZURE_VOICE_JA_LISTENER
        explainer = os.environ.get("EXPLAINER_VOICE_JA", "").strip() or AZURE_VOICE_JA_EXPLAINER
        for name, v in (("LISTENER_VOICE_JA", listener), ("EXPLAINER_VOICE_JA", explainer)):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", v):
                sys.exit(f"❌ {name} is not a valid Azure voice name ({v!r}) — "
                         "e.g. ja-JP-NanamiNeural, without spaces/quotes")
        generate(date, el_key=az_key, an_key=an, listener_voice=listener,
                 explainer_voice=explainer, lang="ja", synth_fn=synth_line_azure)
    else:
        el = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not el:
            sys.exit("❌ ELEVENLABS_API_KEY is required")
        explainer, listener = EXPLAINER_VOICE, LISTENER_VOICE
        if not listener:
            sys.exit("❌ LISTENER_VOICE required (EXPLAINER_VOICE defaults to %s)" % EXPLAINER_VOICE)
        # Fail with a READABLE message on malformed voice IDs (ElevenLabs IDs are alphanumeric).
        # Without this, a bad character reaches the request URL and http.client dies in
        # putrequest with an opaque traceback.
        for name, v in (("LISTENER_VOICE", listener), ("EXPLAINER_VOICE", explainer)):
            if not v.isalnum():
                sys.exit(f"❌ {name} is not a valid ElevenLabs voice ID ({v!r}) — "
                         "re-paste the repo secret without spaces/newlines/quotes")
        generate(date, el_key=el, an_key=an, listener_voice=listener, explainer_voice=explainer, lang="en")
    if lang == "en":
        print("Next: python3 pipeline/listen_inject_edition.py", date)
    else:
        print(f"JA clips uploaded + manifest merged for {date}. "
              "(Injection of listen.ja into editions comes in a later phase — evaluate audio quality first.)")


if __name__ == "__main__":
    main()
