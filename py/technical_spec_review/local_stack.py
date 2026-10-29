"""客户端本地服务：只跑审查 MCP 服务。

规则库、向量索引与嵌入模型都在规则服务端（见同仓库 rule-server/），
客户端这里只做：读本地文档 → 调规则服务取规则 → 调对话模型做判断 → 交付结果。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 插件包根
DATA_DIR = Path(os.getenv("TSR_DATA_DIR", str(ROOT / "data")))
MCP_HOST = os.getenv("TSR_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("TSR_MCP_PORT", "8775"))

# 设置文件里这些键映射到子进程环境变量；与 Host 侧 SETTINGS_ENV 对应。
SETTINGS_ENV = {
    "chatBaseUrl": "TSR_MODEL_BASE_URL",
    "chatModel": "TSR_MODEL",
    "rerankBaseUrl": "TSR_RERANK_BASE_URL",
    "rerankModel": "TSR_RERANK_MODEL",
    "rerankThreshold": "TSR_RERANK_THRESHOLD",
    "rulesBaseUrl": "TSR_REGISTRY_URL",
    # B02 前置筛句（决策模型）；密钥由 Host 读凭据库后注入 TSR_GATE_API_KEY。
    "gateBaseUrl": "TSR_GATE_BASE_URL",
    "gateThreshold": "TSR_GATE_THRESHOLD",
}


def load_keys_env() -> None:
    """开发期便利：读插件目录下的 keys.env（可选，正式安装时不存在）。"""
    keys_file = Path(os.getenv("TSR_KEYS_FILE", str(ROOT / "keys.env")))
    if not keys_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(keys_file, override=False, encoding="utf-8")
    print(f"[local] loaded {keys_file}", flush=True)


def apply_settings(settings: dict) -> None:
    for key, name in SETTINGS_ENV.items():
        value = settings.get(key)
        if value is None or value == "":
            continue
        os.environ[name] = str(value)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_stack_info() -> None:
    info = {
        "mcp_url": f"http://{MCP_HOST}:{MCP_PORT}/mcp",
        "data_dir": str(DATA_DIR),
    }
    (DATA_DIR / "stack.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def report() -> None:
    from .config import Config

    cfg = Config.from_env()
    print(f"[local] chat model : {cfg.model_name or 'MISSING'} @ {cfg.model_url or 'MISSING'}", flush=True)
    print(f"[local] rule server: {cfg.registry_url or 'MISSING'}", flush=True)
    print(
        f"[local] gate       : {cfg.gate_url or '未配置（B02 全量送对话模型）'} "
        f"threshold={cfg.gate_threshold}",
        flush=True,
    )
    print(
        f"[local] rerank     : {cfg.rerank_model or '未配置'} @ {cfg.rerank_url or '-'} "
        f"top_k={cfg.rerank_top_k} threshold={cfg.rerank_threshold}",
        flush=True,
    )


def main() -> None:
    load_keys_env()
    stored = read_json(DATA_DIR / "settings.json")
    if isinstance(stored, dict):
        apply_settings(stored)
        print(f"[local] settings.json applied: {', '.join(sorted(stored))}", flush=True)

    os.environ.setdefault("TSR_DOCUMENT_ROOT", str(DATA_DIR / "workspace"))
    os.environ.setdefault("TSR_RUNS_DIR", str(DATA_DIR / "runs"))
    os.environ.setdefault("TSR_MCP_HOST", MCP_HOST)
    os.environ.setdefault("TSR_MCP_PORT", str(MCP_PORT))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_stack_info()
    report()

    from .mcp_server import create_server

    create_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
