"""Pure-python ядро для чтения CLIP (.animation) The Sims 3.

Модуль не зависит от bpy — его можно тестировать обычным Python 3.
"""

from .clipfile import ClipAnim, ClipTrack, ClipReadError, read_clip, parse_clip_bytes
from .fnv import fnv1_32, fnv1_64
from . import bake

__all__ = [
    "ClipAnim",
    "ClipTrack",
    "ClipReadError",
    "read_clip",
    "parse_clip_bytes",
    "fnv1_32",
    "fnv1_64",
    "bake",
]
