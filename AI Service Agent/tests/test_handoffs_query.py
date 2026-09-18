"""GET /handoffs?conversation_id= 查询端点测试（镜像 GET /approvals，供页面/消费方观测 handoff 状态）。"""

from fastapi.testclient import TestClient
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.harness.server import create_app
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message


class _UnknownReadFakeModel(ToolCallingFakeModel):
    """Router 恒 unknown/read -> Fallback -> check_handoff 关键词触发转人工。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=router_json_message("unknown", "read", 0.5))])


def test_get_handoffs_by_conversation(tmp_path):
    db = str(tmp_path / "s.db")
    client = TestClient(create_app(db, model=_UnknownReadFakeModel(responses=[])))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    # 无 handoff -> 404
    assert client.get(f"/handoffs?conversation_id={sid}").status_code == 404

    # 触发转人工（fake model Router 恒 unknown/read -> Fallback -> check_handoff 关键词）
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as r:
        r.read()
    assert client.get(f"/handoffs?conversation_id={sid}").json()["status"] == "active"

    # resolve 后仍可查询，状态 resolved
    rec = client.get(f"/handoffs?conversation_id={sid}").json()
    assert client.post(f"/handoffs/{rec['handoff_id']}/resolve").status_code == 200
    assert client.get(f"/handoffs?conversation_id={sid}").json()["status"] == "resolved"
