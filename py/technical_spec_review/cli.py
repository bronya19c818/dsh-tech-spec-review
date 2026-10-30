from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .config import Config, runtime
from .local_stack import DATA_DIR, load_keys_env, read_json
from .local_stack import apply_settings as apply_values
from .state import safe_error

load_keys_env()


def apply_settings() -> None:
    """未显式指定时沿用本地栈的数据目录，并应用界面保存的 settings.json。

    CLI 与本地栈必须用同一套配置，否则模型/规则地址会对不上。
    """
    os.environ.setdefault("TSR_DOCUMENT_ROOT", str(DATA_DIR / "workspace"))
    os.environ.setdefault("TSR_RUNS_DIR", str(DATA_DIR / "runs"))
    stored = read_json(DATA_DIR / "settings.json")
    if isinstance(stored, dict):
        apply_values(stored)


async def execute(args) -> int:
    async with runtime(
        Config.from_env(), connect_rules=args.command in {"run", "retry"}
    ) as service:
        if args.command == "run":
            rid = await service.start(args.source, args.object_scope, args.document_role)
            print(json.dumps({"review_id": rid}, ensure_ascii=False), flush=True)
            await service.wait(rid)
            result = await service.status(rid)
        elif args.command == "retry":
            rid = await service.retry(args.review_id)
            await service.wait(rid)
            result = await service.status(rid)
        elif args.command == "results":
            result = await service.results(args.review_id)
        else:
            result = await service.status(args.review_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        status = result.get("status", result.get("state", {}).get("status"))
        return 1 if status == "FAILED" else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="技术规范书审查（客户端）")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="启动审查并等待完成")
    run.add_argument("source", help="DOCX 路径：绝对路径，或相对 TSR_DOCUMENT_ROOT")
    run.add_argument("--object-scope")
    run.add_argument("--document-role", default="采购技术规范书")
    for command in ("status", "results", "retry"):
        sub.add_parser(command).add_argument("review_id")
    serve = sub.add_parser("serve", help="运行审查 MCP 服务")
    serve.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    sub.add_parser("stack", help="启动本地 MCP 服务（streamable-http）")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from .mcp_server import create_server

            create_server().run(transport=args.transport)
        elif args.command == "stack":
            from .local_stack import main as stack_main

            stack_main()
        else:
            apply_settings()
            raise SystemExit(asyncio.run(execute(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(safe_error(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
