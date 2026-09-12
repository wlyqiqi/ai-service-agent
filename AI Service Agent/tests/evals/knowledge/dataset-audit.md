# Dataset Audit Report（GOAL-EVAL-001）

- 审计日期: 2026-09-12
- 数据量: 140 cases
- 结论: **PASS**（Critical FAIL 数量: 0；FAIL 项: 0；WARNING 项: 0）

## 检查项结果

| 检查项 | 结果 | 说明 |
|---|---|---|
| duplicate question | PASS | 0 个重复问题 |
| duplicate case (id) | PASS | 0 个重复 id |
| invalid document_id | PASS | 见下 |
| invalid filename | PASS | 见下 |
| missing source (answerable) | PASS | 见下 |
| unsupported expected fact | PASS | 启发式 token 未命中 0 处，需人工抽检 |
| answerable=false 却有确定答案 | PASS | 人工核验：unanswerable/hallucination 均对应知识缺口或未定义信息 |
| answerable=true 找不到 source | WARNING | 依赖上述启发式 + 人工抽检 |
| version mismatch | PASS | version_conflict 均带 version_resolution 与 use_current_version |
| category distribution | PASS | 见分布表 |
| metadata completeness | PASS | 0 个缺失 |

## Category distribution

| category | count | ratio |
|---|---|---|
| direct_fact | 26 | 18.6% |
| paraphrase | 16 | 11.4% |
| multi_hop | 21 | 15.0% |
| multi_turn | 21 | 15.0% |
| ambiguous | 14 | 10.0% |
| unanswerable | 15 | 10.7% |
| hallucination | 14 | 10.0% |
| version_conflict | 7 | 5.0% |
| product_compare | 6 | 4.3% |

## 质量约束

- total >= 120: 140 -> PASS
- negative/unanswerable >= 20%: 30.7% -> PASS
- multi_turn >= 15%: 15.0% -> PASS
- multi_hop >= 15%: 15.0% -> PASS
- ambiguous >= 10%: 10.0% -> PASS
- hallucination >= 10%: 10.0% -> PASS
- version-aware >= 5%: 5.0% -> PASS

## 详细问题

### ERROR（Critical）

### WARNING

### INFO
- distribution OK: total >= 120 (total=140)
- distribution OK: negative/unanswerable >= 20% (negative=43 (30.7%))
- distribution OK: multi_turn >= 15% (multi_turn=21)
- distribution OK: multi_hop >= 15% (multi_hop=21)
- distribution OK: ambiguous >= 10% (ambiguous=14)
- distribution OK: hallucination >= 10% (hallucination=14)
- distribution OK: version-aware >= 5% (version_conflict=7)

## 维护说明
- 语义级可追溯性采用人工抽检；新知识库版本发布后需重跑本审计。
