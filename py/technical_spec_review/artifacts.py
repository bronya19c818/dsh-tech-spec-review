from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

from docx import Document

from .models import DocumentEvidence, Finding, TaskResult

NON_PROSE_REPORT_ROLES = {
    "heading",
    "table_caption",
    "figure_caption",
    "toc",
    "front_matter",
    "other_special",
}
NO_CHANGE_SUGGESTION = re.compile(r"(?:无需|不需|不必)修改")
INTERNAL_REPORT_IDS = re.compile(
    r"B0[1-5](?:-CH\d{3})?(?:-F\d{4})?|CH\d{3}|S\d{6}|"
    r"t\d+r\d+c\d+p\d+|p\d{4,}|(?:TSR|TSB)-[A-Fa-f0-9]{12,}",
    re.IGNORECASE,
)
INTERNAL_RULE_CODE = re.compile(r"[，,；;]?\s*(?:违反|不符合|符合)?\s*规则\s*B\d{2}-\d{3}")
INTERNAL_STATUS = re.compile(
    r"\b(?:DETERMINISTIC|CANDIDATE|DOCUMENT|candidate|observation|confirmed|"
    r"STRUCTURED|SEMANTIC)\b",
    re.IGNORECASE,
)
QUOTED_SOURCE = re.compile(r"“([^”]+)”|‘([^’]+)’|「([^」]+)」|『([^』]+)』|\"([^\"]+)\"|'([^']+)'")


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def atomic_replace(source: Path, target: Path) -> None:
    """Replace a generated artifact while tolerating short Windows locks."""
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


async def write_json(path: Path, value) -> None:
    await asyncio.to_thread(atomic_text, path, json.dumps(value, ensure_ascii=False, indent=2))


async def read_json(path: Path):
    return json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))


def process_markdown(result: TaskResult) -> str:
    decision_summary = [
        {
            key: value
            for key, value in item.items()
            if key in {"key", "status", "reason", "rule_id", "version", "applicability"}
        }
        for item in result.decisions
        if "key" in item or "rule_id" in item
    ]
    lines = [
        f"# {result.task_id} 审查过程",
        "",
        "状态：COMPLETED",
        f"物资：{result.object_scope}",
        f"路由：{result.route}",
        "范围：" + ", ".join(result.scope),
        "",
        "## 规则",
    ]
    for rule in result.rules:
        lines.append(f"- {rule['rule_id']} / v{rule['version']} / {rule['content_hash']}")
    lines += ["", "## 问题"]
    for item in result.findings:
        lines += [
            f"### {item.id}",
            f"{item.disposition} / {item.binding} / {item.code}",
            item.detail,
            "证据：" + ", ".join(item.evidence_ids),
            "建议：" + item.suggestion,
        ]
    lines += [
        "",
        "## 未覆盖与不足",
        *["- " + gap for gap in result.gaps],
        "",
        "## 规则处置与检索记录",
        "```json",
        json.dumps(
            {
                "decisions": decision_summary,
                "checked_units": len(result.decisions),
                "queries": result.queries,
            },
            ensure_ascii=False,
        ),
        "```",
    ]
    return "\n".join(lines) + "\n"


async def write_result(directory: Path, result: TaskResult) -> None:
    result.assign_ids()
    await write_json(directory / f"{result.task_id}.json", result.model_dump())
    await asyncio.to_thread(
        atomic_text, directory / f"{result.task_id}.md", process_markdown(result)
    )


def normalize_summary_issue_count(summary: dict, issue_ids: list[str]) -> dict:
    """Correct an explicit total in generated prose from the authoritative issue IDs."""
    text = summary.get("summary")
    if not isinstance(text, str):
        return summary.copy()
    pattern = re.compile(
        r"(?P<prefix>(?:本次)?(?:审查)?\s*(?:共)?"
        r"(?:识别出|识别了|发现|形成|提出|归纳出|列出))\s*\d+\s*(?P<unit>个|项)\s*问题"
    )
    corrected, count = pattern.subn(
        lambda match: f"{match.group('prefix')}{len(issue_ids)}{match.group('unit')}问题", text
    )
    if count == 0:
        return summary.copy()
    normalized = summary.copy()
    normalized["summary"] = corrected
    return normalized


def validate_summary(summary: dict, issue_ids: list[str]) -> None:
    if not isinstance(summary.get("summary"), str) or not summary["summary"].strip():
        raise ValueError("SUMMARY_TEXT_REQUIRED")
    groups = summary.get("groups")
    if not isinstance(groups, list):
        raise ValueError("SUMMARY_GROUPS_REQUIRED")
    ids = []
    for group in groups:
        if not isinstance(group.get("title"), str) or not isinstance(group.get("issue_ids"), list):
            raise ValueError("SUMMARY_GROUP_INVALID")
        ids.extend(group["issue_ids"])
    if any(not isinstance(item, str) for item in ids) or sorted(ids) != sorted(issue_ids):
        raise ValueError("SUMMARY_COVERAGE_INVALID")


def _visible_findings(result: TaskResult, document: DocumentEvidence | None) -> list[Finding]:
    blocks = {block.id: block for block in document.blocks} if document else {}
    visible = []
    for finding in result.findings:
        if finding.disposition == "observation" or NO_CHANGE_SUGGESTION.search(finding.suggestion):
            continue
        if result.task_id == "B02" and finding.evidence_ids:
            roles = [blocks[item].role for item in finding.evidence_ids if item in blocks]
            if roles and all(role in NON_PROSE_REPORT_ROLES for role in roles):
                continue
        visible.append(finding)
    return visible


def reportable_findings(
    results: list[TaskResult], document: DocumentEvidence | None = None
) -> list[tuple[TaskResult, Finding]]:
    return [
        (result, finding) for result in results for finding in _visible_findings(result, document)
    ]


def _section_title(result: TaskResult, document: DocumentEvidence | None) -> str:
    fixed_titles = {
        "B01": "版面与格式",
        "B02": "文字与表达",
        "B03": "章节结构",
        "B04": "章节引用",
    }
    if result.task_id in fixed_titles:
        return fixed_titles[result.task_id]
    if result.task_id.startswith("B05-") and document is not None:
        chapter_id = result.task_id.removeprefix("B05-")
        chapter = next((item for item in document.chapters if item.id == chapter_id), None)
        if chapter is not None:
            unit = "章" if "." not in chapter.number else "节"
            number = f"第{chapter.number}{unit} " if chapter.number else ""
            return f"{number}{chapter.title}".strip()
    if result.task_id.startswith("B05-"):
        return "章节内容"
    return "审查结果"


def _plain_report_text(value: str) -> str:
    value = INTERNAL_RULE_CODE.sub("", value)
    value = INTERNAL_REPORT_IDS.sub("", value)
    value = INTERNAL_STATUS.sub("", value)
    replacements = {
        "未绑定确定规则": "未找到可直接对照的审查要求",
        "未绑定规则": "未找到对应的审查要求",
        "候选规则": "可能适用的审查要求",
        "active 版面规则": "格式要求",
        "active 结构规则": "章节检查要求",
        "active 文字规则": "文字检查要求",
        "规则适用性待人工确认": "相关建议是否适用需进一步确认",
        "适用性待确认": "是否适用于本项目需进一步确认",
        "本版尚未执行标题与正文语义匹配、跨章节全局一致性和内容错放判定。": (
            "本次未核对标题是否与正文相符、不同章节之间是否前后一致，以及内容是否放错位置。"
        ),
        "标准一级章节": "标准要求的一级章节",
        "OOXML": "文档格式信息",
    }
    for source, replacement in replacements.items():
        value = value.replace(source, replacement)
    value = re.sub(r"\s*[（(]\s*[）)]", "", value)
    value = re.sub(r"\s+([，。；：、])", r"\1", value)
    value = re.sub(r"([，；、]){2,}", r"\1", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" ，；、")


def _plain_gap(result: TaskResult, gap: str) -> str:
    if result.task_id == "B01":
        if "未取得 active 版面规则" in gap:
            return "未能取得适用的格式检查要求，无法对照模板确认版面设置。"
        if "尚未逐条编译版面规则" in gap or "遮挡、裁切、重叠" in gap:
            return "本次未逐项核对全部格式要求；页面上的遮挡、裁切和重叠等问题需要人工查看。"
    if result.task_id == "B03":
        if "未取得适用的 active 结构规则" in gap:
            return "未找到可对照的章节检查要求，无法确认必备章节和标准标题是否符合要求。"
        if "本版尚未执行标题与正文语义匹配" in gap:
            return "本次未核对标题是否与正文相符、不同章节之间是否前后一致，以及内容是否放错位置。"
        if "需内容映射复核" in gap or "已调整，需内容映射复核" in gap:
            return "部分小节的编号或标题需要人工确认其对应位置。"
    if result.task_id.startswith("B05-"):
        if "未绑定确定规则" in gap:
            return "未找到可直接对照的审查要求，相关事项需结合项目情况人工确认。"
        if "CANDIDATE_FINDING_EVIDENCE_INVALID" in gap:
            return (
                "有一条建议未能在本章原文中准确找到对应位置，未作为问题列出，建议人工核对相关内容。"
            )
        if "CANDIDATE_FINDINGS_IGNORED" in gap:
            return "有一项建议是否适用于本项目尚未确认，因此没有列为明确问题，需人工核对。"
        if "CANDIDATE_ASSESSMENT_MISSING" in gap:
            return "部分审查建议未能确认是否适用，需要人工核对。"
        if "SEMANTIC_TOOL_LOOP_EXHAUSTED" in gap or "SEMANTIC_RESPONSE_INVALID" in gap:
            return "自动查找相关审查要求未能形成可靠结论，需要人工检查本章。"
        if "适用性待确认" in gap:
            _, _, rest = gap.partition("适用性待确认：")
            return _plain_report_text("是否适用于本项目需进一步确认：" + rest)
    return _plain_report_text(gap)


def _decision_quote(result: TaskResult, finding: Finding, block_id: str, source: str) -> str:
    for decision in result.decisions:
        if decision.get("id") == finding.target:
            quote = decision.get("quote")
            if isinstance(quote, str) and quote and quote in source:
                return quote
        evidence = decision.get("evidence", [])
        if isinstance(evidence, list):
            for item in evidence:
                if (
                    isinstance(item, dict)
                    and item.get("id") == block_id
                    and isinstance(item.get("quote"), str)
                    and item["quote"] in source
                ):
                    return item["quote"]
    for text in (finding.detail, finding.suggestion):
        for match in QUOTED_SOURCE.finditer(text):
            quote = next((part for part in match.groups() if part), "")
            if quote and quote in source:
                return quote
    if finding.target and finding.target in source:
        return finding.target
    return ""


def _excerpt(source: str, result: TaskResult, finding: Finding, quote: str) -> tuple[str, bool]:
    if len(source) <= 100:
        return source, False
    if quote:
        excerpt = quote[:100]
        return excerpt + ("…" if len(quote) > 100 else ""), True
    terms = [
        item
        for item in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]+", finding.detail)
        if item not in {"当前章节", "当前文档", "技术规范", "要求", "规则", "模板"}
    ]
    sentences = [item.strip() for item in re.split(r"(?<=[。！？；;])", source) if item.strip()]
    best = max(sentences, key=lambda item: sum(term in item for term in terms), default="")
    if best and any(term in best for term in terms):
        if len(best) <= 100:
            return best, True
        term = next((item for item in terms if item in best), "")
        start = max(0, best.find(term) - 35) if term else 0
        excerpt = best[start : start + 100].strip()
        return excerpt + "…", True
    return source[:100].rstrip() + "…", True


def _evidence_lines(
    result: TaskResult, finding: Finding, document: DocumentEvidence | None
) -> list[str]:
    if not finding.evidence_ids:
        return ["核对结果：在文档中未找到对应内容。"]
    if document is None:
        return ["原文：无法转换为可读摘录。"]
    blocks = {block.id: block for block in document.blocks}
    lines = []
    for block_id in finding.evidence_ids:
        block = blocks.get(block_id)
        if block is None or not block.text.strip():
            continue
        quote = _decision_quote(result, finding, block_id, block.text)
        excerpt, shortened = _excerpt(block.text.strip(), result, finding, quote)
        label = "原文节选" if shortened else "原文"
        lines.append(f"{label}：“{excerpt}”")
    return lines or ["原文：未能定位相应内容。"]


def summary_process_files(
    results: list[TaskResult], document: DocumentEvidence
) -> list[dict[str, str]]:
    files = []
    for result in results:
        lines = ["检查部分：" + _section_title(result, document)]
        for finding in _visible_findings(result, document):
            if finding.disposition == "candidate":
                lines.append("判断：建议核实")
            lines.append("发现：" + _plain_report_text(finding.detail))
            lines.extend(_evidence_lines(result, finding, document))
            if finding.suggestion.strip():
                lines.append("建议：" + _plain_report_text(finding.suggestion))
        lines.extend(
            "需进一步确认：" + _plain_gap(result, gap)
            for gap in result.gaps
            if _plain_gap(result, gap)
        )
        files.append({"task_id": _section_title(result, document), "content": "\n".join(lines)})
    return files


def render_report(
    directory: Path,
    results: list[TaskResult],
    summary: dict,
    source_document: DocumentEvidence | None = None,
) -> None:
    visible = reportable_findings(results, source_document)
    validate_summary(summary, [finding.id for _, finding in visible])
    summary_text = _plain_report_text(summary["summary"])
    lines = ["# 技术规范书审查报告", "", summary_text, "", "## 审查发现", ""]
    document = Document()
    document.add_heading("技术规范书审查报告", 0)
    document.add_paragraph(summary_text)
    document.add_heading("审查发现", 1)
    by_result: dict[int, list[Finding]] = {id(result): [] for result in results}
    for result, finding in visible:
        by_result[id(result)].append(finding)
    issue_number = 0
    for result in results:
        findings = by_result[id(result)]
        if not findings and result.task_id != "B01":
            continue
        section = _section_title(result, source_document)
        lines.extend(["### " + section, ""])
        document.add_heading(section, 2)
        if not findings:
            note = "未发现明确需要修改的版面或格式问题。"
            lines.extend([note, ""])
            document.add_paragraph(note)
            continue
        for finding in findings:
            issue_number += 1
            qualifier = "（待核实）" if finding.disposition == "candidate" else ""
            heading = f"问题 {issue_number}{qualifier}"
            detail = _plain_report_text(finding.detail)
            evidence = _evidence_lines(result, finding, source_document)
            suggestion = (
                _plain_report_text(finding.suggestion) if finding.suggestion.strip() else ""
            )
            lines.extend(["#### " + heading, detail])
            document.add_heading(heading, 3)
            document.add_paragraph(detail)
            for item in evidence:
                lines.append(item)
                document.add_paragraph(item)
            if suggestion:
                suggestion_line = "修改建议：" + suggestion
                lines.append(suggestion_line)
                document.add_paragraph(suggestion_line)
            lines.append("")

    gaps = []
    seen_gaps = set()
    for result in results:
        section = _section_title(result, source_document)
        for gap in result.gaps:
            plain_gap = _plain_gap(result, gap)
            key = (section, plain_gap)
            if plain_gap and key not in seen_gaps:
                seen_gaps.add(key)
                gaps.append((section, plain_gap))
    if gaps:
        lines.extend(["## 需要人工确认的范围", ""])
        document.add_heading("需要人工确认的范围", 1)
        for section, gap in gaps:
            line = f"{section}：{gap}"
            lines.append("- " + line)
            document.add_paragraph(line, style="List Bullet")
    atomic_text(directory / "report.md", "\n".join(lines) + "\n")
    temporary = directory / "report.tmp.docx"
    document.save(str(temporary))
    atomic_replace(temporary, directory / "report.docx")
