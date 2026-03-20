import logging
import sys

class ColorFormatter(logging.Formatter):
    """带颜色和 emoji 的日志格式化器"""
    
    COLORS = {
        'DEBUG': '\033[36m',      # 青色
        'INFO': '\033[32m',       # 绿色
        'WARNING': '\033[33m',    # 黄色
        'ERROR': '\033[31m',      # 红色
        'CRITICAL': '\033[35m',   # 紫色
    }
    RESET = '\033[0m'
    
    EMOJIS = {
        'DEBUG': '🔍',
        'INFO': '✅',
        'WARNING': '⚠️',
        'ERROR': '❌',
        'CRITICAL': '🔥',
    }
    
    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        emoji = self.EMOJIS.get(record.levelname, 'ℹ️')
        record.levelname = f"{emoji} {color}{record.levelname}{self.RESET}"
        return super().format(record)

def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s\n%(message)s\n{'-' * 80}",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    for handler in logging.root.handlers:
        handler.setFormatter(ColorFormatter('%(asctime)s %(levelname)s %(name)s\n%(message)s\n' + '-' * 80))
