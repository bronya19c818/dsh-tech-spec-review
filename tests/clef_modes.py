"""对照实验：换提问方式与模型，排除"是我的用法导致 Clef 失效"。

三种问法：
A. 句子文本嵌进 instructions（首轮探针用的那种）
B. instructions 只写通用判定标准，句子只在 state 里
C. 用 choice 题型，让它在"正确/有问题"之间选

每种都同时测 clef-flash 与 clef，并给出"正确句 vs 错误句"的配对对比。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ENDPOINT = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"

CASES = [
    ("重复字词", "净水器应具备防腐蚀性能。", "净水器应具备防腐腐腐性能。"),
    ("重复字词", "滤芯应便于拆卸和更换。", "滤芯应便于拆卸和和更换。"),
    ("缺字", "设备在额定条件下运行时不得漏水。", "设备在额定条件下运行不得漏水。"),
    ("标点错误", "技术要求包括产水量、净化流量和功耗。", "技术要求包括产水量,净化流量和功耗。"),
    ("语序错误", "出水水质应符合生活饮用水卫生标准。", "出水水质应生活符合饮用水卫生标准。"),
    ("错别字", "外壳表面应平整光洁。", "外壳表面应平整光结。"),
    ("错别字", "设备应具备断水保护功能。", "设备应具备断水保户功能。"),
    ("重复字词", "产品应通过型式试验和出厂试验。", "产品应通过过型式试验和出厂试验。"),
]

GENERIC = "这段文本中是否存在错别字、重复字词、缺字、标点错误或语序错误？"


def creds() -> dict:
    lines = (ROOT.parent / "src.txt").read_text(encoding="utf-8").splitlines()
    return {
        "account": lines[4].split("=", 1)[1].strip(),
        "key": lines[5].split("=", 1)[1].strip(),
    }


def run(client, c, model: str, mode: str, sentences: list[str], state: str) -> list[float]:
    if mode == "A":
        questions = {
            f"s{i}": {"type": "noul", "instructions": f"第{i + 1}句「{t}」{GENERIC}"}
            for i, t in enumerate(sentences)
        }
    elif mode == "B":
        questions = {f"s{i}": {"type": "noul", "instructions": GENERIC} for i in range(len(sentences))}
    else:
        questions = {
            f"s{i}": {
                "type": "choice",
                "instructions": f"第{i + 1}句文字质量如何？",
                "criteria": {"ok": "文字表达正确，没有明显错误", "bad": "存在错别字、重复字词、缺字、标点错误或语序错误"},
            }
            for i in range(len(sentences))
        }
    response = client.post(
        ENDPOINT.format(account=c["account"], model=model),
        headers={"Authorization": f"Bearer {c['key']}"},
        json={"model": model, "state": state, "questions": questions},
        timeout=90,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP_{response.status_code}: {response.text[:150]}")
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"CLEF_FAILED: {payload.get('errors')}")
    answers = payload["result"]["answers"]
    if mode == "C":
        return [float(answers[f"s{i}"]["probabilities"]["bad"]) for i in range(len(sentences))]
    return [float(answers[f"s{i}"]["noul"]) for i in range(len(sentences))]


def main(argv: list[str]) -> int:
    import httpx

    c = creds()
    client = httpx.Client(trust_env=False)
    report = {"startedAt": datetime.now(timezone.utc).isoformat(), "results": []}

    for model in ("clef-flash", "clef"):
        for mode in ("A", "B", "C"):
            label = {"A": "句子入 instructions", "B": "通用 instructions", "C": "choice 题型"}[mode]
            print(f"\n=== {model} / {mode} {label} ===")
            correct_scores, broken_scores = [], []
            for category, correct, broken in CASES:
                state = f"{correct}\n{broken}"
                try:
                    scores = run(client, c, model, mode, [correct, broken], state)
                    good, bad = scores[0], scores[1]
                    correct_scores.append(good)
                    broken_scores.append(bad)
                    mark = "OK" if bad > good else "反"
                    print(f"  {category:<5} 正确 {good:.4f}  错误 {bad:.4f}  差 {bad - good:+.4f}  {mark}")
                except Exception as exc:  # noqa: BLE001
                    report["results"].append({"model": model, "mode": mode, "error": str(exc)})
            if correct_scores:
                sep = sum(1 for g, b in zip(correct_scores, broken_scores) if b > g)
                entry = {
                    "model": model,
                    "mode": mode,
                    "meanCorrect": round(sum(correct_scores) / len(correct_scores), 4),
                    "meanBroken": round(sum(broken_scores) / len(broken_scores), 4),
                    "separated": f"{sep}/{len(correct_scores)}",
                }
                report["results"].append(entry)
                print(
                    f"  >> 均值：正确 {entry['meanCorrect']:.4f}  错误 {entry['meanBroken']:.4f}  "
                    f"排序正确 {entry['separated']}"
                )

    ARTIFACTS.mkdir(exist_ok=True)
    out = ARTIFACTS / f"clef-modes-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
