from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

ParagraphRole = Literal[
    "heading",
    "body",
    "figure_caption",
    "table_caption",
    "table_content",
    "toc",
    "front_matter",
    "other_special",
]


class Block(BaseModel):
    id: str
    text: str
    source_text: str | None = None
    paragraph_index: int | None = None
    role: ParagraphRole = "body"
    style_name: str | None = None
    outline_level: int | None = None
    numbering: str | None = None
    in_table: bool = False
    toc_field: bool = False
    page_field: bool = False
    nearby_visual: bool = False
    kind: str = "paragraph"
    part: str = "全文"
    number: str = ""
    title: str = ""
    is_heading: bool = False
    is_clause: bool = False
    display_number: str = ""
    number_source: str = ""
    level: int = 0
    format: dict[str, Any] = Field(default_factory=dict)


class Chapter(BaseModel):
    id: str
    part: str
    number: str
    title: str
    evidence_ids: list[str] = Field(default_factory=list)


class DocumentEvidence(BaseModel):
    source: str
    sha256: str
    title: str
    blocks: list[Block]
    chapters: list[Chapter]
    sections: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    def chapter_blocks(self, chapter: Chapter) -> list[Block]:
        ids = set(chapter.evidence_ids)
        return [b for b in self.blocks if b.id in ids]


class Rule(BaseModel):
    model_config = {"extra": "allow"}
    rule_id: str
    version: int = Field(ge=1)
    status: str
    content: str
    content_hash: str
    object_scope: str | None = None
    document_role: str = "采购技术规范书"
    part: str = "全文"
    section_number: str = ""
    section_title: str = ""
    rule_kind: str = "chapter"
    document_name: str = ""

    def verify(self) -> Rule:
        if self.status != "active":
            raise ValueError("RULE_NOT_ACTIVE")
        if not self.content.strip():
            raise ValueError("EMPTY_RULE_CONTENT")
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("RULE_HASH_MISMATCH")
        return self


class Finding(BaseModel):
    id: str = ""
    code: str
    detail: str
    target: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    suggestion: str = ""
    rule_id: str = ""
    binding: Literal["DETERMINISTIC", "CANDIDATE", "DOCUMENT"] = "DOCUMENT"
    disposition: Literal["confirmed", "candidate", "observation"] = "confirmed"


class TaskResult(BaseModel):
    task_id: str
    object_scope: str
    route: str = "DETERMINISTIC"
    scope: list[str] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    queries: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
    actual_structure: list[dict[str, Any]] = Field(default_factory=list)
    structure_tree: list[dict[str, Any]] = Field(default_factory=list)
    chapter_mappings: list[dict[str, Any]] = Field(default_factory=list)

    def assign_ids(self) -> TaskResult:
        for i, finding in enumerate(self.findings, 1):
            finding.id = f"{self.task_id}-F{i:04d}"
        return self


class ReviewContext(BaseModel):
    task_id: str = ""
    package_key: str = ""
    match_status: str = "UNKNOWN"
    object_scope: str
    document_role: str = "采购技术规范书"
    source_hash: str
    classification: dict[str, Any] = Field(default_factory=dict)
