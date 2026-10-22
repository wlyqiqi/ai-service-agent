from ai_service_agent.config import Settings, get_settings


def test_model_harness_settings_defaults():
    s = Settings(_env_file=None)
    assert s.router_max_tokens == 256
    assert s.select_max_tokens == 512
    assert s.read_max_tokens == 1024
    assert s.fallback_max_tokens == 512
    assert s.router_temperature == 0.0
    assert s.select_temperature == 0.0
    assert s.read_temperature == 0.3
    assert s.fallback_temperature == 0.3
    assert s.router_model_timeout_seconds == 5.0
    assert s.llm_model_timeout_seconds == 10.0
    assert s.router_model_timeout_seconds < s.router_node_timeout_seconds
    assert s.llm_model_timeout_seconds < s.llm_node_timeout_seconds


def test_model_profile_factories(monkeypatch):
    import ai_service_agent.config as config_mod

    settings = Settings(
        _env_file=None,
        model_api_key="ark-test",
        model_endpoint="https://ark.cn-beijing.volces.com/api/v3",
        model_name="deepseek-v4-flash",
        router_model_name="Qwen/Qwen3.6-35B-A3B",
        router_fallback_model_name="deepseek-ai/DeepSeek-V4-Flash",
        llm_model_api_key="sk-test",
        llm_model_endpoint="https://api.siliconflow.cn/v1",
        llm_model_name="deepseek-ai/DeepSeek-V4-Flash",
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)

    router = config_mod.make_router_model()
    router_fb = config_mod.make_router_fallback_model()
    select = config_mod.make_select_model()
    read = config_mod.make_read_model()
    fallback = config_mod.make_fallback_model()

    assert router.temperature == 0.0
    assert router.max_tokens == 256
    assert router.max_retries == 0
    assert router.extra_body["enable_thinking"] is False
    assert router.extra_body["chat_template_kwargs"]["enable_thinking"] is False
    assert str(router.openai_api_base).startswith("https://api.siliconflow.cn")
    assert router.model_name == "Qwen/Qwen3.6-35B-A3B"
    assert router_fb is not None
    assert router_fb.model_name == "deepseek-ai/DeepSeek-V4-Flash"
    assert router_fb.extra_body["enable_thinking"] is False
    assert str(router_fb.openai_api_base).startswith("https://api.siliconflow.cn")

    assert select.temperature == 0.0
    assert select.max_tokens == 512
    assert read.temperature == 0.3
    assert read.max_tokens == 1024
    assert fallback.temperature == 0.3
    assert fallback.max_tokens == 512
    assert str(read.openai_api_base).startswith("https://api.siliconflow.cn")
    assert read.model_name == "deepseek-ai/DeepSeek-V4-Flash"
    assert select.model_name == read.model_name
    assert fallback.model_name == read.model_name
    # All runtime LLM-node profiles keep think mode off
    for m in (select, read, fallback):
        assert m.extra_body["enable_thinking"] is False
        assert m.extra_body["chat_template_kwargs"]["enable_thinking"] is False
    assert config_mod.make_model().max_tokens == read.max_tokens


def test_model_timeout_must_be_under_node_timeout(monkeypatch):
    import ai_service_agent.config as config_mod
    import pytest

    settings = Settings(
        _env_file=None,
        model_api_key="sk-test",
        model_endpoint="https://api.siliconflow.cn/v1",
        router_model_timeout_seconds=6.0,
        router_node_timeout_seconds=6.0,
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    with pytest.raises(ValueError, match="router"):
        config_mod.make_router_model()


def test_langsmith_env_sync_enabled():
    from ai_service_agent.config import _sync_langsmith_env

    env: dict = {}
    s = Settings(
        _env_file=None,
        langsmith_tracing=True,
        langsmith_api_key="test-key",
        langsmith_project="test-project",
    )
    _sync_langsmith_env(s, environ=env)
    assert env["LANGSMITH_TRACING"] == "true"
    assert env["LANGSMITH_API_KEY"] == "test-key"
    assert env["LANGSMITH_PROJECT"] == "test-project"


def test_langsmith_env_sync_disabled_sets_nothing():
    from ai_service_agent.config import _sync_langsmith_env

    env: dict = {}
    s = Settings(_env_file=None, langsmith_tracing=False)
    _sync_langsmith_env(s, environ=env)
    assert "LANGSMITH_TRACING" not in env
    assert "LANGSMITH_API_KEY" not in env
    assert "LANGSMITH_PROJECT" not in env


def test_langsmith_shell_env_takes_precedence():
    from ai_service_agent.config import _sync_langsmith_env

    env = {"LANGSMITH_API_KEY": "shell-key"}
    s = Settings(
        _env_file=None,
        langsmith_tracing=True,
        langsmith_api_key="env-file-key",
    )
    _sync_langsmith_env(s, environ=env)
    assert env["LANGSMITH_API_KEY"] == "shell-key"
    assert env["LANGSMITH_TRACING"] == "true"


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("FAKE_MODEL", raising=False)
    monkeypatch.delenv("ROUTER_CONF_THRESHOLD", raising=False)
    s = Settings(_env_file=None)
    assert s.db_path == "data/ai_service.db"
    assert s.fake_model is False
    assert s.router_conf_threshold == 0.6
    assert "转人工" in s.handoff_phrases
    assert "培训" in s.handoff_negative_suffix

def test_settings_p3_defaults():
    s = Settings(_env_file=None)
    assert s.approval_expires_minutes == 30
    assert s.simulate_approval is False
    assert "确认" in s.confirm_keywords
    assert "取消" in s.cancel_keywords
    assert r"绕过审批" in s.guard_rule_patterns["approval_bypass"]
    assert "直接执行" not in str(s.guard_rule_patterns)
    assert "投诉到封" not in str(s.guard_rule_patterns)

def test_chatwoot_settings_defaults(monkeypatch):
    from ai_service_agent.config import Settings
    for k in ("CHATWOOT_ENABLED", "CHATWOOT_BASE_URL", "CHATWOOT_ACCOUNT_ID",
              "CHATWOOT_INBOX_ID", "CHATWOOT_API_TOKEN", "CHATWOOT_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.chatwoot_enabled is False
    assert s.chatwoot_base_url is None
    assert s.chatwoot_account_id is None
    assert s.chatwoot_inbox_id is None
    assert s.chatwoot_api_token is None
    assert s.chatwoot_webhook_secret is None

def test_lightrag_defaults():
    s = Settings(_env_file=None)
    assert s.lightrag_base_url == "http://localhost:9621"
    assert s.lightrag_mode == "naive"
    assert s.lightrag_top_k == 5
    assert s.lightrag_chunk_top_k == 5
    assert s.lightrag_include_references is True
    assert s.lightrag_enable_rerank is False
    assert s.lightrag_max_chunk_chars == 1200
    assert s.lightrag_max_context_chars == 4000
