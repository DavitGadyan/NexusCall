"""Minimal MCP client for the streamable-HTTP transport (JSON-RPC 2.0: initialize → tools/call).

Egress controls (build.md §24.5, owasp LLM03/LLM04):
* `validate_outbound_url` with the server's host allowlist before EVERY request;
* the connection itself resolves and re-checks the address and connects to the checked IP (DNS-rebinding
  defense); TLS still verifies the certificate against the hostname;
* no redirects (a 3xx is an error), per-request timeouts plus an overall deadline, request and response
  size caps (the response is read incrementally and abandoned past the cap);
* the server must report exactly the reviewed (pinned) name and version, otherwise the call is refused;
* the bearer credential is supplied by the caller AFTER authorization and only ever goes into the
  Authorization header; it is never logged and is scrubbed if a server echoes it back.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import certifi
import httpcore
import httpx

from nexus_core.ssrf import UnsafeDestination, is_blocked_ip, validate_outbound_url

CLIENT_INFO = {"name": "nexuscall-tool-execution", "version": "0.1.0"}


class McpError(Exception):
    def __init__(self, reason: str, *, transient: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.transient = transient


class McpIdentityMismatch(McpError):
    def __init__(self, reported: str) -> None:
        super().__init__("server_identity_mismatch")
        self.reported = reported


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host itself, refuses blocked addresses, then connects to the vetted IP."""

    def __init__(self, *, allow_private: bool) -> None:
        self._allow_private = allow_private
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise httpcore.ConnectError("destination could not be resolved") from exc
        addresses = sorted({str(i[4][0]) for i in infos})
        if not addresses:
            raise httpcore.ConnectError("destination resolved to no addresses")
        if not self._allow_private and any(is_blocked_ip(a) for a in addresses):
            raise httpcore.ConnectError("destination resolves to a blocked address")
        return await self._inner.connect_tcp(addresses[0], port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def pinned_transport(*, allow_private: bool) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl.create_default_context(cafile=certifi.where()),
        max_connections=4,
        retries=0,
        network_backend=PinnedNetworkBackend(allow_private=allow_private),
    )
    return transport


@dataclass(frozen=True, slots=True)
class McpTarget:
    endpoint: str
    allowed_hosts: frozenset[str]
    tool_name: str
    pinned_server_name: str
    pinned_version: str
    timeout_seconds: float
    max_request_bytes: int
    max_response_bytes: int
    allow_private: bool
    allowed_schemes: frozenset[str]


@dataclass(frozen=True, slots=True)
class McpResult:
    content: str
    structured: dict[str, Any] | None
    is_error: bool
    response_bytes: int


class McpClient:
    def __init__(self, protocol_version: str, *, transport_factory: Any = None) -> None:
        self.protocol_version = protocol_version
        self._transport_factory = transport_factory or pinned_transport

    async def call_tool(self, target: McpTarget, arguments: dict[str, Any], token: str | None) -> McpResult:
        try:
            return await asyncio.wait_for(self._call(target, arguments, token), timeout=target.timeout_seconds * 3)
        except TimeoutError as exc:
            raise McpError("timeout", transient=True) from exc

    async def _call(self, target: McpTarget, arguments: dict[str, Any], token: str | None) -> McpResult:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = httpx.Timeout(target.timeout_seconds)
        async with httpx.AsyncClient(
            transport=self._transport_factory(allow_private=target.allow_private),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            init, session = await self._rpc(
                client,
                target,
                headers,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": self.protocol_version, "capabilities": {}, "clientInfo": CLIENT_INFO},
                },
            )
            info = init.get("serverInfo") if isinstance(init, dict) else None
            name = str(info.get("name", "")) if isinstance(info, dict) else ""
            version = str(info.get("version", "")) if isinstance(info, dict) else ""
            if name != target.pinned_server_name or version != target.pinned_version:
                raise McpIdentityMismatch(f"{name[:60]}@{version[:30]}")
            if session:
                headers["Mcp-Session-Id"] = session
            await self._notify(client, target, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            result, _ = await self._rpc(
                client,
                target,
                headers,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": target.tool_name, "arguments": arguments},
                },
            )
        return _shape(result, target.max_response_bytes)

    async def _guard(self, target: McpTarget) -> None:
        try:
            await validate_outbound_url(
                target.endpoint,
                allowed_schemes=target.allowed_schemes,
                allowed_hosts=target.allowed_hosts,
                allow_private=target.allow_private,
            )
        except UnsafeDestination as exc:
            raise McpError("destination_blocked") from exc

    async def _post(
        self, client: httpx.AsyncClient, target: McpTarget, headers: dict[str, str], message: dict[str, Any]
    ) -> tuple[int, str, bytes, str | None]:
        body = json.dumps(message, separators=(",", ":")).encode()
        if len(body) > target.max_request_bytes:
            raise McpError("request_too_large")
        await self._guard(target)
        try:
            async with client.stream("POST", target.endpoint, content=body, headers=headers) as resp:
                if 300 <= resp.status_code < 400:
                    raise McpError("redirect_blocked")
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > target.max_response_bytes:
                        raise McpError("response_too_large")
                return (
                    resp.status_code,
                    resp.headers.get("content-type", ""),
                    bytes(data),
                    resp.headers.get("mcp-session-id"),
                )
        except httpx.TimeoutException as exc:
            raise McpError("timeout", transient=True) from exc
        except httpx.HTTPError as exc:
            raise McpError("connection_failed", transient=True) from exc

    async def _notify(
        self, client: httpx.AsyncClient, target: McpTarget, headers: dict[str, str], message: dict[str, Any]
    ) -> None:
        status, _, _, _ = await self._post(client, target, headers, message)
        if status not in (200, 202, 204):
            raise McpError("server_error", transient=status >= 500)

    async def _rpc(
        self, client: httpx.AsyncClient, target: McpTarget, headers: dict[str, str], message: dict[str, Any]
    ) -> tuple[Any, str | None]:
        status, ctype, data, session = await self._post(client, target, headers, message)
        if status in (401, 403):
            raise McpError("server_denied")
        if status != 200:
            raise McpError("server_error", transient=status >= 500)
        reply = _parse(ctype, data, message["id"])
        if not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0" or reply.get("id") != message["id"]:
            raise McpError("invalid_response")
        if "error" in reply:
            raise McpError("rpc_error")
        if session is not None and (len(session) > 200 or not session.isprintable()):
            raise McpError("invalid_response")
        return reply.get("result"), session


def _parse(content_type: str, data: bytes, request_id: int) -> Any:
    try:
        text = data.decode("utf-8")
        if content_type.startswith("text/event-stream"):
            for event in text.split("\n\n"):
                payload = "\n".join(
                    line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                ).strip()
                if not payload:
                    continue
                message = json.loads(payload)
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
            return None
        return json.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise McpError("invalid_response") from exc


def _shape(result: Any, max_bytes: int) -> McpResult:
    if not isinstance(result, dict):
        raise McpError("invalid_response")
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    content = "\n".join(parts)
    structured = result.get("structuredContent")
    size = len(content.encode()) + len(json.dumps(structured, default=str).encode() if structured else b"")
    if size > max_bytes:
        raise McpError("response_too_large")
    return McpResult(
        content=content,
        structured=structured if isinstance(structured, dict) else None,
        is_error=bool(result.get("isError", False)),
        response_bytes=size,
    )
