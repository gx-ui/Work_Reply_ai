# Agent 职责矩阵（Team 模式）

## 目标

在 team 并行模式下，确保 summary_agent 与 work_reply_agent 职责边界清晰、零重叠、零盲区。

## 职责矩阵

| Agent | 职责 | 输出类型 | 禁止事项 |
|---|---|---|---|
| summary_agent | 总结、提炼、归档 | 结构化摘要 JSON（summary.question/status/reviews） | 不生成业务对客回复、不承担用户交互 |
| work_reply_agent | 业务回复、上下文衔接、用户交互 | 对客回复建议 JSON（suggestion） | 不输出摘要归档字段、不替代 summary_agent 执行总结 |

## 路由原则

1. `intent=summary`：必须路由到 summary_agent。  
2. `intent=suggestion`：必须路由到 work_reply_agent。  
3. `intent=auto`：仅在 auto 场景允许关键词判定；意图不明确时默认 work_reply_agent。  
4. 非 auto 场景禁止根据关键词改写既定 intent。  

## 初始化约束

1. team 构造仅通过 `AgentInitializer` 完成。  
2. summary_agent 仅加载 `prompt/summary_agent_prompt.py`。  
3. work_reply_agent 仅加载 `prompt/work_reply_agent_prompt.py`。  
4. 任何 agent 不得跨引用对方提示词常量。  
