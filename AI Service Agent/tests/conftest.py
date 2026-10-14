import asyncio
import os

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def pytest_configure(config):
    os.environ["FAKE_MODEL"] = "true"
    os.environ["JEV_ENABLED"] = "false"
    # Explicit opt-out. Production Settings.auth_enabled defaults to True.
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["CHATWOOT_ENABLED"] = "false"
    os.environ["CHATWOOT_BASE_URL"] = ""
    os.environ["CHATWOOT_API_TOKEN"] = ""
    os.environ["CHATWOOT_WEBHOOK_SECRET"] = ""
    os.environ["CHATWOOT_AGENT_BOT_SECRET"] = ""
    os.environ["CHATWOOT_HMAC_SECRET"] = ""
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["TYPESAFE_API_KEY"] = ""
    os.environ["JWT_SECRET"] = ""


def ainvoke_graph(graph, *args, **kwargs):
    return asyncio.run(graph.ainvoke(*args, **kwargs))
