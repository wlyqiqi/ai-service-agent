"""占位串归一化：哨兵识别与参数剥离。"""

from ai_service_agent.tools.params import is_absent_sentinel, normalize_absent_args


def test_absent_sentinels_match_case_and_whitespace():
    for value in ("null", " NULL ", "None", "nil", "undefined", "n/a", "na", "", "   "):
        assert is_absent_sentinel(value)


def test_dash_and_real_values_are_kept():
    assert is_absent_sentinel("-") is False
    assert is_absent_sentinel("paid") is False
    assert is_absent_sentinel(None) is False
    assert is_absent_sentinel(0) is False


def test_normalize_absent_args_drops_sentinels_and_honors_skip():
    normalized = normalize_absent_args(
        {
            "status": "null",
            "note": "-",
            "request_id": "null",
            "amount": 0,
            "flag": False,
        },
        skip=frozenset({"request_id"}),
    )
    assert normalized == {
        "note": "-",
        "request_id": "null",
        "amount": 0,
        "flag": False,
    }
