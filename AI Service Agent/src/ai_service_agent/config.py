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
