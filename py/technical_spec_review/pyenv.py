"""插件自带 Python 环境的准备与解析。

为什么不直接装进宿主的 primary-runtime：那是宿主托管的载荷，升级时整树替换，
用户装的包会被抹掉；Windows 上进程占着文件还会挡住升级。这里在插件数据目录下
建独立 venv，用宿主自带的解释器做 base（版本与 pgserver 的 cp312 wheel 匹配），
只把依赖装进这个 venv，插件与宿主互不影响。

首次运行需要联网拉 wheel；之后完全离线。可用 TSR_PYTHON 指定现成解释器跳过准备。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MARKER = ".deps-ready"
REQUIREMENTS = [
    "python-docx>=1.1,<2",
    "httpx>=0.27,<1",
    "pydantic>=2.8,<3",
    "mcp>=1.12,<2",
    "uvicorn>=0.30,<1",
    "python-dotenv>=1.0,<2",
]


def host_python() -> str | None:
    """宿主 primary-runtime 里的解释器；找不到返回 None。"""
    home = Path(os.getenv("DSH_HOME") or Path.home() / ".dsh")
    candidate = home / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "python" / (
        "python.exe" if os.name == "nt" else "bin/python3"
    )
    return str(candidate) if candidate.exists() else None


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(args: list[str], timeout: float = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def ensure_environment(data_dir: Path, *, log=print) -> Path | None:
    """确保插件 venv 存在且依赖就绪，返回其解释器路径。

    失败返回 None（调用方回退到当前解释器），不抛异常：环境准备不该让插件加载失败。
    """
    override = os.getenv("TSR_PYTHON")
    if override and Path(override).exists():
        log(f"[env] using TSR_PYTHON={override}")
        return Path(override)

    venv_dir = data_dir / "pyenv"
    python = venv_python(venv_dir)
    marker = venv_dir / MARKER

    if python.exists() and marker.exists():
        return python

    base = host_python() or sys.executable
    if not python.exists():
        log(f"[env] creating venv at {venv_dir} (base={base})")
        result = _run([base, "-m", "venv", str(venv_dir)])
        if result.returncode != 0 or not python.exists():
            log(f"[env] venv creation failed: {(result.stderr or result.stdout)[-400:]}")
            return None

    log("[env] installing dependencies (first run, needs network)")
    result = _run([str(python), "-m", "pip", "install", "--disable-pip-version-check", *REQUIREMENTS])
    if result.returncode != 0:
        log(f"[env] dependency install failed: {(result.stderr or result.stdout)[-600:]}")
        return None

    marker.write_text(
        json.dumps({"requirements": REQUIREMENTS, "base": base}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log("[env] environment ready")
    return python


def environment_status(data_dir: Path) -> dict:
    """给设置页/自检用的环境状态。"""
    venv_dir = data_dir / "pyenv"
    python = venv_python(venv_dir)
    return {
        "ready": python.exists() and (venv_dir / MARKER).exists(),
        "venv": str(venv_dir),
        "python": str(python) if python.exists() else "",
        "hostPython": host_python() or "",
        "override": os.getenv("TSR_PYTHON", ""),
    }
