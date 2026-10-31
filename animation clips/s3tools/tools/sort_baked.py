#!/usr/bin/env python3
"""Сортировка УЖЕ ИСПЕЧЁННЫХ .glb по категориям: файлы не трогаем, пишем манифест.

Нужен, когда библиотеки пекутся одним заходом в Blender (панель → «Один файл» + glTF +
«Клипов на файл»), а разложить по смыслу надо потом. Читает только заголовки .glb
(JSON-чанк), имена клипов внутри и раскладывает их по категориям плана.

    python tools\\sort_baked.py --glb "d:\\Temp\\s3_library\\glb" --out "d:\\Temp\\s3_library"

Что получится:
    library_sorted.json  — категория → {клип: файл .glb}
    library_sorted.md    — тот же разбор таблицей, для чтения
    anim_index.json      — плоский индекс клип → файл (его удобно грузить в Godot)

Дополнительно показывает, какие файлы .glb нужны для каждой категории: так видно, сколько
библиотек держать на диске, а сколько подгружать по требованию.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import struct
import sys
from typing import Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from classify_library import classify  # noqa: E402

BUILD_ORDER = (
    "loco_walk", "loco_run", "loco_turn", "loco_step",
    "idle", "sit_transition", "gesture", "emote", "talk",
    "sleep", "work_leisure", "eat", "hygiene", "social", "dance",
    "locomotion", "other",
)

GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A


def read_glb_animations(path: str) -> List[str]:
    """Имена анимаций из JSON-чанка .glb. Без зависимостей."""
    with open(path, "rb") as fh:
        header = fh.read(12)
        if len(header) < 12 or header[:4] != GLB_MAGIC:
            raise ValueError("не .glb: %s" % os.path.basename(path))
        length, chunk_type = struct.unpack_from("<II", fh.read(8), 0)
        if chunk_type != CHUNK_JSON:
            raise ValueError("первый чанк не JSON: %s" % os.path.basename(path))
        payload = fh.read(length)
    data = json.loads(payload.decode("utf-8"))
    names = [a.get("name", "") for a in data.get("animations", [])]
    return [n for n in names if n]


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="Разложить клипы внутри готовых .glb по категориям")
    ap.add_argument("--glb", required=True, help="папка с .glb (или файлы)")
    ap.add_argument("--out", default="", help="куда писать манифесты (по умолчанию рядом с --glb)")
    ap.add_argument("--keep-only", action="store_true",
                    help="в манифест попадут только клипы, которые мы берём в игру")
    args = ap.parse_args()

    glb_dir = os.path.abspath(args.glb)
    if os.path.isdir(glb_dir):
        files = []
        for base, _dirs, names in os.walk(glb_dir):   # включая подпапки категорий
            files += [os.path.join(base, f) for f in names
                      if f.lower().endswith((".glb", ".gltf"))]
        files.sort()
    else:
        files = [os.path.abspath(glb_dir)]
        glb_dir = os.path.dirname(files[0])
    out_dir = os.path.abspath(args.out) if args.out else glb_dir

    if not files:
        print("в %s нет .glb" % glb_dir)
        return 1

    by_category: Dict[str, Dict[str, str]] = collections.defaultdict(dict)
    flat: Dict[str, str] = {}
    per_package: Dict[str, collections.Counter] = {}
    dropped: collections.Counter = collections.Counter()
    total = 0

    for path in files:
        name = os.path.basename(path)
        rel = os.path.relpath(path, glb_dir).replace("\\", "/")
        try:
            clips = read_glb_animations(path)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print("!! %s — %s" % (name, exc))
            continue
        per_package[name] = collections.Counter()
        for clip in clips:
            total += 1
            info = classify(clip)
            per_package[name][info["category"]] += 1
            if args.keep_only and not info["keep"]:
                dropped[info["reason"]] += 1
                continue
            by_category[info["category"]][clip] = rel
            flat[clip] = rel

    if not total:
        print("в .glb не нашлось ни одной анимации")
        return 1

    order = sorted(by_category, key=lambda c: (BUILD_ORDER.index(c) if c in BUILD_ORDER else 99, c))
    print("файлов .glb: %d, клипов внутри: %d, категорий: %d\n" % (len(per_package), total, len(order)))
    header = "%-16s %6s  %s"
    print(header % ("категория", "клипов", "в каких файлах (шт.)"))
    for cat in order:
        packages = collections.Counter(by_category[cat].values())
        sample = ", ".join("%s×%d" % (p, n) for p, n in packages.most_common(3))
        print("%-16s %6d  %s" % (cat, len(by_category[cat]), sample))
    if dropped:
        print("\nне берём (--keep-only):")
        for reason, count in dropped.most_common():
            print("  %-52s %5d" % (reason, count))

    json_path = os.path.join(out_dir, "library_sorted.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"clips": total, "categories": by_category}, fh, ensure_ascii=False, indent=2)

    index_path = os.path.join(out_dir, "anim_index.json")
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(flat, fh, ensure_ascii=False, indent=1)

    md_path = os.path.join(out_dir, "library_sorted.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("# Что лежит в испечённых библиотеках\n\n")
        fh.write("Всего клипов: **%d** в %d файлах .glb.\n\n" % (total, len(per_package)))
        fh.write("| категория | клипов | файлы |\n|---|---|---|\n")
        for cat in order:
            packages = collections.Counter(by_category[cat].values())
            fh.write("| %s | %d | %s |\n" % (
                cat, len(by_category[cat]),
                ", ".join("%s×%d" % (p, n) for p, n in packages.most_common(5))))
        fh.write("\n## Полный список\n")
        for cat in order:
            fh.write("\n### %s (%d)\n\n" % (cat, len(by_category[cat])))
            for clip, package in sorted(by_category[cat].items()):
                fh.write("- `%s` → %s\n" % (clip, package))

    print("\nманифесты:")
    print("  %s" % json_path)
    print("  %s" % index_path)
    print("  %s" % md_path)
    print("\nдальше: переложить .glb в godot_project\\animations\\ и глянуть library_sorted.md —")
    print("какие библиотеки нужны боту сразу, а какие держать на диске")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
