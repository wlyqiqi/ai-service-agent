from langchain_core.messages import AIMessage

from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.testing import ToolCallingFakeModel
from ai_service_agent.tools.kb import kb_search


def test_read_graph_answers_and_completes():
    model = ToolCallingFakeModel(responses=[AIMessage(content="查询结果：订单已发货。")])
    graph = make_read_graph(model, tools=[kb_search])
    out = graph.invoke({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["execution_status"] == "completed"
    assert "发货" in out["messages"][-1].content


def test_read_system_prompt_requires_source_citation():
    from ai_service_agent.prompts import READ_SYSTEM_PROMPT
    assert "来源" in READ_SYSTEM_PROMPT
    assert "（来源：" in READ_SYSTEM_PROMPT
    assert "未查询到" in READ_SYSTEM_PROMPT


def test_read_system_prompt_friendly_no_result_tone():
    """知识缺口回复要求专业客服口吻：如实告知 + 表达歉意/引导替代帮助，而非机械复述。"""
    from ai_service_agent.prompts import READ_SYSTEM_PROMPT
    assert "如实告知" in READ_SYSTEM_PROMPT
    assert "友好" in READ_SYSTEM_PROMPT
    assert "人工客服" in READ_SYSTEM_PROMPT
    assert "引导" in READ_SYSTEM_PROMPT
    assert "换个说法" in READ_SYSTEM_PROMPT
