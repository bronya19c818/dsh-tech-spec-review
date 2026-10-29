from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .models import Rule
from .rules import RegistryClient


def load_endpoint(path: str, *, host_override: str = "") -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    servers = payload.get("servers", payload.get("mcpServers", {}))
    if len(servers) != 1:
        raise ValueError("MCP_CONFIG_REQUIRES_ONE_SERVER")
    endpoint = next(iter(servers.values()))
    if not isinstance(endpoint.get("url"), str) or not endpoint["url"].startswith(
        ("http://", "https://")
    ):
        raise ValueError("MCP_HTTP_URL_REQUIRED")
    if host_override:
        if any(character in host_override for character in ":/@?#"):
            raise ValueError("MCP_HOST_OVERRIDE_INVALID")
        parsed = urlsplit(endpoint["url"])
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            netloc = host_override
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            endpoint = {
                **endpoint,
                "url": urlunsplit(
                    (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
                ),
            }
    return endpoint


def decode_result(result, *, absent_code: str = "") -> dict | None:
    texts = [block.text for block in result.content if block.type == "text"]
    if result.isError:
        # Old Registry ToolError returns its code in text, not a structured error envelope.
        if absent_code and any(
            text == absent_code
            or text.startswith(absent_code + ":")
            or (text.startswith("Error executing tool ") and (": " + absent_code + ":") in text)
            for text in texts
        ):
            return None
        raise RuntimeError("UPSTREAM_MCP_TOOL_FAILED")
    payload = result.structuredContent
    if payload is None:
        if len(texts) != 1:
            raise ValueError("MCP_RESULT_NOT_STRUCTURED")
        payload = json.loads(texts[0])
    if not isinstance(payload, dict):
        raise ValueError("MCP_RESULT_OBJECT_REQUIRED")
    return payload


class RemoteMCPToolSession:
    """Open a short-lived MCP session for each direct semantic search operation."""

    def __init__(self, endpoint: dict):
        self.endpoint = endpoint

    @asynccontextmanager
    async def _session(self):
        async with httpx.AsyncClient(
            headers=self.endpoint.get("headers", {}),
            timeout=httpx.Timeout(120, connect=10),
            follow_redirects=False,
        ) as http:
            async with streamable_http_client(self.endpoint["url"], http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def list_tools(self):
        async with self._session() as session:
            return await session.list_tools()

    async def call_tool(self, name: str, arguments: dict):
        async with self._session() as session:
            return await session.call_tool(name, arguments)


class MCPRegistryClient(RegistryClient):
    """Use existing read-only MCP tools; retain the same rule validation as HTTP access."""

    def __init__(self, session, catalog_session=None):
        self.session = session
        self.catalog_session = catalog_session or session
        self.tool_names: dict[str, str] = {}

    async def discover(self):
        # Deterministic rules and the catalog come from the in-network Registry;
        # only semantic candidate search uses the direct Dify MCP endpoint.
        registry_tools = (await self.catalog_session.list_tools()).tools
        for tool in registry_tools:
            name = tool.name.replace("_", "-")
            required = set(tool.inputSchema.get("required", []))
            role = None
            if name.endswith("technical-spec-basic-rule-get"):
                role = "basic"
            elif (
                name.endswith("technical-spec-review-rule-get")
                and {"object_scope", "part", "section_number"} <= required
            ):
                role = "exact"
            if role:
                if role in self.tool_names:
                    raise ValueError("MCP_AMBIGUOUS_RULE_TOOL")
                self.tool_names[role] = tool.name

        catalog = [
            t.name
            for t in registry_tools
            if t.name.replace("_", "-").endswith("list-rule-packages")
        ]
        if len(catalog) != 1:
            raise ValueError("MCP_CATALOG_TOOL_MISSING_OR_AMBIGUOUS")
        self.tool_names["catalog"] = catalog[0]
        if not {"basic", "exact"} <= self.tool_names.keys():
            raise ValueError("MCP_RULE_TOOLS_MISSING")

    async def _call(self, role: str, arguments: dict, absent_code: str = ""):
        session = self.catalog_session if role in {"catalog", "basic", "exact"} else self.session
        try:
            async with asyncio.timeout(120):
                if role == "search" and role not in self.tool_names:
                    matches = [
                        tool.name
                        for tool in (await session.list_tools()).tools
                        if tool.name.replace("_", "-").endswith(
                            "technical-spec-review-rules-search"
                        )
                    ]
                    if len(matches) != 1:
                        raise ValueError("MCP_SEARCH_TOOL_MISSING_OR_AMBIGUOUS")
                    self.tool_names[role] = matches[0]
                response = await session.call_tool(self.tool_names[role], arguments)
        except Exception:
            raise RuntimeError("UPSTREAM_MCP_REQUEST_FAILED") from None
        return decode_result(response, absent_code=absent_code)

    async def list_packages(self, document_role: str = "采购技术规范书") -> list[dict]:
        payload = await self._call("catalog", {"document_role": document_role})
        packages = payload.get("packages")
        if not isinstance(packages, list) or any(
            row.get("document_role") != document_role
            or row.get("status") != "active"
            or not row.get("object_scope")
            or not row.get("package_key")
            for row in packages
        ):
            raise ValueError("MCP_CATALOG_INVALID")
        return packages

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        role, absent = (
            {
                "/v1/rules/get": ("exact", "ACTIVE_RULE_NOT_FOUND"),
                "/v1/basic-rules/get": ("basic", "ACTIVE_BASIC_RULE_NOT_FOUND"),
            }
        )[path]
        if role == "basic" and payload.get("object_scope") is None:
            payload = {**payload, "object_scope": ""}
        result = await self._call(role, payload, absent)
        return (
            httpx.Response(404, json={"detail": {"code": absent}})
            if result is None
            else httpx.Response(200, json=result)
        )

    async def semantic_search(
        self, query: str, object_scope: str, document_role: str = "采购技术规范书"
    ) -> list[dict]:
        if not query.strip() or len(query) > 250:
            raise ValueError("SEARCH_QUERY_LENGTH_INVALID")
        payload = await self._call(
            "search",
            {
                "query": query,
                "object_scope": "" if object_scope == "UNKNOWN" else object_scope,
                "document_role": document_role,
                "rule_kind": "chapter",
            },
        )
        candidates = payload.get("result")
        if not isinstance(candidates, list):
            raise ValueError("MCP_SEARCH_RESULT_INVALID")
        results = []
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("MCP_SEARCH_CANDIDATE_INVALID")
            identity: dict[str, str] = {}
            for field in ("object_scope", "document_role", "part", "section_number", "rule_kind"):
                value = candidate.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("MCP_SEARCH_IDENTITY_INCOMPLETE")
                identity[field] = value
            if identity["document_role"] != document_role or identity["rule_kind"] != "chapter":
                continue
            if object_scope != "UNKNOWN" and identity["object_scope"] != object_scope:
                continue
            rule: Rule | None = await self.get_rule(**identity)
            if rule is None:
                raise ValueError("MCP_SEARCH_STALE_RULE_IDENTITY")
            key = (rule.rule_id, rule.version)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "rule": rule.model_dump(),
                    "binding": "CANDIDATE",
                    "source": "MCP_SEARCH_THEN_REGISTRY",
                    "score": candidate.get("score"),
                }
            )
        return results


@asynccontextmanager
async def connect_registry(
    config_path: str, catalog_config_path: str = "", *, host_override: str = ""
):
    async with AsyncExitStack() as stack:

        async def connect(path):
            endpoint = await asyncio.to_thread(load_endpoint, path)
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=endpoint.get("headers", {}),
                    timeout=httpx.Timeout(120, connect=10),
                    follow_redirects=False,
                )
            )
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(endpoint["url"], http_client=http)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return session

        if catalog_config_path:
            endpoint = await asyncio.to_thread(
                load_endpoint, config_path, host_override=host_override
            )
            session = RemoteMCPToolSession(endpoint)
            catalog = await connect(catalog_config_path)
        else:
            session = await connect(config_path)
            catalog = session
        registry = MCPRegistryClient(session, catalog)
        await registry.discover()
        yield registry
