#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compile a structure rule Markdown table into canonical Structure IR."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

TABLE_HEADING_ALIASES = ("标准章节树及内容规则导航", "标准章节树", "章节结构", "内容规则导航")
PART_NAVIGATION_LABELS = ("通用部分", "专用部分")


def _norm(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "")).strip()


def _split_row(line: str) -> List[str]:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return []
    return [x.strip() for x in s[1:-1].split("|")]


def _is_separator(cells: List[str]) -> bool:
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", re.sub(r"\s+", "", c or "")) for c in cells
    )


def _rule_files(cell: str) -> List[str]:
    vals = []
    for x in re.findall(r"[^|，,；;\n]*?\.md", str(cell or "")):
        x = x.strip()
        if x and x not in vals:
            vals.append(x)
    return vals


def _part_hints(navigation: str) -> List[str]:
    return [part for part in PART_NAVIGATION_LABELS if part in navigation]


def _parent_number(section_number: str) -> str | None:
    s = str(section_number or "").strip()
    if "." not in s:
        return None
    return s.rsplit(".", 1)[0]


def compile_structure_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    content = str(rule.get("content") or "")
    lines = content.splitlines()
    nodes = []
    in_target = False
    headers = None
    current_part = None
    warnings = []
    for i, raw in enumerate(lines):
        st = raw.strip()
        if st.startswith("## "):
            title = st[3:].strip()
            in_target = any(a in title for a in TABLE_HEADING_ALIASES)
            headers = None
            current_part = None
            continue
        if not in_target or not st.startswith("|"):
            continue
        cells = _split_row(st)
        if not cells:
            continue
        if headers is None:
            headers = cells
            current_part = None
            continue
        if _is_separator(cells):
            continue
        if len(cells) != len(headers):
            warnings.append(f"STRUCTURE_TABLE_COLUMN_MISMATCH: line {i + 1}")
            continue
        row = {_norm(h): c for h, c in zip(headers, cells)}
        part = (row.get("部分") or row.get("所属部分") or "").strip()
        number = row.get("编号") or row.get("原编号") or row.get("章节编号")
        title = row.get("标题") or row.get("章节标题")
        nav = (
            row.get("内容规则文件") or row.get("内容规则文件/分册清单") or row.get("规则文件") or ""
        )
        if not (number and title):
            continue
        if part:
            current_part = part
        else:
            hints = _part_hints(nav)
            if len(hints) == 1:
                part = hints[0]
                current_part = part
            elif len(hints) > 1:
                warnings.append(f"STRUCTURE_PART_AMBIGUOUS: line {i + 1}")
                continue
            else:
                part = current_part
        if not part:
            warnings.append(f"STRUCTURE_PART_UNRESOLVED: line {i + 1}")
            continue
        files = _rule_files(nav)
        is_container = not bool(files)
        nodes.append(
            {
                "part": part,
                "section_number": str(number),
                "section_title": title,
                "parent_section_number": _parent_number(str(number)),
                "is_container": is_container,
                "rule_files": files,
                "navigation_text": nav,
                "sequence_no": len(nodes) + 1,
            }
        )
    if not nodes:
        warnings.append("STRUCTURE_COMPILE_EMPTY: no canonical nodes parsed from structure rule")
    seen = set()
    for n in nodes:
        ident = (n["part"], n["section_number"], n["section_title"])
        if ident in seen:
            warnings.append("STRUCTURE_DUPLICATE_IDENTITY: " + " / ".join(ident))
        seen.add(ident)
    return {
        "ir_version": "1.0",
        "rule_identity": {
            k: rule.get(k)
            for k in (
                "rule_id",
                "version",
                "object_scope",
                "document_role",
                "part",
                "section_number",
                "section_title",
                "rule_kind",
                "document_name",
                "content_hash",
                "status",
            )
            if rule.get(k) is not None
        },
        "nodes": nodes,
        "compile_warnings": warnings,
        "compile_stats": {
            "node_total": len(nodes),
            "content_node_total": sum(1 for x in nodes if x.get("rule_files")),
            "container_node_total": sum(1 for x in nodes if x.get("is_container")),
        },
    }


def _unwrap(payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(payload.get("rule"), dict):
        return payload["rule"]
    return payload


def main():
    ap = argparse.ArgumentParser(
        description="Compile technical-spec structure rule into canonical Structure IR"
    )
    ap.add_argument("--rule-file", required=True)
    ap.add_argument("--output")
    args = ap.parse_args()
    payload = json.load(open(args.rule_file, encoding="utf-8"))
    ir = compile_structure_rule(_unwrap(payload))
    if args.output:
        Path(args.output).write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": not bool(ir["compile_warnings"]),
                "stats": ir["compile_stats"],
                "warnings": ir["compile_warnings"],
                "output": args.output,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
