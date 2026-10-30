"""结构检查汇总行为：简版框架压成少数几条，标准框架不误报。

不连规则服务、不读外部样本：结构规则在这里现造。这样 CI 里也能跑。
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py"))

from technical_spec_review.models import Chapter, DocumentEvidence, Rule  # noqa: E402
from technical_spec_review.structure import (  # noqa: E402
    check_template_structure,
    parse_structure_tree,
)

SCOPE = "净水器"
TREE = [
    ("通用部分", "1", "范围"),
    ("通用部分", "2", "规范性引用文件"),
]


def build_rule() -> Rule:
    lines = ["## 标准章节树", "", "|部分|编号|标题|内容规则文件|", "| --- | --- | --- | --- |"]
    for part, number, title in TREE:
        # 导航串里编号与标题连着写，中间不再加破折号，解析器按这个格式取标题。
        navigation = f"{SCOPE}—{part}—{number} {title}—审查规则.md"
        lines.append(f"|{part}|{number}|{title}|{navigation}|")
    content = "\n".join(lines) + "\n"
    return Rule(
        rule_id="TSR-STRUCTURE-SMOKE",
        version=1,
        status="active",
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        object_scope=SCOPE,
        part="全文",
        section_number="00",
        section_title="structure",
        rule_kind="structure",
    ).verify()


def document_with(chapters: list[tuple[str, str, str]]) -> DocumentEvidence:
    return DocumentEvidence(
        source="synthetic.docx",
        sha256="0" * 64,
        title="合成样本",
        blocks=[],
        chapters=[
            Chapter(id=f"CH{i:03d}", part=part, number=number, title=title)
            for i, (part, number, title) in enumerate(chapters, 1)
        ],
    )


def main() -> int:
    rule = build_rule()
    tree = parse_structure_tree(rule)
    assert len(tree) == len(TREE), f"结构树解析出 {len(tree)} 个节点"

    failures: list[str] = []

    # 1) 简版框架：一级章全不在树内，应汇总成两条并给出简版提示
    simplified = document_with([("全文", "1", "总则"), ("全文", "2", "规范与标准")])
    result = check_template_structure(simplified, SCOPE, rule)
    codes = [item.code for item in result.findings]
    if codes.count("STRUCTURE_CHAPTER_UNEXPECTED") != 1:
        failures.append(f"树外一级章应汇总为 1 条，实际 {codes.count('STRUCTURE_CHAPTER_UNEXPECTED')} 条")
    if codes.count("STRUCTURE_CHAPTER_MISSING") != 1:
        failures.append(f"缺一级章应汇总为 1 条，实际 {codes.count('STRUCTURE_CHAPTER_MISSING')} 条")
    if not any("简版模板" in item.detail for item in result.findings):
        failures.append("简版框架缺少简版提示")
    unexpected = next(
        (item for item in result.findings if item.code == "STRUCTURE_CHAPTER_UNEXPECTED"), None
    )
    if unexpected and "总则" not in unexpected.detail:
        failures.append("汇总条目没有列出涉及的章节")

    # 2) 标准框架：按树建造，不应报树外或缺章
    standard = document_with([(part, number, title) for part, number, title in TREE])
    clean = check_template_structure(standard, SCOPE, rule)
    clean_codes = {item.code for item in clean.findings}
    if "STRUCTURE_CHAPTER_MISSING" in clean_codes:
        failures.append("标准框架被误报缺章")
    if "STRUCTURE_CHAPTER_UNEXPECTED" in clean_codes:
        failures.append("标准框架被误报树外一级章")

    print(f"简版框架问题 {len(result.findings)} 条：{codes}")
    print(f"标准框架问题 {len(clean.findings)} 条")
    if failures:
        print("未通过：")
        for item in failures:
            print(" -", item)
        return 1
    print("通过：简版框架汇总为两条并带提示，标准框架无误报。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())