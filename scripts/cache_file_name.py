"""
将知识库文件列表缓存到本地 JSON 文件
运行方式：python scripts/cache_file_list.py
"""
import json
import logging
from pathlib import Path
from tools.milvus_tool import create_milvus_tools
from config.config_loader import ConfigLoader
from utils.log_utils import configure_logging


logger = logging.getLogger("scripts.cache_file_name")

def main():
    configure_logging()
    config = ConfigLoader()
    milvus_config = config.get_milvus_config()
    embedder_config = config.get_embedding_config()
    milvus_tool = create_milvus_tools(milvus_config, embedder_config)
    
    # 获取所有文件名
    result = milvus_tool.list_chunks_metadata(include_content=False)
    
    # 保存到本地文件
    cache_file = Path(__file__).parent.parent / "config" / "cache_file_name.json"
    cache_file.parent.mkdir(exist_ok=True)
    
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    logger.info("缓存已更新: %s", cache_file)

if __name__ == "__main__":
    main()
