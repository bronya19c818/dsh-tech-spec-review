"""本地源文档快照。

审查在客户端本地进行，不再经过上传存储：启动审查时把待审 DOCX 复制一份到
该次审查的目录里，之后整个过程只读这份副本，所以结果可复现。原始路径也记在
state 里：重试时如果原文件已被改动，就要求新建审查，不拿旧结果糊弄。

原来的 UploadStore（上传目录、过期清理、multipart 流式写入）已整体移除：
规则在服务端，审查在客户端，文件不需要离开本机。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceDocument:
    """一次审查所依据的文档副本。"""

    path: Path
    filename: str
    sha256: str


def snapshot_source(source: Path, destination: Path) -> SourceDocument:
    """把 source 按原始字节复制到 destination，并返回副本的内容摘要。

    destination 的父目录会被创建；同名副本存在时直接覆盖（同一 review 目录
    不会复用）。先写 .part 再改名，避免中途失败留下半个文件。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        shutil.copyfile(source, temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return SourceDocument(path=destination, filename=source.name, sha256=digest)
