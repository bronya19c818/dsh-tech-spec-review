"""Clef 对中文病句判定的探针：误报率 + 召回率。

两件事分开测，因为数据来源不同：
1. 误报率：用真实审查里模型判定"无问题"的句子，看 Clef 会不会误报。
2. 召回率：真实文档只有 2 条问题句，不够标定，所以构造一组已知错误
   （重复字、缺字、标点错、语序错），以及一组与之配对的正确改写作为对照。

两种 state 形态都测：单句 vs 整段（带上下文），这是引入 Clef 的主要动机。

用法：
    python tests/clef_probe.py            # 全部跑一遍，产物写 artifacts/
    python tests/clef_probe.py --model clef
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ENDPOINT = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"

# 已知有问题的句子（正样本）与对应正确改写（负样本对照）
CASES = [
    # (类别, 正确句, 错误句)
    ("重复字词", "净水器应具备防腐蚀性能。", "净水器应具备防腐腐腐性能。"),
    ("重复字词", "滤芯应便于拆卸和更换。", "滤芯应便于拆卸和和更换。"),
    ("缺字", "设备在额定条件下运行时不得漏水。", "设备在额定条件下运行不得漏水。"),
    ("缺字", "投标人应提供完整的检验记录。", "投标人应提供完整的验记录。"),
    ("标点错误", "技术要求包括产水量、净化流量和功耗。", "技术要求包括产水量,净化流量和功耗。"),
    ("标点错误", "试验方法应符合下列要求。", "试验方法应符合下列要求。。"),
    ("语序错误", "出水水质应符合生活饮用水卫生标准。", "出水水质应生活符合饮用水卫生标准。"),
    ("语序错误", "滤芯使用寿命不应低于5000小时。", "滤芯使用不应寿命低于5000小时。"),
    ("错别字", "外壳表面应平整光洁。", "外壳表面应平整光结。"),
    ("错别字", "设备应具备断水保护功能。", "设备应具备断水保户功能。"),
    ("重复字词", "产品应通过型式试验和出厂试验。", "产品应通过过型式试验和出厂试验。"),
    ("缺字", "铭牌信息应清晰耐久。", "铭牌信息应清晰耐。"),
    ("标点错误", "应满足绝缘电阻、接地连续性要求。", "应满足绝缘电阻、接地连续性要求"),
    ("语序错误", "采购人应按合同约定组织验收。", "采购人应按合同组织约定验收。"),
    ("错别字", "密封性能应符合相关标准。", "密封性能应符合相关标淮。"),
    ("重复字词", "该产品适用于办公场所使用。", "该产品适用于于办公场所使用。"),
]

QUESTION_TEXT = "这句话是否存在错别字、重复字词、缺字、标点错误或语序错误？"


def load_env() -> dict:
    source = ROOT.parent / "src.txt"
    if not source.exists():
        raise SystemExit(f"缺少凭据文件：{source}")
    lines = source.read_text(encoding="utf-8").splitlines()
    account = lines[4].split("=", 1)[1].strip()
    key = lines[5].split("=", 1)[1].strip()
    return {"account": account, "key": key}


def ask(client, account: str, key: str, model: str, state: str, sentences: list[str]) -> dict:
    """一次调用同时判断多句：每句一个 noul 问题，共享同一段 state。"""
    questions = {}
    for index, text in enumerate(sentences):
        questions[f"s{index}"] = {
            "type": "noul",
            "instructions": f"第{index + 1}句「{text}」{QUESTION_TEXT}",
        }
    response = client.post(
        ENDPOINT.format(account=account, model=model),
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "state": state, "questions": questions},
        timeout=60,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP_{response.status_code}: {response.text[:200]}")
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"CLEF_FAILED: {payload.get('errors')}")
    answers = payload["result"]["answers"]
    return {
        f"s{index}": float(answers[f"s{index}"]["noul"]) for index in range(len(sentences))
    }, int(payload["result"].get("usage", {}).get("input_tokens") or 0)


def main(argv: list[str]) -> int:
    model = "clef-flash"
    if "--model" in argv:
        model = argv[argv.index("--model") + 1]

    import httpx

    creds = load_env()
    client = httpx.Client(trust_env=False)

    report: dict = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "cases": [],
        "errors": [],
    }
    started = time.time()

    # 1) 召回：正确句与错误句配对，同一 state 里同时判断，检验上下文干扰
    print("=== 召回 / 误报（配对，同一 state 内含正确句与错误句）===")
    pairs = []
    for category, correct, broken in CASES:
        state = f"{correct}\n{broken}"
        try:
            scores, tokens = ask(client, creds["account"], creds["key"], model, state, [correct, broken])
            good = scores["s0"]
            bad = scores["s1"]
            pairs.append({
                "category": category, "correct": correct, "broken": broken,
                "score_correct": good, "score_broken": bad, "tokens": tokens,
            })
            print(f"  {category:<6} 正确 {good:.4f}  错误 {bad:.4f}  差 {bad - good:+.4f}")
        except Exception as exc:  # noqa: BLE001 - 单例失败要记录而不是中断
            report["errors"].append({"stage": "pair", "category": category, "error": str(exc)})
    report["cases"] = pairs

    # 2) 真实文档里的句子：单句 state，检验误报
    print("\n=== 真实文档 84 句（模型判定无问题），单句 state ===")
    run_dir = Path(os.getenv("TSR_RUNS_DIR") or (Path.home() / ".dsh" / "technical-spec-review" / "runs"))
    target = run_dir / "cfca399772864fe3ad2591ccf6763a8a" / "B02.json"
    real_scores: list[dict] = []
    if target.exists():
        decisions = json.loads(target.read_text(encoding="utf-8"))["decisions"]
        clean = [d for d in decisions if not d.get("has_issue")]
        # 每批最多 32 句（每句一个问题，留余量给 64 上限）
        for start in range(0, len(clean), 32):
            batch = clean[start:start + 32]
            state = "\n".join(item["text"] for item in batch)
            try:
                scores, tokens = ask(
                    client, creds["account"], creds["key"], model, state,
                    [item["text"] for item in batch],
                )
                for index, item in enumerate(batch):
                    real_scores.append({
                        "id": item["id"],
                        "text": item["text"],
                        "score": scores[f"s{index}"],
                    })
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"stage": "real", "start": start, "error": str(exc)})
        values = sorted(row["score"] for row in real_scores)
        if values:
            def pct(p):
                return values[min(len(values) - 1, int(p * len(values)))]
            print(f"  样本 {len(values)} 句")
            print(f"  中位 {pct(0.50):.4f}  75分位 {pct(0.75):.4f}  90分位 {pct(0.90):.4f}  最大 {values[-1]:.4f}")
    else:
        print("  跳过：找不到历史 B02 结果")
    report["realScores"] = real_scores

    # 3) 阈值分析：在若干阈值下，召回与误报各是多少
    print("\n=== 阈值分析 ===")
    table = []
    if pairs:
        for threshold in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
            recall = sum(1 for p in pairs if p["score_broken"] >= threshold) / len(pairs)
            false_alarm = (
                sum(1 for p in pairs if p["score_correct"] >= threshold) / len(pairs)
                if pairs else 0.0
            )
            real_fp = (
                sum(1 for row in real_scores if row["score"] >= threshold) / len(real_scores)
                if real_scores else 0.0
            )
            table.append({
                "threshold": threshold,
                "recall": round(recall, 3),
                "false_alarm_paired": round(false_alarm, 3),
                "false_alarm_real": round(real_fp, 3),
            })
            print(
                f"  阈值 {threshold:.2f}  召回 {recall:.0%}  "
                f"配对误报 {false_alarm:.0%}  真实误报 {real_fp:.0%}"
            )
    report["thresholds"] = table
    report["durationSeconds"] = round(time.time() - started, 1)

    ARTIFACTS.mkdir(exist_ok=True)
    out = ARTIFACTS / f"clef-probe-{model}-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
