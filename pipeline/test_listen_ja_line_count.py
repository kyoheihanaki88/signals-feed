#!/usr/bin/env python3
"""JA line count — the 6–10 rule judges the model's RAW array, not the repaired one.

    python3 pipeline/test_listen_ja_line_count.py

Guards the 2026-08-08 failure (run 31275454416, signal 5, attempt 3): the model returned a
valid 10-sentence array whose first sentence was 61 chars — one over _JA_SOLO_MAX_CHARS=60 —
so the deterministic length repair (_split_long_sentence) turned it into two sentences, and
the count gate then rejected the repair's own result: "unexpected solo sentence count 11
(need 6–10)". Two safety mechanisms, each correct alone, deadlocked.

Pinned here: the raw array must be 6–10 items BEFORE splitting; the repair may grow the
count to at most 14; a raw 11-item array still fails; an over-long sentence with no natural
split point still reaches the quality gate's per-line length check unchanged; and the EN
parser is untouched. No network."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listen_generate as lg  # noqa: E402

FAILED = []


def check(label, ok):
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def sent(n):
    """A distinct, clearly under-limit sentence."""
    return f"これは検証用の{n}番目の文です。"


# Reconstruction of run 31275454416 signal 5's failure profile: 10 items, item 1 exactly
# 61 chars with a natural 、 split point, every other item under the ceiling.
LONG_61 = ("火曜日、OpenAIやAnthropicなど主要なAI企業の担当者が、"
           "ホワイトハウスとの非公開の会合に参加をしていました。")
assert len(LONG_61) == 61, len(LONG_61)
RUN_FIXTURE = [LONG_61] + [sent(i) for i in range(2, 11)]          # 10 items
assert len(RUN_FIXTURE) == 10 and all(len(x) <= 60 for x in RUN_FIXTURE[1:])


def parse(arr):
    try:
        return lg.parse_solo_lines(json.dumps(arr, ensure_ascii=False))
    except ValueError as e:
        return e


print("run 31275454416 signal 5 — the exact failure profile now parses")
out = parse(RUN_FIXTURE)
check("raw 10 items with one 61-char sentence succeeds", isinstance(out, list))
check("the long sentence was split: 10 raw → 11 final",
      isinstance(out, list) and len(out) == 11)
check("every final line respects the 60-char ceiling",
      isinstance(out, list) and all(len(l["text"]) <= 60 for l in out))
check("all lines are narrator", isinstance(out, list)
      and all(l["speaker"] == "narrator" for l in out))

print("\nraw-count gate runs BEFORE splitting")
out = parse([sent(i) for i in range(1, 12)])                       # 11 short items
check("raw 11 items still fails", isinstance(out, ValueError)
      and "11" in str(out) and "6–10" in str(out))
out = parse([sent(i) for i in range(1, 6)])                        # 5 items
check("raw 5 items still fails", isinstance(out, ValueError))
out = parse([sent(i) for i in range(1, 9)])                        # 8 clean items
check("raw 6–10 with no split needed: unchanged behavior (8 → 8)",
      isinstance(out, list) and len(out) == 8)

print("\npost-split bound")
out = parse([LONG_61] * 2 + [sent(i) for i in range(3, 11)])       # 10 raw → 12 final
check("two repaired sentences → 12 final, still accepted",
      isinstance(out, list) and len(out) == 12)
very_long = ("その一方で、" + "この点については、" * 12 + "結論はまだ出ていません。")
out = parse([very_long] * 10)
check("runaway fragmentation (>14 after repair) is rejected",
      isinstance(out, ValueError) and "max 14" in str(out))

print("\nno-natural-split sentences still reach the quality gate unchanged")
no_comma = "あ" * 61                                               # 61 chars, no 、 anywhere
out = parse([no_comma] + [sent(i) for i in range(2, 11)])
check("unsplittable over-long sentence passes the parser un-split",
      isinstance(out, list) and any(len(l["text"]) == 61 for l in out))
sig = {"headline": "h", "summary": "s", "keyTakeaways": [], "whyItMatters": "w"}
issues = lg.ja_solo_quality_issues(out, sig) if isinstance(out, list) else []
check("…and the quality gate still rejects it for length",
      any("too long" in i for i in issues))

print("\nEN parser untouched")
src = Path(lg.__file__).read_text(encoding="utf-8")
en_src = src.split("def parse_dialogue", 1)[1].split("def ", 1)[0]
check("parse_dialogue keeps its own 6–14 bound and no splitting",
      "6 <= len(out) <= 14" in en_src and "_split_long_sentence" not in en_src)
good_en = json.dumps([{"speaker": "listener", "text": f"Q{i}?"} if i % 2 else
                      {"speaker": "explainer", "text": f"A{i}."} for i in range(1, 9)])
check("parse_dialogue still parses a clean dialogue",
      len(lg.parse_dialogue(good_en)) == 8)

print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
