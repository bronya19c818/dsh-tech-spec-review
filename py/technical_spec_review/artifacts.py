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
    r"(?<![A-Za-z0-9_])(?:B0[1-5](?:-CH\d{3})?(?:-F\d{4})?|CH\d{3}|S\d{6}|"
    r"t\d+r\d+c\d+p\d+|[tc]\d{3,}(?:-[tc]\d{3,})?|p\d{4,}|"
    r"(?:TSR|TSB)-[A-Fa-f0-9]{12,})(?![A-Za-z0-9_])"
)

INTERNAL_RULE_CODE = re.compile(r"[，,；;]?\s*(?:违反|不符合|符合)?\s*规则\s*B\d{2}-\d{3}")
INTERNAL_STATUS = re.compile(
    r"(?:DETERMINISTIC|CANDIDATE|DOCUMENT|STRUCTURED|SEMANTIC|"
    r"confirmed_issue|candidate_issue|observation)",
    re.IGNORECASE,
)
# "active 规则""active 结构规则" 一类说法：把 active 去掉，剩下的走 PHRASE_REPLACEMENTS。
ACTIVE_MARK = re.compile(r"active\s*", re.IGNORECASE)
QUOTED_SOURCE = re.compile(r"“([^”]+)”|‘([^’]+)’|「([^」]+)」|『([^』]+)』|\"([^\"]+)\"|'([^']+)'")

# 任务代号换成中文名，避免"由 B03 检查"删成"由检查"这类残句。
TASK_NAMES = {
    "B01": "版面格式",
    "B02": "文字表达",
    "B03": "章节结构",
    "B04": "章节引用",
    "B05": "章节内容",
}
TASK_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(B0[1-5])(?![A-Za-z0-9_-])")

# 报告里这些说法指向内部实现，替换成读者能直接理解的中文。
PHRASE_REPLACEMENTS = {
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

# 删掉内部编号后会留下"由负责""route 为，"这类残句。按固定说法补回中文，
# 顺序有讲究：先补动词残缺的句子，再清空括号与多余标点。
RESIDUE_REPAIRS = (
    (re.compile(r"由\s*负责"), "由章节结构检查负责"),
    (re.compile(r"由等[^，。；]{0,12}检查处理"), "由章节结构检查处理"),
    (re.compile(r"本次\s*route\s*为\s*[A-Za-z]*\s*[，,]?"), "本次"),
    (re.compile(r"route\s*为\s*[A-Za-z]*\s*[，,]?"), ""),
    # "证据（-）""段落证据（c001-c005）"：编号删光后剩下的空括号一起清掉。
    (re.compile(r"证据\s*[（(][^）)]{0,60}[）)]"), "证据"),
    (re.compile(r"[（(]\s*证据[^）)]{0,60}[）)]"), ""),
    (re.compile(r"证据[：:]\s*[、,，]?\s*(?=[，。；]|$)"), ""),
    (re.compile(r"[（(][\s、,，\-—–~～.·…]*[）)]"), ""),
    # 编号被删后可能留下"仅、两段"这种孤零零的顿号。
    (re.compile(r"([仅限如等])\s*[、,，]+"), r"\1"),
    # "B05章节" 换成中文后会和后面的"章节"叠字。
    (re.compile(r"章节内容章节"), "章节内容"),
    (re.compile(r"(内容|章节|结构|引用|格式|表达)\1"), r"\1"),
    (re.compile(r"[：:]\s*(?=[，。；])"), ""),
)

# 只剩连接词加一个残动词的句子（"由负责""按分工由检查处理"）；整句命中即丢弃。
# 必须有残动词才匹配，纯空白不会命中。
RESIDUE_ONLY = re.compile(
    r"[由按本次的在该于与及并或，、；：。\s]{0,16}(?:负责|检查处理|处理)[。；]?"
)


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


def repair_summary_coverage(summary: dict, issue_ids: list[str]) -> dict:
    """把模型分组结果修正到"每条问题恰好出现一次"。

    模型偶尔漏掉或重复问题编号。完整性由程序担保，不靠模型自觉：
    去重、丢弃不存在的编号、把漏掉的问题收进「其他问题」分组。
    """
    groups = summary.get("groups")
    if not isinstance(groups, list):
        return summary
    known = set(issue_ids)
    seen: set[str] = set()
    repaired: list[dict] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        ids = []
        for item in group.get("issue_ids") or []:
            if isinstance(item, str) and item in known and item not in seen:
                seen.add(item)
                ids.append(item)
        repaired.append({**group, "issue_ids": ids})
    missing = [item for item in issue_ids if item not in seen]
    if missing:
        repaired.append({"title": "其他问题", "issue_ids": missing})
    return {**summary, "groups": repaired}


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
    """报告要列出的问题：先按可见性过滤，再全局去重。

    去重放在这里而不是渲染时，是因为 engine 也用它算 issue_ids 交给模型汇总；
    两处必须用同一份清单，否则报告头部的计数会和实际列出的条目对不上。
    """
    seen: set[tuple[str, str]] = set()
    picked: list[tuple[TaskResult, Finding]] = []
    for result in results:
        for finding in _visible_findings(result, document):
            key = _finding_key(finding)
            if key in seen:
                continue
            seen.add(key)
            picked.append((result, finding))
    return picked


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


def _drop_residue_sentences(value: str) -> str:
    """丢掉只剩虚词的残句，别让读者对着半句话猜意思。

    清理编号后可能留下"由负责""按分工由检查处理"这类没有主语的片段。整句都在
    RESIDUE_ONLY 里命中时，直接丢弃该句；句中仍有实质内容时原样保留。
    """
    sentences = [item for item in re.split(r"(?<=[。！？；])", value) if item.strip()]
    kept = [item for item in sentences if not RESIDUE_ONLY.fullmatch(item.strip(" 。；，"))]
    return "".join(kept)


def _plain_report_text(value: str) -> str:
    # 先把任务代号换成中文名，再删其他编号。顺序反了会留下"由负责"这种残句：
    # 删掉 B03 却没人补上它指代的检查项。
    value = TASK_TOKEN.sub(lambda match: TASK_NAMES[match.group(1)], value)
    value = INTERNAL_RULE_CODE.sub("", value)
    value = INTERNAL_REPORT_IDS.sub("", value)
    value = INTERNAL_STATUS.sub("", value)
    for source, replacement in PHRASE_REPLACEMENTS.items():
        value = value.replace(source, replacement)
    value = ACTIVE_MARK.sub("", value)
    for pattern, replacement in RESIDUE_REPAIRS:
        value = pattern.sub(replacement, value)
    value = re.sub(r"\s+([，。；：、])", r"\1", value)
    value = re.sub(r"([，；、]){2,}", r"\1", value)
    # 替换英文术语后常留下"未取得 文字检查要求"这种空格，中文之间不该有空格。
    value = re.sub(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])", "", value)
    value = re.sub(r"\s{2,}", " ", value)
    value = _drop_residue_sentences(value)
    return value.strip(" ，；、")


def _shorten(text: str, limit: int = 70) -> str:
    """把一句话压到 limit 字以内，尽量在标点处断开。

    gaps 里常带一大段"为什么这条规则不适用"的推理，读者只需要知道要确认什么。
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("。"), head.rfind("；"), head.rfind("，"))
    if cut >= 24:
        return head[:cut] + "。"
    return head.rstrip("，；、 ")[:-1] + "…"


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


EVIDENCE_LIMIT = 3


def _evidence_lines(
    result: TaskResult, finding: Finding, document: DocumentEvidence | None
) -> list[str]:
    """列出问题对应的原文，但正文里已经引用过的片段不再重复。

    B01 这类检查会把原文直接写进 detail，再逐条附 evidence 就会整段翻一倍。
    同一句被多条证据指向时也只留一次；最多 EVIDENCE_LIMIT 条。
    """
    if not finding.evidence_ids:
        return ["核对结果：在文档中未找到对应内容。"]
    if document is None:
        return ["原文：无法转换为可读摘录。"]
    blocks = {block.id: block for block in document.blocks}
    detail_core = re.sub(r"\s+", "", finding.detail)
    lines: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for block_id in finding.evidence_ids:
        block = blocks.get(block_id)
        if block is None or not block.text.strip():
            continue
        quote = _decision_quote(result, finding, block_id, block.text)
        excerpt, shortened = _excerpt(block.text.strip(), result, finding, quote)
        core = re.sub(r"\s+", "", excerpt.rstrip("…"))
        if core and (core in detail_core or core in seen):
            skipped += 1
            continue
        seen.add(core)
        label = "原文节选" if shortened else "原文"
        lines.append(f"{label}：“{excerpt}”")
        if len(lines) >= EVIDENCE_LIMIT:
            break
    if lines:
        return lines
    if skipped:
        return []
    return ["原文：未能定位相应内容。"]


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


def _finding_key(finding: Finding) -> tuple[str, str]:
    """判定两条问题是否指向同一处、同一改法。

    以证据位置加修改建议为准：同一句原文被两条规则命中时，改法相同，报告里
    只保留一条。没有证据的问题（例如缺少某个一级章）按问题描述区分，避免把
    十几个不同的缺章合并成一条。
    """
    place = ",".join(finding.evidence_ids) or re.sub(r"[\s，。；、（）()]", "", finding.detail)
    return place, re.sub(r"[\s，。；、（）()]", "", finding.suggestion)


GAP_LIMIT_PER_SECTION = 2


GAP_BOILERPLATE = "未找到可直接对照的审查要求，相关事项需结合项目情况人工确认。"


def consolidate_gaps(
    results: list[TaskResult],
    source_document: DocumentEvidence | None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """把"需要人工确认"整理成能照着做的清单。

    "未找到可直接对照的审查要求"这句话几乎每章都有，说一次就够：只有这句的
    章节汇总成一句总说明，其余章节只留具体事项，每节最多两条。
    """
    grouped: list[tuple[str, list[str]]] = []
    for result in results:
        section = _section_title(result, source_document)
        texts: list[str] = []
        seen: set[str] = set()
        for gap in result.gaps:
            plain_gap = _shorten(_plain_gap(result, gap), 70)
            key = re.sub(r"[\s，。；、]", "", plain_gap)
            if not plain_gap or key in seen:
                continue
            seen.add(key)
            texts.append(plain_gap)
        if texts:
            grouped.append((section, texts))

    bullets: list[tuple[str, str]] = []
    bare: list[str] = []
    for section, texts in grouped:
        cleaned = []
        for item in texts:
            rest = item.replace(GAP_BOILERPLATE, "").lstrip("；;，,。 ")
            if rest:
                cleaned.append(rest)
        if not cleaned:
            bare.append(section)
            continue
        head = cleaned[:GAP_LIMIT_PER_SECTION]
        extra = len(cleaned) - len(head)
        line = "；".join(item.rstrip("。；") for item in head)
        if extra > 0:
            line += f"；另有 {extra} 项同类事项"
        bullets.append((section, line + "。"))
    return bullets, bare


def _summary_paragraphs(text: str, limit: int = 160) -> list[str]:
    """把模型给的一大段概况按句号切成几段，避免一整屏没有换行的文字。"""
    sentences = [item for item in re.split(r"(?<=[。；])", text) if item.strip()]
    paragraphs: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > limit:
            paragraphs.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        paragraphs.append(current)
    return paragraphs or [text]


def render_report(
    directory: Path,
    results: list[TaskResult],
    summary: dict,
    source_document: DocumentEvidence | None = None,
) -> None:
    visible = reportable_findings(results, source_document)
    validate_summary(summary, [finding.id for _, finding in visible])
    summary_text = _plain_report_text(summary["summary"])

    confirmed = sum(1 for _, finding in visible if finding.disposition != "candidate")
    candidate = len(visible) - confirmed
    counts_line = f"共 {len(visible)} 项，其中可直接修改 {confirmed} 项，建议核实 {candidate} 项。"

    summary_paragraphs = _summary_paragraphs(summary_text)
    lines = [
        "# 技术规范书审查报告",
        "",
        *summary_paragraphs,
        "",
        counts_line,
        "",
        "## 审查发现",
        "",
    ]
    document = Document()
    document.add_heading("技术规范书审查报告", 0)
    for paragraph in summary_paragraphs:
        document.add_paragraph(paragraph)
    document.add_paragraph(counts_line)
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
            qualifier = "（建议核实）" if finding.disposition == "candidate" else ""
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

    bullets, bare = consolidate_gaps(results, source_document)
    if bullets or bare:
        lines.extend(["## 需要人工确认的范围", ""])
        document.add_heading("需要人工确认的范围", 1)
        if bare:
            lead = (
                f"以下 {len(bare)} 个章节未找到可直接对照的审查要求，"
                f"相关事项需结合项目情况人工确认：{'、'.join(bare)}。"
            )
            lines.extend([lead, ""])
            document.add_paragraph(lead)
        for section, gap in bullets:
            line = f"{section}：{gap}"
            lines.append("- " + line)
            document.add_paragraph(line, style="List Bullet")
    atomic_text(directory / "report.md", "\n".join(lines) + "\n")
    temporary = directory / "report.tmp.docx"
    document.save(str(temporary))
    atomic_replace(temporary, directory / "report.docx")
