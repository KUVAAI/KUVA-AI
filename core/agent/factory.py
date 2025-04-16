"""
KUVA AI Agent Factory Implementation
"""
from __future__ import annotations

import importlib
import inspect
import logging
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    Optional,
    Type,
    TypeVar,
    Union,
)

import pydantic
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from cachetools import TTLCache
from loguru import logger
from kuva.common.exceptions import ConfigurationError, DependencyError
from kuva.core.context import AgentContext
from kuva.core.interfaces import ConfigLoaderProtocol
from kuva.core.config import (
    AgentConfigT,
    ConfigSource,
    EnvironmentLoader,
    VaultLoader,
    ConsulLoader,
)

T = TypeVar("T", bound=Type[Any])
AgentRegistryType = Dict[str, Type[AgentBase]]

class FactoryConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KUVA_FACTORY_",
        extra="ignore",
        case_sensitive=False,
    )
    
    auto_discovery_paths: list[str] = ["kuva.agents"]
    cache_enabled: bool = True
    cache_max_size: int = 256
    cache_ttl: int = 300
    validate_dependencies: bool = True
    strict_mode: bool = True
    config_loader_priority: list[str] = ["vault", "consul", "env"]
    
    @classmethod
    def from_context(cls, context: AgentContext) -> FactoryConfig:
        return cls(**context.factory_settings)

class DependencyGraph:
    def __init__(self):
        self._graph = defaultdict(set)
        self._reverse_graph = defaultdict(set)
        
    def add_dependency(self, dependent: str, dependency: str) -> None:
        self._graph[dependent].add(dependency)
        self._reverse_graph[dependency].add(dependent)
        
    def get_dependencies(self, agent_type: str) -> set[str]:
        return self._graph.get(agent_type, set())
    
    def get_dependents(self, agent_type: str) -> set[str]:
        return self._reverse_graph.get(agent_type, set())

class AgentFactoryMetrics:
    def __init__(self):
        self.agents_created = Counter("kuva_factory_agents_created", "Total agents created")
        self.cache_hits = Counter("kuva_factory_cache_hits", "Configuration cache hits")
        self.cache_misses = Counter("kuva_factory_cache_misses", "Configuration cache misses")
        self.dependency_errors = Counter("kuva_factory_dependency_errors", "Dependency resolution failures")

class AgentFactory:
    _instance = None
    _registry: AgentRegistryType = {}
    _config_loaders: Dict[str, ConfigLoaderProtocol] = {}
    
    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._dependency_graph = DependencyGraph()
            cls._metrics = AgentFactoryMetrics()
            cls._cache = TTLCache(maxsize=1024, ttl=300)
            cls._config = FactoryConfig()
            cls._initialized = False
        return cls._instance
    
    @classmethod
    def initialize(cls, context: AgentContext) -> None:
        instance = cls()
        if not instance._initialized:
            instance._config = FactoryConfig.from_context(context)
            instance._setup_config_loaders(context)
            instance._discover_agents()
            instance._initialized = True
            
    def _setup_config_loaders(self, context: AgentContext) -> None:
        loaders = {
            "env": EnvironmentLoader(),
            "vault": VaultLoader(context.vault_client),
            "consul": ConsulLoader(context.consul_client),
        }
        for loader_name in self._config.config_loader_priority:
            if loader_name in loaders:
                self._config_loaders[loader_name] = loaders[loader_name]
                
    def _discover_agents(self) -> None:
        for path in self._config.auto_discovery_paths:
            try:
                module = importlib.import_module(path)
                self._register_module_agents(module)
            except ImportError as e:
                if self._config.strict_mode:
                    raise ConfigurationError(f"Auto-discovery failed for {path}: {e}")
                logger.warning(f"Auto-discovery skipped invalid path: {path}")
                
    def _register_module_agents(self, module: Any) -> None:
        for name in dir(module):
            obj = getattr(module, name)
            if inspect.isclass(obj) and issubclass(obj, AgentBase) and obj is not AgentBase:
                self.register(obj)
                
    @classmethod
    def register(cls, agent_cls: Type[AgentBase]) -> Type[AgentBase]:
        if not inspect.isclass(agent_cls) or not issubclass(agent_cls, AgentBase):
            raise ConfigurationError(f"Invalid agent class registration: {agent_cls}")
            
        cls._registry[agent_cls.__name__] = agent_cls
        return agent_cls
    
    def _resolve_config(
        self,
        agent_type: str,
        config_source: Optional[ConfigSource] = None
    ) -> AgentConfigT:
        config_data = {}
        
        if config_source:
            for loader_name in self._config.config_loader_priority:
                if loader_name in self._config_loaders:
                    loader = self._config_loaders[loader_name]
                    try:
                        config_data.update(loader.load(config_source))
                    except Exception as e:
                        logger.error(f"Config loader {loader_name} failed: {e}")
                        if self._config.strict_mode:
                            raise
        
        agent_cls = self._registry.get(agent_type)
        if not agent_cls:
            raise ConfigurationError(f"Unregistered agent type: {agent_type}")
            
        try:
            return agent_cls.ConfigModel(**config_data)
        except ValidationError as e:
            raise ConfigurationError(f"Invalid config for {agent_type}: {e}") from e
            
    def _resolve_dependencies(
        self,
        agent_type: str,
        context: AgentContext,
        config: AgentConfigT
    ) -> Dict[str, AgentBase]:
        dependencies = {}
        agent_cls = self._registry[agent_type]
        
        for dep_name, dep_type in agent_cls.dependencies().items():
            try:
                if self._config.cache_enabled and dep_name in self._cache:
                    dependencies[dep_name] = self._cache[dep_name]
                    self._metrics.cache_hits.inc()
                    continue
                    
                self._metrics.cache_misses.inc()
                dep_config = self._resolve_config(dep_type, config.dependencies[dep_name])
                dep_instance = self.create(
                    agent_type=dep_type,
                    context=context,
                    config=dep_config,
                    dependencies={}  # Avoid circular dependency
                )
                dependencies[dep_name] = dep_instance
                if self._config.cache_enabled:
                    self._cache[dep_name] = dep_instance
            except Exception as e:
                self._metrics.dependency_errors.inc()
                raise DependencyError(f"Failed to resolve {dep_name}: {e}") from e
                
        return dependencies
    
    @lru_cache(maxsize=1024)
    def create(
        self,
        agent_type: str,
        context: AgentContext,
        config: Optional[AgentConfigT] = None,
        config_source: Optional[ConfigSource] = None,
        dependencies: Optional[Dict[str, AgentBase]] = None
    ) -> AgentBase:
        try:
            final_config = config or self._resolve_config(agent_type, config_source)
            resolved_deps = dependencies or self._resolve_dependencies(
                agent_type, context, final_config
            )
            
            agent_cls = self._registry[agent_type]
            instance = agent_cls(config=final_config, context=context, dependencies=resolved_deps)
            
            self._dependency_graph.add_dependency(agent_type, *resolved_deps.keys())
            self._metrics.agents_created.inc()
            
            return instance
        except Exception as e:
            logger.critical(f"Agent creation failed for {agent_type}: {e}")
            if isinstance(e, (ConfigurationError, DependencyError)):
                raise
            raise RuntimeError(f"Factory system error: {e}") from e
    
    async def create_async(
        self,
        agent_type: str,
        context: AgentContext,
        config: Optional[AgentConfigT] = None,
        config_source: Optional[ConfigSource] = None,
        dependencies: Optional[Dict[str, AgentBase]] = None
    ) -> AgentBase:
        # Implementation for async agent initialization
        pass
    
    def invalidate_cache(self, agent_name: Optional[str] = None) -> None:
        if agent_name:
            self._cache.pop(agent_name, None)
        else:
            self._cache.clear()
            
    def get_agent_class(self, agent_type: str) -> Type[AgentBase]:
        if agent_type not in self._registry:
            raise ConfigurationError(f"Unregistered agent type: {agent_type}")
        return self._registry[agent_type]
    
    def list_agents(self) -> Dict[str, Type[AgentBase]]:
        return dict(self._registry)
    
    def get_dependency_graph(self) -> DependencyGraph:
        return self._dependency_graph

class FactoryMiddleware:
    @staticmethod
    def config_validator(config_class: Type[BaseModel]) -> Callable:
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(factory: AgentFactory, *args, **kwargs):
                try:
                    config = config_class(**kwargs.pop("raw_config"))
                    return func(factory, *args, config=config, **kwargs)
                except ValidationError as e:
                    raise ConfigurationError(f"Config validation failed: {e}") from e
            return wrapper
        return decorator
    
    @staticmethod
    def lifecycle_hook(hook_type: str) -> Callable:
        valid_hooks = {"pre_init", "post_init", "pre_start", "post_start"}
        if hook_type not in valid_hooks:
            raise ValueError(f"Invalid hook type: {hook_type}")
            
        def decorator(func: Callable) -> Callable:
            setattr(AgentFactory, f"_{hook_type}_hook", func)
            return func
        return decorator

# Protocol definitions
class AgentProtocol(Protocol):
    @classmethod
    def dependencies(cls) -> Dict[str, str]:
        ...
    
    class ConfigModel(BaseModel):
        ...
