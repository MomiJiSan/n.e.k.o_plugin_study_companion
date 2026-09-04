"""Versioned PCG32 implementation shared by deterministic v0.1 simulations."""

from __future__ import annotations

from collections.abc import MutableSequence
from typing import TypeVar

from .contracts import RNG_ALGORITHM

_T = TypeVar("_T")
_MASK_32 = (1 << 32) - 1
_MASK_64 = (1 << 64) - 1
_MULTIPLIER = 6_364_136_223_846_793_005


class PCG32:
    """Small deterministic RNG using the reference PCG-XSH-RR transition."""

    algorithm = RNG_ALGORITHM

    def __init__(self, seed: int, sequence: int = 54) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 or seed > _MASK_64:
            raise ValueError("seed must be an unsigned 64-bit integer")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0 or sequence > _MASK_64:
            raise ValueError("sequence must be an unsigned 64-bit integer")
        self._state = 0
        self._increment = ((sequence << 1) | 1) & _MASK_64
        self.next_uint32()
        self._state = (self._state + seed) & _MASK_64
        self.next_uint32()

    def next_uint32(self) -> int:
        old_state = self._state
        self._state = (old_state * _MULTIPLIER + self._increment) & _MASK_64
        xor_shifted = (((old_state >> 18) ^ old_state) >> 27) & _MASK_32
        rotation = old_state >> 59
        return ((xor_shifted >> rotation) | (xor_shifted << ((-rotation) & 31))) & _MASK_32

    def randbelow(self, bound: int) -> int:
        if isinstance(bound, bool) or not isinstance(bound, int) or not 1 <= bound <= 1 << 32:
            raise ValueError("bound must be an integer from 1 to 2**32")
        threshold = ((1 << 32) - bound) % bound
        while True:
            value = self.next_uint32()
            if value >= threshold:
                return value % bound

    def shuffle(self, values: MutableSequence[_T]) -> None:
        for index in range(len(values) - 1, 0, -1):
            swap_index = self.randbelow(index + 1)
            values[index], values[swap_index] = values[swap_index], values[index]

    def export_state(self) -> dict[str, int | str]:
        return {
            "algorithm": self.algorithm,
            "state": self._state,
            "increment": self._increment,
        }
