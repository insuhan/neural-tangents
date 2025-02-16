# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for `neural_tangents/stax.py`."""


import functools
import itertools
import random as prandom
from typing import Tuple

from absl.testing import absltest
from absl.testing import parameterized
from jax import default_backend
from jax import jit
from jax import test_util as jtu
from jax.config import config
from jax.example_libraries import stax as ostax
import jax.numpy as np
import jax.random as random
import neural_tangents as nt
from neural_tangents import stax
from tests import test_utils
import numpy as onp


config.parse_flags_with_absl()
config.update('jax_numpy_rank_promotion', 'raise')


MODELS = [
    'fc',
    'conv'
]

BATCH_SIZE = 4

INPUT_SHAPE = (BATCH_SIZE, 8, 6, 2)

WIDTHS = [2**10]

N_SAMPLES = 100

RTOL = 0.041

ATOL = 0.1

FILTER_SHAPES = [
    (2, 1),
    (3, 2)
]

PADDINGS = [
    'SAME',
    'VALID',
    'CIRCULAR'
]

STRIDES = [
    (1, 2),
    (2, 1),
]

ACTIVATIONS = {
    stax.Relu(): 'Relu',
}

PROJECTIONS = [
    'FLAT',
    'POOL',
    'ATTN',
]

LAYER_NORM = [
    'C',
    'HC',
    'CHW',
    'NC',
    'NWC',
    'NCHW'
]

POOL_TYPES = [
    'SUM',
    'AVG'
]

PARAMETERIZATIONS = [
    'NTK',
    'STANDARD'
]

test_utils.update_test_tolerance()

prandom.seed(1)


def _get_inputs(
    key,
    same_inputs,
    shape,
    fn=np.cos
) -> Tuple[np.ndarray, np.ndarray]:
  key, split = random.split(key)
  x1 = fn(random.normal(key, shape))
  batch_axis = shape.index(BATCH_SIZE)
  shape = shape[:batch_axis] + (2 * BATCH_SIZE,) + shape[batch_axis + 1:]
  x2 = None if same_inputs else fn(random.normal(split, shape)) * 2
  return x1, x2


def _get_net(W_std, b_std, filter_shape, is_conv, use_pooling, is_res, padding,
             phi, strides, width, is_ntk, proj_into_2d, pool_type, layer_norm,
             parameterization, use_dropout):

  if is_conv:
    # Select a random filter order.
    default_filter_spec = 'HW'
    filter_specs = [''.join(p) for p in itertools.permutations('HWIO')]
    filter_spec = prandom.choice(filter_specs)
    filter_shape = tuple(filter_shape[default_filter_spec.index(c)]
                         for c in filter_spec if c in default_filter_spec)
    strides = tuple(strides[default_filter_spec.index(c)]
                    for c in filter_spec if c in default_filter_spec)

    # Select the activation order.
    default_spec = 'NHWC'
    if default_backend() == 'tpu':
      # Keep batch dimension leading for TPU for batching to work.
      specs = ['N' + ''.join(p) for p in itertools.permutations('CHW')]
    else:
      specs = [''.join(p) for p in itertools.permutations('NCHW')]
    spec = prandom.choice(specs)
    input_shape = tuple(INPUT_SHAPE[default_spec.index(c)] for c in spec)

  else:
    input_shape = (INPUT_SHAPE[0], onp.prod(INPUT_SHAPE[1:]))
    if default_backend() == 'tpu':
      spec = 'NC'
    else:
      spec = prandom.choice(['NC', 'CN'])
      if spec.index('N') == 1:
        input_shape = input_shape[::-1]

    filter_spec = None

  dimension_numbers = (spec, filter_spec, spec)
  batch_axis, channel_axis = spec.index('N'), spec.index('C')

  spec_fc = ''.join(c for c in spec if c in ('N', 'C'))
  batch_axis_fc, channel_axis_fc = spec_fc.index('N'), spec_fc.index('C')

  if not is_conv:
    batch_axis = batch_axis_fc
    channel_axis = channel_axis_fc

  if layer_norm:
    layer_norm = tuple(spec.index(c) for c in layer_norm)

  def fc(out_dim):
    return stax.Dense(
        out_dim=out_dim,
        W_std=W_std,
        b_std=b_std,
        parameterization=parameterization,
        batch_axis=batch_axis_fc,
        channel_axis=channel_axis_fc
    )

  def conv(out_chan):
    return stax.Conv(
        out_chan=out_chan,
        filter_shape=filter_shape,
        strides=strides,
        padding=padding,
        W_std=W_std,
        b_std=b_std,
        dimension_numbers=dimension_numbers,
        parameterization=parameterization
    )

  affine = conv(width) if is_conv else fc(width)

  rate = onp.random.uniform(0.5, 0.9)
  dropout = stax.Dropout(rate, mode='train')

  if pool_type == 'AVG':
    pool_fn = stax.AvgPool
    global_pool_fn = stax.GlobalAvgPool
  elif pool_type == 'SUM':
    pool_fn = stax.SumPool
    global_pool_fn = stax.GlobalSumPool
  else:
    raise ValueError(pool_type)

  if use_pooling:
    pool_or_identity = pool_fn((2, 3),
                               None,
                               'SAME' if padding == 'SAME' else 'CIRCULAR',
                               batch_axis=batch_axis,
                               channel_axis=channel_axis)
  else:
    pool_or_identity = stax.Identity()
  dropout_or_identity = dropout if use_dropout else stax.Identity()
  layer_norm_or_identity = (stax.Identity() if layer_norm is None else
                            stax.LayerNorm(axis=layer_norm,
                                           batch_axis=batch_axis,
                                           channel_axis=channel_axis))
  res_unit = stax.serial(dropout_or_identity, affine, pool_or_identity)
  if is_res:
    block = stax.serial(
        affine,
        stax.FanOut(2),
        stax.parallel(stax.Identity(),
                      res_unit),
        stax.FanInSum(),
        layer_norm_or_identity,
        phi)
  else:
    block = stax.serial(
        affine,
        res_unit,
        layer_norm_or_identity,
        phi)

  if proj_into_2d == 'FLAT':
    proj_layer = stax.Flatten(batch_axis, batch_axis_fc)
  elif proj_into_2d == 'POOL':
    proj_layer = global_pool_fn(batch_axis, channel_axis)
  elif proj_into_2d.startswith('ATTN'):
    n_heads = int(np.sqrt(width))
    n_chan_val = int(np.round(float(width) / n_heads))
    proj_layer = stax.serial(
        stax.GlobalSelfAttention(
            n_chan_out=width,
            n_chan_key=width,
            n_chan_val=n_chan_val,
            n_heads=n_heads,
            linear_scaling=True,
            W_key_std=W_std,
            W_value_std=W_std,
            W_query_std=W_std,
            W_out_std=1.0,
            b_std=b_std,
            batch_axis=batch_axis,
            channel_axis=channel_axis),
        stax.Flatten(batch_axis, batch_axis_fc))
  else:
    raise ValueError(proj_into_2d)
  readout = stax.serial(proj_layer, fc(1 if is_ntk else width))

  device_count = -1 if spec.index('N') == 0 else 0

  return stax.serial(block, readout), input_shape, device_count, channel_axis_fc


def _get_net_pool(width, is_ntk, pool_type, padding,
                  filter_shape, strides, normalize_edges):
  W_std, b_std = 2.**0.5, 0.5**0.5
  phi = stax.Relu()
  parameterization = 'ntk'

  fc = functools.partial(
      stax.Dense,
      W_std=W_std / width if pool_type == 'SUM' else W_std,
      b_std=b_std,
      parameterization=parameterization)

  conv = functools.partial(
      stax.Conv,
      filter_shape=filter_shape,
      strides=None,
      padding='SAME',
      W_std=W_std / onp.prod(filter_shape) if pool_type == 'SUM' else W_std,
      b_std=b_std,
      parameterization=parameterization)

  if pool_type == 'AVG':
    pool_fn = functools.partial(stax.AvgPool, normalize_edges=normalize_edges)
    global_pool_fn = stax.GlobalAvgPool
  elif pool_type == 'SUM':
    pool_fn = stax.SumPool
    global_pool_fn = stax.GlobalSumPool
  else:
    raise ValueError(pool_type)

  pool = pool_fn(filter_shape, strides, padding)

  device_count = -1

  return stax.serial(
      conv(width),
      phi,
      pool,
      conv(width),
      phi,
      global_pool_fn(),
      fc(1 if is_ntk else width)
  ), INPUT_SHAPE, device_count, -1


class StaxTest(test_utils.NeuralTangentsTestCase):

  def _skip_test(self, filter_shape, is_conv, is_res, padding, proj_into_2d,
                 strides, use_pooling):
    if is_conv:
      test_utils.skip_test(self)

      if (is_res and is_conv and ((strides is not None and strides != (1, 1)) or
                                  (padding == 'VALID' and filter_shape !=
                                   (1, 1)))):
        raise absltest.SkipTest('Different paths in a residual models need to '
                                'return outputs of the same shape.')
    elif (filter_shape != FILTER_SHAPES[0] or padding != PADDINGS[0] or
          strides != STRIDES[0] or proj_into_2d != PROJECTIONS[0] or
          use_pooling):
      raise absltest.SkipTest('FC models do not have these parameters.')

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              '_{}_{}_{}_{}_{}_{}_{}_{}_{}_{}_{}'.format(
                  model, phi_name, width, 'same_inputs'
                  if same_inputs else 'different_inputs', 'filter_shape=%s' %
                  str(filter_shape), 'padding=%s' % padding, 'strides=%s' %
                  str(strides), 'pool' if use_pooling else 'flatten',
                  'NTK' if is_ntk else 'NNGP', 'RESNET' if is_res else 'serial',
                  proj_into_2d),
          'model':
              model,
          'width':
              width,
          'strides':
              strides,
          'padding':
              padding,
          'phi':
              phi,
          'same_inputs':
              same_inputs,
          'filter_shape':
              filter_shape,
          'use_pooling':
              use_pooling,
          'is_ntk':
              is_ntk,
          'is_res':
              is_res,
          'proj_into_2d':
              proj_into_2d
      }
                          for model in MODELS
                          for width in WIDTHS
                          for phi, phi_name in ACTIVATIONS.items()
                          for same_inputs in [False]
                          for padding in PADDINGS for strides in STRIDES
                          for filter_shape in FILTER_SHAPES
                          for use_pooling in [False, True]
                          for is_ntk in [False, True]
                          for is_res in [False, True]
                          for proj_into_2d in PROJECTIONS))
  def test_exact(self, model, width, strides, padding, phi, same_inputs,
                 filter_shape, use_pooling, is_ntk, is_res, proj_into_2d):
    is_conv = 'conv' in model

    # Check for duplicate / incorrectly-shaped NN configs / wrong backend.
    self._skip_test(filter_shape, is_conv, is_res, padding, proj_into_2d,
                    strides, use_pooling)

    pool_type = 'AVG'
    W_std, b_std = 2.**0.5, 0.5**0.5
    layer_norm = None
    parameterization = 'ntk'
    use_dropout = False

    net = _get_net(W_std, b_std, filter_shape, is_conv, use_pooling, is_res,
                   padding, phi, strides, width, is_ntk, proj_into_2d,
                   pool_type, layer_norm, parameterization, use_dropout)
    self._check_agreement_with_empirical(
        net, same_inputs, use_dropout, is_ntk, RTOL, 1.05)

  # pylint: disable=g-complex-comprehension
  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              f'_model={model}'
              f'_width={width}'
              f'_same_inputs={same_inputs}'
              f'_filter_shape={filter_shape}'
              f'_proj={proj_into_2d}_'
              f'_is_ntk={is_ntk}_'
              f'_b_std={b_std}_'
              f'_param={parameterization}',
          'model':
              model,
          'width':
              width,
          'same_inputs':
              same_inputs,
          'filter_shape':
              filter_shape,
          'proj_into_2d':
              proj_into_2d,
          'is_ntk':
              is_ntk,
          'b_std':
              b_std,
          'parameterization':
              parameterization
      }
                          for model in MODELS
                          for width in WIDTHS
                          for same_inputs in [False]
                          for is_ntk in [False, True]
                          for filter_shape in FILTER_SHAPES
                          for proj_into_2d in PROJECTIONS[:2]
                          for b_std in [None, 0., 0.5**0.5]
                          for parameterization in PARAMETERIZATIONS))
  def test_parameterizations(
      self,
      model,
      width,
      same_inputs,
      is_ntk,
      filter_shape,
      proj_into_2d,
      b_std,
      parameterization
  ):
    is_conv = 'conv' in model

    W_std = 2.**0.5
    if parameterization == 'STANDARD':
      W_std /= width**0.5
      if b_std is not None:
        b_std /= width**0.5

    padding = PADDINGS[0]
    strides = STRIDES[0]
    phi = stax.Relu()
    use_pooling, is_res = False, False
    layer_norm = None
    pool_type = 'AVG'
    use_dropout = False

    # Check for duplicate / incorrectly-shaped NN configs / wrong backend.
    if is_conv:
      test_utils.skip_test(self)
    elif proj_into_2d != PROJECTIONS[0] or filter_shape != FILTER_SHAPES[0]:
      raise absltest.SkipTest('FC models do not have these parameters.')

    net = _get_net(W_std=W_std,
                   b_std=b_std,
                   filter_shape=filter_shape,
                   is_conv=is_conv,
                   use_pooling=use_pooling,
                   is_res=is_res,
                   padding=padding,
                   phi=phi,
                   strides=strides,
                   width=width,
                   is_ntk=is_ntk,
                   proj_into_2d=proj_into_2d,
                   pool_type=pool_type,
                   layer_norm=layer_norm,
                   parameterization=parameterization,
                   use_dropout=use_dropout)
    self._check_agreement_with_empirical(net=net,
                                         same_inputs=same_inputs,
                                         use_dropout=use_dropout,
                                         is_ntk=is_ntk,
                                         rtol=0.021,
                                         atol=0.2)

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              '_{}_{}_{}_{}_{}_{}'.format(
                  model,
                  width,
                  'same_inputs' if same_inputs else 'different_inputs',
                  'NTK' if is_ntk else 'NNGP',
                  proj_into_2d,
                  'layer_norm=%s' % str(layer_norm)),
          'model':
              model,
          'width':
              width,
          'same_inputs':
              same_inputs,
          'is_ntk':
              is_ntk,
          'proj_into_2d':
              proj_into_2d,
          'layer_norm':
              layer_norm
      }
                          for model in MODELS
                          for width in WIDTHS
                          for same_inputs in [False]
                          for is_ntk in [False, True]
                          for proj_into_2d in PROJECTIONS[:2]
                          for layer_norm in LAYER_NORM))
  def test_layernorm(self,
                     model,
                     width,
                     same_inputs,
                     is_ntk,
                     proj_into_2d,
                     layer_norm):
    is_conv = 'conv' in model
    # Check for duplicate / incorrectly-shaped NN configs / wrong backend.
    if is_conv:
      test_utils.skip_test(self)
    elif proj_into_2d != PROJECTIONS[0] or layer_norm not in ('C', 'NC'):
      raise absltest.SkipTest('FC models do not have these parameters.')

    W_std, b_std = 2.**0.5, 0.5**0.5
    filter_shape = FILTER_SHAPES[0]
    padding = PADDINGS[0]
    strides = STRIDES[0]
    phi = stax.Relu()
    use_pooling, is_res = False, False
    parameterization = 'ntk'
    pool_type = 'AVG'
    use_dropout = False

    net = _get_net(W_std, b_std, filter_shape, is_conv, use_pooling, is_res,
                   padding, phi, strides, width, is_ntk, proj_into_2d,
                   pool_type, layer_norm, parameterization, use_dropout)
    self._check_agreement_with_empirical(net, same_inputs, use_dropout, is_ntk,
                                         0.07)

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              '_{}_{}_{}_{}_{}_{}_{}_{}'.format(
                  width, 'same_inputs' if same_inputs else 'different_inputs',
                  'filter_shape=%s' % str(filter_shape), 'padding=%s' %
                  padding, 'strides=%s' % str(strides),
                  'NTK' if is_ntk else 'NNGP', 'pool_type=%s' %
                  str(pool_type), 'normalize_edges=%s' % str(normalize_edges)),
          'width':
              width,
          'same_inputs':
              same_inputs,
          'is_ntk':
              is_ntk,
          'pool_type':
              pool_type,
          'padding':
              padding,
          'filter_shape':
              filter_shape,
          'strides':
              strides,
          'normalize_edges':
              normalize_edges
      } for width in WIDTHS for same_inputs in [False]
                          for is_ntk in [False, True]
                          for pool_type in POOL_TYPES for padding in PADDINGS
                          for filter_shape in FILTER_SHAPES
                          for strides in STRIDES
                          for normalize_edges in [True, False]))
  def test_pool(self, width, same_inputs, is_ntk, pool_type,
                padding, filter_shape, strides, normalize_edges):
    use_dropout = False
    # Check for duplicate / incorrectly-shaped NN configs / wrong backend.
    test_utils.skip_test(self)
    if pool_type == 'SUM' and normalize_edges:
      raise absltest.SkipTest('normalize_edges not applicable to SumPool.')

    net = _get_net_pool(width, is_ntk, pool_type,
                        padding, filter_shape, strides, normalize_edges)
    self._check_agreement_with_empirical(net, same_inputs, use_dropout, is_ntk)

  def test_avg_pool(self):
    X1 = np.ones((4, 2, 3, 2))
    X2 = np.ones((3, 2, 3, 2))

    _, apply_fn, kernel_fn = stax.AvgPool((2, 2), (1, 1), 'SAME',
                                          normalize_edges=False)
    _, apply_fn_norm, kernel_fn_norm = stax.AvgPool((2, 2), (1, 1), 'SAME',
                                                    normalize_edges=True)
    _, apply_fn_stax = ostax.AvgPool((2, 2), (1, 1), 'SAME')

    out1 = apply_fn((), X1)
    out2 = apply_fn((), X2)

    out1_norm = apply_fn_norm((), X1)
    out2_norm = apply_fn_norm((), X2)

    out1_stax = apply_fn_stax((), X1)
    out2_stax = apply_fn_stax((), X2)

    self.assertAllClose((out1_stax, out2_stax), (out1_norm, out2_norm))

    out_unnorm = np.array([[1., 1., 0.5], [0.5, 0.5, 0.25]]).reshape(
        (1, 2, 3, 1))
    out1_unnormalized = np.broadcast_to(out_unnorm, X1.shape)
    out2_unnormalized = np.broadcast_to(out_unnorm, X2.shape)

    self.assertAllClose((out1_unnormalized, out2_unnormalized), (out1, out2))

    ker = kernel_fn(X1, X2)
    ker_norm = kernel_fn_norm(X1, X2)

    self.assertAllClose(np.ones_like(ker_norm.nngp), ker_norm.nngp)
    self.assertAllClose(np.ones_like(ker_norm.cov1), ker_norm.cov1)
    self.assertAllClose(np.ones_like(ker_norm.cov2), ker_norm.cov2)

    self.assertEqual(ker_norm.nngp.shape, ker.nngp.shape)
    self.assertEqual(ker_norm.cov1.shape, ker.cov1.shape)
    self.assertEqual(ker_norm.cov2.shape, ker.cov2.shape)

    ker_unnorm = np.outer(out_unnorm, out_unnorm).reshape((2, 3, 2, 3))
    ker_unnorm = np.transpose(ker_unnorm, axes=(0, 2, 1, 3))
    nngp = np.broadcast_to(
        ker_unnorm.reshape((1, 1) + ker_unnorm.shape), ker.nngp.shape)
    cov1 = np.broadcast_to(np.expand_dims(ker_unnorm, 0), ker.cov1.shape)
    cov2 = np.broadcast_to(np.expand_dims(ker_unnorm, 0), ker.cov2.shape)
    self.assertAllClose((nngp, cov1, cov2), (ker.nngp, ker.cov1, ker.cov2))

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              '_{}_{}_{}_{}_{}_{}_{}_{}_{}_{}'.format(
                  model, phi_name, width, 'same_inputs'
                  if same_inputs else 'different_inputs', 'filter_shape=%s' %
                  str(filter_shape), 'padding=%s' % padding, 'strides=%s' %
                  str(strides), 'pool' if use_pooling else 'flatten',
                  'NTK' if is_ntk else 'NNGP', proj_into_2d),
          'model':
              model,
          'width':
              width,
          'same_inputs':
              same_inputs,
          'is_ntk':
              is_ntk,
          'padding':
              padding,
          'strides':
              strides,
          'filter_shape':
              filter_shape,
          'phi':
              phi,
          'use_pooling':
              use_pooling,
          'proj_into_2d':
              proj_into_2d
      } for model in MODELS for width in WIDTHS
                          for same_inputs in [True, False]
                          for phi, phi_name in ACTIVATIONS.items()
                          for padding in ['SAME'] for strides in STRIDES
                          for filter_shape in [(2, 1)]
                          for is_ntk in [True, False]
                          for use_pooling in [True, False]
                          for proj_into_2d in ['FLAT', 'POOL']))
  def test_dropout(self, model, width, same_inputs, is_ntk, padding, strides,
                   filter_shape, phi, use_pooling, proj_into_2d):
    pool_type = 'AVG'
    use_dropout = True
    is_conv = 'conv' in model
    is_res = False
    W_std, b_std = 2.**0.5, 0.5**0.5
    layer_norm = None
    parameterization = 'ntk'
    # Check for duplicate / incorrectly-shaped NN configs / wrong backend.
    self._skip_test(filter_shape, is_conv, is_res, padding, proj_into_2d,
                    strides, use_pooling)

    net = _get_net(W_std, b_std, filter_shape, is_conv, use_pooling, is_res,
                   padding, phi, strides, width, is_ntk, proj_into_2d,
                   pool_type, layer_norm, parameterization, use_dropout)
    self._check_agreement_with_empirical(net, same_inputs, use_dropout, is_ntk)

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              f'_act={act}_kernel={kern}_do_stabilize={do_stabilize}',
          'act': act,
          'kernel': kern,
          'do_stabilize': do_stabilize
      }
                          for act in ['erf', 'relu']
                          for do_stabilize in [True, False]
                          for kern in ['nngp', 'ntk']))
  def test_sparse_inputs(self, act, kernel, do_stabilize):
    if do_stabilize and act != 'relu':
      raise absltest.SkipTest('Stabilization possible only in Relu.')

    key = random.PRNGKey(1)

    input_count = 4
    sparse_count = 2
    input_size = 3
    width = 1024

    # NOTE(schsam): It seems that convergence is slower when inputs are sparse.
    samples = N_SAMPLES

    if default_backend() == 'gpu':
      jtu._default_tolerance[onp.dtype(onp.float64)] = 5e-4
      samples = 100 * N_SAMPLES
    else:
      jtu._default_tolerance[onp.dtype(onp.float32)] = 5e-2
      jtu._default_tolerance[onp.dtype(onp.float64)] = 5e-3

    # a batch of dense inputs
    x_dense = random.normal(key, (input_count, input_size))
    x_sparse = x_dense.at[:sparse_count, :].set(0.)

    activation = (stax.Relu(do_stabilize=do_stabilize) if act == 'relu'
                  else stax.Erf())

    init_fn, apply_fn, kernel_fn = stax.serial(
        stax.Dense(width),
        activation,
        stax.Dense(1 if kernel == 'ntk' else width))
    exact = kernel_fn(x_sparse, None, kernel)

    mc = nt.monte_carlo_kernel_fn(
        init_fn,
        apply_fn,
        random.split(key, 2)[0],
        samples,
        vmap_axes=0,
        device_count=-1,
        implementation=2
    )(x_sparse, None, kernel)
    mc = np.reshape(mc, exact.shape)

    assert not np.any(np.isnan(exact))
    self.assertAllClose(exact[sparse_count:, sparse_count:],
                        mc[sparse_count:, sparse_count:])

  def test_composition_dense(self):
    rng = random.PRNGKey(0)
    x1 = random.normal(rng, (2, 3))
    x2 = random.normal(rng, (4, 3))

    Block = stax.serial(stax.Dense(256), stax.Relu())

    _, _, ker_fn = Block
    _, _, composed_ker_fn = stax.serial(Block, Block)

    ker_out = ker_fn(ker_fn(x1))
    composed_ker_out = composed_ker_fn(x1)
    self.assertAllClose(ker_out, composed_ker_out)

    ker_out = ker_fn(ker_fn(x1, x2))
    composed_ker_out = composed_ker_fn(x1, x2)
    self.assertAllClose(ker_out, composed_ker_out)

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name': '_avg_pool={}_same_inputs={}'.format(avg_pool,
                                                                same_inputs),
          'avg_pool': avg_pool,
          'same_inputs': same_inputs
      } for avg_pool in [True, False] for same_inputs in [True, False]))
  def test_composition_conv(self, avg_pool, same_inputs):
    rng = random.PRNGKey(0)
    x1 = random.normal(rng, (3, 5, 5, 3))
    x2 = None if same_inputs else random.normal(rng, (4, 5, 5, 3))

    Block = stax.serial(stax.Conv(256, (3, 3)), stax.Relu())
    if avg_pool:
      Readout = stax.serial(stax.Conv(256, (3, 3)),
                            stax.GlobalAvgPool(),
                            stax.Dense(10))
    else:
      Readout = stax.serial(stax.Flatten(), stax.Dense(10))

    block_ker_fn, readout_ker_fn = Block[2], Readout[2]
    _, _, composed_ker_fn = stax.serial(Block, Readout)

    composed_ker_out = composed_ker_fn(x1, x2)
    ker_out_no_marg = readout_ker_fn(block_ker_fn(x1, x2,
                                                  diagonal_spatial=False))
    ker_out_default = readout_ker_fn(block_ker_fn(x1, x2))
    self.assertAllClose(composed_ker_out, ker_out_no_marg)
    self.assertAllClose(composed_ker_out, ker_out_default)

    if avg_pool:
      with self.assertRaises(ValueError):
        ker_out = readout_ker_fn(block_ker_fn(x1, x2, diagonal_spatial=True))
    else:
      ker_out_marg = readout_ker_fn(block_ker_fn(x1, x2,
                                                 diagonal_spatial=True))
      self.assertAllClose(composed_ker_out, ker_out_marg)

  def _check_agreement_with_empirical(
      self,
      net,
      same_inputs,
      use_dropout,
      is_ntk,
      rtol=RTOL,
      atol=ATOL
  ):
    ((init_fn, apply_fn, kernel_fn),
     input_shape, device_count, channel_axis) = net

    num_samples = N_SAMPLES * 5 if use_dropout else N_SAMPLES
    key = random.PRNGKey(1)
    x1, x2 = _get_inputs(key, same_inputs, input_shape)
    if default_backend() == 'tpu' and use_dropout:
      # including a test case for tpu + dropout with (parallel + batching)
      batch_size = 2
    else:
      batch_size = 0
    x1_out_shape, params = init_fn(key, x1.shape)
    if same_inputs:
      assert x2 is None
    if x2 is None:
      x2_out_shape = x1_out_shape
    else:
      x2_out_shape, params = init_fn(key, x2.shape)
    del params

    def _get_empirical(n_samples, get):
      kernel_fn_empirical = nt.monte_carlo_kernel_fn(
          init_fn, apply_fn, key, n_samples, device_count=device_count,
          trace_axes=(channel_axis,), batch_size=batch_size,
          implementation=2
      )
      if same_inputs:
        assert x2 is None
      return kernel_fn_empirical(x1, x2, get)

    if is_ntk:
      exact, shape1, shape2 = kernel_fn(x1, x2, ('ntk', 'shape1', 'shape2'))
      empirical = _get_empirical(num_samples, 'ntk')
    else:
      exact, shape1, shape2 = kernel_fn(x1, x2, ('nngp', 'shape1', 'shape2'))
      empirical = _get_empirical(num_samples, 'nngp')
    test_utils.assert_close_matrices(self, exact, empirical, rtol, atol)
    self.assertEqual(shape1, x1_out_shape)
    self.assertEqual(shape2, x2_out_shape)


class ParallelInOutTest(test_utils.NeuralTangentsTestCase):

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              f'_same_inputs={same_inputs}_kernel_type={kernel_type}',
          'same_inputs': same_inputs,
          'kernel_type': kernel_type
      }
                          for same_inputs in [True, False]
                          for kernel_type in ['ntk']))
  def test_parallel_in(self, same_inputs, kernel_type):
    platform = default_backend()
    rtol = RTOL if platform != 'tpu' else 0.05

    rng = random.PRNGKey(0)
    input_key1, input_key2, mc_key = random.split(rng, 3)

    x1_1, x2_1 = _get_inputs(input_key1, same_inputs, (BATCH_SIZE, 2))
    x1_2, x2_2 = _get_inputs(input_key2, same_inputs, (BATCH_SIZE, 3))

    x1 = (x1_1, x1_2)
    x2 = (x2_1, x2_2)

    N = 2 ** 7

    def net(logits):
      return stax.serial(
          stax.parallel(stax.Dense(N), stax.Dense(N)),
          stax.serial(stax.FanInSum(), stax.Dense(logits)))

    init_fn, apply_fn, kernel_fn = net(N if kernel_type == 'nngp' else 1)

    kernel_fn_empirical = nt.monte_carlo_kernel_fn(
        init_fn, apply_fn, mc_key, N_SAMPLES, trace_axes=(-1,),
        implementation=2,
        vmap_axes=((0, 0), 0, {})
    )
    test_utils.assert_close_matrices(self,
                                     kernel_fn(x1, x2, kernel_type),
                                     kernel_fn_empirical(x1, x2, kernel_type),
                                     rtol)

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              f'_same_inputs={same_inputs}_kernel_type={kernel_type}',
          'same_inputs': same_inputs,
          'kernel_type': kernel_type
      } for same_inputs in [True, False] for kernel_type in ['ntk']))
  def test_parallel_out(self, same_inputs, kernel_type):
    platform = default_backend()
    rtol = RTOL if platform != 'tpu' else 0.05

    rng = random.PRNGKey(0)
    input_key1, mc_key = random.split(rng, 2)

    x1, x2 = _get_inputs(input_key1, same_inputs, (BATCH_SIZE, 1))

    N = 2 ** 10

    def net(logits):
      return stax.serial(
          stax.Dense(N),
          stax.FanOut(2),
          stax.parallel(stax.Dense(logits), stax.Dense(logits)))

    init_fn, apply_fn, kernel_fn = net(N if kernel_type == 'nngp' else 1)

    kernel_fn_empirical = nt.monte_carlo_kernel_fn(
        init_fn, apply_fn, mc_key, N_SAMPLES, trace_axes=(-1,),
        implementation=2,
        vmap_axes=(0, [0, 0], {}))

    test_utils.assert_close_matrices(self,
                                     kernel_fn(x1, x2, kernel_type),
                                     kernel_fn_empirical(x1, x2, kernel_type),
                                     rtol)

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              f'_same_inputs={same_inputs}_kernel_type={kernel_type}',
          'same_inputs': same_inputs,
          'kernel_type': kernel_type,
      } for same_inputs in [True, False] for kernel_type in ['ntk']))
  def test_parallel_in_out(self, same_inputs, kernel_type):
    platform = default_backend()
    rtol = RTOL if platform != 'tpu' else 0.05

    rng = random.PRNGKey(0)
    input_key1, input_key2, mc_key = random.split(rng, 3)

    x1_1, x2_1 = _get_inputs(input_key1, same_inputs, (BATCH_SIZE, 1))
    x1_2, x2_2 = _get_inputs(input_key2, same_inputs, (BATCH_SIZE, 2))

    x1 = (x1_1, x1_2)
    x2 = (x2_1, x2_2)

    N_in = 2 ** 10
    N_out = N_in if kernel_type == 'nngp' else 1

    readin = stax.serial(stax.parallel(stax.Dense(N_in), stax.Dense(N_in)),
                         stax.FanInSum())
    readout = stax.serial(stax.FanOut(3),
                          stax.parallel(stax.Dense(N_out),
                                        stax.Dense(N_out + 1),
                                        stax.Dense(N_out + 2)))
    init_fn, apply_fn, _ = stax.serial(readin, readout)

    K_readin_fn = jit(readin[2])
    K_readout_fn = jit(functools.partial(readout[2], get=kernel_type))

    kernel_fn_empirical = nt.monte_carlo_kernel_fn(
        init_fn, apply_fn, mc_key, N_SAMPLES, trace_axes=(-1,),
        implementation=2,
        vmap_axes=((0, 0), [0, 0, 0], {})
    )

    test_utils.assert_close_matrices(
        self,
        K_readout_fn(K_readin_fn(x1, x2)),
        kernel_fn_empirical(x1, x2, get=kernel_type),
        rtol)

    # Check Both (here we just want to make sure we _can_ compute the output).
    K_readin_fn = jit(readin[2])
    K_readout_fn = jit(functools.partial(readout[2], get=('nngp', 'ntk')))

    K_readout_fn(K_readin_fn(x1, x2))

  @parameterized.named_parameters(
      jtu.cases_from_list({
          'testcase_name':
              f'_same_inputs={same_inputs}_kernel_type={kernel_type}',
          'same_inputs': same_inputs,
          'kernel_type': kernel_type,
      } for same_inputs in [True, False] for kernel_type in ['ntk']))
  def test_nested_parallel(self, same_inputs, kernel_type):
    platform = default_backend()
    rtol = RTOL if platform != 'tpu' else 0.05

    rng = random.PRNGKey(0)
    (input_key1,
     input_key2,
     input_key3,
     input_key4,
     mask_key,
     mc_key) = random.split(rng, 6)

    x1_1, x2_1 = _get_inputs(input_key1, same_inputs, (BATCH_SIZE, 5))
    x1_2, x2_2 = _get_inputs(input_key2, same_inputs, (BATCH_SIZE, 2, 2, 2))
    x1_3, x2_3 = _get_inputs(input_key3, same_inputs, (BATCH_SIZE, 2, 2, 3))
    x1_4, x2_4 = _get_inputs(input_key4, same_inputs, (BATCH_SIZE, 3, 4))

    m1_key, m2_key, m3_key, m4_key = random.split(mask_key, 4)

    x1_1 = test_utils.mask(
        x1_1, mask_constant=-1, mask_axis=(1,), key=m1_key, p=0.5)
    x1_2 = test_utils.mask(
        x1_2, mask_constant=-1, mask_axis=(2, 3,), key=m2_key, p=0.5)
    if not same_inputs:
      x2_3 = test_utils.mask(
          x2_3, mask_constant=-1, mask_axis=(1, 3,), key=m3_key, p=0.5)
      x2_4 = test_utils.mask(
          x2_4, mask_constant=-1, mask_axis=(2,), key=m4_key, p=0.5)

    x1 = (((x1_1, x1_2), x1_3), x1_4)
    x2 = (((x2_1, x2_2), x2_3), x2_4) if not same_inputs else None

    N_in = 2 ** 7

    # We only include dropout on non-TPU backends, because it takes large N to
    # converge on TPU.
    dropout_or_id = stax.Dropout(0.9) if platform != 'tpu' else stax.Identity()

    init_fn, apply_fn, kernel_fn = stax.parallel(
        stax.parallel(
            stax.parallel(stax.Dense(N_in),
                          stax.serial(stax.Conv(N_in + 1, (2, 2)),
                                      stax.Flatten())),
            stax.serial(stax.Conv(N_in + 2, (2, 2)),
                        dropout_or_id,
                        stax.GlobalAvgPool())),
        stax.Conv(N_in + 3, (2,)))

    kernel_fn_empirical = nt.monte_carlo_kernel_fn(
        init_fn, apply_fn, mc_key, N_SAMPLES, implementation=2,
        vmap_axes=(((((0, 0), 0), 0), (((0, 0), 0), 0), {})
                   if platform == 'tpu' else None)
    )

    test_utils.assert_close_matrices(
        self,
        kernel_fn(x1, x2, get=kernel_type, mask_constant=-1),
        kernel_fn_empirical(x1, x2, get=kernel_type, mask_constant=-1),
        rtol)


if __name__ == '__main__':
  absltest.main()
