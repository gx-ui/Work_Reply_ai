"""
MySQL 持久化初始化：供 Agno Agent 原生会话 / 历史存储使用。
参考 smart_product_selection_team_optimized.py 的连接池与 MySQLDb 用法。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event, text
from agno.db.mysql import MySQLDb
from config.config_loader import ConfigLoader

logger = logging.getLogger("mysql_store")


def _build_mysql_url(mysql_cfg: Dict[str, Any]) -> str:
    host = str(mysql_cfg["host"])
    port = int(mysql_cfg.get("port", 3306))
    user = quote_plus(str(mysql_cfg["user"]))
    password = quote_plus(str(mysql_cfg["password"]))
    database = quote_plus(str(mysql_cfg["database"]))
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4"


def create_shared_engine(mysql_cfg: Dict[str, Any]) -> Any:
    db_url = _build_mysql_url(mysql_cfg)
    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=10,
    )

    @event.listens_for(engine, "connect")
    def set_sort_buffer_size(dbapi_conn, _connection_record):
        try:
            cursor = dbapi_conn.cursor()
            cursor.execute("SET SESSION sort_buffer_size = 104857600")
            cursor.close()
        except Exception as e:
            logger.warning("设置 sort_buffer_size 失败: %s", e)

    return engine


def create_agent_mysql_dbs(
    engine: Any,
    database_name: str,
    *,
    work_reply_session_table: str = "work_reply_ai_work_reply_session",
    work_reply_memory_table: str = "work_reply_ai_work_reply_memories",
    summary_session_table: str = "work_reply_ai_summary_session",
    summary_memory_table: str = "work_reply_ai_summary_memories",
) -> Tuple[MySQLDb, MySQLDb]:
    db_work = MySQLDb(
        id="work-reply-ai-work-reply-db",
        db_engine=engine,
        db_schema=database_name,
        session_table=work_reply_session_table,
        memory_table=work_reply_memory_table,
    )
    db_summary = MySQLDb(
        id="work-reply-ai-summary-db",
        db_engine=engine,
        db_schema=database_name,
        session_table=summary_session_table,
        memory_table=summary_memory_table,
    )
    return db_work, db_summary


def init_mysql_for_agents_from_config(
    config: Optional[ConfigLoader] = None,
) -> Tuple[Optional[Any], Optional[MySQLDb], Optional[MySQLDb]]:
    cfg = config or ConfigLoader()
    persist = cfg.get_session_persistence_config()
    if not persist.get("enabled", True):
        logger.info("session_persistence.enabled=false，跳过 Agent MySQL 持久化")
        return None, None, None
    mysql_cfg = cfg.get_mysql_config()
    if not mysql_cfg:
        logger.warning("未配置 mysql，跳过 Agent 会话持久化")
        return None, None, None
    try:
        engine = create_shared_engine(mysql_cfg)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_work, db_summary = create_agent_mysql_dbs(
            engine,
            str(mysql_cfg["database"]),
            work_reply_session_table=str(
                persist.get("work_reply_session_table", "work_reply_ai_work_reply_session")
            ),
            work_reply_memory_table=str(
                persist.get("work_reply_memory_table", "work_reply_ai_work_reply_memories")
            ),
            summary_session_table=str(
                persist.get("summary_session_table", "work_reply_ai_summary_session")
            ),
            summary_memory_table=str(
                persist.get("summary_memory_table", "work_reply_ai_summary_memories")
            ),
        )
        logger.info(
            "Agent MySQL 持久化已启用 schema=%s",
            mysql_cfg["database"],
        )
        return engine, db_work, db_summary
    except Exception as e:
        logger.warning("Agent MySQL 初始化失败，将以无持久化模式运行: %s", e)
        return None, None, None
