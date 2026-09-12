# Knowledge Evaluation Dataset（知识评估数据集）

- Dataset version: 1.0
- 生成日期: 2026-09-12
- Knowledge base version: 产品主数据 v1.0（effective 2026-01-01）；政策文档 v1.0（effective 2026-09-12）
- 数据量: 140 cases（master.jsonl）
- Source document count: 34 份知识库文档（34 个文件路径）

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

总计 140。

## 用途

评估未来 AI Service Agent 知识问答能力：Retrieval Accuracy、Answer Correctness、Source Citation、
Multi-document Reasoning、Multi-turn Context Understanding、Abstention、Hallucination Resistance、
Version Awareness、Knowledge Gap Detection。

## Ground Truth 原则

- 每个 answerable case 的 expected_answer / expected_facts 均可从知识库文档验证（见 ground_truth_document_ids / ground_truth_sections）。
- answerable=false 的 case 不允许编造答案，expected_behavior 规定抽象行为。
- 版本类 case 使用 `07_版本与变更/版本关系说明.md` 中已登记的真实 supersedes 关系，未创造新版本冲突。

## Known limitations

- expected_facts 的机器可验证采用关键词级启发式（dataset-audit.md 中标注），语义级可追溯性需人工抽检。
- 知识库本身存在 2 项 Warning（GOAL-KB-004 审计：已发货订单改址口径、产品资料 effective_date 与版本生效日期错位），dataset 按知识库当前文本为准，未自行改档。
- 部分 unanswerable / hallucination case 依赖 `08_知识缺口/知识缺口说明.md`（GAP-001~010）与"未定义即不可答"原则；知识缺口关闭后需同步重审对应 case。
- 语言：zh-CN；region 默认 MY（各 case 的 metadata.region 单独标注）。
