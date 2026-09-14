from functools import lru_cache

from langchain_openai import ChatOpenAI
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_path: str = "data/ai_service.db"
    fake_model: bool = False
    model_name: str = "openai-compatible"
    model_endpoint: str | None = None
    model_api_key: str | None = None
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
