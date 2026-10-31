#!/usr/bin/env python3
"""Проверка готовых .glb: что реально внутри файла.

    python tools\\verify_glb.py --glb "d:\\Temp\\sims3-blender4+\\s3_library\\glb\\loco_walk"

Печатает по файлу: размер, анимации, каналы, узлы, меши, скины — и вердикт:
    ок                   анимация есть, мешей нет (то, что нужно)
    без анимации         0 анимаций (пустой экспорт)
    попали меши          в файл уехала геометрия (экспорт в режиме «вся сцена»),
                         от этого файлы по мегабайту и больше
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from typing import Dict, List


def read_glb(path: str) -> Dict:
    with open(path, "rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"glTF":
            raise ValueError("не glTF")
        length, ctype = struct.unpack_from("<II", fh.read(8), 0)
        if ctype != 0x4E4F534A:
            raise ValueError("первый чанк не JSON")
        return json.loads(fh.read(length).decode("utf-8"))


def describe(path: str) -> Dict:
    data = read_glb(path)
    anims = data.get("animations", [])
    channels = sum(len(a.get("channels", [])) for a in anims)
    meshes = data.get("meshes", [])
    vertices = 0
    for mesh in meshes:
        for prim in mesh.get("primitives", []):
            pos = prim.get("attributes", {}).get("POSITION")
            if pos is not None:
                acc = data["accessors"][pos]
                vertices += int(acc.get("count", 0))
    mesh_names = [m.get("name", "?") for m in meshes[:6]]
    size = os.path.getsize(path)
    verdict = "ок"
    if not anims:
        verdict = "без анимации"
    elif meshes:
        verdict = "попали меши"
    return {
        "file": os.path.basename(path),
        "kb": size / 1024.0,
        "animations": len(anims),
        "channels": channels,
        "nodes": len(data.get("nodes", [])),
        "meshes": len(meshes),
        "vertices": vertices,
        "skins": len(data.get("skins", [])),
        "verdict": verdict,
        "mesh_names": mesh_names,
    }


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="Что внутри готовых .glb")
    ap.add_argument("--glb", nargs="+", required=True, help="папка(и) или файлы .glb")
    ap.add_argument("--quiet", action="store_true", help="только сводка")
    args = ap.parse_args()

    files: List[str] = []
    for item in args.glb:
        if os.path.isdir(item):
            for base, _dirs, names in os.walk(item):
                files += [os.path.join(base, f) for f in names if f.lower().endswith(".glb")]
        else:
            files.append(item)
    files.sort()
    if not files:
        print("не нашёл .glb")
        return 1

    rows = []
    broken = []
    for path in files:
        try:
            rows.append(describe(path))
        except Exception as exc:  # noqa: BLE001
            broken.append((os.path.basename(path), str(exc)))

    bad = [r for r in rows if r["verdict"] != "ок"]
    width = max((len(r["file"]) for r in rows), default=10)

    if not args.quiet:
        print("%-*s %8s %5s %7s %6s %6s %8s  %s"
              % (width, "файл", "КБ", "аним", "каналов", "узлов", "мешей", "вершин", "вердикт"))
        for r in rows[:40]:
            print("%-*s %8.0f %5d %7d %6d %6d %8d  %s"
                  % (width, r["file"], r["kb"], r["animations"], r["channels"],
                     r["nodes"], r["meshes"], r["vertices"], r["verdict"]))
        if len(rows) > 40:
            print("… и ещё %d файлов" % (len(rows) - 40))
        print()

    total_kb = sum(r["kb"] for r in rows)
    avg_kb = total_kb / len(rows) if rows else 0
    print("итого: %d файлов, %.1f МБ, в среднем %.0f КБ на файл" % (len(rows), total_kb / 1024.0, avg_kb))
    print("анимаций: %d файлов с 1+ анимацией" % len([r for r in rows if r["animations"] > 0]))
    multi = [r for r in rows if r["animations"] > 1]
    if multi:
        print("!! в файлах больше одной анимации (до %d) — экспорт шёл в режиме ACTIONS"
              " и тащил чужие экшены, файл раздут во столько же раз" % max(r["animations"] for r in multi))
        print("   лечится версией 1.9.4 (режим ACTIVE_ACTIONS): перепечь с --force")
    if any(r["meshes"] for r in rows):
        names = sorted({n for r in rows for n in r["mesh_names"]})
        print("!! в часть файлов попали МЕШИ (%d файлов, до %d вершин) — экспорт шёл"
              " в режиме «вся сцена», файлы раздуты"
              % (len([r for r in rows if r["meshes"]]), max(r["vertices"] for r in rows)))
        print("   меши в файлах: %s" % ", ".join(names[:8]))
        print("   это формы костей (сферы-виджеты) или меши тела: перепечь на 1.9.3 —")
        print("   новые версии не принимают файл с мешами и повторяют только по арматуре")
    if bad:
        print("!! не в порядке: %d" % len(bad))
        for r in bad[:10]:
            print("   %-40s %s" % (r["file"], r["verdict"]))
    for name, error in broken[:5]:
        print("!! %-40s %s" % (name, error))
    if avg_kb > 600 and not any(r["meshes"] for r in rows):
        print(".  файлы крупнее обычного (~250 КБ): похоже, печём без оптимизации каналов"
              " — это резервный режим, работает, но места занимает в 3-4 раза больше")
    return 0 if not bad and not broken else 2


if __name__ == "__main__":
    raise SystemExit(main())
