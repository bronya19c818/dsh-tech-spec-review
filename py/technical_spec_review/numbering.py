"""Recover visible Word list numbers once for all review checks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from docx.oxml.ns import qn


def attr(element, name: str, default: str = "") -> str:
    child = element.find(qn("w:" + name)) if element is not None else None
    return child.get(qn("w:val"), default) if child is not None else default


@dataclass
class Level:
    start: int = 1
    fmt: str = "decimal"
    text: str = "%1"
    restart: int | None = None


def read_level(node) -> Level:
    restart = attr(node, "lvlRestart")
    return Level(
        int(attr(node, "start", "1")),
        attr(node, "numFmt", "decimal"),
        attr(node, "lvlText", "%1"),
        int(restart) if restart else None,
    )


def format_number(value: int, fmt: str) -> str:
    if fmt == "decimalZero":
        return f"{value:02d}"
    if fmt in {"upperLetter", "lowerLetter"} and value > 0:
        text = ""
        while value:
            value, rem = divmod(value - 1, 26)
            text = chr(65 + rem) + text
        return text if fmt == "upperLetter" else text.lower()
    if fmt in {"upperRoman", "lowerRoman"} and 0 < value < 4000:
        text = ""
        for number, symbol in [
            (1000, "M"),
            (900, "CM"),
            (500, "D"),
            (400, "CD"),
            (100, "C"),
            (90, "XC"),
            (50, "L"),
            (40, "XL"),
            (10, "X"),
            (9, "IX"),
            (5, "V"),
            (4, "IV"),
            (1, "I"),
        ]:
            while value >= number:
                value -= number
                text += symbol
        return text if fmt == "upperRoman" else text.lower()
    if (
        fmt in {"chineseCounting", "chineseCountingThousand", "ideographTraditional"}
        and 0 < value < 100
    ):
        digits = "零一二三四五六七八九"
        if value < 10:
            return digits[value]
        tens, ones = divmod(value, 10)
        return (digits[tens] if tens > 1 else "") + "十" + (digits[ones] if ones else "")
    return str(value)


class NumberingResolver:
    def __init__(self, document):
        self.instances: dict[str, dict[int, Level]] = {}
        self.counts: dict[str, dict[int, int]] = {}
        self.limitations: list[str] = []
        self.style_links: dict[str, list[tuple[str, int]]] = {}
        root = document.part.numbering_part.element
        abstracts = {}
        links = {}
        for node in root.findall(qn("w:abstractNum")):
            levels = {
                int(lvl.get(qn("w:ilvl"))): read_level(lvl) for lvl in node.findall(qn("w:lvl"))
            }
            abstracts[node.get(qn("w:abstractNumId"))] = levels
            links[node.get(qn("w:abstractNumId"))] = [
                (attr(lvl, "pStyle"), int(lvl.get(qn("w:ilvl"))))
                for lvl in node.findall(qn("w:lvl"))
                if attr(lvl, "pStyle")
            ]
        for node in root.findall(qn("w:num")):
            levels = dict(abstracts.get(attr(node, "abstractNumId"), {}))
            for override in node.findall(qn("w:lvlOverride")):
                index = int(override.get(qn("w:ilvl")))
                lvl = override.find(qn("w:lvl"))
                original = read_level(lvl) if lvl is not None else levels.get(index, Level())
                start = attr(override, "startOverride")
                levels[index] = Level(
                    int(start) if start else original.start,
                    original.fmt,
                    original.text,
                    original.restart,
                )
            self.instances[node.get(qn("w:numId"))] = levels
            for style_id, index in links.get(attr(node, "abstractNumId"), []):
                self.style_links.setdefault(style_id, []).append((node.get(qn("w:numId")), index))

    def resolve(self, paragraph) -> tuple[str, str, int]:
        num_id = None
        level = None
        properties = [paragraph._p.pPr]
        style = paragraph.style
        seen = set()
        style_ids = []
        while style is not None and style.style_id not in seen:
            seen.add(style.style_id)
            style_ids.append(style.style_id)
            properties.append(style.element.pPr)
            style = style.base_style
        for pr in properties:
            num = pr.find(qn("w:numPr")) if pr is not None else None
            if num is not None:
                num_id = num_id if num_id is not None else (attr(num, "numId") or None)
                raw_level = attr(num, "ilvl")
                if level is None and raw_level:
                    level = int(raw_level)
        if num_id is None:
            for style_id in style_ids:
                candidates = self.style_links.get(style_id, [])
                if len(candidates) == 1:
                    num_id, linked_level = candidates[0]
                    level = linked_level if level is None else level
                    break
                if candidates:
                    self.limitations.append(f"标题样式 {style_id} 对应多个编号实例，需复核。")
                    break
        if num_id is None or num_id == "0":
            return "", "", 0
        level = level or 0
        levels = self.instances.get(num_id, {})
        spec = levels.get(level)
        if spec is None:
            self.limitations.append(f"无法解析自动编号 numId={num_id}, level={level}")
            return "", "", 0
        if spec.fmt in {"bullet", "none"}:
            return "", "", 0
        supported = {
            "decimal",
            "decimalZero",
            "upperLetter",
            "lowerLetter",
            "upperRoman",
            "lowerRoman",
            "chineseCounting",
            "chineseCountingThousand",
            "ideographTraditional",
        }
        if spec.fmt not in supported:
            self.limitations.append(f"未支持编号格式：{spec.fmt}")
            return "", "", 0
        counters = self.counts.setdefault(num_id, {})
        counters[level] = counters[level] + 1 if level in counters else spec.start
        for deeper in list(counters):
            if deeper > level:
                restart = levels.get(deeper, Level()).restart
                if restart is None or restart == level + 1:
                    del counters[deeper]
        for parent in range(level):
            counters.setdefault(parent, levels.get(parent, Level()).start)
        visible = re.sub(
            r"%([1-9])",
            lambda m: format_number(
                counters.get(int(m[1]) - 1, levels.get(int(m[1]) - 1, Level()).start),
                levels.get(int(m[1]) - 1, Level()).fmt,
            ),
            spec.text,
        )
        canonical = ".".join(str(counters[i]) for i in range(level + 1))
        return visible, canonical, level + 1
