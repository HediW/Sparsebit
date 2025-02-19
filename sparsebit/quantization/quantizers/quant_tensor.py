import os
import numpy as np
import torch
import torch.nn as nn
from sparsebit.quantization.common import Backend

if torch.cuda.is_available():
    from torch.utils.cpp_extension import load

    basedir = os.path.dirname(os.path.dirname(__file__))
    if not os.path.exists(os.path.join(basedir, "torch_extensions/build")):
        os.makedirs(os.path.join(basedir, "torch_extensions/build"))
    fake_quant_kernel = load(
        name="fake_quant",
        sources=[
            os.path.join(basedir, "torch_extensions/export.cc"),
            os.path.join(basedir, "torch_extensions/fake_quant_tensor.cu"),
        ],
        with_cuda=True,
        build_directory=os.path.join(basedir, "torch_extensions/build"),
        extra_cflags=["-O3"],
    )


class STE(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, scale, zero_point, qdesc, backend):
        x_fq = fake_quant_factory[backend](x, scale, zero_point, qdesc)
        ctx.save_for_backward(x, x_fq, scale, zero_point)
        ctx.qdesc = qdesc
        return x_fq

    @staticmethod
    def backward(ctx, gout):
        x, x_fq, scale, zero_point = ctx.saved_tensors
        qdesc = ctx.qdesc
        qmin, qmax = qdesc.qmin, qdesc.qmax
        if torch.cuda.is_available():
            if qdesc.is_perchannel:
                gx, gs, gzp = fake_quant_kernel.quant_perchannel_backward(
                    x, x_fq, scale, zero_point.float(), gout, qmin, qmax, qdesc.ch_axis
                )
            else:
                gx, gs, gzp = fake_quant_kernel.quant_pertensor_backward(
                    x, x_fq, scale, zero_point.float(), gout, qmin, qmax
                )
                # min_fq = (qmin - zero_point) * scale
                # max_fq = (qmax - zero_point) * scale
                # zero = gout.new_zeros(1)
                # one = gout.new_ones(1)
                # pred_gs = (x_fq - x) / scale * gout
                # pred_gs[x <= min_fq] = (qdesc.qmin - 0) * gout[x <= min_fq]
                # pred_gs[x >= max_fq] = (qdesc.qmax - 0) * gout[x >= max_fq]
                # pred_gs = pred_gs.sum()
                # snr = ((pred_gs.reshape(-1) - gs.reshape(-1))**2).sum() / (gs**2).sum()
                # print("backward: ", x.shape, scale.shape, snr)
                # from IPython import embed; embed()
            gs = gs if scale.requires_grad else None
            gzp = gzp if zero_point.requires_grad else None
        else:
            min_fq = (qmin - zero_point) * scale
            max_fq = (qmax - zero_point) * scale
            zero = gout.new_zeros(1)
            gx = torch.where((x >= min_fq) * (x <= max_fq), gout, zero)
            if scale.requires_grad or zero_point.requires_grad:
                raise NotImplementedError
            else:
                gs, gzp = None, None
        return gx, gs, gzp, None, None


def trt_fake_quant(x_f, scale, zero_point, qdesc):
    qmin, qmax = qdesc.qrange
    assert (
        abs(zero_point).sum() == 0
    ), "tensorrt only support symmetric quant, but zp={}".format(zero_point)
    if torch.cuda.is_available():
        if qdesc.is_perchannel:
            x_dq = fake_quant_kernel.quant_perchannel_forward(
                x_f, scale, zero_point, qmin, qmax, qdesc.ch_axis, 0
            )
        else:
            x_dq = fake_quant_kernel.quant_pertensor_forward(
                x_f, scale, zero_point, qmin, qmax, 0
            )
    else:
        x_q = torch.clamp((x_f / scale).round(), qmin, qmax)
        x_dq = x_q * scale
    return x_dq


def ort_fake_quant(x_f, scale, zero_point, qdesc):
    qmin, qmax = qdesc.qrange
    if torch.cuda.is_available():
        if qdesc.is_perchannel:
            x_dq = fake_quant_kernel.quant_perchannel_forward(
                x_f, scale, zero_point, qmin, qmax, qdesc.ch_axis, 0
            )
        else:
            x_dq = fake_quant_kernel.quant_pertensor_forward(
                x_f, scale, zero_point, qmin, qmax, 0
            )
    else:
        zp = zero_point.round()
        x_q = torch.clamp((x_f / scale).round() + zp, qmin, qmax)
        x_dq = (x_q - zp) * scale
    return x_dq


fake_quant_factory = {
    Backend.VIRTUAL: ort_fake_quant,
    Backend.ONNXRUNTIME: ort_fake_quant,
    Backend.TENSORRT: trt_fake_quant,
}


def trt_dqrange(scale, zero_point, qdesc):
    assert (
        abs(zero_point).sum() == 0
    ), "tensorrt only support symmetric quant, but zp={}".format(zero_point)
    qmin, qmax = qdesc.qrange
    lower = scale * qmin
    upper = scale * qmax
    return (lower, upper)


def ort_dqrange(scale, zero_point, qdesc):
    qmin, qmax = qdesc.qrange
    lower = (qmin - zero_point) * scale
    upper = (qmax - zero_point) * scale
    return (lower, upper)


fake_qrange_factory = {
    Backend.VIRTUAL: ort_dqrange,
    Backend.ONNXRUNTIME: ort_dqrange,
    Backend.TENSORRT: trt_dqrange,
}


# torch_fake_quant仅用作模型export to onnx使用
def torch_fake_quant(x_f, scale, zero_point, qdesc):
    # lower_bound, upper_bound = qdesc.qrange
    # set [0, 255] for quint and [-128, 127] for qint because onnx only support 8 bit
    if qdesc._type.startswith("uint"):
        lower_bound, upper_bound = (0, 255)
    else:
        lower_bound, upper_bound = (-128, 127)

    if scale.numel() > 1:  # perchannel
        ch_axis = np.argmax(list(scale.shape))
        scale = scale.reshape(-1).to(x_f.device)
        if torch.__version__.startswith("1.9"):  # fix bug in 1.9.x
            zero_point = zero_point.reshape(-1).long().to(x_f.device)
        else:
            zero_point = zero_point.reshape(-1).int().to(x_f.device)
        x_dq = torch.fake_quantize_per_channel_affine(
            x_f, scale, zero_point, ch_axis, lower_bound, upper_bound
        )
    elif scale.numel() == 1:  # pertensor
        scale = scale.item()
        if torch.__version__.startswith("1.9"):  # fix bug in 1.9.x
            zero_point = zero_point.long().item()
        else:
            zero_point = zero_point.int().item()
        x_dq = torch.fake_quantize_per_tensor_affine(
            x_f, scale, zero_point, lower_bound, upper_bound
        )
    else:
        raise TypeError("scale / zeropoint is not allowed to be an empty tensor")
    return x_dq
