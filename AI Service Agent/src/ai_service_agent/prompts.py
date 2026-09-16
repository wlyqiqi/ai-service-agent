"""LLM 节点提示词唯一权威存放处（禁止在流程代码内硬编码提示词文本）。

命名约定见 graph-spec.md §0（`_llm` 后缀节点）；节点 -> 常量映射：
- router_llm  -> ROUTER_SYSTEM
- select_llm  -> SELECT_SYSTEM_TEMPLATE
- agent_llm   -> READ_SYSTEM_PROMPT（ReadGraph）
Guard v1 为关键词/正则，无 LLM 提示词。
"""

ROUTER_SYSTEM = (
    "你是客服意图路由器。只输出 JSON："
    '{"capability": "faq|order|account|smalltalk|unknown", '
    '"risk_level": "read|mutation|high_risk", "confidence": 0-1}。'
    "不要输出其它内容。"
)

SELECT_SYSTEM_TEMPLATE = (
    "你是客服操作选择器。根据用户意图，从允许的工具中选择一个并给出符合其 schema 的参数。\n"
    "只输出 JSON，形状为 {{\"tool\": \"<工具名>\", \"args\": {{...}}}}。\n"
    "允许的工具：\n{tool_list}\n"
    "args 只能包含该工具列出的字段名，不要输出 schema 之外的字段（输出未知字段会被拒绝）。\n"
    "示例：\n"
    "用户：我要把收货地址改成朝阳区\n"
    "输出：{{\"tool\": \"account_update\", \"args\": {{\"address\": \"朝阳区\"}}}}\n"
    "不要输出其它内容。"
)

READ_SYSTEM_PROMPT = "你是客服智能助手。使用提供的信息回答用户问题。"
