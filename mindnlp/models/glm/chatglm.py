# Copyright 2023 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
# pylint: disable=C0325
# pylint: disable=C0103
# pylint: disable=W1401
# pylint: disable=C0415
# pylint: disable=C0209
# pylint: disable=R1721
""" MindSpore ChatGLM model. """

import math
import copy
import os
import warnings
import re

from typing import Optional, Tuple, List, Callable, Dict, Any
import numpy as np
import mindspore
from mindspore import nn, ops
from mindspore.nn import CrossEntropyLoss, LayerNorm
from mindspore import log as logger
from mindspore import Parameter, Tensor


from mindnlp.abc import PreTrainedModel
from mindnlp.generation.logits_process import LogitsProcessor, LogitsProcessorList
from mindnlp.generation.stopping_criteria import StoppingCriteriaList
from mindnlp.abc import GenerationConfig
from mindnlp.modules import functional as F
from mindnlp.configs import MINDNLP_MODEL_URL_BASE
from .chatglm_config import ChatGLMConfig

PRETRAINED_MODEL_ARCHIVE_MAP = {
    'chatglm-6b': MINDNLP_MODEL_URL_BASE.format('glm', 'chatglm-6b')
}


def torch_to_mindspore(pth_file, **kwargs):
    """convert torch checkpoint to mindspore"""
    _ = kwargs.get('prefix', '')

    ms_ckpt_path = re.sub(r'pytorch_model(.*).bin', r'mindspore\1.ckpt', pth_file)
    if os.path.exists(ms_ckpt_path):
        return ms_ckpt_path

    try:
        import torch
    except Exception as exc:
        raise ImportError("'import torch' failed, please install torch by "
                          "`pip install torch` or instructions from 'https://pytorch.org'") \
                          from exc

    from mindspore.train.serialization import save_checkpoint

    logger.info('Starting checkpoint conversion.')
    ms_ckpt = []
    state_dict = torch.load(pth_file, map_location=torch.device('cpu'))

    for key, value in state_dict.items():
        if 'layernorm' in key:
            if '.weight' in key:
                key = key.replace('.weight', '.gamma')
            if '.bias' in key:
                key = key.replace('.bias', '.beta')
        if 'embeddings' in key:
            key = key.replace('weight', 'embedding_table')
        ms_ckpt.append({'name': key, 'data': Tensor(value.numpy())})

    try:
        save_checkpoint(ms_ckpt, ms_ckpt_path)
    except Exception as exc:
        raise RuntimeError(f'Save checkpoint to {ms_ckpt_path} failed, '
                            f'please checkout the path.') from exc

    return ms_ckpt_path

class InvalidScoreLogitsProcessor(LogitsProcessor):
    """Invalid Score Processer."""
    def __call__(self, input_ids: mindspore.Tensor, scores: mindspore.Tensor) -> mindspore.Tensor:
        if ops.isnan(scores).any() or ops.isinf(scores).any():
            scores = ops.zeros_like(scores)
            scores[..., 5] = 5e4
        return scores


class PrefixEncoder(nn.Cell):
    """
    The model to encode the prefix
    Input shape: (batch-size, prefix-length)
    Output shape: (batch-size, prefix-length, 2*layers*hidden)
    """

    def __init__(self, config):
        super().__init__()
        self.prefix_projection = config.prefix_projection
        if self.prefix_projection:
            # Use a two-layer MLP to encode the prefix
            self.embedding = nn.Embedding(config.pre_seq_len, config.hidden_size)
            self.trans = nn.SequentialCell(
                nn.Dense(config.hidden_size, config.hidden_size),
                nn.Tanh(),
                nn.Dense(config.hidden_size, config.num_layers * config.hidden_size * 2)
            )
        else:
            self.embedding = nn.Embedding(config.pre_seq_len, config.num_layers * config.hidden_size * 2)

    def construct(self, prefix: mindspore.Tensor):
        if self.prefix_projection:
            prefix_tokens = self.embedding(prefix)
            past_key_values = self.trans(prefix_tokens)
        else:
            past_key_values = self.embedding(prefix)
        return past_key_values


class RotaryEmbedding(nn.Cell):
    """Rotary Embedding."""
    def __init__(self, dim, base=10000, precision=mindspore.float16, learnable=False):
        super().__init__()
        inv_freq = 1. / (base ** (np.arange(0, dim, 2) / dim))
        inv_freq = Tensor(inv_freq, mindspore.float16)
        self.learnable = learnable
        if learnable:
            self.inv_freq = Parameter(inv_freq, 'inv_freq')
            self.max_seq_len_cached = None
        else:
            self.inv_freq = Parameter(inv_freq, 'inv_freq', requires_grad=False)
            self.max_seq_len_cached = None
            self.cos_cached = None
            self.sin_cached = None
        self.precision = precision

    def construct(self, x, seq_dim=1, seq_len=None):
        if seq_len is None:
            seq_len = x.shape[seq_dim]
        if self.max_seq_len_cached is None or (seq_len > self.max_seq_len_cached):
            self.max_seq_len_cached = None if self.learnable else seq_len
            t = ops.arange(seq_len, dtype=self.inv_freq.dtype)
            # freqs = ops.einsum('i,j->ij', t, self.inv_freq)
            freqs = ops.matmul(t.expand_dims(-1), self.inv_freq.expand_dims(0))
            # Different from paper, but it uses a different permutation in order to obtain the same calculation
            emb = ops.cat((freqs, freqs), axis=-1)

            # [sx, 1 (b * np), hn]
            cos_cached = ops.cos(emb)[:, None, :]
            sin_cached = ops.sin(emb)[:, None, :]
            if self.learnable:
                return cos_cached, sin_cached
            self.cos_cached, self.sin_cached = cos_cached, sin_cached
        return self.cos_cached[:seq_len, ...], self.sin_cached[:seq_len, ...]

    def _apply(self, fn):
        if self.cos_cached is not None:
            self.cos_cached = fn(self.cos_cached)
        if self.sin_cached is not None:
            self.sin_cached = fn(self.sin_cached)
        return super()._apply(fn)


def rotate_half(x):
    """rotate half tensor."""
    x1, x2 = x[..., :x.shape[-1] // 2], x[..., x.shape[-1] // 2:]
    return ops.cat((-x2, x1), axis=x1.ndim - 1)  # dim=-1 triggers a bug in earlier torch versions


def apply_rotary_pos_emb_index(q, k, cos, sin, position_id):
    """apply rotary pos"""
    # position_id: [sq, b], q, k: [sq, b, np, hn], cos: [sq, 1, hn] -> [sq, b, 1, hn]
    cos, sin = F.embedding(position_id, cos.squeeze(1)).unsqueeze(2), \
        F.embedding(position_id, sin.squeeze(1)).unsqueeze(2)

    q, k = (q * cos) + (rotate_half(q) * sin), (k * cos) + (rotate_half(k) * sin)
    return q, k


def attention_fn(
        self,
        query_layer,
        key_layer,
        value_layer,
        attention_mask,
        hidden_size_per_partition,
        layer_id,
        layer_past=None,
        scaling_attention_score=True,
        use_cache=False,
):
    """attention function."""
    if layer_past is not None:
        past_key, past_value = layer_past[0], layer_past[1]
        key_layer = ops.cat((past_key, key_layer), axis=0)
        value_layer = ops.cat((past_value, value_layer), axis=0)

    # seqlen, batch, num_attention_heads, hidden_size_per_attention_head
    hidden_size = key_layer.shape[-1]

    if use_cache:
        present = (key_layer, value_layer)
    else:
        present = None

    query_key_layer_scaling_coeff = float(layer_id + 1)
    # print(query_key_layer_scaling_coeff)
    if scaling_attention_score:
        query_layer = query_layer / (math.sqrt(hidden_size) * query_key_layer_scaling_coeff)

    # ===================================
    # Raw attention scores. [b, np, s, s]
    # ===================================

    # [b, np, sq, sk]
    output_size = (query_layer.shape[1], query_layer.shape[2], query_layer.shape[0], key_layer.shape[0])

    # [sq, b, np, hn] -> [sq, b * np, hn]
    query_layer = query_layer.view((output_size[2], output_size[0] * output_size[1], -1))
    # [sk, b, np, hn] -> [sk, b * np, hn]
    key_layer = key_layer.view((output_size[3], output_size[0] * output_size[1], -1))

    matmul_result = ops.zeros(
        (1, 1, 1),
        dtype=query_layer.dtype
    )

    matmul_result = ops.baddbmm(
        matmul_result,
        query_layer.swapaxes(0, 1),  # [b * np, sq, hn]
        key_layer.transpose(1, 2, 0),  # [b * np, hn, sk]
        beta=0.0,
        alpha=1.0,
    )

    # change view to [b, np, sq, sk]
    attention_scores = matmul_result.view(output_size)

    if self.scale_mask_softmax:
        self.scale_mask_softmax.scale = query_key_layer_scaling_coeff
        attention_probs = self.scale_mask_softmax(attention_scores, attention_mask)
    else:
        if not (attention_mask == 0).all():
            # if auto-regressive, skip
            attention_scores = attention_scores.masked_fill(attention_mask, -10000.0)
        dtype = attention_scores.dtype
        attention_scores = attention_scores.float()
        attention_scores = attention_scores * query_key_layer_scaling_coeff

        attention_probs = ops.softmax(attention_scores, axis=-1)

        attention_probs = attention_probs.astype(dtype)

    # =========================
    # Context layer. [sq, b, hp]
    # =========================

    # value_layer -> context layer.
    # [sk, b, np, hn] --> [b, np, sq, hn]

    # context layer shape: [b, np, sq, hn]
    output_size = (value_layer.shape[1], value_layer.shape[2], query_layer.shape[0], value_layer.shape[3])

    # change view [sk, b * np, hn]
    value_layer = value_layer.view(value_layer.shape[0], output_size[0] * output_size[1], -1)

    # change view [b * np, sq, sk]
    attention_probs = attention_probs.view(output_size[0] * output_size[1], output_size[2], -1)

    # matmul: [b * np, sq, hn]
    context_layer = ops.bmm(attention_probs, value_layer.swapaxes(0, 1))

    # change view [b, np, sq, hn]
    context_layer = context_layer.view(output_size)

    # [b, np, sq, hn] --> [sq, b, np, hn]
    context_layer = context_layer.transpose(2, 0, 1, 3)

    # [sq, b, np, hn] --> [sq, b, hp]
    new_context_layer_shape = context_layer.shape[:-2] + (hidden_size_per_partition,)
    context_layer = context_layer.view(new_context_layer_shape)

    outputs = (context_layer, present, attention_probs)

    return outputs


def default_init(cls, *args, **kwargs):
    """default init"""
    return cls(*args, **kwargs)


class SelfAttention(nn.Cell):
    """Self Attention."""
    def __init__(self, hidden_size, num_attention_heads,
                 layer_id, hidden_size_per_attention_head=None, bias=True,
                 params_dtype=mindspore.float32, position_encoding_2d=True):

        super().__init__()

        self.layer_id = layer_id
        self.hidden_size = hidden_size
        self.hidden_size_per_partition = hidden_size
        self.num_attention_heads = num_attention_heads
        self.num_attention_heads_per_partition = num_attention_heads
        self.position_encoding_2d = position_encoding_2d
        self.rotary_emb = RotaryEmbedding(
            self.hidden_size // (self.num_attention_heads * 2)
            if position_encoding_2d
            else self.hidden_size // self.num_attention_heads,
            base=10000,
            precision=mindspore.float16,
            learnable=False,
        )

        self.scale_mask_softmax = None

        if hidden_size_per_attention_head is None:
            self.hidden_size_per_attention_head = hidden_size // num_attention_heads
        else:
            self.hidden_size_per_attention_head = hidden_size_per_attention_head

        self.inner_hidden_size = num_attention_heads * self.hidden_size_per_attention_head

        # Strided linear layer.
        self.query_key_value = nn.Dense(hidden_size, 3 * self.inner_hidden_size, has_bias=bias).to_float(params_dtype)
        self.dense = nn.Dense(self.inner_hidden_size, hidden_size, has_bias=bias).to_float(params_dtype)

    @staticmethod
    def attention_mask_func(attention_scores, attention_mask):
        """attention mask function"""
        return attention_scores.masked_fill(attention_mask, -10000.0)

    def split_tensor_along_last_dim(self, tensor, num_partitions):
        """Split a tensor along its last dimension.
        Arguments:
            tensor: input tensor.
            num_partitions: number of partitions to split the tensor
            contiguous_split_chunks: If True, make each chunk contiguous
                                    in memory.
        """
        # Get the size and dimension.
        last_dim = tensor.ndim - 1
        last_dim_size = tensor.shape[last_dim] // num_partitions
        # Split.
        tensor_list = ops.split(tensor, last_dim_size, axis=last_dim)
        # Note: torch.split does not create contiguous tensors by default.

        return tensor_list

    def construct(
            self,
            hidden_states: mindspore.Tensor,
            position_ids,
            attention_mask: mindspore.Tensor,
            layer_id,
            layer_past: Optional[Tuple[mindspore.Tensor, mindspore.Tensor]] = None,
            use_cache: bool = False,
            output_attentions: bool = False,
    ):
        """
        hidden_states: [seq_len, batch, hidden_size]
        attention_mask: [(1, 1), seq_len, seq_len]
        """

        # [seq_len, batch, 3 * hidden_size]
        mixed_raw_layer = self.query_key_value(hidden_states)
        # [seq_len, batch, 3 * hidden_size] --> [seq_len, batch, num_attention_heads, 3 * hidden_size_per_attention_head]
        new_tensor_shape = mixed_raw_layer.shape[:-1] + (
            self.num_attention_heads_per_partition,
            3 * self.hidden_size_per_attention_head,
        )
        mixed_raw_layer = mixed_raw_layer.view(new_tensor_shape)
        # [seq_len, batch, num_attention_heads, hidden_size_per_attention_head]
        (query_layer, key_layer, value_layer) = self.split_tensor_along_last_dim(mixed_raw_layer, 3)

        if self.position_encoding_2d:
            q1, q2 = query_layer.chunk(2, axis=(query_layer.ndim - 1))
            k1, k2 = key_layer.chunk(2, axis=(key_layer.ndim - 1))
            # return (k1,)
            cos, sin = self.rotary_emb(q1, seq_len=position_ids.max() + 1)
            position_ids, block_position_ids = position_ids[:, 0, :].swapaxes(0, 1), \
                position_ids[:, 1, :].swapaxes(0, 1)
            q1, k1 = apply_rotary_pos_emb_index(q1, k1, cos, sin, position_ids)
            q2, k2 = apply_rotary_pos_emb_index(q2, k2, cos, sin, block_position_ids)
            query_layer = ops.concat([q1, q2], axis=(q1.ndim - 1))
            key_layer = ops.concat([k1, k2], axis=(k1.ndim - 1))
        else:
            position_ids = position_ids.swapaxes(0, 1)
            cos, sin = self.rotary_emb(value_layer, seq_len=position_ids.max() + 1)
            # [seq_len, batch, num_attention_heads, hidden_size_per_attention_head]
            query_layer, key_layer = apply_rotary_pos_emb_index(query_layer, key_layer, cos, sin, position_ids)
        # [seq_len, batch, hidden_size]

        context_layer, present, attention_probs = attention_fn(
            self=self,
            query_layer=query_layer,
            key_layer=key_layer,
            value_layer=value_layer,
            attention_mask=attention_mask,
            hidden_size_per_partition=self.hidden_size_per_partition,
            layer_id=layer_id,
            layer_past=layer_past,
            use_cache=use_cache
        )

        output = self.dense(context_layer)

        outputs = (output, present)

        if output_attentions:
            outputs += (attention_probs,)

        return outputs  # output, present, attention_probs


def gelu(x):
    """OpenAI's gelu implementation."""
    return 0.5 * x * (1.0 + ops.tanh(0.7978845608028654 * x *
                                       (1.0 + 0.044715 * x * x)))

class GEGLU(nn.Cell):
    """GEGLU"""
    def __init__(self):
        super().__init__()
        self.activation_fn = gelu

    def construct(self, x):
        # dim=-1 breaks in jit for pt<1.10
        x1, x2 = x.chunk(2, axis=(x.ndim - 1))
        return x1 * self.activation_fn(x2)


class GLU(nn.Cell):
    """GLU"""
    def __init__(self, hidden_size, inner_hidden_size=None,
                 layer_id=None, bias=True, activation_func=gelu, params_dtype=mindspore.float32):
        super().__init__()
        self.layer_id = layer_id
        self.activation_func = activation_func

        # Project to 4h.
        self.hidden_size = hidden_size
        if inner_hidden_size is None:
            inner_hidden_size = 4 * hidden_size
        self.inner_hidden_size = inner_hidden_size
        self.dense_h_to_4h = nn.Dense(self.hidden_size, self.inner_hidden_size, has_bias=bias).to_float(params_dtype)

        # Project back to h.
        self.dense_4h_to_h = nn.Dense(self.inner_hidden_size, self.hidden_size, has_bias=bias).to_float(params_dtype)

    def construct(self, hidden_states):
        """
        hidden_states: [seq_len, batch, hidden_size]
        """

        # [seq_len, batch, inner_hidden_size]
        intermediate_parallel = self.dense_h_to_4h(hidden_states)

        intermediate_parallel = self.activation_func(intermediate_parallel)

        output = self.dense_4h_to_h(intermediate_parallel)

        return output


class GLMBlock(nn.Cell):
    """GLM Block."""
    def __init__(
            self,
            hidden_size,
            num_attention_heads,
            layernorm_epsilon,
            layer_id,
            inner_hidden_size=None,
            hidden_size_per_attention_head=None,
            use_bias=True,
            params_dtype=mindspore.float32,
            num_layers=28,
            position_encoding_2d=True,
    ):
        super().__init__()
        # Set output layer initialization if not provided.

        self.layer_id = layer_id

        # Layernorm on the input data.
        self.input_layernorm = nn.LayerNorm([hidden_size], epsilon=layernorm_epsilon)

        self.position_encoding_2d = position_encoding_2d

        # Self attention.
        self.attention = SelfAttention(
            hidden_size,
            num_attention_heads,
            layer_id,
            hidden_size_per_attention_head=hidden_size_per_attention_head,
            bias=use_bias,
            params_dtype=params_dtype,
            position_encoding_2d=self.position_encoding_2d,
        )

        # Layernorm on the input data.
        self.post_attention_layernorm = nn.LayerNorm([hidden_size], epsilon=layernorm_epsilon)

        self.num_layers = num_layers

        # GLU
        self.mlp = GLU(
            hidden_size,
            inner_hidden_size=inner_hidden_size,
            bias=use_bias,
            layer_id=layer_id,
            params_dtype=params_dtype,
        )

    def construct(
            self,
            hidden_states: mindspore.Tensor,
            position_ids,
            attention_mask: mindspore.Tensor,
            layer_id,
            layer_past: Optional[Tuple[mindspore.Tensor, mindspore.Tensor]] = None,
            use_cache: bool = False,
            output_attentions: bool = False,
    ):
        """
        hidden_states: [seq_len, batch, hidden_size]
        attention_mask: [(1, 1), seq_len, seq_len]
        """

        # Layer norm at the begining of the transformer layer.
        # [seq_len, batch, hidden_size]
        attention_input = self.input_layernorm(hidden_states)

        # Self attention.
        attention_outputs = self.attention(
            attention_input,
            position_ids,
            attention_mask=attention_mask,
            layer_id=layer_id,
            layer_past=layer_past,
            use_cache=use_cache,
            output_attentions=output_attentions
        )
        # return attention_outputs

        attention_output = attention_outputs[0]

        outputs = attention_outputs[1:]

        # Residual connection.
        alpha = (2 * self.num_layers) ** 0.5
        hidden_states = attention_input * alpha + attention_output

        mlp_input = self.post_attention_layernorm(hidden_states)

        # MLP.
        mlp_output = self.mlp(mlp_input)

        # Second residual connection.
        output = mlp_input * alpha + mlp_output

        if use_cache:
            outputs = (output,) + outputs
        else:
            outputs = (output,) + outputs[1:]

        return outputs  # hidden_states, present, attentions


class ChatGLMPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and
    a simple interface for downloading and loading pretrained models.
    """

    is_parallelizable = False
    supports_gradient_checkpointing = True
    config_class = ChatGLMConfig
    base_model_prefix = "transformer"
    _no_split_modules = ["GLMBlock"]
    pretrained_model_archive_map = PRETRAINED_MODEL_ARCHIVE_MAP
    convert_torch_to_mindspore = torch_to_mindspore

    def _init_weights(self, cell: nn.Cell):
        """Initialize the weights."""

    def get_masks(self, input_ids):
        """get masks"""
        batch_size, seq_length = input_ids.shape
        context_lengths = [seq.asnumpy().tolist().index(self.config.bos_token_id) for seq in input_ids]
        attention_mask = ops.ones((batch_size, seq_length, seq_length))
        attention_mask = attention_mask.tril()
        for i, context_length in enumerate(context_lengths):
            attention_mask[i, :, :context_length] = 1
        attention_mask = attention_mask.unsqueeze(1)
        attention_mask = (attention_mask < 0.5).bool()

        return attention_mask

    def get_position_ids(self, input_ids, mask_positions, use_gmasks=None):
        """get position ids"""
        batch_size, seq_length = input_ids.shape
        if use_gmasks is None:
            use_gmasks = [False] * batch_size
        context_lengths = [seq.asnumpy().tolist().index(self.config.bos_token_id) for seq in input_ids]
        if self.position_encoding_2d:
            position_ids = ops.arange(seq_length, dtype=mindspore.int32).unsqueeze(0).tile((batch_size, 1))
            for i, context_length in enumerate(context_lengths):
                position_ids[i, context_length:] = mask_positions[i]
            block_position_ids = [ops.cat((
                ops.zeros(context_length, dtype=mindspore.int32),
                ops.arange(seq_length - context_length, dtype=mindspore.int32) + 1
            )) for context_length in context_lengths]
            block_position_ids = ops.stack(block_position_ids, axis=0)
            position_ids = ops.stack((position_ids, block_position_ids), axis=1)
        else:
            position_ids = ops.arange(seq_length, dtype=mindspore.int32).unsqueeze(0).tile((batch_size, 1))
            for i, context_length in enumerate(context_lengths):
                if not use_gmasks[i]:
                    position_ids[i, context_length:] = mask_positions[i]

        return position_ids


class ChatGLMModel(ChatGLMPreTrainedModel):
    """

    The model can behave as an encoder (with only self-attention) as well
    as a decoder, in which case a layer of cross-attention is added between
    the self-attention layers, following the architecture described in [Attention is
    all you need](https://arxiv.org/abs/1706.03762) by Ashish Vaswani,
    Noam Shazeer, Niki Parmar, Jakob Uszkoreit, Llion Jones, Aidan N. Gomez, Lukasz Kaiser and Illia Polosukhin.

    To behave as an decoder the model needs to be initialized with the
    `is_decoder` argument of the configuration set to `True`.
    To be used in a Seq2Seq model, the model needs to initialized with both `is_decoder`
    argument and `add_cross_attention` set to `True`; an
    `encoder_hidden_states` is then expected as an input to the forward pass.
    """

    def __init__(self, config: ChatGLMConfig):
        super().__init__(config)
        # recording parameters
        self.max_sequence_length = config.max_sequence_length
        self.hidden_size = config.hidden_size
        self.params_dtype = mindspore.float16
        self.num_attention_heads = config.num_attention_heads
        self.vocab_size = config.vocab_size
        self.num_layers = config.num_layers
        self.layernorm_epsilon = config.layernorm_epsilon
        self.inner_hidden_size = config.inner_hidden_size
        self.hidden_size_per_attention_head = self.hidden_size // self.num_attention_heads
        self.position_encoding_2d = config.position_encoding_2d
        self.pre_seq_len = config.pre_seq_len
        self.prefix_projection = config.prefix_projection

        self.word_embeddings = nn.Embedding(
            vocab_size=self.vocab_size, embedding_size=self.hidden_size).to_float(self.params_dtype)

        def get_layer(layer_id):
            return GLMBlock(
                self.hidden_size,
                self.num_attention_heads,
                self.layernorm_epsilon,
                layer_id,
                inner_hidden_size=self.inner_hidden_size,
                hidden_size_per_attention_head=self.hidden_size_per_attention_head,
                use_bias=True,
                params_dtype=self.params_dtype,
                position_encoding_2d=self.position_encoding_2d,
            )

        self.layers = nn.CellList(
            [get_layer(layer_id) for layer_id in range(self.num_layers)]
        )
        # Final layer norm before output.
        self.final_layernorm = LayerNorm([self.hidden_size], epsilon=self.layernorm_epsilon)

        if self.pre_seq_len is not None:
            # for param in self.parameters():
            #     param.requires_grad = False
            self.prefix_tokens = Tensor(np.arange(self.pre_seq_len))
            self.prefix_encoder = PrefixEncoder(config)
            self.dropout = nn.Dropout(p=0.1)

            # total_params = sum(p.numel() for p in self.parameters())
            # trainable_params = sum(p.numel() for p in self.parameters() if p.requires_grad)
            # print("Using p-tuning v2: # trainable_params = {} / {}".format(trainable_params, total_params))

    def get_input_embeddings(self):
        return self.word_embeddings

    def set_input_embeddings(self, new_embeddings: mindspore.Tensor):
        self.word_embeddings = new_embeddings

    def get_prompt(self, batch_size, dtype=mindspore.float16):
        """get prompt."""
        prefix_tokens = self.prefix_tokens.unsqueeze(0).expand(batch_size, -1)
        past_key_values = self.prefix_encoder(prefix_tokens).type(dtype)
        past_key_values = past_key_values.view(
            batch_size,
            self.pre_seq_len,
            self.num_layers * 2,
            self.num_attention_heads,
            self.hidden_size // self.num_attention_heads
        )
        # seq_len, b, nh, hidden_size
        past_key_values = self.dropout(past_key_values)
        past_key_values = past_key_values.permute([2, 1, 0, 3, 4]).split(2)
        # past_key_values = [(v[0], v[1]) for v in past_key_values]
        return past_key_values


    def construct(
            self,
            input_ids: Optional[mindspore.Tensor] = None,
            position_ids: Optional[mindspore.Tensor] = None,
            attention_mask: Optional[mindspore.Tensor] = None,
            past_key_values: Optional[Tuple[Tuple[mindspore.Tensor, mindspore.Tensor], ...]] = None,
            inputs_embeds: Optional[mindspore.Tensor] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
    ) -> Tuple[mindspore.Tensor, ...]:

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        if input_ids is not None:
            batch_size, _ = input_ids.shape[:2]
        elif inputs_embeds is not None:
            batch_size, _ = inputs_embeds.shape[:2]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_ids)

        # print(inputs_embeds.dtype)
        # return (inputs_embeds,)

        if past_key_values is None:
            if self.pre_seq_len is not None:
                past_key_values = self.get_prompt(batch_size=input_ids.shape[0],
                                                  dtype=inputs_embeds.dtype)
            else:
                past_key_values = tuple([None] * len(self.layers))

            if attention_mask is None:
                attention_mask = self.get_masks(
                    input_ids,
                )


            if position_ids is None:
                MASK, gMASK = self.config.mask_token_id, self.config.gmask_token_id
                seqs = input_ids.asnumpy().tolist()

                mask_positions, use_gmasks = [], []
                for seq in seqs:
                    mask_token = gMASK if gMASK in seq else MASK
                    use_gmask = mask_token == gMASK
                    mask_positions.append(seq.index(mask_token))
                    use_gmasks.append(use_gmask)

                position_ids = self.get_position_ids(
                    input_ids,
                    mask_positions=mask_positions,
                    use_gmasks=use_gmasks
                )

        if self.pre_seq_len is not None and attention_mask is not None:
            prefix_attention_mask = ops.ones((batch_size, 1, input_ids.shape[-1], self.pre_seq_len))
            prefix_attention_mask = (prefix_attention_mask < 0.5).bool()
            attention_mask = ops.cat((prefix_attention_mask, attention_mask), axis=3)

        # [seq_len, batch, hidden_size]
        hidden_states = inputs_embeds.swapaxes(0, 1)

        presents = () if use_cache else None
        all_self_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None

        if attention_mask is None:
            attention_mask = ops.zeros(1, 1).bool()

        for i, layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)
            layer_past = past_key_values[i]

            layer_ret = layer(
                hidden_states,
                position_ids=position_ids,
                attention_mask=attention_mask,
                layer_id=mindspore.Tensor(i),
                layer_past=layer_past,
                use_cache=use_cache,
                output_attentions=output_attentions
            )
            hidden_states = layer_ret[0]

            # if i == 0:
            #     return (hidden_states,)

            if use_cache:
                presents = presents + (layer_ret[1],)

            if output_attentions:
                all_self_attentions = all_self_attentions + (layer_ret[2 if use_cache else 1],)

        # Final layer norm.
        # return (hidden_states,)
        hidden_states = self.final_layernorm(hidden_states)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        return tuple(v for v in [hidden_states, presents, all_hidden_states, all_self_attentions] if v is not None)


class ChatGLMForConditionalGeneration(ChatGLMPreTrainedModel):
    """ChatGLMForConditionalGeneration"""
    def __init__(self, config: ChatGLMConfig):
        super().__init__(config)

        self.max_sequence_length = config.max_sequence_length
        self.position_encoding_2d = config.position_encoding_2d
        self.transformer = ChatGLMModel(config)
        self.lm_head = nn.Dense(config.hidden_size, config.vocab_size, has_bias=False).to_float(mindspore.float16)

        self.config = config

        self.quantized = False

        if self.config.quantization_bit:
            self.quantize(self.config.quantization_bit, empty_init=True)

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def _update_model_kwargs_for_generation(
        self,
        outputs,
        model_kwargs: Dict[str, Any],
        is_encoder_decoder: bool = False,
        standardize_cache_format: bool = False,
    ) -> Dict[str, Any]:
        # update past_key_values
        model_kwargs["past_key_values"] = self._extract_past_from_model_output(
            outputs, standardize_cache_format=standardize_cache_format
        )

        # update attention mask
        if "attention_mask" in model_kwargs:
            attention_mask = model_kwargs["attention_mask"]
            if attention_mask is not None and attention_mask.dtype == mindspore.bool_:
                attention_mask = ops.cat(
                    [attention_mask, attention_mask.new_ones((*attention_mask.shape[:3], 1))], axis=3)
                new_attention_mask = attention_mask[:, :, -1:].clone()
                new_attention_mask[..., -1] = False
                model_kwargs["attention_mask"] = ops.cat(
                    [attention_mask, new_attention_mask], axis=2
                )

        # update position ids
        if "position_ids" in model_kwargs:
            position_ids = model_kwargs["position_ids"]
            new_position_id = position_ids[..., -1:].clone()
            new_position_id[:, 1, :] += 1
            model_kwargs["position_ids"] = ops.cat(
                [position_ids, new_position_id], axis=-1
            )

        return model_kwargs

    def prepare_inputs_for_generation(
            self,
            input_ids: mindspore.Tensor,
            past: Optional[mindspore.Tensor] = None,
            past_key_values: Optional[mindspore.Tensor] = None,
            attention_mask: Optional[mindspore.Tensor] = None,
            position_ids: Optional[mindspore.Tensor] = None,
            **kwargs
    ) -> dict:
        _, seq_length = input_ids.shape
        MASK, gMASK = self.config.mask_token_id, self.config.gmask_token_id
        seqs = input_ids.asnumpy().tolist()
        mask_positions, use_gmasks = [], []
        for seq in seqs:
            mask_token = gMASK if gMASK in seq else MASK
            use_gmask = mask_token == gMASK
            mask_positions.append(seq.index(mask_token))
            use_gmasks.append(use_gmask)

        # only last token for input_ids if past is not None
        if past is not None or past_key_values is not None:
            last_token = input_ids[:, -1].unsqueeze(-1)
            if attention_mask is not None and attention_mask.dtype == mindspore.bool_:
                attention_mask = attention_mask[:, :, -1:]
            else:
                attention_mask = None
            if position_ids is not None:
                position_ids = position_ids[..., -1:]
            else:
                context_lengths = [seq.index(self.config.bos_token_id) for seq in seqs]
                if self.position_encoding_2d:
                    position_ids = mindspore.Tensor(
                        [[mask_position, seq_length - context_length] for mask_position, context_length in
                         zip(mask_positions, context_lengths)], dtype=mindspore.int32).unsqueeze(-1)
                else:
                    position_ids = mindspore.Tensor([mask_position for mask_position in mask_positions], dtype=mindspore.int32).unsqueeze(-1)

            if past is None:
                past = past_key_values
            return {
                "input_ids": last_token,
                "past_key_values": past,
                "position_ids": position_ids,
                "attention_mask": attention_mask
            }
        if attention_mask is not None and attention_mask.dtype != mindspore.bool_:
            attention_mask = None
        if attention_mask is None:
            attention_mask = self.get_masks(input_ids)
        if position_ids is None:
            position_ids = self.get_position_ids(input_ids, mask_positions=mask_positions, use_gmasks=use_gmasks)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "position_ids": position_ids,
            "attention_mask": attention_mask
        }

    def construct(
            self,
            input_ids: Optional[mindspore.Tensor] = None,
            position_ids: Optional[mindspore.Tensor] = None,
            attention_mask: Optional[mindspore.Tensor] = None,
            past_key_values: Optional[Tuple[mindspore.Tensor]] = None,
            inputs_embeds: Optional[mindspore.Tensor] = None,
            labels: Optional[mindspore.Tensor] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
    ):
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids=input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )


        hidden_states = transformer_outputs[0]

        # return (hidden_states,)
        lm_logits = self.lm_head(hidden_states).permute(1, 0, 2)

        loss = None
        if labels is not None:
            lm_logits = lm_logits.to(mindspore.float32)

            # Shift so that tokens < n predifct n
            shift_logits = lm_logits[..., :-1, :]
            shift_labels = labels[..., 1:]
            # Flatten the tokens
            loss_fct = CrossEntropyLoss(ignore_index=-100)
            loss = loss_fct(shift_logits.view(-1, shift_logits.shape[-1]), shift_labels.view(-1))

            lm_logits = lm_logits.to(hidden_states.dtype)
            loss = loss.to(hidden_states.dtype)

        output = (lm_logits,) + transformer_outputs[1:]
        return ((loss,) + output) if loss is not None else output


    @staticmethod
    def _reorder_cache(
            past: Tuple[Tuple[mindspore.Tensor, mindspore.Tensor], ...], beam_idx: mindspore.Tensor
    ) -> Tuple[Tuple[mindspore.Tensor, mindspore.Tensor], ...]:
        """
        This function is used to re-order the `past_key_values` cache if [`~PreTrainedModel.beam_search`] or
        [`~PreTrainedModel.beam_sample`] is called. This is required to match `past_key_values` with the correct
        beam_idx at every generation step.

        Output shares the same memory storage as `past`.
        """
        return tuple(
            (
                layer_past[0].index_select(1, beam_idx),
                layer_past[1].index_select(1, beam_idx),
            )
            for layer_past in past
        )

    def process_response(self, response):
        """process response."""
        response = response.strip()
        response = response.replace("[[训练时间]]", "2023年")
        punkts = [
            [",", "，"],
            ["!", "！"],
            [":", "："],
            [";", "；"],
            ["\?", "？"],
        ]
        for item in punkts:
            response = re.sub(r"([\u4e00-\u9fff])%s" % item[0], r"\1%s" % item[1], response)
            response = re.sub(r"%s([\u4e00-\u9fff])" % item[0], r"%s\1" % item[1], response)
        return response

    def chat(self, tokenizer, query: str, history: List[Tuple[str, str]] = None, max_length: int = 2048, num_beams=1,
             do_sample=True, top_p=0.7, temperature=0.95, logits_processor=None, **kwargs):
        """chat."""
        if history is None:
            history = []
        if logits_processor is None:
            logits_processor = LogitsProcessorList()
        logits_processor.append(InvalidScoreLogitsProcessor())
        gen_kwargs = {"max_length": max_length, "num_beams": num_beams, "do_sample": do_sample, "top_p": top_p,
                      "temperature": temperature, "logits_processor": logits_processor, **kwargs}
        if not history:
            prompt = query
        else:
            prompt = ""
            for i, (old_query, response) in enumerate(history):
                prompt += "[Round {}]\n问：{}\n答：{}\n".format(i, old_query, response)
            prompt += "[Round {}]\n问：{}\n答：".format(len(history), query)
        inputs = tokenizer([prompt], return_tensors="pt")
        outputs = self.generate(**inputs, **gen_kwargs)
        outputs = outputs.tolist()[0][len(inputs["input_ids"][0]):]
        response = tokenizer.decode(outputs)
        response = self.process_response(response)
        history = history + [(query, response)]
        return response, history

    def stream_chat(self, tokenizer, query: str, history: List[Tuple[str, str]] = None, max_length: int = 2048,
                    do_sample=True, top_p=0.7, temperature=0.95, logits_processor=None, **kwargs):
        """stream chat"""
        if history is None:
            history = []
        if logits_processor is None:
            logits_processor = LogitsProcessorList()
        logits_processor.append(InvalidScoreLogitsProcessor())
        gen_kwargs = {"max_length": max_length, "do_sample": do_sample, "top_p": top_p,
                      "temperature": temperature, "logits_processor": logits_processor, **kwargs}
        if not history:
            prompt = query
        else:
            prompt = ""
            for i, (old_query, response) in enumerate(history):
                prompt += "[Round {}]\n问：{}\n答：{}\n".format(i, old_query, response)
            prompt += "[Round {}]\n问：{}\n答：".format(len(history), query)
        inputs = tokenizer([prompt], return_tensors="pt")
        for outputs in self.stream_generate(**inputs, **gen_kwargs):
            outputs = outputs.tolist()[0][len(inputs["input_ids"][0]):]
            response = tokenizer.decode(outputs)
            response = self.process_response(response)
            new_history = history + [(query, response)]
            yield response, new_history

    def stream_generate(
            self,
            input_ids,
            generation_config: Optional[GenerationConfig] = None,
            logits_processor: Optional[LogitsProcessorList] = None,
            stopping_criteria: Optional[StoppingCriteriaList] = None,
            prefix_allowed_tokens_fn: Optional[Callable[[int, mindspore.Tensor], List[int]]] = None,
            **kwargs,
    ):
        """stream generate"""
        _, input_ids_seq_length = input_ids.shape[0], input_ids.shape[-1]

        if generation_config is None:
            generation_config = self.generation_config
        generation_config = copy.deepcopy(generation_config)
        model_kwargs = generation_config.update(**kwargs)
        _, eos_token_id = generation_config.bos_token_id, generation_config.eos_token_id

        if isinstance(eos_token_id, int):
            eos_token_id = [eos_token_id]

        has_default_max_length = kwargs.get("max_length") is None and generation_config.max_length is not None
        if has_default_max_length and generation_config.max_new_tokens is None:
            warnings.warn(
                f"Using `max_length`'s default ({generation_config.max_length}) to control the generation length. "
                "This behaviour is deprecated and will be removed from the config in v5 of Transformers -- we"
                " recommend using `max_new_tokens` to control the maximum length of the generation.",
                UserWarning,
            )
        elif generation_config.max_new_tokens is not None:
            generation_config.max_length = generation_config.max_new_tokens + input_ids_seq_length
            if not has_default_max_length:
                logger.warn(
                    f"Both `max_new_tokens` (={generation_config.max_new_tokens}) and `max_length`(="
                    f"{generation_config.max_length}) seem to have been set. `max_new_tokens` will take precedence. "
                    "Please refer to the documentation for more information. "
                    "(https://huggingface.co/docs/transformers/main/en/main_classes/text_generation)",
                    UserWarning,
                )

        if input_ids_seq_length >= generation_config.max_length:
            input_ids_string = "decoder_input_ids" if self.config.is_encoder_decoder else "input_ids"
            logger.warning(
                f"Input length of {input_ids_string} is {input_ids_seq_length}, but `max_length` is set to"
                f" {generation_config.max_length}. This can lead to unexpected behavior. You should consider"
                " increasing `max_new_tokens`."
            )

        # 2. Set generation parameters if not already defined
        logits_processor = logits_processor if logits_processor is not None else LogitsProcessorList()
        stopping_criteria = stopping_criteria if stopping_criteria is not None else StoppingCriteriaList()

        logits_processor = self._get_logits_processor(
            generation_config=generation_config,
            input_ids_seq_length=input_ids_seq_length,
            encoder_input_ids=input_ids,
            prefix_allowed_tokens_fn=prefix_allowed_tokens_fn,
            logits_processor=logits_processor,
        )

        stopping_criteria = self._get_stopping_criteria(
            generation_config=generation_config, stopping_criteria=stopping_criteria
        )
        logits_warper = self._get_logits_warper(generation_config)

        unfinished_sequences = input_ids.new(input_ids.shape[0]).fill(1)
        scores = None

        while True:
            model_inputs = self.prepare_inputs_for_generation(input_ids, **model_kwargs)
            # forward pass to get next token
            outputs = self(
                **model_inputs,
                return_dict=True,
                output_attentions=False,
                output_hidden_states=False,
            )

            next_token_logits = outputs.logits[:, -1, :]

            # pre-process distribution
            next_token_scores = logits_processor(input_ids, next_token_logits)
            next_token_scores = logits_warper(input_ids, next_token_scores)

            # sample
            probs = ops.softmax(next_token_scores, axis=-1)
            if generation_config.do_sample:
                next_tokens = ops.multinomial(probs, num_samples=1).squeeze(1)
            else:
                next_tokens = ops.argmax(probs, dim=-1)

            # update generated ids, model inputs, and length for next step
            input_ids = ops.cat([input_ids, next_tokens[:, None]], axis=-1)
            model_kwargs = self._update_model_kwargs_for_generation(
                outputs, model_kwargs, is_encoder_decoder=self.config.is_encoder_decoder
            )
            unfinished_sequences = unfinished_sequences.mul((sum(next_tokens != i for i in eos_token_id)).long())

            # stop when each sentence is finished, or if we exceed the maximum length
            if unfinished_sequences.max() == 0 or stopping_criteria(input_ids, scores):
                break
            yield input_ids

    def quantize(self, bits: int, empty_init=False, **kwargs):
        """TODO: support quantize"""
