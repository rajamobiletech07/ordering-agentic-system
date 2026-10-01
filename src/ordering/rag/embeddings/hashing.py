"""Deterministic feature-hashing embedder (offline tests / CI; no model download, no network).

Signed hashing of word unigrams + bigrams + character trigrams, L2-normalised. Not semantic
like a neural model, but lexical overlap produces meaningful cosine similarity, which is
enough to exercise the full hybrid-retrieval pipeline deterministically.
"""

from __future__ import annotations

import hashlib
import math
import re
from itertools import pairwise

_TOKEN = re.compile(r"[a-z0-9]+")


def _features(text: str) -> list[str]:
    words = _TOKEN.findall(text.lower())
    feats = list(words)
    feats += [f"{a}_{b}" for a, b in pairwise(words)]
    for w in words:
        padded = f"#{w}#"
        feats += [padded[i : i + 3] for i in range(len(padded) - 2)]
    return feats


def hashing_vector(text: str, dims: int = 256) -> list[float]:
    vec = [0.0] * dims
    for feat in _features(text):
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
        idx, sign = h % dims, (1.0 if (h >> 63) & 1 else -1.0)
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
