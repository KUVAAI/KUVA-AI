"""
KUVA AI GNN Model (DGL Implementation)
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

import dgl
import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from dgl.nn import GATv2Conv, GINConv, SAGEConv
from dgl.nn.pytorch import HeteroGraphConv
from kuva.common.monitoring import GNNMetricsCollector
from kuva.common.types import GraphData, ModelConfig
from kuva.common.utils import validate_config
from torch.optim import AdamW
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import DataLoader, DistributedSampler

logger = logging.getLogger(__name__)

class DynamicGraphBuilder:
    """Constructs DGL graphs from environment observations"""
    
    def __init__(self, config: ModelConfig) -> None:
        self.config = validate_config(config, ModelConfig)
        self._init_adapters()
        
    def _init_adapters(self) -> None:
        """Load environment-specific graph construction logic"""
        self.edge_builders = {
            'collaboration': self._build_collab_edges,
            'communication': self._build_comm_edges,
        }
        self.node_featurizers = {
            'agent': self._featurize_agent,
            'resource': self._featurize_resource,
        }
    
    def __call__(self, data: GraphData) -> dgl.DGLGraph:
        """Convert raw data to heterogeneous graph"""
        graph = dgl.heterograph({
            ('agent', 'collaborates', 'agent'): self.edge_builders['collaboration'](data),
            ('agent', 'communicates', 'agent'): self.edge_builders['communication'](data),
            ('agent', 'uses', 'resource'): self._build_usage_edges(data),
        })
        
        # Add node features
        graph.nodes['agent'].data['features'] = self.node_featurizers['agent'](data)
        graph.nodes['resource'].data['features'] = self.node_featurizers['resource'](data)
        
        return graph
    
    def _build_collab_edges(self, data: GraphData) -> Tuple[List[int], List[int]]:
        """Create collaboration edges based on task dependencies"""
        src, dst = zip(*data.task_dependencies)
        return (list(src), list(dst))
    
    def _build_comm_edges(self, data: GraphData) -> Tuple[List[int], List[int]]:
        """Create communication edges with dynamic weights"""
        comm_matrix = data.communication_matrix
        return torch.where(comm_matrix > self.config.comm_threshold)
    
    def _build_usage_edges(self, data: GraphData) -> Tuple[List[int], List[int]]:
        """Connect agents to resources they're using"""
        return (data.agent_ids, data.resource_ids)
    
    def _featurize_agent(self, data: GraphData) -> torch.Tensor:
        """Create agent node features"""
        features = torch.cat([
            data.agent_states,
            data.agent_skills,
            data.task_progress.unsqueeze(1),
        ], dim=1)
        return F.normalize(features, p=2, dim=1)
    
    def _featurize_resource(self, data: GraphData) -> torch.Tensor:
        """Create resource node features"""
        return F.normalize(data.resource_states, p=2, dim=1)

class KUVAHeteroGNN(nn.Module):
    """Enterprise-grade heterogeneous GNN with adaptive architecture"""
    
    def __init__(self, config: ModelConfig) -> None:
        super().__init__()
        self.config = validate_config(config, ModelConfig)
        self.metrics = GNNMetricsCollector()
        self._build_layers()
        self._init_weights()
        
    def _build_layers(self) -> None:
        """Dynamically construct GNN layers based on config"""
        self.conv_layers = nn.ModuleList()
        for layer_cfg in self.config.gnn_layers:
            layer = self._create_layer(layer_cfg)
            self.conv_layers.append(layer)
            
        self.adapters = nn.ModuleDict({
            'agent': nn.Linear(self.config.agent_feat_dim, self.config.hidden_dim),
            'resource': nn.Linear(self.config.resource_feat_dim, self.config.hidden_dim),
        })
        
        self.classifier = nn.Sequential(
            nn.Linear(self.config.hidden_dim, self.config.hidden_dim * 2),
            nn.ReLU(),
            nn.Dropout(self.config.dropout),
            nn.Linear(self.config.hidden_dim * 2, self.config.num_classes),
        )
    
    def _create_layer(self, layer_cfg: Dict) -> nn.Module:
        """Factory method for GNN layers"""
        layer_type = layer_cfg['type'].lower()
        if layer_type == 'gatv2':
            return HeteroGraphConv({
                rel: GATv2Conv(
                    in_feats=(self.config.hidden_dim, self.config.hidden_dim),
                    out_feats=layer_cfg['hidden_dim'],
                    num_heads=layer_cfg['heads'],
                    feat_drop=layer_cfg.get('dropout', 0.1),
                    attn_drop=layer_cfg.get('attn_dropout', 0.1),
                    residual=True,
                )
                for rel in ['collaborates', 'communicates', 'uses']
            }, aggregate='sum')
        elif layer_type == 'sage':
            return HeteroGraphConv({
                rel: SAGEConv(
                    in_feats=(self.config.hidden_dim, self.config.hidden_dim),
                    out_feats=layer_cfg['hidden_dim'],
                    aggregator_type=layer_cfg['aggregator'],
                    feat_drop=layer_cfg.get('dropout', 0.1),
                    activation=F.gelu,
                )
                for rel in ['collaborates', 'communicates', 'uses']
            }, aggregate='mean')
        elif layer_type == 'gin':
            return HeteroGraphConv({
                rel: GINConv(
                    apply_func=nn.Sequential(
                        nn.Linear(self.config.hidden_dim, layer_cfg['hidden_dim']),
                        nn.BatchNorm1d(layer_cfg['hidden_dim']),
                        nn.GELU(),
                    ),
                    aggregator_type='sum',
                )
                for rel in ['collaborates', 'communicates', 'uses']
            }, aggregate='stack')
        else:
            raise ValueError(f"Unsupported GNN layer type: {layer_type}")
    
    def _init_weights(self) -> None:
        """Xavier initialization with residual scaling"""
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.xavier_normal_(module.weight)
                if module.bias is not None:
                    nn.init.constant_(module.bias, 0)
            elif isinstance(module, (GATv2Conv, SAGEConv, GINConv)):
                nn.init.kaiming_normal_(module.fc.weight)
                
    def forward(self, g: dgl.DGLGraph) -> torch.Tensor:
        """Forward pass with instrumentation"""
        # Feature projection
        h = {
            'agent': self.adapters['agent'](g.nodes['agent'].data['features']),
            'resource': self.adapters['resource'](g.nodes['resource'].data['features']),
        }
        
        # GNN layers
        for i, conv in enumerate(self.conv_layers):
            h = conv(g, h)
            h = {k: F.gelu(v) for k, v in h.items()}
            
            # Record layer-wise metrics
            self.metrics.record_layer(
                layer_idx=i,
                activations=h['agent'].detach(),
                attention=getattr(conv, 'attn_weights', None),
            )
        
        # Readout
        with g.local_scope():
            g.ndata['h'] = h
            agent_embeds = dgl.readout_nodes(
                g, 'h', op='mean', ntype='agent'
            )
            return self.classifier(agent_embeds)
    
    def configure_optimizers(self) -> Tuple[torch.optim.Optimizer, torch.optim.lr_scheduler._LRScheduler]:
        """Optimization setup with weight decay and LR scheduling"""
        optimizer = AdamW(
            self.parameters(),
            lr=self.config.learning_rate,
            weight_decay=self.config.weight_decay,
        )
        scheduler = ReduceLROnPlateau(
            optimizer,
            mode='max',
            factor=0.5,
            patience=3,
            verbose=True,
        )
        return optimizer, scheduler

class GNNTrainer:
    """Production-grade GNN training pipeline"""
    
    def __init__(self, config: ModelConfig) -> None:
        self.config = validate_config(config, ModelConfig)
        self.device = torch.device(f"cuda:{self.config.device_id}" 
                                   if torch.cuda.is_available() else "cpu")
        self._init_distributed()
        self._init_components()
        
    def _init_distributed(self) -> None:
        """Initialize distributed training context"""
        if self.config.distributed:
            dist.init_process_group(
                backend='nccl' if torch.cuda.is_available() else 'gloo',
                init_method='env://',
            )
            self.rank = dist.get_rank()
            self.world_size = dist.get_world_size()
        else:
            self.rank = 0
            self.world_size = 1
            
    def _init_components(self) -> None:
        """Initialize model, data, and training tools"""
        self.model = KUVAHeteroGNN(self.config).to(self.device)
        if self.config.distributed:
            self.model = nn.parallel.DistributedDataParallel(
                self.model,
                device_ids=[self.config.device_id],
                output_device=self.config.device_id,
            )
            
        self.graph_builder = DynamicGraphBuilder(self.config)
        self.optimizer, self.scheduler = self.model.configure_optimizers()
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.config.use_amp)
        
    def train_epoch(self, loader: DataLoader) -> float:
        """Single training epoch with metrics collection"""
        self.model.train()
        total_loss = 0.0
        
        for batch_idx, data in enumerate(loader):
            self.optimizer.zero_grad(set_to_none=True)
            graphs = [self.graph_builder(d) for d in data]
            batched_graph = dgl.batch(graphs).to(self.device)
            labels = torch.cat([d.labels for d in data]).to(self.device)
            
            with torch.cuda.amp.autocast(enabled=self.config.use_amp):
                logits = self.model(batched_graph)
                loss = F.cross_entropy(logits, labels)
                
            self.scaler.scale(loss).backward()
            self._clip_gradients()
            self.scaler.step(self.optimizer)
            self.scaler.update()
            
            # Distributed metrics aggregation
            if self.config.distributed:
                dist.all_reduce(loss, op=dist.ReduceOp.SUM)
                loss = loss / self.world_size
                
            total_loss += loss.item()
            
            # Log metrics
            if batch_idx % self.config.log_interval == 0:
                self._log_metrics(batch_idx, loss.item())
                
        avg_loss = total_loss / len(loader)
        self.scheduler.step(avg_loss)
        return avg_loss
    
    def _clip_gradients(self) -> None:
        """Gradient clipping with adaptive threshold"""
        if self.config.grad_clip:
            nn.utils.clip_grad_norm_(
                self.model.parameters(),
                max_norm=self.config.grad_clip,
                norm_type=2,
            )
    
    def _log_metrics(self, batch_idx: int, loss: float) -> None:
        """Log training metrics to multiple backends"""
        metrics = {
            'train/loss': loss,
            'train/lr': self.optimizer.param_groups[0]['lr'],
            'train/batch': batch_idx,
        }
        self.model.metrics.log(metrics)
        
        if self.rank == 0:
            logger.info(f"Batch {batch_idx}: Loss={loss:.4f}")
            if self.config.use_tensorboard:
                for name, value in metrics.items():
                    self.writer.add_scalar(name, value, batch_idx)
    
    def evaluate(self, loader: DataLoader) -> Dict[str, float]:
        """Full evaluation with metrics export"""
        self.model.eval()
        total_correct = 0
        total_samples = 0
        
        with torch.no_grad():
            for data in loader:
                graphs = [self.graph_builder(d) for d in data]
                batched_graph = dgl.batch(graphs).to(self.device)
                labels = torch.cat([d.labels for d in data]).to(self.device)
                
                logits = self.model(batched_graph)
                preds = logits.argmax(dim=1)
                total_correct += (preds == labels).sum().item()
                total_samples += labels.size(0)
                
        accuracy = total_correct / total_samples
        return {'accuracy': accuracy}
    
    def save_checkpoint(self, path: str) -> None:
        """Save model state with metadata"""
        checkpoint = {
            'model_state': self.model.state_dict(),
            'optimizer_state': self.optimizer.state_dict(),
            'scheduler_state': self.scheduler.state_dict(),
            'config': self.config,
        }
        torch.save(checkpoint, path)
        
    def load_checkpoint(self, path: str) -> None:
        """Load checkpoint with device mapping"""
        checkpoint = torch.load(path, map_location=self.device)
        self.model.load_state_dict(checkpoint['model_state'])
        self.optimizer.load_state_dict(checkpoint['optimizer_state'])
        self.scheduler.load_state_dict(checkpoint['scheduler_state'])

class GNNExplainerWrapper:
    """Model explainability with GNNExplainer and production integration"""
    
    def __init__(self, model: KUVAHeteroGNN) -> None:
        self.model = model
        self.explainer = dgl.nn.GNNExplainer(
            model,
            num_hops=3,
            lr=0.01,
            feat_mask_type='feature',
        )
        
    def explain(self, graph: dgl.DGLGraph, target: int) -> Dict:
        """Generate explanation for specific prediction"""
        self.model.eval()
        feat_mask, edge_mask = self.explainer.explain_graph(graph)
        
        return {
            'node_importance': self._process_feat_mask(feat_mask),
            'edge_importance': self._process_edge_mask(edge_mask),
            'salient_subgraph': self._extract_salient_subgraph(graph, edge_mask),
        }
    
    def _process_feat_mask(self, mask: Dict[str, torch.Tensor]) -> Dict:
        """Convert feature mask to interpretable format"""
        return {ntype: mask[ntype].detach().cpu().numpy() for ntype in mask}
    
    def _process_edge_mask(self, mask: Dict[str, torch.Tensor]) -> Dict:
        """Convert edge mask to importance scores"""
        return {etype: mask[etype].detach().cpu().numpy() for etype in mask}
    
    def _extract_salient_subgraph(self, graph: dgl.DGLGraph, edge_mask: Dict) -> dgl.DGLGraph:
        """Extract important subgraph based on explanation masks"""
        threshold = 0.5  # Configurable threshold
        subgraphs = []
        for etype in edge_mask:
            edges = torch.where(edge_mask[etype] > threshold)
            subgraph = dgl.edge_subgraph(
                graph,
                {etype: edges},
                preserve_nodes=True,
            )
            subgraphs.append(subgraph)
        return dgl.merge(subgraphs)

# Production Optimization Techniques ###########################################

class QuantizedKUVAHeteroGNN(nn.Module):
    """Quantized GNN for production deployment"""
    
    def __init__(self, model: KUVAHeteroGNN) -> None:
        super().__init__()
        self.quant = torch.quantization.QuantStub()
        self.model = model
        self.dequant = torch.quantization.DeQuantStub()
        
    def forward(self, g: dgl.DGLGraph) -> torch.Tensor:
        g.ndata['features'] = {ntype: self.quant(feat) 
                              for ntype, feat in g.ndata['features'].items()}
        out = self.model(g)
        return self.dequant(out)

def optimize_for_production(model: KUVAHeteroGNN) -> QuantizedKUVAHeteroGNN:
    """Apply production optimizations: quantization, pruning, ONNX export"""
    # Apply dynamic quantization
    quantized_model = QuantizedKUVAHeteroGNN(model)
    quantized_model.qconfig = torch.quantization.get_default_qconfig('fbgemm')
    torch.quantization.prepare(quantized_model, inplace=True)
    # Calibration steps would go here
    torch.quantization.convert(quantized_model, inplace=True)
    
    # Pruning
    parameters_to_prune = [
        (module, 'weight') 
        for module in quantized_model.modules() 
        if isinstance(module, nn.Linear)
    ]
    torch.nn.utils.prune.global_unstructured(
        parameters_to_prune,
        pruning_method=torch.nn.utils.prune.L1Unstructured,
        amount=0.2,
    )
    
    return quantized_model
