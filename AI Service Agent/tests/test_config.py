from ai_service_agent.config import Settings, get_settings


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
    assert r"绕过审批" in s.guard_patterns

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
    assert s.lightrag_include_references is True
    assert s.lightrag_enable_rerank is False


def test_route_intent_feature_defaults_off(monkeypatch):
    monkeypatch.delenv("JEV_ROUTE_INTENT_ENABLED", raising=False)
    settings = Settings(_env_file=None)
    assert settings.route_intent_enabled is False


def test_route_intent_feature_can_be_enabled(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().route_intent_enabled is True
    finally:
        get_settings.cache_clear()
