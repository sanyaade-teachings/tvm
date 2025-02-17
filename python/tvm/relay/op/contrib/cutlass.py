# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name
"""Patterns supported CUTLASS."""
from tvm.ir.transform import Sequential, PassContext
from tvm.relay import transform
from tvm.relay.build_module import bind_params_by_name
from ...dataflow_pattern import wildcard, is_op, is_constant


def make_gelu_pattern(bias_out, out_dtype="float16"):
    mul = is_op("multiply")(bias_out, is_constant() | wildcard())
    if out_dtype == "float16":
        erf = is_op("cast")(is_op("erf")(is_op("cast")(mul)))
    else:
        erf = is_op("erf")(mul)
    mul_half = is_op("multiply")(erf, is_constant() | wildcard())
    add = is_op("add")(mul_half, is_constant() | wildcard())
    return is_op("multiply")(add, bias_out)


def make_gemm_pattern(with_bias=True, with_act=None, out_dtype="float16"):
    """Create a pattern for dense op followed by activations."""
    data = wildcard()
    weight = wildcard()
    bias = wildcard()
    gemm = is_op("nn.dense")(data, weight)
    if with_bias:
        add_or_bias_add = is_op("add") | is_op("nn.bias_add")
        gemm_out = add_or_bias_add(gemm, bias)
    else:
        gemm_out = gemm

    if with_act is None:
        return gemm_out
    if isinstance(with_act, str) and with_act == "relu":
        return is_op("nn.relu")(gemm_out)

    assert isinstance(with_act, str) and with_act == "gelu"
    return make_gelu_pattern(gemm_out, out_dtype)


def make_batch_matmul_pattern():
    return is_op("nn.batch_matmul")(wildcard(), wildcard())


def make_conv2d_pattern(with_bias=False, with_act=None):
    """Create a pattern for dense op followed by activations."""
    data = wildcard()
    weight = wildcard()
    bias = wildcard()
    conv2d = is_op("nn.conv2d")(data, weight)
    if with_bias:
        add_or_bias_add = is_op("add") | is_op("nn.bias_add")
        conv2d_out = add_or_bias_add(conv2d, bias)
    else:
        conv2d_out = conv2d

    if with_act is not None:
        if with_act == "relu":
            return is_op("nn.relu")(conv2d_out)
        if with_act == "sigmoid":
            return is_op("sigmoid")(conv2d_out)
        if with_act == "silu":
            return is_op("multiply")(conv2d_out, is_op("sigmoid")(conv2d_out))
        if with_act == "hardswish":
            rhs = is_op("divide")(
                is_op("clip")(is_op("add")(conv2d_out, is_constant())), is_constant()
            )
            return is_op("multiply")(conv2d_out, rhs)

        raise ValueError("Unknown activation %s." % with_act)

    return conv2d_out


def check_dtype(lhs, rhs):
    """Check if dtypes in the given workload are supported by CUTLASS."""
    # Only fp16 inputs are supported for now.
    return lhs.dtype == rhs.dtype and lhs.dtype == "float16" and rhs.dtype == "float16"


def get_root_call(call, root_op_name):
    if str(call.op) == root_op_name:
        return call
    return get_root_call(call.args[0], root_op_name)


def check_gemm(call):
    """Check if the given dense workload can be offloaded to CUTLASS."""
    dense = get_root_call(call, "nn.dense")
    lhs = dense.args[0].checked_type
    rhs = dense.args[1].checked_type
    return check_dtype(lhs, rhs)


def check_batch_matmul(call):
    """Check if the given batch_matmul workload can be offloaded to CUTLASS."""
    batch_matmul = get_root_call(call, "nn.batch_matmul")
    lhs = batch_matmul.args[0].checked_type
    rhs = batch_matmul.args[1].checked_type
    transpose_a = batch_matmul.attrs.transpose_a
    transpose_b = batch_matmul.attrs.transpose_b
    return check_dtype(lhs, rhs) and not transpose_a and transpose_b


def is_depthwise_conv2d(ic, oc, groups):
    return ic == oc == groups


def check_conv2d(call):
    """Check if the given conv2d workload can be offloaded to CUTLASS."""
    conv2d = get_root_call(call, "nn.conv2d")
    data_layout = conv2d.attrs.data_layout
    kernel_layout = conv2d.attrs.kernel_layout
    data = conv2d.args[0].checked_type
    weight = conv2d.args[1].checked_type
    if data_layout != "NHWC" or kernel_layout != "OHWI" or not check_dtype(data, weight):
        return False
    IC = data.shape[3]
    OC = weight.shape[0]
    return not is_depthwise_conv2d(IC, OC, conv2d.attrs.groups)


def partition_for_cutlass(mod, params=None):
    """Partition the input module into CUTLASS-supported subgraphs."""
    dense_pat = ("cutlass.dense", make_gemm_pattern(False, None), check_gemm)
    dense_bias_pat = ("cutlass.dense_bias", make_gemm_pattern(True, None), check_gemm)
    dense_bias_relu_pat = ("cutlass.dense_bias_relu", make_gemm_pattern(True, "relu"), check_gemm)
    dense_bias_gelu_fp16_pat = (
        "cutlass.dense_bias_gelu_fp16",
        make_gemm_pattern(True, "gelu"),
        check_gemm,
    )
    dense_bias_gelu_fp32_pat = (
        "cutlass.dense_bias_gelu_fp32",
        make_gemm_pattern(True, "gelu", out_dtype="float32"),
        check_gemm,
    )
    cutlass_patterns = [
        dense_bias_gelu_fp16_pat,
        dense_bias_gelu_fp32_pat,
        dense_bias_relu_pat,
        dense_bias_pat,
        dense_pat,
        ("cutlass.batch_matmul", make_batch_matmul_pattern(), check_batch_matmul),
        (
            "cutlass.conv2d_bias_hardswish",
            make_conv2d_pattern(with_bias=True, with_act="hardswish"),
            check_conv2d,
        ),
        (
            "cutlass.conv2d_bias_silu",
            make_conv2d_pattern(with_bias=True, with_act="silu"),
            check_conv2d,
        ),
        (
            "cutlass.conv2d_bias_relu",
            make_conv2d_pattern(with_bias=True, with_act="relu"),
            check_conv2d,
        ),
        (
            "cutlass.conv2d_bias_sigmoid",
            make_conv2d_pattern(with_bias=True, with_act="sigmoid"),
            check_conv2d,
        ),
        ("cutlass.conv2d_bias", make_conv2d_pattern(with_bias=True), check_conv2d),
        ("cutlass.conv2d", make_conv2d_pattern(), check_conv2d),
    ]

    if params is not None:
        mod["main"] = bind_params_by_name(mod["main"], params)
        remove_bn_pass = Sequential(
            [
                transform.InferType(),
                transform.SimplifyInference(),
                transform.FoldConstant(),
                transform.FoldScaleAxis(),
            ]
        )
        with PassContext(opt_level=3):
            mod = remove_bn_pass(mod)

    seq = Sequential(
        [
            transform.InferType(),
            transform.MergeComposite(cutlass_patterns),
            transform.AnnotateTarget(["cutlass"], include_non_call_ops=False),
            transform.PartitionGraph(bind_constants=False),
        ]
    )

    return seq(mod)
