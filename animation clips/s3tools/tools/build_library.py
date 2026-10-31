#!/usr/bin/env python3
"""Сортировка .animation по категориям + конвертация в библиотеки .glb одним прогоном.

Windows-пример (пути как есть, в кавычках):

    python tools/build_library.py ^
      --clips  "d:\\Temp\\sims3-blender4+\\animation clips" ^
      --out    "d:\\Temp\\s3_library" ^
      --blender "D:\\PortablePrograms\\blender-4.4.3-windows-x64\\blender.exe" ^
      --rig    "d:\\Temp\\sims3-blender4+\\s3_rig\\G9Female_1.2.blend" ^
      --chunk-size 100

Только разложить, без Blender (2 секунды):

    python tools/build_library.py --clips "d:\\Temp\\sims3-blender4+\\animation clips" \
        --out "d:\\Temp\\s3_library" --dry-run

Быстрая проверка на настоящих данных (5 клипов из локомоции):

    python tools/build_library.py ... --cats loco_walk --limit 5

Что делает:
    1) проверяет пути (blender, риг, папка клипов) и версию Blender;
    2) раскладывает .animation по категориям плана (ссылки, при неудаче — копии);
    3) по каждой категории печёт .glb кусками по --chunk-size клипов;
    4) пишет glb_library.json и печатает, что импортировать в Godot.

Исходные файлы не меняются и не удаляются.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from classify_library import classify  # noqa: E402

BUILD_ORDER = (
    "loco_walk", "loco_run", "loco_turn", "loco_step",
    "idle", "sit_transition", "gesture", "emote", "talk",
    "sleep", "work_leisure", "eat", "hygiene", "social", "dance",
    "locomotion", "other",
)

# категории, которые нужны первыми: их печём в первую очередь
FIRST_WAVE = ("loco_walk", "loco_run", "loco_turn", "loco_step",
              "idle", "sit_transition", "gesture", "emote", "talk")


# ---------------------------------------------------------------- утилиты
def setup_console() -> None:
    """Русский текст в cmd.exe/PowerShell не должен ронять скрипт."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def win_path(value: str) -> str:
    """Принять путь как есть (обратные слэши и пробелы допустимы)."""
    if not value:
        return value
    return os.path.normpath(os.path.expanduser(value))


def decode(raw: bytes) -> str:
    """Blender на Windows пишет консоль в cp866/cp1251 — читаем без падений."""
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def find_clips(root: str, recursive: bool = True) -> List[str]:
    if recursive:
        found: List[str] = []
        for base, _dirs, files in os.walk(root):
            found += [os.path.join(base, f) for f in files if f.lower().endswith(".animation")]
        return sorted(found)
    return sorted(
        os.path.join(root, f) for f in os.listdir(root) if f.lower().endswith(".animation")
    )


def clip_name(path: str) -> str:
    return os.path.basename(path).split("%%")[0]


def place(src: str, dst: str, mode: str) -> None:
    if os.path.exists(dst):
        return
    if mode in ("link", "auto"):
        try:
            os.link(src, dst)
            return
        except (OSError, NotImplementedError):
            if mode == "link":
                pass  # не получилось — просто копируем
    shutil.copy2(src, dst)


CLI_NAME = "s3clip_cli.py"


def find_cli(explicit: str = "") -> Optional[str]:
    """Найти s3clip_cli.py: рядом с tools, на уровень выше, в текущей папке, в ~/sims3-clip-fbx."""
    candidates = []
    if explicit:
        candidates.append(win_path(explicit))
    candidates += [
        os.path.join(HERE, CLI_NAME),
        os.path.join(HERE, os.pardir, CLI_NAME),
        os.path.join(HERE, os.pardir, os.pardir, CLI_NAME),
        os.path.join(os.getcwd(), CLI_NAME),
        os.path.join(os.path.expanduser("~"), "sims3-clip-fbx", CLI_NAME),
    ]
    for path in candidates:
        path = os.path.normpath(os.path.abspath(path))
        if os.path.isfile(path):
            return path
    return None


def has_addon_package(cli_path: str) -> bool:
    return os.path.isdir(os.path.join(os.path.dirname(cli_path), "io_s3_clip_fbx"))


def preflight(args: argparse.Namespace) -> bool:
    ok = True
    print("проверка путей:")
    print("  клипы   : %s" % args.clips)
    if not os.path.isdir(args.clips):
        print("    !! папки нет")
        ok = False
    if not args.dry_run:
        print("  blender : %s" % args.blender)
        blender_found = shutil.which(args.blender) or os.path.isfile(args.blender)
        if not blender_found:
            print("    !! не найден (проверь путь, кавычки нужны)")
            ok = False
        else:
            try:
                out = subprocess.run([args.blender, "--version"], capture_output=True, timeout=60)
                version = decode(out.stdout).strip().splitlines()
                print("    %s" % (version[0] if version else "версия неизвестна"))
            except (OSError, subprocess.SubprocessError) as exc:
                print("    !! не запускается: %s" % exc)
                ok = False
        print("  риг     : %s" % args.rig)
        if not os.path.isfile(args.rig):
            print("    !! файла нет (нужен именно .blend с арматурой Sims 3)")
            ok = False
    if not args.dry_run:
        cli = find_cli(args.addon)
        if cli is None:
            print("  аддон   : !! не найден %s" % CLI_NAME)
            print("            нужен файл %s и папка io_s3_clip_fbx рядом с ним" % CLI_NAME)
            print("            проще всего: распаковать весь sims3-clip-fbx и запускать из него")
            ok = False
        else:
            print("  аддон   : %s" % cli)
            if not has_addon_package(cli):
                print("    !! рядом нет папки io_s3_clip_fbx — это и есть аддон, без неё никак")
                ok = False
    print("  отчёт   : %s" % args.out)
    return ok


def progress(done_clips: int, total_clips: int, started: float) -> str:
    """Прогресс по КЛИПАМ, а не по категориям: категории очень разные по размеру."""
    elapsed = time.time() - started
    if done_clips <= 0 or elapsed <= 0:
        return "0/%d клипов" % total_clips
    rate = done_clips / elapsed
    left = total_clips - done_clips
    eta = left / rate if rate > 0 else 0
    return ("%d/%d клипов (%.0f%%), прошло %s, осталось ~%s"
            % (done_clips, total_clips, 100.0 * done_clips / max(1, total_clips),
               human_time(elapsed), human_time(eta)))


def human_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d с" % seconds
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return "%d мин %02d с" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    return "%d ч %02d мин" % (hours, minutes)


# ---------------------------------------------------------------- Blender
def check_category(folder: str, names: List[str], avg_kb: float) -> None:
    """Быстрая проверка: анимация внутри есть, мешей нет, размер вменяемый."""
    sys.path.insert(0, HERE)
    try:
        from verify_glb import describe
    except Exception:  # noqa: BLE001
        return
    empty = 0
    with_mesh = 0
    for name in names[:50]:
        try:
            info = describe(os.path.join(folder, name))
        except Exception:  # noqa: BLE001
            continue
        if not info["animations"]:
            empty += 1
        if info["meshes"]:
            with_mesh += 1
    if empty:
        print("     !! %d файл(ов) без анимации — см. 0_diagnose.cmd" % empty)
    if with_mesh:
        print("     !! в файлы попали МЕШИ: экспорт шёл в режиме «вся сцена», файлы раздуты")
    if avg_kb > 600 and not with_mesh:
        print("     .  крупнее обычного (~250 КБ): печём без оптимизации каналов —")
        print("        рабочий резервный режим, просто в 3-4 раза больше места")


def run_blender(args: argparse.Namespace, clips_dir: str, out_dir: str, name: str) -> bool:
    cli = find_cli(args.addon) or os.path.join(HERE, os.pardir, CLI_NAME)
    cmd = [
        args.blender, "-b", args.rig, "--factory-startup",
        "--python", os.path.normpath(cli), "--",
        "-i", clips_dir, "-o", out_dir, "--overwrite",
    ]
    if getattr(args, "separate", False):
        cmd += ["-m", "separate", "-f", "gltf"]
    else:
        cmd += ["-m", "single", "-f", "gltf", "--file-name", name]
        if args.chunk_size > 0:
            cmd += ["--chunk-size", str(args.chunk_size)]
    if args.armature:
        cmd += ["--rig", args.armature]
    if args.positions:
        cmd += ["--positions", args.positions]
    if args.motion_scale != 1.0:
        cmd += ["--motion-scale", str(args.motion_scale)]
    if args.no_ground_snap:
        cmd.append("--no-ground-snap")
    if args.animated_only:
        cmd.append("--animated-only")
    if args.deform_bones_only:
        cmd.append("--deform-bones-only")
    if args.extra:
        cmd += args.extra.split()

    printable = " ".join('"%s"' % c if " " in c else c for c in cmd)
    print("   $ %s" % printable)
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print("     !! таймаут %d с — увеличь --timeout" % args.timeout)
        return False
    except OSError as exc:
        print("     !! Blender не запустился: %s" % exc)
        return False

    stdout = decode(proc.stdout or b"")
    stderr = decode(proc.stderr or b"")
    useful = [line for line in stdout.splitlines()
              if any(k in line for k in ("готово", "ок", "ошиб", "Error", "Traceback", "клип"))]
    for line in useful[-4:]:
        print("     " + line.strip())

    if proc.returncode != 0 or not useful:
        if proc.returncode != 0:
            print("     !! blender вернул код %d" % proc.returncode)
        else:
            print("     !! blender ничего не сообщил об успехе — показываю весь вывод")
        dump = (stdout.strip() + "\n" + stderr.strip()).strip().splitlines()
        for line in dump[:40]:
            print("     " + line[:200])
        if len(dump) > 40:
            print("     … всего строк: %d" % len(dump))
        return False
    print("     готово за %.1f с" % (time.time() - started))
    return True


# ---------------------------------------------------------------- main
def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(
        description="Разложить .animation по категориям и испечь .glb",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--clips", required=True, help="папка с .animation")
    ap.add_argument("--out", required=True, help="рабочая папка (сюда пойдут clips/ и glb/)")
    ap.add_argument("--blender", default="blender", help="путь к blender.exe")
    ap.add_argument("--rig", default="", help=".blend с арматурой Sims 3")
    ap.add_argument("--armature", default="", help="имя арматуры (по умолчанию — первая в файле)")
    ap.add_argument("--addon", default="", help="путь к s3clip_cli.py (если скрипт лежит отдельно)")
    ap.add_argument("--separate", action="store_true",
                    help="врассыпную: один клип = один .glb, папка на категорию "
                         "(по умолчанию — библиотеками по --chunk-size)")
    ap.add_argument("--chunk-size", type=int, default=100, help="клипов на один .glb (не для --separate)")
    ap.add_argument("--cats", default="", help="через запятую: только эти категории")
    ap.add_argument("--limit", type=int, default=0, help="не больше N клипов на категорию (для пробы)")
    ap.add_argument("--mode", choices=("auto", "link", "copy"), default="auto",
                    help="как раскладывать файлы: auto — ссылки, иначе копии")
    ap.add_argument("--extra", default="", help="доп. аргументы аддону строкой "
                                               "(пиши через =, например --extra=\"--positions root\")")
    ap.add_argument("--animated-only", action="store_true",
                    help="выгружать только анимированные кости (файлы в 3-4 раза меньше)")
    ap.add_argument("--deform-bones-only", action="store_true",
                    help="только деформирующие кости, без служебных")
    ap.add_argument("--positions", choices=("motion", "root", "all", "none"), default="",
                    help="какие трансляции брать из клипа: motion (по умолчанию аддона), "
                         "root (только корень), all (как в клипе, абсолютно), none (только повороты)")
    ap.add_argument("--motion-scale", type=float, default=1.0,
                    help="множитель амплитуд движения (режим motion)")
    ap.add_argument("--no-ground-snap", action="store_true",
                    help="не прижимать персонажа к полу после бейка")
    ap.add_argument("--dry-run", action="store_true", help="только разложить, без Blender")
    ap.add_argument("--force", action="store_true", help="перепечь категории, даже если .glb уже есть")
    ap.add_argument("--timeout", type=int, default=3600, help="таймаут одного вызова Blender, с")
    ap.add_argument("--no-recursive", action="store_true")
    args = ap.parse_args()

    args.clips = win_path(args.clips)
    args.out = win_path(args.out)
    args.rig = win_path(args.rig)
    if args.blender not in ("blender", "blender.exe"):
        args.blender = win_path(args.blender)

    if not preflight(args):
        print("\nпоправь пути и запусти снова (пути с пробелами — в кавычках)")
        return 2

    out_root = args.out
    if os.path.abspath(out_root).lower().startswith(os.path.abspath(args.clips).lower() + os.sep):
        print("\n!! папка результата лежит ВНУТРИ папки с клипами — так нельзя:")
        print("   при следующем запуске скрипт найдёт свои же копии. Вынеси --out наружу.")
        return 2
    sorted_dir = os.path.join(out_root, "clips")
    glb_dir = os.path.join(out_root, "glb")
    os.makedirs(sorted_dir, exist_ok=True)
    os.makedirs(glb_dir, exist_ok=True)

    files = find_clips(args.clips, not args.no_recursive)
    if not files:
        print("\nв %s нет .animation" % args.clips)
        return 1

    wanted = {c.strip() for c in args.cats.split(",") if c.strip()}
    buckets: Dict[str, List[str]] = {}
    skipped = 0
    for path in files:
        info = classify(clip_name(path))
        if not info["keep"] or (wanted and info["category"] not in wanted):
            skipped += 1
            continue
        buckets.setdefault(info["category"], []).append(path)

    kept = sum(len(v) for v in buckets.values())
    print("\nклипов найдено: %d, отобрано: %d, пропущено: %d" % (len(files), kept, skipped))
    order = sorted(buckets, key=lambda c: (BUILD_ORDER.index(c) if c in BUILD_ORDER else 99, c))
    for cat in order:
        print("  %-16s %4d%s" % (cat, len(buckets[cat]),
                                 "   ← первая волна" if cat in FIRST_WAVE else ""))

    for cat in order:
        cat_dir = os.path.join(sorted_dir, cat)
        os.makedirs(cat_dir, exist_ok=True)
        for path in buckets[cat]:
            place(path, os.path.join(cat_dir, os.path.basename(path)), args.mode)
    print("\nразложено в: %s" % sorted_dir)

    if args.dry_run:
        print("\n--dry-run: Blender не запускаю")
        return 0

    produced: Dict[str, List[str]] = {}
    failed: List[str] = []
    started_all = time.time()
    total = len(order)
    total_clips = sum(len(buckets[c]) for c in order)
    done_clips = 0
    for index, cat in enumerate(order, 1):
        if args.separate:
            cat_out_probe = os.path.join(glb_dir, cat)
            existing = sorted(f for f in os.listdir(cat_out_probe) if f.endswith(".glb")) \
                if os.path.isdir(cat_out_probe) else []
        else:
            existing = sorted(f for f in os.listdir(glb_dir)
                              if f.startswith(cat + "_") and f.endswith(".glb"))
        if existing and not args.force:
            produced[cat] = existing
            print("\n[%d/%d] %s — уже есть (%d файл(ов)), пропускаю (--force чтобы перепечь)"
                  % (index, total, cat, len(existing)))
            done_clips += len(buckets[cat])
            continue

        count = len(buckets[cat])
        src_dir = os.path.join(sorted_dir, cat)
        if args.limit > 0 and count > args.limit:
            # проба: отдельная папка с первыми N клипами, чтобы не печь всё
            probe_dir = os.path.join(out_root, ".probe", cat)
            if os.path.isdir(probe_dir):
                shutil.rmtree(probe_dir)
            os.makedirs(probe_dir)
            for path in buckets[cat][: args.limit]:
                place(path, os.path.join(probe_dir, os.path.basename(path)), args.mode)
            src_dir = probe_dir
            count = args.limit
        print("\n[%d/%d] %s — %d клипов | %s"
              % (index, total, cat, count, progress(done_clips, total_clips, started_all)))

        if args.separate:
            # врассыпную: один клип — один .glb, своя папка на категорию
            cat_out = os.path.join(glb_dir, cat)
            os.makedirs(cat_out, exist_ok=True)
            before = set(os.listdir(cat_out))
            ok = run_blender(args, src_dir, cat_out, cat)
            made = sorted(f for f in set(os.listdir(cat_out)) - before if f.endswith(".glb"))
            if not ok or not made:
                failed.append(cat)
                print("     !! не получилось: %s" % cat)
                continue
            sizes = [os.path.getsize(os.path.join(cat_out, f)) for f in made]
            if sizes and max(sizes) < 2000:
                failed.append(cat)
                print("     !! все файлы пустые (макс %d Б) — экспорт ничего не выдал." % max(sizes))
                print("        запусти 0_diagnose.cmd и пришли вывод: там видно, на каком шаге рвётся")
                break
            produced[cat] = made
            done_clips += len(buckets[cat])
            total_size = sum(sizes)
            avg_kb = (total_size / len(sizes) / 1024.0) if sizes else 0
            print("     файлов %d, всего %.1f МБ, в среднем %.0f КБ на клип"
                  % (len(made), total_size / (1024.0 * 1024.0), avg_kb))
            check_category(cat_out, made, avg_kb)
            continue

        before = set(os.listdir(glb_dir))
        ok = run_blender(args, src_dir, glb_dir, cat)
        made = sorted(f for f in set(os.listdir(glb_dir)) - before
                      if f.startswith(cat) and f.endswith(".glb"))
        if not ok or not made:
            failed.append(cat)
            print("     !! не получилось: %s" % cat)
            continue
        sizes = [os.path.getsize(os.path.join(glb_dir, f)) for f in made]
        if sizes and max(sizes) < 2000:
            failed.append(cat)
            print("     !! все файлы пустые (макс %d Б) — экспорт ничего не выдал." % max(sizes))
            print("        запусти 0_diagnose.cmd и пришли вывод: там видно, на каком шаге рвётся")
            break
        produced[cat] = made
        done_clips += len(buckets[cat])
        total_size = 0
        for f in made:
            size = os.path.getsize(os.path.join(glb_dir, f))
            total_size += size
            print("     %-28s %7.1f КБ" % (f, size / 1024.0))
        print("     итого по категории: %.1f МБ" % (total_size / (1024.0 * 1024.0)))
        check_category(glb_dir, made, total_size / len(made) / 1024.0 if made else 0)
        if args.limit > 0 and len(buckets[cat]) > args.limit:
            print("     (проба: --limit %d, в категории всего %d клипов)"
                  % (args.limit, len(buckets[cat])))

    manifest_path = os.path.join(out_root, "glb_library.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump({
            "clips_found": len(files),
            "clips_kept": kept,
            "chunk_size": args.chunk_size,
            "libraries": produced,
            "failed": failed,
        }, fh, ensure_ascii=False, indent=2)

    total_files = sum(len(v) for v in produced.values())
    total_bytes = 0
    for cat, names in produced.items():
        base = os.path.join(glb_dir, cat) if args.separate else glb_dir
        for f in names:
            full = os.path.join(base, f)
            if os.path.exists(full):
                total_bytes += os.path.getsize(full)
    print("\nготово за %.0f с: категорий %d, файлов .glb %d, %.1f МБ"
          % (time.time() - started_all, len(produced), total_files, total_bytes / (1024.0 * 1024.0)))
    if failed:
        print("не получились: %s" % ", ".join(failed))
        print("перезапусти тот же вызов — готовые категории пропустятся (или --cats для нужных)")
    print("манифест: %s" % manifest_path)
    print("\nдальше: скопировать %s\\*.glb в godot_project\\animations\\ и запустить тесты Godot"
          % glb_dir)
    return 0 if not failed else 3


if __name__ == "__main__":
    raise SystemExit(main())
