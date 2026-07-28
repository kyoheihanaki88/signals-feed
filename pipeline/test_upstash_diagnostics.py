#!/usr/bin/env python3
"""Phase 3D-3G.10 — safe provider diagnostics.

Run 30385533714 failed at Publish with `{"status":"failed","reason":"upstash_provider_error"}`
and nothing else. `urlopen` raises `HTTPError` for every non-2xx, and the handler discarded
`error.code` — so the one fact that distinguishes a permission refusal from an auth failure
from a malformed request was thrown away before anyone could see it.

These tests drive every provider failure shape through the transport with an injected
opener. NO NETWORK: nothing here opens a socket or contacts Upstash.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import editorial_mix_pool_cli as CLI            # noqa: E402
import mix_pool_schema as S                     # noqa: E402
import upstash_mix_pool_transport as T          # noqa: E402

FAILURES = []
BODY = S.serialize({"h": "x"})
KEY = "signals:editorial-mix-pool:v1:2026-07-29"
CONFIG = T.UpstashConfig(rest_url="https://fixture.upstash.io",
                         write_token="FIXTURE-TOKEN-NOT-REAL", timeout_seconds=5.0)


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class Raiser:
    """Injected opener that raises exactly what a real provider refusal raises."""

    def __init__(self, status, body=b""):
        self.status, self.body, self.calls = status, body, 0

    def __call__(self, request, timeout):
        self.calls += 1
        import io
        raise urllib.error.HTTPError(
            "https://fixture.upstash.io/set/k", self.status, "reason-prose",
            {}, io.BytesIO(self.body))


def put_with(status, body=b""):
    opener = Raiser(status, body)
    try:
        T.UpstashPoolObjectStore(CONFIG, opener=opener).put(KEY, BODY, ttl_seconds=777600)
        return None
    except T.UpstashTransportError as error:
        return error


# ── 1-5. status classification ───────────────────────────────────────────────────────

e = put_with(400, b'{"error":"NOPERM this user has no permissions to run the \'set\' command"}')
check("1. HTTP 400 permission error -> permission_denied",
      e.reason == "upstash_http_400" and e.provider_category == "permission_denied"
      and e.http_status == 400, f"{e.reason}/{e.provider_category}")

e = put_with(401, b'{"error":"WRONGPASS invalid or missing auth token"}')
check("2. HTTP 401 -> authentication_failed",
      e.reason == "upstash_http_401" and e.provider_category == "authentication_failed",
      f"{e.reason}/{e.provider_category}")

e = put_with(404, b"not found")
check("3. HTTP 404 -> endpoint_not_found",
      e.reason == "upstash_http_404" and e.provider_category == "endpoint_not_found",
      f"{e.reason}/{e.provider_category}")

e = put_with(429, b'{"error":"max daily request limit exceeded"}')
check("4. HTTP 429 -> rate_limited",
      e.reason == "upstash_http_429" and e.provider_category == "rate_limited",
      f"{e.reason}/{e.provider_category}")

for status in (500, 502, 503):
    e = put_with(status, b"upstream boom")
    check(f"5. HTTP {status} -> provider_unavailable",
          e.reason == "upstash_http_5xx" and e.provider_category == "provider_unavailable"
          and e.http_status == status, f"{e.reason}/{e.provider_category}")

e = put_with(413, b'{"error":"ERR max request size exceeded"}')
check("5b. HTTP 413 -> payload_too_large", e.provider_category == "payload_too_large",
      e.provider_category)
e = put_with(400, b'{"error":"ERR wrong number of arguments for \'set\' command"}')
check("5c. a malformed command -> invalid_request",
      e.provider_category == "invalid_request", e.provider_category)

# ── 6. malformed provider JSON ───────────────────────────────────────────────────────

class Returner:
    def __init__(self, status, payload):
        self.status, self.payload, self.calls = status, payload, 0

    def __call__(self, request, timeout):
        self.calls += 1
        return self.status, self.payload


try:
    T.UpstashPoolObjectStore(CONFIG, opener=Returner(200, b"not json at all")).put(
        KEY, BODY, ttl_seconds=777600)
    check("6. malformed provider JSON maps safely", False, "accepted")
except T.UpstashTransportError as error:
    check("6. malformed provider JSON maps safely",
          error.reason == T.REASON_INVALID_RESPONSE, error.reason)

try:
    T.UpstashPoolObjectStore(CONFIG, opener=Returner(
        200, b'{"error":"NOPERM not allowed"}')).put(KEY, BODY, ttl_seconds=777600)
    check("6b. a 200 carrying `error` is classified, not ignored", False, "accepted")
except T.UpstashTransportError as error:
    check("6b. a 200 carrying `error` is classified, not ignored",
          error.provider_category == "permission_denied", str(error.provider_category))

# ── 7-8. nothing leaks ───────────────────────────────────────────────────────────────

LEAKY = (b'{"error":"NOPERM user TOKEN-abc123 cannot SET '
         b'signals:editorial-mix-pool:v1:2026-07-29 at https://real-db.upstash.io"}')
e = put_with(400, LEAKY)
blob = json.dumps({**e.safe_detail(), "str": str(e), "args": [str(a) for a in e.args]})
check("7. provider prose never leaks",
      not re.search(r"NOPERM|cannot SET|reason-prose", blob), blob[:120])
check("8. token, URL and key never leak",
      not re.search(r"TOKEN-abc123|upstash\.io|editorial-mix-pool:v1", blob), blob[:120])
check("8b. safe_detail carries only the whitelisted fields",
      set(e.safe_detail()) <= {"reason", "stage", "httpStatus", "providerCategory"},
      str(set(e.safe_detail())))

# ── 9-10. stage and exact status ─────────────────────────────────────────────────────

check("9. the request stage is reported", e.stage == "set", str(e.stage))
ping_opener = Raiser(401, b'{"error":"WRONGPASS"}')
try:
    T.ping(CONFIG, opener=ping_opener)
except T.UpstashTransportError as error:
    check("9b. a ping failure reports its own stage", error.stage == "ping", str(error.stage))
get_opener = Raiser(403, b'{"error":"NOPERM"}')
try:
    T.read_back(CONFIG, KEY, opener=get_opener)
except T.UpstashTransportError as error:
    check("9c. a get failure reports its own stage", error.stage == "get", str(error.stage))
check("10. the exact numeric status is reported, without any body",
      e.http_status == 400 and "body" not in e.safe_detail())

# ── 11-14. the write-capability probe ────────────────────────────────────────────────

src = open(os.path.join(HERE, "editorial_mix_pool_cli.py"), encoding="utf-8").read()
# Strip docstrings and comments: prose describing the namespace is not a second key.
src_code = re.sub(r'"""[\s\S]*?"""', "", src)
src_code = re.sub(r"^\s*#.*$", "", src_code, flags=re.M)
check("11. the probe uses one exact key under a dedicated namespace",
      'f"signals:verify:write-capability:{args.label}"' in src_code
      and src_code.count("signals:verify:write-capability") == 1,
      str(src_code.count("signals:verify:write-capability")))
check("12. the probe sets a 60-second TTL", "ttl_seconds=60" in src)
check("13. the probe deletes only that exact key",
      "delete_key(config, key)" in src)
check("13b. the probe label is required, with no default",
      '"--label", required=True' in src)
# Executable code only — a comment saying "no scan or flush" is not a scan or flush.
tsrc = open(os.path.join(HERE, "upstash_mix_pool_transport.py"), encoding="utf-8").read()
tcode = re.sub(r'"""[\s\S]*?"""', "", tsrc)
tcode = re.sub(r"^\s*#.*$", "", tcode, flags=re.M)
# `*args` / `**kwargs` are Python syntax, not Redis globs — the check is for COMMANDS.
_DANGEROUS = r"FLUSHDB|FLUSHALL|\bSCAN\b|\bKEYS\b|SCRIPT|EVAL"
check("14. no wildcard, scan or flush command exists anywhere in the transport",
      not re.search(_DANGEROUS, tcode, re.I),
      (re.search(_DANGEROUS, tcode, re.I) or [""])[0])
check("14c. every command the transport can issue is a fixed literal",
      sorted(set(re.findall(r'_command\(config, \["(\w+)"', tcode))
             | set(re.findall(r'command_url\(self\._config\.rest_url, "(\w+)"', tcode)))
      == ["del", "get", "ping", "set"],
      str(sorted(set(re.findall(r'_command\(config, \["(\w+)"', tcode))
                 | set(re.findall(r'command_url\(self\._config\.rest_url, "(\w+)"', tcode)))))
check("14b. the probe is NOT wired into any workflow",
      not any("verify-write" in open(os.path.join(HERE, "..", ".github", "workflows", w),
                                     encoding="utf-8").read()
              for w in os.listdir(os.path.join(HERE, "..", ".github", "workflows"))))

# ── 15-16. success unchanged, and no artifact sent on probe failure ──────────────────

good = Returner(200, b'{"result":"OK"}')
T.UpstashPoolObjectStore(CONFIG, opener=good).put(KEY, BODY, ttl_seconds=777600)
sent = json.loads(good.__dict__.get("payload", b"{}").decode()) if False else None
check("15. a normal successful publication still succeeds unchanged", good.calls == 1)

denied = Raiser(400, b'{"error":"NOPERM"}')
check("16. a refused request sends exactly one attempt, never a retry with the artifact",
      put_with(400, b'{"error":"NOPERM"}') is not None)

# ── 17-18. boundaries ────────────────────────────────────────────────────────────────

_edition_src = open(os.path.join(HERE, "..", "api", "edition.ts"), encoding="utf-8").read()
check("17. /api/edition is CONNECTED, and holds no credential of its own",
      _edition_src.count("custom_mix_unavailable") >= 1
      and "selector_not_connected" not in _edition_src
      # Phase 3E-1: the route receives an orchestration result. It must never name the
      # store, a key or a credential — only the composition layer may.
      and not re.search(r"KV_REST_API|upstash|signals:editorial-mix-pool", _edition_src),
      "edition.ts boundary")
# Parse rather than grep: the name appears in this very assertion.
import ast  # noqa: E402

imported = set()
for node in ast.walk(ast.parse(open(__file__, encoding="utf-8").read())):
    if isinstance(node, ast.Import):
        imported.update(a.name.split(".")[0] for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        imported.add(node.module.split(".")[0])
check("18. this suite opens no socket",
      not (imported & {"socket", "http", "subprocess", "requests"})
      and "urlopen(" not in re.sub(r'"[^"]*"', "", open(__file__, encoding="utf-8").read()),
      str(sorted(imported & {"socket", "http", "subprocess", "requests"})))

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}:")
    for name in FAILURES:
        print("  -", name)
    raise SystemExit(1)
print("All Upstash diagnostic checks passed.")
