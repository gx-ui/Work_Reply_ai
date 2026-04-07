import logging
import logging.handlers
import re
from pathlib import Path


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\uFE0F"
    "]+",
    flags=re.UNICODE,
)


def _normalize_log_text(value: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", str(value or ""))
    text = _EMOJI_RE.sub("", text)
    return text


class PlainTextFormatter(logging.Formatter):
    """将日志格式化为纯文本，去除颜色控制符和 emoji。"""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return _normalize_log_text(rendered)


def _build_log_file_path() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    log_dir = project_root / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "log.txt"


def _build_file_handler() -> logging.Handler:
    handler = logging.FileHandler(_build_log_file_path(), mode="a", encoding="utf-8")
    handler.setFormatter(
        PlainTextFormatter("%(asctime)s %(levelname)s %(name)s\n%(message)s\n" + "-" * 80)
    )
    return handler


def configure_logging() -> None:
    file_handler = _build_file_handler()
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler],
        force=True,
    )
    logging.captureWarnings(True)

    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "asyncio",
    ):
        target_logger = logging.getLogger(logger_name)
        target_logger.handlers.clear()
        target_logger.propagate = True
