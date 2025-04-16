"""
KUVA AI Structured Logging System (Enterprise Edition)
"""
from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

import structlog
from structlog.types import Processor, WrappedLogger
from pydantic import BaseModel, Field, validator
from prometheus_client import Counter

# ======================
# Metrics Configuration
# ======================
LOG_METRICS = Counter(
    "kuva_log_events_total",
    "Total log events by level and service",
    ["level", "service", "environment"]
)

# ======================
# Configuration Models
# ======================
class LoggingConfig(BaseModel):
    service_name: str = Field(..., env="LOG_SERVICE_NAME")
    log_level: str = Field("info", env="LOG_LEVEL")
    json_format: bool = Field(True, env="LOG_JSON_FORMAT")
    enable_async: bool = Field(True, env="LOG_ASYNC")
    log_path: Optional[str] = Field(None, env="LOG_PATH")
    syslog_enabled: bool = Field(False, env="SYSLOG_ENABLED")
    sensitive_keys: Tuple[str, ...] = ("password", "token", "secret")
    enable_metrics: bool = Field(True, env="LOG_METRICS")
    trace_enabled: bool = Field(True, env="LOG_TRACE_CONTEXT")
    buffer_size: int = Field(1000, env="LOG_BUFFER_SIZE")

    @validator("log_level")
    def validate_log_level(cls, value):
        return value.lower()

# ======================
# Security Processors
# ======================
def sensitive_data_filter(
    logger: WrappedLogger,
    method: str,
    event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Redact sensitive information from log events"""
    for key in event_dict:
        if any(sensitive in key.lower() for sensitive in config.sensitive_keys):
            event_dict[key] = "***REDACTED***"
    return event_dict

def pii_scrubber(value: Any) -> Any:
    """Scrub PII patterns from log values"""
    if isinstance(value, str):
        value = re.sub(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b", "***PAYMENT***", value)
        value = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "***SSN***", value)
    return value

# ======================
# Context Processors
# ======================
def add_service_context(
    logger: WrappedLogger,
    method: str,
    event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Inject service-wide context into all logs"""
    event_dict["service"] = config.service_name
    event_dict["environment"] = os.getenv("ENV", "development")
    event_dict["commit_sha"] = os.getenv("GIT_COMMIT", "")
    return event_dict

def add_trace_context(
    logger: WrappedLogger,
    method: str,
    event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Inject distributed tracing context"""
    if config.trace_enabled:
        trace_id = structlog.contextvars.get_contextvars().get("trace_id")
        if trace_id:
            event_dict["trace_id"] = trace_id
    return event_dict

# ======================
# Metrics Processors
# ======================
def metrics_emitter(
    logger: WrappedLogger,
    method: str,
    event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Emit log metrics to Prometheus"""
    if config.enable_metrics:
        LOG_METRICS.labels(
            level=method,
            service=config.service_name,
            environment=os.getenv("ENV", "development")
        ).inc()
    return event_dict

# ======================
# Exception Handling
# ======================
def add_exception_info(
    logger: WrappedLogger,
    method: str,
    event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Add exception details to log events"""
    exc_info = event_dict.pop("exc_info", None)
    if exc_info:
        event_dict["exception"] = {
            "type": exc_info[0].__name__,
            "message": str(exc_info[1]),
            "stack": structlog.processors.format_exc_info(exc_info)
        }
    return event_dict

# ======================
# Output Processors
# ======================
def get_renderer() -> Processor:
    """Get appropriate log renderer based on config"""
    if config.json_format:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer()

def configure_outputs() -> list[Processor]:
    """Configure log outputs based on environment"""
    outputs = []
    
    # Always log to stdout
    outputs.append(structlog.stdlib.ProcessorFormatter.wrap_for_formatter)
    
    if config.log_path:
        outputs.append(structlog.WriteFileProcessor(config.log_path))
        
    if config.syslog_enabled:
        outputs.append(structlog.SysLogProcessor(address=('logs.kuva.ai', 514)))
    
    return outputs

# ======================
# Async Configuration
# ======================
def async_wrapper(processors: list[Processor]) -> list[Processor]:
    """Wrap processors in async queue if enabled"""
    if config.enable_async:
        return [
            structlog.stdlib.AsyncBoundLogger,
            structlog.stdlib.BoundLogger,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            structlog.processors.AsyncIOProcessor(queue_limit=config.buffer_size),
        ] + processors
    return processors

# ======================
# Core Configuration
# ======================
config = LoggingConfig()

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.contextvars.merge_contextvars,
        add_service_context,
        add_trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        sensitive_data_filter,
        structlog.processors.EventRenamer("message"),
        add_exception_info,
        pii_scrubber,
        metrics_emitter,
        structlog.processors.CallsiteParameterAdder(
            parameters=[
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            ]
        ),
        *configure_outputs(),
        get_renderer(),
    ],
    wrapper_class=structlog.stdlib.AsyncBoundLogger if config.enable_async else structlog.stdlib.BoundLogger,
    context_class=structlog.threadlocal.wrap_dict(dict),
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_secret_on_notice=True,
)

# ======================
# Runtime Initialization
# ======================
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=config.log_level.upper(),
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(config.service_name)

# ======================
# Enterprise Extensions
# ======================
class SecureLogger:
    """Enterprise security wrapper for structured logging"""
    
    @staticmethod
    def audit(event: str, **kwargs):
        """Security audit log entry"""
        with structlog.contextvars.bound_contextvars(log_type="audit"):
            log.info(event, **kwargs)
    
    @staticmethod
    def with_trace(trace_id: str):
        """Add distributed tracing context"""
        return structlog.contextvars.bound_contextvars(trace_id=trace_id)

# ======================
# Usage Example
# ======================
if __name__ == "__main__":
    SecureLogger.with_trace(str(uuid.uuid4())):
        log.info(
            "service_startup",
            version="1.0.0",
            config={"level": "debug"},
            sensitive_data="should_be_redacted"
        )
        
        try:
            1 / 0
        except ZeroDivisionError:
            log.error("calculation_failed", dividend=42, exc_info=True)
