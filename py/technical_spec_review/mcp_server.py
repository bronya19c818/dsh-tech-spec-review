from __future__ import annotations

from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .checks import normalize_title
from .config import Config, runtime
from .state import safe_error


def safe_tool(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except Exception as exc:
            raise ToolError(safe_error(exc)) from None

    return wrapped


def create_server(config: Config | None = None) -> FastMCP:
    settings = config or Config.from_env()
    process_runtime: dict[str, Any] = {"service": None}

    @asynccontextmanager
    async def open_runtime():
        async with runtime(settings) as service:
            yield service

    @asynccontextmanager
    async def lifespan(server):
        # Streamable HTTP sessions are short lived, while a review may run for
        # minutes. Borrow the process-owned service so closing an MCP session
        # cannot cancel review workers. Stdio has no outer ASGI lifespan and
        # continues to own its runtime through this per-session context.
        service = process_runtime["service"]
        if service is not None:
            yield service
            return
        async with open_runtime() as service:
            yield service

    server = FastMCP(
        "technical-spec-review",
        lifespan=lifespan,
        host=settings.mcp_host,
        port=settings.mcp_port,
        # 无状态：每次请求独立处理，不保存会话。原来的会话状态让服务重启后
        # 客户端攥着旧 session id 一直收到 "Session not found"，而 HTTP 建连之后
        # 的请求级失败不归客户端的重连监督器管，只能重启整个 Host。这里的工具
        # 都是请求/应答式，不需要会话，因此直接不要会话。
        stateless_http=True,
        json_response=True,
    )

    # 无状态模式下每个请求都会起一份新的传输，MCP 的 lifespan 因此按请求执行。
    # 包一层 Starlette 应用 lifespan，让运行时和它的 HTTP 客户端活满整个进程。
    streamable_http_app = server.streamable_http_app

    def process_scoped_streamable_http_app():
        app = streamable_http_app()
        session_manager_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def app_lifespan(app_instance):
            async with open_runtime() as service:
                process_runtime["service"] = service
                try:
                    async with session_manager_lifespan(app_instance):
                        yield
                finally:
                    process_runtime["service"] = None

        app.router.lifespan_context = app_lifespan
        return app

    setattr(server, "streamable_http_app", process_scoped_streamable_http_app)

    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    execution = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )

    @server.tool(annotations=readonly)
    @safe_tool
    async def list_rule_packages(ctx: Context, document_role: str = "采购技术规范书") -> list[dict]:
        """查询已建设的物资规则目录，仅返回元数据。"""
        return await ctx.request_context.lifespan_context.registry.list_packages(document_role)

    @server.tool(annotations=readonly)
    @safe_tool
    async def get_structure_rule(
        object_scope: str, ctx: Context, document_role: str = "采购技术规范书"
    ) -> dict:
        """只读取 active 全文结构规则及确定性章节树；无规则或解析失败时明确返回状态。"""
        return await ctx.request_context.lifespan_context.registry.get_structure_rule(
            object_scope, document_role
        )

    @server.tool(annotations=readonly)
    @safe_tool
    async def match_structured_chapter(
        object_scope: str,
        part: str,
        section_number: str,
        section_title: str,
        ctx: Context,
        document_role: str = "采购技术规范书",
    ) -> dict:
        """按物资、部分、章节号取完整 active 规则，标题不符时转语义路径。"""
        rule = await ctx.request_context.lifespan_context.registry.get_rule(
            object_scope, part, section_number, document_role=document_role
        )
        matched = rule is not None and normalize_title(rule.section_title) == normalize_title(
            section_title
        )
        return {
            "route": "STRUCTURED" if matched else "SEMANTIC",
            "binding": "DETERMINISTIC" if matched else "CANDIDATE",
            "rule": rule.model_dump() if matched and rule else None,
        }

    @server.tool(annotations=readonly)
    @safe_tool
    async def semantic_search_rules(
        query: str, object_scope: str, ctx: Context, document_role: str = "采购技术规范书"
    ) -> list[dict]:
        """在规则服务的语义索引里检索候选，再取完整规则；不自动认定适用。"""
        return await ctx.request_context.lifespan_context.registry.semantic_search(
            query, object_scope, document_role
        )

    @server.tool(annotations=execution)
    @safe_tool
    async def start_review(
        ctx: Context,
        document_path: str,
        object_scope: str | None = None,
        document_role: str = "采购技术规范书",
    ) -> dict:
        """按本机 DOCX 路径启动审查，返回 review_id。

        `document_path` 给绝对路径时直接读取；给相对路径时按 TSR_DOCUMENT_ROOT
        解析，且不允许用 .. 逃出该目录。文件只在本机读取并快照进该次审查目录，
        不上传。
        """
        rid = await ctx.request_context.lifespan_context.start(
            document_path,
            object_scope,
            document_role,
        )
        return {"review_id": rid}

    @server.tool(annotations=readonly)
    @safe_tool
    async def get_review_status(review_id: str, ctx: Context) -> dict:
        """查询任务状态、心跳、各子任务进度和错误；中断任务标为失败。"""
        return await ctx.request_context.lifespan_context.status(review_id)

    @server.tool(annotations=readonly)
    @safe_tool
    async def get_review_results(review_id: str, ctx: Context) -> dict:
        """读取已完成的过程结果及最终报告路径；失败任务不会产生最终报告。"""
        return await ctx.request_context.lifespan_context.results(review_id)

    @server.tool(annotations=execution)
    @safe_tool
    async def retry_review(review_id: str, ctx: Context) -> dict:
        """显式重试失败任务，复用完成项；源文件变化时必须新建审查。可能产生模型费用。"""
        return {"review_id": await ctx.request_context.lifespan_context.retry(review_id)}

    return server
