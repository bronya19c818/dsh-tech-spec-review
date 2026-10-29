from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .models import Block, DocumentEvidence, Finding, Rule, TaskResult
from .parser import CAPTION, chinese_number

REFERENCE = re.compile(
    r"(?:参见|详见|见|参照|如|按照|按)\s*(?:本文件\s*)?(?:第\s*)?"
    r"(?:(?P<chapter>\d+(?:[.．]\d+)*)\s*(?:条|章|节)|"
    r"(?P<kind>表|图|附录)\s*(?P<target>[A-Z]|\d+(?:[.．-]\d+)*))"
)
KINDS = {"表": "table", "图": "figure", "附录": "appendix"}
CHINESE_NUMERALS = "一二三四五六七八九十"
CIRCLED_NUMERALS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
BODY_LIST_NUMBER_FAMILIES = {
    "cn_paren",
    "arabic_paren_full",
    "arabic_paren",
    "arabic_rparen",
    "circled",
    "cn_dunhao",
    "arabic_dunhao",
    "arabic_dot",
}


@dataclass(frozen=True)
class NumberToken:
    family: str
    value: int
    raw: str
    full_text: str
    block_id: str
    order_index: int
    path: tuple[int, ...] = ()


def _token_from_prefix(
    block: Block, prefix: str, full_text: str, order_index: int
) -> NumberToken | None:
    def make(family: str, value: int, raw: str, path: tuple[int, ...] = ()) -> NumberToken:
        return NumberToken(
            family=family,
            value=value,
            raw=raw,
            full_text=full_text,
            block_id=block.id,
            order_index=order_index,
            path=path,
        )

    match = re.match(
        rf"^(?:（(?P<full>[{CHINESE_NUMERALS}]+)）|\((?P<half>[{CHINESE_NUMERALS}]+)\))",
        prefix,
    )
    if match:
        value = match["full"] or match["half"]
        return make("cn_paren", int(chinese_number(value)), match[0])

    match = re.match(r"^(（(?P<value>\d+)）)", prefix)
    if match:
        return make("arabic_paren_full", int(match["value"]), match[1])

    match = re.match(r"^(\((?P<value>\d+)\))", prefix)
    if match:
        return make("arabic_paren", int(match["value"]), match[1])

    match = re.match(r"^(?P<value>\d+)\)", prefix)
    if match:
        return make("arabic_rparen", int(match["value"]), match[0])

    match = re.match(r"^(?P<value>[①-⑳])", prefix)
    if match:
        return make("circled", CIRCLED_NUMERALS.index(match["value"]) + 1, match[0])

    match = re.match(rf"^([{CHINESE_NUMERALS}]+)([、．.])", prefix)
    if match:
        return make("cn_dunhao", int(chinese_number(match[1])), match[0])

    match = re.match(r"^(\d+)(、)", prefix)
    if match:
        return make("arabic_dunhao", int(match[1]), match[0])

    match = re.match(r"^(\d+(?:[.．]\d+)+)([.．]?)(?=\s|[^\d]|$)", prefix)
    if match:
        path = tuple(int(part) for part in re.split(r"[.．]", match[1]))
        family = f"decimal_depth_{len(path)}"
        return make(family, path[-1], match[0], path)

    match = re.match(r"^(\d+)([.．])(?=\s|[^\d]|$)", prefix)
    if match:
        return make("arabic_dot", int(match[1]), match[0])

    match = re.match(r"^(\d+)(?=\s|$)", prefix)
    if match:
        value = int(match[1])
        return make("decimal_depth_1", value, match[1], (value,))

    return None


def _number_token(block: Block, order_index: int) -> NumberToken | None:
    full_text = block.text.strip()
    for prefix in (full_text, block.display_number.strip()):
        if not prefix:
            continue
        token = _token_from_prefix(block, prefix, full_text, order_index)
        if token is not None:
            return token
    return None


def _is_numbering_candidate(block: Block, token: NumberToken) -> bool:
    if block.role == "toc":
        return False
    if block.number and block.role == "heading":
        return True
    if block.number and block.role == "body" and block.is_clause:
        return True

    if block.kind != "paragraph" or block.in_table or block.role != "body":
        return False
    if not block.numbering:
        return False
    return block.number_source == "word_auto_numbering" or token.family in BODY_LIST_NUMBER_FAMILIES


def check_numbering_sequence(document: DocumentEvidence) -> list[Finding]:
    groups: dict[str, list[NumberToken]] = defaultdict(list)
    for order_index, block in enumerate(document.blocks):
        token = _number_token(block, order_index)
        if token is not None and _is_numbering_candidate(block, token):
            groups[token.family].append(token)

    ordered_findings: list[tuple[int, Finding]] = []
    for tokens in groups.values():
        previous: NumberToken | None = None
        for current in tokens:
            if previous is None:
                if current.value != 1:
                    ordered_findings.append(
                        (
                            current.order_index,
                            Finding(
                                code="NUMBERING_ANOMALY",
                                target=current.full_text,
                                detail=(f"编号格式首次出现于“{current.full_text}”，存在编号异常。"),
                                evidence_ids=[current.block_id],
                            ),
                        )
                    )
            elif current.value != 1 and current.value != previous.value + 1:
                ordered_findings.append(
                    (
                        current.order_index,
                        Finding(
                            code="NUMBERING_ANOMALY",
                            target=current.full_text,
                            detail=(
                                f"编号序列在“{previous.full_text}”之后出现"
                                f"“{current.full_text}”，存在编号异常。"
                            ),
                            evidence_ids=[previous.block_id, current.block_id],
                        ),
                    )
                )
            previous = current

    ordered_findings.sort(key=lambda item: item[0])
    return [finding for _, finding in ordered_findings]


def check_references(document: DocumentEvidence, object_scope: str) -> TaskResult:
    targets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for block in document.blocks:
        if block.role == "toc":
            continue
        if (
            block.kind == "paragraph"
            and (block.role == "heading" or block.is_clause)
            and block.number
        ):
            targets[(block.part, "chapter:" + block.number)].append(block.id)
        caption = CAPTION.match(block.text)
        if (
            caption
            and block.kind == "paragraph"
            and not re.match(r"(?:为|是|中|内|的|应|仅|所|给出|列出|包含|示出)", caption[3].strip())
        ):
            key = KINDS[caption[1]] + ":" + caption[2].replace("．", ".")
            targets[(block.part, key)].append(block.id)
    result = TaskResult(
        task_id="B04", object_scope=object_scope, scope=[b.id for b in document.blocks]
    )
    for block in document.blocks:
        if block.role == "toc":
            continue
        for ref in REFERENCE.finditer(block.text):
            key = (
                "chapter:" + ref["chapter"].replace("．", ".")
                if ref["chapter"]
                else KINDS[ref["kind"]] + ":" + ref["target"].replace("．", ".")
            )
            matches = targets.get((block.part, key), [])
            if not matches and key.startswith("appendix:"):
                matches = [
                    bid for (_, target), ids in targets.items() if target == key for bid in ids
                ]
            result.decisions.append(
                {
                    "target": key,
                    "source_id": block.id,
                    "target_ids": matches,
                    "status": "valid" if len(matches) == 1 else "invalid",
                }
            )
            if len(matches) != 1:
                result.findings.append(
                    Finding(
                        code="REFERENCE_MISSING" if not matches else "REFERENCE_AMBIGUOUS",
                        target=key,
                        detail=f"显式内部引用“{ref[0]}”的目标"
                        + ("不存在。" if not matches else "不唯一。"),
                        evidence_ids=[block.id, *matches],
                        suggestion="核对引用编号与实际目标。",
                    )
                )
    result.stats = {
        "scanned": len(result.decisions),
        "valid": len(result.decisions) - len(result.findings),
        "invalid": len(result.findings),
    }
    return result.assign_ids()


def normalize_title(title: str) -> str:
    return re.sub(r"[\s/／、,，\-—]", "", title)


def check_structure(document: DocumentEvidence, object_scope: str, rule: Rule | None) -> TaskResult:
    from .structure import check_template_structure

    return check_template_structure(document, object_scope, rule)


def check_layout(document: DocumentEvidence, object_scope: str, rules: list[Rule]) -> TaskResult:
    result = TaskResult(
        task_id="B01",
        object_scope=object_scope,
        scope=[b.id for b in document.blocks],
        rules=[r.model_dump() for r in rules],
    )
    if not rules:
        result.gaps.append("未取得 active 版面规则。")

    role_counts = Counter(block.role for block in document.blocks)
    body_blocks = [block for block in document.blocks if block.role == "body"]
    body_style_counts = Counter(block.style_name or "（未指定）" for block in body_blocks)
    most_common_count = max(body_style_counts.values(), default=0)
    most_common_styles = [
        style for style, count in body_style_counts.items() if count == most_common_count
    ]
    primary_body_style = most_common_styles[0] if len(most_common_styles) == 1 else None
    mixed_body_blocks = (
        [block for block in body_blocks if (block.style_name or "（未指定）") != primary_body_style]
        if primary_body_style is not None
        else body_blocks
        if len(body_style_counts) > 1
        else []
    )
    result.stats = {
        "body_paragraphs": len(body_blocks),
        "body_style_count": len(body_style_counts),
        "body_style_inconsistent_paragraphs": len(mixed_body_blocks),
    }
    result.decisions.append(
        {
            "type": "paragraph_role_style_summary",
            "role_counts": dict(role_counts),
            "body_style_counts": dict(body_style_counts),
            "primary_body_style": primary_body_style,
        }
    )
    if len(body_style_counts) > 1:
        style_distribution = "、".join(
            f"“{style}”{count}段" for style, count in body_style_counts.items()
        )
        if primary_body_style is None:
            detail_lines = [
                f"正文段落共 {len(body_blocks)} 段，样式分布为：{style_distribution}。",
                "各正文段落实际使用的样式如下：",
            ]
            suggestion = "请依据文档格式要求确定统一的正文段落样式，并逐段调整。"
        else:
            detail_lines = [
                f"正文段落共 {len(body_blocks)} 段，主要使用“{primary_body_style}”样式；",
                f"样式分布为：{style_distribution}。以下段落使用其他样式：",
            ]
            suggestion = f"请核对并统一以下正文段落的样式，优先采用“{primary_body_style}”样式。"
        detail_lines.extend(
            f"{index}. 段落 {block.paragraph_index if block.paragraph_index is not None else block.id}"
            f"（{block.id}），实际样式“{block.style_name or '（未指定）'}”："
            f"{(block.source_text or block.text).strip()}"
            for index, block in enumerate(mixed_body_blocks, 1)
        )
        result.findings.append(
            Finding(
                code="BODY_STYLE_MIXED",
                target="正文段落样式",
                detail="\n".join(detail_lines),
                evidence_ids=[block.id for block in mixed_body_blocks],
                suggestion=suggestion,
                disposition="confirmed",
            )
        )

    for rule in rules:
        rule.verify()
        # Only compare styles when the published rule explicitly requires like-for-like consistency.
        if "标题" in rule.content and "一致" in rule.content:
            groups: dict[int, list] = defaultdict(list)
            for block in document.blocks:
                if block.role == "heading":
                    level = block.outline_level or block.level or 1
                    groups[level].append(block)
            for level, headings in groups.items():
                signatures = {tuple(sorted(b.format.items())) for b in headings}
                if len(headings) > 1 and len(signatures) > 1:
                    result.findings.append(
                        Finding(
                            code="HEADING_FORMAT_DIFFERENCE",
                            detail=f"第 {level} 级标题的 OOXML 样式或直接字体设置存在差异，需确认实际显示。",
                            evidence_ids=[b.id for b in headings],
                            rule_id=rule.rule_id,
                            binding="DETERMINISTIC",
                            disposition="observation",
                        )
                    )
        result.gaps.append(
            f"{rule.rule_id}：尚未逐条编译版面规则；遮挡、裁切、重叠及显示效果需要页面渲染复核。"
        )
    return result.assign_ids()
