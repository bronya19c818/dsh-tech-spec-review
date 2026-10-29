from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import suppress
from pathlib import Path

from .artifacts import (
    atomic_text,
    normalize_summary_issue_count,
    read_json,
    render_report,
    repair_summary_coverage,
    reportable_findings,
    summary_process_files,
    write_json,
    write_result,
)
from .chapter import review_chapter
from .checks import check_layout, check_numbering_sequence, check_references, check_structure
from .ingress import UploadedDocument, UploadStore
from .language import review_language
from .models import DocumentEvidence, ReviewContext, Rule, TaskResult
from .parser import parse_docx
from .state import Lease, now, safe_error
from .structure import content_chapter_ids, parse_structure_tree


class ReviewService:
    def __init__(
        self,
        runs: Path,
        document_root: Path,
        registry,
        model,
        *,
        concurrency: int = 4,
        b02_concurrency: int | None = None,
        b02_batch: int = 8,
        upload_store: UploadStore | None = None,
    ):
        sentence_concurrency = concurrency if b02_concurrency is None else b02_concurrency
        if not 1 <= concurrency <= 32 or not 1 <= sentence_concurrency <= 32:
            raise ValueError("CONCURRENCY_OUT_OF_RANGE")
        if not 1 <= b02_batch <= 32:
            raise ValueError("BATCH_SIZE_OUT_OF_RANGE")
        self.runs, self.document_root = runs.resolve(), document_root.resolve()
        self.registry, self.model, self.concurrency = registry, model, concurrency
        self.b02_concurrency = sentence_concurrency
        self.b02_batch = b02_batch
        self.upload_store = upload_store
        self.active: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def directory(self, review_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", review_id):
            raise ValueError("REVIEW_ID_INVALID")
        path = (self.runs / review_id).resolve()
        if not path.is_relative_to(self.runs):
            raise ValueError("REVIEW_PATH_OUTSIDE_ROOT")
        return path

    def document_path(self, source: str) -> Path:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = self.document_root / path
        path = path.resolve()
        if not path.is_relative_to(self.document_root):
            raise ValueError("DOCUMENT_OUTSIDE_ROOT")
        if path.suffix.lower() != ".docx" or not path.is_file():
            raise ValueError("DOCX_FILE_REQUIRED")
        return path

    async def _update(self, rid: str, task_id: str | None = None, **values):
        async with self.locks.setdefault(rid, asyncio.Lock()):
            path = self.directory(rid) / "state.json"
            state = await read_json(path)
            target = state["tasks"][task_id] if task_id else state
            target.update(values)
            target["updated_at"] = now()
            state["updated_at"] = now()
            await write_json(path, state)

    async def start(
        self,
        file_id: str,
        object_scope: str | None = None,
        document_role: str = "采购技术规范书",
    ) -> str:
        if self.upload_store is None:
            raise ValueError("UPLOAD_STORAGE_UNAVAILABLE")
        uploaded = await asyncio.to_thread(self.upload_store.get, file_id)
        await self.cleanup_uploads({file_id})
        # Keep the metadata long enough to report FILE_EXPIRED rather than
        # turning a race with cleanup into FILE_NOT_FOUND.
        uploaded = await asyncio.to_thread(self.upload_store.get, file_id)
        rid = uuid.uuid4().hex
        directory = self.directory(rid)
        lease = await asyncio.to_thread(Lease, directory / ".lease")
        try:
            state = {
                "review_id": rid,
                "status": "PENDING",
                "file_id": uploaded.file_id,
                "source": uploaded.stored_path,
                "source_name": uploaded.filename,
                "source_hash": uploaded.sha256,
                "file_expires_at": uploaded.expires_at,
                "requested_object": object_scope,
                "document_role": document_role,
                "created_at": now(),
                "updated_at": now(),
                "heartbeat": now(),
                "tasks": {"INIT": {"status": "PENDING", "progress": [0, 1]}},
            }
            await write_json(directory / "state.json", state)
            self._launch(rid, lease)
        except BaseException:
            lease.close()
            raise
        return rid

    async def upload_local(self, source: str | Path) -> UploadedDocument:
        if self.upload_store is None:
            raise ValueError("UPLOAD_STORAGE_UNAVAILABLE")
        path = await asyncio.to_thread(self.document_path, str(source))
        return await asyncio.to_thread(self.upload_store.upload_path, path)

    async def cleanup_uploads(self, preserve_file_ids: set[str] | None = None) -> int:
        if self.upload_store is None:
            return 0
        return await asyncio.to_thread(
            self.upload_store.cleanup_expired, set(preserve_file_ids or set()), runs_dir=self.runs
        )

    async def wait(self, rid: str):
        if rid in self.active:
            await self.active[rid]

    def _launch(self, rid: str, lease: Lease):
        task = asyncio.create_task(self._run(rid, lease))
        self.active[rid] = task

        def finished(completed):
            # Cancellation can occur before _run executes its first line.
            lease.close()
            if self.active.get(rid) is completed:
                self.active.pop(rid, None)

        task.add_done_callback(finished)

    async def _interrupt(self, rid: str, error: str):
        state = await read_json(self.directory(rid) / "state.json")
        for task_id, task in state["tasks"].items():
            if task["status"] in {"RUNNING", "PENDING"}:
                await self._update(rid, task_id, status="FAILED", error=error)
        await self._update(rid, status="FAILED", error=error)

    async def status(self, rid: str) -> dict:
        directory = self.directory(rid)
        state = await read_json(directory / "state.json")
        if state["status"] in {"RUNNING", "PENDING"} and rid not in self.active:
            try:
                lease = await asyncio.to_thread(Lease, directory / ".lease")
            except RuntimeError:
                return state
            try:
                # Re-read under lease: another process may have completed between read and lock.
                state = await read_json(directory / "state.json")
                if state["status"] in {"RUNNING", "PENDING"}:
                    await self._interrupt(rid, "PROCESS_INTERRUPTED")
                    state = await read_json(directory / "state.json")
            finally:
                lease.close()
        return state

    async def retry(self, rid: str) -> str:
        directory = self.directory(rid)
        lease = await asyncio.to_thread(Lease, directory / ".lease")
        try:
            state = await read_json(directory / "state.json")
            if state["status"] == "COMPLETED":
                raise ValueError("REVIEW_ALREADY_COMPLETED")
            file_id = state.get("file_id")
            if not file_id:
                raise ValueError("LEGACY_REVIEW_NO_FILE_ID")
            if self.upload_store is None:
                raise ValueError("UPLOAD_STORAGE_UNAVAILABLE")
            try:
                await asyncio.to_thread(self.upload_store.get, file_id)
            except ValueError as exc:
                if str(exc) == "FILE_HASH_MISMATCH":
                    raise ValueError("SOURCE_CHANGED_START_NEW_REVIEW") from None
                raise
            for task_id, task in state["tasks"].items():
                if task["status"] != "COMPLETED":
                    await self._update(rid, task_id, status="PENDING", error=None)
            await self._update(rid, status="PENDING", error=None)
            self._launch(rid, lease)
        except BaseException:
            lease.close()
            raise
        return rid

    async def results(self, rid: str) -> dict:
        state = await self.status(rid)
        directory = self.directory(rid)
        results = []
        for task_id, task in state["tasks"].items():
            if task["status"] == "COMPLETED" and task_id not in {"INIT", "SUMMARY"}:
                results.append(await read_json(directory / (task_id + ".json")))
        return {
            "state": state,
            "tasks": results,
            "report": str(directory / "report.docx") if state["status"] == "COMPLETED" else None,
        }

    async def _heartbeat(self, rid: str):
        while True:
            await self._update(rid, heartbeat=now())
            await asyncio.sleep(5)

    async def _initialize(self, rid: str):
        directory = self.directory(rid)
        state = await read_json(directory / "state.json")
        if state["tasks"]["INIT"]["status"] == "COMPLETED":
            return (
                DocumentEvidence.model_validate(await read_json(directory / "document.json")),
                ReviewContext.model_validate(await read_json(directory / "context.json")),
            )
        await self._update(rid, "INIT", status="RUNNING")
        if self.upload_store is None:
            raise ValueError("UPLOAD_STORAGE_UNAVAILABLE")
        uploaded = await asyncio.to_thread(self.upload_store.get, state.get("file_id", ""))
        path = uploaded.path
        if state.get("source_hash") and state["source_hash"] != uploaded.sha256:
            raise ValueError("SOURCE_CHANGED_START_NEW_REVIEW")
        document = await asyncio.to_thread(parse_docx, path)
        if not document.blocks:
            raise ValueError("EMPTY_DOCUMENT")
        catalog = await self.registry.list_packages(state["document_role"])
        allowed = {row["object_scope"] for row in catalog} | {"UNKNOWN"}
        if state["requested_object"] is not None:
            classification = {"object_scope": state["requested_object"], "reason": "用户指定"}
        else:
            classification = await self.model.ask(
                "classify",
                {
                    "title": document.title,
                    "opening": "\n".join(b.text for b in document.blocks[:15])[:6000],
                    "catalog": catalog,
                },
            )
        selected = classification.get("object_scope")
        if selected not in allowed:
            aliases = {row["object_scope"] for row in catalog if selected in row.get("aliases", [])}
            if len(aliases) == 1:
                classification["object_scope"] = aliases.pop()
        if classification.get("object_scope") not in allowed:
            raise ValueError("OBJECT_NOT_IN_CATALOG")
        packages = [
            row["package_key"]
            for row in catalog
            if row["object_scope"] == classification["object_scope"] and "package_key" in row
        ]
        context = ReviewContext(
            task_id=rid,
            package_key=packages[0] if len(packages) == 1 else "",
            match_status="UNKNOWN" if classification["object_scope"] == "UNKNOWN" else "MATCHED",
            object_scope=classification["object_scope"],
            document_role=state["document_role"],
            source_hash=document.sha256,
            classification=classification,
        )
        await write_json(directory / "document.json", document.model_dump())
        await write_json(directory / "context.json", context.model_dump())
        content_ids = [c.id for c in document.chapters]
        try:
            structure_rule = await self._structure_rule(rid, context)
            content_ids = content_chapter_ids(document, structure_rule)
        except Exception as exc:
            # Keep independent tasks runnable. B03 retries the read and fails explicitly
            # if unavailable/invalid; never interpret a broken tree as zero content tasks.
            await write_json(
                directory / "structure_planning_error.json", {"error": safe_error(exc)}
            )
        tasks = {"INIT": {"status": "COMPLETED", "progress": [1, 1]}}
        for task_id in [
            "B01",
            "B02",
            "B03",
            "B04",
            *["B05-" + chapter_id for chapter_id in content_ids],
            "SUMMARY",
        ]:
            tasks[task_id] = {"status": "PENDING", "progress": [0, 1]}
        await self._update(
            rid,
            tasks=tasks,
            object_scope=context.object_scope,
            source_hash=document.sha256,
        )
        return document, context

    async def _run(self, rid: str, lease: Lease):
        heartbeat = asyncio.create_task(self._heartbeat(rid))
        try:
            await self._update(rid, status="RUNNING", error=None)
            document, context = await self._initialize(rid)
            directory = self.directory(rid)
            tasks = (await read_json(directory / "state.json"))["tasks"]
            queue: asyncio.Queue = asyncio.Queue()
            for task_id, task in tasks.items():
                if task_id not in {"INIT", "SUMMARY"} and task["status"] != "COMPLETED":
                    queue.put_nowait(task_id)

            async def worker():
                while not queue.empty():
                    task_id = queue.get_nowait()
                    await self._update(rid, task_id, status="RUNNING")
                    attempt = 0
                    try:
                        while True:
                            attempt += 1
                            try:
                                result = await self._execute(rid, task_id, document, context)
                                await write_result(directory, result)
                                await asyncio.to_thread(
                                    (directory / f"{task_id}.failure.json").unlink,
                                    missing_ok=True,
                                )
                                units = result.stats.get("total", 1)
                                await self._update(
                                    rid, task_id, status="COMPLETED", progress=[units, units]
                                )
                                break
                            except Exception as exc:
                                error = safe_error(exc)
                                # 一次自动重试：只针对已知可恢复的模型依从/传输类错误。
                                # 语义硬门控本身不改；重试仍失败则如实标记失败。
                                if attempt == 1 and self._retryable(error):
                                    await asyncio.to_thread(
                                        atomic_text,
                                        directory / (task_id + ".md"),
                                        f"# {task_id}\n状态：AUTO_RETRY\n"
                                        f"首次失败：{error}\n"
                                        "重试原因：模型依从/传输类错误，自动重试一次。\n",
                                    )
                                    await self._update(
                                        rid, task_id, status="RUNNING", error=None, progress=[0, 1]
                                    )
                                    continue
                                await write_json(
                                    directory / (task_id + ".failure.json"),
                                    {"status": "FAILED", "error": error, "attempts": attempt},
                                )
                                await asyncio.to_thread(
                                    atomic_text,
                                    directory / (task_id + ".md"),
                                    f"# {task_id}\n状态：FAILED\n错误：{error}\n"
                                    f"尝试次数：{attempt}\n",
                                )
                                await self._update(rid, task_id, status="FAILED", error=error)
                                break
                    finally:
                        queue.task_done()

            async with asyncio.TaskGroup() as group:
                for _ in range(self.concurrency):
                    group.create_task(worker())
            state = await read_json(directory / "state.json")
            if any(
                t["status"] != "COMPLETED" for key, t in state["tasks"].items() if key != "SUMMARY"
            ):
                await self._update(rid, "SUMMARY", status="FAILED", error="DEPENDENCY_FAILED")
                await self._update(rid, status="FAILED", error="REVIEW_TASK_FAILED")
                return
            await self._update(rid, "SUMMARY", status="RUNNING")
            results = [
                TaskResult.model_validate(await read_json(directory / (key + ".json")))
                for key in state["tasks"]
                if key not in {"INIT", "SUMMARY"}
            ]
            issue_ids = [finding.id for _, finding in reportable_findings(results, document)]
            summary = await self.model.ask(
                "summary",
                {
                    "process_files": summary_process_files(results, document),
                    "issue_ids": issue_ids,
                    "issue_count": len(issue_ids),
                },
            )
            summary = normalize_summary_issue_count(summary, issue_ids)
            summary = repair_summary_coverage(summary, issue_ids)
            await asyncio.to_thread(render_report, directory, results, summary, document)
            await write_json(directory / "summary.json", summary)
            await self._update(rid, "SUMMARY", status="COMPLETED", progress=[1, 1])
            await self._update(rid, status="COMPLETED")
        except asyncio.CancelledError:
            await self._interrupt(rid, "PROCESS_INTERRUPTED")
            raise
        except Exception as exc:
            await self._interrupt(rid, safe_error(exc))
        finally:
            heartbeat.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await heartbeat
            finally:
                lease.close()
                self.active.pop(rid, None)

    async def _structure_rule(self, rid: str, context: ReviewContext) -> Rule | None:
        path = self.directory(rid) / "structure_rule.json"
        if await asyncio.to_thread(path.exists):
            payload = await read_json(path)
            rule = Rule.model_validate(payload["rule"]).verify() if payload["rule"] else None
        else:
            rule = await self.registry.get_rule(
                context.object_scope, "全文", "00", "structure", context.document_role
            )
        if rule is not None:
            rule.verify()
            if (
                rule.object_scope,
                rule.document_role,
                rule.part,
                rule.section_number,
                rule.rule_kind,
            ) != (context.object_scope, context.document_role, "全文", "00", "structure"):
                raise ValueError("REGISTRY_IDENTITY_MISMATCH")
        if not await asyncio.to_thread(path.exists) and await asyncio.to_thread(path.parent.exists):
            # Snapshot only valid trees. Explicit retry may recover a corrected published rule.
            if rule is not None:
                parse_structure_tree(rule)
            await write_json(path, {"rule": rule.model_dump() if rule else None})
        return rule

    async def _execute(
        self, rid: str, task_id: str, document: DocumentEvidence, context: ReviewContext
    ) -> TaskResult:
        scope, role = context.object_scope, context.document_role
        if task_id == "B01":
            rules = await self.registry.basic_rules("layout", scope, role)
            result = await asyncio.to_thread(check_layout, document, scope, rules)
            result.gaps.extend(document.limitations)
            return result
        if task_id == "B02":
            rules = await self.registry.basic_rules("text_quality", scope, role)

            async def progress(completed, total):
                await self._update(rid, "B02", progress=[completed, total])

            return await review_language(
                document,
                scope,
                rules,
                self.model,
                self.directory(rid),
                workers=self.b02_concurrency,
                batch_size=self.b02_batch,
                progress=progress,
            )
        if task_id == "B03":
            rule = await self._structure_rule(rid, context)
            result = await asyncio.to_thread(check_structure, document, scope, rule)
            findings = await asyncio.to_thread(check_numbering_sequence, document)
            result.findings.extend(findings)
            return result.assign_ids()
        if task_id == "B04":
            return await asyncio.to_thread(check_references, document, scope)
        chapter = next(c for c in document.chapters if "B05-" + c.id == task_id)

        async def audit(value):
            path = self.directory(rid) / (task_id + ".audit.json")
            if await asyncio.to_thread(path.exists):
                previous = await read_json(path)
                events = previous if isinstance(previous, list) else [previous]
            else:
                events = []
            events.append(value)
            await write_json(path, events)

        return await review_chapter(
            document, chapter, context, self.registry, self.model, audit=audit
        )

    @staticmethod
    def _retryable(error: str) -> bool:
        """判断任务失败是否属于"重试一次大概率能过"的类型。

        只收纳模型依从性与传输类错误：模型偶发返回非 JSON、未按要求发起检索、
        工具轮次用尽、网络/限流。规则缺失、身份不一致、校验不通过等确定性错误
        一律不重试，避免把真实缺陷重试成假绿。
        """
        if error in {
            "SEMANTIC_SEARCH_REQUIRED",
            "MODEL_JSON_INVALID",
            "MODEL_TOOL_ROUNDS_EXHAUSTED",
            "MODEL_TRANSPORT_ERROR",
            "MODEL_RETRY_EXHAUSTED",
            # 章节审查里模型没按约定给出合法候选处置（漏 applicability、枚举值不对）。
            # 属输出依从性问题，重问一次通常就能过；规则缺失、身份不一致等确定性错误不在此列。
            "CANDIDATE_APPLICABILITY_INVALID",
            "MODEL_DECISIONS_INVALID",
            "CHAPTER_RESPONSE_INVALID",
        }:
            return True
        if error.startswith("MODEL_HTTP_"):
            code = error.removeprefix("MODEL_HTTP_")
            return code.startswith("429") or code.startswith("5")
        return False

    async def close(self):
        ids = list(self.active)
        tasks = list(self.active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for rid in ids:
            await self.status(rid)
