"""Почему импортированный из .glb экшен «не работает» на риге (Blender 4.4+).

В Blender 4.4 появились слоты действий (action slots). Экшен, импортированный
из .glb, приходит со своим слотом, и если просто перекинуть его на свою
арматуру, он «назначается», но ничего не двигает: слот указывает на другой ID.

Этот скрипт:
  • показывает слоты экшена и сколько в нём кривых;
  • проверяет, что пути кривых ссылаются на существующие кости;
  • назначает экшен арматуре вместе с правильным слотом.

Запуск из Blender (Scripting → Run Script) или из консоли:

    blender -b "риг.blend" -P tools/fix_action_slot.py -- --arm G9Female --action "a_male_walk"
    blender -b "риг.blend" -P tools/fix_action_slot.py -- --check --action "a_male_walk"

Без --action берётся первый экшен, у которого есть кривые.
"""

from __future__ import annotations

import os
import re
import sys

import bpy

BONE_PATH = re.compile(r'pose\.bones\["([^"]+)"\]')


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = {"arm": "", "action": "", "check": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--arm" and i + 1 < len(argv):
            out["arm"] = argv[i + 1]
            i += 2
        elif arg == "--action" and i + 1 < len(argv):
            out["action"] = argv[i + 1]
            i += 2
        elif arg == "--check":
            out["check"] = True
            i += 1
        else:
            i += 1
    return out


def pick_armature(name: str):
    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if not armatures:
        return None
    if name:
        for arm in armatures:
            if arm.name == name or arm.name.startswith(name):
                return arm
        print("  !! арматура «%s» не найдена, беру первую" % name)
    # та, у которой больше костей (обычно это скелет, а не болванка)
    return max(armatures, key=lambda a: len(a.data.bones))


def pick_action(name: str):
    actions = [a for a in bpy.data.actions if a.fcurves]
    if not actions:
        return None
    if name:
        for act in actions:
            if act.name == name or act.name.startswith(name):
                return act
        print("  !! экшен «%s» не найден" % name)
    return actions[0]


def main() -> int:
    args = parse_args()
    print("\n===== СЛОТЫ ДЕЙСТВИЯ И СОВМЕСТИМОСТЬ С РИГОМ =====")
    print("Blender: %s" % bpy.app.version_string)

    arm = pick_armature(args["arm"])
    act = pick_action(args["action"])
    if arm is None:
        print("!! в файле нет арматуры")
        return 1
    if act is None:
        print("!! нет экшенов с кривыми (нечего проверять)")
        return 1

    bones = {b.name for b in arm.data.bones}
    print("арматура: %s (%d костей)" % (arm.name, len(bones)))
    print("экшен   : %s (%d кривых)" % (act.name, len(act.fcurves)))

    slots = getattr(act, "slots", None)
    if slots is None:
        print("слоты   : этой версии Blender слоты не нужны (до 4.4)")
    else:
        print("слоты   : %d" % len(slots))
        for slot in slots:
            print("    name=%-28s id_type=%s" % (slot.name_display or "?", slot.target_id_type))

    missing = []
    used = 0
    for fc in act.fcurves:
        match = BONE_PATH.search(fc.data_path)
        if not match:
            continue
        used += 1
        if match.group(1) not in bones:
            missing.append(match.group(1))
    print("кости в кривых: %d уникальных путей, отсутствуют в риге: %d"
          % (used, len(set(missing))))
    for name in sorted(set(missing))[:10]:
        print("    НЕТ КОСТИ: %s" % name)

    if args["check"]:
        if missing:
            print("вердикт: экшен от другого скелета — имена костей не совпадают")
        else:
            print("вердикт: экшен совместим с ригом; если «не двигается», дело в слоте")
        print("===== КОНЕЦ =====\n")
        return 0

    ad = arm.animation_data or arm.animation_data_create()
    ad.action = act
    if hasattr(ad, "action_slot"):
        try:
            if act.slots and ad.action_slot is None:
                ad.action_slot = act.slots[0]
        except Exception as exc:  # noqa: BLE001
            print("  !! слот не назначился: %s" % exc)
    print("назначено: %s → %s (слот: %s)"
          % (act.name, arm.name, getattr(ad, "action_slot", "нет в этой версии")))

    scene = bpy.context.scene
    scene.frame_set(int(scene.frame_start))
    moved_ok, moved_count = _moved(arm)
    scene.frame_set(int(scene.frame_start) + 5)
    _ok2, moved_count2 = _moved(arm)
    print("проверка: сдвинутых костей на кадре %d: %d, на следующем: %d"
          % (scene.frame_current, moved_count, moved_count2))
    print("вердикт: %s" % ("экшен двигает риг" if moved_ok else
                          "экшен НЕ двигает риг — пришли вывод, разберём"))
    print("===== КОНЕЦ =====\n")
    return 0


def _moved(arm) -> tuple[bool, int]:
    """Сдвинулась ли поза: сравниваем matrix_basis каждой кости с единичной."""
    from mathutils import Matrix

    identity = Matrix.Identity(4)
    moved = 0
    for bone in arm.pose.bones:
        delta = 0.0
        for row in range(4):
            for col in range(4):
                delta += abs(bone.matrix_basis[row][col] - identity[row][col])
        if delta > 1e-4:
            moved += 1
    return moved > 0, moved


main()
