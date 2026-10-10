# Context Policy Evaluation Dataset

- Dataset version: 1.0
- 生成日期: 2026-10-09
- 数据量: 20 cases（`context_policy.jsonl`）
- 覆盖对象: Router / Fallback / ReadGraph / Mutation-HighRisk Select 的模型输入上下文策略

## 覆盖维度

| policy | previous_completed_turns | budget_tokens | 重点 |
|---|---:|---:|---|
| router | 1 | 800 | 只保留最近一轮可见对话和当前消息 |
| fallback | 2 | 800 | 只保留最近两轮可见对话和当前消息 |
| read | 2 | 2000 | 保留最近两轮可见对话，并保留当前轮 tool call / tool result |
| select | 1 | 1200 | 澄清后继续选择业务动作时只带最近一轮可见对话 |

## 数据结构

每个 case 包含：

- `policy`: 使用的 `ContextPolicy`
- `previous_completed_turns`: policy 当前定义的完成轮数量
- `budget_tokens`: 当前配置的总输入 token 预算
- `input_messages`: 原始 state messages 的抽象表示
- `expected_dynamic_context`: `policy.build_messages()` 的精确期望输出
- `expected_dynamic_context_constraints`: 预算裁剪场景下的行为约束
- `expected_behavior`: 语义级断言要点

预算类 case 额外包含 `system_prompt` / `tools`，用于验证 `ContextBudgetMiddleware`
把 system prompt 和 tools schema 一起计入总预算。

## 覆盖行为

- 保留最后 N 个 completed turns
- 丢弃更早的 completed turns
- 丢弃历史轮的 tool call / tool result
- 保留当前轮的 tool call / tool result
- 剥离 `<think>...</think>`
- 多条 AI message 组成的 completed turn 不丢消息
- 无 human message 时的边界行为
- `system prompt + tools schema + messages` 计入总预算
- `trim_messages` 不拆散 tool call / tool result 配对

## 运行方式

最小 runner 位于 `src/ai_service_agent/evals/context_policy.py`，pytest 会加载
本目录下的 golden cases 并逐条校验真实 `ContextPolicy` 和 `ContextBudgetMiddleware` 输出。

## 已知限制
- budget token 是估算值，实际 runner 应继续使用项目统一的
  `count_tokens_approximately`，如后续切换真 tokenizer，需要同步更新预算语义。
- `expected_dynamic_context` 表示 policy builder 输出，不包含 middleware 的 system prompt。
