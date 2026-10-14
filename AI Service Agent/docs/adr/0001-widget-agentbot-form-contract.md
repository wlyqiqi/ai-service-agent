# Widget 主入口与 AgentBot Form Interrupt 契约

C 端用户对话以 **Chatwoot Widget** 为唯一主入口；Runtime 经 **AgentBot Adapter** 接入 Website Channel。Mutation 写路径必须把 Graph `__interrupt__` 投影为 Chatwoot native `form`（同源 `order_candidates` / schema / errors），用户确认或按钮取消走 `Command(resume=...)`，遵守 Form-Only Write——不再把表单提交改写为自然语言进 Router，也不再以自建 SSE 页作为写路径权威。

选用 Contact-first Binding（登录 user_id + Identity HMAC）懒建 Runtime 会话；同一会话单 Form 挂起。v1 覆盖 Read/FAQ、Operation Clarify（聊天）、Mutation Form（含 form_rejected、纯文本拒答并重推表单、按钮 Cancel）与转人工 tip；明确不做 Widget 审批按钮、匿名会话、多表单并行，以及把自建 SSE 主聊当正式写入口。

**Status:** accepted

**Considered Options:** 自建页继续作 Form 权威；Widget 暂缓写路径或双轨旧 clarify/confirm——均与「Widget 主入口」及 Form-Only Write 冲突，故拒绝。
