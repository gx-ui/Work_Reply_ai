# 工单回复 AI 助手（Work Reply AI）

基于 **Agno Agent** 与 **Milvus 向量检索（RAG）** 的智能工单助手：为客服提供**回复建议**、**工单摘要**、**知识库问答**等能力，并提供 **SSE 流式**接口。

## 项目特性

- **多意图统一入口**：`suggestion`（回复建议）、`summary`（工单摘要）、`query`（知识查询）
- **RAG 知识库**：主知识库 + 摘要专用双库（客服售后案例、项目注意事项），Milvus 语义检索
- **父子文档召回**：Parent–Child 结构下可提升召回完整性（见 `utils/parent_child_retrieval.py`）
- **Chrome 扩展**：`extension/` 侧对接后端（网关路径与本文 API 一致）
- **可选持久化**：按配置将单次会话结果写入 MySQL（`chat_run_persistence`）
- **可观测日志**：请求追踪、工具调用记录等（见 `utils/log_utils.py`）

## 环境要求

- **Python**：建议 **3.10+**（3.12 亦可，以本机验证环境为准）
- **Milvus**：可访问的集群或托管实例
- **大模型与向量**：兼容 OpenAI 协议的 **Chat** 与 **Embedding** 端点（如 DashScope 兼容模式）

## 目录结构（与仓库一致）

```
work_reply_ai/
├── app/                      # FastAPI 入口
│   └── app.py                # 路由：/cs_assist_ai/chat、/chat/stream、health 等
├── agent/                    # Agno Agent 封装
│   ├── work_reply_agent.py   # 回复建议 / 通用工单 Agent
│   └── summary_agent.py      # 摘要 Agent
├── entity/                   # Pydantic 请求/响应模型
│   ├── request.py
│   └── response.py
├── services/
│   └── agent_service.py      # Agent 初始化、运行、溯源辅助
├── db/                       # MySQL 引擎与 chat 快照持久化
│   ├── mysql_store.py
│   └── chat_run_store.py
├── config/
│   ├── config.json           # 默认配置（勿提交真实密钥）
│   ├── config_dev.json       # 测试/开发配置（可选）
│   └── config_loader.py
├── tools/
│   ├── milvus_tool.py        # Milvus + Embedding 检索
│   ├── rag_retrieval_tool.py # 主知识库 RAG Toolkit
│   └── summary_rag_tools.py  # 摘要链路专用 RAG Toolkit
├── prompt/
│   ├── work_reply_agent_prompt.py
│   ├── summary_agent_prompt.py
│   └── query_agent_prompt.py
├── utils/
│   ├── log_utils.py
│   ├── milvus_utils.py
│   ├── parent_child_retrieval.py
│   └── common.py             # 通用工具（如脱敏等）
├── scripts/                  # 运维/辅助脚本
├── extension/                # 浏览器扩展前端
├── knowledges/               # 本地试验/知识相关脚本（可选）
├── requirements.txt
└── README.md
```

## 技术栈

| 层级 | 技术 |
|------|------|
| Web | FastAPI、Uvicorn |
| Agent | Agno、`agno.models.dashscope.DashScope`（通义千问兼容 OpenAI 协议） |
| 向量库 | pymilvus |
| 向量化 | OpenAI 兼容 `embeddings.create`（见 `tools/milvus_tool.py`） |
| ORM | SQLAlchemy 2.x + PyMySQL（可选） |

## 快速开始

### 1. 安装依赖

```bash
cd work_reply_ai
pip install -r requirements.txt
```

### 2. 配置

1. 复制并编辑 `config/config.json`（或单独维护一份 **不入库** 的配置文件）。
2. 通过环境变量指定配置文件（**推荐生产**）：

| 变量 | 说明 |
|------|------|
| `WORK_REPLY_CONFIG_FILE` | 配置文件路径（相对项目根或绝对路径） |
| `WORK_REPLY_PROFILE` | `dev` → `config/config.json`；`test` → `config/config_dev.json` |

**配置要点**（键名以 `config_loader` 实际读取为准）：

- **`llm`**：`base_url`、`api_key`、`model_name`、超时与重试等。
- **`embedding`**：与检索共用的向量化服务；**摘要/主 RAG 查 Milvus 前都会请求 embedding**，网络或超时会导致 `APITimeoutError`。
- **`milvus`**：主知识库集合。
- **`milvus_kefu_shouhou` / `milvus_zhuyishixiang`**：摘要链路专用集合（若缺失则对应 Toolkit 初始化会跳过）。
- **`mysql` + `chat_run_persistence`**：可选；启用后写入工单快照表。

**安全**：请勿将真实 `api_key`、数据库密码提交到公开仓库；已泄露的密钥应及时轮换。

### 3. 启动服务

```bash
uvicorn app.app:app --reload --host 0.0.0.0 --port 8003
```

根路径说明：`GET /` 会列出主要 endpoint。业务 API 挂载在 **`/cs_assist_ai`** 下。

### 4. 健康检查

```bash
curl -s http://localhost:8003/cs_assist_ai/health
```

## API 说明

### 统一聊天（JSON）

```http
POST /cs_assist_ai/chat
Content-Type: application/json
```

**请求体**（`ChatRequest`，字段名与 `entity/request.py` 一致）：

| 字段 | 说明 |
|------|------|
| `intent` | `suggestion` \| `summary` \| `query` |
| `session_id` | 可选，用于会话与追踪 |
| `works_info` | 工单：标题、描述、状态、历史等 |
| `core_info` | 客户/项目/商城等 |
| `attention_info` | 项目/供应商注意事项 |
| `query_info` | **`query` 意图必填**：`query_info.query` 为用户问题 |

**响应**（随 `intent` 变化；`suggestion` / `query` 使用 `by_alias=True`，见 `app/app.py`）：

- **`suggestion`**：`{"suggestion": "...", "knowledge_sources": [...]}`
- **`summary`**：`{"summary": {"info_summary": "...", "reviews": "...", "summary_sources": [...]}}`
- **`query`**：`{"answer": "...", "sources": [...]}`

### 流式（SSE）

```http
POST /cs_assist_ai/chat/stream
```

事件流中会包含 `delta`/`tool` 等，结束时 `event=done` 携带与同步接口等价的业务 JSON。

### 兼容路径（旧网关）

- `POST /cs_assist_ai/work_reply_ai/chat`
- `POST /cs_assist_ai/work_reply_ai/chat/stream`

## 调用示例

### 回复建议（suggestion）

```bash
curl -X POST "http://localhost:8003/cs_assist_ai/chat" ^
  -H "Content-Type: application/json" ^
  -d "{\"intent\":\"suggestion\",\"session_id\":\"demo-1\",\"works_info\":{\"ticket_id\":\"T1\",\"title\":\"少发\",\"desc\":\"用户称少发一件\",\"history\":[],\"status\":\"处理中\",\"priority\":\"P1\"},\"core_info\":{\"customer_name\":\"示例客户\",\"project_name\":\"示例项目\",\"mall_name\":\"示例商城\"},\"attention_info\":{\"project_attention\":\"\",\"supplier_attention\":\"\"}}"
```

### 工单摘要（summary）

将上例中 `"intent"` 改为 `"summary"` 即可（字段结构相同；模型将按摘要提示词与 RAG 工具生成结构化 JSON）。

### 知识查询（query）

```bash
curl -X POST "http://localhost:8003/cs_assist_ai/chat" ^
  -H "Content-Type: application/json" ^
  -d "{\"intent\":\"query\",\"query_info\":{\"query\":\"少发如何补发？\"},\"works_info\":{...},\"core_info\":{...},\"attention_info\":{...}}"
```

## RAG 与检索流程（概要）

- **主链路（suggestion / query）**：`KnowledgeRetrievalToolkit` — 可先列举 chunk 元数据再按 `file_name` 过滤语义检索，失败可走全库检索（详见 `tools/rag_retrieval_tool.py`）。
- **摘要链路（summary）**：`create_summary_rag_toolkits` — 客服售后库 + 注意事项库两套路由（见 `tools/summary_rag_tools.py`）。

## 辅助脚本

| 脚本 | 说明 |
|------|------|
| `scripts/init_chat_run_table.py` | 初始化 chat 快照表（需 MySQL 配置） |
| `scripts/cache_file_name.py` | 文件名缓存相关 |
| `scripts/test_parent_child_retrieval.py` | 父子召回测试 |

## 常见问题

1. **`openai.APITimeoutError`（Embedding 超时）**  
   向量检索前会对查询调用 `embeddings.create`。**Embedding 的 `base_url` / 网络 / 限流**异常时会出现超时，与 Milvus 本身无关。
2. **RAG 未生效**  
   检查 Milvus 与 embedding 配置；主 Agent 初始化失败时会降级为无工具模式（见日志）。
3. **端口**  
   默认 `8003`，与扩展或网关配置保持一致即可。

## 许可证

按项目组要求自行补充（如内部专有、MIT 等）。
