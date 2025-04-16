"""
KUVA AI Cyclic Dependency Validation System
"""
from __future__ import annotations

import ast
import inspect
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Set, Tuple, Union
import networkx as nx
from importlib import import_module
from types import ModuleType

logger = logging.getLogger(__name__)

class CycleValidationError(Exception):
    """Base exception for dependency cycle violations"""
    def __init__(self, cycle_path: List[str], context: str = ""):
        self.cycle_path = cycle_path
        self.context = context
        super().__init__(f"Cyclic dependency detected: {' → '.join(cycle_path)} | Context: {context}")

class CycleValidator:
    """Base class for cyclic dependency validation"""
    
    def __init__(self):
        self._cache = {}
        self._module_graph = nx.DiGraph()
        self._current_analysis = None

    def validate(self, target: Union[object, str, ModuleType]) -> bool:
        """Main validation entry point"""
        if inspect.ismodule(target):
            return self._validate_module(target)
        elif inspect.isclass(target):
            return self._validate_class(target)
        elif inspect.isfunction(target):
            return self._validate_function(target)
        elif isinstance(target, str):
            if target.endswith('.py'):
                return self._validate_file(Path(target))
            else:
                return self._validate_dag(target)
        raise ValueError(f"Unsupported validation target: {type(target)}")

    def _validate_module(self, module: ModuleType) -> bool:
        """Validate Python module dependencies"""
        module_id = module.__name__
        if module_id in self._cache:
            return self._cache[module_id]

        self._build_module_graph(module)
        cycles = list(nx.simple_cycles(self._module_graph))
        
        if cycles:
            logger.error(f"Module dependency cycles: {cycles}")
            raise CycleValidationError(
                self._simplify_cycle(cycles[0]), 
                f"Module: {module_id}"
            )
        
        self._cache[module_id] = True
        return True

    def _build_module_graph(self, module: ModuleType) -> None:
        """Construct module dependency graph through AST analysis"""
        try:
            source = inspect.getsource(module)
            tree = ast.parse(source)
            
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        import_path = alias.name.split('.')[0]
                        self._module_graph.add_edge(module.__name__, import_path)
                elif isinstance(node, ast.ImportFrom):
                    module_name = node.module.split('.')[0] if node.module else ''
                    if module_name:
                        self._module_graph.add_edge(module.__name__, module_name)
        except Exception as e:
            logger.error(f"Module analysis failed: {str(e)}")
            raise

    def _validate_class(self, cls: type) -> bool:
        """Validate class inheritance/method dependencies"""
        class_graph = nx.DiGraph()
        self._build_class_hierarchy(cls, class_graph)
        cycles = list(nx.simple_cycles(class_graph))
        
        if cycles:
            logger.error(f"Class dependency cycles: {cycles}")
            raise CycleValidationError(
                self._simplify_cycle(cycles[0]),
                f"Class: {cls.__name__}"
            )
        return True

    def _build_class_hierarchy(self, cls: type, graph: nx.DiGraph) -> None:
        """Analyze class inheritance and method calls"""
        graph.add_node(cls.__name__)
        
        # Base classes
        for base in cls.__bases__:
            base_name = base.__name__
            graph.add_edge(cls.__name__, base_name)
            self._build_class_hierarchy(base, graph)
            
        # Method dependencies
        for name, member in inspect.getmembers(cls):
            if inspect.isfunction(member):
                self._analyze_method_calls(member, cls.__name__, graph)

    def _analyze_method_calls(self, func: callable, parent: str, graph: nx.DiGraph) -> None:
        """Track method-to-method dependencies within class"""
        try:
            source = inspect.getsource(func)
            tree = ast.parse(source)
            
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        callee = node.func.attr
                        graph.add_edge(parent + '.' + func.__name__, parent + '.' + callee)
        except Exception as e:
            logger.warning(f"Method analysis failed: {str(e)}")

    def _validate_function(self, func: callable) -> bool:
        """Validate function call dependencies"""
        call_graph = nx.DiGraph()
        self._build_call_graph(func, call_graph)
        cycles = list(nx.simple_cycles(call_graph))
        
        if cycles:
            logger.error(f"Function call cycles: {cycles}")
            raise CycleValidationError(
                self._simplify_cycle(cycles[0]),
                f"Function: {func.__name__}"
            )
        return True

    def _build_call_graph(self, func: callable, graph: nx.DiGraph) -> None:
        """Construct function call graph through AST analysis"""
        func_id = self._function_identifier(func)
        if func_id in graph:
            return

        graph.add_node(func_id)
        
        try:
            source = inspect.getsource(func)
            tree = ast.parse(source)
            
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        callee_id = node.func.id
                        graph.add_edge(func_id, callee_id)
                        # Recursive analysis
                        if callee_id in globals():
                            self._build_call_graph(globals()[callee_id], graph)
        except Exception as e:
            logger.warning(f"Function analysis failed: {str(e)}")

    def _validate_file(self, file_path: Path) -> bool:
        """Validate dependencies within a file/module"""
        try:
            spec = import_module(str(file_path).replace('/', '.').rstrip('.py'))
            module = import_module(spec.__name__)
            return self._validate_module(module)
        except Exception as e:
            logger.error(f"File validation failed: {str(e)}")
            raise

    def _validate_dag(self, dag_identifier: str) -> bool:
        """Validate workflow/DAG dependencies"""
        if dag_identifier in self._current_analysis:
            raise CycleValidationError(
                ["..."],
                f"DAG analysis recursion detected: {dag_identifier}"
            )
            
        self._current_analysis = dag_identifier
        dag = self._load_dag(dag_identifier)
        
        try:
            if not nx.is_directed_acyclic_graph(dag):
                cycles = list(nx.simple_cycles(dag))
                logger.error(f"DAG dependency cycles: {cycles}")
                raise CycleValidationError(
                    self._simplify_cycle(cycles[0]),
                    f"DAG: {dag_identifier}"
                )
            return True
        finally:
            self._current_analysis = None

    def _load_dag(self, identifier: str) -> nx.DiGraph:
        """Load DAG from persistent storage (implementation varies)"""
        # Implementation would connect to Airflow/Kubeflow/etc.
        return nx.DiGraph()

    def _simplify_cycle(self, cycle: List[str]) -> List[str]:
        """Normalize cycle path for reporting"""
        min_index = cycle.index(min(cycle))
        return cycle[min_index:] + cycle[:min_index]

    def _function_identifier(self, func: callable) -> str:
        """Generate unique function identifier"""
        return f"{func.__module__}.{func.__qualname__}"

class MultiProjectValidator(CycleValidator):
    """Cross-project dependency validation"""
    
    def __init__(self):
        super().__init__()
        self._project_graph = nx.DiGraph()
        self._component_map = defaultdict(set)

    def add_project(self, project_id: str, components: Set[str]) -> None:
        """Register project components"""
        self._project_graph.add_node(project_id)
        self._component_map[project_id] = components

    def validate_project_dependencies(self) -> bool:
        """Check cross-project dependency cycles"""
        cycles = list(nx.simple_cycles(self._project_graph))
        if cycles:
            logger.error(f"Project dependency cycles: {cycles}")
            raise CycleValidationError(
                self._simplify_cycle(cycles[0]),
                "Cross-project dependencies"
            )
        return True

class PerformanceOptimizedValidator(CycleValidator):
    """Cycle validation with caching and parallel processing"""
    
    def __init__(self):
        super().__init__()
        self._analysis_cache = {}
        self._lock = threading.Lock()

    def validate(self, target: Union[object, str, ModuleType]) -> bool:
        """Thread-safe cached validation"""
        target_id = self._target_identifier(target)
        
        with self._lock:
            if target_id in self._analysis_cache:
                return self._analysis_cache[target_id]
                
            result = super().validate(target)
            self._analysis_cache[target_id] = result
            return result

    def _target_identifier(self, target: Union[object, str, ModuleType]) -> str:
        """Generate unique cache key for validation targets"""
        if isinstance(target, str):
            return target
        elif inspect.ismodule(target):
            return target.__name__
        elif inspect.isclass(target) or inspect.isfunction(target):
            return f"{target.__module__}.{target.__qualname__}"
        return str(id(target))

class TarjanCycleDetector:
    """Tarjan's algorithm implementation for efficient cycle detection"""
    
    @staticmethod
    def find_strongly_connected_components(graph: nx.DiGraph) -> List[List[str]]:
        """Find all strongly connected components (potential cycles)"""
        index = 0
        indices = {}
        lowlink = {}
        stack = []
        on_stack = set()
        sccs = []
        
        def strongconnect(node):
            nonlocal index
            indices[node] = index
            lowlink[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            
            for successor in graph.successors(node):
                if successor not in indices:
                    strongconnect(successor)
                    lowlink[node] = min(lowlink[node], lowlink[successor])
                elif successor in on_stack:
                    lowlink[node] = min(lowlink[node], indices[successor])
            
            if lowlink[node] == indices[node]:
                scc = []
                while True:
                    successor = stack.pop()
                    on_stack.remove(successor)
                    scc.append(successor)
                    if successor == node:
                        break
                sccs.append(scc)
        
        for node in graph.nodes:
            if node not in indices:
                strongconnect(node)
                
        return [scc for scc in sccs if len(scc) > 1]

class CycleValidationFramework:
    """Enterprise validation orchestration framework"""
    
    def __init__(self):
        self.validators = {
            'module': CycleValidator(),
            'class': CycleValidator(),
            'function': CycleValidator(),
            'dag': CycleValidator(),
            'project': MultiProjectValidator(),
            'high_perf': PerformanceOptimizedValidator()
        }
        self._report = defaultdict(list)

    def full_validation_pipeline(self, targets: List[Union[str, object]]) -> bool:
        """Execute complete validation workflow"""
        results = []
        
        for target in targets:
            validator_type = self._select_validator(target)
            try:
                result = self.validators[validator_type].validate(target)
                self._report['success'].append(str(target))
                results.append(result)
            except CycleValidationError as e:
                self._report['failures'].append({
                    'target': str(target),
                    'cycle': e.cycle_path,
                    'context': e.context
                })
                results.append(False)
                
        return all(results)

    def _select_validator(self, target: Union[str, object]) -> str:
        """Choose appropriate validator based on target type"""
        if isinstance(target, str) and any(target.endswith(ext) for ext in ['.py', '.yaml']):
            return 'dag' if 'dag' in target.lower() else 'module'
        elif inspect.ismodule(target):
            return 'module'
        elif inspect.isclass(target):
            return 'class'
        elif inspect.isfunction(target):
            return 'function'
        return 'high_perf'

    def generate_compliance_report(self) -> Dict:
        """Generate audit-ready validation report"""
        return {
            'validated_objects': len(self._report['success']) + len(self._report['failures']),
            'success_count': len(self._report['success']),
            'failure_count': len(self._report['failures']),
            'critical_failures': [f for f in self._report['failures'] if 'prod' in f['context']],
            'cycle_details': self._report['failures']
        }

# Unit Tests
import pytest

class TestCycleValidation:
    """Comprehensive test suite for cycle detection"""
    
    def test_simple_cycle_detection(self):
        validator = CycleValidator()
        graph = nx.DiGraph()
        graph.add_edges_from([('A', 'B'), ('B', 'C'), ('C', 'A')])
        
        with pytest.raises(CycleValidationError) as excinfo:
            validator._validate_dag(graph)
            
        assert ['A', 'B', 'C'] in excinfo.value.cycle_path

    def test_module_dependency_cycle(self):
        validator = CycleValidator()
        
        class MockModule:
            __name__ = "mock_module"
            def __init__(self):
                self.source = "import sibling_module"
                
        with pytest.raises(CycleValidationError):
            validator._validate_module(MockModule())

    def test_performance_validator_caching(self):
        validator = PerformanceOptimizedValidator()
        target = "test_dag"
        
        # First run
        validator.validate(target)
        # Second run should use cache
        assert validator.validate(target) is True
