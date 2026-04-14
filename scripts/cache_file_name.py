"""
将知识库 file_name 列表缓存到 config/cache_<collection_name>.json（与 MilvusSearchTool 一致）。

同时刷新：
  - 主库 milvus（如 kefubuzhishiku1028ban_...）
  - 注意事项库 milvus_zhuyishixiang（如 kefushouhouxiangmuzhuyishixiang_...）

运行方式：python scripts/cache_file_name.py

说明：list_chunks_metadata 若发现已有缓存文件会直接读文件、不会回源 Milvus；
因此刷新前先删除目标文件，强制从 Milvus 拉取并写回。

从 scripts 目录或其它 cwd 直接运行本脚本时，需将项目根目录加入 PYTHONPATH。
"""
import sys
import logging
from pathlib import Path
from typing import Any, Dict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.milvus_tool import create_milvus_tools
from config.config_loader import ConfigLoader
from utils.log_utils import configure_logging


logger = logging.getLogger("scripts.cache_file_name")

CONFIG_ROOT = _PROJECT_ROOT


def _cache_path_for_collection(collection_name: str) -> Path:
    return CONFIG_ROOT / "config" / f"cache_{collection_name}.json"


def refresh_collection_cache(
    label: str,
    milvus_config: Dict[str, Any],
    embedder_config: Dict[str, Any],
) -> Path:
    collection_name = milvus_config.get("collection_name") or ""
    if not collection_name:
        raise ValueError(f"[{label}] milvus 配置缺少 collection_name")

    cache_file = _cache_path_for_collection(collection_name)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if cache_file.exists():
        cache_file.unlink()
        logger.info("[%s] 已删除旧缓存以强制回源: %s", label, cache_file)

    milvus_tool = create_milvus_tools(milvus_config, embedder_config)
    # MilvusSearchTool 在回源后会自动写入 cache_<collection_name>.json
    result = milvus_tool.list_chunks_metadata(include_content=False)

    n = result.get("unique_total_entities", 0)
    logger.info("[%s] 缓存已更新: %s（unique_total_entities=%s）", label, cache_file, n)
    return cache_file


def main() -> None:
    configure_logging()
    config = ConfigLoader()
    embedder_config = config.get_embedding_config()

    tasks = [
        ("主库 milvus", config.get_milvus_config()),
        ("注意事项 milvus_zhuyishixiang", config.get_milvus_config_by_key("milvus_zhuyishixiang")),
    ]
    for label, mv in tasks:
        try:
            refresh_collection_cache(label, mv, embedder_config)
        except Exception as e:
            logger.exception("[%s] 刷新失败（已跳过，继续下一项）: %s", label, e)


if __name__ == "__main__":
    main()
