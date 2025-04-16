"""
KUVA AI Agent Lifecycle Management System
"""
from __future__ import annotations

import abc
import asyncio
import enum
import inspect
import logging
import threading
import time
from collections import deque
from contextlib import AbstractAsyncContextManager
from functools import partial
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    ClassVar,
    Deque,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Set,
    Type,
    TypeVar,
    Union,
)

import anyio
import pydantic
from loguru import logger
from pydantic import BaseModel, Field, validator
from typing_extensions import Protocol, Self

from kuva.common.exceptions import (
    LifecycleError,
    DependencyError,
    ConfigurationError,
    TimeoutError,
)
from kuva.common.interfaces import HealthStatus
from kuva.common.types import JSONSerializable
from kuva.core.agent.base import AgentBase
from kuva.core.agent.registry import AgentRegistry
from kuva.core.interfaces import (
    Middleware,
    ObservabilityClient,
    PluginSpec,
    StateStore,
)
from kuva.core.rpc import RPCClient

T = TypeVar("T", bound=AgentBase)
ConfigT = TypeVar("ConfigT", bound=BaseModel)
StateT = TypeVar("StateT", bound=BaseModel)

class LifecyclePhase(enum.Enum):
    PRE_INIT = "pre_initialization"
    POST_INIT = "post_initialization"
    PRE_START = "pre_start"
    POST_START = "post_start"
    PRE_STOP = "pre_stop"
    POST_STOP = "post_stop"
    PRE_PAUSE = "pre_pause"
    POST_PAUSE = "post_pause"
    PRE_RESUME = "pre_resume"
    POST_RESUME = "post_resume"

class AgentState(enum.Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"

class LifecycleConfig(BaseModel):
    startup_timeout: float = Field(30.0, gt=0)
    shutdown_timeout: float = Field(30.0, gt=0)
    health_check_interval: float = Field(5.0, gt=0)
    max_restart_attempts: int = Field(3, ge=0)
    dependency_init_mode: str = Field("parallel", regex="^(parallel|sequential)$")
    state_persistence: bool = Field(True)
    enable_graceful_shutdown: bool = Field(True)

    @validator("startup_timeout")
    def validate_startup(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("Minimum startup timeout is 1 second")
        return v

class LifecycleEvent(BaseModel):
    timestamp: float = Field(default_factory=time.time)
    phase: LifecyclePhase
    agent_id: str
    state: AgentState
    metadata: Dict[str, JSONSerializable] = {}

class LifecycleMiddleware(Middleware):
    @abc.abstractmethod
    async def on_phase(
        self,
        phase: LifecyclePhase,
        agent: AgentBase,
        context: Dict[str, Any],
    ) -> None:
        pass

class LifecycleManager(Generic[T]):
    _registry: ClassVar[Dict[str, LifecycleManager]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        agent_cls: Type[T],
        config: Optional[LifecycleConfig] = None,
        observability: Optional[ObservabilityClient] = None,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self.agent_cls = agent_cls
        self.config = config or LifecycleConfig()
        self.observability = observability
        self.state_store = state_store
        self._agent_instance: Optional[T] = None
        self._state: AgentState = AgentState.CREATED
        self._dependencies: Set[LifecycleManager] = set()
        self._health_status: HealthStatus = HealthStatus.UNKNOWN
        self._lifecycle_events: Deque[LifecycleEvent] = deque(maxlen=1000)
        self._middlewares: List[LifecycleMiddleware] = []
        self._task: Optional[asyncio.Task] = None
        self._state_lock = anyio.Lock()
        self._rpc = RPCClient()
        self._restart_count = 0

    @classmethod
    def get_manager(cls, agent_id: str) -> LifecycleManager:
        with cls._lock:
            if agent_id not in cls._registry:
                raise LifecycleError(f"Lifecycle manager for {agent_id} not found")
            return cls._registry[agent_id]

    def register_middleware(self, middleware: LifecycleMiddleware) -> None:
        self._middlewares.append(middleware)

    async def initialize(self, init_context: Dict[str, Any] = None) -> None:
        async with self._state_lock:
            if self._state != AgentState.CREATED:
                raise LifecycleError("Agent already initialized")

            await self._set_state(AgentState.INITIALIZING)
            init_context = init_context or {}

            try:
                # Load persisted state
                if self.config.state_persistence and self.state_store:
                    stored_state = await self.state_store.load(self.agent_id)
                    if stored_state:
                        init_context.update(stored_state)

                # Resolve dependencies
                await self._resolve_dependencies()

                # Run middlewares
                await self._run_middlewares(LifecyclePhase.PRE_INIT, init_context)

                # Create instance
                self._agent_instance = self.agent_cls(**init_context)

                # Post-initialization
                if inspect.iscoroutinefunction(self._agent_instance.post_init):
                    await self._agent_instance.post_init()
                else:
                    self._agent_instance.post_init()

                # Persist state
                if self.config.state_persistence and self.state_store:
                    await self.state_store.save(self.agent_id, self._agent_instance.state_dict())

                await self._run_middlewares(LifecyclePhase.POST_INIT, init_context)
                await self._set_state(AgentState.INITIALIZED)

            except Exception as e:
                await self._handle_error(e)
                raise LifecycleError("Initialization failed") from e

    async def start(self, start_context: Dict[str, Any] = None) -> None:
        async with self._state_lock:
            if self._state not in (AgentState.INITIALIZED, AgentState.STOPPED):
                raise LifecycleError(f"Cannot start from state {self._state}")

            await self._set_state(AgentState.STARTING)
            start_context = start_context or {}

            try:
                # Start dependencies
                if self.config.dependency_init_mode == "sequential":
                    for dep in self._dependencies:
                        await dep.start(start_context)
                else:
                    await asyncio.gather(*(dep.start(start_context) for dep in self._dependencies))

                # Run middlewares
                await self._run_middlewares(LifecyclePhase.PRE_START, start_context)

                # Start agent
                if not self._agent_instance:
                    raise LifecycleError("Agent not initialized")

                if inspect.iscoroutinefunction(self._agent_instance.start):
                    await self._agent_instance.start()
                else:
                    self._agent_instance.start()

                # Start health monitoring
                self._task = asyncio.create_task(self._monitor_health())

                await self._run_middlewares(LifecyclePhase.POST_START, start_context)
                await self._set_state(AgentState.RUNNING)

            except Exception as e:
                await self._handle_error(e)
                raise LifecycleError("Start failed") from e

    async def stop(self, stop_context: Dict[str, Any] = None) -> None:
        async with self._state_lock:
            if self._state not in (AgentState.RUNNING, AgentState.PAUSED, AgentState.ERROR):
                raise LifecycleError(f"Cannot stop from state {self._state}")

            await self._set_state(AgentState.STOPPING)
            stop_context = stop_context or {}

            try:
                # Run middlewares
                await self._run_middlewares(LifecyclePhase.PRE_STOP, stop_context)

                # Stop agent
                if self.config.enable_graceful_shutdown:
                    if inspect.iscoroutinefunction(self._agent_instance.graceful_stop):
                        await self._agent_instance.graceful_stop()
                    else:
                        self._agent_instance.graceful_stop()
                else:
                    if inspect.iscoroutinefunction(self._agent_instance.stop):
                        await self._agent_instance.stop()
                    else:
                        self._agent_instance.stop()

                # Cancel health monitoring
                if self._task:
                    self._task.cancel()
                    try:
                        await self._task
                    except asyncio.CancelledError:
                        pass

                # Stop dependencies
                await asyncio.gather(*(dep.stop() for dep in self._dependencies))

                # Persist state
                if self.config.state_persistence and self.state_store:
                    await self.state_store.save(self.agent_id, self._agent_instance.state_dict())

                await self._run_middlewares(LifecyclePhase.POST_STOP, stop_context)
                await self._set_state(AgentState.STOPPED)

            except Exception as e:
                await self._handle_error(e)
                raise LifecycleError("Stop failed") from e

    async def pause(self) -> None:
        async with self._state_lock:
            if self._state != AgentState.RUNNING:
                raise LifecycleError(f"Cannot pause from state {self._state}")

            await self._set_state(AgentState.PAUSING)
            try:
                await self._run_middlewares(LifecyclePhase.PRE_PAUSE, {})
                if inspect.iscoroutinefunction(self._agent_instance.pause):
                    await self._agent_instance.pause()
                else:
                    self._agent_instance.pause()
                await self._run_middlewares(LifecyclePhase.POST_PAUSE, {})
                await self._set_state(AgentState.PAUSED)
            except Exception as e:
                await self._handle_error(e)
                raise LifecycleError("Pause failed") from e

    async def resume(self) -> None:
        async with self._state_lock:
            if self._state != AgentState.PAUSED:
                raise LifecycleError(f"Cannot resume from state {self._state}")

            await self._set_state(AgentState.RESUMING)
            try:
                await self._run_middlewares(LifecyclePhase.PRE_RESUME, {})
                if inspect.iscoroutinefunction(self._agent_instance.resume):
                    await self._agent_instance.resume()
                else:
                    self._agent_instance.resume()
                await self._run_middlewares(LifecyclePhase.POST_RESUME, {})
                await self._set_state(AgentState.RUNNING)
            except Exception as e:
                await self._handle_error(e)
                raise LifecycleError("Resume failed") from e

    async def restart(self) -> None:
        if self._restart_count >= self.config.max_restart_attempts:
            raise LifecycleError("Max restart attempts reached")

        try:
            await self.stop()
            await self.initialize()
            await self.start()
            self._restart_count = 0
        except Exception as e:
            self._restart_count += 1
            raise LifecycleError(f"Restart failed (attempt {self._restart_count}): {e}") from e

    @property
    def agent_id(self) -> str:
        return f"{self.agent_cls.__module__}.{self.agent_cls.__name__}"

    @property
    def current_state(self) -> AgentState:
        return self._state

    @property
    def health(self) -> HealthStatus:
        return self._health_status

    async def _resolve_dependencies(self) -> None:
        registry = AgentRegistry()
        dependencies = self.agent_cls.dependencies()

        for dep_name, dep_cls in dependencies.items():
            if not registry.agent_exists(dep_cls):
                raise DependencyError(f"Dependency {dep_cls} not registered")
            
            dep_manager = LifecycleManager(
                dep_cls,
                config=self.config,
                observability=self.observability,
                state_store=self.state_store,
            )
            self._dependencies.add(dep_manager)

    async def _run_middlewares(
        self,
        phase: LifecyclePhase,
        context: Dict[str, Any],
    ) -> None:
        for middleware in self._middlewares:
            try:
                await middleware.on_phase(phase, self._agent_instance, context)
            except Exception as e:
                logger.error(f"Middleware failed in {phase}: {e}")

    async def _set_state(self, new_state: AgentState) -> None:
        event = LifecycleEvent(
            phase=LifecyclePhase(f"transition_to_{new_state.value}"),
            agent_id=self.agent_id,
            state=new_state,
        )

        self._state = new_state
        self._lifecycle_events.append(event)

        if self.observability:
            await self.observability.emit_lifecycle_event(event)

    async def _monitor_health(self) -> None:
        while True:
            try:
                if not self._agent_instance:
                    self._health_status = HealthStatus.DOWN
                    continue

                if inspect.iscoroutinefunction(self._agent_instance.health_check):
                    status = await self._agent_instance.health_check()
                else:
                    status = self._agent_instance.health_check()

                self._health_status = status

                if status == HealthStatus.UNHEALTHY:
                    logger.warning(f"Agent {self.agent_id} unhealthy")
                    await self._handle_unhealthy()

                await asyncio.sleep(self.config.health_check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health monitoring failed: {e}")
                await self._handle_error(e)

    async def _handle_unhealthy(self) -> None:
        if self.config.max_restart_attempts > 0:
            try:
                await self.restart()
            except LifecycleError as e:
                logger.critical(f"Critical failure in {self.agent_id}: {e}")
                await self._set_state(AgentState.ERROR)
        else:
            logger.error(f"Agent {self.agent_id} remains unhealthy")

    async def _handle_error(self, error: Exception) -> None:
        self._health_status = HealthStatus.DOWN
        await self._set_state(AgentState.ERROR)
        if self.observability:
            await self.observability.capture_exception(error)

        if self.config.max_restart_attempts > 0:
            await self.restart()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        asyncio.run(self.stop())

    async def __aenter__(self) -> Self:
        await self.initialize()
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

class LifecycleDirector:
    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store
        self._agents: Dict[str, LifecycleManager] = {}
        self._dependency_graph: Dict[str, Set[str]] = {}

    async def deploy_agent(
        self,
        agent_cls: Type[AgentBase],
        config: Optional[LifecycleConfig] = None,
    ) -> LifecycleManager:
        manager = LifecycleManager(agent_cls, config, state_store=self.state_store)
        
        async with anyio.create_task_group() as tg:
            tg.start_soon(manager.initialize)
            tg.start_soon(self._register_dependencies, manager)

        self._agents[manager.agent_id] = manager
        return manager

    async def _register_dependencies(self, manager: LifecycleManager) -> None:
        dependencies = manager.agent_cls.dependencies()
        self._dependency_graph[manager.agent_id] = set(dependencies.values())

    async def teardown_all(self) -> None:
        async with anyio.create_task_group() as tg:
            for manager in self._agents.values():
                tg.start_soon(manager.stop)

    async def restart_failed_agents(self) -> None:
        async with anyio.create_task_group() as tg:
            for agent_id, manager in self._agents.items():
                if manager.current_state == AgentState.ERROR:
                    tg.start_soon(manager.restart)

    def get_agent_manager(self, agent_id: str) -> LifecycleManager:
        return self._agents[agent_id]

    def get_dependency_tree(self, agent_id: str) -> Dict[str, Any]:
        tree = {}
        for dep in self._dependency_graph.get(agent_id, []):
            tree[dep] = self.get_dependency_tree(dep)
        return {agent_id: tree}
