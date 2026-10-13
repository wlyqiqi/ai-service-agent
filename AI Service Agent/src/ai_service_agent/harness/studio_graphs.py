"""LangGraph Studio 辅助入口：四个子图注册为独立 graph，供 Studio 查看内部结构。

背景（Studio 父图中子图节点无法下钻的根因）：
子图普遍通过 ``set_node_defaults(error_handler=...)`` 做节点级容错，该机制会在静态图里
产生孤立的 ``__default_error_handler__`` 节点；goto 容错节点的 ``Command(goto=...)``
入边是运行时动态边，静态图里容错节点（如 ``finalize_failed``）同样孤立。孤立节点使
子图静态图存在多个入度 0 / 出度 0 节点，langchain-core 的 ``Graph.first_node()`` /
``last_node()`` 因候选不唯一返回 ``None``，langgraph ``pregel/_draw.py`` 的 xray 替换
守卫随之静默跳过子图内联——Studio 父图上的子图节点因此只剩 {id, name}，无法展开。

本模块把子图作为独立 graph 注册（langgraph.json ``graphs`` 多条目），在 Studio 图选择器
中可直接查看每个子图的完整节点与边。构建参数与 ``harness/graph.py:make_ai_graph`` 保持
一致（同一 registry / tool_hooks / 分档 model，防止视图与运行时漂移）。
"""

from ai_service_agent.config import (
    get_settings,
    make_fallback_model,
    make_read_model,
    make_select_model,
)
from ai_service_agent.domain.orders import SQLiteOrderRepository
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.workflows.fallback import make_fallback_graph
from ai_service_agent.graph.workflows.high_risk_agent import make_high_risk_graph
from ai_service_agent.graph.workflows.mutation_agent import make_mutation_graph
from ai_service_agent.harness.tool_hooks import LoggingToolHook
from ai_service_agent.tools import make_tool_registry

_read_model = make_read_model()
_select_model = make_select_model()
_fallback_model = make_fallback_model()
_repository = SQLiteOrderRepository(get_settings().mock_ecommerce_db_path)
_reg = make_tool_registry(_repository)
_hooks = [LoggingToolHook()]

read_graph = make_read_graph(_read_model, _reg.for_risk_level("read"), tool_hooks=_hooks)
mutation_graph = make_mutation_graph(
    _select_model, _reg.for_risk_level("mutation"), _reg, tool_hooks=_hooks,
    order_repository=_repository,
)
high_risk_graph = make_high_risk_graph(
    _select_model, _reg.for_risk_level("high_risk"), _reg, tool_hooks=_hooks,
    order_repository=_repository,
)
fallback_graph = make_fallback_graph(_fallback_model)
