"""LightRAG-chengyuan 检索客户端（read 档 kb_search 的数据源）。

只做「检索取上下文 + 引用来源」，不做生成：POST /query 参数
mode=naive、only_need_context=true、include_references=true、
include_chunk_content=true、enable_rerank=false、top_k=5
（实测检索 0.35–0.66s/case，2026-09-18 复跑）。chunk 切分参数由 LightRAG 服务管理；
本客户端只负责下游 prompt 预算：单 chunk 1200 字符、总 context 4000 字符。
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
        max_chunk_chars: Optional[int] = None,
        max_context_chars: Optional[int] = None,
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
        self.max_chunk_chars = (
            max_chunk_chars if max_chunk_chars is not None else settings.lightrag_max_chunk_chars
        )
        self.max_context_chars = (
            max_context_chars if max_context_chars is not None else settings.lightrag_max_context_chars
        )
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
            candidates: list[dict[str, str]] = []
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                file_path = ref.get("file_path", "")
                reference_id = ref.get("reference_id", "")
                content = ref.get("content")
                if not content:
                    pass
                elif isinstance(content, list):
                    for chunk in content:
                        if chunk not in (None, ""):
                            candidates.append(
                                {
                                    "content": str(chunk),
                                    "file_path": file_path,
                                    "reference_id": reference_id,
                                }
                            )
                else:
                    candidates.append(
                        {
                            "content": str(content),
                            "file_path": file_path,
                            "reference_id": reference_id,
                        }
                    )
        except Exception:
            return {"error": "kb_unavailable"}
        if not candidates:
            return {
                "context": "",
                "references": [],
                "metadata": {
                    "retrieved_chunks": 0,
                    "selected_chunks": 0,
                    "retrieved_chars": 0,
                    "context_chars": 0,
                    "truncated": False,
                },
            }

        separator = "\n\n"
        context_parts: list[str] = []
        selected_references: list[dict[str, str]] = []
        context_chars = 0
        retrieved_chars = sum(len(candidate["content"]) for candidate in candidates)
        chunk_truncated = False
        for candidate in candidates:
            content = candidate["content"]
            if len(content) > self.max_chunk_chars:
                content = content[: self.max_chunk_chars]
                chunk_truncated = True

            projected_chars = len(content)
            if context_parts:
                projected_chars += len(separator) + context_chars
            if projected_chars > self.max_context_chars:
                if not context_parts and self.max_context_chars < len(content):
                    content = content[: self.max_context_chars]
                    chunk_truncated = True
                    projected_chars = len(content)
                else:
                    break

            context_parts.append(content)
            reference = {
                "file_path": candidate["file_path"],
                "reference_id": candidate["reference_id"],
            }
            if reference not in selected_references:
                selected_references.append(reference)
            context_chars = projected_chars

        context = separator.join(context_parts)
        selected_chunks = len(context_parts)
        return {
            "context": context,
            "references": selected_references,
            "metadata": {
                "retrieved_chunks": len(candidates),
                "selected_chunks": selected_chunks,
                "retrieved_chars": retrieved_chars,
                "context_chars": len(context),
                "truncated": chunk_truncated or selected_chunks < len(candidates),
            },
        }


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
