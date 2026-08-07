#!/usr/bin/env python3
"""What llm_dialogue actually puts on the wire.

    python3 pipeline/test_listen_request_payload.py

Guards the 2026-08-06/07 outage. Claude Opus 4.7 and 4.8, Opus 5, Sonnet 5, Fable 5 and
Mythos 5 reject a non-default temperature, top_p or top_k with HTTP 400 on every request,
whether or not thinking is used. The Listen request still carried temperature: 0.5 from the
claude-3-5-sonnet era, so every run 400'd the moment SIGNALS_LISTEN_MODEL was pointed at a
current model — and the bare urllib error said only "HTTP Error 400: Bad Request", which is
what turned a one-line fix into a two-day outage.

These tests assert the payload by inspecting the real Request object, so they fail if a
sampling parameter is ever reintroduced — including by someone "restoring determinism" with
temperature: 1.0, which is still a rejected key on these models. No network is used.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listen_generate as lg  # noqa: E402

FAILED = []


def check(label, ok):
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


SIGNAL = {
    "headline": "Regulator opens inquiry into payment fees",
    "summary": "The competition regulator has opened a formal inquiry into card payment fees.",
    "keyTakeaways": ["The inquiry covers fees charged to small retailers."],
    "whyItMatters": "Fees feed directly into consumer prices.",
    "localized": {"ja": {"headline": "決済手数料に調査", "summary": "競争当局が調査を開始した。"}},
}


class _Captured(Exception):
    """Carries the outgoing Request out of the fake transport."""

    def __init__(self, request):
        self.request = request


def capture(lang):
    """Return the JSON body llm_dialogue would send, without touching the network."""
    real = urllib.request.urlopen
    holder = {}

    def fake(req, *a, **kw):
        holder["req"] = req
        raise _Captured(req)

    urllib.request.urlopen = fake
    try:
        lg.llm_dialogue(SIGNAL, api_key="test-key-not-a-secret", model="claude-opus-4-8", lang=lang)
    except _Captured:
        pass
    finally:
        urllib.request.urlopen = real
    return json.loads(holder["req"].data.decode()), holder["req"]


print("request payload — no sampling parameters")

for lang in ("en", "ja"):
    payload, req = capture(lang)

    for key in ("temperature", "top_p", "top_k"):
        check(f"[{lang}] {key!r} key is absent from the payload", key not in payload)

    check(f"[{lang}] 'thinking' is not sent", "thinking" not in payload)

    # The fields that must survive the change.
    check(f"[{lang}] model is passed through", payload.get("model") == "claude-opus-4-8")
    check(f"[{lang}] max_tokens is set", isinstance(payload.get("max_tokens"), int))
    check(f"[{lang}] system prompt is present", bool(payload.get("system")))
    check(f"[{lang}] exactly one user message", len(payload.get("messages", [])) == 1)
    check(f"[{lang}] endpoint unchanged", req.full_url == lg.ANTHROPIC_URL)

payload_en, _ = capture("en")
payload_ja, _ = capture("ja")
check("EN and JA use different system prompts", payload_en["system"] != payload_ja["system"])
check("JA carries the japanese_reference grounding",
      "japanese_reference" in payload_ja["messages"][0]["content"])

# The source line itself — a sampling key must not creep back in via a different spelling.
src = Path(lg.__file__).read_text(encoding="utf-8")
body_src = src.split("def llm_dialogue", 1)[1].split("req = urllib.request.Request", 1)[0]
for key in ("temperature", "top_p", "top_k"):
    check(f"{key!r} does not appear in the request-building source",
          f'"{key}"' not in body_src and f"'{key}'" not in body_src)

print("\nHTTP 400 is reported with the model name and the response body")


def failing_urlopen(status, body):
    def fake(req, *a, **kw):
        raise urllib.error.HTTPError(
            lg.ANTHROPIC_URL, status, "Bad Request", {},
            __import__("io").BytesIO(body.encode()))
    return fake


real = urllib.request.urlopen
urllib.request.urlopen = failing_urlopen(
    400, '{"type":"error","error":{"type":"invalid_request_error",'
         '"message":"temperature: Extra inputs are not permitted"}}')
try:
    lg.llm_dialogue(SIGNAL, api_key="test-key-not-a-secret", model="claude-opus-4-8", lang="en")
    check("a 400 raises", False)
except RuntimeError as e:
    msg = str(e)
    check("a 400 raises RuntimeError, not a bare HTTPError", True)
    check("the message names the model actually used", "claude-opus-4-8" in msg)
    check("the message includes the API response body", "Extra inputs are not permitted" in msg)
    check("the message includes the status code", "400" in msg)
except Exception as e:  # pragma: no cover
    check(f"a 400 raises RuntimeError, not {type(e).__name__}", False)
finally:
    urllib.request.urlopen = real

print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
