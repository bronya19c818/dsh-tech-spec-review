from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from .engine import ReviewService
from .model import OpenAIModel
from .rules import RegistryClient


def _resolve_rerank_url(env: dict) -> str:
    """重排地址：显式 URL 优先，其次 base（自动补 /rerank），最后复用对话服务的 base。

    多数托管服务把 rerank 挂在同一 base 下的 /rerank（SiliconFlow、Cohere、
    Jina、DashScope 兼容模式都是这样）。Cloudflare 那种 /ai/run/<model> 形态
    请直接填 TSR_RERANK_URL —— 嵌入已移到服务端，客户端不再从它推导。
    """
    explicit = env.get("TSR_RERANK_URL", "")
    if explicit:
        return explicit
    base = env.get("TSR_RERANK_BASE_URL", "") or env.get("TSR_MODEL_BASE_URL", "")
    if not base:
        return ""
    base = base.rstrip("/")
    if base.endswith("/rerank"):
        return base
    return base + "/rerank"


@dataclass(frozen=True)
class Config:
    runs_dir: Path
    document_root: Path
    model_url: str = ""
    model_name: str = ""
    model_key: str = ""
    registry_url: str = ""
    registry_key: str = ""
    dify_url: str = ""
    dify_dataset: str = ""
    dify_key: str = ""
    # 语义检索由规则服务端提供；这里不保存嵌入模型与数据库连接串。
    # 交叉编码重排（可选）：向量多召回后重排取 top_k
    rerank_url: str = ""
    rerank_model: str = ""
    rerank_key: str = ""
    # B02 前置筛句（决策模型，可选）：打分低于阈值即放行，不交对话模型
    gate_url: str = ""
    gate_key: str = ""
    gate_threshold: float = 0.5
    rerank_format: str = "auto"  # auto | cf | openai
    rerank_recall: int = 24
    rerank_top_k: int = 8
    # 默认 0：与原 Dify 检索配置一致（score_threshold_enabled=false）。
    # 阈值会把"整批低分"变成零候选，而零候选在审查里是合法的"无规则"，
    # 等于静默降质；只作为显式收紧召回质量的开关使用。
    rerank_threshold: float = 0.0
    concurrency: int = 4
    b02_concurrency: int = 8
    b02_batch: int = 8
    rules_backend: str = "http"
    mcp_config: str = ""
    catalog_mcp_config: str = ""
    mcp_endpoint_host_override: str = ""
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8775

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            rules_backend=os.getenv("TSR_RULES_BACKEND", "http"),
            mcp_config=os.getenv("TSR_MCP_CONFIG", ""),
            catalog_mcp_config=os.getenv("TSR_CATALOG_MCP_CONFIG", ""),
            mcp_endpoint_host_override=os.getenv("TSR_MCP_ENDPOINT_HOST_OVERRIDE", ""),
            mcp_host=os.getenv("TSR_MCP_HOST", "127.0.0.1"),
            mcp_port=int(os.getenv("TSR_MCP_PORT", "8775")),
            runs_dir=Path(os.getenv("TSR_RUNS_DIR", "runs")),
            document_root=Path(os.getenv("TSR_DOCUMENT_ROOT", ".")),
            model_url=os.getenv("TSR_MODEL_BASE_URL", ""),
            model_name=os.getenv("TSR_MODEL", ""),
            model_key=os.getenv("TSR_MODEL_API_KEY", ""),
            registry_url=os.getenv("TSR_REGISTRY_URL", ""),
            registry_key=os.getenv("TSR_REGISTRY_API_KEY", ""),
            dify_url=os.getenv("TSR_DIFY_BASE_URL", ""),
            dify_dataset=os.getenv("TSR_DIFY_DATASET_ID", ""),
            dify_key=os.getenv("TSR_DIFY_API_KEY", ""),
            rerank_url=_resolve_rerank_url(dict(env)),
            rerank_model=env.get("TSR_RERANK_MODEL", ""),
            rerank_key=env.get("TSR_RERANK_API_KEY", ""),
            rerank_format=env.get("TSR_RERANK_FORMAT", "auto"),
            rerank_recall=int(env.get("TSR_RERANK_RECALL", "24")),
            rerank_top_k=int(env.get("TSR_RERANK_TOP_K", "8")),
            rerank_threshold=float(env.get("TSR_RERANK_THRESHOLD", "0")),
            gate_url=env.get("TSR_GATE_BASE_URL", ""),
            gate_key=env.get("TSR_GATE_API_KEY", ""),
            gate_threshold=float(env.get("TSR_GATE_THRESHOLD", "0.5")),
            concurrency=int(os.getenv("TSR_CONCURRENCY", "4")),
            b02_concurrency=int(os.getenv("TSR_B02_CONCURRENCY", "8")),
            b02_batch=int(os.getenv("TSR_B02_BATCH", "8")),
        )


@asynccontextmanager
async def runtime(
    config: Config,
    *,
    connect_rules: bool = True,
):
    if config.rules_backend not in {"http", "mcp"}:
        raise ValueError("RULES_BACKEND_INVALID")
    async with AsyncExitStack() as stack:
        async with _runtime(config, stack, connect_rules=connect_rules) as service:
            yield service


@asynccontextmanager
async def _runtime(
    config: Config,
    stack,
    *,
    connect_rules: bool,
):
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120, connect=10),
        follow_redirects=False,
        # 本机 registry / 模型服务均走回环，必须绕开系统代理。
        trust_env=False,
    ) as http:
        # 语义检索由规则服务端提供（嵌入模型与向量索引都在服务端）；
        # 重排是客户端侧的可选增益，只用对话类 HTTP 调用，不碰索引。
        reranker = None
        if config.rerank_url:
            from .rerank import RerankClient

            reranker = RerankClient(
                http,
                config.rerank_url,
                config.rerank_key,
                config.rerank_model,
                fmt=config.rerank_format,
            )

        # B02 前置筛句的决策模型：未配置则不筛，全部句子交对话模型。
        gate = None
        if config.gate_url:
            from .gate import ClefGate, GateError

            try:
                gate = ClefGate(
                    http,
                    config.gate_url,
                    config.gate_key,
                    threshold=config.gate_threshold,
                )
            except GateError as exc:
                print(f"[config] 筛选模型不可用，本轮不筛句：{exc}", flush=True)

        registry = RegistryClient(
            http,
            config.registry_url,
            registry_key=config.registry_key,
            dify_url=config.dify_url,
            dataset_id=config.dify_dataset,
            dify_key=config.dify_key,
            reranker=reranker,
            rerank_recall=config.rerank_recall,
            rerank_top_k=config.rerank_top_k,
            rerank_threshold=config.rerank_threshold,
            semantic_via_server=bool(config.registry_url),
        )
        if config.rules_backend == "mcp" and connect_rules:
            from .mcp_registry import connect_registry

            if not config.mcp_config:
                raise ValueError("MCP_CONFIG_REQUIRED")
            registry = await stack.enter_async_context(
                connect_registry(
                    config.mcp_config,
                    config.catalog_mcp_config,
                    host_override=config.mcp_endpoint_host_override,
                )
            )
        model = OpenAIModel(
            http,
            config.model_url,
            config.model_key,
            config.model_name,
            concurrency=max(config.concurrency, config.b02_concurrency),
        )
        service = ReviewService(
            config.runs_dir,
            config.document_root,
            registry,
            model,
            concurrency=config.concurrency,
            b02_concurrency=config.b02_concurrency,
            b02_batch=config.b02_batch,
            gate=gate,
            gate_threshold=config.gate_threshold,
        )
        try:
            yield service
        finally:
            await service.close()
