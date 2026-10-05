#!/usr/bin/env python3
"""Карантин для скиллов Claude Code: план → перенос → возврат. Ничего не стирает.

    python quarantine.py plan --pick "9-15, 27, firecrawl-shop"   # проверяет выбор, пишет plan.json
    python quarantine.py apply --approved                         # переносит по plan.json, по одному скиллу
    python quarantine.py list
    python quarantine.py restore --pick "firecrawl-shop"

Зависимостей нет (стандартная библиотека Python 3.9+).
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# SKILL_CLEANUP_CLAUDE_DIR — только чтобы проверять скрипт на временной копии
CLAUDE = Path(os.environ.get("SKILL_CLEANUP_CLAUDE_DIR") or Path.home() / ".claude")
STATE = CLAUDE / "skills-quarantine"
REPORT, PLAN, JOURNAL = STATE / "report.json", STATE / "plan.json", STATE / "journal.jsonl"
MAX_AGE = 24 * 3600
SELF = "skill-cleanup"
REFUSE = {
    "плагин": "скилл плагина: отдельно не убирается, только весь плагин через /plugin",
    "claude.ai": "синхронизирован с claude.ai: отключается в настройках claude.ai",
}


def die(message):
    print(f"ОТКАЗ: {message}")
    sys.exit(1)


def parse_pick(text):
    tokens = []
    for token in re.split(r"[,\s]+", text.strip()):
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m:
            tokens += list(range(int(m.group(1)), int(m.group(2)) + 1))
        elif token.isdigit():
            tokens.append(int(token))
        elif token:
            tokens.append(token)
    return tokens


def is_link(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    return os.path.realpath(path) != os.path.abspath(path)


def movable(path):
    allowed = {os.path.normcase(os.path.abspath(CLAUDE / d)) for d in ("skills", "commands")}
    return os.path.normcase(os.path.abspath(path.parent)) in allowed


def journal():
    try:
        lines = JOURNAL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def log(entry):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def in_quarantine():
    state = {}
    for entry in journal():
        if entry["action"] == "quarantine":
            state[entry["key"]] = entry
        else:
            state.pop(entry["key"], None)
    return state


def cmd_plan(args):
    try:
        age = time.time() - REPORT.stat().st_mtime
        skills = json.loads(REPORT.read_text(encoding="utf-8"))["skills"]
    except (OSError, ValueError, KeyError):
        die(f"нет отчёта {REPORT} — сначала scan.py")
    if age > MAX_AGE:
        die("отчёту больше суток — запусти scan.py и покажи пользователю свежую таблицу")
    by_n = {it["n"]: it for it in skills}
    by_key = {it["key"]: it for it in skills}
    chosen, refused = {}, []
    for token in parse_pick(args.pick):
        it = by_n.get(token) if isinstance(token, int) else by_key.get(token)
        if not it:
            refused.append((str(token), "нет такого номера или имени в отчёте"))
        elif it["key"] == SELF:
            refused.append((it["key"], "это сам скилл чистки"))
        elif not it["removable"]:
            refused.append((it["key"], REFUSE.get(it["how"], "не убирается точечно")))
        elif not movable(Path(it["path"])):
            refused.append((it["key"], "путь вне ~/.claude/skills и ~/.claude/commands"))
        else:
            chosen[it["key"]] = it
    items = []
    for it in chosen.values():
        warnings = []
        if it["calls"]:
            warnings.append(f"использовался: вызовов {it['calls']}, последний раз {it['last_used']}")
        elif it["touch_sessions"]:
            warnings.append(f"не вызывался, но его файлы открывались в {it['touch_sessions']} сессиях, "
                            f"последний раз {it['last_used']}")
        # ссылка от скилла, который уходит в карантин тем же планом, не в счёт
        leaving = {f"{kind} {key}" for key in chosen for kind in ("скилл", "команда")}
        outside = [r for r in it["referenced_by"] if r not in leaving]
        if outside:
            warnings.append("на него ссылаются: " + ", ".join(outside))
        if it.get("lock_source"):
            warnings.append(f"поставлен через npx skills ({it['lock_source']}): обновление пака может его вернуть")
        action = "перенос папки" if it["how"] == "папка" else "перенос файла"
        if it["how"] == "ссылка":
            action = f"снятие ссылки, оригинал остаётся в {it.get('link_target', '?')}"
        items.append({"n": it["n"], "key": it["key"], "how": it["how"], "path": it["path"],
                      "link_target": it.get("link_target"), "action": action, "warnings": warnings})
    items.sort(key=lambda x: x["n"])
    STATE.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps({"created": time.time(), "items": items}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"## План карантина: {len(items)}\n")
    if items:
        print("| № | Скилл | Что произойдёт | Предупреждения |\n|---|---|---|---|")
        for x in items:
            print(f"| {x['n']} | `{x['key']}` | {x['action']} | {'; '.join(x['warnings']) or '—'} |")
    if refused:
        print("\nНе вошли в план:")
        for name, reason in refused:
            print(f"- `{name}` — {reason}")
    print("\nНичего не перенесено. Перенос — `apply --approved`, только после «да» пользователя на этот список.")


def cmd_apply(args):
    if not args.approved:
        die("нужен флаг --approved; ставь его только после явного «да» пользователя на показанный план")
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        die(f"нет плана {PLAN} — сначала plan --pick, затем покажи план пользователю")
    if time.time() - plan["created"] > MAX_AGE:
        die("плану больше суток — составь и покажи заново")
    batch = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest_root = STATE / batch
    done, failed = [], []
    for x in plan["items"]:
        src = Path(x["path"])
        entry = {"action": "quarantine", "time": datetime.now().isoformat(timespec="seconds"), "batch": batch,
                 "key": x["key"], "from": str(src), "to": None, "link_target": None}
        try:
            if not movable(src):
                raise OSError("путь вне ~/.claude/skills и ~/.claude/commands")
            if not os.path.lexists(src):
                raise OSError("уже нет на месте")
            if is_link(src):
                entry["link_target"] = os.path.realpath(src)
                src.unlink() if src.is_symlink() else os.rmdir(src)
            else:
                dest_root.mkdir(parents=True, exist_ok=True)
                dst = dest_root / src.name
                if os.path.lexists(dst):
                    raise OSError(f"в карантине уже есть {dst}")
                shutil.move(str(src), str(dst))
                entry["to"] = str(dst)
        except OSError as e:
            failed.append((x["key"], str(e)))
            continue
        log(entry)
        done.append(entry)
    PLAN.replace(STATE / f"plan.applied-{batch}.json")
    print(f"## В карантине: {len(done)} из {len(plan['items'])}\n")
    for entry in done:
        where = entry["to"] or f"ссылка снята, оригинал в {entry['link_target']}"
        print(f"- `{entry['key']}` → {where}")
    if failed:
        print("\nНе перенесены:")
        for key, reason in failed:
            print(f"- `{key}` — {reason}")
        sys.exit(1)


def cmd_list(args):
    state = in_quarantine()
    print(f"## В карантине: {len(state)}\n")
    for key, entry in sorted(state.items()):
        where = entry["to"] or f"ссылка на {entry['link_target']}"
        print(f"- `{key}` — {entry['time'][:10]}, было {entry['from']}, сейчас {where}")


def cmd_restore(args):
    state = in_quarantine()
    done, failed = [], []
    for key in parse_pick(args.pick):
        entry = state.get(str(key))
        try:
            if not entry:
                raise OSError("нет в карантине")
            dst = Path(entry["from"])
            if os.path.lexists(dst):
                raise OSError(f"на месте уже есть {dst}")
            if entry["link_target"]:
                if not os.path.isdir(entry["link_target"]):
                    raise OSError(f"оригинал ссылки пропал: {entry['link_target']}")
                if os.name == "nt":
                    import _winapi
                    _winapi.CreateJunction(entry["link_target"], str(dst))
                else:
                    os.symlink(entry["link_target"], dst)
            else:
                shutil.move(entry["to"], str(dst))
                try:
                    os.rmdir(Path(entry["to"]).parent)  # пустую папку партии
                except OSError:
                    pass
        except OSError as e:
            failed.append((str(key), str(e)))
            continue
        log({"action": "restore", "time": datetime.now().isoformat(timespec="seconds"), "key": str(key)})
        done.append(str(key))
    print(f"## Возвращено: {len(done)}\n")
    for key in done:
        print(f"- `{key}`")
    if failed:
        print("\nНе возвращены:")
        for key, reason in failed:
            print(f"- `{key}` — {reason}")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="проверить выбор и записать план; ничего не переносит")
    p.add_argument("--pick", required=True, help="номера, диапазоны и имена из отчёта: \"9-15, 27, firecrawl-shop\"")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("apply", help="перенести в карантин по плану")
    p.add_argument("--approved", action="store_true", help="пользователь явно сказал «да» на показанный план")
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("list", help="что сейчас в карантине")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("restore", help="вернуть из карантина на место")
    p.add_argument("--pick", required=True, help="имена скиллов через запятую")
    p.set_defaults(func=cmd_restore)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    args.func(args)


if __name__ == "__main__":
    main()
