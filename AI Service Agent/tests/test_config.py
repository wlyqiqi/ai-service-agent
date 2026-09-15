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
