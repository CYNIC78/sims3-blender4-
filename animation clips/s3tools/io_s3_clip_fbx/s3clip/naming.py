"""Имена костей: Sims 3 (`b__L_UpperArm__`) ⇄ Blender-симметрия (`UpperArm.L`).

Зачем это здесь
---------------
Аддон сопоставляет джойнты клипа с костями рига по FNV-хешу имени. Если
переименовать кости в `.L/.R` (чтобы работала симметрия при правке весов),
хеши перестанут совпадать. Поэтому:

  * `name_variants()` даёт набор «правдоподобных» имён для кости, и хеш-карта
    регистрирует их все — импорт/экспорт продолжает работать при любом из
    двух вариантов имён;
  * конвертация в `.L/.R` сохраняет исходное имя в кастомном свойстве
    (`bone["s3_name"]`), так что обратное переименование точное.

Модуль без зависимостей от bpy.
"""

from __future__ import annotations

from typing import Optional, Tuple

# префиксы Sims 3 → сторона
_SIMS_PREFIXES = (
    ("b__l_", "L"),
    ("b__r_", "R"),
    ("b__left", "L"),
    ("b__right", "R"),
)

MIRROR_SUFFIXES = (".l", ".r")
S3_NAME_PROP = "s3_name"


def _split_mirror(name: str) -> Optional[Tuple[str, str]]:
    low = name.lower()
    for suffix, side in MIRROR_SUFFIXES:
        if low.endswith(suffix):
            return name[: -len(suffix)], side.upper()
    return None


def s3_side(name: str) -> Optional[str]:
    """'L'/'R', если имя похоже на Sims-джойнт с указанием стороны."""
    low = name.lower()
    for prefix, side in _SIMS_PREFIXES:
        if low.startswith(prefix):
            return side
    return None


def is_mirror_name(name: str) -> bool:
    return _split_mirror(name) is not None


def to_mirror(name: str) -> Optional[str]:
    """`b__L_UpperArm__` → `UpperArm.L`, `b__LeftEye__` → `Eye.L`.

    Возвращает None, если имя не похоже на Sims-джойнт со стороной.
    """
    low = name.lower()
    for prefix, side in _SIMS_PREFIXES:
        if not low.startswith(prefix):
            continue
        base = name[len(prefix):]
        # b__L_UpperArm__ → UpperArm ; b__R_Toe_slot → Toe_slot
        base = base.rstrip("_")
        if not base:
            return None
        return "%s.%s" % (base, side)
    return None


def from_mirror(name: str, original: Optional[str] = None) -> Optional[str]:
    """`UpperArm.L` → `b__L_UpperArm__` (если сохранено исходное имя — оно).

    Внимание: без сохранённого оригинала форма угадывается (с двойным
    подчёркиванием в конце). Для точного восстановления используйте
    `bone["s3_name"]`.
    """
    if original:
        return original
    split = _split_mirror(name)
    if split is None:
        return None
    base, side = split
    if not base:
        return None
    return "b__%s_%s__" % (side, base)


def name_variants(name: str) -> Tuple[str, ...]:
    """Все имена, по которым кость может зваться в клипе/риге.

    Нужны только для хеш-карты: чем больше вариантов, тем меньше шансов, что
    клип «не найдёт» кость после переименований.
    """
    out = []

    def add(value: Optional[str]):
        if value and value not in out:
            out.append(value)

    add(name)

    split = _split_mirror(name)
    if split is not None:
        base, side = split
        add("b__%s_%s__" % (side, base))
        add("b__%s_%s" % (side, base))
        long_side = "Left" if side == "L" else "Right"
        add("b__%s%s__" % (long_side, base))
        add("b__%s%s" % (long_side, base))
        add("b__%s%s" % (long_side.lower(), base))
        add("%s_%s" % (long_side, base))
        add("%s.%s" % (base, side))
    else:
        mirrored = to_mirror(name)
        if mirrored:
            add(mirrored)

    # имя без префикса/хвоста b__...__
    if name.startswith("b__") and name.endswith("__") and len(name) > 5:
        add(name[3:-2])
    elif not name.startswith("b__"):
        candidate = "b__%s__" % name
        if len(name) <= 60:
            add(candidate)

    return tuple(out)
