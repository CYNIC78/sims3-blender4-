"""Разбирает список анимаций Sims 3 по смыслу и раскладывает по категориям.

Зачем: в библиотеке 9000+ клипов, из которых большинство нашей игре не нужно
(бассейн, машины, работа, навыки, питомцы, малыши). Нам нужен короткий список
того, что реально пригодится в комнате с одним ботом и гостями.

Использование:

    # только отчёт и манифест
    python3 tools/classify_library.py "animation clips/animations.txt" -o reports

    # плюс разложить сами файлы по папкам-категориям (копированием)
    python3 tools/classify_library.py "animation clips/animations.txt" -o reports \
            --apply library --src "animation clips"

Формат имени (по анализу 9242 клипов):

    a2a_soc_Neutral_Greet_Friendly_Friendly_x%%+CLIP.animation
     │   │    │      │      │        └ слот: x — «ведущий», y — «ведомый»
     │   │    │      │      └ эмоция/стиль
     │   │    │      └ действие
     │   │    └ эмоциональная рамка (Neutral/Friendly/…)
     │   └ система (soc — социальные)
     └ кто с кем: a2a — персонаж↔персонаж, a2o — персонаж→объект,
       a/c/t/p/b — взрослый/ребёнок/малыш/питомец/…, второе — цель или партнёр
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import sys

# ---------------------------------------------------------------- разбор имени

def split_camel(text: str) -> list[str]:
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+", text)
    return parts or [text]


def tokenize(name: str) -> list[str]:
    base = name.split("%%")[0]
    out: list[str] = []
    for chunk in base.split("_"):
        if not chunk:
            continue
        out.extend(split_camel(chunk))
    return out


def parse(name: str) -> dict:
    base = name.split("%%")[0]
    raw_tokens = base.split("_")
    prefix = raw_tokens[0]
    tokens = [t.lower() for t in tokenize(base)]
    slot = ""
    if raw_tokens and raw_tokens[-1] in ("x", "y", "z"):
        slot = raw_tokens[-1]
    return {
        "name": base,
        "file": name,
        "prefix": prefix,
        "tokens": tokens,
        "slot": slot,
    }


RELATIONS = {
    "a2a": ("paired", "взрослый ↔ взрослый"),
    "c2a": ("paired", "ребёнок ↔ взрослый"),
    "a2c": ("paired", "взрослый ↔ ребёнок"),
    "c2c": ("paired", "ребёнок ↔ ребёнок"),
    "t2t": ("paired", "малыш ↔ малыш"),
    "a2b": ("paired", "взрослый ↔ младенец"),
    "a2o": ("object", "взрослый → объект"),
    "c2o": ("object", "ребёнок → объект"),
    "t2o": ("object", "малыш → объект"),
    "p2o": ("object", "питомец → объект"),
    "b2o": ("object", "младенец → объект"),
    "a2p": ("pet", "взрослый ↔ питомец"),
    "t2p": ("pet", "малыш ↔ питомец"),
    "o": ("object", "объект сам"),
    "a": ("solo", "взрослый"),
    "c": ("solo", "ребёнок"),
    "t": ("solo", "малыш"),
    "p": ("solo", "питомец"),
    "b": ("solo", "младенец"),
    "e": ("solo", "служебное"),
}

# поза — важнейший атрибут: от неё зависит, можно ли проиграть клип на маркере
POSTURES = [
    ("sitting", ("sitting", "sit", "seated")),
    ("standing", ("standing", "stand")),
    ("floor", ("floor", "ground")),
    ("bed", ("bed", "sleep")),
    ("chair", ("chair",)),
    ("couch", ("couch", "sofa")),
    ("kneel", ("kneel", "kneeling")),
    ("swim", ("swim", "swimming", "pool")),
]

# что в нашей игре не нужно совсем (сравнение по ЦЕЛЫМ токенам, не по подстроке:
# иначе "breath" ловит "eat", а "career" — "car")
SKIP_TOPICS = {
    "pool": ("pool", "swim", "swimming", "swimwear", "dive", "diving", "float", "waterslide"),
    "vehicle": ("car", "drive", "driver", "vehicle", "boat", "taxi", "bike", "bicycle"),
    "career": ("career", "job", "work", "business", "office", "meeting", "uniform", "colleague",
               "interview", "notebook", "resume", "boss"),
    "skill": ("skill", "athletic", "guitar", "piano", "paint", "painting", "writing", "chess",
              "cooking", "bake", "baking", "invent", "sculpt"),
    "pet": ("pet", "dog", "cat", "horse", "bird", "rodent", "lizard"),
    "baby": ("baby", "infant", "toddler", "stroller", "crib", "diaper", "bassinet"),
    "outdoor": ("garden", "gardening", "fishing", "hike", "camp", "bonfire", "beach", "snow",
                "sports", "jogging", "park"),
    "school": ("school", "homework", "class", "exam", "teacher", "student"),
    "occult": ("vampire", "werewolf", "witch", "ghost", "mummy", "fairy", "genie", "zombie", "alien"),
    "festival": ("festival", "holiday", "santa", "gift", "fireworks"),
    "combat": ("fight", "punch", "kick", "slap", "burglar", "police", "arrest", "death", "grim"),
}

# порядок важен: первое совпадение выигрывает, поэтому специфичное — раньше общего
KEEP_RULES = [
    ("dance", ("dance", "dancing", "boogie")),
    ("locomotion", ("walk", "walks", "run", "jog", "sneak", "strafe", "stairs", "climb",
                    "jump", "crawl", "stroll")),
    ("sleep", ("sleep", "sleeping", "nap", "doze", "dream", "snore")),
    ("hygiene", ("wash", "shower", "bathe", "bath", "toilet", "brush", "teeth", "sink", "pee")),
    ("eat", ("eat", "eating", "drink", "drinking", "meal", "breakfast", "lunch", "dinner",
             "snack", "chew", "sip", "pizza", "sandwich")),
    ("sit_transition", ("sitdown", "getup", "standup", "rise", "scoot", "recline")),
    ("gesture", ("nod", "shrug", "wave", "point", "clap", "gesture", "beckon", "salute",
                 "thumbsup", "handshake", "highfive", "shoo", "wiggle", "tap", "pat")),
    ("talk", ("talk", "talking", "chat", "chatting", "converse", "gossip", "argue", "complain",
              "ramble", "whisper", "joke", "tease", "discuss", "lecture", "brag", "greet",
              "mumble", "sigh")),
    ("emote", ("laugh", "giggle", "cry", "sob", "angry", "anger", "sad", "smile", "grimace",
               "surprise", "shock", "disgust", "bored", "yawn", "fear", "scared", "react",
               "express", "cheer", "frustrated", "embarrass", "blush", "amused", "sulk")),
    ("social", ("hug", "kiss", "cuddle", "embrace", "flirt", "console", "apologize", "invite",
                "tickle", "propose", "massage", "snuggle", "cryon", "tease", "joke")),
    ("work_leisure", ("computer", "tv", "television", "phone", "book", "read", "reading", "write",
                      "game", "gaming", "radio", "stereo", "music")),
    ("idle", ("idle", "breathe", "breathing", "loiter", "wait", "waiting", "lounge")),
]

# кто исполняет клип: a=взрослый, c=ребёнок, t=малыш, e=пожилой, p=питомец, b=младенец,
# o=объект, S3=система. Второй слот: x = наш сим, y = партнёр/цель.
AGE = {"a": "adult", "c": "child", "t": "toddler", "e": "elder", "p": "pet", "b": "baby",
       "o": "object", "S3": "system", "A": "adult"}

# род движения в системе локомоции Sims 3. Шаблоны строгие: иначе "stepstool", "treadmill_jog"
# и танцевальный "StepTouch" попадают в локомоцию.
LOCO_PATTERNS = (
    ("walk", re.compile(r"^[a-z]{1,2}_(female_|male_)?walk(_[a-z]+)*$", re.I)),
    ("run", re.compile(r"^[a-z]{1,2}_(female_|male_)?(run|jog|fastjog)(_[a-z]+)*$", re.I)),
    ("step", re.compile(r"^[a-z]_step", re.I)),
    ("turn", re.compile(r"^[a-z]_(turnand|turninplace|turn_|runandstop|emergencystop)", re.I)),
)


def motion_of(name: str) -> str:
    for genus, pattern in LOCO_PATTERNS:
        if pattern.match(name):
            return genus
    return ""


# объекты, которые нам реально интересны в комнате
ROOM_OBJECTS = ("couch", "sofa", "tv", "television", "computer", "phone", "book", "radio",
                "stereo", "counter", "fridge", "table", "chair", "bed", "microwave", "coffee")


def _match(token_set: set[str], words: tuple[str, ...]) -> str:
    for word in words:
        if word in token_set:
            return word
    return ""


def classify(name: str) -> dict:
    info = parse(name)
    tokens = info["tokens"]
    token_set = set(tokens)
    low = info["name"].lower()

    relation_kind, _relation_label = RELATIONS.get(info["prefix"], ("unknown", "?"))

    posture = ""
    for posture_name, words in POSTURES:
        if _match(token_set, words):
            posture = posture_name
            break

    topic = ""
    for topic_name, words in SKIP_TOPICS.items():
        if _match(token_set, words):
            topic = topic_name
            break

    category = ""
    matched_word = ""
    motion = motion_of(info["name"])
    if motion:                                   # walk / run / step / turn
        category = "loco_" + motion
        matched_word = motion
    if not category and "trans" in token_set and ("sit" in token_set or "sitting" in token_set or "stand" in token_set):
        category = "sit_transition"
        matched_word = "trans"
    for cat, words in KEEP_RULES:
        if category:
            break
        word = _match(token_set, words)
        if word:
            category = cat
            matched_word = word
            break

    if not category and relation_kind == "paired":
        category = "social"
    if not category:
        category = "other"

    room_object = any(obj in low for obj in ROOM_OBJECTS)

    keep = True
    reason = ""
    if topic:
        keep = False
        reason = "тема вне игры: %s" % topic
    elif relation_kind == "pet":
        keep = False
        reason = "питомцы"
    elif category == "other" and not room_object:
        keep = False
        reason = "смысл не распознан, объект не из нашей комнаты"

    age = AGE.get(info["prefix"].replace("2o", "").replace("2a", "").replace("2b", "")
                  .replace("2c", "").replace("2t", "").replace("2p", ""), "")
    if category.startswith("loco_") and age != "adult" and keep:
        keep = False
        reason = "локомоция %s — у нас один взрослый бот" % (age or "другого вида")

    priority = 3
    if category.startswith("loco_") or category in ("locomotion", "idle", "sit_transition", "gesture", "emote", "talk"):
        priority = 1
    elif category in ("dance", "social", "work_leisure", "eat", "sleep", "hygiene"):
        priority = 2

    return {
        "name": info["name"],
        "file": info["file"],
        "relation": relation_kind,
        "slot": info["slot"],
        "posture": posture,
        "category": category,
        "matched": matched_word,
        "room_object": room_object,
        "keep": keep,
        "reason": reason,
        "priority": priority,
        "age": age,
    }


# ---------------------------------------------------------------- отчёты

def build_report(records: list[dict], samples: int = 6) -> str:
    total = len(records)
    keep = [r for r in records if r["keep"]]
    lines = []
    lines.append("# Разбор библиотеки анимаций\n")
    lines.append("Всего клипов: **%d**, из них предлагаю взять **%d** (%.0f %%), "
                 "отбросить **%d**.\n" % (total, len(keep), 100.0 * len(keep) / max(1, total),
                                          total - len(keep)))

    lines.append("## По категориям\n")
    lines.append("| категория | клипов | из них парных | с позой | примеры |")
    lines.append("|---|---|---|---|---|")
    by_cat = collections.defaultdict(list)
    for r in records:
        if r["keep"]:
            by_cat[r["category"]].append(r)
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        paired = sum(1 for r in items if r["slot"] == "x")
        posed = sum(1 for r in items if r["posture"])
        example = "<br>".join("`%s`" % r["name"] for r in items[:samples])
        lines.append("| %s | %d | %d | %d | %s |" % (cat, len(items), paired, posed, example))

    lines.append("\n## Что отбрасываем (по причинам)\n")
    reasons = collections.Counter(r["reason"] for r in records if not r["keep"])
    lines.append("| причина | клипов |")
    lines.append("|---|---|")
    for reason, count in reasons.most_common():
        lines.append("| %s | %d |" % (reason, count))

    lines.append("\n## Позы: как игра решает «жест в разных положениях»\n")
    posture_stats = collections.Counter(r["posture"] or "(без пометки)" for r in records)
    lines.append("| поза | клипов |")
    lines.append("|---|---|")
    for posture_name, count in posture_stats.most_common():
        lines.append("| %s | %d |" % (posture_name, count))

    lines.append("\nПримеры одного и того же жеста в разных позах "
                 "(игра хранит их отдельными клипами):\n")
    families = collections.defaultdict(list)
    posture_words = {w for _n, words in POSTURES for w in words}
    for r in records:
        if r["category"] not in ("gesture", "emote", "talk", "idle"):
            continue
        tokens = [t for t in tokenize(r["name"]) if t not in posture_words]
        key = "_".join(tokens[:4])
        families[key].append(r)
    shown = 0
    for family, items in sorted(families.items(), key=lambda kv: -len({r["posture"] for r in kv[1]})):
        postures = sorted({r["posture"] for r in items if r["posture"]})
        if len(postures) > 1 and shown < 8:
            shown += 1
            lines.append("- `%s`: %s" % (family, ", ".join(postures)))
    if shown == 0:
        lines.append("- (по именам позы встречаются, но группировка по жесту не сложилась)")

    lines.append("\n## Локомоция: полная система (это важнее всего остального)\n")
    loco = [r for r in records if r["category"].startswith("loco_")]
    by_age = collections.Counter(r["age"] for r in loco)
    by_genus = collections.Counter(r["category"].replace("loco_", "") for r in loco)
    lines.append("Клипов локомоции в библиотеке: **%d** (взрослый %d, ребёнок %d, малыш %d, "
                 "пожилой %d, питомец %d).\n"
                 % (len(loco), by_age.get("adult", 0), by_age.get("child", 0),
                    by_age.get("toddler", 0), by_age.get("elder", 0), by_age.get("pet", 0)))
    lines.append("| род | клипов | что внутри |")
    lines.append("|---|---|---|")
    lines.append("| walk | %d | `a_male_walk`, `a_female_walk` + `_short`, `_fast`, "
                 "`_pregnant`, `_start`, `_stop`, `_stop_trip` |" % by_genus.get("walk", 0))
    lines.append("| run | %d | `a_male_run`, `_fast`, `_slow`, `a_male_jog`, "
                 "`a_male_fastjog`, `a_female_run` |" % by_genus.get("run", 0))
    lines.append("| turn | %d | `turnAndGo[_R]_<угол>`, `turnAndRun`, `turnAndStop`, "
                 "`turnInPlace`, `RunAndStop`, `EmergencyStop` |" % by_genus.get("turn", 0))
    lines.append("| step | %d | `step_<угол шага>_<угол доворота>_<L\\|R>`, `step_short_*`, "
                 "`stepInPlace_*`, `stepOver` |" % by_genus.get("step", 0))

    lines.append("\n**Как это устроено.** Ходьба/бег — обычные циклы (`a_male_walk`, `a_male_run`), "
                 "а повороты и остановки — отдельные клипы-переходы с углом в имени "
                 "(`a_turnAndGo_90`, `a_turnAndGoR_180`, `a_turnInPlace_180`, `a_RunAndStop_90`, "
                 "`a_EmergencyStop_0`). Плюс микро-шаги `a_step_<a>_<b>_L/R` — короткий шаг со "
                 "смещением под углом `a` и доворотом до `b`, ими подправляется позиция у объекта. "
                 "`_short` — урезанная версия того же клипа (похоже, для короткой дистанции; "
                 "проверю по длине после конвертации).\n")

    adult = sorted(r["name"] for r in loco if r["age"] == "adult" and r["keep"])
    lines.append("**Набор для нашего бота (взрослый, пол не важен):**\n")
    lines.append("```")
    lines.extend(adult)
    lines.append("```")
    lines.append("Этого хватает на всё движение: идти/бежать с разной скоростью, "
                 "тронуться, остановиться, повернуться на месте на 90/180, "
                 "повернуться и пойти, повернуться и побежать, экстренно встать.\n")

    lines.append("\n## Кто такие префиксы (a, c, t, e, p, b, o)\n")
    lines.append("| префикс | кто | пример |")
    lines.append("|---|---|---|")
    lines.append("| a | взрослый | `a_male_walk` |")
    lines.append("| c | ребёнок | `c_walk`, `c_run` |")
    lines.append("| t | малыш (тоддлер) — **ходит**, но медленно | `t_walk_male` |")
    lines.append("| e | пожилой | `e_male_walk` |")
    lines.append("| p | питомец (собака/кошка) | `p_walk` |")
    lines.append("| b | младенец | `a2b_...` |")
    lines.append("| o | объект | `o_...` |")
    lines.append("| S3 | системное | `S3_...` |")
    lines.append("\nЧерез `2` — взаимодействие: `a2o` взрослый с объектом, `a2a` взрослый со взрослым, "
                 "`a2c` взрослый с ребёнком. Второй слот: `x` — наш сим, `y` — партнёр.\n")

    lines.append("\n## Жесты в разных позах: как игра это решает\n")
    pose_pairs = collections.defaultdict(set)
    for r in records:
        if r["posture"] and r["category"] in ("emote", "gesture", "talk", "idle"):
            pose_pairs[r["name"].replace("_sitting", "").replace("_standing", "")
                       .replace("_floor", "").replace("_bed", "")].add(r["posture"])
    multi = {k: v for k, v in pose_pairs.items() if len(v) > 1}
    lines.append("Клипов, у которых есть версии в разных позах: **%d семейств**. Примеры:\n" % len(multi))
    for key, postures in list(sorted(multi.items()))[:10]:
        lines.append("- `%s`: %s" % (key, ", ".join(sorted(postures))))
    lines.append("\nВывод: игра не микширует жест в любую позу, а **хранит отдельный клип на позу** "
                 "(`…_sitting_x` / `…_standing_x`). Мы будем делать так же, а если версии для нужной "
                 "позы нет — проигрывать только верхнюю часть тела поверх базы (см. ниже).\n")

    lines.append("\n## Какие объекты вообще встречаются (что моделировать в комнате)\n")
    obj_counter = collections.Counter()
    for r in records:
        for token in r["name"].split("_"):
            if token.lower() in ROOM_OBJECTS:
                obj_counter[token.lower()] += 1
    lines.append("| объект | клипов |")
    lines.append("|---|---|")
    for obj, count in obj_counter.most_common(15):
        lines.append("| %s | %d |" % (obj, count))

    lines.append("\n## Приоритет загрузки\n")
    lines.append("1. **Сразу**: локомоция, idle, переходы сидя/стоя, жесты, эмоции, talk.")
    lines.append("2. **По требованию**: танцы, социальные парные, объекты комнаты (диван, ТВ, телефон, книга).")
    lines.append("3. **Не берём**: бассейн, машины, карьера, навыки, питомцы, малыши, сад/рыбалка, сверхъестественное.")
    return "\n".join(lines) + "\n"


def apply_folders(records: list[dict], src_dir: str, dst_dir: str, only_keep: bool = True) -> dict:
    """Копирует файлы в подпапки по категориям (для пакетного экспорта по категории)."""
    existing = set(os.listdir(src_dir)) if os.path.isdir(src_dir) else set()
    moved = collections.Counter()
    for r in records:
        if only_keep and not r["keep"]:
            continue
        filename = r["name"] + "%%+CLIP.animation"
        if filename not in existing:
            # в списке суффикс может быть обрезан (CLI/CL/…), ищем по началу
            candidates = [f for f in existing if f.startswith(r["name"] + "%%")]
            if not candidates:
                continue
            filename = candidates[0]
        target_dir = os.path.join(dst_dir, r["category"])
        os.makedirs(target_dir, exist_ok=True)
        dst = os.path.join(target_dir, filename)
        if not os.path.exists(dst):
            shutil.copy2(os.path.join(src_dir, filename), dst)
            moved[r["category"]] += 1
    return dict(moved)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("listing", help="animations.txt (UTF-16 или UTF-8)")
    parser.add_argument("-o", "--out", default="reports", help="папка для отчёта и манифеста")
    parser.add_argument("--apply", default="", help="разложить файлы в эту папку по категориям")
    parser.add_argument("--src", default="", help="папка с .animation файлами (для --apply)")
    parser.add_argument("--samples", type=int, default=6)
    args = parser.parse_args()

    raw = open(args.listing, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-16")
    lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]

    records = [classify(line) for line in lines]
    seen = set()
    unique = []
    for r in records:
        key = r["file"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    records = unique

    os.makedirs(args.out, exist_ok=True)
    report_path = os.path.join(args.out, "library_report.md")
    manifest_path = os.path.join(args.out, "library_manifest.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(build_report(records, args.samples))
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump({"total": len(records), "clips": records}, handle, ensure_ascii=False, indent=1)

    keep = sum(1 for r in records if r["keep"])
    print("клипов: %d | берём: %d | отчёт: %s | манифест: %s"
          % (len(records), keep, report_path, manifest_path))

    if args.apply:
        if not args.src:
            print("--apply требует --src (папка с .animation)")
            return 2
        moved = apply_folders(records, args.src, args.apply)
        total = sum(moved.values())
        print("разложено файлов: %d по %d категориям" % (total, len(moved)))
        for cat, count in sorted(moved.items(), key=lambda kv: -kv[1]):
            print("  %-16s %d" % (cat, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
