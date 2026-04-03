"""
智能选品 Team 应用（优化版 - 融合维度确认功能）
使用 Team 模式实现：Team Leader（融合维度确认） + 数据查询 Agent

Team 结构：
1. Team Leader（融合维度确认功能）- 直接分析用户需求，梳理维度，展示给用户确认
2. 数据查询 Agent - 执行商品查询

工作流程：
1. Team Leader 接收用户查询请求
2. Team Leader 直接分析需求，梳理查询维度（不再委托给维度确认专家）
3. Team Leader 展示维度给用户并请求确认
4. 如果用户补充，重新梳理维度
5. 如果用户确认，委托给数据查询 Agent 执行查询

优化点：
- ✅ 减少 Agent 调用：从 3 个 Agent 减少到 2 个（Team Leader + 数据查询专家）
- ✅ 更快响应：减少一次 Agent 委托调用，降低延迟
- ✅ 节约成本：减少一次 LLM API 调用
- ✅ 简化架构：Team Leader 直接完成维度梳理，无需中间环节

功能特性：
- ✅ 查询前确认：必须先梳理维度并请求用户确认
- ✅ 用户补充：支持用户补充或修改查询维度
- ✅ 循环确认：如果用户补充信息，重新梳理并确认
- ✅ 多轮对话：支持自然的多轮对话，保留最近10轮对话历史
- ✅ 长期记忆：用户偏好和历史查询信息会持久化存储到 MySQL

启动方式：
    python smart_product_selection_team_optimized.py

访问地址：
    - Web 界面: http://localhost:7778
    - API 文档: http://localhost:7778/docs
    - 配置页面: http://localhost:7778/config
"""

import sys
import os
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus
from typing import Dict, Any
from fastapi import Request  # 用于路径重写中间件

# agno 框架已通过 pip 安装在 conda 环境中（版本 2.3.13），无需添加路径
# 直接使用已安装的 agno 包即可

# 添加当前目录到 Python 路径（用于导入本地模块）
current_dir = Path(__file__).parent
sys.path.insert(0, str(current_dir))

# 在修改 sys.path 后导入项目模块
from agno.os import AgentOS  # noqa: E402
from agno.agent import Agent  # noqa: E402
from agno.team import Team  # noqa: E402
from agno.models.dashscope import DashScope  # noqa: E402
from agno.tools.reasoning import ReasoningTools  # noqa: E402
# 导入本地工具模块（从当前目录的 tools/ 目录）
from tools.goods_selection_tool import create_goods_selection_toolkit  # noqa: E402
from tools.knowledge_retrieval_tool import create_knowledge_retrieval_toolkit  # noqa: E402
from tools.category_standardizer_toolkit import create_category_standardizer_toolkit  # noqa: E402
from tools.sku_sender_tool import create_sku_sender_toolkit  # noqa: E402
# 导入本地工具模块（从当前目录的 utils/ 目录）
from utils.logger import setup_logger  # noqa: E402
# 导入 Agno 框架原生的 MySQLDb
from agno.db.mysql import MySQLDb  # noqa: E402
# 导入 MemoryManager 用于自定义记忆提取规则
from agno.memory import MemoryManager  # noqa: E402
from prompts.reasoning_instructions_team import (  # noqa: E402
    TEAM_LEADER_REASONING_INSTRUCTIONS,
    QUERY_EXECUTION_REASONING_INSTRUCTIONS,
)
from prompts.agent_instructions import (  # noqa: E402
    TEAM_LEADER_INSTRUCTIONS,
    QUERY_EXECUTION_AGENT_INSTRUCTIONS,
)
from prompts.memory_instructions import (  # noqa: E402
    MEMORY_CAPTURE_INSTRUCTIONS,
    MEMORY_ADDITIONAL_INSTRUCTIONS,
)

# ==================== 日志配置 ====================
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

app_log_file = log_dir / "smart_product_selection_team_optimized.log"
agno_debug_log_file = log_dir / "agno_debug.log"

# 日志文件大小控制配置
# max_bytes: 单个日志文件最大大小（默认 10MB = 10 * 1024 * 1024 字节）
# backup_count: 保留的备份文件数量（默认 5 个）
# 当日志文件达到 max_bytes 时，会自动轮转：
# - 当前文件重命名为 .log.1, .log.2, ... .log.N
# - 创建新的 .log 文件继续写入
# - 超过 backup_count 的旧文件会被自动删除
logger = setup_logger(
    name="smart_product_selection_team_optimized",
    log_file=str(app_log_file),
    level=logging.INFO,
    max_bytes=10 * 1024 * 1024,  # 10MB，可根据需要调整
    backup_count=5  # 保留 5 个备份文件，可根据需要调整
)

# ==================== 配置 tools 模块日志收集 ====================
# 将 tools 模块（如 goods_selection_tool.py）的日志输出到主日志文件
# 注意：tools.goods_selection_tool 等子 logger 会传播到 tools logger（默认 propagate=True）
tools_logger = logging.getLogger("tools")
tools_logger.setLevel(logging.INFO)
tools_logger.propagate = False  # 不向上传播到根 logger，避免重复输出

# 清除已有的处理器（避免重复添加）
if tools_logger.handlers:
    tools_logger.handlers.clear()

# 创建格式化器（与主 logger 相同格式）
tools_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 文件处理器，输出到主日志文件
tools_file_handler = RotatingFileHandler(
    str(app_log_file),
    encoding='utf-8',
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=5
)
tools_file_handler.setLevel(logging.INFO)
tools_file_handler.setFormatter(tools_formatter)
tools_logger.addHandler(tools_file_handler)

# 控制台处理器（输出到 stderr）
tools_console_handler = logging.StreamHandler(sys.stderr)
tools_console_handler.setLevel(logging.INFO)
tools_console_handler.setFormatter(tools_formatter)
tools_logger.addHandler(tools_console_handler)

logger.info("✅ tools 模块日志已配置，将输出到主日志文件")

# ==================== 配置 Agno 框架日志收集 ====================
# Agno 框架内部的日志（工具调用、用户输入、WebSocket 等）使用 "agno" logger
# 这些日志通常是 DEBUG 级别，需要单独配置才能保存到文件
agno_logger = logging.getLogger("agno")
agno_logger.setLevel(logging.DEBUG)
agno_logger.propagate = False  # 不向上传播，避免重复输出

# 清除已有的处理器（避免重复添加）
if agno_logger.handlers:
    agno_logger.handlers.clear()

# 创建文件处理器，将 Agno 调试日志保存到文件
# 使用 RotatingFileHandler 支持日志轮转
agno_file_handler = RotatingFileHandler(
    str(agno_debug_log_file),
    encoding='utf-8',
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=5
)
agno_file_handler.setLevel(logging.DEBUG)

# 创建格式化器（包含详细信息）
agno_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
agno_file_handler.setFormatter(agno_formatter)
agno_logger.addHandler(agno_file_handler)

# 可选：同时输出到控制台（stderr）
agno_console_handler = logging.StreamHandler(sys.stderr)
agno_console_handler.setLevel(logging.DEBUG)
agno_console_handler.setFormatter(agno_formatter)
agno_logger.addHandler(agno_console_handler)

# ==================== 重定向 stdout/stderr 到日志文件 ====================
# debug_mode=True 会使用 print() 直接输出到 stdout，需要重定向才能保存到文件
import re

class TeeOutput:
    """同时输出到控制台和文件的类，去除 ANSI 转义码并去重"""
    def __init__(self, file_path, original_stream):
        self.file = open(file_path, 'a', encoding='utf-8')
        self.original_stream = original_stream
        self.file_path = file_path
        # ANSI 转义码正则表达式
        self.ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        # 用于去重的缓冲区（存储最近写入的内容）
        self._recent_writes = []  # 存储最近几次写入的内容和时间戳
        self._max_recent_size = 5  # 最多保存最近5次写入
    
    def _strip_ansi(self, text):
        """去除 ANSI 转义码"""
        return self.ansi_escape.sub('', text)
    
    def write(self, text):
        # 去除 ANSI 转义码
        clean_text = self._strip_ansi(text)
        
        # 只处理非空内容
        if not clean_text.strip():
            # 空内容直接输出到控制台，不写入文件
            self.original_stream.write(text)
            self.original_stream.flush()
            return
        
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 检查是否是重复的日志（与最近写入的内容相同且时间戳相同）
        is_duplicate = False
        for recent_text, recent_timestamp in self._recent_writes:
            if clean_text == recent_text and timestamp == recent_timestamp:
                is_duplicate = True
                break
        
        # 如果不是重复的，写入文件
        if not is_duplicate:
            # 写入文件（添加时间戳，去除 ANSI 转义码）
            self.file.write(f"{timestamp} - {clean_text}")
            self.file.flush()
            
            # 更新最近写入记录
            self._recent_writes.append((clean_text, timestamp))
            if len(self._recent_writes) > self._max_recent_size:
                self._recent_writes.pop(0)
        
        # 同时输出到原始流（控制台，保留颜色）
        self.original_stream.write(text)
        self.original_stream.flush()
    
    def flush(self):
        self.file.flush()
        self.original_stream.flush()
    
    def close(self):
        if hasattr(self.file, 'close'):
            self.file.close()
    
    def __getattr__(self, name):
        # 转发其他属性到原始流
        return getattr(self.original_stream, name)

# 创建 stdout 和 stderr 的重定向文件
debug_output_file = log_dir / "agno_debug_output.log"
# 注意：只重定向 stdout，stderr 已经通过 logging 处理
stdout_tee = TeeOutput(str(debug_output_file), sys.stdout)

# 保存原始的 stdout（如果需要恢复）
_original_stdout = sys.stdout

# 重定向 stdout（debug_mode 的输出会到这里）
sys.stdout = stdout_tee

logger.info("✅ Agno 框架调试日志已配置")
logger.info(f"   - Agno 调试日志文件: {agno_debug_log_file}")
logger.info(f"   - Agno 调试输出文件（stdout/stderr）: {debug_output_file}")
logger.info(f"   - 应用日志文件: {app_log_file}")

def get_config_path():
    """
    根据环境变量获取配置文件路径
    
    支持多环境配置：
    - dev (默认): 使用 config/config.json (本地开发环境)
    - staging: 使用 config/config.staging.json (预演环境)
    - prod: 使用 config/config.prod.json (生产环境)
    
    环境变量: AGNO_ENV (可选值: dev, staging, prod)
    
    Returns:
        Path: 配置文件路径对象
        
    Raises:
        FileNotFoundError: 如果所有配置文件都不存在
    """
    current_dir = Path(__file__).parent
    config_dir = current_dir / "config"
    
    # 从环境变量读取环境类型，默认为 dev（本地开发）
    env = os.getenv("AGNO_ENV", "dev").lower()
    
    # 环境与配置文件的映射关系
    config_files = {
        "dev": "config.json",           # 本地开发环境
        "staging": "config.staging.json", # 预演环境
        "prod": "config.prod.json"      # 生产环境
    }
    
    # 获取对应环境的配置文件名称
    config_file = config_files.get(env, "config.json")
    config_path = config_dir / config_file
    
    # 如果指定环境的配置文件不存在，回退到默认的 config.json
    if not config_path.exists():
        if env != "dev":
            logger.warning(
                f"⚠️  环境配置文件不存在: {config_path}，"
                f"回退到默认配置文件: {config_dir / 'config.json'}"
            )
        config_path = config_dir / "config.json"
    
    # 如果默认配置文件也不存在，抛出错误
    if not config_path.exists():
        raise FileNotFoundError(
            f"❌ 配置文件不存在: {config_path}\n"
            f"请确保配置文件存在于以下位置之一：\n"
            f"  - {config_dir / 'config.json'}\n"
            f"  - {config_dir / 'config.staging.json'}\n"
            f"  - {config_dir / 'config.prod.json'}"
        )
    
    logger.info(f"📋 使用配置文件: {config_path} (环境: {env})")
    return config_path


def load_mysql_config():
    """
    从配置文件加载 MySQL 配置
    
    根据环境变量 AGNO_ENV 自动选择对应的配置文件：
    - dev: config/config.json
    - staging: config/config.staging.json
    - prod: config/config.prod.json
    """
    # 使用统一的配置文件路径获取函数（支持多环境）
    config_path = get_config_path()
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        
        mysql_config = config_data.get('mysql', {})
        
        if not mysql_config:
            raise ValueError("配置文件中未找到 mysql 配置")
        
        required_keys = ['host', 'port', 'user', 'password', 'database']
        missing_keys = [key for key in required_keys if key not in mysql_config]
        if missing_keys:
            raise ValueError(f"MySQL 配置缺少必需的配置项: {missing_keys}")
        
        return mysql_config
        
    except FileNotFoundError:
        logger.error(f"❌ 配置文件不存在: {config_path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"❌ 配置文件 JSON 解析失败: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ 加载 MySQL 配置失败: {e}")
        raise


def load_gateway_config():
    """
    从配置文件加载网关配置（路径前缀）
    
    根据环境变量 AGNO_ENV 自动选择对应的配置文件，并读取 gateway.path_prefix 配置。
    
    Returns:
        str: 网关路径前缀，如果未配置则返回空字符串（本地开发模式）
    """
    # 使用统一的配置文件路径获取函数（支持多环境）
    config_path = get_config_path()
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        
        # 从 gateway 配置段读取 path_prefix
        gateway_config = config_data.get('gateway', {})
        path_prefix = gateway_config.get('path_prefix', '')
        
        # 如果配置为空字符串或 None，返回空字符串（本地开发模式）
        if not path_prefix:
            logger.debug("配置文件中未设置 gateway.path_prefix，使用默认值: ''（本地开发模式）")
            return ""
        
        # 确保路径前缀以 / 开头
        if not path_prefix.startswith('/'):
            path_prefix = '/' + path_prefix
        
        logger.info(f"✅ 从配置文件加载网关路径前缀: {path_prefix}")
        return path_prefix
        
    except FileNotFoundError:
        logger.debug(f"配置文件不存在: {config_path}，使用默认网关路径前缀: ''（本地开发模式）")
        return ""
    except json.JSONDecodeError as e:
        logger.warning(f"⚠️  配置文件 JSON 解析失败: {e}，使用默认网关路径前缀: ''（本地开发模式）")
        return ""
    except Exception as e:
        logger.warning(f"⚠️  加载网关配置失败: {e}，使用默认网关路径前缀: ''（本地开发模式）")
        return ""


def create_db():
    """
    创建数据库实例（使用 Agno 框架原生的 MySQLDb）
    配置了连接池优化参数以提高在高并发下的稳定性。
    """
    mysql_config = load_mysql_config()
    
    host = mysql_config['host']
    port = mysql_config['port']
    user = quote_plus(str(mysql_config['user']))
    password = quote_plus(str(mysql_config['password']))
    database = quote_plus(str(mysql_config['database']))
    
    # 构建数据库连接 URL
    db_url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4"
    
    # 🚀 优化：手动创建带有连接池保护的引擎
    from sqlalchemy import create_engine
    engine = create_engine(
        db_url,
        pool_pre_ping=True,   # 每次使用前检查连接是否有效，解决“连接已断开”问题
        pool_recycle=1800,    # 每30分钟强制回收连接，防止 MySQL wait_timeout 超时
        pool_size=5,          # 限制基础连接数，防止端口耗尽
        max_overflow=10       # 允许的最大溢出连接
    )
    
    # 使用共享引擎初始化 MySQLDb
    db = MySQLDb(
        db_engine=engine,
        db_schema=mysql_config['database'],
        session_table="smart_product_selection_team_optimized_session",
        memory_table="smart_product_selection_team_optimized_memories",
    )
    
    # 配置 MySQL sort_buffer_size（解决排序内存不足问题）
    # 使用 SQLAlchemy 事件监听器，确保每个新连接都自动设置
    try:
        if hasattr(db, 'db_engine') and db.db_engine:
            from sqlalchemy import event
            
            @event.listens_for(db.db_engine, "connect")
            def set_sort_buffer_size(dbapi_conn, connection_record):
                """为每个新连接设置 sort_buffer_size"""
                try:
                    cursor = dbapi_conn.cursor()
                    cursor.execute("SET SESSION sort_buffer_size = 104857600")  # 100MB
                    cursor.close()
                except Exception as e:
                    logger.warning(f"⚠️  为新连接设置 sort_buffer_size 失败: {e}")
            
            logger.info("✅ 已配置 MySQL sort_buffer_size = 100MB（自动应用到所有新连接）")
    except Exception as e:
        logger.warning(f"⚠️  配置 sort_buffer_size 失败: {e}，继续使用默认配置")
    
    logger.info("✅ 数据库连接创建成功（使用 Agno 框架原生 MySQLDb，默认配置）")
    logger.info(f"   - 数据库地址: {host}:{port}")
    logger.info(f"   - 数据库名称: {database}")
    logger.info(f"   - 会话表名: smart_product_selection_team_optimized_session")
    
    # 测试数据库连接
    try:
        if hasattr(db, 'db_engine') and db.db_engine:
            with db.db_engine.connect() as conn:
                from sqlalchemy import text
                result = conn.execute(text("SELECT 1"))
                result.fetchone()
            logger.info("✅ 数据库连接测试成功")
        else:
            logger.warning("⚠️  数据库引擎未初始化，无法测试连接")
    except Exception as e:
        logger.error(f"❌ 数据库连接测试失败: {e}")
        logger.error(f"   请检查数据库配置和网络连接")
        logger.error(f"   数据库地址: {host}:{port}")
    
    # 自动创建所有数据库视图（如果不存在）
    try:
        import sys
        from pathlib import Path
        current_dir = Path(__file__).parent
        if str(current_dir) not in sys.path:
            sys.path.insert(0, str(current_dir))
        from scripts.create_database_views import create_all_views
        # 传入 db 参数，确保使用正确配置的数据库实例和连接池
        create_all_views(db=db)
    except Exception as e:
        logger.warning(f"⚠️  创建数据库视图失败: {e}，可以稍后手动创建")
    
    # 自动创建 SKU 发送日志表（如果不存在）
    try:
        from scripts.create_sku_send_log_table import create_sku_send_log_table
        create_sku_send_log_table(db=db)
    except Exception as e:
        logger.warning(f"⚠️  创建 SKU 发送日志表失败: {e}，可以稍后手动创建")
    
    # 自动创建运行评分表（如果不存在）
    try:
        from scripts.create_run_ratings_table import create_run_ratings_table
        create_run_ratings_table(db=db)
    except Exception as e:
        logger.warning(f"⚠️  创建运行评分表失败: {e}，可以稍后手动创建")
    
    return db


# 全局数据库实例（单例模式，供其他脚本使用）
_shared_db = None


def get_shared_db():
    """
    获取共享的数据库实例（单例模式）
    
    供其他脚本（如 create_database_views.py, check_database_tables.py）使用
    
    Returns:
        MySQLDb: 数据库实例
    """
    global _shared_db
    if _shared_db is None:
        _shared_db = create_db()
    return _shared_db


def create_query_execution_agent(db=None):
    """
    创建数据查询 Agent
    :param db: 可选的数据库共享实例
    """
    logger.info("📦 正在创建数据查询 Agent...")
    
    goods_toolkit = create_goods_selection_toolkit()
    
    model = DashScope(
        id="qwen-flash",
        api_key="sk-f6df435f67c648c7852723e1aef076d0",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    
    # 如果没有传入共享 db，则创建一个（保证兼容性）
    if db is None:
        db = create_db()
    
    # 创建品类标准化工具
    logger.info("📦 正在创建品类标准化工具...")
    try:
        category_toolkit = create_category_standardizer_toolkit()
        logger.info("✅ 品类标准化工具创建成功")
    except Exception as e:
        logger.warning(f"⚠️  品类标准化工具创建失败: {e}")
        category_toolkit = None
    
    # 创建 SKU 处理工具（传入数据库实例，用于存储处理日志）
    logger.info("📦 正在创建 SKU 处理工具...")
    try:
        sku_sender_toolkit = create_sku_sender_toolkit(db=db)
        logger.info("✅ SKU 处理工具创建成功")
    except Exception as e:
        logger.warning(f"⚠️  SKU 处理工具创建失败: {e}")
        sku_sender_toolkit = None
    
    reasoning_tools = ReasoningTools(
        enable_think=True,
        enable_analyze=True,
        add_instructions=True,
        instructions=QUERY_EXECUTION_REASONING_INSTRUCTIONS,
    )
    
    # 添加工具钩子来预处理 send_sku_list 的参数，确保 JSON 格式正确
    def sku_sender_hook(
        run_context, function_name: str, function_call, arguments: Dict[str, Any]
    ):
        """预处理 send_sku_list 工具的参数，确保格式正确"""
        if function_name == "send_sku_list":
            logger.info(f"🔧 [工具钩子] 处理 send_sku_list 调用，原始参数类型: {type(arguments.get('sku_list'))}")
            
            sku_list = arguments.get("sku_list")
            
            # 如果 sku_list 是字符串，尝试解析为列表
            if isinstance(sku_list, str):
                logger.info(f"🔧 [工具钩子] sku_list 是字符串，尝试解析: {sku_list[:100]}...")
                try:
                    # 尝试解析 JSON 字符串
                    import json
                    parsed = json.loads(sku_list)
                    if isinstance(parsed, list):
                        arguments["sku_list"] = parsed
                        logger.info(f"✅ [工具钩子] 成功解析 JSON 字符串为列表，数量: {len(parsed)}")
                    elif isinstance(parsed, dict) and "sku_list" in parsed:
                        arguments["sku_list"] = parsed["sku_list"]
                        logger.info(f"✅ [工具钩子] 从字典中提取 sku_list，数量: {len(parsed['sku_list'])}")
                except (json.JSONDecodeError, ValueError) as e:
                    # 如果不是 JSON，尝试按逗号分割
                    logger.warning(f"⚠️  [工具钩子] JSON 解析失败: {e}，尝试按逗号分割")
                    arguments["sku_list"] = [s.strip() for s in sku_list.split(",") if s.strip()]
            
            # 确保 sku_list 是列表
            if not isinstance(arguments.get("sku_list"), list):
                logger.warning(f"⚠️  [工具钩子] sku_list 参数格式错误，已转换为列表: {type(arguments.get('sku_list'))}")
                arguments["sku_list"] = []
            
            # 限制 SKU 数量，避免参数过长
            sku_list = arguments["sku_list"]
            original_count = len(sku_list)
            if len(sku_list) > 30:
                logger.warning(f"⚠️  [工具钩子] SKU 列表过长（{len(sku_list)} 个），截取前 30 个")
                arguments["sku_list"] = sku_list[:30]
                logger.info(f"✅ [工具钩子] 已截取 SKU 列表: {original_count} -> {len(arguments['sku_list'])}")
            
            # 去重
            if arguments["sku_list"]:
                seen = set()
                deduplicated = []
                for sku in arguments["sku_list"]:
                    sku_str = str(sku).strip()
                    if sku_str and sku_str not in seen:
                        seen.add(sku_str)
                        deduplicated.append(sku_str)
                if len(deduplicated) < len(arguments["sku_list"]):
                    logger.info(f"✅ [工具钩子] 已去重 SKU 列表: {len(arguments['sku_list'])} -> {len(deduplicated)}")
                arguments["sku_list"] = deduplicated
            
            logger.info(f"✅ [工具钩子] 最终 SKU 列表数量: {len(arguments['sku_list'])}")
        
        # 调用原始函数
        return function_call(**arguments)
    
    agent = Agent(
        id="query-execution-agent",
        name="数据查询专家",
        model=model,
        role="你是一个专业的商品数据查询专家，负责执行商品查询并返回结果。",
        tools=[
            goods_toolkit,
            reasoning_tools,
            *([category_toolkit] if category_toolkit else []),
            *([sku_sender_toolkit] if sku_sender_toolkit else []),  # 添加 SKU 发送工具
        ],
        db=db,
        instructions=QUERY_EXECUTION_AGENT_INSTRUCTIONS,
        markdown=True,
        debug_mode=True,
        reasoning=False,
        read_chat_history=False,  # 移除 read_chat_history，依赖 add_history_to_context 自动加载历史
        add_history_to_context=True,  # 自动加载历史消息到上下文
        num_history_runs=10,  # 保留最近10轮对话历史
        tool_hooks=[sku_sender_hook],  # 添加工具钩子
    )
    
    logger.info("✅ 数据查询 Agent 创建成功")
    return agent


def patch_team_read_or_create_session():
    """
    Monkey Patch: 修复 Team._read_or_create_session() 方法
    在创建新会话前，先检查数据库中是否有旧数据，如果有则合并而不是覆盖
    """
    from agno.team.team import Team
    from agno.db.base import SessionType
    from agno.session.team import TeamSession
    from agno.run.team import TeamRunOutput
    from time import time
    from copy import deepcopy
    from typing import Optional, cast
    from uuid import uuid4
    from agno.models.message import Message
    
    # 获取我们自己的 logger，确保日志输出到控制台和日志文件
    # 注意：使用全局 logger，确保日志配置一致
    patch_logger = logger  # 使用模块级别的 logger，而不是重新获取
    
    # 保存原始方法（虽然不使用，但保留以便将来需要时可以恢复）
    # original_read_or_create_session = Team._read_or_create_session
    
    def patched_read_or_create_session(
        self, session_id: str, user_id: Optional[str] = None
    ) -> TeamSession:
        """
        修复版本的 _read_or_create_session
        在创建新会话前，先尝试从数据库直接读取旧数据，如果有则合并
        """
        # Return existing session if we have one
        if self._cached_session is not None and self._cached_session.session_id == session_id:
            patch_logger.info(f"[Monkey Patch] 使用缓存的 session: {session_id}")
            return self._cached_session

        # Try to load from database using original method
        team_session = None
        if self.db is not None and self.parent_team_id is None and self.workflow_id is None:
            patch_logger.info(f"[Monkey Patch] 开始读取 session: {session_id}")
            patch_logger.debug(f"[Monkey Patch] 尝试通过 _read_session() 读取 session: {session_id}")
            team_session = cast(TeamSession, self._read_session(session_id=session_id))
            if team_session:
                runs_count = len(team_session.runs) if hasattr(team_session, 'runs') and team_session.runs else 0
                patch_logger.info(
                    f"[Monkey Patch] _read_session() 成功返回 session，"
                    f"runs数量={runs_count}，无需创建新会话"
                )
            else:
                patch_logger.info(f"[Monkey Patch] _read_session() 返回 None，将尝试直接从数据库读取旧数据")

        # ⚠️ 关键修复：如果 _read_session 返回 None，在创建新会话前先尝试直接读取数据库
        existing_runs = None
        existing_session_data = None
        if team_session is None:
            try:
                if self.db is not None:
                    # 直接调用数据库的 get_session，绕过 _read_session 的异常处理
                    # 这样可以区分连接错误和 session 不存在
                    patch_logger.info(f"[Monkey Patch] 尝试直接从数据库读取旧数据: {session_id}")
                    try:
                        existing_session = self.db.get_session(
                            session_id=session_id,
                            session_type=SessionType.TEAM
                        )
                        if existing_session:
                            patch_logger.info(f"[Monkey Patch] 数据库查询成功，检查是否有旧数据...")
                            if hasattr(existing_session, 'runs'):
                                existing_runs = existing_session.runs or []
                                if hasattr(existing_session, 'session_data'):
                                    existing_session_data = existing_session.session_data
                                if existing_runs:
                                    patch_logger.info(
                                        f"✅ [Monkey Patch] 找到数据库中的旧数据！session_id={session_id}, "
                                        f"runs数量={len(existing_runs)}"
                                    )
                                else:
                                    patch_logger.info("[Monkey Patch] 数据库中有 session，但 runs 为空")
                            else:
                                patch_logger.info("[Monkey Patch] 数据库中有 session，但没有 runs 属性")
                        else:
                            patch_logger.info(f"[Monkey Patch] 数据库中未找到 session: {session_id}")
                    except Exception as e:
                        # 所有错误（包括连接错误和 session 不存在），记录日志但继续创建新会话
                        patch_logger.debug(f"[Monkey Patch] 数据库查询出错: {e}，将创建新会话")
            except Exception as e:
                # 如果读取旧数据时出错，记录日志但继续创建新会话
                patch_logger.warning(f"⚠️  [Monkey Patch] 读取旧数据时出错: {e}，将创建新会话")

        # Create new session if none found
        if team_session is None:
            patch_logger.info(f"[Monkey Patch] 准备创建新 TeamSession: {session_id}")
            patch_logger.debug(f"Creating new TeamSession: {session_id}")
            session_data = {}
            if self.session_state is not None:
                session_data["session_state"] = deepcopy(self.session_state)
            
            # 如果找到旧数据，合并 session_data
            if existing_session_data:
                session_data.update(existing_session_data)
                patch_logger.info(f"✅ [Monkey Patch] 已合并旧 session_data 到新会话")
            
            team_session = TeamSession(
                session_id=session_id,
                team_id=self.id,
                user_id=user_id,
                team_data=self._get_team_data(),
                session_data=session_data,
                metadata=self.metadata,
                created_at=int(time()),
            )
            
            # ⚠️ 关键修复：如果找到旧 runs，合并到新会话中
            if existing_runs:
                team_session.runs = existing_runs
                patch_logger.info(
                    f"✅ [Monkey Patch] 已合并 {len(existing_runs)} 个旧 runs 到新会话，"
                    f"避免历史数据丢失"
                )
            
            if self.introduction is not None:
                team_session.upsert_run(
                    TeamRunOutput(
                        run_id=str(uuid4()),
                        team_id=self.id,
                        session_id=session_id,
                        user_id=user_id,
                        team_name=self.name,
                        content=self.introduction,
                        messages=[Message(role=self.model.assistant_message_role, content=self.introduction)],  # type: ignore
                    )
                )

        # Cache the session if relevant
        if team_session is not None and self.cache_session:
            self._cached_session = team_session

        # 记录补丁代码执行结果
        if team_session:
            # --- 新增：建立 Session 与 Team 的关联，用于强制同步 ---
            try:
                team_session._agno_team_ref = self
                patch_logger.info(f"🔗 [Monkey Patch] 已将会话 {session_id} 与 Team 实例关联")
            except Exception as e:
                patch_logger.warning(f"⚠️ [Monkey Patch] 无法建立会话关联: {e}")
            # --------------------------------------------------

            final_runs_count = len(team_session.runs) if hasattr(team_session, 'runs') and team_session.runs else 0
            if existing_runs and final_runs_count > 0:
                patch_logger.info(
                    f"✅ [Monkey Patch] 补丁执行完成：成功合并 {len(existing_runs)} 个旧 runs，"
                    f"最终 session 有 {final_runs_count} 个 runs"
                )
            else:
                patch_logger.info(
                    f"[Monkey Patch] 补丁执行完成：session 已加载，"
                    f"runs数量={final_runs_count}（无需合并，数据完整）"
                )

        return team_session
    
    # 应用 Monkey Patch
    Team._read_or_create_session = patched_read_or_create_session
    logger.info("✅ 已应用 Monkey Patch: Team._read_or_create_session() 修复")
    logger.info("   - 在创建新会话前，会先检查数据库中是否有旧数据")
    logger.info("   - 如果找到旧数据，会合并到新会话中，避免历史数据丢失")


def patch_team_save_session():
    """
    Monkey Patch: 为 Team.save_session() 和 TeamRunOutput.add_member_run() 添加日志记录
    用于追踪会话保存状态和 member_responses 的添加
    """
    from agno.team.team import Team
    from agno.session.team import TeamSession
    from agno.run.team import TeamRunOutput
    
    # 保存原始方法
    original_save_session = Team.save_session
    original_asave_session = Team.asave_session
    original_upsert_run = TeamSession.upsert_run
    original_add_member_run = TeamRunOutput.add_member_run
    original_cleanup_and_store = Team._cleanup_and_store
    original_acleanup_and_store = Team._acleanup_and_store
    original_scrub_run_output_for_storage = Team._scrub_run_output_for_storage
    original_scrub_member_responses = Team._scrub_member_responses
    
    def patched_save_session(self, session):
        """带日志的 save_session"""
        session_id = session.session_id if session else None
        logger.info(f"💾 [Save Session] 开始保存会话: session_id={session_id}")
        
        # 检查保存条件
        db_status = "已配置" if self.db is not None else "未配置"
        parent_team_id = getattr(self, 'parent_team_id', None)
        workflow_id = getattr(self, 'workflow_id', None)
        
        logger.info(f"💾 [Save Session] 保存条件检查: db={db_status}, parent_team_id={parent_team_id}, workflow_id={workflow_id}")
        
        if self.db is None:
            logger.warning("⚠️  [Save Session] Team.db 为 None，无法保存会话")
        elif parent_team_id is not None:
            logger.info(f"ℹ️  [Save Session] 作为子Team运行，不保存会话（parent_team_id={parent_team_id}）")
        elif workflow_id is not None:
            logger.info(f"ℹ️  [Save Session] 在Workflow中运行，不保存会话（workflow_id={workflow_id}）")
        else:
            logger.info("✅ [Save Session] 保存条件满足，将调用数据库保存")
        
        try:
            # 调用原始方法
            result = original_save_session(self, session)
            logger.info(f"✅ [Save Session] 会话保存完成: session_id={session_id}")
            return result
        except Exception as e:
            logger.error(f"❌ [Save Session] 会话保存失败: session_id={session_id}, 错误={e}", exc_info=True)
            raise
    
    async def patched_asave_session(self, session):
        """带日志的异步 save_session"""
        import traceback
        
        session_id = session.session_id if session else None
        
        # 获取调用栈信息
        stack = traceback.extract_stack()
        caller_info = f"{stack[-3].filename.split('/')[-1]}:{stack[-3].lineno}" if len(stack) >= 3 else "unknown"
        
        logger.info(f"💾 [Save Session Async] 开始保存会话 (调用位置: {caller_info})")
        logger.info(f"   Session ID: {session_id}")
        
        # 检查保存条件
        db_status = "已配置" if self.db is not None else "未配置"
        parent_team_id = getattr(self, 'parent_team_id', None)
        workflow_id = getattr(self, 'workflow_id', None)
        logger.info(f"   保存条件: db={db_status}, parent_team_id={parent_team_id}, workflow_id={workflow_id}")
        
        # 检查 member_responses 状态（保存前）
        if session and hasattr(session, 'runs') and session.runs:
            logger.info(f"   总 Runs 数量: {len(session.runs)}")
            for i, run in enumerate(session.runs):
                if hasattr(run, 'member_responses'):
                    member_count = len(run.member_responses) if run.member_responses else 0
                    run_id = run.run_id if hasattr(run, 'run_id') else 'N/A'
                    run_status = run.status if hasattr(run, 'status') else 'N/A'
                    logger.info(f"   Run {i+1} (ID: {run_id}, Status: {run_status}) member_responses 数量: {member_count}")
                    if member_count > 0:
                        for j, member in enumerate(run.member_responses):
                            member_run_id = member.run_id if hasattr(member, 'run_id') else 'N/A'
                            member_status = member.status if hasattr(member, 'status') else 'N/A'
                            logger.info(f"     Member {j+1}: Run ID = {member_run_id}, Status = {member_status}")
                    else:
                        logger.warning(f"     ⚠️  member_responses 为空！")
        
        # 检查 store_member_responses 设置
        store_member = getattr(self, 'store_member_responses', False)
        logger.info(f"   store_member_responses = {store_member}")
        
        if not store_member:
            logger.warning(f"   ⚠️  store_member_responses=False，member_responses 将被清空！")
        
        # 记录保存前的详细状态（用于对比）
        before_save_state = {}
        if session and hasattr(session, 'runs') and session.runs:
            for i, run in enumerate(session.runs):
                if hasattr(run, 'member_responses'):
                    run_id = run.run_id if hasattr(run, 'run_id') else 'N/A'
                    before_save_state[run_id] = len(run.member_responses) if run.member_responses else 0
        
        try:
            # 调用原始方法
            result = await original_asave_session(self, session)
            
            # 检查保存后的状态（原始方法可能会修改 member_responses）
            if session and hasattr(session, 'runs') and session.runs:
                logger.info(f"   保存后检查 - 总 Runs 数量: {len(session.runs)}")
                for i, run in enumerate(session.runs):
                    if hasattr(run, 'member_responses'):
                        run_id = run.run_id if hasattr(run, 'run_id') else 'N/A'
                        after_count = len(run.member_responses) if run.member_responses else 0
                        before_count = before_save_state.get(run_id, 0)
                        logger.info(f"   Run {i+1} (ID: {run_id}) member_responses: 保存前={before_count}, 保存后={after_count}")
                        if before_count > 0 and after_count == 0:
                            logger.error(f"   ❌ 严重问题：Run {run_id} 的 member_responses 在保存过程中被清空！(保存前: {before_count}, 保存后: {after_count})")
                        elif before_count > 0 and after_count > 0:
                            logger.info(f"   ✅ Run {run_id} 的 member_responses 已保留")
            
            logger.info(f"✅ [Save Session Async] 会话保存完成: session_id={session_id}")
            return result
        except Exception as e:
            logger.error(f"❌ [Save Session Async] 会话保存失败: session_id={session_id}, 错误={e}", exc_info=True)
            raise
    
    # 追踪正在进行的保存任务，防止“保存风暴”
    _active_save_tasks = {}

    def patched_upsert_run(self, run_response):
        """带日志的 upsert_run，追踪 member_responses 的添加"""
        from agno.run.team import TeamRunOutput
        import traceback
        import time
        
        run_id = run_response.run_id if hasattr(run_response, 'run_id') else 'N/A'
        is_team_run = isinstance(run_response, TeamRunOutput)
        
        # 获取调用栈信息
        stack = traceback.extract_stack()
        caller_info = f"{stack[-3].filename.split('/')[-1]}:{stack[-3].lineno}" if len(stack) >= 3 else "unknown"
        
        # 调用原始方法
        result = original_upsert_run(self, run_response)
        
        # --- 增强：强制同步逻辑（带防抖和数据同步） ---
        if not is_team_run and hasattr(self, '_agno_team_ref'):
            team = self._agno_team_ref
            if team and team.db:
                session_id = self.session_id
                current_time = time.time()
                
                # 简单的防抖：同一会话在 2 秒内不重复触发强制保存
                last_save_time = getattr(self, '_last_forced_save_time', 0)
                if current_time - last_save_time < 2.0:
                    return result
                
                self._last_forced_save_time = current_time
                
                parent_run = getattr(run_response, '_parent_team_run', None)
                if parent_run:
                    # 关键补丁：如果父级内容为空，先同步成员内容，防止最后保存失败导致内容丢失
                    if not parent_run.content and run_response.content:
                        parent_run.content = run_response.content
                        logger.info(f"🔄 [Upsert Run] 已将 Agent 内容同步到父级 Team Run")
                    
                    logger.info(f"⚡ [Upsert Run] 发现父级 Team Run ({parent_run.run_id})，先将其同步到会话中...")
                    original_upsert_run(self, parent_run)
                
                logger.info(f"⚡ [Upsert Run] 检测到成员 Agent Run 完成，正在触发强制同步到数据库... (来源: {caller_info})")
                try:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # 使用 ensure_future 确保任务被调度
                        asyncio.ensure_future(team.asave_session(self))
                        logger.info(f"✅ [Upsert Run] 已提交异步强制保存请求")
                    else:
                        team.save_session(self)
                        logger.info(f"✅ [Upsert Run] 已同步执行强制保存")
                except Exception as e:
                    logger.error(f"❌ [Upsert Run] 强制同步失败: {e}")
        # --------------------------------------------
        
        return result
    
    def patched_add_member_run(self, run_response):
        """带日志的 add_member_run，追踪 member_responses 的添加"""
        import traceback
        
        team_run_id = self.run_id if hasattr(self, 'run_id') else 'N/A'
        member_run_id = run_response.run_id if hasattr(run_response, 'run_id') else 'N/A'
        member_status = run_response.status if hasattr(run_response, 'status') else 'N/A'
        member_count_before = len(self.member_responses) if self.member_responses else 0
        
        # 获取调用栈信息
        stack = traceback.extract_stack()
        caller_info = f"{stack[-3].filename.split('/')[-1]}:{stack[-3].lineno}" if len(stack) >= 3 else "unknown"
        
        logger.info(f"➕ [Add Member Run] 开始添加 Agent Run 到 Team Run 的 member_responses (调用位置: {caller_info})")
        logger.info(f"   Team Run ID: {team_run_id}")
        logger.info(f"   Agent Run ID: {member_run_id}")
        logger.info(f"   Agent Run Status: {member_status}")
        logger.info(f"   添加前 member_responses 数量: {member_count_before}")
        
        # 检查 member_responses 对象
        if self.member_responses is None:
            logger.warning(f"   ⚠️  self.member_responses 为 None，将被初始化为空列表")
        
        # 调用原始方法
        result = original_add_member_run(self, run_response)
        
        # --- 新增：建立 Agent Run 与 Team Run 的关联 ---
        try:
            run_response._parent_team_run = self
            logger.info(f"🔗 [Add Member Run] 已建立 Agent Run ({member_run_id}) 与 Team Run ({team_run_id}) 的关联")
        except Exception as e:
            logger.warning(f"⚠️ [Add Member Run] 无法建立关联: {e}")
        # --------------------------------------------
        
        # 检查添加后的状态
        member_count_after = len(self.member_responses) if self.member_responses else 0
        logger.info(f"   添加后 member_responses 数量: {member_count_after}")
        
        if member_count_after == member_count_before + 1:
            logger.info(f"✅ [Add Member Run] Agent Run 成功添加到 member_responses")
            # 验证添加的内容
            if self.member_responses and len(self.member_responses) > 0:
                last_member = self.member_responses[-1]
                last_member_id = last_member.run_id if hasattr(last_member, 'run_id') else 'N/A'
                if last_member_id == member_run_id:
                    logger.info(f"   ✅ 验证通过：最后一个 member 的 Run ID 匹配 ({last_member_id})")
                else:
                    logger.warning(f"   ⚠️  验证失败：最后一个 member 的 Run ID 不匹配 (期望: {member_run_id}, 实际: {last_member_id})")
        else:
            logger.error(f"❌ [Add Member Run] Agent Run 添加失败！数量未增加（前: {member_count_before}, 后: {member_count_after}）")
        
        return result
    
    def patched_cleanup_and_store(self, run_response, session):
        """带日志的同步 _cleanup_and_store"""
        import traceback
        
        run_id = run_response.run_id if hasattr(run_response, 'run_id') else 'N/A'
        session_id = session.session_id if hasattr(session, 'session_id') else 'N/A'
        
        # 获取调用栈信息
        stack = traceback.extract_stack()
        caller_info = f"{stack[-3].filename.split('/')[-1]}:{stack[-3].lineno}" if len(stack) >= 3 else "unknown"
        
        logger.info(f"🧹 [Cleanup And Store] 开始清理和存储 Team Run (调用位置: {caller_info})")
        logger.info(f"   Team Run ID: {run_id}")
        logger.info(f"   Session ID: {session_id}")
        
        # 检查清理前的 member_responses 状态
        if hasattr(run_response, 'member_responses'):
            before_scrub_count = len(run_response.member_responses) if run_response.member_responses else 0
            logger.info(f"   清理前 - run_response.member_responses 数量: {before_scrub_count}")
            if before_scrub_count > 0:
                for j, member in enumerate(run_response.member_responses):
                    member_run_id = member.run_id if hasattr(member, 'run_id') else 'N/A'
                    logger.info(f"     Member {j+1}: Run ID = {member_run_id}")
        
        # 调用原始方法
        try:
            result = original_cleanup_and_store(self, run_response, session)
            logger.info(f"✅ [Cleanup And Store] 清理和存储完成: Team Run ID={run_id}")
            return result
        except Exception as e:
            logger.error(f"❌ [Cleanup And Store] 清理和存储失败: Team Run ID={run_id}, 错误={e}", exc_info=True)
            raise
    
    async def patched_acleanup_and_store(self, run_response, session):
        """带日志的异步 _acleanup_and_store"""
        import traceback
        
        run_id = run_response.run_id if hasattr(run_response, 'run_id') else 'N/A'
        session_id = session.session_id if hasattr(session, 'session_id') else 'N/A'
        
        # 获取调用栈信息
        stack = traceback.extract_stack()
        caller_info = f"{stack[-3].filename.split('/')[-1]}:{stack[-3].lineno}" if len(stack) >= 3 else "unknown"
        
        logger.info(f"🧹 [Cleanup And Store Async] 开始清理和存储 Team Run (调用位置: {caller_info})")
        logger.info(f"   Team Run ID: {run_id}")
        logger.info(f"   Session ID: {session_id}")
        
        # 检查清理前的 member_responses 状态
        if hasattr(run_response, 'member_responses'):
            before_scrub_count = len(run_response.member_responses) if run_response.member_responses else 0
            logger.info(f"   清理前 - run_response.member_responses 数量: {before_scrub_count}")
            if before_scrub_count > 0:
                for j, member in enumerate(run_response.member_responses):
                    member_run_id = member.run_id if hasattr(member, 'run_id') else 'N/A'
                    member_status = member.status if hasattr(member, 'status') else 'N/A'
                    logger.info(f"     Member {j+1}: Run ID = {member_run_id}, Status = {member_status}")
        
        # 调用原始方法
        try:
            result = await original_acleanup_and_store(self, run_response, session)
            logger.info(f"✅ [Cleanup And Store Async] 清理和存储完成: Team Run ID={run_id}")
            return result
        except Exception as e:
            logger.error(f"❌ [Cleanup And Store Async] 清理和存储失败: Team Run ID={run_id}, 错误={e}", exc_info=True)
            raise
    
    def patched_scrub_run_output_for_storage(self, run_response):
        """带日志的 _scrub_run_output_for_storage"""
        import traceback
        
        run_id = run_response.run_id if hasattr(run_response, 'run_id') else 'N/A'
        
        # 获取调用栈信息
        stack = traceback.extract_stack()
        caller_info = f"{stack[-3].filename.split('/')[-1]}:{stack[-3].lineno}" if len(stack) >= 3 else "unknown"
        
        # 检查清理前的状态
        before_count = 0
        if hasattr(run_response, 'member_responses'):
            before_count = len(run_response.member_responses) if run_response.member_responses else 0
            store_member = getattr(self, 'store_member_responses', False)
            
            logger.info(f"🧽 [Scrub Run Output] 开始清理 Run Output (调用位置: {caller_info})")
            logger.info(f"   Run ID: {run_id}")
            logger.info(f"   store_member_responses = {store_member}")
            logger.info(f"   清理前 - member_responses 数量: {before_count}")
        else:
            logger.info(f"🧽 [Scrub Run Output] 开始清理 Run Output (调用位置: {caller_info})")
            logger.info(f"   Run ID: {run_id}")
            logger.info(f"   ⚠️  run_response 没有 member_responses 属性")
        
        # 调用原始方法
        result = original_scrub_run_output_for_storage(self, run_response)
        
        # 检查清理后的状态
        if hasattr(run_response, 'member_responses'):
            after_count = len(run_response.member_responses) if run_response.member_responses else 0
            logger.info(f"   清理后 - member_responses 数量: {after_count}")
            
            if before_count > 0 and after_count == 0:
                store_member = getattr(self, 'store_member_responses', False)
                if store_member:
                    logger.error(f"   ❌ 严重问题：store_member_responses=True，但 member_responses 被清空了！")
                else:
                    logger.warning(f"   ⚠️  store_member_responses=False，member_responses 已被清空（符合预期）")
            elif before_count > 0 and after_count > 0:
                logger.info(f"   ✅ member_responses 保留，数量: {after_count}")
        
        return result
    
    def patched_scrub_member_responses(self, member_responses):
        """带日志的 _scrub_member_responses"""
        import traceback
        
        # 获取调用栈信息
        stack = traceback.extract_stack()
        caller_info = f"{stack[-3].filename.split('/')[-1]}:{stack[-3].lineno}" if len(stack) >= 3 else "unknown"
        
        before_count = len(member_responses) if member_responses else 0
        
        logger.info(f"🧽 [Scrub Member Responses] 开始清理 member_responses (调用位置: {caller_info})")
        logger.info(f"   清理前 - member_responses 数量: {before_count}")
        
        if before_count > 0:
            for j, member in enumerate(member_responses):
                member_run_id = member.run_id if hasattr(member, 'run_id') else 'N/A'
                logger.info(f"     Member {j+1}: Run ID = {member_run_id}")
        
        # 调用原始方法（原始方法没有返回值，是 void）
        original_scrub_member_responses(self, member_responses)
        
        after_count = len(member_responses) if member_responses else 0
        logger.info(f"   清理后 - member_responses 数量: {after_count}")
        
        if before_count > 0 and after_count == 0:
            logger.warning(f"   ⚠️  member_responses 已被清空（数量从 {before_count} 变为 0）")
        elif before_count > 0 and after_count > 0:
            logger.info(f"   ✅ member_responses 保留，数量: {after_count}")
    
    # 应用 Monkey Patch
    Team.save_session = patched_save_session
    Team.asave_session = patched_asave_session
    TeamSession.upsert_run = patched_upsert_run
    TeamRunOutput.add_member_run = patched_add_member_run
    Team._cleanup_and_store = patched_cleanup_and_store
    Team._acleanup_and_store = patched_acleanup_and_store
    Team._scrub_run_output_for_storage = patched_scrub_run_output_for_storage
    Team._scrub_member_responses = patched_scrub_member_responses
    logger.info("✅ 已应用 Monkey Patch: Team.save_session(), TeamSession.upsert_run(), TeamRunOutput.add_member_run(), Team._cleanup_and_store(), Team._acleanup_and_store(), Team._scrub_run_output_for_storage() 和 Team._scrub_member_responses() 日志记录")


def handle_user_name_metadata_pre_hook(
    team, run_input, session, session_state, dependencies, metadata, user_id, debug_mode
):
    """
    Pre-hook: 记录用户名称到 session.metadata

    功能说明：
    - 从 metadata 参数中提取 user_name（如果存在）
    - 如果传递了 user_name，就写入/更新到 session.metadata 中（覆盖之前的值）
    - 如果没有传递 user_name，就不处理（保持原值或为空）
    - 用户名称会被存储到数据库的 session 表的 metadata 字段中

    参数说明：
    - metadata: 本次运行传入的 metadata（可能是字典或 JSON 字符串，可选包含 user_name 字段）
    - session: 当前的 TeamSession 对象，其 metadata 会被存储到数据库
    - user_id: 用户 ID（可选，不是必须的）

    使用场景：
    - 首次创建 session 时传递 user_name，会记录到数据库
    - 后续如果 user_id 没变，但传递了新的 user_name，会覆盖之前的值
    - 如果没有传递 user_name，则保持原值不变

    ⚠️ 重要：此函数使用 try-except 包裹，确保任何错误都不会影响记忆功能和其他功能
    """
    try:
        # ⭐ 添加详细日志，记录接收到的 metadata 类型和内容
        logger.info(f"🔍 [Pre-Hook] 接收到 metadata 参数: type={type(metadata)}, value={metadata}")

        # ⭐ 关键调试：记录 session 当前的 metadata 状态
        current_session_metadata = session.metadata if session else None
        logger.info(f"🔍 [Pre-Hook] Session 当前 metadata: {current_session_metadata}")

        # ⭐ 关键调试：检查是否有现有 session 数据
        if hasattr(session, '_agno_team_ref') and session._agno_team_ref:
            team_ref = session._agno_team_ref
            if hasattr(team_ref, 'db') and team_ref.db:
                try:
                    from agno.db.base import SessionType
                    existing_session = team_ref.db.get_session(
                        session_id=session.session_id,
                        session_type=SessionType.TEAM
                    )
                    if existing_session and hasattr(existing_session, 'metadata'):
                        logger.info(f"🔍 [Pre-Hook] 数据库中现有 session metadata: {existing_session.metadata}")
                    else:
                        logger.info("🔍 [Pre-Hook] 数据库中无现有 session 或无 metadata")
                except Exception as e:
                    logger.debug(f"🔍 [Pre-Hook] 检查数据库现有 session 时出错: {e}")

        # 如果 metadata 为空，直接返回（不影响其他功能）
        if not metadata:
            logger.debug("ℹ️  [Pre-Hook] metadata 为空，跳过处理")
            return

        # ⭐ 关键修复：支持 JSON 字符串格式的 metadata
        # 封装服务可能将 metadata 作为 JSON 字符串传递，需要先解析
        metadata_dict = None
        if isinstance(metadata, str):
            # 如果是字符串，尝试解析为字典
            try:
                metadata_dict = json.loads(metadata)
                logger.info(f"✅ [Pre-Hook] 成功解析 JSON 字符串格式的 metadata: {metadata_dict}")
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"⚠️  [Pre-Hook] 无法解析 metadata JSON 字符串: {e}, 原始值: {metadata}")
                return
        elif isinstance(metadata, dict):
            # 如果已经是字典，直接使用
            metadata_dict = metadata
            logger.debug(f"ℹ️  [Pre-Hook] metadata 已经是字典格式: {metadata_dict}")
        else:
            # 其他类型，记录警告并返回（不影响其他功能）
            logger.warning(f"⚠️  [Pre-Hook] metadata 类型不支持: {type(metadata)}, 值: {metadata}")
            return

        # 从解析后的字典中提取 user_name
        if metadata_dict and isinstance(metadata_dict, dict):
            user_name = metadata_dict.get("user_name")

            # ⭐ 关键调试：记录找到的 user_name
            logger.info(f"🔍 [Pre-Hook] 从 metadata 中提取的 user_name: '{user_name}' (type: {type(user_name)})")

            # 如果传递了 user_name，就写入/更新（覆盖之前的值）
            if user_name and isinstance(user_name, str):
                # 确保 session.metadata 存在
                if session.metadata is None:
                    session.metadata = {}

                # 更新 user_name（覆盖之前的值）
                old_user_name = session.metadata.get("user_name")
                session.metadata["user_name"] = user_name

                logger.info(f"✅ [Pre-Hook] 设置 session.metadata['user_name'] = '{user_name}'")
                logger.info(f"✅ [Pre-Hook] 更新后的 session.metadata: {session.metadata}")

                if old_user_name:
                    logger.info(f"✅ [Pre-Hook] 已更新用户名称: {old_user_name} -> {user_name} (session_id={session.session_id})")
                else:
                    logger.info(f"✅ [Pre-Hook] 已记录用户名称: {user_name} (session_id={session.session_id})")

                # ⚠️ 关键修复：不在 pre_hook 中保存 session，避免留下挂起的事务
                # 原因分析：
                # 1. Agno 框架会在运行结束时自动保存 session，包括 metadata
                # 2. 如果在 pre_hook 中保存 session，可能会留下未提交的事务
                # 3. 当数据查询专家启动时，Agno 框架尝试读取 session，但遇到挂起的事务，导致错误：
                #    "Can't reconnect until invalid transaction is rolled back"
                #
                # 解决方案：只更新内存中的 session.metadata，让框架在运行结束时自动保存
                # 这样不会留下挂起的事务，数据查询专家可以正常启动
                #
                # 注意：如果确实需要在 pre_hook 中保存，可以使用以下方式（但需要确保事务被正确提交）：
                # if team.db is not None:
                #     try:
                #         team.save_session(session)
                #         # 使连接池失效，强制清理所有挂起的事务
                #         if hasattr(team.db, 'db_engine') and team.db.db_engine:
                #             try:
                #                 pool = team.db.db_engine.pool
                #                 if pool and hasattr(pool, 'invalidate'):
                #                     pool.invalidate()
                #                     logger.debug("✅ [Pre-Hook] 已使连接池失效，清理挂起的事务")
                #             except Exception:
                #                 pass
                #     except Exception as e:
                #         logger.warning(f"⚠️  [Pre-Hook] 保存 session 失败（可忽略，框架会在运行结束时保存）: {e}")

                # 当前方案：不保存，只更新内存中的 session.metadata
                # Agno 框架会在运行结束时自动保存 session，包括 metadata
                logger.debug("ℹ️  [Pre-Hook] 已更新 session.metadata（将在运行结束时自动保存到数据库）")
            else:
                logger.debug(f"ℹ️  [Pre-Hook] metadata 中未找到有效的 user_name，当前值: {user_name}")
        else:
            logger.warning(f"⚠️  [Pre-Hook] 解析后的 metadata 不是字典类型: {type(metadata_dict)}")

    except Exception as e:
        # ⚠️ 关键：捕获所有异常，确保 pre-hook 的错误不会影响记忆功能和其他功能
        # 记录错误但不抛出，让 Agno 框架继续执行后续流程（包括记忆功能）
        logger.error(f"❌ [Pre-Hook] 处理 metadata 时发生错误: {e}", exc_info=True)
        logger.warning("⚠️  [Pre-Hook] Pre-hook 错误已被捕获，不会影响记忆功能和其他功能")
        # 不抛出异常，让框架继续执行


def handle_user_name_metadata_post_hook(
    team, run_output, session, session_state, dependencies, metadata, user_id, debug_mode
):
    """
    Post-hook: 检查用户名称和会话保存状态
    
    功能说明：
    - 在运行完成后，检查 session.metadata 中的 user_name 是否正确
    - 检查会话是否已保存到数据库
    """
    # 检查用户名称
    if metadata and isinstance(metadata, dict):
        user_name = metadata.get("user_name")
        
        # 如果传递了 user_name，仅检查是否正确
        if user_name and isinstance(user_name, str):
            # 检查 session.metadata 中的 user_name 是否正确
            current_user_name = session.metadata.get("user_name") if session.metadata else None
            if current_user_name == user_name:
                logger.info(f"✅ [Post-Hook] 用户名称已验证: {user_name}")
            else:
                logger.warning(f"⚠️  [Post-Hook] 用户名称不一致: 期望={user_name}, 实际={current_user_name}")
    
    # 检查会话保存状态
    try:
        session_id = session.session_id if session else None
        runs_count = len(session.runs) if session and hasattr(session, 'runs') and session.runs else 0
        
        logger.info(f"📊 [Post-Hook] 会话状态检查: session_id={session_id}, runs数量={runs_count}")
        
        # 检查数据库配置
        if team:
            db_status = "已配置" if team.db is not None else "未配置"
            parent_team_id = getattr(team, 'parent_team_id', None)
            workflow_id = getattr(team, 'workflow_id', None)
            
            logger.info(f"📊 [Post-Hook] Team配置检查: db={db_status}, parent_team_id={parent_team_id}, workflow_id={workflow_id}")
            
            # 检查保存条件
            if team.db is None:
                logger.warning("⚠️  [Post-Hook] Team.db 为 None，会话可能无法保存到数据库")
            elif parent_team_id is not None:
                logger.warning(f"⚠️  [Post-Hook] Team.parent_team_id={parent_team_id}，作为子Team运行时不会保存会话")
            elif workflow_id is not None:
                logger.warning(f"⚠️  [Post-Hook] Team.workflow_id={workflow_id}，在Workflow中运行时不会保存会话")
            else:
                logger.info("✅ [Post-Hook] Team配置正常，应该可以保存会话到数据库")
                
                # 尝试验证会话是否在数据库中
                if session_id and team.db:
                    try:
                        from agno.db.base import SessionType
                        saved_session = team.db.get_session(session_id=session_id, session_type=SessionType.TEAM)
                        if saved_session:
                            saved_runs_count = len(saved_session.runs) if hasattr(saved_session, 'runs') and saved_session.runs else 0
                            logger.info(f"✅ [Post-Hook] 数据库验证: 会话已存在于数据库，runs数量={saved_runs_count}")
                        else:
                            logger.warning(f"⚠️  [Post-Hook] 数据库验证: 会话不存在于数据库中（可能尚未保存）")
                    except Exception as e:
                        logger.warning(f"⚠️  [Post-Hook] 数据库验证失败: {e}")
    except Exception as e:
        logger.error(f"❌ [Post-Hook] 检查会话状态时出错: {e}", exc_info=True)


def create_goods_selection_team():
    """创建商品查询 Team（优化版：Team Leader 融合维度确认功能）"""
    
    logger.info("📦 正在创建商品查询 Team（优化版）...")
    
    # ⚠️ 关键：在创建 Team 之前，先应用 Monkey Patch
    patch_team_read_or_create_session()
    patch_team_save_session()
    
    # 🚀 优化：先创建共享数据库实例
    db = create_db()
    
    # 将共享的 db 传递给成员 Agent
    query_agent = create_query_execution_agent(db=db)
    
    # 创建 Team Leader 的模型和工具
    model = DashScope(
        id="qwen-plus",
        api_key="sk-f6df435f67c648c7852723e1aef076d0",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    
    # Team Leader 融合维度确认功能，需要知识库检索工具
    logger.info("📦 正在创建知识库检索工具（Team Leader 使用）...")
    try:
        knowledge_toolkit = create_knowledge_retrieval_toolkit()
        logger.info("✅ 知识库检索工具创建成功")
    except Exception as e:
        logger.warning(f"⚠️  知识库检索工具创建失败: {e}")
        knowledge_toolkit = None
    
    # Team Leader 需要 SKU 处理工具（用于直接推送过滤后的历史 SKU 列表）
    logger.info("📦 正在创建 SKU 处理工具（Team Leader 使用）...")
    try:
        sku_sender_toolkit = create_sku_sender_toolkit(db=db)
        logger.info("✅ SKU 处理工具创建成功（Team Leader）")
    except Exception as e:
        logger.warning(f"⚠️  SKU 处理工具创建失败: {e}")
        sku_sender_toolkit = None
    
    # Team Leader 的推理工具
    reasoning_tools = ReasoningTools(
        enable_think=True,
        enable_analyze=False,
        add_instructions=True,
        instructions=TEAM_LEADER_REASONING_INSTRUCTIONS,
    )
    
    # 创建自定义 MemoryManager，定义收集用户偏好的维度
    logger.info("📦 正在创建自定义 MemoryManager（用户偏好记忆）...")
    memory_manager = MemoryManager(
        db=db,
        model=model,  # 使用 Team Leader 的模型来创建记忆
        memory_capture_instructions=MEMORY_CAPTURE_INSTRUCTIONS,
        additional_instructions=MEMORY_ADDITIONAL_INSTRUCTIONS,
    )
    logger.info("✅ 自定义 MemoryManager 创建成功")
    
    # 创建 Team（Team Leader 融合维度确认功能，只有一个成员：数据查询专家）
    # ⚠️ 重要：Team ID 必须与项目名称保持一致
    # Team ID 使用项目名称格式：smart-product-selection-team-optimized
    team = Team(
        id="smart-product-selection-team-optimized",  # 与项目名称保持一致
        name="智能选品工具（优化版）",
        model=model,
        members=[query_agent],  # 只有一个成员：数据查询专家
        tools=[
            reasoning_tools,
            *([knowledge_toolkit] if knowledge_toolkit else []),  # Team Leader 可以使用知识库检索工具
            *([sku_sender_toolkit] if sku_sender_toolkit else []),  # Team Leader 可以使用 SKU 处理工具（用于历史过滤场景）
        ],
        db=db,
        instructions=TEAM_LEADER_INSTRUCTIONS,
        markdown=True,
        debug_mode=True,
        reasoning=False,
        read_chat_history=False,  # 移除 read_chat_history，依赖 add_history_to_context 自动加载历史
        show_members_responses=False,
        store_member_responses=True,  # ⚠️ 关键：必须设置为 True，否则 Agent 的 run 数据（SKU列表、查询结果等）会被清空
        add_history_to_context=True,  # 自动加载历史消息到上下文，LLM 可以直接从上下文获取
        num_history_runs=10,  # 保留最近10轮对话历史
        memory_manager=memory_manager,  # 使用自定义 MemoryManager，定义偏好收集维度
        enable_user_memories=True,  # 启用用户记忆功能（自动记忆模式）
        add_memories_to_context=True,  # 将用户记忆添加到上下文，Team Leader 可以记住用户偏好
        pre_hooks=[handle_user_name_metadata_pre_hook],  # 添加 pre-hook 处理用户名称并保存
        post_hooks=[handle_user_name_metadata_post_hook],  # 添加 post-hook 检查会话保存状态
    )
    
    logger.info("✅ 商品查询 Team 创建成功（优化版）")
    logger.info(f"   - 多轮对话: 已启用（保留最近5轮历史）")
    logger.info("   - 用户记忆: 已启用（Team Leader 自动记录用户偏好）")
    logger.info("   - 偏好维度: 价格、品类、品牌、商品特征、购买习惯")
    logger.info("   - Team Leader: 商品查询团队协调者（融合维度确认功能）")
    logger.info("   - 成员: 数据查询专家")
    logger.info("   - 优化: 减少一次 Agent 调用，更快响应，节约成本")
    
    return team


def create_agent_os():
    """创建 AgentOS 实例"""
    
    logger.info("🚀 正在创建 AgentOS 实例...")
    
    # 创建 Team
    team = create_goods_selection_team()
    
    # 创建 AgentOS 实例
    agent_os = AgentOS(
        name="商品查询系统（Team 模式 - 优化版）",
        description="商品查询团队 - Team Leader（融合维度确认） + 数据查询，使用优化版 Team 模式",
        teams=[team],
    )
    
    logger.info("✅ AgentOS 实例创建成功")
    return agent_os


# ==================== 路径重写中间件（已禁用）====================
# 网关路径前缀功能已移除，不再使用路径前缀
# 所有请求直接按原路径处理，无需重写
GATEWAY_PATH_PREFIX = ""  # 固定为空，不使用路径前缀

# 在模块级别创建 AgentOS 实例和 app
# 使用检查机制防止重复初始化（当模块被重新导入时）
# 注意：当使用字符串形式的 app 参数时，AgentOS 会重新导入模块，导致模块级别代码再次执行
# 因此我们需要检查是否已经初始化过，避免重复输出日志和创建实例

# 使用模块属性来标记是否已初始化（即使模块被重新导入，这个属性也会保留在 sys.modules 中）
_module_name = __name__
_module = sys.modules.get(_module_name)

# 检查是否已经初始化（通过检查模块属性）
if not hasattr(_module, '_agent_os_initialized'):
    logger.info("=" * 80)
    logger.info("🚀 初始化商品查询 AgentOS 服务（Team 模式 - 优化版）")
    logger.info("=" * 80)
    
    # 创建 AgentOS 实例
    agent_os = create_agent_os()
    
    # 获取 FastAPI 应用（必须在模块级别）
    app = agent_os.get_app()
    
    # 标记为已初始化（使用当前模块对象）
    _module = sys.modules[_module_name]
    _module._agent_os_initialized = True
    
    # 服务信息日志（只在第一次初始化时输出）
    logger.info("")
    logger.info("=" * 80)
    logger.info("📋 服务信息")
    logger.info("=" * 80)
    # 统一端口配置：三个环境都使用7778端口（主服务）
    env = os.getenv("AGNO_ENV", "dev")
    agentos_port = os.getenv("AGENTOS_PORT")
    if agentos_port:
        _port = int(agentos_port)
    else:
        # 三个环境统一使用7778端口
        _port = 7778
    logger.info(f"🌍 运行环境: {env}")
    logger.info(f"🌐 Web 界面: http://localhost:{_port}")
    logger.info(f"📚 API 文档: http://localhost:{_port}/docs")
    logger.info(f"⚙️  配置页面: http://localhost:{_port}/config")
    # 网关路径前缀已移除，不再使用路径前缀
    logger.info("🔀 网关路径前缀: 已禁用（直接访问，无前缀）")
    logger.info(f"   - 原始接口: http://localhost:{_port}/teams/smart-product-selection-team-optimized/runs")
    logger.info(f"   - 封装接口: http://localhost:14403/smart_product_selection/api/chat/stream (需单独启动封装服务)")
    logger.info("=" * 80)
    logger.info("")
    logger.info("💡 提示：")
    logger.info("   - Team 模式（优化版）：Team Leader（融合维度确认） + 数据查询 Agent")
    logger.info("   - 优化点：减少一次 Agent 调用，更快响应，节约成本")
    logger.info("   - 查询前必须确认：先梳理维度，用户确认后再执行查询")
    logger.info("   - 支持用户补充：可以补充或修改查询维度")
    logger.info("   - 数据存储在 MySQL 数据库中（配置在 config/config.json）")
    logger.info("   - 按 Ctrl+C 停止服务")
    logger.info("")
else:
    # 如果已经初始化过，直接使用已创建的实例（不输出日志）
    # agent_os 和 app 已经在模块级别可用
    pass

# 路径重写中间件已禁用（网关路径前缀功能已移除）
# 所有请求直接按原路径处理，无需重写
# @app.middleware("http")
# async def rewrite_path_middleware(request: Request, call_next):
#     original_path = request.url.path
#     if GATEWAY_PATH_PREFIX and original_path.startswith(GATEWAY_PATH_PREFIX):
#         new_path = original_path[len(GATEWAY_PATH_PREFIX):] or "/"
#         request.scope["path"] = new_path
#         logger.debug(f"🔀 路径重写: {original_path} -> {new_path}")
#     response = await call_next(request)
#     return response
# ==================== 路径重写中间件结束 ====================


def main():
    """主函数 - 启动 AgentOS 服务"""
    
    # 统一端口配置：三个环境都使用7778端口（主服务）
    env = os.getenv("AGNO_ENV", "dev")
    agentos_port = os.getenv("AGENTOS_PORT")
    if agentos_port:
        port = int(agentos_port)
    else:
        # 三个环境统一使用7778端口
        port = 7778
    
    host = os.getenv("AGENTOS_HOST", "0.0.0.0")
    
    try:
        agent_os.serve(
            app="smart_product_selection_team_optimized:app",
            reload=False,
            host=host,
            port=port,
        )
        
    except KeyboardInterrupt:
        logger.info("\n收到键盘中断信号，正在停止服务...")
    except OSError as e:
        if "address already in use" in str(e):
            logger.error(f"❌ 端口 {port} 已被占用")
            logger.info("💡 解决方案：")
            logger.info("   1. 等待端口释放后重试")
            logger.info(f"   2. 使用其他端口：AGENTOS_PORT=7780 python3 smart_product_selection_team_optimized.py")
            logger.info(f"   3. 查找并停止占用端口的进程：lsof -i :{port}")
        else:
            logger.error(f"❌ 服务启动失败: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"❌ 服务启动失败: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()

