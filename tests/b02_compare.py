"""单独跑 B02，对比"有筛选"与"无筛选"的实际开销。

不跑整份审查，只跑逐句文字质量这一层，便于看清筛掉了多少、省了几次调用。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT / "tests"))

import importlib.util  # noqa: E402 - 必须先插入 sys.path 再导入被测包

spec = importlib.util.spec_from_file_location("e2e", ROOT / "tests" / "e2e_review.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)
e2e.load_env()

import httpx  # noqa: E402
from technical_spec_review.config import Config  # noqa: E402
from technical_spec_review.gate import ClefGate  # noqa: E402
from technical_spec_review.language import review_language  # noqa: E402
from technical_spec_review.model import OpenAIModel  # noqa: E402
from technical_spec_review.parser import parse_docx  # noqa: E402


async def main() -> int:
    cfg = Config.from_env()
    docx = Path(os.getenv("TSR_DOCUMENT_ROOT") or (Path.home() / ".dsh" / "technical-spec-review" / "workspace"))
    target = next(docx.glob("*.docx"))
    print(f"样本：{target.name}")

    out = Path(ROOT / "artifacts" / "b02-compare")
    out.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), trust_env=False) as http:
        model = OpenAIModel(
            http, cfg.model_url, cfg.model_key, cfg.model_name,
            concurrency=max(cfg.concurrency, cfg.b02_concurrency),
        )
        gate = None
        if cfg.gate_url:
            gate = ClefGate(http, cfg.gate_url, cfg.gate_key, threshold=cfg.gate_threshold)
            print(f"筛选模型：clef  阈值 {cfg.gate_threshold}")

        document = parse_docx(target)

        for label, use_gate in (("无筛选（纯 LLM）", None), ("有筛选（Clef）", gate)):
            directory = out / ("gate" if use_gate else "nogate")
            started = time.time()
            result = await review_language(
                document, "净水器", [], model, directory,
                workers=cfg.b02_concurrency, batch_size=cfg.b02_batch,
                gate=use_gate, gate_threshold=cfg.gate_threshold,
            )
            elapsed = time.time() - started
            stats = result.stats
            print(f"\n{label}  耗时 {elapsed:.1f}s")
            print(f"  总句数 {stats['total']}  送 LLM {stats['llm_checked']}  "
                  f"筛掉 {stats['gate_passed']}  批数 {stats['batch_count']}")
            print(f"  问题数 {stats['issues']}  缺口 {result.gaps}")
            (directory / "stats.json").write_text(
                __import__("json").dumps({"stats": stats, "seconds": round(elapsed, 1), "gaps": result.gaps},
                                         ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
