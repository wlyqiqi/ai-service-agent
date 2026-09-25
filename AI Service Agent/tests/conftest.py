import asyncio

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def ainvoke_graph(graph, *args, **kwargs):
    return asyncio.run(graph.ainvoke(*args, **kwargs))
