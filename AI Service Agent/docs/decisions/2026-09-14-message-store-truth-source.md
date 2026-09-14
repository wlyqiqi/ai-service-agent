# 真相源裁决：Message Store 唯一权威（2026-09-14）

> 定案来源：用户直接裁决 + 实现落地（MessageStore 水合/持久化已实现）。
> 关联：`docs/specs/handoff-spec.md` §5.2 · `docs/specs/state-spec.md` §1 · `docs/specs/architecture.md` §4 · `docs/specs/mvp-first-mile.md` §7。

## 1. 决定

- **唯一真相源 = Message Store**（`conversation_message` 表）：会话的 user / assistant 轮次只在此持久化。
- **`state.messages` 降级为工作视图**：每次 graph 调用时从 Message Store 水合（按 id 升序，`user→HumanMessage`、`assistant→AIMessage`）后作为图输入；图执行期间的内存消息列表不构成权威记录。
- 只持久化 user / assistant 轮次；工具调用、Router 结构化输出等内部消息不入 Store。

## 2. 理由

- 人工接管期间 Graph 不执行（`handoff-spec.md` §1.1），消息只能写 Message Store；若 State 也是权威，则接管期间会产生"只在 Store 里"的消息，双源必然漂移。
- 水合式工作视图让回切 `ai` 后的下一个 turn 天然看到完整历史（含人工坐席的交流），无需额外同步。
- 与现有分层一致：`conversation_mode` 在入口层轻量判断，消息持久化/水合同样落在入口层（Conversation Runtime），Graph 不直接读写 Store。

## 3. 落地

- 新增 `harness/messages.py::MessageStore`（`append` / `list` / `hydrate`）。
- 入口层（`harness/server.py`）AI 车道：turn 开始先持久化用户消息 → 水合历史作为 `state.messages` 输入 → 正常结束（发送前复检 `mode==ai` 通过）才持久化 assistant 回复；ModeChanged 丢弃的回复**不落库**。
- 人工车道：只写 Message Store，不触图。

## 4. 边界 / 未决

- 真实人工坐席回复的写入协议（坐席端如何 append）不在本里程碑范围。
- 水合时是否回放工具结果 / 上下文（context）不在此裁决内，后续按 `state-spec.md` 登记。
