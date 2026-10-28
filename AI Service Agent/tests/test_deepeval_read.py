"""Unit tests for Read quality-plane helpers (no live LLM / DeepEval judge)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_service_agent.evals import deepeval_read as dr

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "tests" / "evals" / "knowledge"


def test_load_default_phase1_categories():
    cases = dr.load_knowledge_cases(
        KNOWLEDGE,
        categories=("direct_fact", "paraphrase", "multi_hop"),
        answerable_only=True,
    )
    cats = {c["category"] for c in cases}
    assert cats == {"direct_fact", "paraphrase", "multi_hop"}
    assert len(cases) == 26 + 16 + 21
    assert all(c.get("answerable", True) for c in cases)


def test_load_ids_and_limit(tmp_path):
    p = tmp_path / "direct_fact.jsonl"
    rows = [
        {
            "id": "KB-T1",
            "category": "direct_fact",
            "question": "Q1",
            "answerable": True,
            "expected_facts": ["a"],
        },
        {
            "id": "KB-T2",
            "category": "direct_fact",
            "question": "Q2",
            "answerable": True,
            "expected_facts": ["b"],
        },
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    got = dr.load_knowledge_cases(tmp_path, categories=("direct_fact",), ids={"KB-T2"})
    assert [c["id"] for c in got] == ["KB-T2"]
    limited = dr.load_knowledge_cases(tmp_path, categories=("direct_fact",), limit=1)
    assert len(limited) == 1


def test_l0_rejects_empty_tech_greeting():
    assert dr.check_l0("")[0] is False
    assert dr.check_l0("抱歉，系统遇到技术问题，请稍后再试。")[0] is False
    assert dr.check_l0("您好！有什么可以帮您？")[0] is False
    ok, reason = dr.check_l0("JoyAir A1 炸篮容量为 4.5L。（来源：JoyAir_A1智能空气炸锅.md）")
    assert ok is True
    assert reason == "ok"


def test_l1_facts_requires_numbers_and_tokens():
    answer = "JoyAir A1 Pro 的额定功率是 1700 W，整机保修 12 个月。"
    ok, hit, miss = dr.check_l1_facts(answer, ["JoyAir A1 Pro 额定功率 1700 W"])
    assert ok is True
    assert miss == []
    assert hit

    ok2, _, miss2 = dr.check_l1_facts(answer, ["JoyAir A1 Pro 额定功率 2100 W"])
    assert ok2 is False
    assert miss2


def test_l1_facts_chinese_phrasing_variant():
    """Multi-hop style: keyword overlap across split sentences (not full NL entailment)."""
    answer = (
        "JoyClean V1 的 HEPA 滤网建议每 6 个月更换一次。"
        "滤网属于易耗件，正常损耗不在保修范围内。"
    )
    ok, hit, miss = dr.check_l1_facts(
        answer,
        ["V1 HEPA 滤网每 6 个月更换", "滤网等易耗件不在保修范围"],
    )
    assert ok is True, (hit, miss)
    assert miss == []


SEMANTIC_FACT_CASES = [
    (
        "KB-0016-address-count",
        {
            "label": "每笔订单限修改地址 1 次",
            "all_of": [
                {"any_of": ["每笔订单", "每个订单"]},
                {"any_of": ["修改收货地址", "修改地址"]},
                {"any_of": ["限", "只能", "仅可"]},
            ],
            "numbers": ["1"],
        },
        "根据我们的订单政策，每笔订单限修改收货地址 1 次。",
        True,
    ),
    (
        "KB-0016-pending-status",
        {
            "label": "仅待发货状态可修改地址",
            "all_of": [
                {"any_of": ["待发货", "待发货状态", "待发货时"]},
                {"any_of": ["修改地址", "修改收货地址", "申请修改"]},
                {"any_of": ["可申请修改", "可以申请修改", "才可以修改", "才可修改"]},
            ],
            "must_not": [r"已发货后(?:仍|也)?可(?:以)?自行修改"],
        },
        "订单处于「待发货」状态时可以申请修改；订单已发货后不可自行修改。",
        True,
    ),
    (
        "KB-0020-repair-window",
        {
            "label": "检测与维修合计 5-7 个工作日，不含物流在途",
            "all_of": [
                {"any_of": ["完成检测和维修", "检测与维修", "检测和维修"]},
                {"any_of": ["不含物流在途", "不含物流时间"]},
                {"any_of": ["个工作日", "工作日"]},
            ],
            "numbers": ["5", "7"],
        },
        "送修后一般需要 5–7 个工作日完成检测和维修（不含物流在途时间）。",
        True,
    ),
    (
        "KB-0020-parts-delay",
        {
            "label": "配件缺货延期由客服告知并更新预计时间",
            "all_of": [
                {"any_of": ["配件缺货", "缺配件"]},
                {"any_of": ["客服会主动告知", "客服告知", "客服通知"]},
                {"any_of": ["更新预计完成时间", "更新预计时间", "更新预计时效"]},
            ],
        },
        "如果因配件缺货导致延期，客服会主动告知您并更新预计完成时间。",
        True,
    ),
    (
        "KB-0025-proof",
        {
            "label": "保修凭证为订单号或发票任选其一",
            "all_of": [
                {"any_of": ["订单号", "发票"]},
                {"any_of": ["订单号或发票", "订单号、发票"]},
                {"any_of": ["任选其一", "任选一个", "二者选一"]},
            ],
        },
        "保修时提供订单号或发票，任选其一即可。",
        True,
    ),
    (
        "KB-0025-no-proof-approval",
        {
            "label": "无凭证需按审批流程处理",
            "all_of": [
                {"any_of": ["无凭证", "无法提供凭证", "两者都无法提供"]},
                {"any_of": ["流程处理", "按流程", "进入审批"]},
                {"any_of": ["需要额外审批", "需审批", "需要审批", "须审批"]},
            ],
            "must_not": [r"无凭证.{0,20}(?:无需|不需要|不必).{0,8}审批"],
        },
        "如果两者都无法提供，会按“无凭证”流程处理，需要额外审批。",
        True,
    ),
    (
        "KB-0029-wifi-band",
        {
            "label": "JoyClean S1 配网与地图需要 2.4 GHz Wi-Fi",
            "all_of": [
                {"any_of": ["JoyClean S1", "S1"]},
                {"any_of": ["配网", "连接 Wi-Fi", "联网"]},
                {"any_of": ["地图功能", "地图"]},
            ],
            "numbers": ["2.4"],
        },
        "JoyClean S1 配网和地图功能都必须使用 2.4 GHz 网络才能正常完成。",
        True,
    ),
    (
        "KB-0029-no-5g",
        {
            "label": "产品仅支持 2.4 GHz 不支持 5 GHz",
            "all_of": [
                {"any_of": ["仅支持 2.4", "只支持 2.4", "必须使用 2.4"]},
                {"any_of": ["不支持 5", "不兼容 5", "无法使用 5"]},
            ],
            "numbers": ["2.4", "5"],
        },
        "JoyClean S1 仅支持 2.4 GHz Wi-Fi，不支持 5 GHz。",
        True,
    ),
    (
        "KB-0048-refuse",
        {
            "label": "已发货未签收可拒收",
            "all_of": [
                {"any_of": ["已发货", "发货后"]},
                {"any_of": ["未签收", "尚未签收", "没有签收"]},
                {"any_of": ["拒收", "拒绝签收"]},
            ],
        },
        "已发货（未签收）的订单，您可以选择拒收包裹。",
        True,
    ),
    (
        "KB-0048-return-refund",
        {
            "label": "包裹退回后按退款流程处理",
            "all_of": [
                {"any_of": ["包裹退回后", "退回后", "退件后"]},
                {"any_of": ["退款流程", "按退款处理", "进入退款"]},
            ],
        },
        "包裹退回后按退款流程处理。",
        True,
    ),
    (
        "KB-0048-carrier-fee",
        {
            "label": "已产生的承运费用在退款中扣除（如有）",
            "all_of": [
                {"any_of": ["承运费用", "物流费用", "运费"]},
                {"any_of": ["退款中扣除", "从退款扣除", "退款时扣除"]},
                {"any_of": ["如有", "若产生", "如产生"]},
            ],
        },
        "已产生的承运费用会在退款中扣除（如有）。",
        True,
    ),
    (
        "KB-0048-refund-timing",
        {
            "label": "退款到账时效按支付方式说明",
            "all_of": [
                {"any_of": ["退款到账时效", "到账时效", "退款时效"]},
                {"any_of": ["电子钱包", "钱包"]},
                {"any_of": ["银行卡", "银行处理"]},
                {"any_of": ["工作日", "到账时间"]},
            ],
            "numbers": ["3", "5", "10"],
        },
        "退款一律原路退回；到账时效：电子钱包约 3–5 个工作日，银行卡约 5–10 个工作日。",
        True,
    ),
    (
        "KB-0050-cross-border",
        {
            "label": "5 国任一购买的产品其余四国同等保修",
            "all_of": [
                {"any_of": ["五个销售国", "5 个销售国", "五国"]},
                {"any_of": ["任一销售国购买", "任一国家购买"]},
                {"any_of": ["其余四国", "其他四国"]},
                {"any_of": ["同等保修", "相同保修"]},
            ],
            "numbers": [],
        },
        "五个销售国实行跨境互保，任一销售国购买的产品其余四国均可享受同等保修。",
        True,
    ),
    (
        "KB-0055-presets",
        {
            "label": "R1 Pro 预设程序 12 个",
            "all_of": [
                {"any_of": ["R1 Pro", "JoyCook R1 Pro"]},
                {"any_of": ["预设程序", "预设"]},
            ],
            "numbers": ["12"],
        },
        "JoyCook R1 Pro 内置 12 个预设程序。",
        True,
    ),
    (
        "KB-0055-texture",
        {
            "label": "口感三档：偏软/标准/偏硬",
            "all_of": [
                {"any_of": ["口感三档", "三档口感", "口感"]},
                {"any_of": ["偏软"]},
                {"any_of": ["标准"]},
                {"any_of": ["偏硬"]},
            ],
        },
        "提供口感三档：偏软、标准、偏硬。",
        True,
    ),
    (
        "KB-0055-app-save",
        {
            "label": "口感调节需在 App 保存设置并选对应预设",
            "all_of": [
                {"any_of": ["App 中", "App 里", "App 内"]},
                {"any_of": ["保存设置", "保存口感", "保存"]},
                {"any_of": ["对应米种", "对应预设", "选择的预设"]},
            ],
        },
        "选择对应米种的预设程序后，在 App 里设置口感档位并保存，设置才会生效。",
        True,
    ),
    (
        "KB-0057-jar-capacity",
        {
            "label": "搅拌杯容量 1.75L",
            "all_of": [
                {"any_of": ["搅拌杯", "杯体"]},
                {"any_of": ["总容量", "容量"]},
            ],
            "numbers": ["1.75"],
        },
        "搅拌杯总容量为 1.75L。",
        True,
    ),
    (
        "KB-0057-hot-limit",
        {
            "label": "热饮液量上限 1.4L",
            "all_of": [
                {"any_of": ["热饮液量", "热饮"]},
                {"any_of": ["上限", "不超过", "不能超过", "最多"]},
            ],
            "numbers": ["1.4"],
            "must_not": [r"搅拌杯(?:总)?容量.{0,8}1\.4"],
        },
        "热饮液量不能超过 1.4L。",
        True,
    ),
    (
        "KB-0057-overflow",
        {
            "label": "限制目的是防止加热溢出",
            "all_of": [
                {"any_of": ["防止加热时溢出", "防止加热溢出", "避免溢出"]},
                {"any_of": ["沸腾", "溢出", "留出空间"]},
            ],
        },
        "加热过程中液体容易沸腾溢出，留出足够空间可以防止加热时溢出。",
        True,
    ),
    (
        "KB-0060-damage-liability",
        {
            "label": "运输破损由品牌承担",
            "all_of": [
                {"any_of": ["运输破损", "运输损坏"]},
                {"any_of": ["品牌方负责", "品牌承担", "由品牌负责"]},
            ],
        },
        "运输破损由品牌承担，您不需要承担损失。",
        True,
    ),
    (
        "KB-0060-remedy",
        {
            "label": "免费换新或退款，按退换货政策执行",
            "all_of": [
                {"any_of": ["免费换新", "换新"]},
                {"any_of": ["退款"]},
                {"any_of": ["退换货政策", "按退换货政策"]},
            ],
        },
        "可以免费换新或退款，按退换货政策执行。",
        True,
    ),
    (
        "KB-0060-inspection",
        {
            "label": "建议当面验货，破损可拒收并注明",
            "all_of": [
                {"any_of": ["当面验货", "当场验货"]},
                {"any_of": ["拒收"]},
                {"any_of": ["当场注明", "注明破损", "备注破损"]},
            ],
        },
        "建议当面验货，包装明显破损可以拒收并当场注明。",
        True,
    ),
    (
        "KB-0061-bluetooth",
        {
            "label": "JoyBrew C1 蓝牙 5.0 用于初始配对",
            "all_of": [
                {"any_of": ["JoyBrew C1", "C1"]},
                {"any_of": ["蓝牙", "Bluetooth"]},
                {"any_of": ["初始配对", "首次配对", "辅助配网", "辅助初始配对"]},
            ],
            "numbers": ["5.0"],
        },
        "JoyBrew C1 支持蓝牙 5.0 辅助初始配对。",
        True,
    ),
    (
        "negative-address-after-shipping",
        {
            "label": "仅待发货状态可修改地址",
            "all_of": [
                {"any_of": ["待发货"]},
                {"any_of": ["修改地址", "申请修改"]},
                {"any_of": ["可以", "可"]},
            ],
            "must_not": [r"已发货后(?:仍|也)?可(?:以)?自行修改"],
        },
        "待发货时可以申请修改，已发货后仍可自行修改。",
        False,
    ),
    (
        "negative-repair-window-includes-transit",
        {
            "label": "检测与维修合计 5-7 个工作日，不含物流在途",
            "all_of": [
                {"any_of": ["工作日"]},
                {"any_of": ["检测和维修", "检测与维修"]},
                {"any_of": ["不含物流在途"]},
            ],
            "numbers": ["5", "7"],
            "must_not": [r"5-7个工作日.{0,12}(?:包含|含)物流在途"],
        },
        "检测和维修需要 5–7 个工作日，包含物流在途时间。",
        False,
    ),
    (
        "negative-no-proof-without-approval",
        {
            "label": "无凭证需按审批流程处理",
            "all_of": [
                {"any_of": ["无凭证"]},
                {"any_of": ["流程处理", "按流程"]},
            ],
            "must_not": [r"无凭证.{0,20}(?:无需|不需要|不必).{0,8}审批"],
        },
        "无凭证按流程处理，无需审批。",
        False,
    ),
    (
        "negative-supports-5g",
        {
            "label": "产品仅支持 2.4 GHz 不支持 5 GHz",
            "all_of": [
                {"any_of": ["仅支持 2.4", "只支持 2.4", "必须使用 2.4"]},
                {"any_of": ["不支持 5", "不兼容 5", "无法使用 5"]},
            ],
            "numbers": ["2.4", "5"],
        },
        "JoyClean S1 支持 2.4 GHz 和 5 GHz Wi-Fi。",
        False,
    ),
    (
        "negative-refuse-after-signed",
        {
            "label": "已发货未签收可拒收",
            "all_of": [
                {"any_of": ["已发货"]},
                {"any_of": ["未签收"]},
                {"any_of": ["拒收"]},
            ],
            "must_not": [r"已签收.{0,12}(?:仍|也)?可(?:以)?拒收"],
        },
        "已发货未签收可以拒收；已签收仍可以拒收。",
        False,
    ),
    (
        "negative-cross-border-exclusion",
        {
            "label": "5 国任一购买的产品其余四国同等保修",
            "all_of": [
                {"any_of": ["五个销售国", "5 个销售国"]},
                {"any_of": ["其余四国", "其他四国"]},
            ],
            "numbers": ["5.0"],
            "must_not": [r"其余四国.{0,8}(?:不保修|不保|不享受)"],
        },
        "五个销售国中，其余四国不保修。",
        False,
    ),
    (
        "negative-app-not-required",
        {
            "label": "口感调节需在 App 保存设置并选对应预设",
            "all_of": [
                {"any_of": ["App"]},
                {"any_of": ["保存", "设置"]},
                {"any_of": ["对应预设", "米种预设"]},
            ],
            "must_not": [r"(?:无需|不需要|不必).{0,6}App"],
        },
        "口感调节无需 App，选择对应预设即可。",
        False,
    ),
    (
        "negative-jar-capacity",
        {
            "label": "搅拌杯容量 1.75L",
            "all_of": [{"any_of": ["搅拌杯", "杯体"]}, {"any_of": ["容量"]}],
            "numbers": ["1.75"],
        },
        "搅拌杯容量为 1.4L。",
        False,
    ),
    (
        "negative-bluetooth-music",
        {
            "label": "JoyBrew C1 蓝牙 5.0 用于初始配对",
            "all_of": [
                {"any_of": ["JoyBrew C1", "C1"]},
                {"any_of": ["蓝牙", "Bluetooth"]},
                {"any_of": ["初始配对", "首次配对", "辅助配网"]},
            ],
            "numbers": ["5"],
            "must_not": [r"蓝牙.{0,12}(?:仅|只).{0,8}(?:播放音乐|放音乐)"],
        },
        "JoyBrew C1 蓝牙 5.0 仅用于播放音乐。",
        False,
    ),
]


@pytest.mark.parametrize(
    ("case_id", "fact", "answer", "expected"),
    SEMANTIC_FACT_CASES,
    ids=[case[0] for case in SEMANTIC_FACT_CASES],
)
def test_structured_facts_accept_equivalent_wording_and_reject_contradictions(
    case_id, fact, answer, expected
):
    assert dr.fact_hit(fact, answer) is expected, case_id


def test_structured_fact_hit_and_miss_use_labels():
    spec = {
        "label": "仅待发货状态可修改地址",
        "all_of": [{"any_of": ["待发货"]}, {"any_of": ["修改地址"]}],
    }
    ok, hit, miss = dr.check_l1_facts(
        "待发货时可以申请修改地址。",
        [spec, {"label": "需要 L2 审批", "all_of": [{"any_of": ["L2"]}]}],
    )
    assert ok is False
    assert hit == ["仅待发货状态可修改地址"]
    assert miss == ["需要 L2 审批"]


def _load_master_cases() -> dict[str, dict]:
    path = KNOWLEDGE / "master.jsonl"
    return {
        row["id"]: row
        for row in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


def test_multi_hop_category_and_master_stay_in_sync():
    categories = {
        row["id"]: row
        for row in dr.load_knowledge_cases(KNOWLEDGE, categories=("multi_hop",))
    }
    master = _load_master_cases()
    for case_id in ("KB-0046", "KB-0048", "KB-0050", "KB-0055", "KB-0057", "KB-0060", "KB-0061"):
        assert categories[case_id] == master[case_id]


def test_multi_hop_expected_facts_do_not_exceed_question_scope():
    master = _load_master_cases()
    assert [fact["label"] for fact in master["KB-0046"]["expected_facts"]] == [
        "JoyBrew C1 建议每周除垢",
        "使用 DC-COMMON-01 除垢剂",
    ]
    assert "水路堵塞" not in master["KB-0046"]["expected_answer"]
    assert [fact["label"] for fact in master["KB-0061"]["expected_facts"]] == [
        "JoyBrew C1 蓝牙 5.0 用于初始配对",
    ]
    assert "2.4 GHz" not in master["KB-0061"]["expected_answer"]


@pytest.mark.parametrize(
    ("case_id", "fact_index", "answer"),
    [
        (
            "KB-0048",
            3,
            "退款方式与时效：电子钱包约 3–5 个工作日，银行卡约 5–10 个工作日到账。",
        ),
        (
            "KB-0050",
            0,
            "跨境互保覆盖五个市场，在任一销售国购买的产品，其余四国均可享受同等保修。",
        ),
        (
            "KB-0055",
            2,
            "您可以在 App 中调节口感档位，确认保存设置，并选择对应的米种预设。",
        ),
        (
            "KB-0057",
            2,
            "这是为了防止加热过程中液体溢出。",
        ),
        (
            "KB-0060",
            2,
            "如果包裹外包装有明显破损，建议签收时当场拒收并注明。",
        ),
    ],
)
def test_multi_hop_expected_facts_accept_run_observed_equivalent_wording(
    case_id: str, fact_index: int, answer: str
):
    case = _load_master_cases()[case_id]
    assert dr.fact_hit(case["expected_facts"][fact_index], answer) is True


def test_l1_abstention_for_unanswerable():
    case = {"answerable": False}
    good = dr.check_l1(case, "知识库未查询到槟城上门维修的明确信息，建议以官方渠道确认为准。")
    assert good["pass"] is True
    bad = dr.check_l1(case, "可以，槟城全部支持免费上门维修。")
    assert bad["pass"] is False


def test_decide_pass_phase1_faithfulness_hard():
    l1 = {"pass": True}
    metrics_ok = {
        "FaithfulnessMetric": {"score": 0.9, "success": True},
        "GEval_Citations": {"score": 0.2, "success": False},
    }
    ok, reasons = dr.decide_pass(
        l0_ok=True,
        l1=l1,
        metrics=metrics_ok,
        citations_hard=False,
        must_cite_source=True,
    )
    assert ok is True
    assert reasons == []

    ok2, reasons2 = dr.decide_pass(
        l0_ok=True,
        l1=l1,
        metrics=metrics_ok,
        citations_hard=True,
        must_cite_source=True,
    )
    assert ok2 is False
    assert "citations_fail" in reasons2


def test_metrics_to_run_hard_only_by_default():
    assert dr.metrics_to_run(soft_metrics=False, citations_hard=False) == frozenset(
        {"FaithfulnessMetric"}
    )
    assert dr.metrics_to_run(soft_metrics=True, citations_hard=False) == frozenset(
        {
            "FaithfulnessMetric",
            "AnswerRelevancyMetric",
            "GEval_Tone",
            "GEval_Citations",
        }
    )
    # citations-hard forces Citations even when soft metrics are off
    assert dr.metrics_to_run(soft_metrics=False, citations_hard=True) == frozenset(
        {"FaithfulnessMetric", "GEval_Citations"}
    )


def test_build_parser_soft_metrics_default_off():
    p = dr.build_parser()
    assert p.get_default("soft_metrics") is False
    assert p.get_default("pause_s") == 0.0
    ns = p.parse_args(["--soft-metrics"])
    assert ns.soft_metrics is True
    ns2 = p.parse_args(["--no-soft-metrics"])
    assert ns2.soft_metrics is False


def test_make_judge_model_enables_thinking_high(monkeypatch):
    # deepeval is an optional eval dependency; CI installs the offline baseline only.
    pytest.importorskip("deepeval.models")
    captured: dict = {}

    class _FakeLocal:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPEVAL_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("DEEPEVAL_JUDGE_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPEVAL_JUDGE_API_KEY", raising=False)
    monkeypatch.setattr(
        "deepeval.models.LocalModel",
        _FakeLocal,
    )
    monkeypatch.setattr(dr, "_load_env", lambda: {
        "LLM_MODEL": "deepseek-ai/DeepSeek-V4-Flash",
        "LLM_BASE_URL": "https://api.siliconflow.cn/v1",
        "LLM_API_KEY": "sk-test",
    })

    with pytest.warns(UserWarning, match="DEEPEVAL_JUDGE"):
        dr.make_judge_model()

    gen = captured.get("generation_kwargs") or {}
    body = gen.get("extra_body") or {}
    assert body.get("enable_thinking") is True
    assert body.get("reasoning_effort") == "high"
    assert captured.get("temperature") == 0


def test_measure_skips_soft_metrics_without_judge_calls(monkeypatch):
    """soft_metrics=False must not construct/measure soft DeepEval metrics."""
    # deepeval is an optional eval dependency; CI installs the offline baseline only.
    metrics_mod = pytest.importorskip("deepeval.metrics")

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError(f"unexpected metric ctor: {self.__class__.__name__}")

    class _FaithOk:
        def __init__(self, *args, **kwargs):
            self.score = 1.0
            self.success = True
            self.threshold = kwargs.get("threshold")
            self.reason = "ok"

        def measure(self, tc):
            return self.score

    monkeypatch.setattr(metrics_mod, "FaithfulnessMetric", _FaithOk)
    monkeypatch.setattr(metrics_mod, "AnswerRelevancyMetric", _Boom)
    monkeypatch.setattr(metrics_mod, "GEval", _Boom)

    out = dr.measure_deepeval_metrics(
        case={"question": "Q", "expected_answer": "A", "must_cite_source": True},
        actual_output="A",
        retrieval_context=["chunk"],
        judge=object(),
        faithfulness_threshold=0.7,
        relevancy_threshold=0.5,
        tone_threshold=0.5,
        citations_threshold=0.5,
        soft_metrics=False,
        citations_hard=False,
    )
    assert out["FaithfulnessMetric"]["success"] is True
    assert out["AnswerRelevancyMetric"]["skipped"] is True
    assert out["GEval_Tone"]["skipped"] is True
    assert out["GEval_Citations"]["skipped"] is True
    assert out["AnswerRelevancyMetric"]["reason"] == "soft_metrics_disabled"


def test_build_input_messages_includes_conversation_context():
    case = {
        "question": "还是连不上",
        "conversation_context": ["我的 JoyClean S1 连不上 Wi-Fi。", "我已经重启过设备了。"],
    }
    msgs = dr.build_input_messages(case)
    assert msgs[-1].content == "还是连不上"
    assert any(getattr(m, "content", None) == "我的 JoyClean S1 连不上 Wi-Fi。" for m in msgs)


def test_summarize_counts():
    summary = dr.summarize(
        [
            {"pass": True, "category": "direct_fact"},
            {"pass": False, "category": "direct_fact"},
            {"pass": True, "category": "multi_hop", "error": None},
        ]
    )
    assert summary["total"] == 3
    assert summary["pass"] == 2
    assert summary["fail"] == 1
    assert summary["by_category"]["direct_fact"]["pass"] == 1
