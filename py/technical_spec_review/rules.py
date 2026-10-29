from __future__ import annotations

import re
from typing import Any

import httpx

from .models import Rule


class RegistryClient:
    """规则源客户端：结构化规则走 Registry HTTP，语义检索走服务端接口。

    只读，从不发布或修改规则。嵌入模型与向量索引都在服务端，
    客户端只发查询文本；重排是客户端侧的可选增益。
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        *,
        dify_url: str = "",
        dataset_id: str = "",
        dify_key: str = "",
        registry_key: str = "",
        reranker: Any = None,
        rerank_recall: int = 24,
        rerank_top_k: int = 8,
        rerank_threshold: float = 0.0,
        semantic_via_server: bool = True,
    ):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.dify_url = dify_url.rstrip("/")
        self.dataset_id = dataset_id
        self.dify_key = dify_key
        self.registry_key = registry_key
        # 语义检索由服务端提供；未配置时回退旧 Dify 链路。
        self.semantic_via_server = semantic_via_server
        # 重排是可选增益：未配置时召回顺序即为最终顺序。
        self.reranker = reranker
        self.rerank_recall = max(rerank_recall, rerank_top_k)
        self.rerank_top_k = rerank_top_k
        self.rerank_threshold = rerank_threshold

    async def list_packages(self, document_role: str = "采购技术规范书") -> list[dict[str, Any]]:
        """已发布规则目录，只经服务端 HTTP 读取。

        客户端不持有数据库连接串：规则库与索引是服务端的资产。
        """
        response = await self._post("/v1/rule-packages/list", {"document_role": document_role})
        if not response.is_success:
            raise RuntimeError(f"CATALOG_HTTP_{response.status_code}")
        packages = response.json().get("packages")
        if not isinstance(packages, list) or any(
            row.get("document_role") != document_role or not row.get("object_scope")
            or not row.get("package_key")
            for row in packages
        ):
            raise ValueError("CATALOG_INVALID")
        return packages

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        if not self.base_url:
            raise RuntimeError("REGISTRY_URL_REQUIRED")
        headers = {"Authorization": f"Bearer {self.registry_key}"} if self.registry_key else {}
        try:
            return await self.http.post(self.base_url + path, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise RuntimeError("REGISTRY_TRANSPORT_ERROR") from exc

    @staticmethod
    def absent(response: httpx.Response, code: str) -> bool:
        if response.status_code != 404:
            return False
        try:
            return response.json().get("detail", {}).get("code") == code
        except (ValueError, AttributeError):
            return False

    async def get_rule(
        self,
        object_scope: str,
        part: str,
        section_number: str,
        rule_kind: str = "chapter",
        document_role: str = "采购技术规范书",
    ) -> Rule | None:
        if object_scope == "UNKNOWN":
            return None
        identity = dict(
            object_scope=object_scope,
            part=part,
            section_number=section_number,
            rule_kind=rule_kind,
            document_role=document_role,
        )
        response = await self._post("/v1/rules/get", identity)
        if self.absent(response, "ACTIVE_RULE_NOT_FOUND"):
            return None
        if not response.is_success:
            raise RuntimeError(f"REGISTRY_HTTP_{response.status_code}")
        rule = Rule.model_validate(response.json()["rule"]).verify()
        if any(getattr(rule, key) != value for key, value in identity.items()):
            raise ValueError("REGISTRY_IDENTITY_MISMATCH")
        return rule

    async def get_structure_rule(
        self, object_scope: str, document_role: str = "采购技术规范书"
    ) -> dict:
        from .structure import structure_rule_response

        rule = await self.get_rule(
            object_scope=object_scope,
            part="全文",
            section_number="00",
            rule_kind="structure",
            document_role=document_role,
        )
        return structure_rule_response(rule)

    async def basic_rules(
        self, stage: str, object_scope: str, document_role: str = "采购技术规范书"
    ) -> list[Rule]:
        response = await self._post(
            "/v1/basic-rules/get",
            dict(
                stage=stage,
                object_scope=None if object_scope == "UNKNOWN" else object_scope,
                document_role=document_role,
            ),
        )
        if self.absent(response, "ACTIVE_BASIC_RULE_NOT_FOUND"):
            return []
        if not response.is_success:
            raise RuntimeError(f"REGISTRY_HTTP_{response.status_code}")
        payload = response.json()
        if payload.get("merge_policy") != "object_overrides_global_by_rule_code":
            raise ValueError("BASIC_MERGE_POLICY_INVALID")
        expected = {}
        for scope in ("global", "object"):
            for raw in payload.get(scope + "_rules", []):
                if raw.get("document_role", document_role) != document_role:
                    raise ValueError("BASIC_RULE_ROLE_MISMATCH")
                if raw.get("scope_level") != scope or raw.get("stage") != stage:
                    raise ValueError("BASIC_RULE_SCOPE_INVALID")
                if raw.get("object_scope") != (None if scope == "global" else object_scope):
                    raise ValueError("BASIC_RULE_OBJECT_MISMATCH")
                Rule.model_validate({"document_role": document_role, **raw}).verify()
                expected[raw["rule_code"]] = raw
        effective = payload.get("effective_rules", [])
        if len(effective) != len(expected) or {r["rule_code"]: r for r in effective} != expected:
            raise ValueError("BASIC_EFFECTIVE_RULES_INVALID")
        return [
            Rule.model_validate({"document_role": document_role, **raw}).verify()
            for raw in effective
        ]

    async def semantic_search(
        self, query: str, object_scope: str, document_role: str = "采购技术规范书"
    ) -> list[dict[str, Any]]:
        # 语义检索由规则服务端提供：嵌入模型与索引都在服务端，客户端只发查询文本。
        # 旧 Dify 链路保留为回退（未配置服务端时）。
        if self.semantic_via_server:
            return await self._semantic_search_server(query, object_scope, document_role)
        return await self._semantic_search_dify(query, object_scope, document_role)

    async def _semantic_search_server(
        self, query: str, object_scope: str, document_role: str = "采购技术规范书"
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not 1 <= len(query) <= 250:
            raise ValueError("SEARCH_QUERY_LENGTH_INVALID")
        payload = {
            "query": query,
            "object_scope": "" if object_scope == "UNKNOWN" else object_scope,
            "document_role": document_role,
            "rule_kind": "chapter",
            "top_k": self.rerank_recall if self.reranker is not None else self.rerank_top_k,
        }
        response = await self._post("/v1/rules/semantic-search", payload)
        if not response.is_success:
            raise RuntimeError(f"SEMANTIC_SEARCH_HTTP_{response.status_code}")
        body = response.json()
        hits = body.get("candidates")
        if not isinstance(hits, list):
            raise ValueError("SEMANTIC_SEARCH_RESPONSE_INVALID")
        hits = await self._rerank_hits(query, hits)
        hits = self._drop_low_rerank(hits)
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for hit in hits:
            if len(results) >= self.rerank_top_k:
                break
            identity = dict(
                object_scope=hit["object_scope"],
                part=hit["part"],
                section_number=hit["section_number"],
                rule_kind=hit["rule_kind"],
            )
            if identity["object_scope"] != object_scope and object_scope != "UNKNOWN":
                continue
            rule = await self.get_rule(**identity, document_role=document_role)
            if rule is None:
                # 索引与结构化规则不一致说明索引过期，按契约报错而不是静默零命中。
                raise ValueError("SEMANTIC_STALE_RULE_IDENTITY")
            key = (rule.rule_id, rule.version)
            if key in seen:
                continue
            seen.add(key)
            entry: dict[str, Any] = {
                "rule": rule.model_dump(),
                "score": hit.get("rerank_score", hit.get("score")),
                "binding": "CANDIDATE",
                "source": "SERVER_THEN_REGISTRY",
                "requested_object_scope": object_scope,
            }
            if hit.get("rerank_score") is not None:
                entry["vector_score"] = hit.get("score")
                entry["rerank_score"] = hit["rerank_score"]
            results.append(entry)
        return results

    async def _rerank_hits(self, query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """向量召回结果按交叉编码重排；重排不可用时原样返回向量顺序。"""
        if self.reranker is None or not hits:
            return hits
        scores = await self.reranker.rerank(query, [hit["content"] for hit in hits])
        if scores is None or len(scores) != len(hits):
            return hits
        for hit, score in zip(hits, scores):
            hit["rerank_score"] = score
        return sorted(hits, key=lambda hit: hit["rerank_score"], reverse=True)

    def _drop_low_rerank(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """阈值只作用在重排分上；未重排时保持原行为（阈值 0 不过滤）。"""
        if self.rerank_threshold <= 0 or not hits:
            return hits
        if "rerank_score" not in hits[0]:
            return hits
        return [hit for hit in hits if hit["rerank_score"] >= self.rerank_threshold]

    async def _semantic_search_dify(
        self, query: str, object_scope: str, document_role: str = "采购技术规范书"
    ) -> list[dict[str, Any]]:
        if not self.dify_url or not self.dataset_id or not self.dify_key:
            raise RuntimeError("DIFY_SEARCH_NOT_CONFIGURED")
        query = query.strip()
        if not 1 <= len(query) <= 250:
            raise ValueError("SEARCH_QUERY_LENGTH_INVALID")
        try:
            response = await self.http.post(
                f"{self.dify_url}/datasets/{self.dataset_id}/retrieve",
                headers={"Authorization": f"Bearer {self.dify_key}"},
                json={
                    "query": query,
                    "retrieval_model": {
                        "search_method": "semantic_search",
                        "reranking_enable": False,
                        "top_k": 8,
                        "score_threshold_enabled": False,
                    },
                },
            )
        except httpx.RequestError as exc:
            raise RuntimeError("DIFY_TRANSPORT_ERROR") from exc
        if not response.is_success:
            raise RuntimeError(f"DIFY_HTTP_{response.status_code}")
        data = response.json()
        if not isinstance(data.get("records"), list):
            raise ValueError("DIFY_RECORDS_INVALID")
        results = []
        seen = set()
        for record in data["records"]:
            segment = record.get("segment") or {}
            content = segment.get("content", "")
            routes = re.findall(r"<!--\s*ROUTE:\s*(.*?)-->", content, flags=re.S)
            if not routes:
                # An unrouteable hit is evidence of an indexing contract error, not a zero-match.
                raise ValueError("DIFY_HIT_MISSING_ROUTE_IDENTITY")
            for route in routes:
                fields = dict(
                    piece.strip().split("=", 1) for piece in route.split("|") if "=" in piece
                )
                fields = {k.strip(): v.strip() for k, v in fields.items()}
                if (
                    fields.get("rule_domain") != "chapter_review"
                    or fields.get("rule_kind") != "chapter"
                ):
                    continue
                if fields.get("document_role") != document_role:
                    continue
                if object_scope != "UNKNOWN" and fields.get("object_scope") != object_scope:
                    continue
                required = ("object_scope", "part", "section_number", "rule_kind")
                if any(not fields.get(k) for k in required):
                    raise ValueError("DIFY_ROUTE_INCOMPLETE")
                rule = await self.get_rule(
                    **{k: fields[k] for k in required}, document_role=document_role
                )
                if rule is None:
                    raise ValueError("DIFY_STALE_RULE_IDENTITY")
                identity = (rule.rule_id, rule.version)
                if identity in seen:
                    continue
                seen.add(identity)
                results.append(
                    {
                        "rule": rule.model_dump(),
                        "score": record.get("score"),
                        "binding": "CANDIDATE",
                        "source": "DIFY_THEN_REGISTRY",
                        "requested_object_scope": object_scope,
                    }
                )
        return results
