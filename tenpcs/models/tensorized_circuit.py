from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, Type, Union
import numpy as np
import torch
from torch import Tensor, nn

from tenpcs.layers.input import InputLayer
from tenpcs.layers.input.exp_family import ExpFamilyLayer
from tenpcs.layers.input.integral import IntegralInputLayer
from tenpcs.layers.layer import Layer
from tenpcs.layers.scope import ScopeLayer
from tenpcs.layers.sum import SumLayer
from tenpcs.layers.sum_product import SumProductLayer
from tenpcs.region_graph import PartitionNode, RegionGraph, RegionNode
from tenpcs.reparams.leaf import ReparamIdentity
from tenpcs.utils.scope import one_hot_variables
from tenpcs.utils.type_aliases import ReparamFactory

# TODO: rework docstrings


class TensorizedPC(nn.Module):
    """Tensorized and folded PC implementation."""

    @classmethod
    # pylint: disable-next=too-many-locals
    def from_region_graph(  # type: ignore[misc]   # pylint: disable=too-many-arguments
        cls,
        rg: RegionGraph,
        layer_cls: Union[Type[SumProductLayer], List[Type[SumProductLayer]]],
        efamily_cls: Type[ExpFamilyLayer],
        *,
        layer_kwargs: Optional[Dict[str, Any]] = None,
        efamily_kwargs: Optional[Dict[str, Any]] = None,
        reparam: ReparamFactory = ReparamIdentity,
        num_inner_units: int = 2,
        num_input_units: int = 2,
        num_channels: int = 1,
        num_classes: int = 1,
        bottom_up_folding: bool = False
    ) -> "TensorizedPC":
        """Construct a folded TensorizedPC given a region graph, layers and hyper-parameters.

        Args:
            rg (RegionGraph): The region rg.
            layer_cls (Type[SumProductLayer]): The inner layer class.
            efamily_cls (Type[ExpFamilyLayer]): The exponential family class.
            layer_kwargs (Dict[str, Any]): The parameters for the inner layer class.
            efamily_kwargs (Dict[str, Any]): The parameters for the exponential family class.
            reparam (Callable[[Tensor], Tensor]): The reparametrization function.
            num_inner_units (int): The number of units for each layer.
            num_input_units (int): The number of input units in the input layer.
            num_channels (int): The number of channels (e.g., 3 for RGB pixel). Defaults to 1.
            num_classes (int): The number of classes of the PC. Defaults to 1.

        Returns:
            TensorizedPC: A tensorized circuit.
        """
        assert num_inner_units > 0, "The number of output units per layer should be positive"
        assert (
            num_input_units > 0
        ), "The number of input untis in the input layer should be positive"
        assert num_classes > 0, "The number of classes should be positive"
        assert (
            len(list(rg.output_nodes)) == 1
        ), "Currently only circuits with single root region node are supported."

        # TODO: return a list instead?
        num_variables = rg.num_vars
        output_node = list(rg.output_nodes)[0]
        assert output_node.scope == set(
            range(num_variables)
        ), "The region rg should have been defined on the same number of variables"

        if layer_kwargs is None:  # type: ignore[misc]
            layer_kwargs = {}
        if efamily_kwargs is None:  # type: ignore[misc]
            efamily_kwargs = {}

        # Algorithm 1 in the paper -- organize the PC in layers  NOT BOTTOM UP !!!
        rg_layers = rg.topological_layers(bottom_up=bottom_up_folding)

        # Initialize input layer
        input_layer = efamily_cls(
            num_vars=rg.num_vars,
            num_channels=num_channels,
            num_replicas=rg.num_replicas,
            num_output_units=num_input_units,
            **efamily_kwargs,  # type: ignore[misc]
        )

        # Initialize scope layer
        scope_layer = ScopeLayer(rg_layers[0][1])

        # Build layers: this will populate the bookkeeping data structure above
        bookkeeping, inner_layers = TensorizedPC._build_layers(
            rg_layers,
            layer_cls,
            layer_kwargs,  # type: ignore[misc]
            reparam,
            num_inner_units,
            num_input_units,
            num_classes=num_classes,
        )

        return cls(input_layer, scope_layer, bookkeeping, inner_layers)

    def __init__(  # pylint: disable=too-many-arguments
        self,
        input_layer: InputLayer,
        scope_layer: ScopeLayer,
        bookkeeping: List[Tuple[bool, List[int], Tensor]],
        inner_layers: Union[List[Layer], nn.ModuleList],
        integral_input_layer: Optional[IntegralInputLayer] = None,
    ) -> None:
        """Initialize a tensorized circuit.

        Args:
            input_layer: The input layer.
            scope_layer: The scope layer, which maps the output of the input layer to the input
             of the circuit based on the specified region nodes.
            bookkeeping: The bookkeeping data structure.
             For each layer keep track of the following information
             (i) Whether the input tensor needs to be padded.
             This is necessary if we want to fold layers with different number of inputs.
             (ii) The list of layers whose output tensors needs to be concatenated.
             This is necessary because the inputs of a layer might come from different layers.
             (iii) The tensorized indices of shape (fold count, arity): (F, H),
             where arity is the number of inputs of the layer. When folding
             layers with different arity (e.g., the mixing layer) a padding will be
             added *and* the last dimension will correspond to the maximum arity.
            inner_layers: A list or module list consisting of layers.
            integral_input_layer: The integral input layer, it can be None and will be lazily built.
        """
        super().__init__()
        self.input_layer = input_layer
        self.scope_layer = scope_layer
        self.bookkeeping = bookkeeping
        if isinstance(inner_layers, list):
            inner_layers = nn.ModuleList(inner_layers)
        # TODO: can we annotate a list here?
        self.inner_layers: List[Layer] = inner_layers  # type: ignore[assignment]
        self.integral_input_layer = integral_input_layer

    @staticmethod
    # pylint: disable-next=too-many-arguments,too-complex,too-many-locals,too-many-statements
    def _build_layers(  # type: ignore[misc]
        rg_layers: List[Tuple[List[PartitionNode], List[RegionNode]]],
        layer_cls: Union[Type[SumProductLayer], List[Type[SumProductLayer]]],
        layer_kwargs: Dict[str, Any],
        reparam: ReparamFactory,
        num_inner_units: int,
        num_input_units: int,
        num_classes: int = 1,
    ) -> Tuple[List[Tuple[bool, List[int], Tensor]], List[Layer]]:
        """Build the layers of the network.

        Args:
            rg_layers: The region graph layers.
            layer_cls (Type[SumProductNetwork]): The layer class.
            layer_kwargs (Dict[str, Any]): The layer arguments.
            reparam (ReparamFunction): The reparametrization function.
            num_inner_units (int): The number of units per inner layer.
            num_input_units (int): The number of units of the input layer.
            num_classes (int): The number of outputs of the network.
        """
        inner_layers: List[Layer] = []
        bookkeeping: List[Tuple[bool, List[int], Tensor]] = []

        # A dictionary mapping each region node ID to
        #   (i) its index in the corresponding fold, and
        #   (ii) the id of the layer that computes such fold
        #        (0 for the input layer and > 0 for inner layers)
        # TODO: do we really need node_id? or just object itself? or id()?
        region_id_fold: Dict[int, Tuple[int, int]] = {}
        for i, region in enumerate(rg_layers[0][1]):
            region_id_fold[region.node_id] = (i, 0)

        # A list mapping layer ids to the number of folds in the output tensor
        num_folds = [len(rg_layers[0][1])]

        # Build inner layers
        for rg_layer_idx, (lpartitions, lregions) in enumerate(rg_layers[1:], start=1):
            # Gather the input regions of each partition
            input_regions = [sorted(p.inputs) for p in lpartitions]
            num_input_regions = list(len(ins) for ins in input_regions)
            max_num_input_regions = max(num_input_regions)

            # Retrieve which folds need to be concatenated
            input_regions_ids = [list(r.node_id for r in ins) for ins in input_regions]
            input_layers_ids = [
                list(region_id_fold[i][1] for i in ids) for ids in input_regions_ids
            ]
            unique_layer_ids = list(set(i for ids in input_layers_ids for i in ids))
            cumulative_idx: List[int] = np.cumsum(  # type: ignore[misc]
                [0] + [num_folds[i] for i in unique_layer_ids]
            ).tolist()
            base_layer_idx = dict(zip(unique_layer_ids, cumulative_idx))

            # Build indices
            should_pad = False
            input_region_indices = []  # (F, H)
            for regions in input_regions:
                region_indices = []
                for r in regions:
                    fold_idx, layer_id = region_id_fold[r.node_id]
                    region_indices.append(base_layer_idx[layer_id] + fold_idx)
                if len(regions) < max_num_input_regions:
                    should_pad = True
                    region_indices.extend(
                        [cumulative_idx[-1]] * (max_num_input_regions - len(regions))
                    )
                input_region_indices.append(region_indices)
            fold_indices = torch.tensor(input_region_indices)
            book_entry = (should_pad, unique_layer_ids, fold_indices)
            bookkeeping.append(book_entry)

            # Update dictionaries and number of folds
            region_mixing_indices: Dict[int, List[int]] = defaultdict(list)
            for i, p in enumerate(lpartitions):
                assert len(p.outputs) == 1, "Each partition must belong to exactly one region"
                out_region = p.outputs[0]
                if len(out_region.inputs) == 1:
                    region_id_fold[out_region.node_id] = (i, len(inner_layers) + 1)
                else:
                    region_mixing_indices[out_region.node_id].append(i)
            num_folds.append(len(lpartitions))

            # Build the actual layer
            num_outputs = num_inner_units if rg_layer_idx < len(rg_layers) - 1 else num_classes
            num_inputs = num_input_units if rg_layer_idx == 1 else num_inner_units
            # TODO: add shape analysis for unsqueeze
            fold_mask = (fold_indices < cumulative_idx[-1]) if should_pad else None

            # TODO: check this AMARI edit
            if isinstance(layer_cls, list):
                current_layer_cls = layer_cls[rg_layer_idx-1]
            else:
                current_layer_cls = layer_cls



            layer = current_layer_cls(
                num_input_units=num_inputs,
                num_output_units=num_outputs,
                arity=max_num_input_regions,
                num_folds=num_folds[-1],
                fold_mask=fold_mask,
                reparam=reparam,
                **layer_kwargs,  # type: ignore[misc]
            )
            inner_layers.append(layer)

            # Fold mixing layers, if any
            if not (non_unary_regions := [r for r in lregions if len(r.inputs) > 1]):
                continue
            max_num_input_partitions = max(len(r.inputs) for r in non_unary_regions)

            # Same as above, construct indices and update dictionaries
            should_pad = False
            input_partition_indices = []  # (F, H)
            for i, region in enumerate(non_unary_regions):
                num_input_partitions = len(region.inputs)
                partition_indices = region_mixing_indices[region.node_id]
                if max_num_input_partitions > num_input_partitions:
                    should_pad = True
                    partition_indices.extend(
                        [num_folds[-1]] * (max_num_input_partitions - num_input_partitions)
                    )
                input_partition_indices.append(partition_indices)
                region_id_fold[region.node_id] = (i, len(inner_layers) + 1)
            num_folds.append(len(non_unary_regions))
            fold_indices = torch.tensor(input_partition_indices)
            book_entry = (should_pad, [len(inner_layers)], fold_indices)
            bookkeeping.append(book_entry)

            # Build the actual mixing layer
            # TODO: add shape analysis for unsqueeze
            fold_mask = (fold_indices < num_folds[-2]) if should_pad else None
            mixing_layer = SumLayer(
                num_input_units=num_outputs,
                num_output_units=num_outputs,
                arity=max_num_input_partitions,
                num_folds=num_folds[-1],
                fold_mask=fold_mask,
                reparam=reparam,
            )
            inner_layers.append(mixing_layer)

        return bookkeeping, inner_layers

    @property
    def num_vars(self) -> int:
        """The number of variables the circuit is defined on."""
        return self.input_layer.num_vars

    def __call__(self, x: Tensor) -> Tensor:
        """Invoke the forward function.

        Args:
            x (Tensor): The input to the circuit, shape (*B, D, C).

        Returns:
            Tensor: The output of the circuit, shape (*B, K).
        """
        return super().__call__(x)  # type: ignore[no-any-return,misc]

    def _eval_layers(self, x: Tensor) -> Tensor:
        """Eval the inner layers of the circuit.

        Args:
            x (Tensor): The folded inputs to be passed to the sequence of layers, shape (F, K, *B).

        Returns:
            Tensor: The output of the circuit, shape (K, *B).
        """
        layer_outputs = [x]  # list of shape (F, K, *B)

        for layer, (should_pad, in_layer_ids, fold_idx) in zip(self.inner_layers, self.bookkeeping):
            layer_inputs = [layer_outputs[i] for i in in_layer_ids]  # list of shape (F, K, *B)
            if should_pad:
                # TODO: The padding value depends on the computation space.
                #  It should be the neutral element of a group.
                #  For now computations are in log-space, thus 0 is our pad value.
                layer_inputs.append(
                    torch.zeros(()).to(layer_inputs[0]).expand_as(layer_inputs[0][0:1])
                )  # shape (1, K, *B)
            inputs = torch.cat(layer_inputs, dim=0)  # shape (sum(F), K, *B)
            # fold_idx shape (F, H)
            inputs = inputs[fold_idx]  # shape (F, H, K, *B)
            outputs = layer(inputs)  # shape (F, K, *B)
            layer_outputs.append(outputs)

        return layer_outputs[-1].squeeze(dim=0)  # shape (F=1, K, *B) -> (K, *B)

    def forward(self, x: Tensor) -> Tensor:
        """Evaluate the circuit in a feed forward way.

        Args:
            x (Tensor): The input to the circuit, shape (*B, D, C).

        Returns:
            Tensor: The output of the circuit, shape (*B, K).
        """
        x = self.input_layer(x)  # shape (*B, D, K, P)
        x = self.scope_layer(x)  # shape (F, K, *B)
        return self._eval_layers(x).movedim(source=0, destination=-1)  # shape (K, *B) -> (*B, K)

    def integrate(self, x: Tensor, in_vars: Union[List[int], List[List[int]]]) -> Tensor:
        """Evaluate an integral of the circuit over some variables.

        Args:
            x: The input having shape (batch_size, num_vars, num_channels).
            in_vars: A list of variables to integrate, or a batch of variables to integrate.

        Returns:
            Tensor: The evaluation of an integral of the circuit.
        """
        if self.integral_input_layer is None:
            # Initialize the integral input layer
            self.integral_input_layer = IntegralInputLayer(self.input_layer)

        # TODO: cache the construction of the integration mask here.
        #  Perhaps we can hash in_vars and construct a cache for them in this class.
        in_mask = one_hot_variables(self.num_vars, in_vars, device=x.device)
        in_outputs = self.scope_layer(self.integral_input_layer(x, in_mask))
        return self._eval_layers(in_outputs).movedim(source=0, destination=-1)
