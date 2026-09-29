"""Four compact sequence classifiers; padding cannot affect valid representations."""

from __future__ import annotations

import torch
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence

ARCHITECTURES = ("gru", "lstm", "tcn", "transformer")


class SequenceClassifier(nn.Module):
    def __init__(
        self,
        architecture: str,
        vocabulary_size: int,
        *,
        hidden: int = 32,
        max_steps: int = 128,
        numeric_features: int = 4,
    ):
        super().__init__()
        if architecture not in ARCHITECTURES or hidden < 4 or hidden % 4 or max_steps < 1:
            raise ValueError("unknown architecture or invalid hidden/max_steps")
        self.settings = dict(
            architecture=architecture,
            vocabulary_size=vocabulary_size,
            hidden=hidden,
            max_steps=max_steps,
            numeric_features=numeric_features,
        )
        self.architecture = architecture
        self.embedding = nn.Embedding(vocabulary_size, 12, padding_idx=0)
        self.project = nn.Linear(12 + numeric_features, hidden)
        if architecture in ("gru", "lstm"):
            self.encoder = (nn.GRU if architecture == "gru" else nn.LSTM)(
                hidden, hidden, batch_first=True
            )
        elif architecture == "tcn":
            self.encoder = nn.ModuleList(
                [
                    nn.Conv1d(hidden, hidden, 3, dilation=d)
                    for d in ((1, 2, 4) if numeric_features == 4 else (1, 2, 4, 8, 16, 32))
                ]
            )
        else:
            self.position = nn.Embedding(max_steps, hidden)
            layer = nn.TransformerEncoderLayer(
                hidden, nhead=4, dim_feedforward=hidden * 2, dropout=0.0, batch_first=True
            )
            self.encoder = nn.TransformerEncoder(layer, num_layers=1, enable_nested_tensor=False)
        self.head = nn.Linear(hidden, 1)

    def forward(self, tokens, numeric, lengths):
        positions = torch.arange(tokens.shape[1], device=tokens.device)
        valid = positions.unsqueeze(0) < lengths.unsqueeze(1)
        # Mask inputs too: callers may hold arbitrary values in padded slots.
        tokens = tokens.masked_fill(~valid, 0)
        numeric = numeric.masked_fill(~valid.unsqueeze(-1), 0)
        x = self.project(torch.cat([self.embedding(tokens), numeric], dim=-1))
        if self.architecture in ("gru", "lstm"):
            packed = pack_padded_sequence(x, lengths.cpu(), batch_first=True, enforce_sorted=False)
            _, h = self.encoder(packed)
            if self.architecture == "lstm":
                h = h[0]
            representation = h[-1]
        elif self.architecture == "tcn":
            x = x.transpose(1, 2)
            for layer in self.encoder:
                # Left padding makes every convolution causal.
                x = torch.relu(layer(nn.functional.pad(x, (2 * layer.dilation[0], 0))))
            representation = x.transpose(1, 2)[torch.arange(len(x), device=x.device), lengths - 1]
        else:
            x = x + self.position(positions).unsqueeze(0)
            x = self.encoder(x, src_key_padding_mask=~valid)
            representation = (x * valid.unsqueeze(-1)).sum(1) / lengths.unsqueeze(1)
        return self.head(representation).squeeze(-1)


def device_for(requested: str) -> torch.device:
    if requested == "auto":
        requested = (
            "cuda"
            if torch.cuda.is_available()
            else ("mps" if torch.backends.mps.is_available() else "cpu")
        )
    device = torch.device(requested)
    # Fail on unavailable accelerators instead of silently changing the experiment.
    (torch.ones(1, device=device) * 2).cpu()
    return device
