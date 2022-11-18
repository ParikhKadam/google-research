# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""1D weight stationary xmap layer."""

from typing import Sequence, Tuple

import jax
from jax import lax
import jax.numpy as jnp
import jax.scipy

from scaling_transformer_inference_efficiency import attention
from scaling_transformer_inference_efficiency import checkpoint
from scaling_transformer_inference_efficiency import collectives
from scaling_transformer_inference_efficiency import inference
from scaling_transformer_inference_efficiency import layers_parallel
from scaling_transformer_inference_efficiency import special2
from scaling_transformer_inference_efficiency import weights
from scaling_transformer_inference_efficiency.weights import Layer

HParams = checkpoint.HParams
CheckpointSpec = checkpoint.CheckpointSpec


# pylint: disable = protected-access
# pylint: disable = g-doc-return-or-yield
# pylint: disable = g-doc-args
# TODO(sholto): Update
def transformer_layer_weight_stationary_1d_weight_stationary(
    hparams, layer, params, sin,
    cos, kv_caches, x,
    x_axis, y_axis, z_axis,
    attn_all_to_all,
    latency_collectives):
  """Forward pass through a single layer, returning output, K, V.

  Partitioning:
  * 'x' is the longest axis (most chips), then 'y', then 'z'.
  * weights are sharded [dmodel, heads.XYZ, *_per_head]
  * hidden-dimension activations are sharded [batch, time, heads.XYZ,
  *_per_head]
  * embed-dimension activations are "naturally" sharded [batch, time, dmodel]
    but may be "oversharded" after reducescatter operations.

  To support XYZ>heads, we simply increase the number of heads, by padding. The
  FFN can be sharded finer and continues to gain speedup with more chips, but
  the ATTN will just be padded and not gain speedup as we add chips.
  """
  if latency_collectives:
    matmul_reducescatter = collectives.matmul_reducescatter_bidirectional_latency
    # reducescatter = collectives.reducescatter_bidirectional_latency
    matmul_allgather = collectives.async_matmul_allgather_latency
  else:
    matmul_reducescatter = collectives.matmul_reducescatter_bidirectional_throughput
    # reducescatter = collectives.reducescatter_bidirectional_throughput
    matmul_allgather = collectives.async_matmul_allgather_throughput

  def my_layer(t, axis=0):
    """Gets the parameters corresponding to a given layer."""
    return lax.dynamic_index_in_dim(t, layer, axis=axis, keepdims=False)

  batch, max_len, _ = x.shape
  batch_z = batch // z_axis
  batch_yz = batch_z // y_axis
  batch_xyz = batch_yz // x_axis

  # x_index = lax.axis_index('x')
  y_index = lax.axis_index('y')
  z_index = lax.axis_index('z')
  yz_index = y_index * z_axis + z_index

  if batch == 1 and max_len == 1:
    raise ValueError('sharded batch-1 matmul is broken on VLC, b/246436629')

  with jax.named_scope('layernorm'):
    # x: [batch, maxlen, dmodel.X]
    # mean2: [batch, maxlen]
    # xnorm: [batch, maxlen, dmodel.X]
    epsilon = 1e-6
    mean2 = lax.pmean(
        jnp.mean(lax.square(x), axis=-1, keepdims=True), axis_name='x')
    xnorm = jnp.bfloat16(x * lax.rsqrt(mean2 + epsilon))

  # einsum(xnorm, q_wi):
  # [batch, maxlen, dmodel.X] @ [heads.XYZ, dmodel, q_wi_per_head]
  # -> (allgather lhs)   (fused with matmul)
  # -> [batch, maxlen, dmodel]
  # -> (matmul)
  # -> [batch, maxlen, heads.XYZ, q_wi_per_head]
  with jax.named_scope('q_wi'):
    q_wi = matmul_allgather(
        'bte,hed->bthd',
        xnorm,
        params.q_wi,
        gather_dimension=(1, None),
        axis_name='x',
        layer=layer,
        subsplit_axis=2)

    layers_parallel.assert_equal(
        q_wi.shape, (batch, max_len, hparams.heads //
                     (x_axis * y_axis * z_axis), hparams.q_wi_per_head))

    if isinstance(params, weights.QuantizedLayer):
      prev_shape = q_wi.shape
      q_wi = jnp.bfloat16(q_wi * jnp.squeeze(my_layer(params.q_wi_scale)))
      layers_parallel.assert_equal(prev_shape, q_wi.shape)

    # unlike in https://arxiv.org/pdf/2002.05202.pdf, PaLM implements
    # swiGLU with full d_ff dimension, rather than 2/3 scaled
    wi0 = q_wi[:, :, :, hparams.qkv:hparams.qkv + (hparams.ff // hparams.heads)]
    wi1 = q_wi[:, :, :, hparams.qkv + (hparams.ff // hparams.heads):]

  # einsum(xnorm, kv):
  #
  # [batch, maxlen, dmodel.X] @ [dmodel.X, 1, 2*qkv]
  # -> (matmul)
  # -> [batch, maxlen, 1, 2*qkv]{x unreduced}
  # -> (reducescatter over x into batch)
  #         *NOT* collective matmul, because it's batch
  # -> { Attn.NONE:      [batch.B, maxlen,  1, 2*qkv]
  #    { Attn.AXIS_Z:    [batch.ZB, maxlen, 1, 2*qkv]
  #    { Attn.AXES_YZ:   [batch.YZB, maxlen, 1, 2*qkv]
  #    { Attn.AXES_YZX:  [batch.YZXB, maxlen, 1, 2*qkv]
  with jax.named_scope('kv'):

    def kv_einsum(lhs):
      return jnp.einsum('bte,ezd->btzd', lhs, my_layer(params.kv))

    # kv_unreduced = jnp.einsum('bte,ezd->btzd', xnorm,
    #                           my_layer(params.kv))

    if attn_all_to_all == layers_parallel.AttnAllToAll.NONE:
      # [batch, maxlen, 1, 2*qkv]{x_unreduced}
      # --ARx-->   [batch, maxlen, 1, 2*qkv]
      kv = lax.psum(kv_einsum(xnorm), 'x')
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXIS_Z:
      assert batch_z >= 1, ('Batch size too small for AXIS_Z and this chip '
                            'count')
      # xnorm: [batch, maxlen, dmodel.X] -> [batch.Z, maxlen, dmodel.X]
      xnorm = lax.dynamic_slice_in_dim(
          xnorm, z_index * batch_z, batch_z, axis=0)
      # [batch.Z, maxlen, dmodel.X] @ [dmodel.X, 1, 2*qkv]
      # --matmul--> [batch.Z, maxlen, 1, 2*qkv]{x unreduced}
      # --ARx-->    [batch.Z, maxlen, 1, 2*qkv]
      kv = lax.psum(kv_einsum(xnorm), 'x')
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXES_YZ:
      assert batch_yz >= 1, ('Batch size too small for AXES_YZ and this chip '
                             'count')
      # xnorm: [batch, maxlen, dmodel.X] -> [batch.YZ, maxlen, dmodel.X]
      xnorm = lax.dynamic_slice_in_dim(
          xnorm, yz_index * batch_yz, batch_yz, axis=0)
      # [batch.YZ, maxlen, dmodel.X] @ [dmodel.X, 1, 2*qkv]
      # --matmul--> [batch.YZ, maxlen, 1, 2*qkv]{x unreduced}
      # --ARx-->    [batch.YZ, maxlen, 1, 2*qkv]
      kv = lax.psum(kv_einsum(xnorm), 'x')
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXES_YZX:
      assert batch_xyz >= 1, ('Batch size too small for AXES_XYZ and this chip '
                              'count')
      # xnorm: [batch, maxlen, dmodel.X] -> [batch.YZ, maxlen, dmodel.X]
      xnorm = lax.dynamic_slice_in_dim(
          xnorm, yz_index * batch_yz, batch_yz, axis=0)
      # [batch.YZ, maxlen, dmodel.X] @ [dmodel.X, 1, 2*qkv]
      # --matmul--> [batch.YZ, maxlen, 1, 2*qkv]{x unreduced}
      # --RSx-->    [batch.YZ, maxlen, 1, 2*qkv]
      kv = lax.psum_scatter(
          kv_einsum(xnorm), 'x', scatter_dimension=0, tiled=True)

    if isinstance(params, inference.QuantizedLayer):
      prev_shape = kv.shape
      kv = jnp.bfloat16(kv * jnp.squeeze(my_layer(params.kv_scale)))
      layers_parallel.assert_equal(prev_shape, kv.shape)

    k = kv[:, :, 0, :hparams.qkv]
    v = kv[:, :, 0, hparams.qkv:]

  with jax.named_scope('attn'):
    k = inference._rope(sin, cos, k)

    # q: [batch, maxlen, heads.YZX, qkv]
    # -> { NONE:                   [batch,  maxlen, heads.YZX, qkv]
    #    { AXIS_Z:                 [batch.Z, maxlen, heads.YX, qkv]
    #    { AXES_YZ:                [batch.YZ, maxlen, heads.X, qkv]
    #    { AXES_YZX:               [batch.YZX, maxlen, heads,  qkv]
    q = q_wi[:, :, :, :hparams.qkv]
    if attn_all_to_all == layers_parallel.AttnAllToAll.NONE:
      pass
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXIS_Z:
      q = lax.all_to_all(
          q, axis_name='z', split_axis=0, concat_axis=2, tiled=True)
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXES_YZ:
      q = lax.all_to_all(
          q, axis_name=('y', 'z'), split_axis=0, concat_axis=2, tiled=True)
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXES_YZX:
      q = lax.all_to_all(
          q, axis_name='x', split_axis=0, concat_axis=2, tiled=True)
      q = lax.all_to_all(
          q, axis_name=('y', 'z'), split_axis=0, concat_axis=2, tiled=True)

    q = inference._rope(sin, cos, q)

    y_att = jnp.bfloat16(attention.attend(q, k, v, kv_caches, layer))
    # y_att:
    #    { NONE:                   [batch,  maxlen, heads.YZX, qkv]
    #    { AXIS_Z:                 [batch.Z, maxlen, heads.YX, qkv]
    #    { AXES_YZ:                [batch.YZ, maxlen, heads.X, qkv]
    #    { AXES_YZX:               [batch.YZX, maxlen, heads,  qkv]
    # -> [batch.B, maxlen, heads.YZX, qkv]
    if attn_all_to_all == layers_parallel.AttnAllToAll.NONE:
      pass
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXIS_Z:
      y_att = lax.all_to_all(
          y_att, axis_name='z', split_axis=2, concat_axis=0, tiled=True)
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXES_YZ:
      y_att = lax.all_to_all(
          y_att, axis_name=('y', 'z'), split_axis=2, concat_axis=0, tiled=True)
    elif attn_all_to_all == layers_parallel.AttnAllToAll.AXES_YZX:
      y_att = lax.all_to_all(
          y_att, axis_name=('y', 'z'), split_axis=2, concat_axis=0, tiled=True)
      y_att = lax.all_to_all(
          y_att, axis_name='x', split_axis=2, concat_axis=0, tiled=True)

  with jax.named_scope('SwiGLU'):
    y_mlp = special2.swish2(wi0) * wi1

  # einsum(y_fused, o_wo):
  # [batch, maxlen, heads.XYZ, o_wo_per_head]
  #   @ [heads.XYZ, o_wo_per_head, dmodel]
  # -> (matmul)
  # -> [batch, maxlen, dmodel]{XYZ unreduced}
  # -> (fused reducescatter over X)
  # -> [batch, maxlen, dmodel.X]{YZ unreduced}
  # -> (non-fused allreduce)
  # -> [batch, maxlen, dmodel.X]
  with jax.named_scope('o_wo'):
    y_fused = jnp.concatenate([y_att, y_mlp], axis=-1)
    layers_parallel.assert_equal(
        y_fused.shape, (batch, max_len, hparams.heads //
                        (x_axis * y_axis * z_axis), hparams.o_wo_per_head))

    y_out = matmul_reducescatter(
        'bthd,hde->bte',
        y_fused,
        params.o_wo,
        scatter_dimension=(2, 2),
        axis_name='x',
        layer=layer,
        subsplit_axis=2)

    y_out = lax.psum(y_out, axis_name=('y', 'z'))

    if isinstance(params, inference.QuantizedLayer):
      prev_shape = y_out.shape
      y_out = jnp.bfloat16(y_out * jnp.squeeze(my_layer(params.o_wo_scale)))
      layers_parallel.assert_equal(y_out.shape, prev_shape)

  with jax.named_scope('residual'):
    z = jnp.bfloat16(y_out + x)
  return z, k[:batch_xyz], v[:batch_xyz]
