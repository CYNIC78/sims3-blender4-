#!/usr/bin/env python3
"""Замер локомоции прямо из .animation (без Blender).

Для каждого клипа показывает: длительность, сколько корень реально проходит,
с какой скоростью, замкнут ли цикл, на какой высоте корень и совпадает ли
набор костей с эталонным клипом.

Зачем: чтобы подставлять клипы другого возраста (взрослый/ребёнок/малыш)
по числам, а не на глаз. Ключевой вопрос — authored ли клип в абсолютных
метрах под свой рост или в «своих» единицах.

    python3 tools/measure_locomotion.py "/путь/к/клипам" [--ref a_male_walk]
    python3 tools/measure_locomotion.py a_male_walk.animation c_walk.animation
    python3 tools/measure_locomotion.py папка --json out.json

Скорость клипа нужна движку: time_scale = желаемая_скорость / скорость_клипа,
это убирает скольжение стоп без IK.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from io_s3_clip_fbx.s3clip import read_clip                      # noqa: E402
from io_s3_clip_fbx.s3clip.fnv import fnv1_32                   # noqa: E402

ROOT_NAMES = ("b__ROOT_bind__", "b__ROOT__")
ROOT_KEYS = [(n, fnv1_32(n)) for n in ROOT_NAMES]


def _keys(track) -> List[Tuple[int, Tuple[float, float, float]]]:
    return sorted(track.positions, key=lambda kp: kp[0])


def _planar(a, b) -> float:
    return math.hypot(b[0] - a[0], b[2] - a[2])


def measure(path: str) -> dict:
    anim = read_clip(path)
    duration = max(1e-6, anim.frame_end * anim.frame_duration)

    root_track = None
    root_name = ""
    for name, key in ROOT_KEYS:
        if key in anim.tracks and anim.tracks[key].positions:
            root_track, root_name = anim.tracks[key], name
            break

    if root_track is None:  # запасной путь: джойнт с самой большой горизонтальной пробежкой
        best = 0.0
        for track in anim.tracks.values():
            keys = _keys(track)
            if len(keys) < 2:
                continue
            travel = sum(_planar(keys[i][1], keys[i + 1][1]) for i in range(len(keys) - 1))
            if travel > best:
                best, root_track, root_name = travel, track, "auto:%08X" % track.key

    keys = _keys(root_track) if root_track else []
    path_len = sum(_planar(keys[i][1], keys[i + 1][1]) for i in range(len(keys) - 1)) if keys else 0.0
    net = _planar(keys[0][1], keys[-1][1]) if keys else 0.0
    heights = [k[1][1] for k in keys] if keys else [0.0]

    return {
        "clip": os.path.basename(path).split("%%")[0],
        "file": path,
        "duration_s": round(duration, 3),
        "frames": anim.frame_end,
        "fps": round(anim.fps, 2),
        "bones": len(anim.tracks),
        "root_joint": root_name,
        "root_keys": len(keys),
        "travel_m": round(path_len, 3),
        "net_m": round(net, 3),
        "speed_mps": round(path_len / duration, 3),
        "is_cycle": bool(net < 0.02),
        "root_y": (round(min(heights), 3), round(max(heights), 3)),
        "bone_hashes": sorted(anim.tracks),
    }


def compare(ref: dict, other: dict) -> str:
    a, b = set(ref["bone_hashes"]), set(other["bone_hashes"])
    only_ref, only_other = len(a - b), len(b - a)
    ratio_speed = other["speed_mps"] / ref["speed_mps"] if ref["speed_mps"] else 0.0
    ratio_height = ((other["root_y"][0] / ref["root_y"][0]) if ref["root_y"][0] else 0.0)
    return ("кости: общих %d, только в эталоне %d, только в этом %d | "
            "скорость x%.2f | высота корня x%.2f"
            % (len(a & b), only_ref, only_other, ratio_speed, ratio_height))


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер локомоции из .animation")
    ap.add_argument("paths", nargs="+", help="файлы .animation или папки")
    ap.add_argument("--ref", default="", help="имя эталонного клипа (например a_male_walk)")
    ap.add_argument("--json", default="", help="куда сохранить результат")
    args = ap.parse_args()

    files: List[str] = []
    for p in args.paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.endswith(".animation")]
        else:
            files.append(p)

    rows = []
    for f in files:
        try:
            rows.append(measure(f))
        except Exception as exc:                                   # noqa: BLE001
            print("!! %s: %s" % (os.path.basename(f), exc), file=sys.stderr)

    if not rows:
        print("нечего мерить")
        return 1

    ref = None
    if args.ref:
        ref = next((r for r in rows if args.ref in r["clip"]), None)

    width = max(len(r["clip"]) for r in rows)
    print("%-*s %8s %7s %6s %8s %8s %-8s %s"
          % (width, "клип", "длит,с", "кадров", "костей", "путь,м", "скор,м/с", "цикл", "корень Y"))
    for r in rows:
        print("%-*s %8.3f %7d %6d %8.3f %8.3f %-8s %s"
              % (width, r["clip"], r["duration_s"], r["frames"], r["bones"],
                 r["travel_m"], r["speed_mps"], "да" if r["is_cycle"] else "нет", r["root_y"]))
        if ref and r is not ref:
            print("%-*s   ↳ %s" % (width, "", compare(ref, r)))

    if args.json:
        payload = [{k: v for k, v in r.items() if k != "bone_hashes"} for r in rows]
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("\njson: %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
