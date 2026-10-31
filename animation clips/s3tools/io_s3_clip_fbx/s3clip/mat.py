"""Минимальная 4x4 / кватернионная математика без зависимостей.

Матрицы — плоские кортежи из 16 float, row-major (как mathutils.Matrix,
если читать по строкам). Кватернионы — кортежи (x, y, z, w), как в CLIP.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

Mat4 = Tuple[float, ...]
Quat = Tuple[float, float, float, float]
Vec3 = Tuple[float, float, float]


def identity() -> Mat4:
    return (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def mul(a: Mat4, b: Mat4) -> Mat4:
    o = [0.0] * 16
    for r in range(4):
        for c in range(4):
            o[r * 4 + c] = (
                a[r * 4 + 0] * b[0 * 4 + c]
                + a[r * 4 + 1] * b[1 * 4 + c]
                + a[r * 4 + 2] * b[2 * 4 + c]
                + a[r * 4 + 3] * b[3 * 4 + c]
            )
    return tuple(o)


def translation(v: Sequence[float]) -> Mat4:
    return (1.0, 0.0, 0.0, v[0], 0.0, 1.0, 0.0, v[1], 0.0, 0.0, 1.0, v[2], 0.0, 0.0, 0.0, 1.0)


def from_quat(q: Quat) -> Mat4:
    x, y, z, w = quat_normalize(q)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy), 0.0,
        2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx), 0.0,
        2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy), 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def rest_rotation(m: Mat4) -> Mat4:
    """Убирает трансляцию, оставляет поворот (масштаб считаем единичным)."""
    return (
        m[0], m[1], m[2], 0.0,
        m[4], m[5], m[6], 0.0,
        m[8], m[9], m[10], 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def to_translation(m: Mat4) -> Vec3:
    return (m[3], m[7], m[11])


def inverse_rigid(m: Mat4) -> Mat4:
    """Обратная матрица для жёсткого преобразования (поворот + сдвиг)."""
    r = (
        m[0], m[1], m[2],
        m[4], m[5], m[6],
        m[8], m[9], m[10],
    )
    # транспонируем поворот
    t = (
        r[0], r[3], r[6],
        r[1], r[4], r[7],
        r[2], r[5], r[8],
    )
    tx, ty, tz = m[3], m[7], m[11]
    nx = -(t[0] * tx + t[1] * ty + t[2] * tz)
    ny = -(t[3] * tx + t[4] * ty + t[5] * tz)
    nz = -(t[6] * tx + t[7] * ty + t[8] * tz)
    return (
        t[0], t[1], t[2], nx,
        t[3], t[4], t[5], ny,
        t[6], t[7], t[8], nz,
        0.0, 0.0, 0.0, 1.0,
    )


def to_quat(m: Mat4) -> Quat:
    """Матрица поворота → кватернион (x, y, z, w)."""
    m00, m01, m02 = m[0], m[1], m[2]
    m10, m11, m12 = m[4], m[5], m[6]
    m20, m21, m22 = m[8], m[9], m[10]
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m21 - m12) / s
        y = (m02 - m20) / s
        z = (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        w = (m21 - m12) / s
        x = 0.25 * s
        y = (m01 + m10) / s
        z = (m02 + m20) / s
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        w = (m02 - m20) / s
        x = (m01 + m10) / s
        y = 0.25 * s
        z = (m12 + m21) / s
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        w = (m10 - m01) / s
        x = (m02 + m20) / s
        y = (m12 + m21) / s
        z = 0.25 * s
    return quat_normalize((x, y, z, w))


def quat_normalize(q: Quat) -> Quat:
    n = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    if n == 0.0:
        return (0.0, 0.0, 0.0, 1.0)
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def quat_mul(a: Quat, b: Quat) -> Quat:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return quat_normalize((
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ))


def quat_dot(a: Quat, b: Quat) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def quat_slerp(a: Quat, b: Quat, t: float) -> Quat:
    d = quat_dot(a, b)
    if d < 0.0:
        b = (-b[0], -b[1], -b[2], -b[3])
        d = -d
    if d > 0.9995:
        lerped = (
            a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t,
            a[3] + (b[3] - a[3]) * t,
        )
        return quat_normalize(lerped)
    theta = math.acos(max(-1.0, min(1.0, d)))
    sin_theta = math.sin(theta)
    s0 = math.sin((1.0 - t) * theta) / sin_theta
    s1 = math.sin(t * theta) / sin_theta
    return quat_normalize(
        (
            a[0] * s0 + b[0] * s1,
            a[1] * s0 + b[1] * s1,
            a[2] * s0 + b[2] * s1,
            a[3] * s0 + b[3] * s1,
        )
    )
