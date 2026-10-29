#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compile professional technical-spec chapter rules into a stable Rule IR.

Current goal: keep the compiler independent from one fixed Markdown section layout.  The compiler
walks topic sections and recognizes semantic headings such as 本章应写明的要求、
必要内容、条件与允许变化、原规则不符合口径 wherever they appear.
"""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

SEMANTIC_HEADINGS = {
    "requirements": ("本章应写明的要求", "应写明的要求", "审查要求", "内容要求", "本章要求"),
    "necessary": ("必要内容", "必备内容", "必需内容", "应包含内容", "必须包含"),
    "variations": ("条件与允许变化", "允许变化", "允许调整", "条件与例外", "可调整范围"),
    "noncompliance": ("原规则不符合口径", "不符合口径", "明确不符合", "不符合情形", "禁止情形"),
}
TEMPLATE_MARKERS = ("本章模板", "模板原文", "模板示例", "参考示例")
EXAMPLE_MARKERS = ("仅为格式", "仅为示例", "示例组件", "不是最终标准", "样例", "示例场景")
RELAXATION_MARKERS = (
    "可按项目",
    "可根据项目",
    "可以根据",
    "可细化",
    "可补充",
    "可调整",
    "可以调整",
    "按项目需求",
    "根据招标项目",
    "以正式",
    "按正式",
    "不得固化",
    "不是最终",
    "仅为格式",
    "仅为示例",
)
CONDITION_MARKERS = ("如", "若", "当", "发生", "仅当", "需要时", "必要时", "特殊环境", "如需")
PROHIBITION_MARKERS = ("不得", "禁止", "不应", "严禁")


def _norm(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "")).strip()


def _clean_md_text(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("　", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def _heading_kind(title: str) -> str | None:
    t = _norm(title)
    for kind, names in SEMANTIC_HEADINGS.items():
        if any(_norm(n) == t or _norm(n) in t for n in names):
            return kind
    return None


def parse_markdown_blocks(content: str) -> List[Dict[str, Any]]:
    """Parse markdown into semantic blocks, preserving the nearest topic heading."""
    lines = str(content or "").splitlines()
    blocks: List[Dict[str, Any]] = []
    topic = "章节规则"
    current_kind = None
    current_title = None
    buf: List[str] = []
    in_template = False

    def flush():
        nonlocal buf
        text = "\n".join(buf).strip()
        if current_kind and text:
            blocks.append(
                {"topic": topic, "kind": current_kind, "heading": current_title, "text": text}
            )
        buf = []

    for raw in lines:
        line = raw.rstrip()
        st = line.strip()
        if st == "<!-- TEMPLATE_ORIGINAL_START -->":
            flush()
            in_template = True
            continue
        if st == "<!-- TEMPLATE_ORIGINAL_END -->":
            in_template = False
            continue
        if in_template:
            continue
        m = re.match(r"^(#{2,4})\s+(.+?)\s*$", st)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            if any(x in title for x in TEMPLATE_MARKERS):
                current_kind = None
                current_title = title
                continue
            kind = _heading_kind(title)
            if kind:
                current_kind = kind
                current_title = title
            else:
                current_kind = None
                current_title = title
                # H2 normally names a review topic; H3/H4 can also act as topic if not semantic.
                if level <= 3 and title not in ("逐项审查方式", "本章小结"):
                    topic = title
            continue
        if current_kind:
            buf.append(line)
    flush()
    return blocks


def _bullets_and_paragraphs(text: str) -> Tuple[List[str], List[str]]:
    bullets: List[str] = []
    paras: List[str] = []
    cur: List[str] = []
    for raw in str(text or "").splitlines():
        s = raw.strip()
        if not s:
            if cur:
                paras.append(_clean_md_text(" ".join(cur)))
                cur = []
            continue
        if re.match(r"^[-*+]\s+", s):
            if cur:
                paras.append(_clean_md_text(" ".join(cur)))
                cur = []
            bullets.append(_clean_md_text(re.sub(r"^[-*+]\s+", "", s)))
        else:
            cur.append(s)
    if cur:
        paras.append(_clean_md_text(" ".join(cur)))
    return bullets, [p for p in paras if p]


def _split_atomic_sentences(text: str) -> List[str]:
    text = _clean_md_text(text)
    if not text:
        return []
    # Semicolons are strong requirement boundaries; full stop is secondary.
    parts = re.split(r"[；;。]\s*", text)
    out = []
    for p in parts:
        p = p.strip(" ，,。；;")
        if len(p) >= 4 and p not in out:
            out.append(p)
    return out or [text]


def extract_expected_terms(text: str) -> List[str]:
    raw = _clean_md_text(text)
    terms: List[str] = []

    def add(x: str):
        x = re.sub(
            r"^(应|须|需|应当|必须|包括|包含|覆盖|明确|保留|列明|写明|具备|能够表达|能表达)",
            "",
            x.strip(),
        )
        x = re.sub(r"(等信息|等内容|等要求|等项目|等字段|等参数|字段|项目)$", "", x).strip(
            " ：:，,；;。"
        )
        if 2 <= len(x) <= 36 and x not in terms:
            terms.append(x)

    for x in re.findall(r"[“‘\"']([^”’\"']{2,40})[”’\"']", raw):
        add(x)

    # Strong noun enumerations after requirement verbs.
    for m in re.finditer(
        r"(?:应|须|需|必须|应当)?(?:具备|包括|包含|覆盖|列明|写明|明确|保留|表达|承载)([^。；;]{3,180})",
        raw,
    ):
        frag = m.group(1)
        frag = re.sub(r"^(以下|相应|相关|实际|项目的|对|为)", "", frag.strip())
        for x in re.split(r"[、，,]|以及|及|和|与", frag):
            add(x)

    # Bullet-like compact requirement itself is often the best expected term.
    if len(raw) <= 36 and not any(
        x in raw for x in ("一致", "关系", "说明", "处理口径", "适用范围", "来源")
    ):
        add(raw)

    return terms[:30]


def _basis_type(kind: str, text: str) -> str:
    if kind == "noncompliance":
        return "explicit_noncompliance"
    if any(m in text for m in PROHIBITION_MARKERS) and not any(
        m in text for m in CONDITION_MARKERS
    ):
        return "prohibition"
    if any(m in text for m in CONDITION_MARKERS) and any(
        m in text for m in ("应", "须", "必须", "不得")
    ):
        return "condition_triggered"
    return "required_content"


def _allows_variation(text: str, variations: Iterable[str]) -> bool:
    n = _norm(text)
    if any(_norm(m) in n for m in RELAXATION_MARKERS):
        return True
    for v in variations:
        if n and (n in _norm(v) or _norm(v) in n):
            return True
    return False


def compile_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    content = str(rule.get("content") or "")
    blocks = parse_markdown_blocks(content)
    variations: List[Dict[str, Any]] = []
    examples: List[Dict[str, Any]] = []
    requirements: List[Dict[str, Any]] = []
    warnings: List[str] = []

    # First collect variation text so hard requirements can carry relaxation flags.
    variation_texts = []
    for b in blocks:
        if b["kind"] == "variations":
            bullets, paras = _bullets_and_paragraphs(b["text"])
            for t in bullets + paras:
                if not t:
                    continue
                variation_texts.append(t)
                variations.append(
                    {
                        "id": f"A{len(variations) + 1}",
                        "basis_section": f"{b['topic']} > {b['heading']}",
                        "basis_text": t,
                        "topic": b["topic"],
                    }
                )
                if any(m in t for m in EXAMPLE_MARKERS):
                    examples.append(
                        {
                            "id": f"E{len(examples) + 1}",
                            "basis_section": f"{b['topic']} > {b['heading']}",
                            "basis_text": t,
                            "topic": b["topic"],
                        }
                    )

    seen = set()
    for b in blocks:
        kind = b["kind"]
        if kind not in ("requirements", "necessary", "noncompliance"):
            continue
        bullets, paras = _bullets_and_paragraphs(b["text"])
        source_items: List[str] = []
        if kind == "requirements":
            for p in paras:
                source_items.extend(_split_atomic_sentences(p))
            source_items.extend(bullets)
        else:
            source_items.extend(bullets)
            for p in paras:
                source_items.extend(_split_atomic_sentences(p))
        for text in source_items:
            text = _clean_md_text(text)
            if len(text) < 3:
                continue
            key = (kind, _norm(text))
            if key in seen:
                continue
            seen.add(key)
            bt = _basis_type(kind, text)
            expected = extract_expected_terms(text)
            req = {
                "id": f"H{len(requirements) + 1}",
                "type": bt,
                "basis_type": bt,  # compatibility with state validator
                "requirement": text,
                "basis_text": text,
                "basis_section": f"{b['topic']} > {b['heading']}",
                "topic": b["topic"],
                "expected_terms": expected,
                "conditions": [m for m in CONDITION_MARKERS if m in text],
                "allowed_variations": [],
                "examples": [],
                "allows_variation": _allows_variation(text, variation_texts),
            }
            requirements.append(req)

    # Keep example-only lines anywhere outside TEMPLATE_ORIGINAL.
    for raw in content.splitlines():
        t = raw.strip().lstrip("-*+ ").strip()
        if t and any(m in t for m in EXAMPLE_MARKERS):
            if not any(_norm(e["basis_text"]) == _norm(t) for e in examples):
                examples.append(
                    {
                        "id": f"E{len(examples) + 1}",
                        "basis_section": "规则正文示例边界",
                        "basis_text": t,
                        "topic": "章节规则",
                    }
                )

    if not requirements:
        warnings.append(
            "RULE_COMPILE_EMPTY_REQUIREMENTS: active chapter rule compiled to zero executable requirements"
        )
    if blocks and not any(b["kind"] == "necessary" for b in blocks):
        warnings.append("RULE_COMPILE_NO_NECESSARY_SECTION: no recognized 必要内容 block")
    if not blocks:
        warnings.append("RULE_COMPILE_NO_SEMANTIC_BLOCKS: no recognized semantic rule blocks")

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
        "requirements": requirements,
        "allowed_variations": variations,
        "examples": examples,
        "compile_warnings": warnings,
        "compile_stats": {
            "semantic_block_total": len(blocks),
            "requirement_total": len(requirements),
            "variation_total": len(variations),
            "example_total": len(examples),
        },
    }


def to_interpretation(ir: Dict[str, Any]) -> Dict[str, Any]:
    hard = []
    for r in ir.get("requirements", []):
        hard.append(
            {
                "id": r["id"],
                "basis_type": r["basis_type"],
                "basis_section": r["basis_section"],
                "basis_text": r["basis_text"],
                "allows_variation": bool(r.get("allows_variation", False)),
                "expected_terms": deepcopy(r.get("expected_terms", [])),
                "topic": r.get("topic", ""),
            }
        )
    allowed = [
        {"id": x["id"], "basis_section": x["basis_section"], "basis_text": x["basis_text"]}
        for x in ir.get("allowed_variations", [])
    ]
    examples = [
        {"id": x["id"], "basis_section": x["basis_section"], "basis_text": x["basis_text"]}
        for x in ir.get("examples", [])
    ]
    return {"hard_requirements": hard, "allowed_variations": allowed, "examples_only": examples}


def build_claim_candidates(ir_or_interp: Dict[str, Any]) -> List[Dict[str, Any]]:
    reqs = ir_or_interp.get("requirements")
    if reqs is None:
        reqs = []
        for x in ir_or_interp.get("hard_requirements", []):
            reqs.append(
                {
                    "id": x.get("id"),
                    "basis_type": x.get("basis_type"),
                    "basis_text": x.get("basis_text"),
                    "expected_terms": x.get("expected_terms")
                    or extract_expected_terms(x.get("basis_text", "")),
                    "topic": x.get("topic", ""),
                }
            )
    out = []
    for r in reqs:
        btype = r.get("basis_type") or r.get("type")
        terms = r.get("expected_terms") or extract_expected_terms(r.get("basis_text", ""))
        base = {
            "basis_id": r.get("id"),
            "basis_type": btype,
            "basis_text": r.get("basis_text") or r.get("requirement"),
            "topic": r.get("topic", ""),
            "expected_terms": terms,
        }
        if btype == "required_content":
            base["allowed_claim_types"] = ["missing_required_content", "required_content_mismatch"]
        elif btype == "explicit_noncompliance":
            base["allowed_claim_types"] = ["explicit_noncompliance"]
            base["violation_terms"] = terms
        elif btype == "prohibition":
            base["allowed_claim_types"] = ["prohibited_content_present"]
            base["prohibited_terms"] = terms
        elif btype == "condition_triggered":
            base["allowed_claim_types"] = ["condition_triggered_violation"]
            base["trigger_terms"] = terms
        else:
            continue
        out.append(base)
    return out


def _unwrap(payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(payload.get("rule"), dict):
        return payload["rule"]
    return payload


def main():
    ap = argparse.ArgumentParser(
        description="Compile technical-spec chapter Markdown rule into Rule IR"
    )
    ap.add_argument(
        "--rule-file",
        required=True,
        help="technical_spec_review_rule_get full JSON response or raw rule JSON",
    )
    ap.add_argument("--output")
    ap.add_argument("--claims")
    args = ap.parse_args()
    payload = json.load(open(args.rule_file, encoding="utf-8"))
    rule = _unwrap(payload)
    ir = compile_rule(rule)
    if args.output:
        Path(args.output).write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.claims:
        Path(args.claims).write_text(
            json.dumps(build_claim_candidates(ir), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "ok": True,
                "requirement_total": ir["compile_stats"]["requirement_total"],
                "warning_total": len(ir["compile_warnings"]),
                "warnings": ir["compile_warnings"],
                "output": args.output,
                "claims": args.claims,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
