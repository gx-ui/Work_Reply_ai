from pathlib import Path
ROOT = Path(".").resolve()
p = ROOT / "config" / "config_loader.py"
text = p.read_text(encoding="utf-8")
old = """        return {
            \"base_url\": llm_config.get(\"base_url\"),
            \"api_key\": llm_config.get(\"api_key\"),
            \"model_name\": llm_config.get(\"model_name\"),
            \"temperature\": llm_config.get(\"temperature\", 0.1),
            \"timeout\": llm_config.get(\"timeout\", 120),
            \"max_retries\": llm_config.get(\"max_retries\", 3),
        }
    
    def get_embedding_config"""
new = """        return {
            \"base_url\": llm_config.get(\"base_url\"),
            \"api_key\": llm_config.get(\"api_key\"),
            \"model_name\": llm_config.get(\"model_name\"),
            \"summary_model\": llm_config.get(\"summary_model\"),
            \"temperature\": llm_config.get(\"temperature\", 0.1),
            \"timeout\": llm_config.get(\"timeout\", 120),
            \"max_retries\": llm_config.get(\"max_retries\", 3),
        }

    def get_mysql_config(self) -> Dict[str, Any]:
        mysql_cfg = self.config.get(\"mysql\")
        if not isinstance(mysql_cfg, dict):
            return {}
        required = (\"host\", \"port\", \"user\", \"password\", \"database\")
        if any(k not in mysql_cfg for k in required):
            return {}
        return {
            \"host\": mysql_cfg.get(\"host\"),
            \"port\": int(mysql_cfg.get(\"port\", 3306)),
            \"user\": mysql_cfg.get(\"user\"),
            \"password\": mysql_cfg.get(\"password\"),
            \"database\": mysql_cfg.get(\"database\"),
        }

    def get_session_persistence_config(self) -> Dict[str, Any]:
        sp = self.config.get(\"session_persistence\")
        if not isinstance(sp, dict):
            return {}
        return {
            \"work_reply_session_table\": sp.get(\"work_reply_session_table\", \"work_reply_ai_work_reply_session\"),
            \"work_reply_memory_table\": sp.get(\"work_reply_memory_table\", \"work_reply_ai_work_reply_memories\"),
            \"summary_session_table\": sp.get(\"summary_session_table\", \"work_reply_ai_summary_session\"),
            \"summary_memory_table\": sp.get(\"summary_memory_table\", \"work_reply_ai_summary_memories\"),
            \"num_history_runs\": int(sp.get(\"num_history_runs\", 10)),
        }
    
    def get_embedding_config"""
if old not in text:
    raise SystemExit("config_loader anchor missing")
p.write_text(text.replace(old, new), encoding="utf-8")
print("config_loader patched")
