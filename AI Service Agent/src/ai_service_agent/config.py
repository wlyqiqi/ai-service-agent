from functools import lru_cache

from langchain_openai import ChatOpenAI
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_path: str = "data/ai_service.db"
    fake_model: bool = False
    # 真实运行走 ARK（OpenAI 兼容）：字段与 .env 的 ARK_* 对齐
    model_name: str = Field(default="deepseek-v4-flash", validation_alias="ARK_MODEL")
    model_endpoint: str | None = Field(default=None, validation_alias="ARK_BASE_URL")
    model_api_key: str | None = Field(default=None, validation_alias="ARK_API_KEY")
    router_conf_threshold: float = 0.6
    handoff_keywords: list[str] = ["转人工", "人工客服", "找真人", "人工"]
    # P3（D2/D4/OQ-1）：Guard v1 过渡规则（关键词/正则，非终态）；确认/取消关键词；审批有效期
    guard_patterns: list[str] = [
        r"忽略.*规则", r"绕过审批", r"不需要审批", r"不用审批", r"直接执行",
        r"系统管理员", r"admin", r"查询.*标记为已退款", r"投诉到封",
    ]
    confirm_keywords: list[str] = ["确认", "同意", "好的", "可以", "是", "没错"]
    cancel_keywords: list[str] = ["取消", "算了", "不用了", "不要", "否"]
    approval_expires_minutes: int = 30
    simulate_approval: bool = False
    # P4（D1）：Clarify 最大澄清轮次，超限转 fallback
    clarify_max_turns: int = 2
    # Chatwoot 坐席端（API inbox；仅人工车道启用；未配置则 chatwoot_enabled=False）
    chatwoot_enabled: bool = False
    chatwoot_base_url: str | None = Field(default=None, validation_alias="CHATWOOT_BASE_URL")
    chatwoot_account_id: int | None = Field(default=None, validation_alias="CHATWOOT_ACCOUNT_ID")
    chatwoot_inbox_id: int | None = Field(default=None, validation_alias="CHATWOOT_INBOX_ID")
    chatwoot_api_token: str | None = Field(default=None, validation_alias="CHATWOOT_API_TOKEN")
    chatwoot_webhook_secret: str | None = Field(default=None, validation_alias="CHATWOOT_WEBHOOK_SECRET")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def make_model() -> ChatOpenAI:
    """生产模型：ARK（OpenAI 兼容，走 config）。"""
    settings = get_settings()
    return ChatOpenAI(
        model=settings.model_name,
        api_key=settings.model_api_key,
        base_url=settings.model_endpoint,
    )
