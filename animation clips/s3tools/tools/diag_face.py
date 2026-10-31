"""Диагностика костей лица: чем ТВОЙ риг отличается от ванильного.

Задача: понять, почему центральные кости губ уезжают вверх, хотя на ванильном
риге всё нормально. Скрипт ничего не двигает и не бейкает — только читает
rest-данные и сравнивает.

--------------------------------------------------------------------------
1) СНЯТЬ ДАННЫЕ С РИГА (в Blender)

    blender -b "d:\\...\\s3_rig\\G9Female_1.2.blend" -P tools\\diag_face.py -- --dump my.json
    blender -b "vanilla_afemale.blend"              -P tools\\diag_face.py -- --dump van.json

    --rig имя      какую арматуру брать, если их несколько
    --all          снять ВСЕ кости (по умолчанию — только лицо и его родители)

2) СРАВНИТЬ ДВА ФАЙЛА (обычный Python, Blender не нужен)

    python tools\\diag_face.py --compare my.json van.json

    Печатает, какие кости лица есть в одном риге и нет в другом, и у общих —
    отличия rest-позы: смещение головки относительно родителя, длина, roll,
    масштаб, родитель. Плюс отдельно — кости, у которых масштаб не единичный
    (это важно: в клипе трансляция задана в пространстве родителя).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

FACE_KEYWORDS = (
    "lip", "jaw", "mouth", "tongue", "teeth", "tooth", "cheek", "nose",
    "brow", "eyelid", "eye", "chin", "head", "face", "ear",
)


# --------------------------------------------------------------------------
# 1. СНЯТЬ ДАННЫЕ (внутри Blender)
# --------------------------------------------------------------------------
def dump_from_blender(args) -> int:
    try:
        import bpy  # noqa: F401
    except ImportError:
        print("этот режим запускается внутри Blender (-P diag_face.py)")
        return 2

    import bpy
    from mathutils import Matrix

    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if not arms:
        print("!! в файле нет арматуры")
        return 1
    if args.rig:
        picked = [a for a in arms if a.name == args.rig]
        if not picked:
            picked = [a for a in arms if args.rig.lower() in a.name.lower()]
        if not picked:
            print("!! не нашёл арматуру %r, есть: %s" % (args.rig, [a.name for a in arms]))
            return 1
        arm = picked[0]
    else:
        arm = max(arms, key=lambda a: len(a.data.bones))

    bones = list(arm.data.bones)
    print("арматура: %s — %d костей" % (arm.name, len(bones)))

    wanted = set()
    if args.all:
        wanted = {b.name for b in bones}
    else:
        for bone in bones:
            if any(k in bone.name.lower() for k in FACE_KEYWORDS):
                # сама кость и все её предки — чтобы видеть, где сидит масштаб
                node = bone
                while node is not None:
                    if node.name not in wanted:
                        wanted.add(node.name)
                    node = node.parent

    data = {"armature": arm.name, "blend": os.path.basename(bpy.data.filepath),
            "blender": bpy.app.version_string, "bones": {}}

    for bone in bones:
        if bone.name not in wanted:
            continue
        loc, rot, scale = bone.matrix_local.decompose()
        # смещение головки в пространстве РОДИТЕЛЯ — именно его и применяет клип
        if bone.parent is not None:
            parent_space = bone.parent.matrix_local.inverted() @ bone.matrix_local
            off_loc, off_rot, off_scale = parent_space.decompose()
            child = True
        else:
            off_loc, off_rot, off_scale = loc, rot, scale
            child = False
        # ограничители и драйверы на позе — они могут перебивать бейк
        pose_bone = arm.pose.bones.get(bone.name)
        constraints = []
        if pose_bone is not None:
            constraints = ["%s(%s)" % (c.type, c.name) for c in pose_bone.constraints]
        drivers = []
        ad = getattr(bone, "animation_data", None)
        if ad is not None:
            for fc in ad.drivers:
                drivers.append(fc.data_path)

        data["bones"][bone.name] = {
            "parent": bone.parent.name if bone.parent else "",
            "children": len(bone.children),
            "length": round(bone.length, 6),
            "roll": round(getattr(bone, "roll", 0.0), 6),
            "head": [round(v, 6) for v in (loc)],
            "tail": [round(v, 6) for v in bone.tail_local],
            "parent_offset": [round(v, 6) for v in off_loc],
            "parent_quat": [round(v, 6) for v in off_rot],
            "parent_scale": [round(v, 6) for v in off_scale],
            "use_connect": bool(bone.use_connect),
            "inherit_rotation": bool(bone.use_inherit_rotation),
            "inherit_scale": getattr(bone, "inherit_scale", "FULL"),
            "deform": bool(bone.use_deform),
            "constraints": constraints,
            "drivers": drivers,
        }

    out = args.dump
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("снято %d костей -> %s" % (len(data["bones"]), out))

    scaled = [n for n, b in data["bones"].items()
              if max(abs(s - 1.0) for s in b["parent_scale"]) > 1e-4]
    if scaled:
        print("!! костей с масштабом != 1: %d" % len(scaled))
        for name in scaled[:12]:
            print("     %-28s масштаб %s" % (name, data["bones"][name]["parent_scale"]))
    return 0


# --------------------------------------------------------------------------
# 1b. ЧТО КЛИП ДЕЛАЕТ С КОСТЯМИ ЛИЦА (обычный Python, Blender не нужен)
# --------------------------------------------------------------------------
def analyse_clip(args) -> int:
    """Читает .animation и показывает, что записано в кривых костей лица.

    Имена костей берутся из слепка рига (--names face_my.json): в клипе лежат
    только хеши, поэтому без списка имён их не расшифровать.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from io_s3_clip_fbx.s3clip import clipfile
    from io_s3_clip_fbx.s3clip.fnv import fnv1_32

    with open(args.names, encoding="utf-8") as fh:
        rig = json.load(fh)
    wanted = {}
    for name in rig["bones"]:
        if args.all or any(k in name.lower() for k in FACE_KEYWORDS):
            wanted[fnv1_32(name)] = name

    anim = None
    for order in ("offset_scale", "scale_offset"):
        try:
            anim = clipfile.read_clip(args.clip, order)
            break
        except Exception:  # noqa: BLE001 — пробуем другой порядок полей
            continue
    if anim is None:
        print("!! не смог прочитать клип %s" % args.clip)
        return 1

    print("клип   : %s" % os.path.basename(args.clip))
    print("кадров : %d (fps %.0f)" % (anim.frame_end + 1, anim.fps))
    print("кривых : %d (костей с данными: %d)"
          % (len(anim.tracks), sum(1 for t in anim.tracks.values() if not t.is_morph_only)))
    print("имена  : из %s (%d костей лица в слепке)" % (os.path.basename(args.names), len(wanted)))
    print()

    rows = []
    found = 0
    for key, track in anim.tracks.items():
        name = wanted.get(key)
        if name is None or track.is_morph_only:
            continue
        found += 1
        pos = ""
        if track.positions:
            base = track.positions[0][1]
            worst = max(track.positions, key=lambda kv: _dist(kv[1], base))
            peak = _dist(worst[1], base)
            axis = "Y" if abs(worst[1][1] - base[1]) == max(
                abs(worst[1][i] - base[i]) for i in range(3)) else (
                "X" if abs(worst[1][0] - base[0]) >= abs(worst[1][2] - base[2]) else "Z")
            pos = "%4.2f по %s" % (peak, axis)
        rot = ""
        if track.orientations:
            q0 = track.orientations[0][1]
            peak_angle, peak_axis = 0.0, ""
            for _frame, q in track.orientations:
                dot = abs(sum(q[i] * q0[i] for i in range(4)))
                angle = 2.0 * math.acos(min(1.0, max(-1.0, dot)))
                if angle > peak_angle:
                    peak_angle = angle
                    # ось поворота в системе координат самой кости
                    inv = (q0[3], -q0[0], -q0[1], -q0[2])
                    dq = _quat_mul(inv, q)
                    norm = math.sqrt(sum(dq[i] ** 2 for i in range(3))) or 1.0
                    ax = [abs(dq[i]) / norm for i in range(3)]
                    peak_axis = "XYZ"[ax.index(max(ax))]
            rot = "%4.1f° вокруг %s" % (math.degrees(peak_angle), peak_axis)
        rows.append((name, len(track.positions), pos, len(track.orientations), rot))

    if not rows:
        print("в этом клипе нет кривых для костей лица из слепка")
        return 0

    rows.sort(key=lambda r: r[0])
    print("%-26s %5s %-16s %5s %s" % ("кость", "поз", "макс. смещение", "пов", "макс. поворот"))
    for name, npos, pos, nrot, rot in rows:
        print("%-26s %5d %-16s %5d %s" % (name, npos, pos, nrot, rot))
    print("\nвсего костей лица с данными: %d" % found)
    return 0


def _quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


# --------------------------------------------------------------------------
# 2. СРАВНИТЬ ДВА СЛЕПКА (обычный Python)
# --------------------------------------------------------------------------
def _fmt(vec) -> str:
    return "(%7.3f %7.3f %7.3f)" % tuple(vec)


def _dist(a, b) -> float:
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def compare(args) -> int:
    with open(args.compare[0], encoding="utf-8") as fh:
        mine = json.load(fh)
    with open(args.compare[1], encoding="utf-8") as fh:
        ref = json.load(fh)

    a, b = mine["bones"], ref["bones"]
    print("мой : %s (%s, %s)" % (mine.get("blend"), mine.get("armature"), mine.get("blender")))
    print("ван: %s (%s, %s)" % (ref.get("blend"), ref.get("armature"), ref.get("blender")))
    print("костей в слепке: %d и %d" % (len(a), len(b)))

    # ограничители/драйверы на костях лица — даже если rest совпал
    dirty = {n: b for n, b in a.items()
             if b.get("constraints") or b.get("drivers")}
    if dirty:
        print("\nна моём риге у костей лица есть ограничители/драйверы:")
        for name, info in sorted(dirty.items()):
            print("  %-26s %s%s" % (name,
                                    ", ".join(info.get("constraints") or []) or "-",
                                    "  драйверов: %d" % len(info["drivers"]) if info.get("drivers") else ""))

    only_mine = sorted(set(a) - set(b))
    only_ref = sorted(set(b) - set(a))
    if only_mine:
        print("\nтолько у меня (%d): %s" % (len(only_mine), ", ".join(only_mine)))
    if only_ref:
        print("только в ванили (%d): %s" % (len(only_ref), ", ".join(only_ref)))

    rows = []
    for name in sorted(set(a) & set(b)):
        ba, bb = a[name], b[name]
        problems = []
        if ba["parent"] != bb["parent"]:
            problems.append("родитель: %s vs %s" % (ba["parent"] or "-", bb["parent"] or "-"))
        shift = _dist(ba["parent_offset"], bb["parent_offset"])
        if shift > 0.001:
            problems.append("сдвиг от родителя %.3f" % shift)
        dlen = ba["length"] - bb["length"]
        if abs(dlen) > 0.001:
            problems.append("длина %+.3f" % dlen)
        droll = (ba["roll"] - bb["roll"] + 3.14159265) % 6.2831853 - 3.14159265
        if abs(droll) > 0.01:
            problems.append("roll %+.1f°" % (droll * 57.29578))
        dscale = max(abs(ba["parent_scale"][i] - bb["parent_scale"][i]) for i in range(3))
        if dscale > 1e-4:
            problems.append("масштаб %s vs %s" % (_fmt(ba["parent_scale"]), _fmt(bb["parent_scale"])))
        if ba["inherit_rotation"] != bb["inherit_rotation"]:
            problems.append("inherit_rotation %s vs %s" % (ba["inherit_rotation"], bb["inherit_rotation"]))
        if ba["use_connect"] != bb["use_connect"]:
            problems.append("connect %s vs %s" % (ba["use_connect"], bb["use_connect"]))
        if ba.get("constraints"):
            problems.append("ОГРАНИЧИТЕЛИ: %s" % ", ".join(ba["constraints"]))
        if ba.get("drivers"):
            problems.append("ДРАЙВЕРЫ: %d" % len(ba["drivers"]))
        if problems:
            rows.append((name, shift, problems))

    if not rows:
        print("\nотличий по общим костям нет — причина не в rest-данных рига")
        return 0

    rows.sort(key=lambda r: -r[1])
    print("\nОТЛИЧИЯ (сначала самые смещённые от родителя)")
    print("%-26s %8s  %s" % ("кость", "сдвиг", "что не так"))
    for name, shift, problems in rows:
        print("%-26s %8.3f  %s" % (name, shift, "; ".join(problems)))

    lips = [r for r in rows if "lip" in r[0].lower()]
    if lips:
        print("\nГУБЫ (то, из-за чего всё началось)")
        for name, shift, problems in lips:
            print("  %-26s сдвиг %.3f | %s" % (name, shift, "; ".join(problems)))
            print("     мой: %s  ван: %s" % (_fmt(a[name]["parent_offset"]), _fmt(b[name]["parent_offset"])))
            print("     мой: roll %+.3f длина %.3f | ван: roll %+.3f длина %.3f"
                  % (a[name]["roll"], a[name]["length"], b[name]["roll"], b[name]["length"]))
    return 0


def _script_argv():
    """Аргументы ТОЛЬКО наши.

    Внутри Blender в sys.argv лежит вся командная строка (`-b`, `-P`, пути...),
    поэтому берём то, что идёт после `--`. Запущенный обычным Python — берём как есть.
    """
    argv = list(sys.argv)
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    try:
        import bpy  # noqa: F401
    except ImportError:
        return argv[1:]
    return []  # внутри Blender без `--` своих аргументов нет


def main() -> int:
    parser = argparse.ArgumentParser(description="диагностика костей лица")
    parser.add_argument("--dump", metavar="JSON", help="снять данные рига в JSON (внутри Blender)")
    parser.add_argument("--compare", nargs=2, metavar=("МОЙ", "ВАНИЛЬНЫЙ"),
                        help="сравнить два слепка")
    parser.add_argument("--rig", default="", help="имя арматуры в сцене")
    parser.add_argument("--clip", metavar="ANIMATION",
                        help="показать, что клип записывает в кривые костей лица")
    parser.add_argument("--names", metavar="JSON", help="слепок рига (для режима --clip)")
    parser.add_argument("--all", action="store_true", help="снять все кости, не только лицо")
    # parse_known_args: лишнее (например, случайно перетащенный в окно .animation)
    # не должно ронять диагностику
    args, extra = parser.parse_known_args(_script_argv())
    if extra:
        print("(лишние аргументы пропущены: %s)" % " ".join(extra[:3]))

    if args.compare:
        return compare(args)
    if args.clip:
        if not args.names:
            print("для --clip нужен --names face_my.json (откуда взять имена костей)")
            return 1
        return analyse_clip(args)
    if args.dump:
        return dump_from_blender(args)
    if not _script_argv():
        print("внутри Blender запускай так:")
        print("  blender -b \"риг.blend\" --factory-startup -P diag_face.py -- --dump face_my.json")
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
