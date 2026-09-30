import asyncio
import os

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def pytest_configure(config):
    os.environ["FAKE_MODEL"] = "true"
    os.environ["JEV_ENABLED"] = "false"
    os.environ["JEV_ROUTE_INTENT_ENABLED"] = "false"
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["CHATWOOT_ENABLED"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["TYPESAFE_API_KEY"] = ""
    os.environ["JWT_SECRET"] = ""


def ainvoke_graph(graph, *args, **kwargs):
    return asyncio.run(graph.ainvoke(*args, **kwargs))
