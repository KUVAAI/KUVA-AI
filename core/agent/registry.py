"""
KUVA AI Agent Registry Implementation
"""
from __future__ import annotations

import importlib
import inspect
import threading
from collections import defaultdict
from functools import wraps
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterator,
    List,
    Optional,
    Set,
    Type,
    TypeVar,
    Union,
)

import pydantic
from loguru import logger
from pydantic import BaseModel, ValidationError
from typing_extensions import Protocol

from kuva.common.exceptions import (
    RegistrationError,
    ConfigurationError,
    DependencyConflictError,
)
from kuva.core.agent.base import AgentBase
from kuva.core.interfaces import PluginSpec

T = TypeVar("T", bound=Type[AgentBase])
RegistryEntry = Dict[str, Union[Type[AgentBase], PluginSpec]]

class AgentRegistryMeta(type):
    _instances: ClassVar[Dict[Any, Any]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    instance = super().__call__(*args, **kwargs)
                    cls._instances[cls] = instance
        return cls._instances[cls]

class AgentRegistry(metaclass=AgentRegistryMeta):
    def __init__(self) -> None:
        self._registry: Dict[str, RegistryEntry] = {}
        self._reverse_dependencies: Dict[str, Set[str]] = defaultdict(set)
        self._lock = threading.RLock()
        self._initialized = False
        self._plugin_paths: Set[Path] = set()

    def initialize(self, plugin_paths: List[Union[str, Path]] = None) -> None:
        with self._lock:
            if self._initialized:
                return
                
            self._plugin_paths = {Path(p) for p in (plugin_paths or [])}
            self._load_builtin_agents()
            self._discover_plugins()
            self._validate_registry()
            self._initialized = True

    def _load_builtin_agents(self) -> None:
        """Auto-register agents from core modules"""
        try:
            from kuva.core.agent import builtin  # noqa: F401
            self._register_module(builtin)
        except ImportError:
            logger.warning("Built-in agents module not found")

    def _discover_plugins(self) -> None:
        for path in self._plugin_paths:
            try:
                module = self._load_plugin_module(path)
                self._register_module(module)
            except Exception as e:
                logger.error(f"Failed to load plugin from {path}: {e}")

    def _load_plugin_module(self, path: Path) -> Any:
        spec = importlib.util.spec_from_file_location(f"kuva.plugin.{path.stem}", path)
        if not spec or not spec.loader:
            raise RegistrationError(f"Invalid plugin path: {path}")
            
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _register_module(self, module: Any) -> None:
        for name in dir(module):
            obj = getattr(module, name)
            if inspect.isclass(obj) and issubclass(obj, AgentBase) and obj is not AgentBase:
                self.register(obj)

    def register(
        self,
        agent_cls: T,
        *,
        plugin_meta: Optional[PluginSpec] = None,
        override: bool = False
    ) -> T:
        with self._lock:
            return self._unsafe_register(agent_cls, plugin_meta, override)

    def _unsafe_register(
        self,
        agent_cls: T,
        plugin_meta: Optional[PluginSpec],
        override: bool
    ) -> T:
        if not inspect.isclass(agent_cls):
            raise RegistrationError(f"Non-class object cannot be registered: {agent_cls}")

        if not issubclass(agent_cls, AgentBase):
            raise RegistrationError(f"Invalid agent class: {agent_cls.__name__}")

        agent_type = agent_cls.__name__
        existing = self._registry.get(agent_type)

        if existing and not override:
            raise RegistrationError(f"Agent {agent_type} already registered")

        try:
            self._validate_agent_config_model(agent_cls)
        except ValidationError as e:
            raise RegistrationError(f"Invalid config model for {agent_type}: {e}") from e

        entry: RegistryEntry = {
            "class": agent_cls,
            "plugin": plugin_meta or PluginSpec(),
        }

        self._registry[agent_type] = entry
        self._update_dependency_graph(agent_cls)
        logger.info(f"Registered agent: {agent_type}")
        return agent_cls

    def _validate_agent_config_model(self, agent_cls: Type[AgentBase]) -> None:
        if not hasattr(agent_cls, "ConfigModel"):
            raise RegistrationError(f"Missing ConfigModel in {agent_cls.__name__}")
            
        if not issubclass(agent_cls.ConfigModel, BaseModel):
            raise RegistrationError(f"Invalid ConfigModel in {agent_cls.__name__}")

    def _update_dependency_graph(self, agent_cls: Type[AgentBase]) -> None:
        agent_type = agent_cls.__name__
        dependencies = agent_cls.dependencies()
        
        for dep_name, dep_type in dependencies.items():
            if dep_type not in self._registry:
                logger.warning(f"Unresolved dependency {dep_type} for {agent_type}")
                
            self._reverse_dependencies[dep_type].add(agent_type)

    def unregister(self, agent_type: str) -> None:
        with self._lock:
            if agent_type not in self._registry:
                raise RegistrationError(f"Agent {agent_type} not registered")
                
            if agent_type in self._reverse_dependencies:
                dependents = self._reverse_dependencies[agent_type]
                if dependents:
                    raise DependencyConflictError(
                        f"Cannot unregister {agent_type} - required by {dependents}"
                    )
                    
            del self._registry[agent_type]
            logger.info(f"Unregistered agent: {agent_type}")

    def get(self, agent_type: str) -> Type[AgentBase]:
        entry = self._registry.get(agent_type)
        if not entry:
            raise RegistrationError(f"Agent {agent_type} not registered")
            
        return entry["class"]

    def get_plugin_spec(self, agent_type: str) -> PluginSpec:
        entry = self._registry.get(agent_type)
        if not entry:
            raise RegistrationError(f"Agent {agent_type} not registered")
            
        return entry["plugin"]

    def get_all_agents(self) -> Dict[str, Type[AgentBase]]:
        return {k: v["class"] for k, v in self._registry.items()}

    def agent_exists(self, agent_type: str) -> bool:
        return agent_type in self._registry

    def get_dependents(self, agent_type: str) -> Iterator[str]:
        return iter(self._reverse_dependencies.get(agent_type, set()))

    def _validate_registry(self) -> None:
        circular_deps = self._detect_circular_dependencies()
        if circular_deps:
            logger.critical(f"Circular dependencies detected: {circular_deps}")
            raise DependencyConflictError(f"Circular dependencies: {circular_deps}")

    def _detect_circular_dependencies(self) -> List[List[str]]:
        visited = set()
        stack = []
        cycles = []

        def dfs(node: str) -> None:
            if node in visited:
                return
                
            visited.add(node)
            stack.append(node)
            
            for dependent in self._reverse_dependencies.get(node, []):
                if dependent in stack:
                    cycle = stack[stack.index(dependent):] + [dependent]
                    cycles.append(cycle)
                elif dependent not in visited:
                    dfs(dependent)
                    
            stack.pop()

        for agent_type in self._registry:
            dfs(agent_type)
            
        return cycles

    def __contains__(self, agent_type: str) -> bool:
        return agent_type in self._registry

    def __getitem__(self, agent_type: str) -> Type[AgentBase]:
        return self.get(agent_type)

    def __iter__(self) -> Iterator[str]:
        return iter(self._registry.keys())

def register_agent(
    agent_cls: Optional[T] = None,
    *,
    name: Optional[str] = None,
    version: str = "0.1.0",
    description: Optional[str] = None,
    override: bool = False
) -> Union[T, Callable[[T], T]]:
    def decorator(cls: T) -> T:
        nonlocal name
        registry = AgentRegistry()
        plugin_meta = PluginSpec(
            name=name or cls.__name__,
            version=version,
            description=description or cls.__doc__,
        )
        registry.register(cls, plugin_meta=plugin_meta, override=override)
        return cls

    if agent_cls is None:
        return decorator
        
    return decorator(agent_cls)

class RegistryMiddleware:
    @staticmethod
    def dependency_validator(*deps: str) -> Callable:
        def wrapper(func: Callable) -> Callable:
            @wraps(func)
            def wrapped(registry: AgentRegistry, *args: Any, **kwargs: Any) -> Any:
                missing = [d for d in deps if d not in registry]
                if missing:
                    raise DependencyConflictError(
                        f"Missing required dependencies: {missing}"
                    )
                return func(registry, *args, **kwargs)
            return wrapped
        return wrapper
