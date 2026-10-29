from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from .artifacts import read_json, write_json
from .gate import GateError, obvious_defects
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
            text = text.strip()
            # 纯标点的碎片（"。。"被拆出的"。"）没有独立审查价值，
            # 这类问题由段落层的确定性检查捕获，避免同一处报两次。
            if text and re.search(r"[\w\u4e00-\u9fff]", text):
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


def block_contexts(document: DocumentEvidence) -> dict[str, str]:
    """为每个块准备判断句子时可用的上下文。

    只把"所属章节标题 + 本段原文"交给模型：章节标题解决"这句话在说什么"，
    本段原文解决指代、并列结构和"如下所示"这类悬空表述。整篇塞进去既超长
    也没有额外价值。
    """
    context: dict[str, str] = {}
    current_heading = ""
    paragraphs: dict[str, list[str]] = {}
    for block in document.blocks:
        if block.role == "heading":
            current_heading = block.text.strip()
            continue
        paragraphs[block.id] = [current_heading, block.text.strip()]
    for block in document.blocks:
        parts = paragraphs.get(block.id)
        if not parts:
            continue
        heading, text = parts
        piece = []
        if heading:
            piece.append(f"所属章节：{heading}")
        piece.append(f"本段原文：{text}")
        context[block.id] = "\n".join(piece)
    return context


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
    gate=None,
    gate_threshold: float = 0.5,
    progress=None,
) -> TaskResult:
    """逐句文字质量审查，三层递进。

    T0 确定性规则：重复字词、标点连用这类问题直接定性，不进模型。
    T1 决策模型筛选：对剩余句子打分，低于阈值的放行，不交对话模型。
    T2 对话模型：只处理 T1 判为疑似的句子，按 batch_size 打包调用。

    T1 不可用（未配置、网络失败、返回不合法）时整批退回 T2，绝不静默丢句。
    """
    if workers < 1:
        raise ValueError("WORKERS_MUST_BE_POSITIVE")
    if batch_size < 1:
        raise ValueError("BATCH_SIZE_MUST_BE_POSITIVE")
    if not 0.0 <= gate_threshold <= 1.0:
        raise ValueError("GATE_THRESHOLD_OUT_OF_RANGE")

    rule_data = [rule.verify().model_dump() for rule in rules]
    units = sentences(document)
    contexts = block_contexts(document)

    # --- T0：确定性判定（只补模型盲区，命中的句子不再送后续环节）----------
    # 实测 Clef 对半角标点、重复标点不敏感（0.26 分，低于阈值），而这两类
    # 正则能确定性判断。T0 命中的句子直接出结论，既不送筛选也不送 LLM，
    # 因此不会与后续环节重复报同一处。
    defects: dict[str, list[tuple[str, str, str]]] = {}
    for unit in units:
        found = obvious_defects(unit["text"])
        if found:
            defects[unit["id"]] = found
    # 重复标点在分词阶段就会被拆成两句，单句检查看不到，改在段落层补一次。
    for block in document.blocks:
        if block.role in NON_PROSE_ROLES:
            continue
        extra = obvious_defects(block.text)
        repeated = [item for item in extra if item[0] == "标点错误" and len(item[1]) > 1]
        if not repeated:
            continue
        owner = next(
            (unit for unit in units if unit["evidence_id"] == block.id and unit["id"] not in defects),
            None,
        )
        if owner is not None:
            defects[owner["id"]] = repeated

    pending = [unit for unit in units if unit["id"] not in defects]

    # --- T1：决策模型筛选（对 T0 未命中的句子）----------------------------
    gate_scores: dict[str, float] = {}
    gate_note = ""
    if gate is not None and pending and gate_threshold < 1.0:
        # 按段落的上下文拼成若干批：一次调用能问 40 句，逐段调用会把
        # 几十次串行请求的延迟加起来，比纯 LLM 还慢。
        limit = 40
        chunks: list[tuple[str, list[dict]]] = []
        current: list[dict] = []
        current_contexts: list[str] = []
        seen_contexts: set[str] = set()
        for unit in units:
            if len(current) >= limit:
                chunks.append(("\n\n".join(current_contexts), current))
                current, current_contexts, seen_contexts = [], [], set()
            current.append(unit)
            context = contexts.get(unit["evidence_id"], "")
            if context and context not in seen_contexts:
                seen_contexts.add(context)
                current_contexts.append(context)
        if current:
            chunks.append(("\n\n".join(current_contexts), current))

        groups: list[tuple[str, list[str]]] = [
            (state, [unit["text"] for unit in chunk]) for state, chunk in chunks
        ]
        order: list[dict] = [unit for _, chunk in chunks for unit in chunk]
        try:
            scores = await gate.score(groups)
            if len(scores) != len(order):
                raise GateError("GATE_COVERAGE_INVALID")
            for unit, score in zip(order, scores):
                gate_scores[unit["id"]] = round(float(score), 4)
        except GateError as exc:
            gate_note = f"筛选模型不可用（{exc}），本轮全部句子交对话模型判断。"
            gate_scores = {}

    if gate_scores:
        suspect = [unit for unit in pending if gate_scores.get(unit["id"], 1.0) >= gate_threshold]
        passed = [unit for unit in pending if gate_scores.get(unit["id"], 1.0) < gate_threshold]
    else:
        suspect, passed = pending, []

    # --- T2：对话模型只处理疑似句 ---------------------------------------
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "source": document.sha256,
                "rules": rule_data,
                "object_scope": object_scope,
                "sentence_policy": 5,
                "batch_size": batch_size,
                "gate_threshold": gate_threshold if gate_scores else None,
                "units": units,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cache = directory / "sentences" / cache_key
    batches = [suspect[i : i + batch_size] for i in range(0, len(suspect), batch_size)]
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
                "sentence",
                {
                    **unit,
                    "context": contexts.get(unit["evidence_id"], ""),
                    "object_scope": object_scope,
                    "rules": rule_data,
                },
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
                        {
                            "id": unit["id"],
                            "text": unit["text"],
                            "source_type": unit["source_type"],
                            "context": contexts.get(unit["evidence_id"], ""),
                        }
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
                        await progress(completed, len(suspect))
                queue.task_done()

    if batches:
        await asyncio.gather(*(worker() for _ in range(min(workers, max(1, len(batches))))))
    await write_json(directory / "sentence-failures.json", failures)
    if failures:
        raise RuntimeError(f"SENTENCE_FAILURES:{len(failures)}")

    # --- 汇总 -----------------------------------------------------------
    screened_answers = {
        unit["id"]: {
            "has_issue": False, "type": "", "reason": "", "suggestion": "", "quote": "",
        }
        for unit in passed
    }
    result = TaskResult(
        task_id="B02", object_scope=object_scope, scope=[u["id"] for u in units], rules=rule_data
    )
    result.stats = {
        "total": len(units),
        "deterministic": len(defects),
        "gate_scored": len(gate_scores),
        "gate_suspect": len(suspect) if gate_scores else 0,
        "gate_passed": len(passed),
        "llm_checked": len(answers),
        "completed": len(answers) + len(passed),
        "failed": 0,
        "issues": sum(1 for answer in answers.values() if answer["has_issue"])
        + sum(len(found) for found in defects.values()),
        "batch_size": batch_size,
        "batch_count": len(batches),
        # 阈值按千分比存整数：stats 的类型是 dict[str, int]
        "gate_threshold_permille": int(round(gate_threshold * 1000)) if gate_scores else 0,
    }
    if not rules:
        result.gaps.append("未取得 active 文字规则，本轮仅按通用文字质量提示执行。")
    if gate_note:
        result.gaps.append(gate_note)
    if passed:
        result.gaps.append(
            f"另有 {len(passed)} 句经筛选模型判定无文字问题，未送对话模型判断。"
        )

    for unit in units:
        if unit["id"] in defects:
            for category, evidence, detail in defects[unit["id"]]:
                result.findings.append(
                    Finding(
                        code="TEXT_DEFECT",
                        detail=f"{category}：{detail}",
                        evidence_ids=[unit["evidence_id"]],
                        target=unit["id"],
                        suggestion=f"核对并修正{category}：{evidence}",
                        disposition="candidate",
                    )
                )
            continue
        answer = answers.get(unit["id"])
        if answer is None:
            continue
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

    decisions = []
    for unit in units:
        if unit["id"] in defects:
            decisions.append({
                **unit,
                "has_issue": True,
                "type": defects[unit["id"]][0][0],
                "reason": defects[unit["id"]][0][2],
                "suggestion": "",
                "quote": defects[unit["id"]][0][1],
                "source": "deterministic",
            })
        elif unit["id"] in answers:
            decisions.append({**unit, **answers[unit["id"]], "source": "llm",
                              "gate_score": gate_scores.get(unit["id"])})
        else:
            decisions.append({**unit, **screened_answers[unit["id"]],
                              "source": "screened",
                              "gate_score": gate_scores.get(unit["id"])})
    result.decisions = decisions
    return result.assign_ids()
