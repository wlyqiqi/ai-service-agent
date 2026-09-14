from ai_service_agent.config import Settings


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.db_path == "data/ai_service.db"
    assert s.fake_model is False
    assert s.router_conf_threshold == 0.6
    assert "转人工" in s.handoff_keywords
