"""
配置加载模块
用于读取和解析配置文件，提供统一的配置访问接口
"""

import json
import logging
from typing import Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger("config_loader")

class ConfigLoader:
    _instance: Optional['ConfigLoader'] = None
    
    def __new__(cls, config_path: Optional[str] = None):
        """
        单例模式实现：确保全局只有一个 ConfigLoader 实例
        """
        if cls._instance is None:
            cls._instance = super(ConfigLoader, cls).__new__(cls)
            # 只有第一次创建实例时才初始化
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置加载器
        
        Args:
            config_path: 配置文件路径，如果为 None 则自动查找
        """
        if getattr(self, "_initialized", False):
            return
            
        if config_path is None:
            config_path = self._find_config_file()
        
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self._initialized = True
    def _find_config_file(self) -> str:
        """
        自动查找配置文件路径
        
        Returns:
            配置文件路径
            
        Raises:
            FileNotFoundError: 如果找不到配置文件
        """
        # 获取当前文件所在目录（core/）
        current_dir = Path(__file__).parent
        # 获取项目根目录
        project_root = current_dir.parent

        config_path = project_root / "config" / "config.json"
        if config_path.exists():
            return str(config_path)

        raise FileNotFoundError(f"未找到配置文件: {config_path}")
    
    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """
        加载 JSON 配置文件
        
        Args:
            config_path: 配置文件路径
            
        Returns:
            配置字典
            
        Raises:
            FileNotFoundError: 如果文件不存在
            json.JSONDecodeError: 如果 JSON 格式错误
        """
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            logger.info("成功加载配置文件: %s", config_path)
            return config
        
        except FileNotFoundError:
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"配置文件 JSON 格式错误: {e}")
        except Exception as e:
            raise RuntimeError(f"加载配置文件失败: {e}")


    def get_llm_config(self) -> Dict[str, Any]:
        """
        获取 LLM 模型配置
        
        Returns:
            LLM 配置字典，包含 base_url、api_key、model_name 等
        """
        llm_config = self.config.get("llm", {})

        return {
            "base_url": llm_config.get("base_url"),
            "api_key": llm_config.get("api_key"),
            "model_name": llm_config.get("model_name"),
            "summary_model": llm_config.get("summary_model"),
            "temperature": llm_config.get("temperature", 0.1),
            "timeout": llm_config.get("timeout", 120),
            "max_retries": llm_config.get("max_retries", 3),
        }

    def get_mysql_config(self) -> Optional[Dict[str, Any]]:
        """
        获取 MySQL 连接配置；未配置 host 时返回 None。
        """
        mysql = self.config.get("mysql") or {}
        if not mysql.get("host"):
            return None
        return {
            "host": mysql.get("host"),
            "port": int(mysql.get("port", 3306)),
            "user": mysql.get("user"),
            "password": mysql.get("password"),
            "database": mysql.get("database"),
        }

    def get_session_persistence_config(self) -> Dict[str, Any]:
        """Agent 会话 MySQL 持久化相关配置。"""
        return dict(self.config.get("session_persistence") or {})
    
    def get_embedding_config(self) -> Dict[str, Any]:
        """
        获取 Embedding 模型配置
        
        Returns:
            Embedding 配置字典
        """
        embedding_config = self.config.get("embedding", {})

        return {
            "model_name": embedding_config.get("model_name"),
            "base_url": embedding_config.get("base_url"),
            "api_key": embedding_config.get("api_key"),
        }
    
    def get_milvus_config(self) -> Dict[str, Any]:
        """
        获取 Milvus 向量数据库配置（主知识库）
        """
        milvus_config = self.config.get("milvus", {})
        return {
            "host": milvus_config.get("host"),
            "port": int(milvus_config.get("port", 19530)),
            "db_name": milvus_config.get("db_name", "default"),
            "collection_name": milvus_config.get("collection_name"),
            "dim": milvus_config.get("dim", 2048),
            "limit": milvus_config.get("limit", 5),
            "search_params": milvus_config.get("search_params", {}),
        }

    def get_milvus_config_by_key(self, key: str) -> Dict[str, Any]:
        """
        按配置键名获取指定 Milvus 集合配置。
        用于多集合场景（如 milvus_kefu_shouhou、milvus_zhuyishixiang）。
        缺失的连接参数（host/port/db_name）自动从主 milvus 配置继承。

        Args:
            key: 配置键名，如 "milvus_kefu_shouhou" 或 "milvus_zhuyishixiang"

        Returns:
            Milvus 配置字典
        """
        base = self.config.get("milvus", {})
        extra = self.config.get(key, {})
        return {
            "host":            extra.get("host")            or base.get("host"),
            "port":            int(extra.get("port")        or base.get("port", 19530)),
            "db_name":         extra.get("db_name")         or base.get("db_name", "default"),
            "collection_name": extra.get("collection_name") or "",
            "dim":             extra.get("dim")             or base.get("dim", 2048),
            "limit":           extra.get("limit")           or base.get("limit", 5),
            "search_params":   extra.get("search_params")   or base.get("search_params", {}),
            "output_field":    extra.get("output_field")    or base.get("output_field", "content"),
        }
    
    def get_rerank_config(self) -> Dict[str, Any]:
        """
        获取 Rerank 重排序模型配置
        
        Returns:
            Rerank 配置字典，包含 enabled、model_name、api_key 等
        """
        rerank_config = self.config.get("rerank", {})
        
        return {
            "enabled": rerank_config.get("enabled", False),
            "model_name": rerank_config.get("model_name", "gte-rerank-v2"),
            "base_url": rerank_config.get("base_url"),
            "api_key": rerank_config.get("api_key"),
            "max_tokens": rerank_config.get("max_tokens", 4096),
            "top_k": rerank_config.get("top_k", 3),
            "threshold": rerank_config.get("threshold", 0.3),
            "fallback_to_direct_query": rerank_config.get("fallback_to_direct_query", True),
        }
