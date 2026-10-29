from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from .artifacts import read_json, write_json
from .models import DocumentEvidence, Finding, Rule, TaskResult

NON_PROSE_ROLES = {
    "heading",
    "table_caption",
    "figure_caption",
    "toc",
    "front_matter",
    "other_special",
}
TABLE_CELL_ID = re.compile(r"(?P<table>t\d{3})r(?P<row>\d{3})c(?P<column>\d{3})p\d{3}$")


def split_sentences(text: str) -> list[str]:
    result = []
    start = 0
    for match in re.finditer(r"[。！？!?\n]|\.(?=\s+[A-Z\u4e00-\u9fff]|$)", text):
        if match[0] == ".":
            prefix = text[start : match.start()]
            token = prefix.split()[-1] if prefix.split() else ""
            if token.lower() in {"dr", "mr", "mrs", "ms", "prof", "e.g", "i.e", "etc"}:
                continue
            if re.fullmatch(r"(?:[A-Z]\.)+[A-Z]|\d+", token):
                continue
        piece = text[start : match.end()].strip()
        if piece:
            result.append(piece)
        start = match.end()
    if text[start:].strip():
        result.append(text[start:].strip())
    return result


def _table_cell_coordinates(block_id: str) -> tuple[str, int, int] | None:
    match = TABLE_CELL_ID.search(block_id)
    if match is None:
        return None
    table_id = block_id[: match.start("table")] + match["table"]
    return table_id, int(match["row"]), int(match["column"])


def _unit_column_value_ids(document: DocumentEvidence) -> set[str]:
    table_cells = [
        (block, _table_cell_coordinates(block.id))
        for block in document.blocks
        if block.role == "table_content"
    ]
    first_rows: dict[str, int] = {}
    for _, coordinates in table_cells:
        if coordinates is None:
            continue
        table_id, row, _ = coordinates
        first_rows[table_id] = min(first_rows.get(table_id, row), row)

    unit_columns = set()
    for block, coordinates in table_cells:
        if coordinates is None:
            continue
        table_id, row, column = coordinates
        header = re.sub(r"\s+", "", block.text)
        if row == first_rows[table_id] and "单位" in header:
            unit_columns.add((table_id, column))

    return {
        block.id
        for block, coordinates in table_cells
        if coordinates is not None
        and coordinates[1] > first_rows[coordinates[0]]
        and (coordinates[0], coordinates[2]) in unit_columns
    }


def sentences(document: DocumentEvidence) -> list[dict]:
    units: list[dict] = []
    unit_column_values = _unit_column_value_ids(document)
    for block in document.blocks:
        if block.role in NON_PROSE_ROLES or block.id in unit_column_values:
            continue
        # Decimal values, models and standard identifiers must not be split at periods.
        for text in split_sentences(block.text):
            if text.strip():
                units.append(
                    {
                        "id": f"S{len(units) + 1:06d}",
                        "evidence_id": block.id,
                        "text": text.strip(),
                        "source_type": (
                            "表格单元格" if block.role == "table_content" else "正文段落"
                        ),
                    }
                )
    return units


def validate_sentence(answer: dict, text: str) -> None:
    if type(answer.get("has_issue")) is not bool:
        raise ValueError("SENTENCE_BOOLEAN_REQUIRED")
    for key in ("type", "reason", "quote", "suggestion"):
        if not isinstance(answer.get(key), str):
            raise ValueError("SENTENCE_FIELDS_REQUIRED")
    if answer["has_issue"] and (
        not answer["reason"].strip()
        or not answer["quote"].strip()
        or answer["quote"] not in text
        or not answer["type"].strip()
    ):
        raise ValueError("SENTENCE_EVIDENCE_INVALID")


async def review_language(
    document: DocumentEvidence,
    object_scope: str,
    rules: list[Rule],
    model,
    directory: Path,
    *,
    workers: int = 4,
    batch_size: int = 8,
    progress=None,
) -> TaskResult:
    """逐句文字质量审查。

    句子按 batch_size 打包成一次模型调用（默认 8 句），把请求数从"一句一次"
    降到"一批一次"；某批输出不合法时退回逐句重问该批，保证结论不因批处理丢失。
    """
    if workers < 1:
        raise ValueError("WORKERS_MUST_BE_POSITIVE")
    if batch_size < 1:
        raise ValueError("BATCH_SIZE_MUST_BE_POSITIVE")
    rule_data = [rule.verify().model_dump() for rule in rules]
    units = sentences(document)
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "source": document.sha256,
                "rules": rule_data,
                "object_scope": object_scope,
                "sentence_policy": 4,
                "batch_size": batch_size,
                "units": units,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cache = directory / "sentences" / cache_key
    batches = [units[i : i + batch_size] for i in range(0, len(units), batch_size)]
    queue: asyncio.Queue = asyncio.Queue()
    for batch in batches:
        queue.put_nowait(batch)
    answers: dict[str, dict] = {}
    failures: list[dict] = []
    completed = 0
    progress_lock = asyncio.Lock()

    async def ask_one(unit: dict) -> dict:
        path = cache / (unit["id"] + ".json")
        if await asyncio.to_thread(path.exists):
            answer = await read_json(path)
        else:
            answer = await model.ask(
                "sentence", {**unit, "object_scope": object_scope, "rules": rule_data}
            )
            validate_sentence(answer, unit["text"])
            await write_json(path, answer)
        validate_sentence(answer, unit["text"])
        return answer

    async def ask_batch(batch: list[dict]) -> dict[str, dict]:
        path = cache / (batch[0]["id"] + ".batch.json")
        if await asyncio.to_thread(path.exists):
            payload = await read_json(path)
        else:
            payload = await model.ask(
                "sentence_batch",
                {
                    "items": [
                        {"id": unit["id"], "text": unit["text"], "source_type": unit["source_type"]}
                        for unit in batch
                    ],
                    "object_scope": object_scope,
                    "rules": rule_data,
                },
            )
            await write_json(path, payload)
        decisions = payload.get("decisions") if isinstance(payload, dict) else None
        if not isinstance(decisions, list):
            raise ValueError("BATCH_DECISIONS_REQUIRED")
        by_id: dict[str, dict] = {}
        for decision in decisions:
            if isinstance(decision, dict) and isinstance(decision.get("id"), str):
                by_id.setdefault(decision["id"], decision)
        resolved: dict[str, dict] = {}
        for unit in batch:
            decision = by_id.get(unit["id"])
            if decision is None:
                raise ValueError("BATCH_COVERAGE_INVALID")
            answer = {
                key: decision.get(key, "")
                for key in ("has_issue", "type", "reason", "suggestion", "quote")
            }
            validate_sentence(answer, unit["text"])
            resolved[unit["id"]] = answer
        return resolved

    async def worker():
        nonlocal completed
        while not queue.empty():
            batch = queue.get_nowait()
            try:
                answers.update(await ask_batch(batch))
            except Exception as batch_error:  # noqa: BLE001 - 整批不可用就退回逐句
                # 限流/传输类错误说明服务端压力已高，逐句重问只会火上浇油：
                # 直接记失败，让上层按可重试错误处理。
                if isinstance(batch_error, RuntimeError) and str(batch_error).startswith(
                    ("MODEL_HTTP_429", "MODEL_HTTP_5", "MODEL_TRANSPORT_ERROR", "MODEL_RETRY_EXHAUSTED")
                ):
                    for unit in batch:
                        failures.append({"id": unit["id"], "error_type": str(batch_error)})
                else:
                    # 输出不合规（漏 id、字段非法）才值得逐句重问，宁可多花几次调用也不丢句子。
                    for unit in batch:
                        try:
                            answers[unit["id"]] = await ask_one(unit)
                        except Exception as exc:  # noqa: BLE001 - 单句失败要记下来而不是中断
                            failures.append({"id": unit["id"], "error_type": type(exc).__name__})
            finally:
                async with progress_lock:
                    completed += len(batch)
                    if progress:
                        await progress(completed, len(units))
                queue.task_done()

    await asyncio.gather(*(worker() for _ in range(min(workers, max(1, len(batches))))))
    await write_json(directory / "sentence-failures.json", failures)
    if failures:
        raise RuntimeError(f"SENTENCE_FAILURES:{len(failures)}")
    result = TaskResult(
        task_id="B02", object_scope=object_scope, scope=[u["id"] for u in units], rules=rule_data
    )
    result.stats = {
        "total": len(units),
        "completed": len(answers),
        "failed": 0,
        "issues": sum(1 for answer in answers.values() if answer["has_issue"]),
        "batch_size": batch_size,
        "batch_count": len(batches),
    }
    if not rules:
        result.gaps.append("未取得 active 文字规则，本轮仅按通用文字质量提示执行。")
    for unit in units:
        answer = answers[unit["id"]]
        if answer["has_issue"]:
            result.findings.append(
                Finding(
                    code="LANGUAGE_QUALITY",
                    detail=answer["reason"],
                    evidence_ids=[unit["evidence_id"]],
                    target=unit["id"],
                    suggestion=answer["suggestion"],
                    disposition="candidate",
                )
            )
    result.decisions = [{**unit, **answers[unit["id"]]} for unit in units]
    return result.assign_ids()
