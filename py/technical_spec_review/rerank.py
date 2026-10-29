from __future__ import annotations

import asyncio
from typing import Any

import httpx

# 响应里可能出现的分数键名，按优先级尝试。
_SCORE_KEYS = ("relevance_score", "relevance_scores", "score", "similarity")
# 响应里可能出现的序号键名。
_INDEX_KEYS = ("index", "id", "document_index")


class RerankClient:
    """交叉编码重排：向量召回后按 query-doc 相关性重排。

    各家 rerank 接口的请求/响应形态不一致，这里按 ``fmt`` 适配，``auto`` 时
    依 URL 猜一个，猜错也能靠响应解析兜住（见 ``_scores``）：

    - ``siliconflow`` / ``cohere`` / ``jina`` / ``dashscope`` / ``openai``：
      ``POST {base}/rerank``，请求 ``{"model", "query", "documents", "top_n"}``，
      响应 ``{"results": [{"index", "relevance_score"}]}``。
    - ``cf``（Cloudflare Workers AI 原生 ``/ai/run/@cf/...``）：
      请求 ``{"query", "contexts": [{"text": ...}]}``，
      响应 ``{"result": {"response": [{"id", "score"}]}}``。
    - ``tei``（HuggingFace Text Embeddings Inference）：
      ``POST {base}/rerank``，请求 ``{"query", "texts", "raw_scores"}``，
      响应 ``[{"index", "score"}]``（裸数组）。

    任何失败都返回 ``None``，调用方回退向量顺序 —— 重排是召回质量的增益环节，
    不能让它的可用性影响审查流程能否跑完。
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        api_key: str,
        model: str = "",
        *,
        fmt: str = "auto",
        retries: int = 1,
        max_input_chars: int = 2000,
    ):
        if not url:
            raise ValueError("RERANK_URL_REQUIRED")
        self.http = http
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.fmt = self._detect(fmt, self.url)
        self.retries = retries
        self.max_input_chars = max_input_chars

    @staticmethod
    def _detect(fmt: str, url: str) -> str:
        """按显式配置或 URL 特征选形态；无法判定时用最通用的 documents 形态。"""
        known = {"cf", "tei", "siliconflow", "cohere", "jina", "dashscope", "openai"}
        if fmt in known:
            return fmt
        lowered = url.lower()
        if "/ai/run/" in lowered:
            return "cf"
        if "siliconflow" in lowered:
            return "siliconflow"
        if "cohere" in lowered:
            return "cohere"
        if "jina" in lowered:
            return "jina"
        if "dashscope" in lowered or "aliyuncs" in lowered:
            return "dashscope"
        return "openai"

    def _endpoint(self) -> str:
        """各家 rerank 的路径不同；已带 /rerank 的直接用。"""
        if self.url.endswith("/rerank"):
            return self.url
        if self.fmt == "cf":
            return self.url
        return self.url + "/rerank"

    def _payload(self, query: str, documents: list[str]) -> dict[str, Any]:
        clipped = [d[: self.max_input_chars] for d in documents]
        if self.fmt == "cf":
            return {"query": query, "contexts": [{"text": d} for d in clipped]}
        if self.fmt == "tei":
            return {"query": query, "texts": clipped, "raw_scores": False}
        body: dict[str, Any] = {
            "query": query,
            "documents": clipped,
            "top_n": len(clipped),
        }
        if self.model:
            body["model"] = self.model
        # DashScope 原生兼容接口要求 return_documents 显式关闭，否则响应体很大。
        if self.fmt == "dashscope":
            body["return_documents"] = False
        return body

    @staticmethod
    def _rows(payload: Any, fmt: str) -> list[dict[str, Any]] | None:
        """从各种响应外壳里取出结果数组。"""
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return None
        if fmt == "cf":
            inner = payload.get("result")
            if isinstance(inner, dict):
                rows = inner.get("response")
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
        for key in ("results", "data", "output"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("results")
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return None

    @classmethod
    def _scores(cls, payload: Any, count: int, fmt: str) -> list[float] | None:
        """把响应还原成与输入同序的分数列表；形态不符返回 None。"""
        rows = cls._rows(payload, fmt)
        if not rows:
            return None
        scores = [0.0] * count
        hit = 0
        for row in rows:
            index: Any = None
            for key in _INDEX_KEYS:
                if isinstance(row.get(key), int):
                    index = row[key]
                    break
            score: Any = None
            for key in _SCORE_KEYS:
                if isinstance(row.get(key), (int, float)):
                    score = row[key]
                    break
            if not isinstance(index, int) or not 0 <= index < count:
                return None
            if not isinstance(score, (int, float)):
                return None
            scores[index] = float(score)
            hit += 1
        if hit != count:
            # 稀疏返回会让未命中文档拿到 0 分并被误当成"不相关"，直接判为不可用。
            return None
        return scores

    async def rerank(self, query: str, documents: list[str]) -> list[float] | None:
        if not documents:
            return []
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = self._endpoint()
        for attempt in range(self.retries + 1):
            try:
                response = await self.http.post(
                    url, json=self._payload(query, documents), headers=headers
                )
            except httpx.RequestError:
                if attempt == self.retries:
                    return None
            else:
                if not response.is_success:
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                        await asyncio.sleep(min(2**attempt, 5))
                        continue
                    return None
                try:
                    return self._scores(response.json(), len(documents), self.fmt)
                except (ValueError, AttributeError):
                    return None
            await asyncio.sleep(min(2**attempt, 5))
        return None
