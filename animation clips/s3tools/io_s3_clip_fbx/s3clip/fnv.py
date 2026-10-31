"""FNV-хеши, которые использует The Sims 3.

Игра хеширует имена строк (джойнтов, акторов, шейпкеев) вариантом FNV-1,
а не FNV-1a: сначала умножение, потом XOR (см. s3py.helpers.FNV32).
Строка перед хешированием приводится к нижнему регистру.
"""

MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF

PRIME32 = 0x01000193
OFFSET32 = 0x811C9DC5

PRIME64 = 0x00000100000001B3
OFFSET64 = 0xCBF29CE484222325


def _bytes(name):
    return str(name).lower().encode("ascii", "replace")


def fnv1_32(name: str) -> int:
    h = OFFSET32
    for b in _bytes(name):
        h = (h * PRIME32) & MASK32
        h ^= b
        h &= MASK32
    return h


def fnv1_64(name: str) -> int:
    h = OFFSET64
    for b in _bytes(name):
        h = (h * PRIME64) & MASK64
        h ^= b
        h &= MASK64
    return h


def morph_key(name: str) -> int:
    """Ключ морф-трека: FNV64 от имени, урезанный до 32 бит."""
    return fnv1_64(name) & MASK32
