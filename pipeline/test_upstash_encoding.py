#!/usr/bin/env python3
"""Phase 3D-3G.8 — the publication path's UTF-8 contract.

Run 30382778419 reached the publish step and failed with a bare
`{"status":"failed","reason":"UnicodeEncodeError"}`. The artifact was innocent: the failure
was `http.client.putheader` encoding the AUTHORIZATION HEADER as latin-1, which a token
carrying a typographic character cannot survive. `UnicodeEncodeError` subclasses
`ValueError`, so the CLI's generic handler printed the class name and nothing else.

Every test here uses a LOCALHOST server or a recording opener. No external host is
contacted, no Upstash request is made, and no article content appears anywhere.
"""

import http.server
import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import editorial_mix_pool_publisher as EP       # noqa: E402
import mix_pool_schema as S                     # noqa: E402
import upstash_mix_pool_transport as T          # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ── a localhost endpoint: the REAL urllib / http.client encoding path ─────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _Handler.received["body"] = self.rfile.read(length)
        _Handler.received["ctype"] = self.headers.get("Content-Type")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"result":"OK"}')

    def log_message(self, *args):
        pass


_server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=_server.serve_forever, daemon=True).start()
LOCAL = f"http://127.0.0.1:{_server.server_address[1]}"
KEY = "signals:editorial-mix-pool:v1:2026-07-29"


def put(body, token="AX1aQ2bR3cS4dT5e"):
    _Handler.received.clear()
    config = T.UpstashConfig(rest_url=LOCAL, write_token=token, timeout_seconds=5.0)
    T.UpstashPoolObjectStore(config).put(KEY, body, ttl_seconds=777600)
    return _Handler.received


# ── 1. the pre-fix failure, and where it actually lived ──────────────────────────────

try:
    put(S.serialize({"h": "plain"}), token="AX1aQ2b“R3cS4d")   # smart quote in the TOKEN
    check("1. a non-latin-1 credential is rejected, not sent", False, "sent anyway")
except T.UpstashTransportError as error:
    check("1. a non-latin-1 credential is rejected, not sent",
          error.reason == T.REASON_INVALID_CREDENTIAL, error.reason)
except UnicodeEncodeError:
    check("1. a non-latin-1 credential is rejected, not sent", False,
          "UnicodeEncodeError still escapes the transport")

for label, bad in (("smart quote", "tok“en"), ("en dash", "tok–en"),
                   ("Japanese", "tok東en")):
    try:
        T.resolve_config({T.ENV_URL: "https://fixture.upstash.io", T.ENV_WRITE_TOKEN: bad})
        check(f"1b. {label} in the token fails closed at config time", False, "accepted")
    except T.UpstashTransportError as error:
        check(f"1b. {label} in the token fails closed at config time",
              error.reason == T.REASON_INVALID_CREDENTIAL, error.reason)

leak = "tok“en-SECRETVALUE"
try:
    T.resolve_config({T.ENV_URL: "https://fixture.upstash.io", T.ENV_WRITE_TOKEN: leak})
except T.UpstashTransportError as error:
    check("1c. the rejected credential value never appears in the error",
          "SECRETVALUE" not in f"{error} {error.reason} {error.args}")

# ── 2-5. Unicode editorial content publishes unchanged ───────────────────────────────

CASES = {
    "2. Japanese":              {"headline": "東京都が支援制度を拡大", "n": 1},
    "3. accented Latin":        {"headline": "Café société — Málaga", "n": 2},
    "4. typographic punctuation": {"headline": "“pilot” — it’s here… ‹ok›", "n": 3},
    "5. supplementary plane":   {"headline": "Tokyo tower 🗼 and 𝔘nicode", "n": 4},
}
for label, artifact in CASES.items():
    body = S.serialize(artifact)
    got = put(body)
    check(f"{label} publishes byte-exact", got["body"] == body,
          f"{len(got.get('body') or b'')} vs {len(body)}")

# ── 6-8. the encoding contract itself ────────────────────────────────────────────────

artifact = {"headline": "東京 — “café” 🗼", "n": 5}
text = json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
body = S.serialize(artifact)

check("6. serialize() output equals text.encode('utf-8') exactly",
      body == text.encode("utf-8"))
check("7. byte length is UTF-8 bytes, not characters",
      len(body) == len(text.encode("utf-8")) and len(body) > len(text),
      f"bytes={len(body)} chars={len(text)}")
check("8. no double encoding — the server receives exactly those bytes",
      put(body)["body"] == body)
check("8b. the transport is handed bytes, never str", isinstance(body, bytes))
try:
    put(text)   # a str body must be refused, not silently encoded
    check("8c. a str body is refused by the bytes-only contract", False, "accepted a str")
except T.UpstashTransportError as error:
    check("8c. a str body is refused by the bytes-only contract",
          error.reason == T.REASON_PUBLISH_REJECTED, error.reason)

# ── 9-10. ASCII behaviour is untouched ───────────────────────────────────────────────

ascii_artifact = {"headline": "Plain ASCII headline", "n": 6}
ascii_body = S.serialize(ascii_artifact)
check("9. ASCII artifacts remain byte-identical to previous behaviour",
      ascii_body == (json.dumps(ascii_artifact, ensure_ascii=False, sort_keys=True,
                                indent=2) + "\n").encode("utf-8")
      and put(ascii_body)["body"] == ascii_body)

fixture = json.load(open(os.path.join(HERE, "..", "api", "_fixtures",
                                      "editorial_mix_pool.json"), encoding="utf-8"))
smoke_body = S.serialize(fixture)
check("10. the committed smoke fixture is still exactly 16,909 bytes",
      len(smoke_body) == 16909, str(len(smoke_body)))
check("10b. it round-trips through the transport byte-exact",
      put(smoke_body)["body"] == smoke_body)

# ── 15. lone surrogates are rejected, never replaced ─────────────────────────────────

class _Recorder:
    def __init__(self): self.calls = 0
    def put(self, key, body, *, ttl_seconds): self.calls += 1


surrogate = json.loads(json.dumps(fixture))
surrogate["candidates"][0]["editorial"]["headline"] = "bad \ud800 surrogate"
probe = _Recorder()
try:
    EP.publish_editorial_mix_pool(surrogate, store=probe, date=surrogate["date"])
    check("15. a lone surrogate is rejected before publication", False, "published")
except EP.EditorialPublishError as error:
    check("15. a lone surrogate is rejected before publication",
          error.reason in (EP.REASON_ARTIFACT_ENCODING, EP.REASON_INVALID_ARTIFACT),
          error.reason)
    check("15b. no replacement character was substituted",
          "�" not in str(error) and "bad" not in str(error))
check("18. a failure before HTTP performs zero transport requests", probe.calls == 0)

# ── 16-17. no implicit encoding remains in the chain ─────────────────────────────────

chain = {
    "upstash_mix_pool_transport.py": open(os.path.join(HERE, "upstash_mix_pool_transport.py"),
                                          encoding="utf-8").read(),
    "editorial_mix_pool_publisher.py": open(os.path.join(HERE, "editorial_mix_pool_publisher.py"),
                                            encoding="utf-8").read(),
}
import re  # noqa: E402

implicit = []
for name, src in chain.items():
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"^\s*#.*$", "", code, flags=re.M)
    for match in re.finditer(r"\.encode\(\s*\)|\.decode\(\s*\)|bytes\(\s*str\b", code):
        implicit.append(f"{name}: {match.group(0)}")
check("17. no implicit encode/decode remains in the publication chain",
      implicit == [], str(implicit))
check("16. the transport declares an explicit content type",
      put(smoke_body)["ctype"] == "application/octet-stream",
      str(_Handler.received.get("ctype")))

# ── 19. the route stays disconnected ─────────────────────────────────────────────────

_edition_src = open(os.path.join(HERE, "..", "api", "edition.ts"), encoding="utf-8").read()
check("19. /api/edition is CONNECTED, and holds no credential of its own",
      _edition_src.count("custom_mix_unavailable") >= 1
      and "selector_not_connected" not in _edition_src
      # Phase 3E-1: the route receives an orchestration result. It must never name the
      # store, a key or a credential — only the composition layer may.
      and not re.search(r"KV_REST_API|upstash|signals:editorial-mix-pool", _edition_src),
      "edition.ts boundary")

_server.shutdown()
print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}:")
    for name in FAILURES:
        print("  -", name)
    raise SystemExit(1)
print("All Upstash encoding checks passed.")
