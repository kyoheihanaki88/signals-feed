#!/usr/bin/env python3
"""Phase 3D-3D — the Upstash publisher transport and the Editorial Mix Pool publisher.

NO LIVE NETWORK. Every test replaces the transport's single socket-opening function with a
recorder, so nothing here contacts Upstash, and passing proves the CODE is correct — not
that a database exists. The one thing that can prove the storage path works is the manual
smoke test in `UPSTASH_PROVISIONING.md`.

Only synthetic article text is used; no copyrighted article body is copied.
"""

import copy
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mix_pool                                                    # noqa: E402
import mix_pool_schema as S                                        # noqa: E402
import ranker as ranker_module                                     # noqa: E402
import editorial_mix_pool as B                                     # noqa: E402
import editorial_mix_pool_publisher as EP                          # noqa: E402
import upstash_mix_pool_transport as T                             # noqa: E402
from editorial_mix_pool_schema import validate_editorial_mix_pool  # noqa: E402

DATE = "2026-07-27"
GENERATED_AT = "2026-07-27T09:00:00Z"
URL_VALUE = "https://fixture-not-a-real-database.upstash.io"
TOKEN_VALUE = "FIXTURE-WRITE-TOKEN-8c41f2b7ae0d4931b6e5-NOT-REAL"
GOOD_ENV = {T.ENV_URL: URL_VALUE, T.ENV_WRITE_TOKEN: TOKEN_VALUE}

FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ── a real Editorial Mix Pool, built offline through the real builder ──────────────────

ARTICLE = (
    "Tokyo officials said on Friday that the programme would expand to twelve wards "
    "before the end of the year. The ministry confirmed the budget had been approved "
    "after a review lasting several months. Local operators welcomed the decision and "
    "said hiring would begin immediately across the affected districts. Analysts noted "
    "that similar schemes in Osaka and Kyoto had reduced waiting times substantially. "
    "The prefecture will publish quarterly figures so residents can track progress. "
    "A spokesperson added that the rollout would be reviewed again next spring. "
    "Officials expect the first sites to open within eight weeks of the announcement."
)

IMAGES = {
    "category_pools": {}, "aliases": {},
    "default_pool": [
        {"imageURL": f"https://images.example.com/photo-{i:03d}?w=900", "placeTime": f"Place {i}"}
        for i in range(40)
    ],
    "topic_pools": {}, "topic_matchers": {}, "pool": [], "cats": {},
    "default": {"imageURL": "https://images.example.com/fallback?w=900", "placeTime": "Desk"},
}


def raw_pool(count=20):
    base = json.load(open(os.path.join(HERE, "fixtures", "mix_pool_scout_candidates.json"),
                          encoding="utf-8"))
    template = base["candidates"][0]
    candidates = []
    for index in range(count):
        row = copy.deepcopy(template)
        row["url"] = f"https://example.com/news/tokyo-care-programme-phase-{index:02d}"
        row["canonical_url"] = row["url"]
        row["id"] = ranker_module.selection_id(row)
        row["title"] = f"Tokyo expands its care programme, phase {index}"
        row["snippet"] = "The ministry confirmed the budget this week."
        row["cluster_id"] = f"cluster-{index:02d}"
        candidates.append(row)
    source = {**base, "candidates": candidates}
    pool = mix_pool.build_mix_pool(source, DATE, GENERATED_AT, now=GENERATED_AT)
    return S.freeze_artifact(pool, source_input=source, source="offline-fixture",
                             reference_at=GENERATED_AT)


def offline_fetch(item, articles_dir, allow_fetch, unavailable, force_refetch=frozenset()):
    return ARTICLE, "full_article"


ARTIFACT = B.build_editorial_mix_pool(
    raw_pool(20), generated_at=GENERATED_AT, articles_dir="/nonexistent",
    images_config=IMAGES, allow_fetch=False, get_source_text=offline_fetch,
)["artifact"]
BODY = S.serialize(ARTIFACT)


class Recorder:
    """Stands in for the ONE socket-opening function. Records; never opens anything."""

    def __init__(self, status=200, payload=b'{"result":"OK"}', raises=None):
        self.calls = []
        self.status = status
        self.payload = payload
        self.raises = raises

    def __call__(self, request, timeout):
        self.calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": dict(request.header_items()),
            "body": request.data,
            "timeout": timeout,
        })
        if self.raises is not None:
            raise self.raises
        return self.status, self.payload


def publish(artifact=None, recorder=None, env=None, **kwargs):
    recorder = recorder or Recorder()
    store = T.create_store(env if env is not None else GOOD_ENV, opener=recorder)
    result = EP.publish_editorial_mix_pool(artifact or ARTIFACT, store=store, date=DATE, **kwargs)
    return result, recorder


# ── 21-24. configuration ──────────────────────────────────────────────────────────────

config = T.resolve_config(GOOD_ENV)
check("21. a valid configuration is accepted",
      config.rest_url == URL_VALUE and config.write_token == TOKEN_VALUE)

for name, env, expected in [
    ("22. a missing URL is rejected before the transport", {T.ENV_WRITE_TOKEN: TOKEN_VALUE},
     T.REASON_PARTIAL_CONFIGURATION),
    ("23. a missing write token is rejected before the transport", {T.ENV_URL: URL_VALUE},
     T.REASON_PARTIAL_CONFIGURATION),
    ("24. an entirely absent configuration is rejected", {}, T.REASON_NOT_CONFIGURED),
]:
    probe = Recorder()
    try:
        T.create_store(env, opener=probe)
        check(name, False, "accepted")
    except T.UpstashTransportError as error:
        check(name, error.reason == expected and probe.calls == [], error.reason)

for bad in ["http://fixture.upstash.io", "https://fixture.upstash.io?t=1",
            "https://fixture.upstash.io#f", "not-a-url"]:
    try:
        T.resolve_config({T.ENV_URL: bad, T.ENV_WRITE_TOKEN: TOKEN_VALUE})
        check(f"24b. an insecure or decorated URL is refused ({bad})", False, "accepted")
    except T.UpstashTransportError as error:
        check(f"24b. an insecure or decorated URL is refused ({bad})",
              error.reason == T.REASON_INSECURE_URL, error.reason)

check("24c. the API's read variable name is NOT accepted as a write credential",
      T.ENV_WRITE_TOKEN != "KV_REST_API_TOKEN")
try:
    T.resolve_config({T.ENV_URL: URL_VALUE, "KV_REST_API_TOKEN": TOKEN_VALUE})
    check("24d. a read token pasted into the publisher fails closed", False, "accepted")
except T.UpstashTransportError as error:
    check("24d. a read token pasted into the publisher fails closed",
          error.reason == T.REASON_PARTIAL_CONFIGURATION, error.reason)

# ── 25-28. the write itself ───────────────────────────────────────────────────────────

result, recorder = publish()
call = recorder.calls[0]
KEY = f"signals:editorial-mix-pool:v1:{DATE}"

check("25. the exact versioned, dated key is used, as its own URL path segment",
      call["url"] == f"{URL_VALUE}/set/{KEY}?EX=777600" == f"{URL_VALUE}/set/{result['key']}?EX=777600",
      call["url"].replace(URL_VALUE, "<base>"))
check("25b. the editorial key cannot collide with the raw selector pool key",
      "/set/signals:editorial-mix-pool:v1:" in call["url"]
      and "signals:mix-pool:" not in call["url"])
check("26. the exact serialized bytes are the raw request BODY, not a URL segment",
      call["body"] == BODY and result["byteLength"] == len(BODY)
      and BODY[:40].decode("utf-8") not in call["url"])
check("27. the TTL is exactly 9 days, carried as a query parameter",
      call["url"].endswith("?EX=777600") and 9 * 24 * 60 * 60 == 777600)
check("28. exactly ONE request is issued", len(recorder.calls) == 1, str(len(recorder.calls)))
check("28b. it is a single POST to the /set/ URL command, not the root endpoint",
      call["method"] == "POST"
      and call["url"] != URL_VALUE
      and call["url"].startswith(f"{URL_VALUE}/set/"))
check("28c. the credential travels in the Authorization header only",
      call["headers"].get("Authorization") == f"Bearer {TOKEN_VALUE}"
      and TOKEN_VALUE not in call["url"]
      and TOKEN_VALUE.encode() not in call["body"])
check("28d. an explicit timeout is applied to the request",
      isinstance(call["timeout"], float) and call["timeout"] > 0)
check("28e. the body is declared opaque, never re-encodable JSON or form data",
      call["headers"].get("Content-type") == "application/octet-stream",
      str(call["headers"].get("Content-type")))

# ── 3D-3F.1 regressions: the request FORM itself ──────────────────────────────────────
#
# Real endpoint evidence that motivated this: root POST `["PING"]` -> HTTP 400, while the
# URL command GET /ping -> HTTP 200 {"result":"PONG"}. These assertions exist so the root
# form can never quietly come back.

check("F1. the root JSON-array command form is never used for SET",
      call["url"] != URL_VALUE and not call["url"].rstrip("/").endswith(".upstash.io"))
try:
    json.loads(call["body"].decode("utf-8"))
    body_is_array = isinstance(json.loads(call["body"].decode("utf-8")), list)
except Exception:
    body_is_array = False
check("F2. the request body is the artifact, never a ['SET', …] command array",
      not body_is_array)

TRANSPORT_SRC = open(os.path.join(HERE, "upstash_mix_pool_transport.py"), encoding="utf-8").read()
check("F3. no root-endpoint JSON command array is constructed anywhere in the transport",
      "json.dumps(command" not in TRANSPORT_SRC
      and '["SET"' not in TRANSPORT_SRC
      and '"SET", key' not in TRANSPORT_SRC)
check("F4. every request URL is built by the single command_url helper",
      TRANSPORT_SRC.count("urllib.request.Request(") == 2
      and TRANSPORT_SRC.count("command_url(") >= 2)

# PING — the exact request that proved the URL form works against the real endpoint.
ping_rec = Recorder(payload=b'{"result":"PONG"}')
ok = T.ping(T.resolve_config(GOOD_ENV), opener=ping_rec)
check("F5. PING uses the URL path form and reports PONG",
      ok is True
      and ping_rec.calls[0]["url"] == f"{URL_VALUE}/ping"
      and ping_rec.calls[0]["method"] == "GET"
      and ping_rec.calls[0]["body"] is None)

# GET — exact key, byte-exact 16,909-byte round trip including multi-byte characters.
UNICODE_VALUE = '{"t":"東京 café — \\"pilot\\" ✓","n":1}'
get_rec = Recorder(payload=json.dumps({"result": BODY.decode("utf-8")},
                                      ensure_ascii=False).encode("utf-8"))
got = T.read_back(T.resolve_config(GOOD_ENV), KEY, opener=get_rec)
check("F6. GET uses the exact key as a URL path segment",
      get_rec.calls[0]["url"] == f"{URL_VALUE}/get/{KEY}"
      and get_rec.calls[0]["method"] == "GET")
check("F7. a 16,909-byte artifact round-trips byte-exact",
      got == BODY and len(got) == len(BODY), f"{len(got or b'')} vs {len(BODY)}")

uni_rec = Recorder(payload=json.dumps({"result": UNICODE_VALUE},
                                      ensure_ascii=False).encode("utf-8"))
uni = T.read_back(T.resolve_config(GOOD_ENV), KEY, opener=uni_rec)
check("F8. multi-byte and escaped characters round-trip unchanged",
      uni == UNICODE_VALUE.encode("utf-8"), repr(uni))

missing_rec = Recorder(payload=b'{"result":null}')
check("F9. an absent key reads as None, not as an error",
      T.read_back(T.resolve_config(GOOD_ENV), KEY, opener=missing_rec) is None)

# DEL — one exact key, no pattern.
del_rec = Recorder(payload=b'{"result":1}')
removed = T.delete_key(T.resolve_config(GOOD_ENV), KEY, opener=del_rec)
check("F10. DEL targets exactly one key by path segment",
      removed == 1 and del_rec.calls[0]["url"] == f"{URL_VALUE}/del/{KEY}")

check("F11. dangerous characters are escaped out of the path",
      T.encode_segment("a/b?c#d e") == "a%2Fb%3Fc%23d%20e"
      and T.encode_segment("x:y") == "x:y")
check("F12. a wildcard cannot survive into a request path",
      "%2A" in T.encode_segment("signals:*") or "*" == T.encode_segment("signals:*")[-1])

# The value never enters a URL: percent-encoding tens of KB would blow past proxy URL
# limits, which is precisely why the body form is used instead.
check("F13. the SET request line stays small however large the artifact is",
      len(call["url"]) < 200 < len(call["body"]),
      f"url={len(call['url'])} body={len(call['body'])}")
check("F14. the artifact is far below Upstash's 10 MB request ceiling",
      len(BODY) < 10 * 1024 * 1024 and T.MAX_BODY_BYTES <= 10 * 1024 * 1024)

# The exact artifact the smoke test publishes: the committed 16,909-byte fixture.
SMOKE_BODY = S.serialize(json.load(
    open(os.path.join(HERE, "..", "api", "_fixtures", "editorial_mix_pool.json"),
         encoding="utf-8")))
check("F15. the committed smoke fixture is exactly 16,909 bytes",
      len(SMOKE_BODY) == 16909, str(len(SMOKE_BODY)))

smoke_set = Recorder()
T.UpstashPoolObjectStore(T.resolve_config(GOOD_ENV), opener=smoke_set).put(
    "signals:smoke:editorial-mix-pool:v1:2026-01-01", SMOKE_BODY, ttl_seconds=3600)
check("F16. the smoke TTL is exactly 3600 on the exact smoke key",
      smoke_set.calls[0]["url"]
      == f"{URL_VALUE}/set/signals:smoke:editorial-mix-pool:v1:2026-01-01?EX=3600"
      and smoke_set.calls[0]["body"] == SMOKE_BODY,
      smoke_set.calls[0]["url"].replace(URL_VALUE, "<base>"))

smoke_get = Recorder(payload=json.dumps({"result": SMOKE_BODY.decode("utf-8")},
                                        ensure_ascii=False).encode("utf-8"))
smoke_got = T.read_back(T.resolve_config(GOOD_ENV),
                        "signals:smoke:editorial-mix-pool:v1:2026-01-01", opener=smoke_get)
check("F17. the 16,909-byte smoke artifact round-trips byte-exact",
      smoke_got == SMOKE_BODY and len(smoke_got) == 16909, str(len(smoke_got or b"")))

# ── 29-31. transport failures ─────────────────────────────────────────────────────────

for name, raises, expected in [
    ("29. a timeout is handled safely", TimeoutError("connect timeout"), T.REASON_TIMEOUT),
    # Phase 3D-3G.10 refines these: the status and a safe category now survive, so a
    # permission refusal is no longer indistinguishable from a DNS failure.
    ("30. a provider rejection is handled safely",
     urllib.error.HTTPError(URL_VALUE, 401, "WRONGPASS bad token", {}, None),
     "upstash_http_401"),
    ("30b. a connection failure is handled safely",
     urllib.error.URLError("name resolution failed"), T.REASON_NETWORK_ERROR),
]:
    try:
        publish(recorder=Recorder(raises=raises))
        check(name, False, "no error raised")
    except T.UpstashTransportError as error:
        check(name, error.reason == expected, error.reason)

for name, status, payload in [
    ("30c. a non-200 status is a provider error", 500, b'{"result":"OK"}'),
    ("31. a malformed success body is rejected", 200, b"not json"),
    ("31b. a non-object body is rejected", 200, b"[1,2,3]"),
    ("31c. a 200 carrying an error field is a provider error", 200, b'{"error":"ERR bad"}'),
    ("31d. a result that is not OK is rejected", 200, b'{"result":"QUEUED"}'),
    ("31e. a missing result is rejected", 200, b'{"ok":true}'),
]:
    try:
        publish(recorder=Recorder(status=status, payload=payload))
        check(name, False, "accepted")
    except T.UpstashTransportError as error:
        check(name, error.reason in (T.REASON_PROVIDER_ERROR, T.REASON_INVALID_RESPONSE,
                                     "upstash_http_5xx", "upstash_http_400"),
              error.reason)

# ── 32-33. nothing leaks ──────────────────────────────────────────────────────────────

leaked = []
try:
    publish(recorder=Recorder(
        raises=urllib.error.HTTPError(
            f"{URL_VALUE}/set/x", 401, f"WRONGPASS token {TOKEN_VALUE} for db 12345", {}, None)))
except T.UpstashTransportError as error:
    text = f"{error} {error.reason} {error.args}"
    for needle in (TOKEN_VALUE, URL_VALUE, "WRONGPASS", "12345"):
        if needle in text:
            leaked.append(needle)
check("32. a safe failure contains no credential and no provider prose", leaked == [], str(leaked))

check("32b. the config object cannot print its own token",
      TOKEN_VALUE not in repr(config) and TOKEN_VALUE not in str(config)
      and URL_VALUE not in repr(config))

serialized_result = json.dumps(result, ensure_ascii=False)
content_leaks = [
    row["editorial"]["headline"] for row in ARTIFACT["candidates"]
    if row["editorial"]["headline"] in serialized_result
]
check("33. the safe result contains no candidate content",
      content_leaks == []
      and TOKEN_VALUE not in serialized_result
      and URL_VALUE not in serialized_result
      and "summary" not in serialized_result
      and len(result["editorialPoolIdentityPrefix"]) == 12,
      str(content_leaks))
check("33b. the result reports only counts, versions and identity prefixes",
      set(result) == {"status", "date", "key", "artifactType", "schemaVersion",
                      "selectorVersion", "editorialVersion", "candidateCount", "selectorCount",
                      "byteLength", "selectorPoolIdentityPrefix", "editorialPoolIdentityPrefix",
                      "ttlSeconds"},
      str(sorted(result)))

# ── 34-35. no write for an artifact that must not be published ────────────────────────

def refuses(name, artifact, expected=None, **kwargs):
    probe = Recorder()
    store = T.create_store(GOOD_ENV, opener=probe)
    try:
        EP.publish_editorial_mix_pool(artifact, store=store, date=DATE, **kwargs)
        check(name, False, "published")
    except EP.EditorialPublishError as error:
        check(name, probe.calls == [] and (expected is None or error.reason == expected),
              f"{error.reason} calls={len(probe.calls)}")


refuses("34. an invalid artifact is never written", {"nope": 1})

broken_identity = copy.deepcopy(ARTIFACT)
broken_identity["editorialPoolIdentity"] = "0" * 64
# The schema validator re-derives the identity itself, so it catches this BEFORE the
# publisher's own identity gate. Either reason is a correct refusal; what matters is that
# no write happened. The publisher's gate remains the backstop for a validator that ever
# stops checking.
refuses("34b. an identity mismatch is never written", broken_identity)

wrong_date = copy.deepcopy(ARTIFACT)
wrong_date["date"] = "2026-07-26"
refuses("34c. a date mismatch is never written", wrong_date, EP.REASON_DATE_MISMATCH)

bad_version = copy.deepcopy(ARTIFACT)
bad_version["schemaVersion"] = 99
refuses("34d. an unsupported schema version is never written", bad_version,
        EP.REASON_VERSION_MISMATCH)

short_pool = copy.deepcopy(ARTIFACT)
short_pool["candidates"] = short_pool["candidates"][:14]
short_pool["candidateCount"] = 14
short_pool["selectorPoolIdentity"] = S.pool_identity(
    [row["selector"] for row in short_pool["candidates"]])
from editorial_mix_pool_schema import editorial_pool_identity  # noqa: E402
short_pool["editorialPoolIdentity"] = editorial_pool_identity(short_pool["candidates"])
refuses("35. an Editorial Mix Pool below the 15-candidate minimum is never written",
        short_pool, EP.REASON_INSUFFICIENT_CANDIDATES)
check("35b. that under-sized pool is otherwise schema-valid, so the count is the only gate",
      validate_editorial_mix_pool(short_pool)["valid"])

probe = Recorder()
try:
    EP.publish_editorial_mix_pool(ARTIFACT, store=None, date=DATE)
    check("35c. a missing store refuses rather than silently succeeding", False, "published")
except EP.EditorialPublishError as error:
    check("35c. a missing store refuses rather than silently succeeding",
          error.reason == EP.REASON_NO_STORE, error.reason)

# ── 36. no live network anywhere in this suite ────────────────────────────────────────

source = open(os.path.join(HERE, "upstash_mix_pool_transport.py"), encoding="utf-8").read()
check("36. the transport opens a socket in exactly one place",
      source.count("urllib.request.urlopen") == 1)

# Parse the module rather than grepping it: prose ABOUT `os.environ` in a docstring is not
# a read of it, and a substring test cannot tell the difference.
import ast  # noqa: E402

tree = ast.parse(source)
imports_os = any(
    (isinstance(node, ast.Import) and any(alias.name == "os" for alias in node.names))
    or (isinstance(node, ast.ImportFrom) and node.module == "os")
    for node in ast.walk(tree)
)
reads_environ = any(
    isinstance(node, ast.Attribute) and node.attr == "environ" for node in ast.walk(tree)
)
check("36b. the transport imports no `os` and reads no environment variable itself",
      not imports_os and not reads_environ, f"import={imports_os} environ={reads_environ}")
logs = [
    node for node in ast.walk(tree)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
]
check("36c. the transport logs nothing", logs == [], str(len(logs)))
check("36d. this suite replaced that one place in every test",
      all(isinstance(r, Recorder) for r in [recorder]))

# ── rerun semantics and the raw-pool publisher's own contract ─────────────────────────

first, rec1 = publish()
second, rec2 = publish()
check("a rerun for the same date targets the identical key and body",
      first["key"] == second["key"]
      and rec1.calls[0]["body"] == rec2.calls[0]["body"]
      and len(rec2.calls) == 1)

check("the raw Mix Pool publisher is untouched and still uses its own namespace",
      __import__("mix_pool_publisher").mix_pool_key(DATE) == f"signals:mix-pool:v1:{DATE}")

# ── the smoke test cannot run by accident ─────────────────────────────────────────────

smoke = open(os.path.join(HERE, "upstash_smoke_test.py"), encoding="utf-8").read()
check("the smoke test requires an explicit non-production confirmation flag",
      "--i-understand-this-is-not-production" in smoke and "required=True" in smoke)
check("the smoke test has no default key and no default date",
      '"--test-date", required=True' in smoke and "SMOKE_NAMESPACE" in smoke)
check("the smoke test writes only to a namespace no reader consults",
      'SMOKE_NAMESPACE = "signals:smoke:' in smoke)
check("the smoke test can express no wildcard, pattern, scan or flush",
      not any(word in smoke for word in ("FLUSHDB", "FLUSHALL", "SCAN", "KEYS ", "*")))
# No workflow may mention it at all. Read the files directly rather than shelling out to
# grep — this suite starts no process of any kind.
WORKFLOW_DIR = os.path.join(HERE, "..", ".github", "workflows")
workflow_refs = [
    name for name in sorted(os.listdir(WORKFLOW_DIR))
    if "upstash_smoke_test"
    in open(os.path.join(WORKFLOW_DIR, name), encoding="utf-8").read()
]
check("no workflow references the smoke test", workflow_refs == [], str(workflow_refs))

# And no test may EXECUTE it. Reading its source as text — which this suite and the workflow
# suite both do — is inspection, not invocation, so parse each test file and look for a real
# import or a subprocess launch rather than grepping for the name.
executors = []
for name in sorted(os.listdir(HERE)):
    if not (name.startswith("test_") and name.endswith(".py")):
        continue
    module = ast.parse(open(os.path.join(HERE, name), encoding="utf-8").read())
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    # A LAUNCH is the name appearing inside a process-starting call. `open(...)` reading the
    # file is not a launch, and neither is a bare string, so look at the call site itself.
    launches = False
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        callee = ast.dump(node.func)
        if not any(
            marker in callee
            for marker in ("subprocess", "'system'", "'popen'", "'execv'", "runpy", "'exec'")
        ):
            continue
        if any(
            isinstance(inner, ast.Constant)
            and isinstance(inner.value, str)
            and "upstash_smoke_test" in inner.value
            for inner in ast.walk(node)
        ):
            launches = True
    if "upstash_smoke_test" in imported or launches:
        executors.append(name)
check("no test imports or launches the smoke test", executors == [], str(executors))

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}:")
    for name in FAILURES:
        print("  -", name)
    raise SystemExit(1)
print("All Upstash transport checks passed.")
