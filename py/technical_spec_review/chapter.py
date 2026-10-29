from __future__ import annotations

from .checks import normalize_title
from .legacy_compilers.rule_compiler import compile_rule
from .models import Chapter, DocumentEvidence, Finding, ReviewContext, Rule, TaskResult


def _normalize_evidence(
    evidence: list, blocks: dict[str, str], *, required: bool = True
) -> tuple[list[dict], list[str]]:
    if not isinstance(evidence, list) or (required and not evidence):
        raise ValueError("EVIDENCE_REQUIRED")
    normalized = []
    ids = []
    for item in evidence:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("quote"), str)
            or not item["quote"].strip()
        ):
            raise ValueError("EVIDENCE_NOT_IN_SOURCE")
        block_id = item.get("id")
        quote = item["quote"]
        if not isinstance(block_id, str) or block_id not in blocks or quote not in blocks[block_id]:
            matching_ids = [candidate_id for candidate_id, text in blocks.items() if quote in text]
            if len(matching_ids) != 1:
                raise ValueError("EVIDENCE_NOT_IN_SOURCE")
            block_id = matching_ids[0]
        normalized.append({**item, "id": block_id})
        ids.append(block_id)
    return normalized, list(dict.fromkeys(ids))


def evidence_ids(evidence: list, blocks: dict[str, str], *, required: bool = True) -> list[str]:
    return _normalize_evidence(evidence, blocks, required=required)[1]


async def review_chapter(
    document: DocumentEvidence,
    chapter: Chapter,
    context: ReviewContext,
    registry,
    model,
    *,
    audit=None,
) -> TaskResult:
    rule = await registry.get_rule(
        context.object_scope, chapter.part, chapter.number, document_role=context.document_role
    )
    result = TaskResult(
        task_id="B05-" + chapter.id, object_scope=context.object_scope, scope=chapter.evidence_ids
    )
    children = [
        c
        for c in document.chapters
        if c.part == chapter.part and c.number.startswith(chapter.number + ".")
    ]
    own_blocks = document.chapter_blocks(chapter)
    if (
        rule is None
        and children
        and own_blocks
        and all(b.is_heading and b.text.rstrip().endswith(b.title) for b in own_blocks)
    ):
        result.route = "CONTAINER"
        result.decisions = [
            {"status": "delegated", "child_tasks": ["B05-" + c.id for c in children]}
        ]
        return result
    if rule is not None and normalize_title(rule.section_title) != normalize_title(chapter.title):
        result.gaps.append("编号对应的规则标题与实际标题不同，已转入语义候选路径。")
        rule = None
    requirements = []
    if rule is not None:
        rule.verify()
        compiled = compile_rule(rule.model_dump())
        if not compiled["requirements"]:
            raise ValueError("RULE_COMPILED_REQUIREMENTS_EMPTY")
        requirements = [
            {**r, "key": f"{rule.rule_id}@{rule.version}:{r['id']}"}
            for r in compiled["requirements"]
        ]
        result.rules.append({**rule.model_dump(), "binding": "DETERMINISTIC", "source": "REGISTRY"})
        result.route = "STRUCTURED"
    else:
        result.route = "SEMANTIC"
        result.gaps.append("未绑定确定规则；候选规则及相关问题均需人工确认适用性。")
    candidates: dict[tuple[str, int], Rule] = {}
    candidate_sources: dict[tuple[str, int], str] = {}

    async def search(query: str) -> dict:
        hits = await registry.semantic_search(query, context.object_scope, context.document_role)
        accepted = []
        for hit in hits:
            candidate = Rule.model_validate(hit["rule"]).verify()
            identity = (candidate.rule_id, candidate.version)
            # Deterministic rules retain their authority when retrieved again.
            if rule and identity == (rule.rule_id, rule.version):
                continue
            candidates[identity] = candidate
            candidate_sources[identity] = hit.get("source", "SEARCH_THEN_REGISTRY")
            accepted.append({**hit, "binding": "CANDIDATE"})
        result.queries.append(
            {
                "query": query,
                "matches": [
                    {"rule_id": h["rule"]["rule_id"], "version": h["rule"]["version"]}
                    for h in accepted
                ],
            }
        )
        if audit:
            await audit(
                {"phase": "retrieval", "result": result.model_dump(), "candidates": accepted}
            )
        return {
            "candidates": accepted,
            "all_candidate_ids": [{"rule_id": key[0], "version": key[1]} for key in candidates],
        }

    blocks = document.chapter_blocks(chapter)
    if audit:
        await audit({"phase": "prepared", "result": result.model_dump()})
    try:
        response = await model.ask(
            "chapter",
            {
                "object_scope": context.object_scope,
                "chapter": chapter.model_dump(),
                "route": result.route,
                "evidence": [b.model_dump() for b in blocks],
                "deterministic_requirements": requirements,
                "deterministic_rules": result.rules,
            },
            search=search,
        )
    except RuntimeError as exc:
        if result.route != "SEMANTIC" or str(exc) != "MODEL_TOOL_ROUNDS_EXHAUSTED":
            raise
        response = {"decisions": [], "candidates": [], "coverage_notes": []}
        result.gaps.append(
            "SEMANTIC_TOOL_LOOP_EXHAUSTED：模型检索达到轮数上限，已检索候选均保留为待人工复核。"
        )
        if audit:
            await audit(
                {
                    "phase": "model_limit",
                    "error": "MODEL_TOOL_ROUNDS_EXHAUSTED",
                    "result": result.model_dump(),
                }
            )
    except ValueError as exc:
        if result.route != "SEMANTIC" or str(exc) != "MODEL_JSON_INVALID":
            raise
        response = {"decisions": [], "candidates": [], "coverage_notes": []}
        result.gaps.append(
            "SEMANTIC_RESPONSE_INVALID：模型多次未返回有效 JSON，已检索候选保留为待人工复核。"
        )
        if audit:
            await audit(
                {
                    "phase": "model_limit",
                    "error": "MODEL_JSON_INVALID",
                    "result": result.model_dump(),
                }
            )
    if audit:
        await audit({"phase": "response", "result": result.model_dump(), "response": response})
    if result.route == "SEMANTIC" and not result.queries:
        raise ValueError("SEMANTIC_SEARCH_REQUIRED")
    decisions = response.get("decisions", [])
    expected = [r["key"] for r in requirements]
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("MODEL_DECISIONS_INVALID")
    if result.route == "STRUCTURED":
        if sorted(d.get("key", "") for d in decisions) != sorted(expected):
            raise ValueError("DETERMINISTIC_COVERAGE_INVALID")
    elif decisions:
        # Semantic candidate judgements are validated from `candidates` below.
        # Do not turn unbound model decisions into deterministic findings.
        result.gaps.append(
            "SEMANTIC_DECISIONS_IGNORED：语义路径未绑定确定规则，候选判断仅按 candidates 记录。"
        )
        decisions = []
    texts = {b.id: b.text for b in blocks}
    for decision in decisions:
        status = decision.get("status")
        if status not in {
            "satisfied",
            "confirmed_issue",
            "not_applicable",
            "evidence_insufficient",
        }:
            raise ValueError("RULE_DISPOSITION_INVALID")
        if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
            raise ValueError("RULE_REASON_REQUIRED")
        normalized_evidence, ids = _normalize_evidence(
            decision.get("evidence", []), texts, required=status != "evidence_insufficient"
        )
        decision = {**decision, "evidence": normalized_evidence}
        if status == "confirmed_issue":
            result.findings.append(
                Finding(
                    code="RULE_REQUIREMENT",
                    detail=decision["reason"],
                    target=decision["key"],
                    evidence_ids=ids,
                    suggestion=decision.get("suggestion", ""),
                    rule_id=rule.rule_id if rule else "",
                    binding="DETERMINISTIC",
                )
            )
        elif status == "evidence_insufficient":
            result.gaps.append(decision["key"] + "：" + decision["reason"])
        result.decisions.append(decision)
    dispositions = response.get("candidates", [])
    if not isinstance(dispositions, list) or any(
        not isinstance(disposition, dict) for disposition in dispositions
    ):
        raise ValueError("CANDIDATE_COVERAGE_INVALID")
    supplied: set[tuple[str, int]] = set()
    for disposition in dispositions:
        rule_id = disposition.get("rule_id")
        version = disposition.get("version")
        if not isinstance(rule_id, str) or type(version) is not int:
            raise ValueError("CANDIDATE_COVERAGE_INVALID")
        identity = (rule_id, version)
        if identity not in candidates or identity in supplied:
            raise ValueError("CANDIDATE_COVERAGE_INVALID")
        supplied.add(identity)
    missing_candidates = [identity for identity in candidates if identity not in supplied]
    if missing_candidates:
        missing_ids = ", ".join(f"{rule_id}@{version}" for rule_id, version in missing_candidates)
        result.gaps.append(
            f"CANDIDATE_ASSESSMENT_MISSING：模型未处置候选规则 {missing_ids}，已保留为待人工复核。"
        )
        dispositions.extend(
            {
                "rule_id": rule_id,
                "version": version,
                "applicability": "uncertain",
                "reason": "模型未返回该候选规则的处置，已自动保留为待人工复核。",
                "findings": [],
            }
            for rule_id, version in missing_candidates
        )
    for disposition in dispositions:
        candidate = candidates[(disposition["rule_id"], disposition["version"])]
        applicability = disposition.get("applicability")
        if applicability not in {"relevant", "irrelevant", "uncertain"} or not disposition.get(
            "reason"
        ):
            raise ValueError("CANDIDATE_APPLICABILITY_INVALID")
        result.rules.append(
            {
                **candidate.model_dump(),
                "binding": "CANDIDATE",
                "source": candidate_sources[(candidate.rule_id, candidate.version)],
            }
        )
        findings = disposition.get("findings", [])
        if not isinstance(findings, list):
            raise ValueError("CANDIDATE_FINDINGS_INVALID")
        if applicability != "relevant" and findings:
            result.gaps.append(
                f"CANDIDATE_FINDINGS_IGNORED：{candidate.rule_id} 的适用性为 {applicability}，"
                "其附带问题已忽略并需人工复核。"
            )
            findings = []
        accepted_findings = []
        for finding in findings:
            if not isinstance(finding, dict) or not finding.get("detail"):
                raise ValueError("CANDIDATE_DETAIL_REQUIRED")
            try:
                normalized_evidence, ids = _normalize_evidence(finding.get("evidence", []), texts)
            except ValueError as exc:
                if str(exc) not in {"EVIDENCE_REQUIRED", "EVIDENCE_NOT_IN_SOURCE"}:
                    raise
                result.gaps.append(
                    f"CANDIDATE_FINDING_EVIDENCE_INVALID：{candidate.rule_id} 的模型问题引文"
                    "无法唯一匹配本章原文，已忽略并需人工复核。"
                )
                continue
            accepted_findings.append({**finding, "evidence": normalized_evidence})
            result.findings.append(
                Finding(
                    code="CANDIDATE_REQUIREMENT",
                    detail=finding["detail"],
                    evidence_ids=ids,
                    suggestion=finding.get("suggestion", ""),
                    rule_id=candidate.rule_id,
                    binding="CANDIDATE",
                    disposition="candidate",
                )
            )
        disposition = {**disposition, "findings": accepted_findings}
        if (
            applicability == "uncertain"
            and (candidate.rule_id, candidate.version) not in missing_candidates
        ):
            result.gaps.append(candidate.rule_id + " 适用性待确认：" + disposition["reason"])
        result.decisions.append(disposition)
    notes = response.get("coverage_notes", [])
    if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
        raise ValueError("COVERAGE_NOTES_INVALID")
    result.gaps.extend(notes)
    return result.assign_ids()
