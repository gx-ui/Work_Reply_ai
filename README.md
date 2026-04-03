# 工单回复 AI 助手（Work Reply AI）

基于 RAG（检索增强生成）技术的智能工单回复建议系统，为客服人员提供 AI 生成的回复建议。

## 项目特性

- 🤖 **AI 智能回复**：基于通义千问（Qwen）大语言模型生成客服回复建议
- 📚 **RAG 知识库**：基于 Milvus 向量数据库，提供知识库检索增强
- 🔍 **语义搜索**：支持语义相似度检索，精准匹配相关知识
- 🔄 **父子文档召回**：支持 Parent-Child 文档结构，提升检索完整性
- 🎯 **敏感信息脱敏**：自动脱敏手机号、订单号、密码等敏感信息
- � **美观日志**：带颜色和表情符号的友好日志输出

## 目录结构

```
work_reply_ai/
├── backend/                    # FastAPI 后端服务
│   ├── app.py                 # 应用主入口
│   ├── model.py               # Pydantic 数据模型
│   └── services/              # 业务服务层
│       ├── agent_service.py   # Agent 服务管理
│       └── prompt_service.py  # Prompt 构建服务
├── config/                    # 配置模块
│   ├── config.json            # 配置文件（LLM、Milvus、Embedding等）
│   └── config_loader.py       # 配置加载器
├── tools/                     # 工具模块
│   ├── milvus_tool.py        # Milvus 向量检索工具
│   └── rag_retrieval_tool.py # RAG 检索工具封装
├── agent/                     # Agent 模块
│   └── work_reply_agent.py    # 工单回复 Agent
├── prompt/                    # Prompt 模板
│   └── agent_prompt.py       # Agent 提示词模板
├── utils/                    # 工具函数
│   ├── common.py             # 通用工具（脱敏、解析）
│   ├── milvus_utils.py       # Milvus 辅助工具
│   └── parent_child_retrieval.py # 父子文档召回
├── extension/                  # Chrome 扩展（预留）
└── README.md                   # 本文件
```

## 技术栈

### 后端
- **FastAPI** - 现代 Python Web 框架
- **Agno** - Agent 框架（基于 DashScope/通义千问）
- **Milvus** - 向量数据库
- **DashScope** - 阿里云通义千问 API
- **OpenAI SDK** - Embedding 向量化

### 核心依赖
```
fastapi
uvicorn
pydantic
agno
pymilvus
openai
dashscope
```

## 快速开始

### 1. 安装依赖

```bash
pip install fastapi uvicorn pydantic agno pymilvus openai dashscope
```

### 2. 配置说明

编辑 `config/config.json` 文件：

```json
{
    "llm": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/",
        "api_key": "your-api-key",
        "model_name": "qwen3-max",
        "temperature": 0.1,
        "timeout": 120
    },
    "milvus": {
        "host": "your-milvus-host",
        "port": 19530,
        "db_name": "rag",
        "collection_name": "your-collection-name",
        "dim": 2048,
        "limit": 5
    },
    "embedding": {
        "model_name": "text-embedding-v4",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "your-api-key"
    }
}
```

**配置项说明：**

| 配置项 | 说明 |
|--------|------|
| `llm.base_url` | LLM API 基础地址 |
| `llm.api_key` | DashScope API 密钥 |
| `llm.model_name` | 使用的 LLM 模型（默认 qwen3-max） |
| `milvus.host` | Milvus 服务器地址 |
| `milvus.port` | Milvus 端口（默认 19530） |
| `milvus.collection_name` | 知识库 Collection 名称 |
| `embedding.model_name` | Embedding 模型名称 |

### 3. 启动服务

```bash
cd work_reply_ai

uvicorn backend.app:app --reload --host 0.0.0.0 --port 8003
```

### 4. 调用 API

**请求示例：**

```bash
curl -X POST "http://localhost:8003/work_reply_ai/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "intent": "suggestion",
    "session_id": "182192792",
    "query_info": {
      "query": "请生成回复建议"
    },
    "works_info": {
      "ticket_id": "182192792",
      "title": "南网异常件3.10（超时未签收）",
      "desc": "用户反馈超时未签收，要求尽快处理",
      "tags": ["公司级核心项目"],
      "history": [
        {"index": 1, "summary": "今天 17:08回复了工单：已发短信提醒客户及时取件。"}
      ],
      "priority": "高",
      "status": "处理中"
    },
    "core_info": {
      "customer_name": "张三",
      "project_name": "南网",
      "mall_name": "官方商城"
    },
    "attention_info": {
      "project_attention": "",
      "supplier_attention": ""
    }
  }'
```

**响应示例：**

```json
{
  "suggestion": "已发短信提醒客户及时取件，后续将跟进物流妥投状态并闭环处理。",
  "knowledge_sources": ["内-南网售后处理.md"]
}
```

## API 接口

### 1. 统一聊天接口

```
POST /work_reply_ai/chat
```

**请求体：**

| 字段 | 类型 | 说明 |
|------|------|------|
| intent | string | suggestion / summary / query / auto |
| session_id | string | 可选，与会话绑定； |
| query_info | object | `query`：与意图相关的询问/补充文本 |
| works_info | object | 工单信息（含 `ticket_id`、title、desc 等） |
| core_info | object | 核心项目信息 |
| attention_info | object | 注意事项 |

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| suggestion | string | AI 生成的回复建议 |
| knowledge_sources | string[] | 使用的知识库文件 |

### 2. 健康检查

```
GET /work_reply_ai/health
```

### 3. 根路径

```
GET /
```

## 核心功能

### 1. RAG 检索流程

系统采用**两阶段检索**策略：

1. **Step 1（探路）**：调用 `list_knowledge_base_chunks_metadata` 获取知识库文件列表
2. **Step 2（决策）**：结合工单信息筛选最相关的 2-5 个文件名
3. **Step 3（检索）**：调用 `search_knowledge_base` 进行精准语义检索
4. **Step 4（兜底）**：若筛选失败，则进行全量语义检索

### 2. 父子文档召回

支持 Milvus Parent-Child 文档结构：
- 当检索到 child 文档时，自动查找并替换为 parent 文档内容
- 避免检索结果碎片化，提升答案完整性

### 3. 敏感信息脱敏

自动脱敏以下信息：
- 手机号（11位数字）
- 订单号/编号（15-20位数字）
- 密码、账号等敏感词

### 4. Agent 输出规范

- 仅输出 JSON 格式：`{"suggestion": "..."}`
- suggestion 长度为 5-70 字中文
- 语气真诚、简明、可执行

## 日志系统

系统采用美观的日志输出格式：

```
📨 收到请求
📝 工单标题：南网异常件3.10（超时未签收）
🏷️ 标签：['公司级核心项目']
📜 历史记录：[{"index": 1, "summary": "今天 17:08回复了工单..."}]...
--------------------------------------------------------------------------------
```

日志特点：
- ✅ INFO - 绿色标识成功操作
- ⚠️ WARNING - 黄色标识警告信息
- ❌ ERROR - 红色标识错误信息
- 支持 Emoji 图标和颜色高亮

## 项目流程图

```
┌─────────────┐
│  用户请求   │
│ (工单信息)  │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────┐
│  FastAPI 后端               │
│  /work_reply_ai/chat        │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  Prompt 构建                │
│  (工单+标签+历史)            │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  Agno Agent (RAG模式)       │
│  ┌────────────────────────┐ │
│  │ 知识库检索 Toolkit     │ │
│  │ - list_chunks_metadata│ │
│  │ - search_knowledge    │ │
│  └───────────┬────────────┘ │
│              │              │
│              ▼              │
│  ┌────────────────────────┐ │
│  │ Milvus 向量检索        │ │
│  │ - 语义搜索             │ │
│  │ - 父子文档召回         │ │
│  └────────────────────────┘ │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  LLM 生成回复建议          │
│  (通义千问 qwen3-max)      │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  敏感信息脱敏               │
│  返回 JSON 响应              │
└─────────────────────────────┘
```

## 注意事项

1. **API Key 安全**：请勿将真实 API Key 提交到代码仓库
2. **Milvus 连接**：确保 Milvus 服务正常运行
3. **知识库配置**：collection_name 需要与实际 Milvus 中的 Collection 名称一致
4. **端口占用**：默认端口 8003，如需修改请更新启动命令

## 许可证

[根据项目实际情况填写]
