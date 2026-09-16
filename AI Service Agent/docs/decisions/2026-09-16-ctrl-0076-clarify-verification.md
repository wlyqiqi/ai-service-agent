# CT-RL-0076 澄清验证收尾决策（2026-09-16）

- 状态：已审核通过；维持 Guard v1、不扩 P5 功能范围
- 关联：`docs/specs/graph-spec.md` §5/§6.2/§6.3 · `docs/specs/event-spec.md` §2（clarification_required）·
  `docs/plans/2026-09-15-p4-clarify-precheck-resume.md`（D1/D8/OQ-3）·
  `docs/decisions/2026-09-15-p3-design-decisions.md`（P3 Open Question：CTRL-0076）

## 背景
- CTRL-0076（"退了吧"）：真实模型判 `order/high_risk/confidence=0.90`，原始风险为"模糊指令高置信
  触发不该发生的审批流"。
- P4 D1 已把拦截点落到 select 参数完备性：缺必填（order_id）→ `clarify_missing` → clarify 节点
  （普通轮非 interrupt），在审批闸之前拦截。
- P4 D8/OQ-3 已把数据集语义改为 `expected_policy=clarify` 真实断言（master.jsonl）。

## 决策
- **D1（采纳）**：不新增 Router confidence-based clarify gate。0076 的实际风险已由 select 参数
  完备性 Clarify 消解；置信度 0.90 无法被任何"中置信带"捕获，信号不可靠。
- **D2（采纳）**：验证补齐——真机冒烟单轮"退了吧"（`clarification_required` 出现、
  `approval_required` 不出现）；离线跨 turn 链路断言（澄清 → 补订单号 → 下一轮正常进 HighRisk；
  `clarify_count` 跨 turn 保留、完整 select 清零；超 `clarify_max_turns=2` → fallback）。
- **D3（采纳为已知风险，不实现）**：必填参数来源校验（值必须出自用户原文）暂不实现；
  "模型可能编造必填参数导致 select 放行"登记为残余风险；真机冒烟 0076 场景作为探针
  （若真实模型编造 order_id 放行 → 冒烟 FAIL 暴露此风险）。
- **D4（DEFERRED）**：`(low_conf/unknown, high_risk)` 的 Policy 归宿不进入本轮；维持当前
  fail-closed fallback 行为，作为独立 OQ，后续用 eval 数据单独决定 fallback / clarify / reject。

## 数据漂移注记
- `tests/evals/control/routing.jsonl` 的 CTRL-0076 仍是旧 fallback 语义（与 master.jsonl 矛盾）；
  该文件为 legacy 数据集，runner 只消费 master.jsonl，仅 `dataset-audit.md` 引用计数；
  不改其数据，标注为历史副本。

## 涉及文件（本计划）
- 决策：本文件；验证：`tests/test_clarify.py`、`scripts/real_model_http_smoke.py`、`docs/real-model-smoke.md`。