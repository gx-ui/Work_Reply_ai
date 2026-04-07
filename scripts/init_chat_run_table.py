# -*- coding: utf-8 -*-
"""
根据 config.json 中的 mysql 配置，执行 db/schema/work_reply_chat_run.sql 建表。

用法（项目根目录）:
    python scripts/init_chat_run_table.py

若库中仍是旧版（含 detail_json 列），请改用 db/schema/migrate_work_reply_chat_run_detail_to_columns.sql 做结构迁移，勿重复执行本脚本的 CREATE。

Agno 只会自动管理其自带的 session/memory 表结构；业务表需自行建表或运行本脚本。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from config.config_loader import ConfigLoader
from db.mysql_store import create_shared_engine
from utils.log_utils import configure_logging


logger = logging.getLogger("scripts.init_chat_run_table")


def _load_ddl() -> str:
    sql_path = ROOT / "db" / "schema" / "work_reply_chat_run.sql"
    if not sql_path.is_file():
        raise FileNotFoundError(f"未找到 DDL 文件: {sql_path}")
    return sql_path.read_text(encoding="utf-8")


def main() -> int:
    configure_logging()
    cfg = ConfigLoader()
    mysql_cfg = cfg.get_mysql_config()
    if not mysql_cfg:
        logger.error("config.json 未配置 mysql（需要 host/user/password/database）")
        return 1

    ddl = _load_ddl()
    # 去掉单行注释，避免部分驱动对注释解析挑剔
    lines = []
    for line in ddl.splitlines():
        s = line.strip()
        if s.startswith("--"):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    if not cleaned:
        logger.error("DDL 为空")
        return 1

    engine = create_shared_engine(mysql_cfg)
    with engine.begin() as conn:
        conn.execute(text(cleaned))

    table = cfg.get_chat_run_persistence_config().get("table", "work_reply_chat_run")
    dbname = mysql_cfg.get("database", "")
    logger.info("已执行建表脚本，库=%r 表=%r", dbname, table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
