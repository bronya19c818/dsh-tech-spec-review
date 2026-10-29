"""Deterministic consumption of published structure rules; no semantic inference."""

from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel

from .models import DocumentEvidence, Finding, Rule, TaskResult


class StructureNode(BaseModel):
    part: str
    section_number: str
    section_title: str
    parent: str | None
    level: int
    container: bool
    has_content_rule: bool


def _title(value: str) -> str:
    # Keep semantic punctuation; only whitespace is a pure presentation difference.
    return re.sub(r"\s+", "", value)


def _number(value: str) -> str:
    return ".".join(str(int(x)) for x in value.split("."))


def _navigation_title(value: str) -> str:
    # The generation contract replaces path separators with 、 in filenames.
    return _title(value).replace("/", "、").replace("／", "、").replace("\\", "、")


def parse_structure_tree(rule: Rule) -> list[StructureNode]:
    rule.verify()
    if rule.rule_kind != "structure" or rule.part != "全文" or rule.section_number != "00":
        raise ValueError("STRUCTURE_TREE_INVALID")
    nodes: list[StructureNode] = []
    parents: list[str] = []
    headers: list[str] | None = None
    context_part = ""
    in_tree = False
    modern = ["原编号", "标题", "父章节关系", "内容规则文件/分册清单"]
    legacy = ["部分", "编号", "标题", "内容规则文件"]
    navigation_pattern = re.compile(
        re.escape(rule.object_scope or "")
        + r"—(?P<part>[^—|\n]+)—(?P<number>\d+(?:\.\d+)*)(?P<title>[^|\n]*?)—审查规则\.md"
    )
    for line in rule.content.splitlines():
        line = line.strip()
        if line.startswith("## "):
            in_tree = line[3:].strip() in {"标准章节树", "标准章节树及内容规则导航"}
            headers = None
            context_part = ""
            continue
        if not in_tree:
            continue
        if line.startswith("### "):
            context_part = line[4:].strip()
            headers = None
            continue
        if not line.startswith("|"):
            continue
        if not line.endswith("|"):
            raise ValueError("STRUCTURE_RULE_PARSE_FAILED")
        cells = [x.strip() for x in line[1:-1].split("|")]
        if headers is None:
            if cells not in (modern, legacy):
                raise ValueError("STRUCTURE_RULE_PARSE_FAILED")
            headers = cells
            continue
        if all(re.fullmatch(r":?-{3,}:?", x) for x in cells):
            continue
        if len(cells) != 4:
            raise ValueError("STRUCTURE_RULE_PARSE_FAILED")
        if headers == modern:
            number, title, parent_text, navigation = cells
            explicit_part = context_part
        else:
            explicit_part, number, title, navigation = cells
            parent_text = number.rsplit(".", 1)[0] if "." in number else "—"
        if not re.fullmatch(r"[1-9]\d*(?:\.[1-9]\d*)*", number) or not title:
            raise ValueError("STRUCTURE_TREE_INVALID")
        refs = list(navigation_pattern.finditer(navigation))
        parts = {m["part"] for m in refs}
        if len(parts) > 1 or (explicit_part and parts and parts != {explicit_part}):
            raise ValueError("STRUCTURE_TREE_INVALID")
        # Use exact navigation identities, never substring guesses on arbitrary filenames.
        part = explicit_part or (next(iter(parts)) if parts else "")
        container = bool(re.match(r"无[（(].*容器章", navigation)) or bool(
            re.fullmatch(r"由[\d.]+[—–-][\d.]+规则覆盖", navigation)
        )
        if not part and container and nodes:
            part = nodes[-1].part
        if not part or not navigation or (not container and not refs):
            raise ValueError("STRUCTURE_TREE_INVALID")
        if not container and any(
            _number(m["number"]) != number
            or _navigation_title(m["title"]) != _navigation_title(title)
            for m in refs
        ):
            raise ValueError("STRUCTURE_TREE_INVALID")
        parent = number.rsplit(".", 1)[0] if "." in number else None
        if parent is None:
            if parent_text not in {"—", "-", "无", "无（一级章）"}:
                raise ValueError("STRUCTURE_TREE_INVALID")
        elif not re.fullmatch(re.escape(parent) + r"(?:\s+.+)?", parent_text):
            raise ValueError("STRUCTURE_TREE_INVALID")
        nodes.append(
            StructureNode(
                part=part,
                section_number=number,
                section_title=title,
                parent=parent,
                level=number.count(".") + 1,
                container=container,
                has_content_rule=not container,
            )
        )
        parents.append(parent_text)
    if not nodes:
        raise ValueError("STRUCTURE_RULE_PARSE_FAILED")
    identities = {(n.part, n.section_number): n for n in nodes}
    if len(identities) != len(nodes):
        raise ValueError("STRUCTURE_TREE_INVALID")
    seen: set[tuple[str, str]] = set()
    for node, parent_text in zip(nodes, parents):
        if node.parent:
            parent_node = identities.get((node.part, node.parent))
            if parent_node is None or (node.part, node.parent) not in seen:
                raise ValueError("STRUCTURE_TREE_INVALID")
            declared_title = parent_text[len(node.parent) :].strip()
            if declared_title and _title(declared_title) != _title(parent_node.section_title):
                raise ValueError("STRUCTURE_TREE_INVALID")
        if node.container and not any(
            n.part == node.part and n.parent == node.section_number for n in nodes
        ):
            raise ValueError("STRUCTURE_TREE_INVALID")
        seen.add((node.part, node.section_number))
    return nodes


def structure_rule_response(rule: Rule | None) -> dict:
    if rule is None:
        return {"status": "NO_RULE", "rule": None, "structure_tree": []}
    try:
        tree = parse_structure_tree(rule)
    except ValueError as exc:
        if str(exc) not in {"STRUCTURE_RULE_PARSE_FAILED", "STRUCTURE_TREE_INVALID"}:
            raise
        return {
            "status": "ERROR",
            "error": str(exc),
            "rule": rule.model_dump(),
            "structure_tree": None,
        }
    return {
        "status": "AVAILABLE",
        "rule": rule.model_dump(),
        "structure_tree": [n.model_dump() for n in tree],
    }


def content_chapter_ids(document: DocumentEvidence, rule: Rule | None) -> list[str]:
    """Exclude only exact unique container identities; do not apply inferred mappings to B05."""
    tree = parse_structure_tree(rule) if rule else []
    containers = {
        (n.part, n.section_number, _title(n.section_title))
        for n in tree
        if n.container and not n.has_content_rule
    }
    counts = Counter((c.part, c.number) for c in document.chapters)
    return [
        c.id
        for c in document.chapters
        if counts[(c.part, c.number)] != 1 or (c.part, c.number, _title(c.title)) not in containers
    ]


def check_template_structure(
    document: DocumentEvidence, object_scope: str, rule: Rule | None
) -> TaskResult:
    actual = [c for c in document.chapters if c.number]
    result = TaskResult(
        task_id="B03", object_scope=object_scope, scope=[c.id for c in document.chapters]
    )
    result.actual_structure = [
        dict(
            actual_chapter_id=c.id,
            part=c.part,
            section_number=c.number,
            section_title=c.title,
            parent=c.number.rsplit(".", 1)[0] if "." in c.number else None,
            level=c.number.count(".") + 1,
            evidence_ids=c.evidence_ids[:1],
        )
        for c in actual
    ]
    result.gaps.append("本版尚未执行标题与正文语义匹配、跨章节全局一致性和内容错放判定。")
    if rule is None:
        result.gaps.append("未取得适用的 active 结构规则，未判定必备章节及标准标题符合性。")
        return result
    if rule.object_scope != object_scope:
        raise ValueError("REGISTRY_IDENTITY_MISMATCH")
    tree = parse_structure_tree(rule)
    result.rules = [rule.model_dump()]
    result.structure_tree = [n.model_dump() for n in tree]
    used: set[int] = set()
    assignments: dict[int, int] = {}
    counts = Counter((c.part, c.number) for c in actual)
    # Reserve exact identities first, so misplaced chapters cannot steal valid matches.
    for i, c in enumerate(actual):
        matches = [
            j
            for j, n in enumerate(tree)
            if (c.part, c.number, _title(c.title))
            == (n.part, n.section_number, _title(n.section_title))
        ]
        if len(matches) == 1 and matches[0] not in used:
            assignments[i] = matches[0]
            used.add(matches[0])
    for i, c in enumerate(actual):
        if i in assignments:
            continue
        candidates = [
            j
            for j, n in enumerate(tree)
            if j not in used and (c.part, _title(c.title)) == (n.part, _title(n.section_title))
        ]
        if not candidates:
            candidates = [
                j
                for j, n in enumerate(tree)
                if j not in used
                and (c.number, _title(c.title)) == (n.section_number, _title(n.section_title))
            ]
        if not candidates:
            candidates = [
                j
                for j, n in enumerate(tree)
                if j not in used and (c.part, c.number) == (n.part, n.section_number)
            ]
        if not candidates:
            # Do not resolve ambiguous titles by removing already-used alternatives.
            same_title = [
                j for j, n in enumerate(tree) if _title(c.title) == _title(n.section_title)
            ]
            if len(same_title) == 1 and same_title[0] not in used:
                candidates = same_title
        if len(candidates) == 1:
            assignments[i] = candidates[0]
            used.add(candidates[0])

    def finding(code, detail, c=None, n=None):
        result.findings.append(
            Finding(
                code=code,
                detail=detail,
                rule_id=rule.rule_id,
                binding="DETERMINISTIC",
                target=f"{n.part}/{n.section_number}" if n else f"{c.part}/{c.number}",
                evidence_ids=c.evidence_ids[:1] if c else [],
            )
        )

    last_ranks: dict[tuple[str, str | None], int] = {}
    part_ranks = {part: i for i, part in enumerate(dict.fromkeys(n.part for n in tree))}
    last_part_rank = -1
    current_primary: dict[str, str] = {}
    for i, c in enumerate(actual):
        parent = c.number.rsplit(".", 1)[0] if "." in c.number else None
        physical_parent = current_primary.get(c.part)
        if parent is None:
            current_primary[c.part] = c.number
        n = tree[assignments[i]] if i in assignments else None
        status, confidence, reasons = "MATCHED", "high", []
        before = len(result.findings)
        if n is None:
            if parent is None:
                finding(
                    "STRUCTURE_CHAPTER_UNEXPECTED",
                    f"出现标准树外的一级章：{c.part} {c.number} {c.title}。",
                    c,
                )
                status, reasons = "MISMATCH", ["未找到标准一级章节身份"]
            else:
                status, confidence, reasons = (
                    "NEEDS_REVIEW",
                    "low",
                    ["新增或调整的二级章节，需内容映射复核"],
                )
        else:
            if c.part != n.part:
                finding(
                    "STRUCTURE_PART_MISMATCH",
                    f"章节“{c.title}”应属于{n.part}，实际属于{c.part}。",
                    c,
                    n,
                )
                reasons.append("所属部分不符")
            if n.level == 1:
                if part_ranks[n.part] < last_part_rank:
                    finding(
                        "STRUCTURE_ORDER_MISMATCH",
                        f"章节“{c.title}”所在部分与标准树的部分顺序不符。",
                        c,
                        n,
                    )
                    reasons.append("部分顺序不符")
                last_part_rank = max(last_part_rank, part_ranks[n.part])
                if c.number != n.section_number:
                    finding(
                        "STRUCTURE_NUMBER_MISMATCH",
                        f"一级章“{n.section_title}”编号应为{n.section_number}，实际为{c.number}。",
                        c,
                        n,
                    )
                    reasons.append("一级编号不符")
                if _title(c.title) != _title(n.section_title):
                    finding(
                        "STRUCTURE_TITLE_MISMATCH",
                        f"一级标题应为“{n.section_title}”，实际为“{c.title}”。",
                        c,
                        n,
                    )
                    reasons.append("一级标题不符")
            else:
                if parent != n.parent or (
                    physical_parent and physical_parent != n.section_number.split(".")[0]
                ):
                    finding(
                        "STRUCTURE_PARENT_MISMATCH",
                        f"章节“{c.title}”应属于{n.part} {n.parent}，实际编号或位置的父章不符。",
                        c,
                        n,
                    )
                    reasons.append("父章关系不符")
                if _title(c.title) != _title(n.section_title) or c.number != n.section_number:
                    status, confidence = "NEEDS_REVIEW", "medium"
                    reasons.append("二级标题或编号有调整，需内容映射复核")
            group = (n.part, n.parent)
            rank = assignments[i]
            if rank < last_ranks.get(group, -1):
                finding(
                    "STRUCTURE_ORDER_MISMATCH", f"章节“{c.title}”与标准树的同级顺序不符。", c, n
                )
                reasons.append("同级顺序不符")
            last_ranks[group] = max(rank, last_ranks.get(group, -1))
            if counts[(c.part, c.number)] > 1:
                status, confidence = "NEEDS_REVIEW", "low"
                reasons.append("实际章节身份重复，不能唯一绑定")
        if parent and not any(a.part == c.part and a.number == parent for a in actual):
            finding(
                "STRUCTURE_PARENT_MISMATCH",
                f"章节“{c.number} {c.title}”的父章节 {parent} 不存在。",
                c,
                n,
            )
            reasons.append("实际父章不存在")
        if len(result.findings) > before:
            status = "MISMATCH"
        mapping = dict(
            actual_chapter_id=c.id,
            actual_part=c.part,
            actual_number=c.number,
            actual_title=c.title,
            expected_part=n.part if n else None,
            expected_number=n.section_number if n else None,
            expected_title=n.section_title if n else None,
            mapping_status=status,
            confidence=confidence,
            reason="；".join(reasons) or "部分、编号和标题唯一匹配",
            container=n.container if n else None,
            has_content_rule=n.has_content_rule if n else None,
        )
        result.chapter_mappings.append(mapping)
        if status == "NEEDS_REVIEW":
            result.gaps.append(f"{c.part}/{c.number} {c.title}：{mapping['reason']}。")
    for j, n in enumerate(tree):
        if j in used:
            continue
        if n.level == 1:
            finding(
                "STRUCTURE_CHAPTER_MISSING",
                f"缺少标准一级章节：{n.part} {n.section_number} {n.section_title}。",
                n=n,
            )
        else:
            result.gaps.append(
                f"{n.part}/{n.section_number} {n.section_title} 未找到可靠对应，需内容映射复核。"
            )
        result.chapter_mappings.append(
            dict(
                actual_chapter_id=None,
                actual_part=None,
                actual_number=None,
                actual_title=None,
                expected_part=n.part,
                expected_number=n.section_number,
                expected_title=n.section_title,
                mapping_status="MISMATCH" if n.level == 1 else "NEEDS_REVIEW",
                confidence="high" if n.level == 1 else "low",
                reason="未找到可靠实际章节对应",
                container=n.container,
                has_content_rule=n.has_content_rule,
            )
        )
    result.stats = dict(
        actual_chapters=len(actual),
        expected_chapters=len(tree),
        matched=sum(m["mapping_status"] == "MATCHED" for m in result.chapter_mappings),
        needs_review=sum(m["mapping_status"] == "NEEDS_REVIEW" for m in result.chapter_mappings),
    )
    return result.assign_ids()
