#!/usr/bin/env python3
"""Concrete Upstash Redis REST transport for the Mix Pool publisher. (Phase 3D-3D)

NOT PROVISIONED. No Upstash database exists for this project and no credential is present
in GitHub Actions, Vercel, `vercel.json` or any env file. This module is the code that
becomes operational the moment the workflow supplies `KV_REST_API_URL` and
`KV_REST_API_WRITE_TOKEN` — and nothing more. Importing it performs NO action: it opens no
socket, reads no environment variable and publishes nothing. Every effect requires an
explicit call with an explicitly built configuration.

WHY THE COMMAND FORM. Upstash exposes both `POST {url}/set/{key}` and a JSON command body.
The key contains `:` separators and the value is a whole JSON document; sending
`["SET", key, value, "EX", ttl]` puts both in the body as DATA, so no path encoding, query
string or proxy can alter the key we write or the bytes we store. It is also exactly ONE
request — the publisher contract forbids a multi-step assembly that could leave a partial
object behind.

BYTE EXACTNESS. The value is the publisher's canonical UTF-8 bytes, decoded to `str` only
so the JSON request body can carry it. JSON string escaping is lossless for valid UTF-8, so
what Upstash stores round-trips to the same bytes the TypeScript reader will hash. This
module never reformats, re-sorts or re-serialises the artifact.

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
from urllib.parse import urlparse

#: Slightly beyond the reader's freshness ceiling so provider expiry is never the gate.
DEFAULT_TTL_SECONDS = 9 * 24 * 60 * 60
#: One request, one deadline. The daily workflow is not latency-sensitive, but it must not
#: hang a runner: a stuck publish should fail visibly rather than block the job.
DEFAULT_TIMEOUT_SECONDS = 10.0
#: Must not exceed the reader's MAX_POOL_BYTES.
MAX_BODY_BYTES = 2 * 1024 * 1024
#: The provider's own reply is tiny (`{"result":"OK"}`); anything larger is not Upstash.
MAX_RESPONSE_BYTES = 64 * 1024

#: Stable, safe failure categories. Never a provider message.
REASON_NOT_CONFIGURED = "upstash_not_configured"
REASON_PARTIAL_CONFIGURATION = "upstash_partial_configuration"
REASON_INSECURE_URL = "upstash_insecure_url"
REASON_TIMEOUT = "upstash_timeout"
REASON_PROVIDER_ERROR = "upstash_provider_error"
REASON_INVALID_RESPONSE = "upstash_invalid_response"
REASON_VALUE_TOO_LARGE = "upstash_value_too_large"
REASON_PUBLISH_REJECTED = "upstash_publish_rejected"

#: The variable names the WRITE path uses. Deliberately different from the API read path's
#: `KV_REST_API_TOKEN` so a read credential can never be pasted into the publisher secret
#: by accident, and so the two can be rotated independently.
ENV_URL = "KV_REST_API_URL"
ENV_WRITE_TOKEN = "KV_REST_API_WRITE_TOKEN"


class UpstashTransportError(RuntimeError):
    """A transport failure. Carries a category code and nothing else."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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

    normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return UpstashConfig(
        rest_url=normalized, write_token=token, timeout_seconds=timeout_seconds
    )


def _default_opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """The only place a socket is opened. Isolated so tests can replace it wholesale."""
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.status, response.read(MAX_RESPONSE_BYTES + 1)


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

        try:
            value = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            # The canonical serializer always emits UTF-8; anything else is a caller bug.
            raise UpstashTransportError(REASON_PUBLISH_REJECTED) from None

        command: list[Any] = ["SET", key, value]
        if ttl_seconds is not None:
            if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
                raise UpstashTransportError(REASON_PUBLISH_REJECTED)
            command += ["EX", str(ttl_seconds)]

        payload = json.dumps(command, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme validated in resolve_config
            self._config.rest_url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._config.write_token}",
                "Content-Type": "application/json",
            },
        )

        try:
            status, raw = self._opener(request, self._config.timeout_seconds)
        except (TimeoutError, socket.timeout):
            raise UpstashTransportError(REASON_TIMEOUT) from None
        except urllib.error.HTTPError:
            # The body can echo the key and, in some shapes, the command. Discard it.
            raise UpstashTransportError(REASON_PROVIDER_ERROR) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise UpstashTransportError(REASON_TIMEOUT) from None
            raise UpstashTransportError(REASON_PROVIDER_ERROR) from None
        except OSError:
            raise UpstashTransportError(REASON_PROVIDER_ERROR) from None

        if status != 200:
            raise UpstashTransportError(REASON_PROVIDER_ERROR)
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
            raise UpstashTransportError(REASON_PROVIDER_ERROR)
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
    opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
) -> Any:
    """Run one Upstash command and return its `result`. Shared by the helpers below."""
    payload = json.dumps(command, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - scheme validated in resolve_config
        config.rest_url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.write_token}",
            "Content-Type": "application/json",
        },
    )
    run = opener or _default_opener
    try:
        status, raw = run(request, config.timeout_seconds)
    except (TimeoutError, socket.timeout):
        raise UpstashTransportError(REASON_TIMEOUT) from None
    except urllib.error.URLError as error:
        if isinstance(getattr(error, "reason", None), (TimeoutError, socket.timeout)):
            raise UpstashTransportError(REASON_TIMEOUT) from None
        raise UpstashTransportError(REASON_PROVIDER_ERROR) from None
    except OSError:
        raise UpstashTransportError(REASON_PROVIDER_ERROR) from None

    if status != 200:
        raise UpstashTransportError(REASON_PROVIDER_ERROR)
    try:
        envelope = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UpstashTransportError(REASON_INVALID_RESPONSE) from None
    if not isinstance(envelope, dict):
        raise UpstashTransportError(REASON_INVALID_RESPONSE)
    if "error" in envelope:
        raise UpstashTransportError(REASON_PROVIDER_ERROR)
    if "result" not in envelope:
        raise UpstashTransportError(REASON_INVALID_RESPONSE)
    return envelope["result"]


def read_back(
    config: UpstashConfig,
    key: str,
    *,
    opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
) -> bytes | None:
    """Fetch one exact key's bytes, or None when absent. Smoke-test verification only."""
    result = _command(config, ["GET", key], opener=opener)
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
    result = _command(config, ["DEL", key], opener=opener)
    if not isinstance(result, int):
        raise UpstashTransportError(REASON_INVALID_RESPONSE)
    return result
