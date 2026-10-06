"""AgentCore Platform v1.0 - kintone REST API client.

Service layer: a thin wrapper around the kintone (Cybozu app-record SaaS) REST
API record endpoints. Contains NO business logic, NO routing, and NO
credentials - the integration token is passed in per call by the node (which
reads it via ctx.secrets). This module imports no framework/SDK internals -
pure stdlib.

LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented kintone response shapes (a ``record`` object for lookups; the
    ``{"id", "revision"}`` receipt shape with a synthetic ``id`` echo for
    create/update, derived from the request) so the pipeline is runnable and
    testable without a live kintone tenant or the ``requests`` package - it
    does NOT perform a live kintone call. The template never fakes a live call;
    the limitation is stated instead.

    To perform real kintone calls, inject live transports (requests-based
    ``post`` / ``put`` / ``get``) at construction time and set the tenant
    base_url (https://<subdomain>.cybozu.com/k/v1) under `kintone:` in
    config/config.yaml; the method contracts and payload shapes are already
    kintone REST API exact (GET/POST/PUT /record.json), so no business-logic
    change is needed to go live. A live transport also requires a real
    integration token (see CallKintoneApiNode - the stub runs without one
    because no request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

# Placeholder tenant - a live deployment sets the real subdomain via the
# `kintone.base_url` entry in config/config.yaml (the stub performs no network I/O).
_BASE_URL = "https://example.cybozu.com/k/v1"


class KintoneApiError(Exception):
    """Raised when the kintone REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"kintone API error {status_code}: {message}")


class KintoneClient:
    """kintone REST API record client.

    Args:
        base_url: kintone API base URL (default the documented placeholder
            https://example.cybozu.com/k/v1; live tenants use their subdomain).
        post/put/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE stub is used
            (see the module docstring - it returns the documented shape without
            a live kintone call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
        put: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post
        self._put = put
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free default)."""
        return self._post is None and self._put is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the kintone REST API auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "X-Cybozu-API-Token": api_token,
        }

    # -- deterministic stub transport (default; NO network) -------------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free stub - returns the documented kintone shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the
        limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        if json_body.get("_kintone_op") == "lookup":
            rid = str(json_body.get("id", "")) or f"r-{digest[:8]}"
            # Documented GET /record.json shape: {"record": {field: {type, value}}}.
            return 200, {
                "record": {
                    "$id": {"type": "__ID__", "value": rid},
                    "$revision": {"type": "__REVISION__", "value": "1"},
                    "title": {"type": "SINGLE_LINE_TEXT", "value": f"Record {rid}"},
                },
                "_stub": True,  # marks the network-free stub response
            }
        # POST /record.json (create) / PUT /record.json (update) - documented
        # receipt shape {"id", "revision"}; for update (whose live response
        # carries only "revision") the request id is echoed so the caller can
        # reference the affected record without a follow-up lookup.
        rid = str(json_body.get("id", "")) or f"r-{digest[:8]}"
        return 200, {
            "id": rid,
            "revision": "1",
            "_stub": True,  # marks the network-free stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def get_record(self, app_id: str, record_id: str, api_token: str) -> "dict[str, Any]":
        """GET /record.json - look up one app record by app id + record number.

        The live kintone endpoint takes ``app`` and ``id`` as query params; a
        live ``get`` transport adapter is expected to translate the json_body
        into those params (the stub consumes it directly). Returns the parsed
        response dict (containing ``record``). Raises KintoneApiError on non-2xx.
        """
        url = f"{self._base_url}/record.json"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_token),
            {"_kintone_op": "lookup", "app": app_id, "id": record_id},
        )
        if not (200 <= status < 300):
            raise KintoneApiError(status, _err_message(body))
        return body

    def add_record(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /record.json - register a new app record.

        ``payload`` is the documented ``{"app": ..., "record": {...}}`` request
        body. Returns the parsed response dict (``{"id", "revision"}``). Raises
        KintoneApiError on a non-2xx status.
        """
        url = f"{self._base_url}/record.json"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise KintoneApiError(status, _err_message(body))
        return body

    def update_record(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """PUT /record.json - partially update an existing app record.

        ``payload`` is the documented ``{"app": ..., "id": ..., "record": {...}}``
        request body. Returns the parsed response dict (receipt). Raises
        KintoneApiError on a non-2xx status.
        """
        url = f"{self._base_url}/record.json"
        transport = self._resolve(self._put)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise KintoneApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a kintone error body."""
    if isinstance(body, dict):
        msg = body.get("message")
        if msg:
            return str(msg)
        errors = body.get("errors")
        if errors:
            return str(errors)
    return str(body)
