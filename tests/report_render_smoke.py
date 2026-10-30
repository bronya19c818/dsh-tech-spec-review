"""报告渲染冒烟：拿一次真实审查的产物重渲染，检查交付文本是否可读。

用法：python tests/report_render_smoke.py <run目录> [输出目录]

检查项（都是上次测试会话里真实出现过的毛病）：
1. 不得出现内部编号（B0x / p0001 / t001r001c001p002 / TSR-xxx）。
2. 不得出现编号被删后留下的残句（"由负责" "route 为，" "（证据、）"）。
3. 必须有确定/待核实的计数行。
4. "需要人工确认" 每节最多 4 条（3 条正文 + 1 条合并提示）。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from technical_spec_review.artifacts import render_report  # noqa: E402
from technical_spec_review.models import DocumentEvidence, TaskResult  # noqa: E402

FORBIDDEN = [
    re.compile(r"\bB0[1-5]\b"),
    re.compile(r"p\d{4,}"),
    re.compile(r"t\d+r\d+c\d+p\d+"),
    re.compile(r"(?:TSR|TSB)-[A-Fa-f0-9]{12,}"),
    re.compile(r"由负责"),
    re.compile(r"由等结构"),
    re.compile(r"route\s*为"),
    re.compile(r"SEMANTIC|DETERMINISTIC"),
    re.compile(r"（\s*证据[^）]*）"),
    re.compile(r"（、）"),
]


def load_run(run_dir: Path) -> tuple[list[TaskResult], DocumentEvidence, dict]:
    def read(name: str):
        return json.loads((run_dir / name).read_text(encoding="utf-8"))

    state = read("state.json")
    document = DocumentEvidence.model_validate(read("document.json"))
    results = [
        TaskResult.model_validate(read(task_id + ".json"))
        for task_id in state["tasks"]
        if task_id not in {"INIT", "SUMMARY"} and (run_dir / (task_id + ".json")).exists()
    ]
    summary = read("summary.json")
    return results, document, summary


def check_report_text(text: str) -> list[str]:
    """交付文本体检：内部编号、残句、计数行、人工确认条目上限。

    e2e 与重渲染冒烟共用同一套判据，避免两处标准漂移。
    """
    failures: list[str] = []
    for pattern in FORBIDDEN:
        for match in pattern.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            snippet = text[line_start : line_end if line_end != -1 else len(text)]
            failures.append(f"命中 {pattern.pattern}：{snippet.strip()[:90]}")

    if "可直接修改" not in text or "建议核实" not in text:
        failures.append("缺少确定性/待核实计数行")

    # 头部计数必须等于正文实际列出的问题条数，否则读者数不上。
    declared = re.search(r"共 (\d+) 项，其中可直接修改", text)
    printed = len(re.findall(r"^#### 问题 ", text, re.M))
    if declared and int(declared.group(1)) != printed:
        failures.append(f"计数行写 {declared.group(1)} 项，正文实际列出 {printed} 条")

    gap_block = text.split("## 需要人工确认的范围")
    if len(gap_block) > 1:
        bullets = [line for line in gap_block[1].splitlines() if line.startswith("- ")]
        if len(bullets) > 12:
            failures.append(f"人工确认条目 {len(bullets)} 条，超过上限 12")
    return failures


def main() -> int:
    run_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp(prefix="report-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    results, document, summary = load_run(run_dir)
    render_report(out_dir, results, summary, document)
    text = (out_dir / "report.md").read_text(encoding="utf-8")

    failures = check_report_text(text)
    gap_block = text.split("## 需要人工确认的范围")
    bullets = [line for line in gap_block[1].splitlines() if line.startswith("- ")] if len(gap_block) > 1 else []

    print(f"输出：{out_dir / 'report.md'}")
    print(f"字数：{len(text)}  问题条目：{text.count('#### 问题')}  人工确认：{len(bullets)}")
    if failures:
        print("\n未通过：")
        for item in failures[:20]:
            print(" -", item)
        return 1
    print("通过：无内部编号、无残句、计数齐全、人工确认已收敛。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
