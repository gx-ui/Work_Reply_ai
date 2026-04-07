"""
MySQL 连接池：供业务表（如 work_reply_chat_run）写入；不再挂载 Agno MySQLDb 会话表。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from config.config_loader import ConfigLoader

logger = logging.getLogger("mysql_store")


def _build_mysql_url(mysql_cfg: Dict[str, Any]) -> str:
    from urllib.parse import quote_plus

    host = str(mysql_cfg["host"])
    port = int(mysql_cfg.get("port", 3306))
    user = quote_plus(str(mysql_cfg["user"]))
    password = quote_plus(str(mysql_cfg["password"]))
    database = quote_plus(str(mysql_cfg["database"]))
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4"


def create_shared_engine(mysql_cfg: Dict[str, Any]) -> Engine:
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


def init_mysql_engine_from_config(
    config: Optional[ConfigLoader] = None,
) -> Optional[Engine]:
    """
    若配置了 mysql，则创建共享 Engine（用于 chat_run 等业务写入）。
    失败或未配置时返回 None。
    """
    cfg = config or ConfigLoader()
    mysql_cfg = cfg.get_mysql_config()
    if not mysql_cfg:
        logger.info("未配置 mysql，跳过 MySQL Engine 初始化")
        return None
    try:
        engine = create_shared_engine(mysql_cfg)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("MySQL Engine 已就绪 schema=%s", mysql_cfg.get("database"))
        return engine
    except Exception as e:
        logger.warning("MySQL Engine 初始化失败: %s", e)
        return None
