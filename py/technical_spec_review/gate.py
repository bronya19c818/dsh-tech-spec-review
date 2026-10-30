"""B02 前置筛选：决策模型打分，把明显没有文字问题的句子挡在 LLM 之前。

用 Cloudflare 的 Clef（决策模型，不是生成模型）：一次调用把整段正文作为 state、
每句作为一个 noul 问题，返回每句"存在文字问题"的概率。句子文本写进 instructions
是实测有效的前提——只给通用问法时模型分不清在问哪句。

三条硬约束：
- 必须用 27B 的 clef。9B 的 clef-flash 在中文病句上与随机噪声接近，实测配对只有 3/8 排对。
- 任何失败都抛出 GateError，由调用方退回"全部句子交 LLM"，绝不静默放行。
- 只输出"疑似/放行"，不输出结论；定性仍由 LLM 负责。
"""
from __future__ import annotations

import re

import httpx

# 单次调用的问题数上限是 64；留出余量给 instructions 里的句子文本。
MAX_SENTENCES_PER_CALL = 40

QUESTION = "这句话是否存在错别字、重复字词、缺字、标点错误或语序错误？"


class GateError(RuntimeError):
    """筛选不可用。调用方必须退回全量交 LLM。"""


def endpoint_for(base_url: str, model: str) -> str:
    """把配置地址补成完整调用地址，兼容只填 account 段或填全 URL 两种写法。"""
    base = (base_url or "").rstrip("/")
    if "/ai/run/" in base:
        return base if base.endswith(model) else base
    if base.endswith("clef") or base.endswith("clef-flash"):
        return base
    return f"{base}/@cf/cloudflare/{model}"


class ClefGate:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        *,
        model: str = "clef",
        threshold: float = 0.5,
    ):
        if not base_url:
            raise GateError("GATE_NOT_CONFIGURED")
        if model != "clef":
            # flash 版实测不可用，配置错了要拦下来而不是悄悄用差结果。
            raise GateError(f"GATE_MODEL_UNSUPPORTED:{model}")
        self.http = http
        self.url = endpoint_for(base_url, model)
        self.api_key = api_key
        self.model = model
        self.threshold = threshold

    async def _one_call(self, state: str, sentences: list[str]) -> list[float]:
        questions = {
            f"s{index}": {
                "type": "noul",
                "instructions": f"第{index + 1}句「{text}」{QUESTION}",
            }
            for index, text in enumerate(sentences)
        }
        try:
            response = await self.http.post(
                self.url,
                json={"model": self.model, "state": state, "questions": questions},
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
        except httpx.RequestError as exc:
            raise GateError("GATE_TRANSPORT_ERROR") from exc
        if response.status_code != 200:
            raise GateError(f"GATE_HTTP_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GateError("GATE_RESPONSE_INVALID") from exc
        answers = ((payload or {}).get("result") or {}).get("answers")
        if not isinstance(answers, dict) or not payload.get("success", True):
            raise GateError("GATE_RESPONSE_INVALID")
        scores: list[float] = []
        for index in range(len(sentences)):
            entry = answers.get(f"s{index}")
            if not isinstance(entry, dict) or not isinstance(entry.get("noul"), (int, float)):
                raise GateError("GATE_COVERAGE_INVALID")
            scores.append(float(entry["noul"]))
        return scores

    async def score(self, groups: list[tuple[str, list[str]]]) -> list[float]:
        """按上下文分组打分。

        groups 是 (state 文本, 该组句子列表) 的序列，返回与展平后句子同序的概率。
        """
        out: list[float] = []
        for state, sentences in groups:
            for start in range(0, len(sentences), MAX_SENTENCES_PER_CALL):
                chunk = sentences[start : start + MAX_SENTENCES_PER_CALL]
                out.extend(await self._one_call(state or "\n".join(chunk), chunk))
        return out

    def split(self, units: list[dict], scores: list[float]) -> tuple[list[dict], list[dict]]:
        """按阈值分成"送 LLM"与"放行"，边界一律偏保守。"""
        suspect: list[dict] = []
        passed: list[dict] = []
        for unit, score in zip(units, scores):
            row = {**unit, "gate_score": round(score, 4)}
            (suspect if score >= self.threshold else passed).append(row)
        return suspect, passed


# --- T0：确定性规则，不需要模型 ---

REPEATED_CHAR = re.compile(r"([\u4e00-\u9fff])\1\1+")
# 整词重复（如"试验试验"），排除数字与常见叠词
REPEATED_WORD = re.compile(r"([\u4e00-\u9fff]{2,6})\1")
# 必须是真的"连续重复两个以上"标点：字符类里每个字单独匹配会命中所有单个句号
DOUBLED_PUNCT = re.compile(r"([。，、；：！？（）【】])\1+")
FULLWIDTH_IN_NUMBERS = re.compile(r"[０-９]+(?:\.[０-９]+)?")
SPACE_AROUND_CJK_PUNCT = re.compile(r"[\u4e00-\u9fff]\s+[。，、；：]|[。，、；：]\s+[\u4e00-\u9fff]")
# 虚词叠用一定是错误（"和和""的的"），实词叠词（"慢慢""常常"）是正常表达，不在此列。
REPEATED_FUNCTION_WORD = re.compile(r"([和的了是在与及或把被将对为就都也还而但并且])\1")
# 中文字符后面紧跟半角标点，说明中英文标点混用
HALFWIDTH_AFTER_CJK = re.compile(r"[\u4e00-\u9fff][,;:!?]")


def obvious_defects(text: str) -> list[tuple[str, str, str]]:
    """返回 (类别, 证据片段, 说明)。这些判定不依赖模型，可直接写进报告。"""
    found: list[tuple[str, str, str]] = []

    for match in REPEATED_CHAR.finditer(text):
        token = match.group(0)
        # "慢慢""常常"这类叠词是正常表达，三个字以上连写才可疑
        if len(token) >= 3 and token not in {"整整三", "月月月"}:
            found.append(("重复字词", token, f"“{token[0]}”连续重复 {len(token)} 次"))
            break

    for match in REPEATED_WORD.finditer(text):
        token = match.group(0)
        head = match.group(1)
        # 数字与单位连写、以及"和和"以外的常见叠用不报
        if len(head) >= 2 and not re.search(r"\d", head) and head not in {"大大", "单单", "人人", "年年", "日日"}:
            found.append(("重复字词", token, f"“{head}”整词重复"))
            break

    if match := REPEATED_FUNCTION_WORD.search(text):
        token = match.group(0)
        found.append(("重复字词", token, f"虚词“{match.group(1)}”重复"))

    if match := DOUBLED_PUNCT.search(text):
        found.append(("标点错误", match.group(0), f"标点“{match.group(1)}”连续出现 {len(match.group(0))} 次"))

    if match := SPACE_AROUND_CJK_PUNCT.search(text):
        found.append(("标点错误", match.group(0), "中文标点前后存在多余空格"))

    if match := HALFWIDTH_AFTER_CJK.search(text):
        found.append(("标点错误", match.group(0), f"中文之后使用了半角标点“{match.group(0)[-1]}”"))

    if match := FULLWIDTH_IN_NUMBERS.search(text):
        found.append(("格式错误", match.group(0), "技术指标数值使用了全角字符"))

    return found
