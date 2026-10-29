from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FILE_ID_PATTERN = re.compile(r"file_[a-f0-9]{32}\Z")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadedDocument:
    """Metadata for a document owned by the review service."""

    file_id: str
    status: str
    stored_path: str
    filename: str
    sha256: str
    uploaded_at: str
    expires_at: str
    path: Path

    def to_metadata(self) -> dict[str, str]:
        return {
            "file_id": self.file_id,
            "status": self.status,
            "stored_path": self.stored_path,
            "filename": self.filename,
            "sha256": self.sha256,
            "uploaded_at": self.uploaded_at,
            "expires_at": self.expires_at,
        }

    def to_public(self) -> dict[str, str]:
        return {
            "file_id": self.file_id,
            "status": self.status,
            "filename": self.filename,
            "sha256": self.sha256,
            "expires_at": self.expires_at,
        }


class _UploadWriter:
    """Bounded streaming writer used by both HTTP and local ingestion."""

    def __init__(self, store: "UploadStore", filename: str):
        self.store = store
        self.filename = store.validate_filename(filename)
        store._ensure_root()
        self.temporary = store.upload_root / f".{uuid.uuid4().hex}.part"
        self.digest = hashlib.sha256()
        self.total = 0
        store._register_partial(self.temporary)
        try:
            self.handle = self.temporary.open("xb")
        except BaseException:
            store._unregister_partial(self.temporary)
            raise
        self.finished = False

    def write(self, chunk: bytes | bytearray | memoryview) -> None:
        if self.finished:
            raise ValueError("UPLOAD_WRITE_FAILED")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ValueError("UPLOAD_WRITE_FAILED")
        data = bytes(chunk)
        self.total += len(data)
        if self.total > self.store.max_bytes:
            raise ValueError("UPLOAD_TOO_LARGE")
        self.digest.update(data)
        self.handle.write(data)

    def finish(self) -> UploadedDocument:
        if self.finished:
            raise ValueError("UPLOAD_WRITE_FAILED")
        self.finished = True
        try:
            try:
                self.handle.flush()
            finally:
                self.handle.close()
            return self.store._finalize(
                self.temporary,
                self.filename,
                self.digest.hexdigest(),
                datetime.now(UTC),
            )
        finally:
            self.store._unregister_partial(self.temporary)
            self.temporary.unlink(missing_ok=True)

    def abort(self) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            self.handle.close()
        finally:
            self.store._unregister_partial(self.temporary)
            self.temporary.unlink(missing_ok=True)


class UploadStore:
    """Persist and validate DOCX files uploaded to the review service.

    The store is independent of HTTP. The MCP server uses ``upload_stream``
    while the local CLI uses ``upload_path``; both paths produce identical
    sidecar metadata and random internal filenames.
    """

    def __init__(
        self,
        document_root: Path,
        *,
        max_bytes: int,
        ttl_seconds: int,
        runs_dir: Path | None = None,
    ):
        if max_bytes <= 0:
            raise ValueError("UPLOAD_MAX_BYTES_INVALID")
        if ttl_seconds <= 0:
            raise ValueError("UPLOAD_TTL_INVALID")
        self.document_root = document_root.resolve()
        self.upload_root = (self.document_root / ".uploads").resolve()
        if not self.upload_root.is_relative_to(self.document_root):
            raise ValueError("UPLOAD_STORAGE_INVALID")
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self.runs_dir = runs_dir.resolve() if runs_dir is not None else None
        self._active_paths: set[Path] = set()
        self._active_paths_lock = threading.Lock()

    @staticmethod
    def validate_filename(filename: str | None) -> str:
        if (
            not isinstance(filename, str)
            or not filename
            or "\x00" in filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or not filename.lower().endswith(".docx")
            or filename in {".", ".."}
        ):
            raise ValueError("UPLOAD_FILENAME_INVALID")
        return filename

    def _register_partial(self, path: Path) -> None:
        with self._active_paths_lock:
            self._active_paths.add(path)

    def _unregister_partial(self, path: Path) -> None:
        with self._active_paths_lock:
            self._active_paths.discard(path)

    def begin_upload(self, filename: str | None) -> _UploadWriter:
        if filename is None:
            raise ValueError("UPLOAD_FILENAME_INVALID")
        try:
            return _UploadWriter(self, filename)
        except ValueError:
            raise
        except OSError:
            raise ValueError("UPLOAD_WRITE_FAILED") from None

    @staticmethod
    def _validate_file_id(file_id: object) -> None:
        if not isinstance(file_id, str) or not FILE_ID_PATTERN.fullmatch(file_id):
            raise ValueError("FILE_ID_INVALID")

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError("FILE_STORAGE_INVALID") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _ensure_root(self) -> None:
        try:
            self.upload_root.mkdir(parents=True, exist_ok=True)
            if not self.upload_root.resolve().is_relative_to(self.document_root):
                raise ValueError("UPLOAD_STORAGE_INVALID")
        except ValueError:
            raise
        except OSError:
            raise ValueError("UPLOAD_WRITE_FAILED") from None

    @staticmethod
    def _validate_docx(path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise ValueError("UPLOAD_NOT_DOCX")
                if archive.testzip() is not None:
                    raise ValueError("UPLOAD_NOT_DOCX")
        except ValueError:
            raise
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise ValueError("UPLOAD_NOT_DOCX") from None

    def _new_metadata(
        self, file_id: str, target: Path, filename: str, digest: str, uploaded_at: datetime
    ) -> UploadedDocument:
        return UploadedDocument(
            file_id=file_id,
            status="READY",
            stored_path=target.relative_to(self.document_root).as_posix(),
            filename=filename,
            sha256=digest,
            uploaded_at=uploaded_at.isoformat(),
            expires_at=(uploaded_at + timedelta(seconds=self.ttl_seconds)).isoformat(),
            path=target,
        )

    def _write_metadata(self, document: UploadedDocument) -> None:
        metadata_path = self.upload_root / f"{document.file_id}.json"
        temporary = self.upload_root / f".{document.file_id}.json.part"
        self._register_partial(temporary)
        try:
            temporary.write_text(
                json.dumps(document.to_metadata(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(metadata_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise ValueError("UPLOAD_WRITE_FAILED") from None
        finally:
            self._unregister_partial(temporary)

    def _finalize(
        self, temporary: Path, filename: str, digest: str, uploaded_at: datetime
    ) -> UploadedDocument:
        file_id = f"file_{uuid.uuid4().hex}"
        target = self.upload_root / f"{uuid.uuid4().hex}.docx"
        self._register_partial(target)
        try:
            self._validate_docx(temporary)
            temporary.replace(target)
            document = self._new_metadata(file_id, target, filename, digest, uploaded_at)
            self._write_metadata(document)
            return document
        except ValueError:
            target.unlink(missing_ok=True)
            raise
        except OSError:
            target.unlink(missing_ok=True)
            raise ValueError("UPLOAD_WRITE_FAILED") from None
        finally:
            self._unregister_partial(target)
            temporary.unlink(missing_ok=True)

    def upload_path(self, source: Path) -> UploadedDocument:
        """Copy a local DOCX through the same storage and validation path."""
        path = source.expanduser().resolve()
        if not path.is_relative_to(self.document_root):
            raise ValueError("DOCUMENT_OUTSIDE_ROOT")
        if path.suffix.lower() != ".docx" or not path.is_file():
            raise ValueError("DOCX_FILE_REQUIRED")
        filename = self.validate_filename(path.name)
        try:
            size = path.stat().st_size
        except OSError:
            raise ValueError("UPLOAD_WRITE_FAILED") from None
        if size > self.max_bytes:
            raise ValueError("UPLOAD_TOO_LARGE")
        writer = self.begin_upload(filename)
        try:
            with path.open("rb") as source_file:
                while chunk := source_file.read(1024 * 1024):
                    writer.write(chunk)
            return writer.finish()
        except ValueError:
            writer.abort()
            raise
        except OSError:
            writer.abort()
            raise ValueError("UPLOAD_WRITE_FAILED") from None
        finally:
            writer.abort()

    async def upload_stream(
        self, filename: str | None, stream: Any, *, content_length: int | None = None
    ) -> UploadedDocument:
        """Read an async or sync file-like stream without buffering the whole upload."""
        if content_length is not None and content_length > self.max_bytes:
            raise ValueError("UPLOAD_TOO_LARGE")
        writer = self.begin_upload(filename)
        try:
            while True:
                chunk = stream.read(1024 * 1024)
                if inspect.isawaitable(chunk):
                    chunk = await chunk
                if not chunk:
                    break
                writer.write(chunk)
            return writer.finish()
        except ValueError:
            writer.abort()
            raise
        except OSError:
            writer.abort()
            raise ValueError("UPLOAD_WRITE_FAILED") from None
        finally:
            writer.abort()

    def get(self, file_id: str, *, verify_hash: bool = True) -> UploadedDocument:
        """Read one upload by exact file id and validate its current lifecycle state."""
        self._validate_file_id(file_id)
        metadata_path = self.upload_root / f"{file_id}.json"
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("FILE_NOT_FOUND") from None
        except (OSError, json.JSONDecodeError, TypeError):
            raise ValueError("FILE_STORAGE_INVALID") from None
        try:
            status = raw["status"]
            stored_path = raw["stored_path"]
            filename = raw["filename"]
            digest = raw["sha256"]
            uploaded_at = raw["uploaded_at"]
            expires_at = raw["expires_at"]
            if not all(
                isinstance(value, str)
                for value in (status, stored_path, filename, digest, uploaded_at, expires_at)
            ):
                raise ValueError("FILE_STORAGE_INVALID")
            self.validate_filename(filename)
            if status != "READY" or raw["file_id"] != file_id:
                raise ValueError("FILE_NOT_READY")
            expiry = self._parse_time(expires_at)
            if expiry <= datetime.now(UTC):
                raise ValueError("FILE_EXPIRED")
            path = (self.document_root / stored_path).resolve()
            if (
                Path(stored_path).is_absolute()
                or not path.is_relative_to(self.upload_root)
                or path.suffix.lower() != ".docx"
            ):
                raise ValueError("FILE_STORAGE_INVALID")
            if not path.is_file():
                raise ValueError("FILE_NOT_FOUND")
            document = UploadedDocument(
                file_id=file_id,
                status=status,
                stored_path=Path(stored_path).as_posix(),
                filename=filename,
                sha256=digest,
                uploaded_at=self._parse_time(uploaded_at).isoformat(),
                expires_at=expiry.isoformat(),
                path=path,
            )
        except (KeyError, TypeError):
            raise ValueError("FILE_STORAGE_INVALID") from None
        if verify_hash:
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                raise ValueError("FILE_NOT_FOUND") from None
            if actual != document.sha256:
                raise ValueError("FILE_HASH_MISMATCH")
        return document

    def _active_file_ids_from_runs(self, runs_dir: Path | None = None) -> set[str] | None:
        roots = dict.fromkeys(path for path in (runs_dir, self.runs_dir) if path is not None)
        active: set[str] = set()
        for state_path in (
            path for root in roots if root.exists() for path in root.glob("*/state.json")
        ):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or state.get("status") not in {
                    "PENDING",
                    "RUNNING",
                    "COMPLETED",
                    "FAILED",
                }:
                    raise ValueError("REVIEW_STATE_INVALID")
                file_id = state.get("file_id")
                if state["status"] in {"PENDING", "RUNNING"}:
                    self._validate_file_id(file_id)
            except (OSError, TypeError, ValueError):
                # Unknown references must never be interpreted as no references.
                review_id = state_path.parent.name
                logger.warning(
                    "UPLOAD_CLEANUP_SKIPPED:REVIEW_STATE_INVALID review_id=%s",
                    review_id if re.fullmatch(r"[a-f0-9]{32}", review_id) else "UNKNOWN",
                )
                return None
            if isinstance(file_id, str) and state.get("status") in {"PENDING", "RUNNING"}:
                active.add(file_id)
        return active

    def cleanup_expired(
        self,
        active_file_ids: set[str] | frozenset[str] = frozenset(),
        *,
        runs_dir: Path | None = None,
    ) -> int:
        """Clean owned uploads only when all reference metadata is readable."""
        if not self.upload_root.exists():
            return 0
        run_references = self._active_file_ids_from_runs(runs_dir)
        if run_references is None:
            return 0
        active_file_ids = set(active_file_ids) | run_references
        records: list[tuple[Path, dict]] = []
        referenced_paths: set[Path] = set()
        # Validate the complete inventory before any deletion, including orphan cleanup.
        for metadata_path in self.upload_root.glob("file_*.json"):
            try:
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("FILE_STORAGE_INVALID")
                self._validate_file_id(raw.get("file_id"))
                stored = raw["stored_path"]
                if not isinstance(stored, str):
                    raise ValueError("FILE_STORAGE_INVALID")
                path = (self.document_root / stored).resolve()
                if (
                    raw["file_id"] != metadata_path.stem
                    or raw.get("status") != "READY"
                    or Path(stored).is_absolute()
                    or not path.is_relative_to(self.upload_root)
                    or path.suffix.lower() != ".docx"
                ):
                    raise ValueError("FILE_STORAGE_INVALID")
                self._parse_time(raw["expires_at"])
                self._parse_time(raw["uploaded_at"])
                self.validate_filename(raw["filename"])
                digest = raw["sha256"]
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("FILE_STORAGE_INVALID")
                records.append((metadata_path, raw))
                referenced_paths.add(path)
            except (KeyError, OSError, TypeError, ValueError):
                metadata_id = metadata_path.stem
                logger.warning(
                    "UPLOAD_CLEANUP_SKIPPED:FILE_STORAGE_INVALID file_id=%s",
                    metadata_id if FILE_ID_PATTERN.fullmatch(metadata_id) else "UNKNOWN",
                )
                return 0
        removed = 0
        for partial in self.upload_root.glob("*.part"):
            with self._active_paths_lock:
                if partial in self._active_paths:
                    continue
                partial.unlink(missing_ok=True)
        for metadata_path, raw in records:
            try:
                file_id = raw.get("file_id")
                stored = raw.get("stored_path")
                if isinstance(stored, str):
                    candidate_path = (self.document_root / stored).resolve()
                    if candidate_path.is_relative_to(self.upload_root):
                        referenced_paths.add(candidate_path)
                if file_id in active_file_ids:
                    continue
                if raw.get("status") == "READY" and self._parse_time(
                    raw["expires_at"]
                ) > datetime.now(UTC):
                    continue
                with self._active_paths_lock:
                    if isinstance(stored, str):
                        path = (self.document_root / stored).resolve()
                        if path in self._active_paths:
                            continue
                        if path.is_relative_to(self.upload_root):
                            path.unlink(missing_ok=True)
                    metadata_path.unlink(missing_ok=True)
                    removed += 1
            except (KeyError, OSError, TypeError, ValueError):
                logger.warning(
                    "UPLOAD_CLEANUP_SKIPPED:FILE_STORAGE_INVALID file_id=%s", metadata_path.stem
                )
        for document_path in self.upload_root.glob("*.docx"):
            resolved = document_path.resolve()
            with self._active_paths_lock:
                if resolved in referenced_paths or resolved in self._active_paths:
                    continue
                try:
                    document_path.unlink()
                except OSError:
                    continue
        return removed
