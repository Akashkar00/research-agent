import logging
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


class LogSchema(BaseModel):
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    level: str
    logger: str
    message: str
    job_id: Optional[str] = None
    session_id: Optional[str] = None
    topic: Optional[str] = None
    
    class Config:
        extra = "allow"


class PydanticJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Standard LogRecord attributes that we want to ignore when looking for custom "extra" fields
        standard_attrs = {
            "args", "asctime", "created", "exc_info", "exc_text", "filename",
            "funcName", "levelname", "levelno", "lineno", "module",
            "msecs", "message", "msg", "name", "pathname", "process",
            "processName", "relativeCreated", "stack_info", "thread", "threadName", "taskName"
        }
        
        extra_fields = {
            k: v for k, v in record.__dict__.items()
            if k not in standard_attrs
        }
        
        log_entry = LogSchema(
            timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            level=record.levelname,
            logger=record.name,
            message=record.getMessage(),
            **extra_fields
        )
        return log_entry.model_dump_json(exclude_none=True)


def setup_logger(name: str = "app") -> logging.Logger:
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = PydanticJSONFormatter()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        
    return logger

def get_logger(name: str = "app") -> logging.Logger:
    return setup_logger(name)
