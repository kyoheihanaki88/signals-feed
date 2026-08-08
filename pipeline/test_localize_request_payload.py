#!/usr/bin/env python3
"""What localize.py actually puts on the wire.

    python3 pipeline/test_localize_request_payload.py

Guards the 2026-08-08 outage: _call_anthropic still carried temperature: 0.4 from the
claude-3-5-sonnet era, and Claude Sonnet 5 rejects any non-default temperature/top_p/top_k
with HTTP 400 — so the moment SIGNALS_JA_MODEL pointed at claude-sonnet-5, all five signals
failed ("`temperature` is deprecated for this model.") and the edition shipped English-only.
listen_generate.py had the identical bug two days earlier; these tests are the localize.py
counterpart of test_listen_request_payload.py and inspect the real urllib Request, so they
fail if a sampling key is ever reintroduced. No network is used.
"""

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localize  # noqa: E402

FAILED = []
FAKE_KEY = "test-key-抜き取り検査-not-a-secret"


def check(label, ok):
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


SIG = {
    "number": 1,
    "headline": "Regulator opens inquiry into payment fees",
    "summary": "The competition regulator has opened a formal inquiry into card payment fees.",
    "keyTakeaways": ["The inquiry covers fees charged to small retailers."],
    "whyItMatters": "Fees feed directly into consumer prices.",
}

GOOD_JA = {"headline": "決済手数料に調査", "summary": "競争当局が正式な調査を開始した。",
           "keyTakeaways": ["小規模店の手数料が対象"], "whyItMatters": "手数料は物価に直結する。"}


def with_transport(fn, body_fn):
    """Run fn with urlopen replaced; returns (result_or_exc, captured_requests)."""
    captured = []
    real = urllib.request.urlopen

    def fake(req, *a, **kw):
        captured.append(req)
        return body_fn(req)

    urllib.request.urlopen = fake
    try:
        try:
            out = fn()
        except Exception as e:                    # noqa: BLE001 — tests inspect the exception
            out = e
    finally:
        urllib.request.urlopen = real
    return out, captured


def ok_response(_req):
    payload = {"content": [{"type": "text", "text": json.dumps(GOOD_JA, ensure_ascii=False)}]}
    return io.BytesIO(json.dumps(payload).encode())


class _Ctx(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok_ctx(_req):
    payload = {"content": [{"type": "text", "text": json.dumps(GOOD_JA, ensure_ascii=False)}]}
    return _Ctx(json.dumps(payload).encode())


print("request payload — no sampling parameters")

result, reqs = with_transport(
    lambda: localize.localize_signal(SIG, "claude-sonnet-5", FAKE_KEY), ok_ctx)
check("request was issued", len(reqs) == 1)
payload = json.loads(reqs[0].data.decode())

for key in ("temperature", "top_p", "top_k"):
    check(f"{key!r} key is absent from the payload", key not in payload)
check("'thinking' is not sent", "thinking" not in payload)
check("model is passed through", payload.get("model") == "claude-sonnet-5")
check("max_tokens is set", payload.get("max_tokens") == localize.MAX_TOKENS)
check("system prompt is the localizer prompt", payload.get("system") == localize.SYSTEM_PROMPT)
check("exactly one user message", len(payload.get("messages", [])) == 1
      and payload["messages"][0]["role"] == "user")
check("user message carries the English fields",
      SIG["headline"] in payload["messages"][0]["content"])
check("endpoint unchanged", reqs[0].full_url == localize.API_URL)

# The source itself — a sampling key must not creep back in under a different spelling.
src = Path(localize.__file__).read_text(encoding="utf-8")
body_src = src.split("def _call_anthropic", 1)[1].split("req = urllib.request.Request", 1)[0]
for key in ("temperature", "top_p", "top_k"):
    check(f"{key!r} does not appear in the request-building source",
          f'"{key}"' not in body_src and f"'{key}'" not in body_src)

print("\nsuccess path — localized.ja keeps its shape")

check("localize_signal returns the validated ja block",
      isinstance(result, dict) and result == GOOD_JA)

feed = {"date": "2026-08-08", "signals": [dict(SIG)]}
(out_feed, stats), _ = with_transport(
    lambda: localize.localize_feed(feed, model="claude-sonnet-5", api_key=FAKE_KEY,
                                   log=lambda *_: None), ok_ctx)
ja = out_feed["signals"][0]["localized"]["ja"]
check("localize_feed attaches localized.ja with the expected keys",
      set(ja) == {"headline", "summary", "keyTakeaways", "whyItMatters"})
check("stats count the success", stats["localized"] == 1 and stats["failed"] == 0)

print("\nfailure path — best-effort and annotations are intact")


def reject_400(_req):
    raise urllib.error.HTTPError(
        localize.API_URL, 400, "Bad Request", {},
        io.BytesIO(b'{"type":"error","error":{"type":"invalid_request_error",'
                   b'"message":"`temperature` is deprecated for this model."}}'))


logs = []
feed2 = {"date": "2026-08-08", "signals": [dict(SIG)]}
(out2, stats2), _ = with_transport(
    lambda: localize.localize_feed(feed2, model="claude-sonnet-5", api_key=FAKE_KEY,
                                   log=logs.append), reject_400)
check("a failing signal is skipped, not fatal (best-effort preserved)",
      stats2["failed"] == 1 and "localized" not in out2["signals"][0])
check("the HTTP body is logged for diagnosis",
      any("deprecated for this model" in l for l in logs))
check("the API key never appears in any log line",
      all(FAKE_KEY not in l for l in logs))
check("the API key is only in the x-api-key header, not the body",
      FAKE_KEY not in payload_str if (payload_str := json.dumps(payload)) else True)

ann = []
text = localize.ci_annotations(stats2, key_present=True, log=ann.append)
check("ci_annotations still warns on the wipe-out", text is not None
      and ann and ann[0].startswith("::warning title=localize.py::"))
check("annotation does not leak the key", all(FAKE_KEY not in l for l in ann))

print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
