from ai_service_agent.config import Settings


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.db_path == "data/ai_service.db"
    assert s.fake_model is False
    assert s.router_conf_threshold == 0.6
    assert "转人工" in s.handoff_keywords

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


def test_chatwoot_settings_alias(monkeypatch):
    from ai_service_agent.config import Settings
    monkeypatch.setenv("CHATWOOT_ENABLED", "true")
    monkeypatch.setenv("CHATWOOT_BASE_URL", "http://localhost:3000")
    monkeypatch.setenv("CHATWOOT_ACCOUNT_ID", "3")
    monkeypatch.setenv("CHATWOOT_INBOX_ID", "2")
    monkeypatch.setenv("CHATWOOT_API_TOKEN", "tok")
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "sec")
    s = Settings(_env_file=None)
    assert s.chatwoot_enabled is True
    assert s.chatwoot_base_url == "http://localhost:3000"
    assert s.chatwoot_account_id == 3
    assert s.chatwoot_inbox_id == 2
    assert s.chatwoot_api_token == "tok"
    assert s.chatwoot_webhook_secret == "sec"
