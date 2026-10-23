from functools import lru_cache
import os

from langchain_openai import ChatOpenAI
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # populate_by_name：测试与代码可用字段名（model_api_key），.env 仍用 ARK_* alias
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    db_path: str = "data/ai_service.db"
    # 模拟电商库（order_lookup 等 read 工具的真实数据源替代品；工具对外接口不变）
    mock_ecommerce_db_path: str = "data/mock_ecommerce.db"
    fake_model: bool = False
    jev_enabled: bool = Field(default=False, validation_alias="JEV_ENABLED")
    jev_api_key: str | None = Field(default=None, validation_alias="TYPESAFE_API_KEY")
    jev_api_url: str = Field(
        default="https://api.typesafe.ai/v1/systemone", validation_alias="JEV_API_URL"
    )
    jev_model: str = Field(default="jev-latest", validation_alias="JEV_MODEL")
    jev_timeout_seconds: float = Field(
        default=2.5, validation_alias="JEV_TIMEOUT_SECONDS"
    )
    jev_node_timeout_seconds: float = Field(
        default=3.0, validation_alias="JEV_NODE_TIMEOUT_SECONDS"
    )
    # ARK_*：历史默认 / 可选回落；Router / LLM 优先各自 ROUTER_* / LLM_*
    model_name: str = Field(default="deepseek-v4-flash", validation_alias="ARK_MODEL")
    model_endpoint: str | None = Field(default=None, validation_alias="ARK_BASE_URL")
    model_api_key: str | None = Field(default=None, validation_alias="ARK_API_KEY")
    # Router 主模型（空则回落 ARK_MODEL）；凭证 ROUTER_* → LLM_* → ARK_*
    router_model_name: str | None = Field(default=None, validation_alias="ROUTER_MODEL")
    router_model_endpoint: str | None = Field(default=None, validation_alias="ROUTER_BASE_URL")
    router_model_api_key: str | None = Field(default=None, validation_alias="ROUTER_API_KEY")
    # Router 主模型失败时的兜底（同端点凭证）；空则不启用二段调用
    router_fallback_model_name: str | None = Field(
        default=None, validation_alias="ROUTER_FALLBACK_MODEL"
    )
    # 非 Router LLM 节点（read / select / fallback）：默认可走 SiliconFlow；空则回落 ARK_*
    llm_model_name: str | None = Field(default=None, validation_alias="LLM_MODEL")
    llm_model_endpoint: str | None = Field(default=None, validation_alias="LLM_BASE_URL")
    llm_model_api_key: str | None = Field(default=None, validation_alias="LLM_API_KEY")
    router_conf_threshold: float = 0.6
    router_node_timeout_seconds: float = Field(
        default=6.0, validation_alias="ROUTER_NODE_TIMEOUT_SECONDS"
    )
    llm_node_timeout_seconds: float = Field(
        default=12.0, validation_alias="LLM_NODE_TIMEOUT_SECONDS"
    )
    # Model HTTP timeout（须小于对应节点 TimeoutPolicy）
    router_model_timeout_seconds: float = Field(
        default=5.0, ge=0.1, validation_alias="ROUTER_MODEL_TIMEOUT_SECONDS"
    )
    llm_model_timeout_seconds: float = Field(
        default=10.0, ge=0.1, validation_alias="LLM_MODEL_TIMEOUT_SECONDS"
    )
    # 分档 temperature / max output tokens（非 hook；设在 ChatOpenAI 实例上）
    router_temperature: float = Field(default=0.0, ge=0.0, validation_alias="ROUTER_TEMPERATURE")
    select_temperature: float = Field(default=0.0, ge=0.0, validation_alias="SELECT_TEMPERATURE")
    read_temperature: float = Field(default=0.3, ge=0.0, validation_alias="READ_TEMPERATURE")
    fallback_temperature: float = Field(
        default=0.3, ge=0.0, validation_alias="FALLBACK_TEMPERATURE"
    )
    router_max_tokens: int = Field(default=256, ge=1, validation_alias="ROUTER_MAX_TOKENS")
    select_max_tokens: int = Field(default=512, ge=1, validation_alias="SELECT_MAX_TOKENS")
    read_max_tokens: int = Field(default=1024, ge=1, validation_alias="READ_MAX_TOKENS")
    fallback_max_tokens: int = Field(default=512, ge=1, validation_alias="FALLBACK_MAX_TOKENS")
    # Context 输入预算（trim_messages / ContextBudgetMiddleware）
    router_total_input_max_tokens: int = Field(
        default=800, ge=1, validation_alias="ROUTER_TOTAL_INPUT_MAX_TOKENS"
    )
    fallback_total_input_max_tokens: int = Field(
        default=800, ge=1, validation_alias="FALLBACK_TOTAL_INPUT_MAX_TOKENS"
    )
    read_total_input_max_tokens: int = Field(
        default=2000, ge=1, validation_alias="READ_TOTAL_INPUT_MAX_TOKENS"
    )
    select_total_input_max_tokens: int = Field(
        default=1200, ge=1, validation_alias="SELECT_TOTAL_INPUT_MAX_TOKENS"
    )
    # P0（2026-09-18）：显式转人工意图检测配置（graph/handoff_rules.py 消费）。
    # 替代旧 handoff_keywords 裸子串匹配（"人工" 会误伤 "人工呼吸/人工智能" 等话题性表达）。
    handoff_phrases: list[str] = ["转人工", "转接人工", "找人工", "找客服", "人工客服", "真人客服"]
    handoff_cancel_phrases: list[str] = [
        "取消转人工",
        "不用转人工",
        "不转人工了",
        "取消人工",
        "不要人工了",
        "不用人工了",
    ]
    handoff_negative_suffix: list[str] = [
        "培训", "审核", "成本", "智能", "呼吸", "操作", "考试",
        "招聘", "岗位", "工作", "工资", "流程", "制度", "标准",
    ]
    # Guard v2：命名规则包（确定性、无 LLM）。情绪威胁不在此 reject。
    # 键为 guard_reason；值为该包正则列表。勿恢复裸词「直接执行」「投诉到封」。
    guard_rule_patterns: dict[str, list[str]] = {
        "prompt_injection": [r"忽略.*规则"],
        "approval_bypass": [r"绕过审批", r"不需要审批", r"不用审批"],
        "privilege_spoof": [r"系统管理员", r"admin"],
        "state_forgery": [r"查询.*标记为已退款"],
    }
    confirm_keywords: list[str] = ["确认", "同意", "好的", "可以", "是", "没错"]
    cancel_keywords: list[str] = ["取消", "算了", "不用了", "不要", "否"]
    approval_expires_minutes: int = 30
    simulate_approval: bool = False
    # P4（D1）：Clarify 最大澄清轮次，超限转 fallback
    clarify_max_turns: int = 2
    # 同一会话最多 fallback 回复次数；第 fallback_max_turns + 1 次触发转人工。
    # 默认 4：最多 4 次兜底回复，第 5 次进入 Fallback 时确定性转人工。
    fallback_max_turns: int = 4
    # Chatwoot 坐席端（API inbox；仅人工车道启用；未配置则 chatwoot_enabled=False）
    chatwoot_enabled: bool = False
    chatwoot_base_url: str | None = Field(default=None, validation_alias="CHATWOOT_BASE_URL")
    chatwoot_account_id: int | None = Field(default=None, validation_alias="CHATWOOT_ACCOUNT_ID")
    chatwoot_inbox_id: int | None = Field(default=None, validation_alias="CHATWOOT_INBOX_ID")
    chatwoot_api_token: str | None = Field(default=None, validation_alias="CHATWOOT_API_TOKEN")
    # Agent Bot access_token：出站 bot 气泡的 sender=agent_bot（与坐席 CHATWOOT_API_TOKEN 分开）。
    chatwoot_agent_bot_token: str | None = Field(
        default=None, validation_alias="CHATWOOT_AGENT_BOT_TOKEN"
    )
    chatwoot_webhook_secret: str | None = Field(default=None, validation_alias="CHATWOOT_WEBHOOK_SECRET")
    chatwoot_agent_bot_secret: str | None = Field(default=None, validation_alias="CHATWOOT_AGENT_BOT_SECRET")
    chatwoot_website_token: str | None = Field(default=None, validation_alias="CHATWOOT_WEBSITE_TOKEN")
    chatwoot_hmac_secret: str | None = Field(default=None, validation_alias="CHATWOOT_HMAC_SECRET")
    # Agent Bot 读超时后 Chatwoot 会把 pending 会话 open。没有人工 assignee 时
    # 默认拉回 pending。宽限留给 Take over「先 open、再 self-assign」。
    chatwoot_revert_bot_error_open: bool = Field(
        default=True, validation_alias="CHATWOOT_REVERT_BOT_ERROR_OPEN"
    )
    chatwoot_bot_error_open_grace_seconds: float = Field(
        default=2.0, ge=0, validation_alias="CHATWOOT_BOT_ERROR_OPEN_GRACE_SECONDS"
    )
    # LightRAG-chengyuan（read 档 kb_search 数据源，OQ-1 naive / OQ-3 不加缓存 / OQ-4 不做自动同步）
    lightrag_base_url: str = "http://localhost:9621"
    lightrag_timeout_seconds: float = 15.0
    lightrag_mode: str = "naive"
    lightrag_top_k: int = 5
    # 显式传给 /query，避免吃服务端 CHUNK_TOP_K 默认（客服低延迟：少而准）
    lightrag_chunk_top_k: int = Field(default=5, ge=1)
    lightrag_include_references: bool = True
    lightrag_enable_rerank: bool = False
    lightrag_max_chunk_chars: int = Field(default=1200, ge=1)
    lightrag_max_context_chars: int = Field(default=4000, ge=1)
    auth_enabled: bool = Field(default=False, validation_alias="AUTH_ENABLED")
    jwt_secret: str | None = Field(default=None, validation_alias="JWT_SECRET")
    jwt_expires_minutes: int = Field(default=60, validation_alias="JWT_EXPIRES_MINUTES")
    # Seat HTTP badge（approval-spec §3.2.1 / ADR-0002）：人工 approve/reject 的内部服务口令
    seat_approval_secret: str | None = Field(default=None, validation_alias="SEAT_APPROVAL_SECRET")
    # LangSmith 观测（architecture.md §8.1 tracing → LangSmith；SDK 只认 os.environ，
    # pydantic-settings 不导出 .env，故由 _sync_langsmith_env 显式回写。默认关闭。）
    langsmith_tracing: bool = Field(default=False, validation_alias="LANGSMITH_TRACING")
    langsmith_api_key: str | None = Field(default=None, validation_alias="LANGSMITH_API_KEY")
    langsmith_project: str | None = Field(default=None, validation_alias="LANGSMITH_PROJECT")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    _sync_langsmith_env(settings)
    return settings


def _sync_langsmith_env(settings: Settings, environ: dict | None = None) -> None:
    """LangSmith SDK 从 os.environ 读取配置；setdefault 保证真实 shell 环境变量优先。

    tracing 关闭时不写任何 LANGSMITH_* 变量（默认零行为变化）。
    """
    environ = os.environ if environ is None else environ
    if not settings.langsmith_tracing:
        return
    environ.setdefault("LANGSMITH_TRACING", "true")
    if settings.langsmith_api_key:
        environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    if settings.langsmith_project:
        environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)


def _assert_model_timeout_under_node(model_timeout: float, node_timeout: float, label: str) -> None:
    if model_timeout >= node_timeout:
        raise ValueError(
            f"{label}: model HTTP timeout ({model_timeout}s) must be < "
            f"node TimeoutPolicy ({node_timeout}s)"
        )


def _openai_compat_model(
    model_name: str,
    *,
    temperature: float,
    timeout: float,
    max_tokens: int,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ChatOpenAI:
    """OpenAI 兼容端点（SiliconFlow / ARK）；关闭 thinking；SDK 不叠乘图级 Retry。"""
    settings = get_settings()
    return ChatOpenAI(
        model=model_name,
        api_key=api_key if api_key is not None else settings.model_api_key,
        base_url=base_url if base_url is not None else settings.model_endpoint,
        temperature=temperature,
        timeout=timeout,
        max_tokens=max_tokens,
        max_retries=0,
        # Runtime LLM nodes: always non-thinking (SiliconFlow DeepSeek/Qwen).
        # DeepEval judge enables thinking separately in evals.deepeval_read.
        extra_body={
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )


def _llm_profile() -> tuple[str, str | None, str | None]:
    """非 Router LLM 节点凭证：LLM_* 优先，否则回落 ARK_*。"""
    settings = get_settings()
    name = (settings.llm_model_name or "").strip() or settings.model_name
    endpoint = (settings.llm_model_endpoint or "").strip() or settings.model_endpoint
    api_key = (settings.llm_model_api_key or "").strip() or settings.model_api_key
    return name, endpoint, api_key


def _router_profile() -> tuple[str, str | None, str | None]:
    """Router 主模型凭证：ROUTER_* → LLM_* → ARK_*。"""
    settings = get_settings()
    name = (settings.router_model_name or "").strip() or settings.model_name
    endpoint = (
        (settings.router_model_endpoint or "").strip()
        or (settings.llm_model_endpoint or "").strip()
        or settings.model_endpoint
    )
    api_key = (
        (settings.router_model_api_key or "").strip()
        or (settings.llm_model_api_key or "").strip()
        or settings.model_api_key
    )
    return name, endpoint, api_key


def make_read_model() -> ChatOpenAI:
    """ReadGraph 生产模型（LLM_MODEL / SiliconFlow，或回落 ARK_MODEL）。"""
    settings = get_settings()
    _assert_model_timeout_under_node(
        settings.llm_model_timeout_seconds,
        settings.llm_node_timeout_seconds,
        "read",
    )
    name, endpoint, api_key = _llm_profile()
    return _openai_compat_model(
        name,
        temperature=settings.read_temperature,
        timeout=settings.llm_model_timeout_seconds,
        max_tokens=settings.read_max_tokens,
        api_key=api_key,
        base_url=endpoint,
    )


def make_model() -> ChatOpenAI:
    """兼容别名 → make_read_model()。"""
    return make_read_model()


def make_router_model() -> ChatOpenAI:
    """Router 主模型（ROUTER_MODEL，凭证见 _router_profile）。"""
    settings = get_settings()
    _assert_model_timeout_under_node(
        settings.router_model_timeout_seconds,
        settings.router_node_timeout_seconds,
        "router",
    )
    name, endpoint, api_key = _router_profile()
    return _openai_compat_model(
        name,
        temperature=settings.router_temperature,
        timeout=settings.router_model_timeout_seconds,
        max_tokens=settings.router_max_tokens,
        api_key=api_key,
        base_url=endpoint,
    )


def make_router_fallback_model() -> ChatOpenAI | None:
    """Router 主模型失败时的兜底；ROUTER_FALLBACK_MODEL 未设则返回 None。"""
    settings = get_settings()
    name = (settings.router_fallback_model_name or "").strip()
    if not name:
        return None
    primary_name, endpoint, api_key = _router_profile()
    if name == primary_name:
        return None
    _assert_model_timeout_under_node(
        settings.router_model_timeout_seconds,
        settings.router_node_timeout_seconds,
        "router_fallback",
    )
    return _openai_compat_model(
        name,
        temperature=settings.router_temperature,
        timeout=settings.router_model_timeout_seconds,
        max_tokens=settings.router_max_tokens,
        api_key=api_key,
        base_url=endpoint,
    )


def make_select_model() -> ChatOpenAI:
    """Mutation / HighRisk select_llm 专用模型。"""
    settings = get_settings()
    _assert_model_timeout_under_node(
        settings.llm_model_timeout_seconds,
        settings.llm_node_timeout_seconds,
        "select",
    )
    name, endpoint, api_key = _llm_profile()
    return _openai_compat_model(
        name,
        temperature=settings.select_temperature,
        timeout=settings.llm_model_timeout_seconds,
        max_tokens=settings.select_max_tokens,
        api_key=api_key,
        base_url=endpoint,
    )


def make_fallback_model() -> ChatOpenAI:
    """Fallback explain_llm 专用模型。"""
    settings = get_settings()
    _assert_model_timeout_under_node(
        settings.llm_model_timeout_seconds,
        settings.llm_node_timeout_seconds,
        "fallback",
    )
    name, endpoint, api_key = _llm_profile()
    return _openai_compat_model(
        name,
        temperature=settings.fallback_temperature,
        timeout=settings.llm_model_timeout_seconds,
        max_tokens=settings.fallback_max_tokens,
        api_key=api_key,
        base_url=endpoint,
    )
