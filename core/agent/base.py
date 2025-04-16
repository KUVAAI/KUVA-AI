"""
KUVA AI Core Agent Base Class
"""
from __future__ import annotations

import abc
import asyncio
import inspect
import logging
from contextlib import AsyncContextDecorator
from functools import wraps
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    Optional,
    TypeVar,
    TYPE_CHECKING,
)

import pydantic
from pydantic import BaseModel, Field, PrivateAttr
from loguru import logger
from prometheus_client import Counter, Gauge, Histogram

if TYPE_CHECKING:
    from kuva.core.context import AgentContext
    from kuva.core.eventbus import EventBus
    from kuva.core.config import AgentConfigT

T = TypeVar("T", bound="AgentConfigT")

class AgentError(Exception):
    """Base exception for agent operations"""
    
    def __init__(self, 
                 message: str, 
                 retryable: bool = False,
                 code: str = "AGENT_ERROR"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code

class AgentLifecycleState:
    """Thread-safe state machine for agent lifecycle"""
    _states = {"INIT", "STARTING", "RUNNING", "PAUSED", "STOPPING", "TERMINATED"}
    
    def __init__(self):
        self._state = "INIT"
        self._lock = asyncio.Lock()
        
    async def transition(self, new_state: str) -> None:
        async with self._lock:
            if new_state not in self._states:
                raise AgentError(f"Invalid state transition: {self._state} -> {new_state}")
            # Validate state transitions here
            self._state = new_state

class AgentMetrics:
    """Prometheus metrics collector"""
    
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.metrics_prefix = "kuva_agent_"
        
        self.message_counter = Counter(
            f"{self.metrics_prefix}messages_total",
            "Total processed messages",
            ["agent_id", "message_type"]
        )
        
        self.processing_time = Histogram(
            f"{self.metrics_prefix}processing_seconds",
            "Message processing time",
            ["agent_id", "status"]
        )
        
        self.memory_usage = Gauge(
            f"{self.metrics_prefix}memory_bytes",
            "Agent memory usage",
            ["agent_id"]
        )

class BaseAgentConfig(BaseModel):
    """Base configuration model for agents"""
    agent_id: str = Field(..., min_length=3, max_length=64, regex=r"^[a-zA-Z0-9_-]+$")
    max_retries: int = Field(3, ge=0)
    heartbeat_interval: float = Field(5.0, gt=0.0)
    log_level: str = Field("INFO", regex=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    
    class Config:
        extra = "forbid"
        validate_assignment = True
        json_encoders = {
            # Add custom JSON encoders if needed
        }

class AgentSignalHandler:
    """OS signal handling for graceful shutdown"""
    
    def __init__(self):
        self.should_stop = asyncio.Event()
        
    async def handle_signals(self):
        """Async signal handler setup"""
        loop = asyncio.get_running_loop()
        for sig in {"SIGINT", "SIGTERM"}:
            loop.add_signal_handler(
                getattr(signal, sig),
                lambda: self.should_stop.set()
            )

class AgentBase(Generic[T], AsyncContextDecorator):
    """Abstract base class for all KUVA AI agents"""
    
    _config: T
    _context: AgentContext
    _event_bus: EventBus
    _metrics: AgentMetrics
    _state: AgentLifecycleState
    _dependencies: Dict[str, AgentBase]
    
    def __init__(
        self,
        config: T,
        context: AgentContext,
        dependencies: Optional[Dict[str, AgentBase]] = None
    ):
        self._config = config
        self._context = context
        self._event_bus = context.event_bus
        self._metrics = AgentMetrics(config.agent_id)
        self._state = AgentLifecycleState()
        self._dependencies = dependencies or {}
        self._setup_logging()
        self._signal_handler = AgentSignalHandler()
        
    def _setup_logging(self) -> None:
        """Configure agent-specific logging"""
        logger.configure(
            handlers=[
                {
                    "sink": sys.stderr,
                    "level": self._config.log_level,
                    "format": self._get_log_format(),
                }
            ]
        )
        
    def _get_log_format(self) -> str:
        return (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[agent_id]}</cyan> | "
            "<level>{message}</level>"
        )
    
    @abc.abstractmethod
    async def process_message(self, message: Any) -> Any:
        """Process incoming message (implement in subclasses)"""
        raise NotImplementedError
    
    async def _heartbeat(self) -> None:
        """Periodic health reporting"""
        while True:
            try:
                await self._event_bus.publish(
                    "agent.heartbeat",
                    {"agent_id": self._config.agent_id}
                )
                await asyncio.sleep(self._config.heartbeat_interval)
            except asyncio.CancelledError:
                break
                
    async def __aenter__(self):
        await self.start()
        return self
        
    async def __aexit__(self, exc_type, exc, tb):
        await self.stop()
        
    async def start(self) -> None:
        """Start agent operations"""
        await self._state.transition("STARTING")
        
        # Initialize dependencies
        for dep in self._dependencies.values():
            if inspect.isawaitable(dep):
                await dep
                
        # Start background tasks
        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        
        await self._state.transition("RUNNING")
        
    async def stop(self) -> None:
        """Graceful shutdown procedure"""
        await self._state.transition("STOPPING")
        
        # Cancel background tasks
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass
            
        # Cleanup resources
        await self._event_bus.unsubscribe_all()
        
        await self._state.transition("TERMINATED")
        
    def retry_policy(self) -> Callable:
        """Decorator for retryable operations"""
        def decorator(func: Callable[..., Awaitable[Any]]) -> Callable:
            @wraps(func)
            async def wrapper(*args, **kwargs):
                for attempt in range(self._config.max_retries + 1):
                    try:
                        return await func(*args, **kwargs)
                    except AgentError as e:
                        if not e.retryable or attempt == self._config.max_retries:
                            raise
                        logger.warning(f"Retry attempt {attempt + 1} for {func.__name__}")
                        await asyncio.sleep(2 ** attempt)
                raise AgentError("Max retries exceeded", code="RETRY_LIMIT")
            return wrapper
        return decorator
    
    def register_rpc(self, method_name: str) -> Callable:
        """Decorator to expose methods via RPC"""
        def decorator(func: Callable) -> Callable:
            self._context.rpc_server.register(
                f"{self._config.agent_id}.{method_name}",
                func
            )
            return func
        return decorator
    
    @property
    def status(self) -> Dict[str, Any]:
        """Current agent status snapshot"""
        return {
            "agent_id": self._config.agent_id,
            "state": self._state._state,
            "metrics": {
                "message_count": self._metrics.message_counter._samples()[0][2],
                "memory_usage": self._metrics.memory_usage._value.get()
            }
        }

# Generic agent factory for dependency injection
class AgentFactory:
    _registry: Dict[str, Type[AgentBase]] = {}
    
    @classmethod
    def register(cls, name: str) -> Callable[[Type[AgentBase]], Type[AgentBase]]:
        def decorator(agent_cls: Type[AgentBase]) -> Type[AgentBase]:
            cls._registry[name] = agent_cls
            return agent_cls
        return decorator
    
    @classmethod
    def create(
        cls,
        name: str,
        config: T,
        context: AgentContext,
        dependencies: Dict[str, AgentBase]
    ) -> AgentBase:
        agent_cls = cls._registry[name]
        return agent_cls(config, context, dependencies)
