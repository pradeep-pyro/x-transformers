import math
from typing import Callable

import torch
import torch.nn.functional as F
from torch import nn

from einops import rearrange, pack, unpack

# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

# sampling helpers

def top_k(logits, thres = 0.9):
    k = math.ceil((1 - thres) * logits.shape[-1])
    val, ind = logits.topk(k, dim = -1)
    probs = torch.full_like(logits, float('-inf'))
    probs.scatter_(2, ind, val)
    return probs

# schedules

def linear_schedule(t):
    return 1 - t

def cosine_schedule(t):
    """ https://arxiv.org/abs/2202.04200 """
    return torch.cos(t * math.pi / 2)

# wrapper class

class NonAutoregressiveWrapper(nn.Module):
    """
    https://arxiv.org/abs/1904.09324
    https://arxiv.org/abs/2202.04200
    """

    def __init__(
        self,
        net,
        *,
        mask_id,
        steps = 18,
        schedule_fn: Callable = cosine_schedule
    ):
        super().__init__()
        self.net = net
        self.mask_id = mask_id

        self.max_seq_len = net.max_seq_len
        self.steps = steps
        self.schedule_fn = schedule_fn
    
    @torch.no_grad()
    def generate(
        self,
        batch_size = None,
        start_temperature = 1.,
        filter_thres = None,
        **kwargs
    ):
        sample_one = not exists(batch_size)
        batch_size = default(batch_size, 1)

        device = next(self.net.parameters()).device

        was_training = self.net.training
        self.net.eval()

        times = torch.linspace(0., 1., self.steps + 1)

        # sequence starts off as all masked

        shape = (batch_size, self.max_seq_len)

        seq = torch.full(shape, self.mask_id, device = device)
        mask = torch.full(shape, True, device = device)

        # slowly demask

        all_mask_num_tokens = (self.schedule_fn(times[1:]) * self.max_seq_len).long()

        for mask_num_tokens, steps_until_x0 in zip(all_mask_num_tokens.tolist(), reversed(range(self.steps))):
            logits = self.net(seq, **kwargs)

            if exists(filter_thres):
                logits = top_k(logits, filter_thres)

            temperature = start_temperature * (steps_until_x0 / self.steps)

            probs = (logits / max(temperature, 1e-3)).softmax(dim = -1)

            packed_probs, packed_shape = pack([probs], '* c')

            sampled_ids = torch.multinomial(packed_probs, 1)

            sampled_ids = rearrange(sampled_ids, '... 1 -> ...')
            sampled_ids, = unpack(sampled_ids, packed_shape, '*')

            seq = torch.where(mask, sampled_ids, seq)

            scores = (1 - probs).gather(2, rearrange(sampled_ids, 'b n -> b n 1'))
            scores = rearrange(scores, 'b n 1 -> b n')

            if mask_num_tokens == 0:
                pass

            scores = scores.masked_fill(~mask, -torch.finfo(scores.dtype).max)
            mask_indices = scores.topk(mask_num_tokens, dim = -1).indices
            mask = torch.zeros_like(scores, dtype = torch.bool).scatter(1, mask_indices, True)
            seq = seq.masked_fill(mask, self.mask_id)

        self.net.train(was_training)

        if sample_one:
            seq = rearrange(seq, '1 n -> n')

        return seq

    def forward(
        self,
        x,
        **kwargs
    ):
        b, n, device = *x.shape, x.device
        assert n == self.max_seq_len

        rand_times = torch.empty(b, device = device).uniform_(0, 1)
        rand_probs = self.schedule_fn(rand_times)
        num_tokens_mask = (rand_probs * n).clamp(min = 1.)

        batched_randperm = torch.rand((b, n), device = device).argsort(dim = -1).float()
        mask = batched_randperm < rearrange(num_tokens_mask, 'b -> b 1')

        masked = torch.where(mask, self.mask_id, x)

        logits = self.net(masked, **kwargs)

        loss = F.cross_entropy(
            logits[mask],
            x[mask]
        )

        return loss
