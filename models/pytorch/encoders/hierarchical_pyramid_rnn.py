#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Hierarchical Pyramid RNN encoders."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from utils.io.variable import var2np


class HierarchicalPyramidRNNEncoder(nn.Module):
    """Hierarchical Pyramid RNN encoder.
    Args:
        input_size (int): the dimension of input features
        rnn_type (string): lstm or gru or rnn
        bidirectional (bool): if True, use the bidirectional encoder
        num_units (int): the number of units in each layer
        num_proj (int): the number of nodes in the projection layer
        num_layers (int): the number of layers in the main task
        num_layers_sub (int): the number of layers in the sub task
        dropout (float): the probability to drop nodes
        parameter_init (float): the range of uniform distribution to
            initialize weight parameters (>= 0)
        subsample_list (list): subsample in the corresponding layers (True)
            ex.) [False, True, True, False] means that downsample is conducted
                in the 2nd and 3rd layers.
        subsample_type (string, optional): drop or concat
        use_cuda (bool, optional): if True, use GPUs
        batch_first (bool, optional): if True, batch-major computation will be
            performed
        merge_bidirectional (bool, optional): if True, sum bidirectional outputs
    """

    def __init__(self,
                 input_size,
                 rnn_type,
                 bidirectional,
                 num_units,
                 num_proj,
                 num_layers,
                 num_layers_sub,
                 dropout,
                 parameter_init,
                 subsample_list,
                 subsample_type='drop',
                 use_cuda=False,
                 batch_first=False,
                 merge_bidirectional=False):

        super(HierarchicalPyramidRNNEncoder, self).__init__()

        if num_layers_sub < 1 or num_layers < num_layers_sub:
            raise ValueError(
                'Set num_layers_sub between 1 to num_layers.')
        if len(subsample_list) != num_layers:
            raise ValueError(
                'subsample_list must be the same size as num_layers.')
        if subsample_type not in ['drop', 'concat']:
            raise TypeError('subsample_type must be "drop" or "concat".')

        self.input_size = input_size
        self.rnn_type = rnn_type
        self.bidirectional = bidirectional
        self.num_directions = 2 if bidirectional else 1
        self.num_units = num_units
        self.num_proj = num_proj
        self.num_layers = num_layers
        self.num_layers_sub = num_layers_sub
        self.dropout = dropout
        # NOTE: dropout is applied except the last layer
        self.parameter_init = parameter_init
        self.use_cuda = use_cuda
        self.batch_first = batch_first
        self.merge_bidirectional = merge_bidirectional

        self.subsample_list = subsample_list
        self.subsample_type = subsample_type

        self.rnns = []
        for i_layer in range(num_layers):
            if i_layer == 0:
                next_input_size = input_size
            else:
                next_input_size = num_units * self.num_directions
                if subsample_type == 'concat' and i_layer > 0 and subsample_list[i_layer - 1] and i_layer != num_layers_sub:
                    next_input_size *= 2

            if rnn_type == 'lstm':
                rnn = nn.LSTM(
                    next_input_size,
                    hidden_size=num_units,
                    num_layers=1,
                    bias=True,
                    batch_first=batch_first,
                    dropout=dropout,
                    bidirectional=bidirectional)
            elif rnn_type == 'gru':
                rnn = nn.GRU(
                    next_input_size,
                    hidden_size=num_units,
                    num_layers=1,
                    bias=True,
                    batch_first=batch_first,
                    dropout=dropout,
                    bidirectional=bidirectional)
            elif rnn_type == 'rnn':
                rnn = nn.RNN(
                    next_input_size,
                    hidden_size=num_units,
                    num_layers=1,
                    bias=True,
                    batch_first=batch_first,
                    dropout=dropout,
                    bidirectional=bidirectional)
            else:
                raise ValueError('rnn_type must be "lstm" or "gru" or "rnn".')

            setattr(self, 'p' + rnn_type + '_l' + str(i_layer), rnn)

            if use_cuda:
                rnn = rnn.cuda()

            self.rnns.append(rnn)

    def _init_hidden(self, batch_size, volatile):
        """Initialize hidden states.
        Args:
            batch_size (int): the size of mini-batch
            volatile (bool): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            if rnn_type is 'lstm', return a tuple of tensors (h_0, c_0).
                h_0: A tensor of size
                    `[num_layers * num_directions, batch_size, num_units]`
                c_0: A tensor of size
                    `[num_layers * num_directions, batch_size, num_units]`
            otherwise return h_0.
        """
        h_0 = Variable(torch.zeros(
            1 * self.num_directions, batch_size, self.num_units))

        if volatile:
            h_0.volatile = True

        if self.use_cuda:
            h_0 = h_0.cuda()

        if self.rnn_type == 'lstm':
            c_0 = Variable(torch.zeros(
                1 * self.num_directions, batch_size, self.num_units))

            if volatile:
                c_0.volatile = True

            if self.use_cuda:
                c_0 = c_0.cuda()

            return (h_0, c_0)
        else:
            # gru or rnn
            return h_0

    def forward(self, inputs, inputs_seq_len, volatile=True,
                mask_sequence=True):
        """Forward computation.
        Args:
            inputs: A tensor of size `[B, T, input_size]`
            inputs_seq_len (IntTensor or LongTensor): A tensor of size `[B]`
            volatile (bool, optional): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
            mask_sequence (bool, optional): if True, mask by sequence
                lenghts of inputs
        Returns:
            outputs:
                if batch_first is True, a tensor of size
                    `[B, T // sum(subsample_list), num_units (* num_directions)]`
                else
                    `[T // sum(subsample_list), B, num_units (* num_directions)]`
            final_state_fw: A tensor of size `[1, B, num_units]`
            outputs_sub:
                if batch_first is True, a tensor of size
                    `[B, T // sum(subsample_list), num_units (* num_directions)]`
                else
                    `[T // sum(subsample_list), B, num_units (* num_directions)]`
            final_state_fw_sub: A tensor of size `[1, B, num_units]`
            perm_indices ():
        """
        batch_size, max_time = inputs.size()[:2]

        # Initialize hidden states (and memory cells) per mini-batch
        h_0 = self._init_hidden(batch_size=batch_size, volatile=volatile)

        if mask_sequence:
            # Sort inputs by lengths in descending order
            inputs_seq_len, perm_indices = inputs_seq_len.sort(
                dim=0, descending=True)
            inputs = inputs[perm_indices]
        else:
            perm_indices = None

        if not self.batch_first:
            # Reshape to the time-major
            inputs = inputs.transpose(0, 1)

        if not isinstance(inputs_seq_len, list):
            pack_seq_len = var2np(inputs_seq_len).tolist()
        else:
            pack_seq_len = inputs_seq_len

        outputs = inputs
        for i_layer in range(self.num_layers):
            if mask_sequence:
                # Pack encoder inputs in each layer
                outputs = pack_padded_sequence(
                    outputs, pack_seq_len, batch_first=self.batch_first)

            if self.rnn_type == 'lstm':
                outputs, (h_n, c_n) = self.rnns[i_layer](outputs, hx=h_0)
            else:
                outputs, h_n = self.rnns[i_layer](outputs, hx=h_0)

            if mask_sequence:
                # Unpack encoder outputs in each layer
                outputs, unpacked_seq_len = pad_packed_sequence(
                    outputs, batch_first=self.batch_first)
                # TODO: update version for padding_value=0.0

                assert pack_seq_len == unpacked_seq_len

            outputs_list = []
            if self.subsample_list[i_layer]:
                for t in range(max_time):
                    # Pick up features at even time step
                    if (t + 1) % 2 == 0:
                        if self.batch_first:
                            outputs_t = outputs[:, t:t + 1, :]
                            # NOTE: `[B, 1, num_units * num_directions]`
                        else:
                            outputs_t = outputs[t:t + 1, :, :]
                            # NOTE: `[1, B, num_units * num_directions]`

                        # Concatenate the successive frames
                        if self.subsample_type == 'concat' and i_layer not in [self.num_layers - 1, self.num_layers_sub - 1]:
                            if self.batch_first:
                                outputs_t_prev = outputs[:, t - 1:t, :]
                            else:
                                outputs_t_prev = outputs[t - 1:t, :, :]
                            outputs_t = torch.cat(
                                [outputs_t_prev, outputs_t], dim=2)

                        outputs_list.append(outputs_t)

                if self.batch_first:
                    outputs = torch.cat(outputs_list, dim=1)
                    # `[B, T_prev // 2, num_units (* 2) * num_directions]`
                    max_time = outputs.size(1)
                else:
                    outputs = torch.cat(outputs_list, dim=0)
                    # `[T_prev // 2, B, num_units (* 2) * num_directions]`
                    max_time = outputs.size(0)

                # Update inputs_seq_len
                for i in range(len(pack_seq_len)):
                    pack_seq_len[i] = pack_seq_len[i] // 2

            if i_layer == self.num_layers_sub - 1:
                outputs_sub = outputs
                h_n_sub = h_n

        # Sum bidirectional outputs
        if self.bidirectional and self.merge_bidirectional:
            outputs = outputs[:, :, :self.num_units] + \
                outputs[:, :, self.num_units:]
            outputs_sub = outputs_sub[:, :, :self.num_units] + \
                outputs_sub[:, :, self.num_units:]

        # Pick up the final state of the top layer (forward)
        if self.num_directions == 2:
            final_state_fw = h_n[-2:-1, :, :]
            final_state_fw_sub = h_n_sub[-2:-1, :, :]
        else:
            final_state_fw = h_n[-1, :, :].unsqueeze(dim=0)
            final_state_fw_sub = h_n_sub[-1, :, :].unsqueeze(dim=0)
        # NOTE: h_n: `[num_layers * num_directions, B, num_units]`
        #   h_n_sub: `[num_layers_sub * num_directions, B, num_units]`

        return outputs, final_state_fw, outputs_sub, final_state_fw_sub, perm_indices