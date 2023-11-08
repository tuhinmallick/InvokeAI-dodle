from inspect import isfunction
import math
from typing import Callable, Optional

import torch
import torch.nn.functional as F
from torch import nn, einsum
from einops import rearrange, repeat

from ldm.modules.diffusionmodules.util import checkpoint

import psutil

def exists(val):
    return val is not None


def uniq(arr):
    return{el: True for el in arr}.keys()


def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d


def max_neg_value(t):
    return -torch.finfo(t.dtype).max


def init_(tensor):
    dim = tensor.shape[-1]
    std = 1 / math.sqrt(dim)
    tensor.uniform_(-std, std)
    return tensor


# feedforward
class GEGLU(nn.Module):
    def __init__(self, dim_in, dim_out):
        super().__init__()
        self.proj = nn.Linear(dim_in, dim_out * 2)

    def forward(self, x):
        x, gate = self.proj(x).chunk(2, dim=-1)
        return x * F.gelu(gate)


class FeedForward(nn.Module):
    def __init__(self, dim, dim_out=None, mult=4, glu=False, dropout=0.):
        super().__init__()
        inner_dim = int(dim * mult)
        dim_out = default(dim_out, dim)
        project_in = nn.Sequential(
            nn.Linear(dim, inner_dim),
            nn.GELU()
        ) if not glu else GEGLU(dim, inner_dim)

        self.net = nn.Sequential(
            project_in,
            nn.Dropout(dropout),
            nn.Linear(inner_dim, dim_out)
        )

    def forward(self, x):
        return self.net(x)


def zero_module(module):
    """
    Zero out the parameters of a module and return it.
    """
    for p in module.parameters():
        p.detach().zero_()
    return module


def Normalize(in_channels):
    return torch.nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)


class LinearAttention(nn.Module):
    def __init__(self, dim, heads=4, dim_head=32):
        super().__init__()
        self.heads = heads
        hidden_dim = dim_head * heads
        self.to_qkv = nn.Conv2d(dim, hidden_dim * 3, 1, bias = False)
        self.to_out = nn.Conv2d(hidden_dim, dim, 1)

    def forward(self, x):
        b, c, h, w = x.shape
        qkv = self.to_qkv(x)
        q, k, v = rearrange(qkv, 'b (qkv heads c) h w -> qkv b heads c (h w)', heads = self.heads, qkv=3)
        k = k.softmax(dim=-1)
        context = torch.einsum('bhdn,bhen->bhde', k, v)
        out = torch.einsum('bhde,bhdn->bhen', context, q)
        out = rearrange(out, 'b heads c (h w) -> b (heads c) h w', heads=self.heads, h=h, w=w)
        return self.to_out(out)


class SpatialSelfAttention(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.in_channels = in_channels

        self.norm = Normalize(in_channels)
        self.q = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.k = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.v = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.proj_out = torch.nn.Conv2d(in_channels,
                                        in_channels,
                                        kernel_size=1,
                                        stride=1,
                                        padding=0)

    def forward(self, x):
        h_ = x
        h_ = self.norm(h_)
        q = self.q(h_)
        k = self.k(h_)
        v = self.v(h_)

        # compute attention
        b,c,h,w = q.shape
        q = rearrange(q, 'b c h w -> b (h w) c')
        k = rearrange(k, 'b c h w -> b c (h w)')
        w_ = torch.einsum('bij,bjk->bik', q, k)

        w_ = w_ * (int(c)**(-0.5))
        w_ = torch.nn.functional.softmax(w_, dim=2)

        # attend to values
        v = rearrange(v, 'b c h w -> b c (h w)')
        w_ = rearrange(w_, 'b i j -> b j i')
        h_ = torch.einsum('bij,bjk->bik', v, w_)
        h_ = rearrange(h_, 'b c (h w) -> b c h w', h=h)
        h_ = self.proj_out(h_)

        return x+h_

def get_mem_free_total(device):
    #only on cuda
    if not torch.cuda.is_available():
        return None
    stats = torch.cuda.memory_stats(device)
    mem_active = stats['active_bytes.all.current']
    mem_reserved = stats['reserved_bytes.all.current']
    mem_free_cuda, _ = torch.cuda.mem_get_info(device)
    mem_free_torch = mem_reserved - mem_active
    return mem_free_cuda + mem_free_torch


class CrossAttention(nn.Module):
    def __init__(self, query_dim, context_dim=None, heads=8, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads
        context_dim = default(context_dim, query_dim)

        self.scale = dim_head ** -0.5
        self.heads = heads

        self.to_q = nn.Linear(query_dim, inner_dim, bias=False)
        self.to_k = nn.Linear(context_dim, inner_dim, bias=False)
        self.to_v = nn.Linear(context_dim, inner_dim, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, query_dim),
            nn.Dropout(dropout)
        )

        self.mem_total_gb = psutil.virtual_memory().total // (1 << 30)

        self.cached_mem_free_total = None
        self.attention_slice_wrangler = None
        self.slicing_strategy_getter = None

    def set_attention_slice_wrangler(self, wrangler: Optional[Callable[[nn.Module, torch.Tensor, int, int, int], torch.Tensor]]):
        '''
        Set custom attention calculator to be called when attention is calculated
        :param wrangler: Callback, with args (module, suggested_attention_slice, dim, offset, slice_size),
        which returns either the suggested_attention_slice or an adjusted equivalent.
            `module` is the current CrossAttention module for which the callback is being invoked.
            `suggested_attention_slice` is the default-calculated attention slice
            `dim` is -1 if the attenion map has not been sliced, or 0 or 1 for dimension-0 or dimension-1 slicing.
                If `dim` is >= 0, `offset` and `slice_size` specify the slice start and length.

        Pass None to use the default attention calculation.
        :return:
        '''
        self.attention_slice_wrangler = wrangler

    def set_slicing_strategy_getter(self, getter: Optional[Callable[[nn.Module], tuple[int,int]]]):
        self.slicing_strategy_getter = getter

    def cache_free_memory_count(self, device):
        self.cached_mem_free_total = get_mem_free_total(device)
        print("free cuda memory: ", self.cached_mem_free_total)

    def clear_cached_free_memory_count(self):
        self.cached_mem_free_total = None

    def einsum_lowest_level(self, q, k, v, dim, offset, slice_size):
        # calculate attention scores
        attention_scores = einsum('b i d, b j d -> b i j', q, k)
        # calculate attention slice by taking the best scores for each latent pixel
        default_attention_slice = attention_scores.softmax(dim=-1, dtype=attention_scores.dtype)
        attention_slice_wrangler = self.attention_slice_wrangler
        if attention_slice_wrangler is not None:
            attention_slice = attention_slice_wrangler(self, default_attention_slice, dim, offset, slice_size)
        else:
            attention_slice = default_attention_slice

        return einsum('b i j, b j d -> b i d', attention_slice, v)

    def einsum_op_slice_dim0(self, q, k, v, slice_size):
        r = torch.zeros(q.shape[0], q.shape[1], v.shape[2], device=q.device, dtype=q.dtype)
        for i in range(0, q.shape[0], slice_size):
            end = i + slice_size
            r[i:end] = self.einsum_lowest_level(q[i:end], k[i:end], v[i:end], dim=0, offset=i, slice_size=slice_size)
        return r

    def einsum_op_slice_dim1(self, q, k, v, slice_size):
        r = torch.zeros(q.shape[0], q.shape[1], v.shape[2], device=q.device, dtype=q.dtype)
        for i in range(0, q.shape[1], slice_size):
            end = i + slice_size
            r[:, i:end] = self.einsum_lowest_level(q[:, i:end], k, v, dim=1, offset=i, slice_size=slice_size)
        return r

    def einsum_op_mps_v1(self, q, k, v):
        if q.shape[1] <= 4096:
            return self.einsum_lowest_level(q, k, v, None, None, None)
        slice_size = math.floor(2**30 / (q.shape[0] * q.shape[1]))
        return self.einsum_op_slice_dim1(q, k, v, slice_size)

    def einsum_op_mps_v2(self, q, k, v):
        if self.mem_total_gb > 8 and q.shape[1] <= 4096:
            return self.einsum_lowest_level(q, k, v, None, None, None)
        else:
            return self.einsum_op_slice_dim0(q, k, v, 1)

    def einsum_op_tensor_mem(self, q, k, v, max_tensor_mb):
        size_mb = q.shape[0] * q.shape[1] * k.shape[1] * q.element_size() // (1 << 20)
        if size_mb <= max_tensor_mb:
            return self.einsum_lowest_level(q, k, v, None, None, None)
        div = 1 << int((size_mb - 1) / max_tensor_mb).bit_length()
        if div <= q.shape[0]:
            return self.einsum_op_slice_dim0(q, k, v, q.shape[0] // div)
        return self.einsum_op_slice_dim1(q, k, v, max(q.shape[1] // div, 1))

    def einsum_op_cuda(self, q, k, v):
        # check if we already have a slicing strategy (this should only happen during cross-attention controlled generation)
        slicing_strategy_getter = self.slicing_strategy_getter
        if slicing_strategy_getter is not None:
            (dim, slice_size) = slicing_strategy_getter(self)
            if dim is not None:
                # print("using saved slicing strategy with dim", dim, "slice size", slice_size)
                if dim == 0:
                    return self.einsum_op_slice_dim0(q, k, v, slice_size)
                elif dim == 1:
                    return self.einsum_op_slice_dim1(q, k, v, slice_size)

        # fallback for when there is no saved strategy, or saved strategy does not slice
        mem_free_total = self.cached_mem_free_total or get_mem_free_total(q.device)
        # Divide factor of safety as there's copying and fragmentation
        return self.einsum_op_tensor_mem(q, k, v, mem_free_total / 3.3 / (1 << 20))


    def get_attention_mem_efficient(self, q, k, v):
        if q.device.type == 'cuda':
            #print("in get_attention_mem_efficient with q shape", q.shape, ", k shape", k.shape, ", free memory is", get_mem_free_total(q.device))
            return self.einsum_op_cuda(q, k, v)

        if q.device.type == 'mps':
            if self.mem_total_gb >= 32:
                return self.einsum_op_mps_v1(q, k, v)
            return self.einsum_op_mps_v2(q, k, v)

        # Smaller slices are faster due to L2/L3/SLC caches.
        # Tested on i7 with 8MB L3 cache.
        return self.einsum_op_tensor_mem(q, k, v, 32)

    def forward(self, x, context=None, mask=None):
        h = self.heads

        q = self.to_q(x)
        context = default(context, x)
        k = self.to_k(context) * self.scale
        v = self.to_v(context)
        del context, x

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (q, k, v))

        r = self.get_attention_mem_efficient(q, k, v)

        hidden_states = rearrange(r, '(b h) n d -> b n (h d)', h=h)
        return self.to_out(hidden_states)




class BasicTransformerBlock(nn.Module):
    def __init__(self, dim, n_heads, d_head, dropout=0., context_dim=None, gated_ff=True, checkpoint=True):
        super().__init__()
        self.attn1 = CrossAttention(query_dim=dim, heads=n_heads, dim_head=d_head, dropout=dropout)  # is a self-attention
        self.ff = FeedForward(dim, dropout=dropout, glu=gated_ff)
        self.attn2 = CrossAttention(query_dim=dim, context_dim=context_dim,
                                    heads=n_heads, dim_head=d_head, dropout=dropout)  # is self-attn if context is none
        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)
        self.norm3 = nn.LayerNorm(dim)
        self.checkpoint = checkpoint

    def forward(self, x, context=None):
        return checkpoint(self._forward, (x, context), self.parameters(), self.checkpoint)

    def _forward(self, x, context=None):
        x = x.contiguous() if x.device.type == 'mps' else x
        x += self.attn1(self.norm1(x.clone()))
        x += self.attn2(self.norm2(x.clone()), context=context)
        x += self.ff(self.norm3(x.clone()))
        return x


class SpatialTransformer(nn.Module):
    """
    Transformer block for image-like data.
    First, project the input (aka embedding)
    and reshape to b, t, d.
    Then apply standard transformer action.
    Finally, reshape to image
    """
    def __init__(self, in_channels, n_heads, d_head,
                 depth=1, dropout=0., context_dim=None):
        super().__init__()
        self.in_channels = in_channels
        inner_dim = n_heads * d_head
        self.norm = Normalize(in_channels)

        self.proj_in = nn.Conv2d(in_channels,
                                 inner_dim,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)

        self.transformer_blocks = nn.ModuleList(
            [
                BasicTransformerBlock(
                    inner_dim,
                    n_heads,
                    d_head,
                    dropout=dropout,
                    context_dim=context_dim,
                )
                for _ in range(depth)
            ]
        )

        self.proj_out = zero_module(nn.Conv2d(inner_dim,
                                              in_channels,
                                              kernel_size=1,
                                              stride=1,
                                              padding=0))

    def forward(self, x, context=None):
        # note: if no context is given, cross-attention defaults to self-attention
        b, c, h, w = x.shape
        x_in = x
        x = self.norm(x)
        x = self.proj_in(x)
        x = rearrange(x, 'b c h w -> b (h w) c')
        for block in self.transformer_blocks:
            x = block(x, context=context)
        x = rearrange(x, 'b (h w) c -> b c h w', h=h, w=w)
        x = self.proj_out(x)
        return x + x_in
