import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "real_model_http_smoke", ROOT / "scripts" / "real_model_http_smoke.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from ai_service_agent.graph.workflows.copy import confirmation_summary


def test_summary_parts_reverse_maps_mutation_display_label():
    _summary_parts = _load_smoke_module()._summary_parts
    args = {"phone": "18600001111", "request_id": "req-1"}
    tool, parsed_args = _summary_parts(confirmation_summary("update_phone", args))

    assert tool == "update_phone"
    assert parsed_args == args


def test_summary_parts_preserves_high_risk_english_tool_id():
    _summary_parts = _load_smoke_module()._summary_parts
    summary = (
        "退款申请 ORD-PAID-1｜退款金额 88.0｜订单总额 99.0｜"
        "已占用退款 0.0｜商品：商品信息不可用｜申请人 u1｜状态 已付款"
    )
    tool, parsed_args = _summary_parts(summary)

    assert tool == "refund_apply"
    assert parsed_args == {"order_id": "ORD-PAID-1", "amount": 88.0}
