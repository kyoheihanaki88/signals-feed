#!/usr/bin/env python3
"""JA output budget — retries must escalate max_tokens, and artifacts must say why.

    python3 pipeline/test_listen_ja_output_budget.py

Guards the 2026-08-08 outage (run 31272374419, signal 5, three failures at a flat 1200):
Claude Sonnet 5 runs adaptive thinking BY DEFAULT and max_tokens caps thinking + text, and
its new tokenizer yields ~30% more tokens for the same text (docs: What's new in Claude
Sonnet 5). Signal 5's thinking consumed the whole 1200 budget — attempts 1–2 returned an
EMPTY text block, attempt 3 cut off mid-array — and the retry loop reused the same ceiling,
so no retry could ever succeed. The artifacts also discarded stop_reason/usage, so the
budget exhaustion was indistinguishable from a formatting whim.

Pinned here: the JA budget ladder 1200 → 2400 → 4800; success once the budget allows it;
stop_reason/usage/content_block_types in every failure artifact with no secret and no
thinking content; HTTP errors never retried; EN untouched at a flat 900; and the
checkpoint/manifest code unchanged. No network."""

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
THINKING_MARKER = "THINKING-CONTENT-MUST-NEVER-BE-SAVED"


def check(label, ok):
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


SIG = {
    "number": 5,
    "headline": "The White House's plan to vet potentially dangerous AI is cloaked in secrecy",
    "summary": "So far, the White House is keeping details of the framework private.",
    "keyTakeaways": ["Staff from major AI companies attended a private meeting."],
    "whyItMatters": "Discussions began after a model release was withheld.",
    "localized": {"ja": {"headline": "AI審査の枠組み、非公開のまま", "summary": "詳細は不明。"}},
}

GOOD_SOLO = ["火曜日、主要なAI企業の担当者がホワイトハウスに集まりました。",
             "議題は、危険性のあるAIを事前に審査する枠組みです。",
             "その中身は、いまも詳しく公表されていません。",
             "きっかけは今年4月のある決定でした。",
             "Anthropicが新モデルの公開を見送ったのです。",
             "審査の透明性を求める声は強まっています。",
             "一方、企業側には非公開を望む事情もあります。",
             "議論はまだ始まったばかりです。"]
GOOD_TEXT = json.dumps(GOOD_SOLO, ensure_ascii=False)
TRUNCATED = GOOD_TEXT[:GOOD_TEXT.index("、いまも")]        # mid-array, no closing bracket


def resp(text_blocks, stop_reason, thinking=False,
         usage=None):
    """Build a full Messages-API-shaped response body."""
    content = ([{"type": "thinking", "thinking": THINKING_MARKER}] if thinking else []) \
        + [{"type": "text", "text": t} for t in text_blocks]
    return {"content": content, "stop_reason": stop_reason,
            "usage": usage or {"input_tokens": 900, "output_tokens": 1200}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(outputs, lang="ja"):
    """outputs: list of response dicts (or Exceptions). Returns (result_or_exc, requests)."""
    script, reqs = list(outputs), []

    def fake(req, *a, **kw):
        reqs.append(req)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(json.dumps(item, ensure_ascii=False).encode())

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


_REAL_ROOT = lg.ROOT
TMP = tempfile.mkdtemp(prefix="ja-budget-test-")
lg.ROOT = TMP
lg._JA_RUN_DATE = "2026-08-08"


def dumped():
    return sorted(glob.glob(os.path.join(TMP, "scratch", "failed_ja_dialogue_*.json")))


def clear():
    for p in dumped():
        os.unlink(p)


def budgets(reqs):
    return [json.loads(r.data.decode())["max_tokens"] for r in reqs]


print("1) empty text + stop_reason=max_tokens → retried with a larger budget, then succeeds")
clear()
out, reqs = run([resp([], "max_tokens", thinking=True,
                      usage={"input_tokens": 900, "output_tokens": 1200}),
                 resp([GOOD_TEXT], "end_turn", thinking=True)])
check("succeeds on attempt 2", isinstance(out, list) and len(out) == 8)
check("budget escalated 1200 → 2400", budgets(reqs) == [1200, 2400])

print("\n2) truncated JSON + stop_reason=max_tokens → retried, succeeds at 2400")
clear()
out, reqs = run([resp([TRUNCATED], "max_tokens", thinking=True),
                 resp([GOOD_TEXT], "end_turn")])
check("succeeds after truncation", isinstance(out, list) and len(out) == 8)
check("budget escalated 1200 → 2400", budgets(reqs) == [1200, 2400])
rec = json.load(open(dumped()[0], encoding="utf-8"))
check("artifact records the truncation cause",
      rec["stop_reason"] == "max_tokens" and rec["max_tokens"] == 1200
      and rec["raw_output"].startswith('["'))

print("\n3) full ladder 1200 → 2400 → 4800, then fail-closed")
clear()
out, reqs = run([resp([], "max_tokens", thinking=True)] * 3)
check("raises ValueError after 3 attempts", isinstance(out, ValueError))
check("budgets were 1200, 2400, 4800", budgets(reqs) == [1200, 2400, 4800])
check("error names budgets and last stop_reason",
      "1200, 2400, 4800" in str(out) and "max_tokens" in str(out))
check("three artifacts, one per attempt", len(dumped()) == 3)

print("\n4) artifact metadata is safe and complete")
recs = [json.load(open(p, encoding="utf-8")) for p in dumped()]
check("every artifact has stop_reason / usage / content_block_types / max_tokens",
      all(r["stop_reason"] == "max_tokens" and isinstance(r["usage"], dict)
          and r["content_block_types"] == ["thinking"] and r["max_tokens"] in (1200, 2400, 4800)
          for r in recs))
blob = "".join(open(p, encoding="utf-8").read() for p in dumped())
check("no secret in artifacts", FAKE_KEY not in blob and "x-api-key" not in blob)
check("no thinking content in artifacts", THINKING_MARKER not in blob)

print("\n5) refusal (HTTP 200 + stop_reason=refusal) — never retried, one call, fail-closed")
clear()
out, reqs = run([resp([], "refusal"), resp([GOOD_TEXT], "end_turn")])
check("fail-closed with a refusal-specific error",
      isinstance(out, ValueError) and "refusal" in str(out) and "not retried" in str(out))
check("exactly ONE API call — a bigger budget cannot change a safety verdict",
      len(reqs) == 1)
check("no budget escalation happened", budgets(reqs) == [1200])
check("artifact names the refusal",
      len(dumped()) == 1
      and json.load(open(dumped()[0], encoding="utf-8"))["stop_reason"] == "refusal")

print("\n5b) plain format defect (stop_reason=end_turn) retries at the SAME budget")
clear()
GARBAGE = "承知しました。以下が台本です。(JSON配列ではない)"
out, reqs = run([resp([GARBAGE], "end_turn"), resp([GOOD_TEXT], "end_turn")])
check("format defect still retries and succeeds", isinstance(out, list) and len(out) == 8)
check("budget did NOT escalate for a non-budget failure", budgets(reqs) == [1200, 1200])
clear()
out, reqs = run([resp([GARBAGE], "end_turn"), resp([], "max_tokens", thinking=True),
                 resp([GOOD_TEXT], "end_turn")])
check("mixed case: escalation begins only after the max_tokens failure",
      isinstance(out, list) and budgets(reqs) == [1200, 1200, 2400])

print("\n6) HTTP errors are never retried")
clear()
err = urllib.error.HTTPError(lg.ANTHROPIC_URL, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
out, reqs = run([err, resp([GOOD_TEXT], "end_turn")])
check("RuntimeError immediately, one call", isinstance(out, RuntimeError) and len(reqs) == 1)
check("no artifact for a transport failure", dumped() == [])

print("\n7) EN unchanged — one attempt, flat 900, no retry")
clear()
GOOD_EN = json.dumps([{"speaker": "listener", "text": f"Question {i}?"} if i % 2 else
                      {"speaker": "explainer", "text": f"Answer number {i}."}
                      for i in range(1, 9)])
out, reqs = run([resp([GOOD_EN], "end_turn")], lang="en")
check("EN succeeds with one call at 900", isinstance(out, list) and budgets(reqs) == [900])
out, reqs = run([resp([], "max_tokens"), resp([GOOD_EN], "end_turn")], lang="en")
check("EN empty output fails immediately — no retry, no escalation",
      isinstance(out, ValueError) and budgets(reqs) == [900])
check("EN failure writes no JA artifact", dumped() == [])

print("\n8) no sampling or thinking parameters on any attempt")
clear()
_, reqs = run([resp([], "max_tokens", thinking=True)] * 3)
for i, r in enumerate(reqs, 1):
    body = json.loads(r.data.decode())
    check(f"attempt {i}: no temperature/top_p/top_k/thinking",
          all(k not in body for k in ("temperature", "top_p", "top_k", "thinking")))

print("\n9) checkpoint and manifest code untouched (source pins)")
src = Path(lg.__file__).read_text(encoding="utf-8")
gen = src.split("def generate(", 1)[1]
check("checkpoint reuse still runs before any LLM call",
      gen.find("_load_ja_checkpoint") < gen.find("llm_fn(sig"))
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
