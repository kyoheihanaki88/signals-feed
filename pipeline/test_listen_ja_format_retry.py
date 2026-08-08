#!/usr/bin/env python3
"""JA format retry — one malformed LLM output must no longer kill the run, and every
rejected output must land where the workflow's artifact glob can find it.

    python3 pipeline/test_listen_ja_format_retry.py

Guards the 2026-08-08 outage (run 31265219459, twice at the same place): Sonnet 5 answered
the solo-narration prompt for signal 3 with something that was not a JSON array,
parse_solo_lines raised "no JSON array in LLM output", there was no retry, and the failure
summary pointed at scratch/failed_ja_dialogue_*.json — which was never written, because the
existing dump helper only covers quality-gate failures, not parse failures.

Pinned here: JA retries format errors up to 3 total attempts with an explicit
JSON-array-only reinstruction; HTTP errors are never retried; every rejected raw output is
saved (date, signal, attempt, model, parse error, raw output — never the API key) under the
exact glob the workflow uploads; EN keeps exactly one attempt with an unchanged payload; and
the checkpoint-reuse and manifest-shape code in generate() is untouched. No network."""

import glob
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listen_generate as lg  # noqa: E402

FAILED = []
FAKE_KEY = "test-key-抜き取り-not-a-secret"


def check(label, ok):
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


SIG = {
    "number": 3,
    "headline": "Regulator opens inquiry into payment fees",
    "summary": "The competition regulator has opened a formal inquiry into card payment fees.",
    "keyTakeaways": ["The inquiry covers fees charged to small retailers."],
    "whyItMatters": "Fees feed directly into consumer prices.",
    "localized": {"ja": {"headline": "決済手数料に調査", "summary": "当局が調査を開始。"}},
}

GOOD_SOLO = ["決済手数料をめぐり、競争当局が正式な調査を始めました。",
             "対象はカード決済にかかる手数料です。",
             "小規模な店舗が支払う手数料も含まれます。",
             "手数料は商品の価格に影響します。",
             "調査の結果しだいで、仕組みが見直される可能性があります。",
             "結論が出るまでには時間がかかる見通しです。",
             "朝の時点で新しい発表はありません。",
             "続報があれば、あらためてお伝えします。"]
GOOD_TEXT = json.dumps(GOOD_SOLO, ensure_ascii=False)
GARBAGE = "承知しました。以下がナレーション台本です。\n\n一文目、二文目 … (JSON配列ではない)"

GOOD_EN = json.dumps([{"speaker": "listener", "text": f"Question {i}?"} if i % 2 else
                      {"speaker": "explainer", "text": f"Answer number {i}."}
                      for i in range(1, 9)])


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def scripted(outputs):
    """urlopen double: pops one scripted assistant text per call; records each Request."""
    reqs = []

    def fake(req, *a, **kw):
        reqs.append(req)
        item = outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(json.dumps(
            {"content": [{"type": "text", "text": item}]}).encode())
    return fake, reqs


def run(outputs, lang="ja"):
    fake, reqs = scripted(list(outputs))
    real = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        try:
            out = lg.llm_dialogue(SIG, api_key=FAKE_KEY, model="claude-sonnet-5", lang=lang)
        except Exception as e:                # noqa: BLE001 — tests inspect the exception
            out = e
    finally:
        urllib.request.urlopen = real
    return out, reqs


# All dumps go to a temp scratch, not the repo's.
_REAL_ROOT = lg.ROOT
TMP = tempfile.mkdtemp(prefix="ja-retry-test-")
lg.ROOT = TMP
lg._JA_RUN_DATE = "2026-08-08"


def dumped():
    return sorted(glob.glob(os.path.join(TMP, "scratch", "failed_ja_dialogue_*.json")))


def clear_dumps():
    for p in dumped():
        os.unlink(p)


print("1) clean first attempt")
out, reqs = run([GOOD_TEXT])
check("succeeds with narrator lines", isinstance(out, list)
      and len(out) == 8 and all(l["speaker"] == "narrator" for l in out))
check("exactly one API call", len(reqs) == 1)
check("no failure artifact written", dumped() == [])

print("\n2) garbage then clean → succeeds on attempt 2")
clear_dumps()
out, reqs = run([GARBAGE, GOOD_TEXT])
check("succeeds", isinstance(out, list) and len(out) == 8)
check("exactly two API calls", len(reqs) == 2)
retry_body = json.loads(reqs[1].data.decode())
first_body = json.loads(reqs[0].data.decode())
retry_msg = retry_body["messages"][0]["content"]
check("retry reinstructs: JSON array only, no fences/markdown/prose",
      "JSON配列のみ" in retry_msg and "コードフェンス" in retry_msg and "Markdown" in retry_msg)
check("retry does not embed the previous broken output", GARBAGE[:12] not in retry_msg)
check("first attempt payload carries no retry note",
      "再指示" not in first_body["messages"][0]["content"])
check("the rejected attempt-1 output was saved even though the run succeeded",
      len(dumped()) == 1 and "_attempt1_" in dumped()[0])

print("\n3) three garbage attempts → fail-closed")
clear_dumps()
out, reqs = run([GARBAGE, GARBAGE, GARBAGE])
check("raises ValueError", isinstance(out, ValueError))
check("error names the attempt count and the artifact location",
      "3 attempts" in str(out) and "failed_ja_dialogue" in str(out))
check("exactly three API calls, then stop", len(reqs) == 3)
check("one artifact per attempt", len(dumped()) == 3
      and all(f"_attempt{i}_" in p for i, p in enumerate(dumped(), 1)))

print("\n4) artifact contents")
rec = json.load(open(dumped()[0], encoding="utf-8"))
check("artifact carries date/signal/attempt/model/parse_error/raw_output",
      rec["date"] == "2026-08-08" and rec["signal"] == 3 and rec["attempt"] == 1
      and rec["model"] == "claude-sonnet-5" and "JSON array" in rec["parse_error"]
      and rec["raw_output"].startswith("承知しました"))
blob = "".join(open(p, encoding="utf-8").read() for p in dumped())
check("no secret in any artifact", FAKE_KEY not in blob and "x-api-key" not in blob)
check("filenames match the workflow glob scratch/failed_ja_dialogue_*.json",
      all(os.path.basename(p).startswith("failed_ja_dialogue_") and p.endswith(".json")
          for p in dumped()))

print("\n5) sampling parameters absent on every attempt")
clear_dumps()
_, reqs = run([GARBAGE, GARBAGE, GARBAGE])
for i, r in enumerate(reqs, 1):
    body = json.loads(r.data.decode())
    check(f"attempt {i}: no temperature/top_p/top_k/thinking",
          all(k not in body for k in ("temperature", "top_p", "top_k", "thinking")))

print("\n6) HTTP errors are never retried")
clear_dumps()
err = urllib.error.HTTPError(lg.ANTHROPIC_URL, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
out, reqs = run([err, GOOD_TEXT])
check("raises RuntimeError immediately", isinstance(out, RuntimeError))
check("only one API call was made", len(reqs) == 1)
check("no format artifact for a transport failure", dumped() == [])

print("\n7) EN path unchanged — one attempt, no retry, no dump")
clear_dumps()
out, reqs = run([GOOD_EN], lang="en")
check("EN success still works", isinstance(out, list) and len(out) == 8)
check("EN: one call", len(reqs) == 1)
en_body = json.loads(reqs[0].data.decode())
check("EN payload shape unchanged (max_tokens 900, EN system prompt, no sampling keys)",
      en_body["max_tokens"] == 900 and en_body["system"] == lg.SCRIPT_SYSTEM
      and all(k not in en_body for k in ("temperature", "top_p", "top_k")))
out, reqs = run([GARBAGE, GOOD_EN], lang="en")
check("EN garbage fails immediately with no retry",
      isinstance(out, ValueError) and len(reqs) == 1)
check("EN failure writes no JA artifact", dumped() == [])

print("\n8+9) generate() checkpoint reuse and manifest shape untouched (source pins)")
src = Path(lg.__file__).read_text(encoding="utf-8")
gen = src.split("def generate(", 1)[1]
check("checkpoint reuse still runs before any LLM call",
      gen.find("_load_ja_checkpoint") < gen.find("llm_fn(sig"))
check("reused checkpoint still skips generation via continue",
      "no LLM/TTS calls" in gen and "continue" in gen.split("no LLM/TTS calls")[0][-500:] or
      "continue" in gen[gen.find("_load_ja_checkpoint"):gen.find("llm_fn(sig")])
check("manifest entry shape unchanged",
      '{"format": "dialogue",' in gen and '"gap": 0.0, "captions": caps}' in gen)
check("quality gate still raises without retry",
      "ja_solo_quality_issues(lines, sig)" in gen and "JA quality gate failed" in gen)

lg.ROOT = _REAL_ROOT

print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
