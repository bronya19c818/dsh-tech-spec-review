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
    progress=None,
) -> TaskResult:
    if workers < 1:
        raise ValueError("WORKERS_MUST_BE_POSITIVE")
    rule_data = [rule.verify().model_dump() for rule in rules]
    units = sentences(document)
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "source": document.sha256,
                "rules": rule_data,
                "object_scope": object_scope,
                "sentence_policy": 3,
                "units": units,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cache = directory / "sentences" / cache_key
    queue: asyncio.Queue = asyncio.Queue()
    for unit in units:
        queue.put_nowait(unit)
    answers = {}
    failures = []
    completed = 0
    progress_lock = asyncio.Lock()

    async def worker():
        nonlocal completed
        while not queue.empty():
            unit = queue.get_nowait()
            try:
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
                answers[unit["id"]] = answer
            except Exception as exc:
                failures.append({"id": unit["id"], "error_type": type(exc).__name__})
            finally:
                async with progress_lock:
                    completed += 1
                    if progress:
                        await progress(completed, len(units))
                queue.task_done()

    await asyncio.gather(*(worker() for _ in range(min(workers, max(1, len(units))))))
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
