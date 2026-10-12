# AI Service Agent

LangGraph 客服运行时：按风险档分流读/写/高风险操作，写操作以结构化收集参数为主；**能同功能时优先官方 HITL / 预置组件**。

## Language

**MutationGraph**:
处理 `min_risk_level == mutation` 的写操作子图（改地址、改电话、备注、开票、售后工单等）；不含退款/取消。显式 `StateGraph` workflow，不用 ReAct agent 主路径。
_Avoid_: 把 HighRisk 审批流称作 Mutation；Mutation 主路径用 create_react_agent

**Mutation Form**:
仅在 **tool 已确定** 且需要业务字段时，由客户端按 interrupt 载荷 / `response_schema` 渲染的结构化填参 UI；不是操作类型菜单。
_Avoid_: 用表单做操作类型选择；自造与 interrupt 无关的第二套挂起语义

**Form Interrupt**:
Mutation 等人填表的官方挂起：节点内 `interrupt(value=..., response_schema=FormModel)` + checkpointer/`thread_id`。载荷供 UI 渲染；`response_schema`（langgraph≥1.2.12）描述确认提交的 JSON 形状。
_Avoid_: 用「普通轮 END + Form Pending 假状态」代替 interrupt；图内再发明 confirmation_required 挂起

**Form Resume**:
用户点击确认后，经 harness 转为 **`Command(resume=form_values | cancel)`** 续跑同一 thread；resume 值即 `interrupt()` 返回值。对外可仍走 HTTP/SSE，对内必须是官方 resume，而不是再当新用户消息进 Router。
_Avoid_: Form Submit 当普通 chat 再跑 Router；后端另设「跳过 Router」旁路假装 resume

**Order Picker**:
Mutation Form 中订单字段一律以下拉选择（含仅一笔可操作订单）；candidates 放在 interrupt `value` 里。
_Avoid_: 单笔自动填 order_id；聊天里口述选单后再出表单

**Operation Clarify**:
tool 未定时用**聊天**问清要做什么；问清后再进 MutationGraph 发 Form Interrupt。发生在进子图之前。
_Avoid_: 低置信时强制表单点选操作类型；在 MutationGraph 内聊天选 tool

**Form-Only Write**:
Mutation 写库只接受 Form Resume 成功路径上的结构化值；纯文本在 Form Interrupt 挂起期间不得写库，友好提示用表单。
_Avoid_: 文本降级 clarify 写库

**Hybrid Tool Resolution**:
高置信：确定性映射到唯一 tool 后 Form Interrupt；低置信：Operation Clarify（聊天）。
_Avoid_: 写路径 ReAct

**Closed MutationGraph**:
子图不内跳 Read/HighRisk/Fallback；校验/预检失败则**同一节点再次 interrupt**（或回 ensure_form 再 interrupt）以原地更新同一表单会话；新意图在取消或完成后下一 turn 重走总图 Router。
_Avoid_: 子图内部业务转交；失败后新开无关表单会话

**Form Cancel**:
表单显式取消 → `Command(resume={"action":"cancel", ...})`（或约定 cancel 哨兵），图内收尾不写库并结束 Form Interrupt。
_Avoid_: 仅靠打字「取消」作为主路径

**ToolNode**:
写工具执行使用 `langgraph.prebuilt.ToolNode`（或等价官方工具执行积木）；execute 节点只构造受控单次 tool_call。
_Avoid_: 在 Mutation 主路径让 LLM 自由循环选工具
