"""端到端验证：生成小样本 DOCX → 跑完整审查 → 校验过程文件与报告。

用生成的小样本而不是真实规范书：真实样本 84 句要走 84 次模型调用（约 9 分钟），
不适合做回归测试。这里只保留能触发各类检查的最小结构，跑一次约 1–2 分钟。

需要规则服务在跑，以及可用的对话模型（读 keys.env 或环境变量）；缺任一项即跳过。

用法：
    python tests/e2e_review.py            # 跑一次，产物写到 artifacts/
    python tests/e2e_review.py --keep     # 保留临时目录
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"

sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT / "tests"))

try:
    from report_render_smoke import check_report_text  # noqa: E402
except ImportError:  # 报告体检脚本没跟着提交时，记为一步失败，不连累其余检查

    def check_report_text(text: str) -> list[str]:
        return ["缺少 tests/report_render_smoke.py，无法校验报告文本"]

# 小样本：一个一级章 + 两个二级章，故意混入样式不一致与编号跳号
CHAPTERS = [
    ("1 范围", [
        "本文件规定了净水器采购的技术要求、试验方法与验收规则。",
        "本文件适用于办公区、食堂等场所使用的净水器类产品采购。",
    ]),
    ("2 技术要求", [
        "净水器的滤芯使用寿命不应低于5000小时，并应提供更换提示。",
        "设备应具备断水保护和过载保护功能，运行噪声不应影响正常办公。",
        "外壳材质应符合饮用水接触材料的安全要求，表面应平整光洁。",
    ]),
    ("4 试验方法", [
        "型式试验应覆盖出水水质、产水量与电气安全项目。",
        "出厂试验应逐台进行，并保留检验记录。",
    ]),
]


def build_docx(path: Path) -> None:
    from docx import Document
    from docx.shared import Pt

    document = Document()
    document.add_heading("净水器采购技术规范书", level=0)
    for title, paragraphs in CHAPTERS:
        document.add_heading(title, level=1)
        for index, text in enumerate(paragraphs):
            paragraph = document.add_paragraph(text)
            # 第二段故意用不同字号，触发版面样式不一致
            if index == 1:
                paragraph.runs[0].font.size = Pt(12)
    document.save(str(path))


def load_env() -> None:
    """凭据与配置来源：插件目录的 keys.env（开发）+ 界面保存的 settings.json。"""
    for name in ("keys.env", ".env"):
        env_file = ROOT / name
        if not env_file.exists():
            continue
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip()

    home = Path(os.getenv("DSH_HOME") or Path.home() / ".dsh")
    settings_file = home / "technical-spec-review" / "settings.json"
    if not settings_file.exists():
        return
    try:
        stored = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    mapping = {
        "chatBaseUrl": "TSR_MODEL_BASE_URL",
        "chatModel": "TSR_MODEL",
        "rerankBaseUrl": "TSR_RERANK_BASE_URL",
        "rerankModel": "TSR_RERANK_MODEL",
        "rerankThreshold": "TSR_RERANK_THRESHOLD",
        "rulesBaseUrl": "TSR_REGISTRY_URL",
        "gateBaseUrl": "TSR_GATE_BASE_URL",
        "gateThreshold": "TSR_GATE_THRESHOLD",
    }
    for key, name in mapping.items():
        value = stored.get(key)
        if value not in (None, "") and name not in os.environ:
            os.environ[name] = str(value)

    # 密钥与插件一样从 DSH 凭据库取，避免测试依赖开发用的 keys.env
    refs = {
        "TSR_CHAT_API_KEY": "TSR_MODEL_API_KEY",
        "TSR_RERANK_API_KEY": "TSR_RERANK_API_KEY",
        "TSR_GATE_API_KEY": "TSR_GATE_API_KEY",
    }
    store = home / ".credentials.yaml"
    if store.exists():
        text = store.read_text(encoding="utf-8", errors="replace")
        for ref, env_name in refs.items():
            # keys.env 里留空的占位会写进环境，此时应继续从凭据库补
            if os.environ.get(env_name):
                continue
            match = re.search(rf"^\s*{re.escape(ref)}:\s*(\S+)\s*$", text, re.M)
            if match:
                os.environ[env_name] = match.group(1)


def resolve_rules_url() -> str:
    """规则服务地址：环境变量优先，其次本机默认端口探测。"""
    explicit = os.getenv("TSR_REGISTRY_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    for candidate in ("http://127.0.0.1:8010",):
        try:
            import httpx

            if httpx.get(candidate + "/health", timeout=3, trust_env=False).status_code == 200:
                return candidate
        except Exception:  # noqa: BLE001 - 探测失败就是没起
            continue
    return ""


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    load_env()

    if not os.getenv("TSR_MODEL_BASE_URL") or not os.getenv("TSR_MODEL_API_KEY"):
        print("SKIP: 未配置对话模型（TSR_MODEL_BASE_URL / TSR_MODEL_API_KEY）")
        return 2
    rules_url = resolve_rules_url()
    if not rules_url:
        print("SKIP: 规则服务不可达，先启动 rule-server 或在设置里填地址")
        return 2
    os.environ["TSR_REGISTRY_URL"] = rules_url

    data_dir = Path(tempfile.mkdtemp(prefix="review-e2e-"))
    workspace = data_dir / "workspace"
    runs = data_dir / "runs"
    workspace.mkdir(parents=True)
    runs.mkdir(parents=True)

    docx = workspace / "小样技术规范书.docx"
    build_docx(docx)

    steps: list[dict] = []
    started = time.time()

    def record(name: str, ok: bool, detail: object = "") -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f" -> {detail}"))

    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "py"),
        "TSR_DOCUMENT_ROOT": str(workspace),
        "TSR_RUNS_DIR": str(runs),
        "PYTHONIOENCODING": "utf-8",
    }
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "technical_spec_review.cli", "run",
             docx.name, "--object-scope", "净水器"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=1800,
        )
        if completed.returncode != 0:
            record("审查执行", False, (completed.stderr or completed.stdout)[-600:])
        else:
            record("审查执行", True)

        review_dirs = [d for d in runs.iterdir() if d.is_dir()] if runs.exists() else []
        if not review_dirs:
            record("产出目录", False, "没有生成审查目录")
        else:
            run_dir = review_dirs[0]
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            tasks = state.get("tasks") or {}
            failed = [k for k, v in tasks.items() if v.get("status") != "COMPLETED"]
            record("全部子任务完成", not failed, {"failed": failed, "status": state.get("status")})

            expected = ["B01", "B02", "B03", "B04", "SUMMARY"]
            missing = [name for name in expected if not (run_dir / f"{name}.json").exists()]
            record("过程文件齐全", not missing, {"missing": missing})

            report_md = run_dir / "report.md"
            report_docx = run_dir / "report.docx"
            record(
                "报告已生成",
                report_md.exists() and report_docx.exists(),
                {"md": report_md.stat().st_size if report_md.exists() else 0,
                 "docx": report_docx.stat().st_size if report_docx.exists() else 0},
            )

            # 本地审查契约：文档快照进审查目录，状态里不再有上传期的 file_id
            snapshot = run_dir / "source.docx"
            snapshot_ok = (
                snapshot.exists()
                and hashlib.sha256(snapshot.read_bytes()).hexdigest() == state.get("source_hash")
            )
            record(
                "源文档已快照",
                snapshot_ok,
                {"snapshot": str(snapshot.name), "source": state.get("source")},
            )
            legacy_fields = [name for name in ("file_id", "file_expires_at") if name in state]
            record("状态无上传字段", not legacy_fields, {"leftover": legacy_fields})
            record(
                "记录原始路径",
                bool(state.get("source_path")),
                {"source_path": state.get("source_path")},
            )

            # 交付文本体检：内部编号、残句、计数行、人工确认条目上限
            report_text = report_md.read_text(encoding="utf-8") if report_md.exists() else ""
            text_problems = check_report_text(report_text)
            record("报告文本可读", not text_problems, text_problems[:5])

            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            groups = summary.get("groups") or []
            issues = sum(len(g.get("issue_ids") or []) for g in groups)
            record("报告含问题分组", bool(groups) and issues > 0, {"groups": len(groups), "issues": issues})

            b03 = json.loads((run_dir / "B03.json").read_text(encoding="utf-8"))
            codes = {f.get("code") for f in b03.get("findings") or []}
            # 样本故意从第 2 章跳到第 4 章，编号连续性应被抓到
            record(
                "编号跳号被检出",
                "NUMBERING_ANOMALY" in codes,
                {"codes": sorted(c for c in codes if c)},
            )
    except Exception as exc:  # noqa: BLE001 - 报告要记录失败原因而不是崩栈
        record("执行链路", False, f"{type(exc).__name__}: {exc}")
    finally:
        if not keep:
            shutil.rmtree(data_dir, ignore_errors=True)

    duration = round((time.time() - started) / 60, 1)
    passed = all(step["ok"] for step in steps) and bool(steps)
    report = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "durationMinutes": duration,
        "dataDir": str(data_dir) if keep else "(removed)",
        "passed": passed,
        "steps": steps,
    }
    ARTIFACTS.mkdir(exist_ok=True)
    out = ARTIFACTS / f"e2e-review-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'E2E PASSED' if passed else 'E2E FAILED'}  {duration} min  报告: {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
