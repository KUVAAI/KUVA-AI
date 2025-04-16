"""
KUVA AI YAML to DAG Parser
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import networkx as nx
import yaml
from pydantic import BaseModel, Field, ValidationError, validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, Node, ScalarNode

from kuva.common.exceptions import InvalidPipelineError
from kuva.common.metrics import ParserMetrics
from kuva.common.utils import topological_generations

logger = logging.getLogger(__name__)

# --- Schema Definitions ------------------------------------------------------

class NodeConfig(BaseModel):
    id: str = Field(..., min_length=3, max_length=64, regex=r"^[a-z0-9\-]+$")
    type: str = Field(..., regex=r"^(task|decision|parallel|service|sensor|control)$")
    executor: Optional[str] = Field(
        default="default",
        description="Executor class or runtime environment",
    )
    depends_on: Union[List[str], str] = Field(default_factory=list)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    timeout: int = Field(default=300, gt=0)
    retries: int = Field(default=2, ge=0, le=5)
    resources: Dict[str, Union[int, str]] = Field(
        default_factory=lambda: {"cpu": "1", "memory": "1Gi"}
    )
    hooks: Dict[str, List[str]] = Field(
        default_factory=lambda: {
            "pre_execute": [],
            "post_success": [],
            "post_failure": [],
        }
    )
    tags: Set[str] = Field(default_factory=set)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @validator("depends_on", pre=True)
    def normalize_dependencies(cls, v):
        if isinstance(v, str):
            return [v.strip() for v in v.split(",") if v.strip()]
        return v

    @validator("resources")
    def validate_resources(cls, v):
        if "cpu" in v and not re.match(r"^\d+(\.\d+)?$", str(v["cpu"])):
            raise ValueError("Invalid CPU format. Use cores (e.g., 1.5) or millicores (e.g., 1500m)")
        return v

class PipelineConfig(BaseModel):
    version: str = Field(..., regex=r"^\d+\.\d+$")
    variables: Dict[str, Any] = Field(default_factory=dict)
    nodes: Dict[str, NodeConfig] = Field(..., min_items=1)
    imports: List[str] = Field(default_factory=list)
    dag_options: Dict[str, Any] = Field(
        default_factory=lambda: {
            "max_concurrency": 10,
            "execution_order": "depth_first",
        }
    )

# --- Core Parser Implementation ----------------------------------------------

class DAGParser:
    def __init__(self, *, validate_schema: bool = True, metrics: ParserMetrics = None):
        self._cache = {}
        self.metrics = metrics or ParserMetrics()
        self.validate_schema = validate_schema
        self._custom_constructors = {}
        self._template_vars = {}
        
        # Security: Disable unsafe YAML constructs
        self._safe_loader = yaml.SafeLoader
        self._safe_loader.add_constructor(
            "tag:yaml.org,2002:python/object/apply",
            self._block_python_objects
        )

    def parse(self, source: Union[str, Path]) -> nx.DiGraph:
        """Parse YAML pipeline into validated DAG"""
        try:
            self.metrics.inc_parse_attempts()
            content = self._load_content(source)
            raw_data = self._render_templates(content)
            config = self._validate_config(raw_data)
            dag = self._build_dag(config)
            self._analyze_dag(dag)
            self.metrics.inc_parse_success()
            return dag
        except Exception as e:
            self.metrics.inc_parse_failures()
            logger.error(f"Pipeline parse failed: {str(e)}", exc_info=True)
            raise InvalidPipelineError(f"YAML parsing error: {str(e)}") from e

    def _load_content(self, source: Union[str, Path]) -> str:
        """Load YAML content with caching and circular import detection"""
        path = Path(source).resolve()
        if path in self._cache:
            return self._cache[path]
            
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            
        parsed = yaml.compose(content, Loader=self._safe_loader)
        self._detect_circular_imports(parsed, path)
        self._cache[path] = content
        return content

    def _render_templates(self, content: str) -> Dict:
        """Handle Jinja-style template variables"""
        rendered = re.sub(
            r"\$\{\{\s*(\w+)\s*\}\}", 
            lambda m: str(self._template_vars.get(m.group(1), "")),
            content
        )
        return yaml.safe_load(rendered)

    def _validate_config(self, raw_data: Dict) -> PipelineConfig:
        """Validate schema and business rules"""
        if self.validate_schema:
            config = PipelineConfig(**raw_data)
            self._validate_dag_rules(config)
            return config
        return raw_data

    def _validate_dag_rules(self, config: PipelineConfig) -> None:
        """Validate DAG-specific business logic"""
        node_ids = set(config.nodes.keys())
        
        for node_id, node_cfg in config.nodes.items():
            # Validate dependencies exist
            invalid_deps = set(node_cfg.depends_on) - node_ids
            if invalid_deps:
                raise InvalidPipelineError(
                    f"Node '{node_id}' has invalid dependencies: {invalid_deps}"
                )
                
            # Validate executor compatibility
            if node_cfg.type == "service" and node_cfg.executor == "default":
                raise InvalidPipelineError(
                    f"Service node '{node_id}' requires explicit executor"
                )

    def _build_dag(self, config: PipelineConfig) -> nx.DiGraph:
        """Construct validated DAG with execution metadata"""
        dag = nx.DiGraph()
        
        # Add nodes with full configuration
        for node_id, node_cfg in config.nodes.items():
            dag.add_node(
                node_id,
                **node_cfg.dict(exclude={"depends_on"}),
                execution_group=self._calculate_execution_group(node_id, config)
            )
            
        # Add edges with dependency metadata
        for node_id, node_cfg in config.nodes.items():
            for dep in node_cfg.depends_on:
                dag.add_edge(dep, node_id, dependency_type="explicit")
                
        # Add implicit dependencies from hook ordering
        self._process_hook_dependencies(dag)
        
        return dag

    def _calculate_execution_group(self, node_id: str, config: PipelineConfig) -> int:
        """Determine execution wave for parallel scheduling"""
        generations = list(topological_generations(dag))
        return next(i for i, gen in enumerate(generations) if node_id in gen)

    def _process_hook_dependencies(self, dag: nx.DiGraph) -> None:
        """Create implicit dependencies between hook operations"""
        for node_id in dag.nodes:
            hooks = dag.nodes[node_id]["hooks"]
            
            # Create sequence dependencies between hooks
            prev_hook = None
            for hook_type in ["pre_execute", "post_success", "post_failure"]:
                for hook in hooks.get(hook_type, []):
                    hook_id = f"{node_id}.{hook_type}.{hook}"
                    dag.add_node(hook_id, type="hook", parent_node=node_id)
                    dag.add_edge(node_id, hook_id)
                    
                    if prev_hook:
                        dag.add_edge(prev_hook, hook_id)
                    prev_hook = hook_id

    def _analyze_dag(self, dag: nx.DiGraph) -> None:
        """Perform DAG structure analysis and optimizations"""
        if not nx.is_directed_acyclic_graph(dag):
            raise InvalidPipelineError("Pipeline contains cycles")
            
        self._detect_unused_nodes(dag)
        self._optimize_edge_ordering(dag)
        
        if len(list(nx.weakly_connected_components(dag))) > 1:
            logger.warning("Pipeline contains disconnected components")

    def _detect_unused_nodes(self, dag: nx.DiGraph) -> None:
        """Identify nodes not connected to the main workflow"""
        roots = {n for n, d in dag.in_degree() if d == 0}
        reachable = set()
        
        for root in roots:
            reachable.update(nx.descendants(dag, root))
            
        unreachable = set(dag.nodes) - reachable - roots
        if unreachable:
            raise InvalidPipelineError(f"Unreachable nodes detected: {unreachable}")

    def _optimize_edge_ordering(self, dag: nx.DiGraph) -> None:
        """Optimize edge order for efficient execution planning"""
        for node in dag.nodes:
            edges = sorted(
                dag.in_edges(node),
                key=lambda e: dag.nodes[e[0]].get("execution_group", 0),
                reverse=True
            )
            dag.in_edges[node] = edges

# --- Security & Customization -------------------------------------------------

    def _block_python_objects(self, loader: yaml.Loader, node: Node) -> None:
        """Block dangerous Python object deserialization"""
        raise ConstructorError(
            problem="Unsafe YAML construct detected",
            problem_mark=node.start_mark,
            context="Python object deserialization is disabled"
        )

    def register_custom_constructor(
        self,
        tag: str,
        constructor: callable
    ) -> None:
        """Register custom YAML tag handlers"""
        self._custom_constructors[tag] = constructor
        self._safe_loader.add_constructor(tag, constructor)

    def add_template_vars(self, **kwargs) -> None:
        """Add template variables for dynamic YAML rendering"""
        self._template_vars.update(kwargs)

# --- Advanced Features --------------------------------------------------------

class ParallelTaskOptimizer:
    """Optimize parallel task execution groups"""
    
    @staticmethod
    def optimize(dag: nx.DiGraph) -> nx.DiGraph:
        """Identify and group parallelizable tasks"""
        optimized = dag.copy()
        generations = list(topological_generations(dag))
        
        for gen in generations:
            independent_tasks = [
                n for n in gen
                if optimized.nodes[n]["type"] == "task"
                and len(optimized.out_edges(n)) == 0
            ]
            
            if len(independent_tasks) > 1:
                parent_group = f"parallel_group_{hash(tuple(independent_tasks))}"
                optimized.add_node(parent_group, type="parallel", tasks=independent_tasks)
                
                for task in independent_tasks:
                    for pred in optimized.predecessors(task):
                        optimized.add_edge(pred, parent_group)
                    optimized.remove_node(task)
                    
        return optimized

class DependencyVisualizer:
    """Generate visualization artifacts from DAG"""
    
    @staticmethod
    def to_mermaid(dag: nx.DiGraph) -> str:
        """Generate MermaidJS compatible syntax"""
        lines = ["graph TD"]
        for node in dag.nodes:
            lines.append(f"    {node}[{dag.nodes[node]['type']}]")
        for src, dst in dag.edges:
            lines.append(f"    {src} --> {dst}")
        return "\n".join(lines)

# --- Unit Tests ---------------------------------------------------------------

class TestDAGParser:
    """Unit test suite for DAG parser"""
    
    def test_valid_pipeline(self):
        yaml_content = """
        version: 1.0
        nodes:
          data_load:
            type: task
            executor: spark
          data_clean:
            type: task
            depends_on: data_load
        """
        parser = DAGParser()
        dag = parser.parse(yaml_content)
        assert len(dag.nodes) == 2
        assert ("data_load", "data_clean") in dag.edges

    def test_invalid_dependency(self):
        yaml_content = """
        version: 1.0
        nodes:
          task_a:
            type: task
            depends_on: missing_task
        """
        parser = DAGParser()
        with pytest.raises(InvalidPipelineError):
            parser.parse(yaml_content)

# --- Enterprise Integration ---------------------------------------------------

class GitOpsSync:
    """Synchronize pipeline definitions with Git repositories"""
    
    def __init__(self, parser: DAGParser):
        self.parser = parser
        self.repo_observer = FileSystemObserver()
        
    def watch_directory(self, path: Path) -> None:
        """Watch for YAML file changes using inotify"""
        self.repo_observer.schedule(
            self._gitops_handler, 
            path=path,
            recursive=True
        )
        self.repo_observer.start()

    def _gitops_handler(self, event):
        if event.is_directory or not event.src_path.endswith(".yaml"):
            return
            
        try:
            dag = self.parser.parse(event.src_path)
            self._deploy_to_runtime(dag)
        except InvalidPipelineError as e:
            logger.error(f"GitOps sync failed: {str(e)}")

    def _deploy_to_runtime(self, dag: nx.DiGraph) -> None:
        """Deploy validated DAG to execution runtime"""
        # Integration with Kubernetes/airflow/custom scheduler
        pass
