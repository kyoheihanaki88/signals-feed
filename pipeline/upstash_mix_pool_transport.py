#!/usr/bin/env python3
"""Concrete Upstash Redis REST transport for the Mix Pool publisher. (Phase 3D-3D)

NOT PROVISIONED. No Upstash database exists for this project and no credential is present
in GitHub Actions, Vercel, `vercel.json` or any env file. This module is the code that
becomes operational the moment the workflow supplies `KV_REST_API_URL` and
`KV_REST_API_WRITE_TOKEN` — and nothing more. Importing it performs NO action: it opens no
socket, reads no environment variable and publishes nothing. Every effect requires an
explicit call with an explicitly built configuration.

REQUEST FORM — CORRECTED IN PHASE 3D-3F.1 AGAINST REAL ENDPOINT EVIDENCE.

  root POST, body `["PING"]`  ->  HTTP 400
  URL command  GET /ping      ->  HTTP 200  {"result":"PONG"}

Phase 3D-3D used the root JSON-array form. Upstash documents it, but THIS deployment
rejects it, so the transport now uses the URL-command form exclusively. That is not a
fallback: the root form is never constructed anywhere in this module, and a regression
test asserts it.

  SET  ->  POST {base}/set/{key}?EX={ttl}   with the value as the raw REQUEST BODY
  GET  ->  GET  {base}/get/{key}
  DEL  ->  POST {base}/del/{key}
  PING ->  GET  {base}/ping

WHY THE VALUE IS NEVER IN THE URL. Upstash's docs are explicit that a POST body "is
appended to the command as the last parameter", with trailing options supplied as query
parameters — `POST /set/foo?EX=100` is exactly `SET foo <body> EX 100`. That matters
because the artifact is ~16.9 KB of JSON: percent-encoded into a path segment it would
exceed 40 KB, well past the 8–16 KB URL ceiling typical of proxies and CDNs. Only the KEY
travels in the URL (about 45 characters), so the request line stays small and the value
stays binary-safe. Upstash's own request ceiling is 10 MB, far above `MAX_BODY_BYTES`.

KEY ENCODING. Each argument is percent-encoded as its own path segment via
`encode_segment`. `:` is deliberately left literal: RFC 3986 admits it in a path segment,
Upstash's documentation uses it unencoded (`/hget/employee:23381/salary`), and the
TypeScript reader encodes identically — a cross-language test pins the two together, since
a divergence would silently read the wrong object rather than fail.

BYTE EXACTNESS. The value is written as the publisher's exact canonical UTF-8 bytes with no
JSON wrapper, no escaping and no re-serialisation. On the way back, Upstash returns the
stored string inside its `{"result": …}` envelope; the reader encodes that string to UTF-8
and gets the same bytes. The artifact is valid UTF-8 by construction, so the provider's
U+FFFD substitution for invalid sequences cannot apply — and the smoke test's byte
comparison is what proves it rather than this comment.

SAFETY. No logging of any kind. No payload, headline, URL, key material or provider prose
appears in a raised error or a returned result — failures are stable category codes only.
Stdlib only: `urllib.request` is sufficient, so no dependency is added.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlparse

#: Slightly beyond the reader's freshness ceiling so provider expiry is never the gate.
DEFAULT_TTL_SECONDS = 9 * 24 * 60 * 60
#: One request, one deadline. The daily workflow is not latency-sensitive, but it must not
#: hang a runner: a stuck publish should fail visibly rather than block the job.
DEFAULT_TIMEOUT_SECONDS = 10.0
#: Must not exceed the reader's MAX_POOL_BYTES. Upstash's own request ceiling is 10 MB.
MAX_BODY_BYTES = 2 * 1024 * 1024
#: A write reply is tiny (`{"result":"OK"}`); anything larger is not an Upstash SET reply.
MAX_RESPONSE_BYTES = 64 * 1024
#: A read reply carries the whole artifact inside a JSON envelope, so it needs its own,
#: much larger ceiling — JSON string escaping can inflate the value several times over.
MAX_READ_RESPONSE_BYTES = 6 * MAX_BODY_BYTES + 1024

#: `:` stays literal: RFC 3986 permits it inside a path segment, Upstash's documentation
#: uses it unencoded, and `api/_lib/upstash-pool-store.ts` encodes identically. Everything
#: else that could change the target object — `/`, `?`, `#`, space, non-ASCII — is escaped.
_SEGMENT_SAFE = ":"

#: Stable, safe failure categories. Never a provider message.
REASON_NOT_CONFIGURED = "upstash_not_configured"
REASON_PARTIAL_CONFIGURATION = "upstash_partial_configuration"
REASON_INSECURE_URL = "upstash_insecure_url"
REASON_TIMEOUT = "upstash_timeout"
REASON_PROVIDER_ERROR = "upstash_provider_error"
REASON_INVALID_RESPONSE = "upstash_invalid_response"
REASON_VALUE_TOO_LARGE = "upstash_value_too_large"
REASON_PUBLISH_REJECTED = "upstash_publish_rejected"
#: The credential itself cannot be put on the wire. HTTP header values are encoded as
#: latin-1 by `http.client`, so a token carrying a typographic character — a smart quote or
#: an en dash picked up by copying from a rendered document rather than the console's copy
#: button — raises UnicodeEncodeError deep inside the socket layer, long after the artifact
#: has been built. Caught here instead, before anything is sent.
REASON_INVALID_CREDENTIAL = "upstash_invalid_credential"
#: The artifact could not be encoded as UTF-8. For valid Python text this is unreachable;
#: it is reachable for a LONE SURROGATE, which is rejected rather than silently replaced.
REASON_ARTIFACT_ENCODING = "artifact_utf8_encoding_failed"
REASON_NETWORK_ERROR = "upstash_network_error"

#: SAFE PROVIDER CATEGORIES. Derived from the HTTP status and, when the provider sends a
#: JSON `error`, from a keyword match on that text — which is read ONLY long enough to
#: classify and is then discarded. The prose itself never leaves this module, because an
#: Upstash error can echo the key and, in some shapes, the command.
CATEGORY_PERMISSION_DENIED = "permission_denied"
CATEGORY_AUTHENTICATION_FAILED = "authentication_failed"
CATEGORY_INVALID_REQUEST = "invalid_request"
CATEGORY_PAYLOAD_TOO_LARGE = "payload_too_large"
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_NOT_FOUND = "endpoint_not_found"
CATEGORY_PROVIDER_UNAVAILABLE = "provider_unavailable"
CATEGORY_PROVIDER_REJECTED = "provider_rejected"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_NETWORK = "network_error"
CATEGORY_MALFORMED_RESPONSE = "malformed_response"

#: Keyword -> category. Matched case-insensitively against the provider's `error` string.
#: Ordered: first match wins.
_PROVIDER_ERROR_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("noperm", CATEGORY_PERMISSION_DENIED),
    ("permission", CATEGORY_PERMISSION_DENIED),
    ("read only", CATEGORY_PERMISSION_DENIED),
    ("readonly", CATEGORY_PERMISSION_DENIED),
    ("not allowed", CATEGORY_PERMISSION_DENIED),
    ("wrongpass", CATEGORY_AUTHENTICATION_FAILED),
    ("unauthorized", CATEGORY_AUTHENTICATION_FAILED),
    ("unauthenticated", CATEGORY_AUTHENTICATION_FAILED),
    ("invalid auth", CATEGORY_AUTHENTICATION_FAILED),
    ("invalid token", CATEGORY_AUTHENTICATION_FAILED),
    ("wrong number of arguments", CATEGORY_INVALID_REQUEST),
    ("unknown command", CATEGORY_INVALID_REQUEST),
    ("syntax", CATEGORY_INVALID_REQUEST),
    ("max request size", CATEGORY_PAYLOAD_TOO_LARGE),
    ("too large", CATEGORY_PAYLOAD_TOO_LARGE),
    ("daily request limit", CATEGORY_RATE_LIMITED),
    ("rate limit", CATEGORY_RATE_LIMITED),
    ("exceeded", CATEGORY_RATE_LIMITED),
)

#: HTTP status -> (reason, category). Upstash answers 400 for a command the token may not
#: run, which is why the body keyword match refines it when present.
_STATUS_MAP: dict[int, tuple[str, str]] = {
    400: ("upstash_http_400", CATEGORY_INVALID_REQUEST),
    401: ("upstash_http_401", CATEGORY_AUTHENTICATION_FAILED),
    403: ("upstash_http_403", CATEGORY_PERMISSION_DENIED),
    404: ("upstash_http_404", CATEGORY_NOT_FOUND),
    405: ("upstash_http_405", CATEGORY_INVALID_REQUEST),
    413: ("upstash_http_413", CATEGORY_PAYLOAD_TOO_LARGE),
    429: ("upstash_http_429", CATEGORY_RATE_LIMITED),
}


def classify_provider_failure(status: int, body: bytes | None) -> tuple[str, str]:
    """
    Turn an HTTP status and an OPAQUE response body into a stable (reason, category).

    The body is inspected only to match a keyword; neither it, nor any substring of it, nor
    its length is ever returned. A body-derived category REFINES the status-derived one —
    Upstash reports a permission refusal as HTTP 400, so the status alone would call it a
    malformed request.
    """
    reason, category = _STATUS_MAP.get(int(status), ("", ""))
    if not reason:
        reason = "upstash_http_5xx" if 500 <= int(status) <= 599 else "upstash_http_other"
        category = (CATEGORY_PROVIDER_UNAVAILABLE if 500 <= int(status) <= 599
                    else CATEGORY_PROVIDER_REJECTED)

    if body:
        try:
            text = bytes(body[:MAX_RESPONSE_BYTES]).decode("utf-8", errors="ignore").lower()
        except Exception:  # noqa: BLE001 - classification must never itself raise
            text = ""
        for keyword, refined in _PROVIDER_ERROR_KEYWORDS:
            if keyword in text:
                category = refined
                break
    return reason, category

#: The variable names the WRITE path uses. Deliberately different from the API read path's
#: `KV_REST_API_TOKEN` so a read credential can never be pasted into the publisher secret
#: by accident, and so the two can be rotated independently.
ENV_URL = "KV_REST_API_URL"
ENV_WRITE_TOKEN = "KV_REST_API_WRITE_TOKEN"


class UpstashTransportError(RuntimeError):
    """
    A transport failure. Carries stable categories and an HTTP status — nothing else.

    `http_status` is the numeric status the provider returned, which is safe: it names the
    KIND of refusal without revealing the endpoint, the credential, the key or the body.
    `provider_category` further distinguishes permission from authentication from request
    shape, which a bare `upstash_provider_error` could not.
    """

    def __init__(self, reason: str, *, http_status: int | None = None,
                 provider_category: str | None = None, stage: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status
        self.provider_category = provider_category
        self.stage = stage

    def safe_detail(self) -> dict[str, Any]:
        """Everything safe to print. Never a URL, token, body or provider sentence."""
        detail: dict[str, Any] = {"reason": self.reason}
        if self.stage:
            detail["stage"] = self.stage
        if self.http_status is not None:
            detail["httpStatus"] = self.http_status
        if self.provider_category:
            detail["providerCategory"] = self.provider_category
        return detail


@dataclass(frozen=True)
class UpstashConfig:
    """A validated REST endpoint and write credential. Never rendered into a message."""

    rest_url: str
    write_token: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __repr__(self) -> str:  # pragma: no cover - defensive, but cheap and important
        # A dataclass repr would print the token in any traceback that touches it.
        return "UpstashConfig(rest_url=<redacted>, write_token=<redacted>)"

    __str__ = __repr__


def resolve_config(
    env: dict[str, str] | None,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> UpstashConfig:
    """
    Build a config from an explicitly supplied mapping. Fails CLOSED.

    The caller passes the mapping — this module never reads `os.environ` itself, so nothing
    can be configured implicitly by importing it. The three states an operator needs to
    tell apart (nothing set / half set / set but unusable) get distinct reasons.
    """
    source = env or {}
    url = str(source.get(ENV_URL, "") or "").strip()
    token = str(source.get(ENV_WRITE_TOKEN, "") or "").strip()

    if not url and not token:
        raise UpstashTransportError(REASON_NOT_CONFIGURED)
    if not url or not token:
        raise UpstashTransportError(REASON_PARTIAL_CONFIGURATION)

    parsed = urlparse(url)
    # Plaintext would put a write token on the wire. Refuse rather than downgrade.
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpstashTransportError(REASON_INSECURE_URL)
    if parsed.query or parsed.fragment:
        raise UpstashTransportError(REASON_INSECURE_URL)

    # Both values end up in a request line / header, which `http.client` encodes as
    # latin-1. Require plain ASCII: a REST URL and an Upstash token are ASCII by
    # construction, so anything else is a corrupted paste, not a legitimate credential.
    # The offending value is NEVER echoed — only the category leaves this function.
    for candidate in (url, token):
        try:
            candidate.encode("ascii")
        except UnicodeEncodeError:
            raise UpstashTransportError(REASON_INVALID_CREDENTIAL) from None

    normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return UpstashConfig(
        rest_url=normalized, write_token=token, timeout_seconds=timeout_seconds
    )


def encode_segment(argument: str) -> str:
    """
    Percent-encode ONE Redis argument as its own URL path segment.

    Mirrored exactly by `encodePathSegment` in `api/_lib/upstash-pool-store.ts`; a
    cross-language test pins them together, because a divergence would make the reader ask
    for a different object than the publisher wrote — silently, and with a valid response.
    """
    if not isinstance(argument, str) or not argument:
        raise UpstashTransportError(REASON_PUBLISH_REJECTED)
    return quote(argument, safe=_SEGMENT_SAFE)


def command_url(base: str, *arguments: str, query: str = "") -> str:
    """
    Build a URL-command request target: `{base}/arg1/arg2/…`.

    This is the ONLY way a request URL is constructed in this module. The root JSON-array
    form is never built — this deployment answers it with HTTP 400 (recorded in the module
    docstring), and a regression test asserts the form is absent from the source.
    """
    path = "/".join(encode_segment(argument) for argument in arguments)
    return f"{base}/{path}{query}"


def _from_http_error(error: urllib.error.HTTPError) -> tuple[str, dict[str, Any]]:
    """
    Extract the SAFE facts from an HTTPError: the status, and a category derived from a
    keyword scan of the body. The body itself is read into a local, classified, and
    dropped — it is never stored on the exception, returned or logged.
    """
    status = int(getattr(error, "code", 0) or 0)
    try:
        body = error.read(MAX_RESPONSE_BYTES)
    except Exception:  # noqa: BLE001 - a body we cannot read simply carries no keyword
        body = b""
    reason, category = classify_provider_failure(status, body)
    return reason, {"http_status": status, "provider_category": category}


def _default_opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """The only place a socket is opened. Isolated so tests can replace it wholesale."""
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.status, response.read(MAX_READ_RESPONSE_BYTES + 1)


class UpstashPoolObjectStore:
    """
    Implements the publisher's `PoolObjectStore` protocol with one atomic Upstash SET.

    Construction validates nothing about the network — it cannot, and pretending otherwise
    would be a false readiness signal. The first real proof that credentials work is the
    manual smoke test described in `UPSTASH_PROVISIONING.md`.
    """

    def __init__(
        self,
        config: UpstashConfig,
        *,
        opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
        max_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self._config = config
        self._opener = opener or _default_opener
        self._max_bytes = max_bytes

    def put(self, key: str, body: bytes, *, ttl_seconds: int | None) -> None:
        """
        Store `body` at `key` atomically, with an expiry.

        Raises `UpstashTransportError` with a category code. A partial write is impossible:
        Redis SET replaces the whole value in one command, so a rerun for the same date
        atomically supersedes the previous artifact rather than merging with it.
        """
        if not isinstance(key, str) or not key:
            raise UpstashTransportError(REASON_PUBLISH_REJECTED)
        if not isinstance(body, (bytes, bytearray)):
            raise UpstashTransportError(REASON_PUBLISH_REJECTED)
        if len(body) == 0:
            raise UpstashTransportError(REASON_PUBLISH_REJECTED)
        if len(body) > self._max_bytes:
            raise UpstashTransportError(REASON_VALUE_TOO_LARGE)

        # BYTES-ONLY CONTRACT. The publisher owns the single str -> bytes boundary
        # (`mix_pool_schema.serialize`); the transport never encodes text, so no caller can
        # choose a different codec and no value can be double-encoded.
        try:
            bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            # The canonical serializer always emits UTF-8; anything else is a caller bug.
            raise UpstashTransportError(REASON_PUBLISH_REJECTED) from None

        # `SET {key} {body} EX {ttl}`. The value is the raw request body, never a URL
        # segment and never wrapped in JSON, so a 16.9 KB artifact costs a ~45-character
        # request line. Trailing options must be query parameters — Upstash appends the
        # body as the LAST argument, so `EX` cannot follow it in the path.
        query = ""
        if ttl_seconds is not None:
            if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
                raise UpstashTransportError(REASON_PUBLISH_REJECTED)
            query = f"?EX={int(ttl_seconds)}"

        request = urllib.request.Request(  # noqa: S310 - scheme validated in resolve_config
            command_url(self._config.rest_url, "set", key, query=query),
            data=bytes(body),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._config.write_token}",
                # Upstash does not interpret this: it takes the body verbatim as the last
                # command argument. `octet-stream` states honestly that it is opaque bytes
                # and stops an intermediary from trying to re-encode a form or JSON body.
                "Content-Type": "application/octet-stream",
            },
        )

        try:
            status, raw = self._opener(request, self._config.timeout_seconds)
        except UnicodeEncodeError:
            # A header value the socket layer could not encode. `resolve_config` rejects
            # this at configuration time; this is the backstop, and it still reveals
            # nothing about the value.
            raise UpstashTransportError(REASON_INVALID_CREDENTIAL) from None
        except (TimeoutError, socket.timeout):
            raise UpstashTransportError(REASON_TIMEOUT, provider_category=CATEGORY_TIMEOUT,
                                        stage="set") from None
        except urllib.error.HTTPError as error:
            # `urlopen` raises for EVERY non-2xx, so this is the path a real provider
            # refusal takes — and until Phase 3D-3G.10 it discarded `error.code`, which is
            # exactly the fact needed to tell permission from auth from request shape.
            # The body is read only to classify and is never retained.
            reason, detail = _from_http_error(error)
            raise UpstashTransportError(reason, stage="set", **detail) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise UpstashTransportError(REASON_TIMEOUT,
                                            provider_category=CATEGORY_TIMEOUT,
                                            stage="set") from None
            raise UpstashTransportError(REASON_NETWORK_ERROR,
                                        provider_category=CATEGORY_NETWORK,
                                        stage="set") from None
        except OSError:
            raise UpstashTransportError(REASON_NETWORK_ERROR,
                                        provider_category=CATEGORY_NETWORK,
                                        stage="set") from None

        if status != 200:
            reason, category = classify_provider_failure(status, raw)
            raise UpstashTransportError(reason, http_status=status,
                                        provider_category=category, stage="set")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RESPONSE_BYTES:
            raise UpstashTransportError(REASON_INVALID_RESPONSE)

        try:
            envelope = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise UpstashTransportError(REASON_INVALID_RESPONSE) from None

        if not isinstance(envelope, dict):
            raise UpstashTransportError(REASON_INVALID_RESPONSE)
        # A 200 carrying `error` is Upstash reporting a command failure.
        if "error" in envelope:
            # A 200 carrying `error` is a command-level refusal. Classify from the text,
            # then drop it.
            _, category = classify_provider_failure(200, raw)
            raise UpstashTransportError(REASON_PROVIDER_ERROR, http_status=200,
                                        provider_category=category, stage="set")
        # Redis answers a successful SET with the simple string OK. Anything else means the
        # command did not do what we asked, and we must not report a successful publish.
        if envelope.get("result") != "OK":
            raise UpstashTransportError(REASON_INVALID_RESPONSE)


def create_store(
    env: dict[str, str] | None,
    *,
    opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> UpstashPoolObjectStore:
    """Resolve configuration and build the store. Raises `UpstashTransportError` if unusable."""
    config = resolve_config(env, timeout_seconds=timeout_seconds)
    return UpstashPoolObjectStore(config, opener=opener)


# ── verification helpers ──────────────────────────────────────────────────────────────
#
# These exist for the MANUAL smoke test only, which is why they are module-level functions
# rather than methods: the publisher store stays write-only, so nothing in the daily path
# can accidentally read or delete. Both act on ONE exact key and can express no pattern,
# no scan and no flush — a wildcard is not representable here.


def _command(
    config: UpstashConfig,
    command: list[str],
    *,
    method: str = "POST",
    opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
) -> Any:
    """
    Run one URL-command request and return its `result`. Shared by the helpers below.

    Every argument becomes its own percent-encoded path segment; no argument is ever
    concatenated raw, and no request body is sent — these commands carry no value.
    """
    request = urllib.request.Request(  # noqa: S310 - scheme validated in resolve_config
        command_url(config.rest_url, *command),
        method=method,
        headers={"Authorization": f"Bearer {config.write_token}"},
    )
    run = opener or _default_opener
    stage = str(command[0]).lower() if command else "command"
    try:
        status, raw = run(request, config.timeout_seconds)
    except UnicodeEncodeError:
        raise UpstashTransportError(REASON_INVALID_CREDENTIAL, stage=stage) from None
    except (TimeoutError, socket.timeout):
        raise UpstashTransportError(REASON_TIMEOUT, provider_category=CATEGORY_TIMEOUT,
                                    stage=stage) from None
    except urllib.error.HTTPError as error:
        reason, detail = _from_http_error(error)
        raise UpstashTransportError(reason, stage=stage, **detail) from None
    except urllib.error.URLError as error:
        if isinstance(getattr(error, "reason", None), (TimeoutError, socket.timeout)):
            raise UpstashTransportError(REASON_TIMEOUT, provider_category=CATEGORY_TIMEOUT,
                                        stage=stage) from None
        raise UpstashTransportError(REASON_NETWORK_ERROR, provider_category=CATEGORY_NETWORK,
                                    stage=stage) from None
    except OSError:
        raise UpstashTransportError(REASON_NETWORK_ERROR, provider_category=CATEGORY_NETWORK,
                                    stage=stage) from None

    if status != 200:
        reason, category = classify_provider_failure(status, raw)
        raise UpstashTransportError(reason, http_status=status, provider_category=category,
                                    stage=stage)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_READ_RESPONSE_BYTES:
        raise UpstashTransportError(REASON_VALUE_TOO_LARGE)
    try:
        envelope = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UpstashTransportError(REASON_INVALID_RESPONSE) from None
    if not isinstance(envelope, dict):
        raise UpstashTransportError(REASON_INVALID_RESPONSE)
    if "error" in envelope:
        _, category = classify_provider_failure(200, raw)
        raise UpstashTransportError(REASON_PROVIDER_ERROR, http_status=200,
                                    provider_category=category, stage=stage)
    if "result" not in envelope:
        raise UpstashTransportError(REASON_INVALID_RESPONSE)
    return envelope["result"]


def ping(
    config: UpstashConfig,
    *,
    opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
) -> bool:
    """
    `GET {base}/ping` — the cheapest proof that the URL, token and request form all work.

    This is the exact request that answered HTTP 200 / PONG when the root JSON-array form
    answered 400, so it is also the canary that would catch a future form regression.
    """
    return _command(config, ["ping"], method="GET", opener=opener) == "PONG"


def read_back(
    config: UpstashConfig,
    key: str,
    *,
    opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
) -> bytes | None:
    """Fetch one exact key's bytes, or None when absent. Smoke-test verification only."""
    result = _command(config, ["get", key], method="GET", opener=opener)
    if result is None:
        return None
    if not isinstance(result, str):
        raise UpstashTransportError(REASON_INVALID_RESPONSE)
    return result.encode("utf-8")


def delete_key(
    config: UpstashConfig,
    key: str,
    *,
    opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
) -> int:
    """Delete ONE exact key. Returns the number removed (0 or 1). Smoke-test cleanup only."""
    result = _command(config, ["del", key], opener=opener)
    if not isinstance(result, int):
        raise UpstashTransportError(REASON_INVALID_RESPONSE)
    return result
