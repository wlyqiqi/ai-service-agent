# CustomerServiceGraph 总图（AI 路径内部结构）

> 权威定义：`docs/specs/graph-spec.md`、`docs/specs/handoff-spec.md`。
> 顶层会话流程见 `handoff-overview.svg`。Archify 渲染版见 `架构图/archify-architecture.html`。

```mermaid
flowchart TD
    A["新消息"] --> B{"conversation_mode == human ?<br/>Conversation Runtime（轻量）"}
    B -- "是(human)" --> H["人工接管中（AI 暂停）<br/>写 Message Store，通知坐席"]
    B -- "否(ai)" --> C{"存在挂起 interrupt ?<br/>入口层 aget_state 读 __interrupt__"}
    C -- "是" --> R["入口层 resume 调度<br/>Command(resume=...) 续接子图"]
    R --> RG["恢复至被中断子图<br/>不重走 Router"]
    C -- "否" --> G["Guard ∥ Router<br/>并行；Router 产出 route_intent + confidence"]
    G --> P["Policy Gate<br/>route_intent → target_graph / allowed_tools / …"]
    P --> SF["safety_final<br/>Guard fail"]
    P --> RD["ReadGraph<br/>ReAct + 只读工具"]
    P --> MU["MutationGraph<br/>select（tool 或 Operation Clarify）→ 预检 → Form Interrupt → 执行"]
    P --> HR["HighRiskGraph<br/>select → 预检/Form → 审批 interrupt"]
    P --> FB["Fallback<br/>unknown / 兜底"]
    SF & RD & MU & HR & FB --> F["Final Response<br/>写回 messages + 归一化事件"]
    RD & MU & HR & FB --> HO["interrupt(handoff_request)<br/>外层：建 Record + 切 mode + 发事件"]
    HO -.-> H
    F --> PRE{"发送前重校验 mode == ai ?<br/>读实时值"}
    PRE -- "是(ai)" --> SEND["通过 SSE / AgentBot 发送"]
    PRE -- "否" --> DROP["丢弃（防双重回复）"]
    H --> RESOLVE["人工 Resolve<br/>校验 pending 审批 → mode=ai<br/>审批旁路见 ADR-0002"]
    RESOLVE --> A
```

## 关键注记

- Guard 为确定性规则包（与 Router 并行）；fail 走 `safety_final`，不消费 Router 分类结果做子图路由。
- Router 唯一语义输出是 `route_intent` + `confidence`；子图选择由 Policy Gate 确定性派生（见 `state-spec.md`）。
- Mutation 主路径用 Form Interrupt（`form_required`），不是聊天 `confirmation_required`；tool 未定由 Mutation `mutation_select_llm` 内联 Operation Clarify（ADR 0004），无入口 Hybrid。HighRisk 工具选择为 `high_risk_select_llm`（ADR 0005）。
- Form / 审批挂起时父图 `execution_status` 常仍为 `routing`；对外权威是 checkpointer `__interrupt__`。
- `conversation_id=session_id`、`thread_id` 一对一映射见 `handoff-spec.md` §5.1。pending 审批「继续挂起等 AI」归宿 **已废止**（[ADR-0002](../adr/0002-seat-approver-side-channel.md)）。
