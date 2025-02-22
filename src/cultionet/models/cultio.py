import typing as T

from . import model_utils
from .nunet import NestedUNet
from .convstar import StarRNN

import numpy as np
import torch
from torch_geometric.data import Data
from torch_geometric import nn


class CultioGraphNet(torch.nn.Module):
    """The cultionet graph network model framework

    Args:
        ds_features (int): The total number of dataset features (bands x time).
        ds_time_features (int): The number of dataset time features in each band/channel.
        filters (int): The number of output filters for each stream.
        num_classes (int): The number of output classes.
        dropout (Optional[float]): The dropout fraction for the transformer stream.
    """
    def __init__(
        self,
        ds_features: int,
        ds_time_features: int,
        filters: int = 32,
        num_classes: int = 2,
        dropout: T.Optional[float] = 0.1
    ):
        super(CultioGraphNet, self).__init__()

        self.ds_features = ds_features
        self.ds_time_features = ds_time_features
        self.num_indices = int(self.ds_features / self.ds_time_features)
        self.filters = filters
        num_quantiles = 3
        num_index_streams = 2
        base_in_channels = (filters * self.num_indices) * num_index_streams + self.filters

        self.gc = model_utils.GraphToConv()
        self.cg = model_utils.ConvToGraph()

        # Transformer stream (+self.filters x self.num_indices)
        self.transformer = self.mid_sequence_weights(
            nn.TransformerConv(self.ds_time_features, self.filters, heads=1, edge_dim=2, dropout=dropout)
        )

        # Nested UNet (+self.filters x self.num_indices)
        self.nunet = NestedUNet(in_channels=self.ds_time_features, out_channels=self.filters)
        self.star_rnn = StarRNN(
            input_dim=self.num_indices,
            hidden_dim=32,
            nclasses=self.filters,
            n_layers=4
        )

        # Boundary distances (+num_quantiles) (0.1, 0.5, 0.9)
        self.dist_layer = self.final_sequence_weights(
            nn.GCNConv(base_in_channels, self.filters, improved=True),
            nn.TransformerConv(self.filters, self.filters, heads=1, edge_dim=2, dropout=0.1),
            self.filters, num_quantiles
        )

        # Edges (+num_classes)
        self.edge_layer = self.final_sequence_weights_logits(
            nn.GCNConv(base_in_channels+num_quantiles, self.filters, improved=True),
            nn.TransformerConv(self.filters, num_classes, heads=1, edge_dim=2, dropout=0.1),
            self.filters, num_classes
        )

        # Classes (+num_classes)
        self.class_layer = self.final_sequence_weights_logits(
            nn.GCNConv(base_in_channels+num_quantiles+num_classes, self.filters, improved=True),
            nn.TransformerConv(
                self.filters, num_classes, heads=1, edge_dim=2, dropout=0.1
            ),
            self.filters, num_classes
        )

    @staticmethod
    def mid_sequence_weights(conv: T.Callable) -> nn.Sequential:
        return nn.Sequential('x, edge_index, edge_weight',
                             [
                                 (conv, 'x, edge_index, edge_weight -> x'),
                                 (torch.nn.ELU(alpha=0.1, inplace=False), 'x -> x')
                             ])

    @staticmethod
    def final_sequence_weights(
        conv1: T.Callable, conv2: T.Callable, mid_channels: int, out_channels: int
    ) -> nn.Sequential:
        return nn.Sequential('x, edge_index, edge_weight, edge_weight2d',
                             [
                                 (conv1, 'x, edge_index, edge_weight -> x'),
                                 (nn.BatchNorm(in_channels=mid_channels), 'x -> x'),
                                 (conv2, 'x, edge_index, edge_weight2d -> x'),
                                 (nn.BatchNorm(in_channels=mid_channels), 'x -> x'),
                                 (torch.nn.ELU(alpha=0.1, inplace=False), 'x -> x'),
                                 (torch.nn.Linear(mid_channels, out_channels), 'x -> x')
                             ])

    @staticmethod
    def final_sequence_weights_logits(
        conv1: T.Callable, conv2: T.Callable, mid_channels: int, out_channels: int
    ) -> nn.Sequential:
        return nn.Sequential('x, edge_index, edge_weight, edge_weight2d',
                             [
                                 (conv1, 'x, edge_index, edge_weight -> x'),
                                 (nn.BatchNorm(in_channels=mid_channels), 'x -> x'),
                                 (conv2, 'x, edge_index, edge_weight2d -> x'),
                                 (nn.BatchNorm(in_channels=out_channels), 'x -> x'),
                                 (torch.nn.ELU(alpha=0.1, inplace=False), 'x -> x')
                             ])

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

    def forward(self, data: Data) -> T.Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        # Transformer on each band time series
        transformer_stream = []
        for band in range(0, self.ds_features, self.ds_time_features):
            t = self.transformer(
                data.x[:, band:band+self.ds_time_features], data.edge_index, data.edge_attrs
            )
            transformer_stream.append(t)
        transformer_stream = torch.cat(transformer_stream, dim=1)

        # Nested UNet on each band time series
        nunet_stream = []
        for band in range(0, self.ds_features, self.ds_time_features):
            t = self.nunet(
                data.x[:, band:band+self.ds_time_features],
                data.edge_index,
                data.edge_attrs[:, 1],
                data.batch,
                int(data.height[0]),
                int(data.width[0])
            )
            nunet_stream.append(t)
        nunet_stream = torch.cat(nunet_stream, dim=1)

        # RNN ConvStar
        # Reshape from (B x C x H x W) -> (B x T x C x H x W)
        star_stream = self.gc(
            data.x, data.batch.unique().size(0), int(data.height[0]), int(data.width[0])
        )
        nbatch, ntime, height, width = star_stream.shape
        star_stream = star_stream.reshape(
            nbatch, self.num_indices, self.ds_time_features, height, width
        ).permute(0, 2, 1, 3, 4)
        star_stream = self.star_rnn(star_stream)
        star_stream = self.cg(star_stream)

        # Concatenate streams
        h = torch.cat([transformer_stream, nunet_stream, star_stream], dim=1)

        # Estimate distance from edges
        logits_distances = self.dist_layer(
            h,
            data.edge_index,
            data.edge_attrs[:, 1],
            data.edge_attrs
        )

        # Concatenate streams + distances
        h = torch.cat([h, logits_distances], dim=1)

        # Estimate edges
        logits_edges = self.edge_layer(
            h,
            data.edge_index,
            data.edge_attrs[:, 1],
            data.edge_attrs
        )

        # Concatenate streams + distances + edges
        h = torch.cat([h, logits_edges], dim=1)

        # Estimate all classes
        logits_labels = self.class_layer(
            h,
            data.edge_index,
            data.edge_attrs[:, 1],
            data.edge_attrs
        )

        return logits_distances, logits_edges, logits_labels
