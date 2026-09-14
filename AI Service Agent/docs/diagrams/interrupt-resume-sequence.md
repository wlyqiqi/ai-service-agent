# interrupt / resume 时序图

> 权威定义：`docs/specs/graph-spec.md` §3（ResumeHandler / resume payload schema）、`docs/specs/approval-spec.md`（审批流程）、`docs/specs/event-spec.md`（事件协议）。

## HighRiskGraph：人工审批路径

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant G as CustomerServiceGraph
    participant H as Harness (FastAPI)
    participant A as 审批服务/人工

    U->>H: POST /chat (message=退款申请, session_id)
    H->>G: ainvoke (thread_id=session_id)
    G->>G: Active Interrupt? → NO
    G->>G: Guard ∥ Router → Policy → HighRiskGraph
    G->>G: 安全预检通过
    G->>G: interrupt() 挂起, 写 pending_approval
    G-->>H: 事件: approval_required
    H-->>U: 事件流 (等待人工审批)

    A->>H: 审批接口: approve
    H->>G: ainvoke (thread_id=session_id,<br/>Command resume={action:approve})
    G->>G: ResumeHandler → 恢复 HighRiskGraph
    G->>G: 执行写操作工具 (幂等)
    G-->>H: 事件: done + 最终回复
    H-->>U: 最终响应
```

## MutationGraph：用户确认路径

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant G as CustomerServiceGraph
    participant H as Harness (FastAPI)

    U->>H: POST /chat (message=修改地址, session_id)
    H->>G: ainvoke (thread_id=session_id)
    G->>G: Active Interrupt? → NO
    G->>G: Guard ∥ Router → Policy → MutationGraph
    G->>G: 安全预检通过
    G->>G: interrupt() 挂起, 附影响摘要
    G-->>H: 事件: confirmation_required
    H-->>U: 确认请求

    U->>H: POST /chat (message=确认, 同一 session_id)
    H->>G: ainvoke (thread_id=session_id,<br/>Command resume={action:confirm})
    G->>G: ResumeHandler → 恢复 MutationGraph
    G->>G: 执行写操作工具
    G-->>H: 事件: done + 最终回复
    H-->>U: 最终响应
```

## 关键规则

- **resume payload 单一 schema**：`Command(resume={ "action": "approve"|"reject"|"confirm"|"cancel", "note"?: str } | { "result": "expired" })`，审批 / 用户确认 / 超时共用（`graph-spec.md` §3）。
- **有挂起中断不得重新 Router**：新消息语义是回应上一个中断，一律先经 ResumeHandler（`graph-spec.md` §1）。
- **恢复后的子图操作必须幂等**：审批可能重复提交或超时后重试（`AGENTS.md` §12）。
- **超时惰性处理**：下一 turn 检查中断过期后以 `Command(resume={"result":"expired"})` 收尾（`graph-spec.md` §3、§6.3）。
- **用户拒绝 / 审批拒绝**：不执行写操作，收尾并记录（`graph-spec.md` §6.2、§6.3）。

## Handoff：子图中途转人工 + 人工 Resolve 回切

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant C as Conversation Runtime（入口）
    participant G as CustomerServiceGraph
    participant H as 人工客服

    U->>C: POST /chat (message=..., session_id)
    C->>C: conversation_mode==ai → 进入图
    C->>G: ainvoke (thread_id=session_id)
    G->>G: Router → Policy → 子图执行
    G-->>C: interrupt(handoff_request, reason, summary)
    C->>C: 创建 Handoff Record + 原子切 mode=human
    C-->>U: 事件: handoff_created
    C-->>H: 通知坐席（写 Message Store）
    H->>H: 人工与用户对话
    H->>C: Resolve
    C->>C: 校验 pending 审批（继续挂起）→ mode=ai
    C-->>U: 事件: handoff_resolved
    U->>C: 下一条消息
    C->>C: mode==ai → 进入图
    C->>G: ainvoke → ResumeHandler 恢复原被中断子图 → 完成
```

## 关键规则（Handoff）

- 子图不自行创建 Handoff Record / 不自行切 `conversation_mode`，统一 `interrupt(handoff_request)`，由外层接管（`handoff-spec.md` §3）。
- 发送 AI 回复前实时校验 `conversation_mode==ai`，否则丢弃（`handoff-spec.md` §1.4）。
- human → ai 切换前必须校验该会话是否存在 pending 审批；归宿定案为继续挂起，AI 重新接管后自然恢复（`handoff-spec.md` §1.3）。
