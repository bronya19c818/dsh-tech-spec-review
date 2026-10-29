from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .checks import normalize_title
from .config import Config, runtime
from .ingress import UploadedDocument, UploadStore
from .state import safe_error

_MULTIPART_ENVELOPE_LIMIT = 1024 * 1024


def _decode_multipart_filename(value: bytes | None) -> str:
    if value is None:
        raise ValueError("UPLOAD_FILENAME_INVALID")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("UPLOAD_FILENAME_INVALID") from None


def _secure_upload_request(request: Request, *, trust_proxy: bool = False) -> bool:
    """Accept HTTPS, trusted proxy HTTPS, or an explicitly local dev request."""
    if request.url.scheme.lower() == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    if trust_proxy and forwarded.split(",", 1)[0].strip().lower() == "https":
        return True
    return request.url.hostname in {"127.0.0.1", "localhost", "::1", "testserver"}


async def _receive_upload(request: Request, upload_store: UploadStore) -> UploadedDocument:
    """Parse multipart bytes while writing only the attachment to the bounded store."""
    content_type = request.headers.get("content-type", "").encode("latin-1")
    media_type, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary:
        raise ValueError("UPLOAD_WRITE_FAILED")

    state: dict[str, Any] = {
        "headers": {},
        "part": None,
        "field": bytearray(),
        "file_seen": False,
        "filename_value": None,
        "filename_supplied": False,
        "writer": None,
        "ended": False,
    }

    def on_part_begin() -> None:
        state["headers"] = {}
        state["part"] = None
        state["field"] = bytearray()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state.setdefault("header_field", bytearray())
        state["header_field"].extend(data[start:end])

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state.setdefault("header_value", bytearray())
        state["header_value"].extend(data[start:end])

    def on_header_end() -> None:
        headers = state["headers"]
        assert isinstance(headers, dict)
        header_field = bytes(state.pop("header_field", bytearray())).lower()
        header_value = bytes(state.pop("header_value", bytearray()))
        headers[header_field] = header_value

    def on_headers_finished() -> None:
        headers = state["headers"]
        assert isinstance(headers, dict)
        disposition = headers.get(b"content-disposition")
        _, options = parse_options_header(disposition)
        name = options.get(b"name")
        part_filename = options.get(b"filename")
        if name == b"attachment":
            if state["file_seen"]:
                raise ValueError("UPLOAD_FILENAME_INVALID")
            state["file_seen"] = True
            state["part"] = "attachment"
            supplied = state.get("filename_value")
            if supplied is None and part_filename is not None:
                supplied = _decode_multipart_filename(part_filename)
                state["filename_supplied"] = True
            # The independent filename field may appear after the attachment
            # part. Use a harmless placeholder until that field is parsed.
            state["writer"] = upload_store.begin_upload(supplied or "upload.docx")
        elif name == b"filename" and part_filename is None:
            state["part"] = "filename"
        else:
            state["part"] = "ignored"

    def on_part_data(data: bytes, start: int, end: int) -> None:
        part = state["part"]
        if part == "attachment":
            writer = state["writer"]
            assert writer is not None
            writer.write(data[start:end])
        elif part == "filename":
            field = state["field"]
            assert isinstance(field, bytearray)
            if len(field) + (end - start) > 4096:
                raise ValueError("UPLOAD_FILENAME_INVALID")
            field.extend(data[start:end])

    def on_part_end() -> None:
        if state["part"] != "filename":
            return
        field = state["field"]
        assert isinstance(field, bytearray)
        value = _decode_multipart_filename(bytes(field).strip())
        state["filename_value"] = value
        state["filename_supplied"] = True
        writer = state["writer"]
        if writer is not None:
            writer.filename = upload_store.validate_filename(value)

    def on_end() -> None:
        state["ended"] = True

    parser = MultipartParser(
        boundary,
        callbacks={
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        },
        max_size=upload_store.max_bytes + _MULTIPART_ENVELOPE_LIMIT,
        max_header_count=8,
        max_header_size=4096,
    )
    writer = None
    try:
        async for chunk in request.stream():
            if not isinstance(chunk, bytes):
                raise ValueError("UPLOAD_WRITE_FAILED")
            if parser.write(chunk) != len(chunk):
                raise ValueError("UPLOAD_TOO_LARGE")
        parser.finalize()
        writer = state["writer"]
        if not state["ended"] or writer is None or not state["filename_supplied"]:
            if writer is not None and not state["filename_supplied"]:
                raise ValueError("UPLOAD_FILENAME_INVALID")
            raise ValueError("UPLOAD_WRITE_FAILED")
        return writer.finish()
    except BaseException:
        writer = state.get("writer")
        if writer is not None:
            writer.abort()
        raise


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
    upload_store = UploadStore(
        settings.document_root,
        max_bytes=settings.upload_max_bytes,
        ttl_seconds=settings.upload_ttl_seconds,
        runs_dir=settings.runs_dir,
    )
    process_runtime: dict[str, Any] = {"service": None}

    @asynccontextmanager
    async def open_runtime():
        async with runtime(settings, upload_store=upload_store) as service:
            cleanup = getattr(service, "cleanup_uploads", None)
            if cleanup is not None:
                await cleanup()
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
    )

    # FastMCP's MCP lifespan runs once per Streamable HTTP session. Wrap the
    # Starlette application lifespan so the runtime and its HTTP clients live
    # for the server process, outlasting individual client sessions.
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

    @server.custom_route("/upload_review_document", methods=["POST"])
    async def upload_review_document(request: Request) -> Response:
        """Receive one DOCX through the SI-SYNC service-to-service adapter."""
        if not settings.upload_api_token:
            return JSONResponse({"error": "UPLOAD_AUTH_NOT_CONFIGURED"}, status_code=503)
        authorization = request.headers.get("authorization")
        if not authorization:
            return JSONResponse({"error": "UPLOAD_AUTH_REQUIRED"}, status_code=401)
        scheme, _, supplied = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not supplied
            or not secrets.compare_digest(supplied, settings.upload_api_token)
        ):
            return JSONResponse({"error": "UPLOAD_AUTH_INVALID"}, status_code=401)
        if not _secure_upload_request(request, trust_proxy=settings.upload_trust_proxy):
            return JSONResponse({"error": "UPLOAD_HTTPS_REQUIRED"}, status_code=400)
        try:
            await asyncio.to_thread(upload_store.cleanup_expired)
            content_length = request.headers.get("content-length")
            parsed_length = int(content_length) if content_length else None
            if parsed_length is not None and parsed_length < 0:
                parsed_length = None
            # Content-Length covers the multipart envelope as well as the DOCX
            # bytes. Allow a bounded envelope overhead here and let the upload
            # store enforce the exact attachment limit while streaming.
            if (
                parsed_length is not None
                and parsed_length > settings.upload_max_bytes + 1024 * 1024
            ):
                return JSONResponse({"error": "UPLOAD_TOO_LARGE"}, status_code=413)
            uploaded = await _receive_upload(request, upload_store)
            return JSONResponse(uploaded.to_public(), status_code=201)
        except ValueError as exc:
            error = safe_error(exc)
            status = (
                413 if error == "UPLOAD_TOO_LARGE" else 415 if error == "UPLOAD_NOT_DOCX" else 400
            )
            if error == "UPLOAD_WRITE_FAILED":
                status = 500
            return JSONResponse({"error": error}, status_code=status)
        except Exception:
            return JSONResponse({"error": "UPLOAD_WRITE_FAILED"}, status_code=500)

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
        file_id: str,
        object_scope: str | None = None,
        document_role: str = "采购技术规范书",
    ) -> dict:
        """使用已上传文件启动 DOCX 审查。"""
        rid = await ctx.request_context.lifespan_context.start(
            file_id,
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
