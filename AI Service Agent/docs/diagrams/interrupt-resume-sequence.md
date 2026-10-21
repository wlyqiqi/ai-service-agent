# interrupt / resume 时序图

> 权威定义：`docs/specs/graph-spec.md` §3（入口层 resume 调度 / resume payload schema）、`docs/specs/approval-spec.md`、`docs/specs/event-spec.md`、`docs/specs/mutation-form-interrupt-spec.md`。

## HighRiskGraph：人工审批路径

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant H as Harness (FastAPI)
    participant G as CustomerServiceGraph
    participant A as 审批服务/坐席

    U->>H: POST /chat (message=退款申请, session_id)
    H->>H: aget_state：无挂起 interrupt
    H->>G: ainvoke (messages 水合历史)
    G->>G: Guard ∥ Router → Policy → HighRiskGraph
    G->>G: select / 预检通过
    G->>G: interrupt() 挂起, 写 pending_approval
    G-->>H: 事件: approval_required
    H-->>U: 事件流 (等待人工审批)

    A->>H: 审批侧信道: approve
    H->>G: ainvoke (Command resume={action:approve})
    G->>G: 从 HighRiskGraph 中断点续接（不重走 Router）
    G->>G: 执行写操作工具 (幂等)
    G-->>H: 事件: done + 最终回复
    H-->>U: 最终响应
```

## MutationGraph：Form Interrupt / Form Resume

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant H as Harness (FastAPI / AgentBot)
    participant G as CustomerServiceGraph

    U->>H: 入站消息 (message=修改地址, session_id)
    H->>H: aget_state：无挂起 interrupt
    H->>G: ainvoke (messages 水合历史)
    G->>G: Guard ∥ Router → Policy → MutationGraph
    G->>G: select → 预检 → Form Interrupt
    G-->>H: 事件: form_required
    H-->>U: Chatwoot form / SSE form_required

    U->>H: Form Confirm（结构化字段 + action=confirm）
    H->>G: ainvoke (Command resume={action:confirm, ...fields})
    G->>G: Form Resume → recheck → ToolNode 写一次
    G-->>H: 事件: done + 最终回复
    H-->>U: 最终响应
```

## 关键规则

- **resume payload 单一 schema**：`Command(resume={ "action": "approve"|"reject"|"confirm"|"cancel"|"handoff_resumed", "note"?: str } | { "result": "expired" })`（`graph-spec.md` §3）。
- **有挂起中断不得重新 Router**：由入口层 `aget_state` 判定后以 `Command(resume=...)` 发起（无图内 ResumeHandler 节点）。
- **Mutation 主路径事件**：`form_required` / `form_rejected`；不得再发 `confirmation_required`。
- **恢复后的子图操作必须幂等**：审批可能重复提交或超时后重试。
- **超时惰性处理**：下一 turn 以 `Command(resume={"result":"expired"})` 收尾（`graph-spec.md` §3、§6.3）。
- **用户 Cancel / 审批拒绝**：不执行写操作，收尾并记录（`graph-spec.md` §6.2、§6.3）。

## Handoff：子图中途转人工 + 人工 Resolve 回切

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant C as Conversation Runtime（入口）
    participant G as CustomerServiceGraph
    participant S as 人工客服

    U->>C: POST /chat (message=..., session_id)
    C->>C: conversation_mode==ai → 进入图
    C->>G: ainvoke (thread_id)
    G->>G: Router → Policy → 子图执行
    G-->>C: interrupt(handoff_request, reason, summary)
    C->>C: 创建 Handoff Record + 原子切 mode=human
    C-->>U: 事件: handoff_created
    C-->>S: 通知坐席（写 Message Store）
    S->>S: 人工与用户对话
    S->>C: Resolve
    C->>C: 校验 pending 审批 → mode=ai（审批旁路 ADR-0002）
    C->>G: Command(resume={action:handoff_resumed}) 主动续接
    C-->>U: 事件: handoff_resolved
    U->>C: 下一条消息
    C->>C: mode==ai → 正常新 turn（可再走 Router）
```

## 关键规则（Handoff）

- 子图不自行创建 Handoff Record / 不自行切 `conversation_mode`，统一 `interrupt(handoff_request)`，由外层接管（`handoff-spec.md` §3）。
- 发送 AI 回复前实时校验 `conversation_mode==ai`，否则丢弃（`handoff-spec.md` §1.4）。
- human → ai 切换前必须校验该会话是否存在 pending 审批（校验条仍有效）。旧「归宿=继续挂起、等 AI 接管后自然恢复」**已废止**——Seat Approver / Approval Side-Channel，见 [ADR-0002](../adr/0002-seat-approver-side-channel.md)。
