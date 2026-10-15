# CustomerServiceGraph 总图（AI 路径内部结构）

> 与 `customer_service_final_architecture.svg` 内容等价（本文件为 Mermaid 源码版）；顶层会话流程见 `handoff-overview.svg`。
> 权威定义：`docs/specs/graph-spec.md`、`docs/specs/handoff-spec.md`。

```mermaid
flowchart TD
    A["新消息"] --> B{"conversation_mode == human ?<br/>Conversation Runtime 仅做此判断（轻量）"}
    B -- "是(human)" --> H["人工接管中（AI 暂停）<br/>写入 Message Store，通知坐席"]
    B -- "否(ai)" --> C{"存在挂起 interrupt ?<br/>读 __interrupt__，权威判断依据"}
    C -- "是" --> R["ResumeHandler<br/>Command(resume=...) 恢复"]
    R --> RG["恢复至原子图"]
    C -- "否" --> G["Guard（MVP 推迟）∥ Router<br/>Guard 暂不生效，仅 Router 产出分类"]
    G --> P["Policy Gate<br/>确定性映射 capability × risk_level"]
    P --> RD["ReadGraph<br/>ReAct + 只读工具"]
    P --> MU["MutationGraph<br/>预检 → confirm → 执行"]
    P --> HR["HighRiskGraph<br/>预检 → 审批 → 执行"]
    P --> FB["Fallback<br/>低置信度 / 无法映射"]
    RD & MU & HR & FB --> F["Final Response<br/>写回 messages + 归一化事件"]
    RD & MU & HR --> HO["interrupt(handoff_request)<br/>外层接管：建 Record + 切 mode + 发事件"]
    HO -.-> H
    F --> PRE{"发送前重校验 mode == ai ?<br/>读实时值，非生成时刻值"}
    PRE -- "是(ai)" --> SEND["通过 SSE/WS 发送"]
    PRE -- "否" --> DROP["丢弃（防双重回复）"]
    H --> RESOLVE["人工 Resolve<br/>先查 pending 审批（校验）→ mode=ai<br/>审批旁路见 ADR-0002"]
    RESOLVE --> A
```

## 关键注记

- Guard 节点 MVP 阶段推迟生效，不阻断任何请求，但保留占位不删除。
- `conversation_id=session_id`、`thread_id` 独立一对一映射已定案（见 `docs/specs/handoff-spec.md` §5.1）。pending 审批「继续挂起等 AI」归宿 **已废止**（[ADR-0002](../adr/0002-seat-approver-side-channel.md)；`handoff-spec` §1.3 / §5.1 归宿段已 supersede）。`state.messages` 唯一真相源见 Message Store 定案。
- 发送前重校验读取实时 mode 值，非生成时刻值。
