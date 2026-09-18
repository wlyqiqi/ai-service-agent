"""LightRAG-chengyuan 检索客户端（read 档 kb_search 的数据源）。

只做「检索取上下文 + 引用来源」，不做生成：POST /query 参数
mode=naive、only_need_context=true、include_references=true、
include_chunk_content=true、enable_rerank=false、top_k=5
（实测检索 0.35–0.66s/case，2026-09-18 复跑）。
httpx 同步 Client 模块级单例（线程安全、连接复用）。离线测试用
transport=httpx.MockTransport(...) 注入。
"""

from typing import Optional

import httpx

from ai_service_agent.config import get_settings


class LightRagClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        mode: Optional[str] = None,
        top_k: Optional[int] = None,
        include_references: Optional[bool] = None,
        enable_rerank: Optional[bool] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url if base_url is not None else settings.lightrag_base_url).rstrip("/")
        self.timeout = timeout_seconds if timeout_seconds is not None else settings.lightrag_timeout_seconds
        self.mode = mode if mode is not None else settings.lightrag_mode
        self.top_k = top_k if top_k is not None else settings.lightrag_top_k
        self.include_references = (
            include_references if include_references is not None else settings.lightrag_include_references
        )
        self.enable_rerank = enable_rerank if enable_rerank is not None else settings.lightrag_enable_rerank
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=transport,
        )

    def query_context(self, query: str) -> dict:
        try:
            resp = self._client.post(
                "/query",
                json={
                    "query": query,
                    "mode": self.mode,
                    "only_need_context": True,
                    "include_references": self.include_references,
                    "include_chunk_content": True,
                    "top_k": self.top_k,
                    "enable_rerank": self.enable_rerank,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            refs = payload.get("references") or []
            contexts = []
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                content = ref.get("content")
                if not content:
                    continue
                if isinstance(content, list):
                    chunks = [str(c) for c in content if c not in (None, "")]
                    if not chunks:
                        continue
                    contexts.append("\n".join(chunks))
                else:
                    contexts.append(str(content))
            references = [
                {"file_path": ref.get("file_path", ""), "reference_id": ref.get("reference_id", "")}
                for ref in refs
                if isinstance(ref, dict)
            ]
        except Exception:
            return {"error": "kb_unavailable"}
        if not contexts:
            return {"context": "", "references": []}
        return {"context": "\n".join(contexts), "references": references}


_client: Optional[LightRagClient] = None


def get_lightrag_client() -> LightRagClient:
    global _client
    if _client is None:
        _client = LightRagClient()
    return _client


def reset_lightrag_client() -> None:
    """测试用：清空单例，避免跨用例共享 client。"""
    global _client
    if _client is not None:
        _client._client.close()
    _client = None
