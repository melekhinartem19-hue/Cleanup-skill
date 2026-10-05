#!/usr/bin/env python3
"""Инвентаризация скиллов Claude Code и их использование по истории сессий.

Только читает. Зависимостей нет (стандартная библиотека Python 3.9+).

    python scan.py        # пишет report.md и report.json в ~/.claude/skills-quarantine
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
STATE = CLAUDE / "skills-quarantine"  # вне ~/.claude/skills, иначе карантин загрузится как скиллы
ACTIVE_FROM = 3  # вызовов за окно истории, начиная с которых скилл «активно»

CMD_RE = re.compile(r"<command-name>/([^<\s]+)</command-name>")
KEY_RE = re.compile(r"^([A-Za-z_-]+):\s*(.*)$")
TOUCH_TOOLS = {"Read", "Bash", "PowerShell", "Grep", "Glob"}


def frontmatter(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data, key = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = KEY_RE.match(line)
        if m:
            key = m.group(1)
            data[key] = m.group(2).strip()
        elif key and (line[:1] in (" ", "\t") or line.startswith("- ")):
            data[key] += " " + line.strip()
    for k, v in data.items():
        v = re.sub(r"^[|>][+-]?\s*", "", v).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        data[k] = v
    return data


def lock_sources():
    sources = {}
    for lock in (HOME / ".agents" / ".skill-lock.json", HOME / "skills-lock.json"):
        try:
            skills = json.loads(lock.read_text(encoding="utf-8")).get("skills", {})
        except (OSError, ValueError):
            continue
        for name, meta in skills.items():
            if isinstance(meta, dict) and meta.get("source"):
                sources[name] = meta["source"]
    return sources


def record(key, source, pack, path, how, fm, aliases, needles):
    return {
        "key": key,
        "source": source,
        "pack": pack,
        "path": str(path),
        "how": how,
        "manual_only": fm.get("disable-model-invocation", "").lower() == "true",
        "description": fm.get("description", ""),
        "aliases": [a for a in dict.fromkeys(aliases) if a],
        "needles": needles,
    }


def plugin_skill_dirs(root, plugin_id):
    """Плагин грузит только объявленные скиллы: plugin.json → запись маркетплейса → skills/*."""
    declared = None
    try:
        declared = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("skills")
    except (OSError, ValueError):
        pass
    if not declared and "@" in plugin_id:
        name, market = plugin_id.split("@", 1)
        manifest = CLAUDE / "plugins" / "marketplaces" / market / ".claude-plugin" / "marketplace.json"
        try:
            for p in json.loads(manifest.read_text(encoding="utf-8")).get("plugins", []):
                if p.get("name") == name:
                    declared = p.get("skills")
        except (OSError, ValueError):
            pass
    if isinstance(declared, str):
        declared = [declared]
    dirs = []
    for rel in declared or ["./skills"]:
        path = root / rel
        if (path / "SKILL.md").is_file():
            dirs.append(path)
        elif path.is_dir():
            dirs += [md.parent for md in sorted(path.glob("*/SKILL.md"))]
    return dirs


def inventory():
    items = []
    locks = lock_sources()

    personal = CLAUDE / "skills"
    dirs = [d for d in sorted(personal.iterdir()) if (d / "SKILL.md").is_file()] if personal.is_dir() else []
    prefixes = Counter(d.name.split("-")[0] for d in dirs if d.name not in locks)
    for d in dirs:
        fm = frontmatter(d / "SKILL.md")
        prefix = d.name.split("-")[0]
        pack = locks.get(d.name) or (prefix + "-*" if prefixes[prefix] >= 3 else "одиночные")
        link = os.path.realpath(d) != os.path.abspath(d)
        items.append(record(d.name, "личный", pack, d, "ссылка" if link else "папка", fm,
                            [d.name, fm.get("name")], [[f".claude/skills/{d.name.lower()}/"]]))
        items[-1]["lock_source"] = locks.get(d.name, "")
        if link:
            items[-1]["link_target"] = os.path.realpath(d)

    commands = CLAUDE / "commands"
    if commands.is_dir():
        for f in sorted(commands.glob("*.md")):
            items.append(record(f.stem, "команда", "команды", f, "файл", frontmatter(f),
                                [f.stem], [[f".claude/commands/{f.name.lower()}"]]))

    synced = personal / "synced"
    if synced.is_dir():
        for md in sorted(synced.glob("*/*/SKILL.md")):
            d = md.parent
            fm = frontmatter(md)
            items.append(record(f"anthropic-skills:{d.name}", "claude.ai", "claude.ai (anthropic-skills)", d,
                                "claude.ai", fm, [f"anthropic-skills:{d.name}"],
                                [["/skills/synced/", f"/{d.name.lower()}/"]]))

    try:
        installed = json.loads((CLAUDE / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        installed = {}
    seen = set()
    for plugin_id, entries in installed.get("plugins", {}).items():
        plugin = plugin_id.split("@")[0]
        for entry in entries:
            root = Path(entry.get("installPath", ""))
            if not root.is_dir() or root in seen:
                continue
            seen.add(root)
            for d in plugin_skill_dirs(root, plugin_id):
                fm = frontmatter(d / "SKILL.md")
                rel = d.relative_to(root).as_posix().lower()
                items.append(record(f"{plugin}:{d.name}", "плагин", f"плагин {plugin_id}", d, "плагин", fm,
                                    [f"{plugin}:{d.name}", f"{plugin}:{fm.get('name', '')}".rstrip(":")],
                                    [[f"/{plugin.lower()}/", f"/{rel}/"]]))
    return items


def alias_map(items):
    amap, bare = {}, defaultdict(list)
    for it in items:
        for a in it["aliases"]:
            amap.setdefault(a, it["key"])
        if it["source"] in ("плагин", "claude.ai"):
            bare[it["key"].split(":", 1)[1]].append(it["key"])
    # /security без префикса → security-ru:security, если имя однозначно и не занято личным скиллом
    for name, keys in bare.items():
        if name not in amap and len(keys) == 1:
            amap[name] = keys[0]
    return amap


def scan_usage(items):
    amap = alias_map(items)
    use = defaultdict(lambda: {"tool": 0, "slash": 0, "sessions": set(), "touch": set(), "last": ""})
    unknown = Counter()
    first_ts, sessions, cwds = [], 0, set()

    def hit(name, kind, session, ts):
        key = amap.get(name)
        if not key:
            # неизвестная /команда — встроенная команда CLI (/clear, /model), а не скилл
            if kind == "tool":
                unknown[name] += 1
            return
        u = use[key]
        u[kind] += 1
        u["sessions"].add(session)
        u["last"] = max(u["last"], ts[:10])

    projects = CLAUDE / "projects"
    for f in projects.rglob("*.jsonl") if projects.is_dir() else []:
        sessions += 1
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            stamped = False
            for line in fh:
                if stamped and '"Skill"' not in line and "<command-name>" not in line and "skills" not in line \
                        and "commands" not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                ts = o.get("timestamp") or ""
                if ts and not stamped:
                    first_ts.append(ts[:10])
                    stamped = True
                    if o.get("cwd"):
                        cwds.add(os.path.normcase(o["cwd"]))
                session = o.get("sessionId") or f.stem
                content = (o.get("message") or {}).get("content")
                if o.get("type") == "user":
                    texts = [content] if isinstance(content, str) else \
                        [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
                    for text in texts:
                        for name in CMD_RE.findall(text):
                            hit(name, "slash", session, ts)
                elif o.get("type") == "assistant" and isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict) or c.get("type") != "tool_use":
                            continue
                        inp = c.get("input") or {}
                        if c.get("name") == "Skill" and inp.get("skill"):
                            hit(str(inp["skill"]).lstrip("/"), "tool", session, ts)
                        elif c.get("name") in TOUCH_TOOLS:
                            s = json.dumps(inp, ensure_ascii=False).replace("\\\\", "/").replace("\\", "/").lower()
                            if "/skills/" not in s and "/commands/" not in s:
                                continue
                            for it in items:
                                if any(all(n in s for n in needle) for needle in it["needles"]):
                                    u = use[it["key"]]
                                    u["touch"].add(session)
                                    u["last"] = max(u["last"], ts[:10])
    window = (min(first_ts), max(first_ts)) if first_ts else ("", "")
    return use, unknown, window, sessions, cwds


def ref_pattern(aliases):
    """Упоминание скилла: путь skills/имя, команда /имя, `имя` в бэктиках, «скилл имя»."""
    alts = []
    for a in aliases:
        e = re.escape(a)
        if ":" in a:
            alts.append(rf"(?<![\w:-]){e}(?![\w-])")
        else:
            alts += [rf"skills[/\\]{e}(?![\w-])", rf"(?<![\w/.:-])/{e}(?![\w/:-])", rf"`{e}`",
                     rf"(?:skill|скилл\w*)\s+[`\"«']?{e}(?![\w-])", rf"(?<![\w-]){e}[`\"»']?\s+(?:skill|скилл)"]
    return re.compile("|".join(alts), re.IGNORECASE)


def references(items, cwds):
    sources = [("CLAUDE.md глобальный", CLAUDE / "CLAUDE.md"), ("CLAUDE.md домашний", HOME / "CLAUDE.md")]
    roots = [(CLAUDE, "")]
    for cwd in sorted(cwds):
        p = Path(cwd)
        if not p.is_dir() or p == HOME or "appdata" in (part.lower() for part in p.parts):
            continue
        sources += [(f"CLAUDE.md проекта {p.name}", p / "CLAUDE.md"),
                    (f"CLAUDE.md проекта {p.name}", p / ".claude" / "CLAUDE.md")]
        roots.append((p / ".claude", f" ({p.name})"))
    for root, suffix in roots:
        sources += [(f"агент {f.stem}{suffix}", f) for f in sorted((root / "agents").glob("*.md"))]
        if suffix:
            sources += [(f"команда {f.stem}{suffix}", f) for f in sorted((root / "commands").glob("*.md"))]
    own = {}
    for it in items:
        p = Path(it["path"])
        own[it["key"]] = p if p.is_file() else p / "SKILL.md"
        sources.append((("команда " if it["source"] == "команда" else "скилл ") + it["key"], own[it["key"]]))
    texts = []
    for label, path in sources:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        texts.append((label, path, text, text.lower()))
    refs = {}
    for it in items:
        pattern = ref_pattern(it["aliases"])
        names = [a.lower() for a in it["aliases"]]
        refs[it["key"]] = list(dict.fromkeys(
            label for label, path, text, low in texts
            if path != own[it["key"]] and any(n in low for n in names) and pattern.search(text)))
    return refs


def grade(calls, touch):
    if calls >= ACTIVE_FROM:
        return "активно"
    if calls:
        return "редко"
    return "косвенно" if touch else "ни разу"


def build(items):
    use, unknown, window, sessions, cwds = scan_usage(items)
    refs = references(items, cwds)
    by_desc = defaultdict(list)
    for it in items:
        norm = " ".join(it["description"].lower().split())
        if norm:
            by_desc[norm].append(it["key"])
    for it in items:
        u = use[it["key"]]
        calls = u["tool"] + u["slash"]
        norm = " ".join(it["description"].lower().split())
        it.update({
            "tool_calls": u["tool"],
            "slash_calls": u["slash"],
            "calls": calls,
            "sessions": len(u["sessions"]),
            "touch_sessions": len(u["touch"]),
            "last_used": u["last"],
            "grade": grade(calls, len(u["touch"])),
            "duplicates": [k for k in by_desc.get(norm, []) if k != it["key"]],
            "referenced_by": refs[it["key"]],
            "description_chars": 0 if it["manual_only"] else len(it["description"]),
            "removable": it["how"] in ("папка", "ссылка", "файл"),
        })
        del it["needles"], it["aliases"]
    packs = defaultdict(list)
    for it in items:
        packs[it["pack"]].append(it)
    ordered = sorted(packs.items(), key=lambda p: (p[1][0]["source"] != "личный", -len(p[1]), p[0]))
    n = 0
    for _, members in ordered:
        members.sort(key=lambda it: (-it["calls"], -it["touch_sessions"], it["key"]))
        for it in members:
            n += 1
            it["n"] = n
    return {
        "window": {"from": window[0], "to": window[1], "session_files": sessions},
        "skills": [it for _, members in ordered for it in members],
        "not_on_disk": dict(unknown.most_common()),
    }


def markdown(data):
    skills = data["skills"]
    w = data["window"]
    totals = Counter(it["grade"] for it in skills)
    out = [
        "# Использование скиллов",
        "",
        f"История: {w['from']} — {w['to']}, файлов сессий: {w['session_files']}. "
        "«Ни разу» значит «ни разу за это окно».",
        "",
        f"Всего скиллов: {len(skills)} — " + ", ".join(
            f"{g}: {totals[g]}" for g in ("активно", "редко", "косвенно", "ни разу")),
        "",
        f"Градация: активно — {ACTIVE_FROM}+ вызовов; редко — 1–{ACTIVE_FROM - 1}; косвенно — не вызывался, "
        "но его файлы читались или запускались; ни разу — никаких следов.",
        "",
        "«Ссылаются» — кто упоминает скилл: CLAUDE.md, агенты, команды, другие скиллы. "
        "Скилл с такой ссылкой может быть нужен, даже если ни разу не вызывался.",
        "",
    ]
    pack = None
    for it in skills:
        if it["pack"] != pack:
            pack = it["pack"]
            members = [x for x in skills if x["pack"] == pack]
            g = Counter(x["grade"] for x in members)
            out += ["", f"## {pack} ({len(members)})", "",
                    ", ".join(f"{k}: {v}" for k, v in g.most_common()), "",
                    "| № | Скилл | Градация | Вызовы (модель / ты) | Файлы | Последний раз | Ссылаются "
                    "| Дубль | Удаление |",
                    "|---|---|---|---|---|---|---|---|---|"]
        dup = ", ".join(it["duplicates"]) or "—"
        refs = it["referenced_by"]
        ref = (", ".join(refs[:3]) + (f" +{len(refs) - 3}" if len(refs) > 3 else "")) if refs else "—"
        out.append(
            f"| {it['n']} | `{it['key']}` | {it['grade']} | {it['calls']} ({it['tool_calls']} / {it['slash_calls']}) "
            f"| {it['touch_sessions']} | {it['last_used'] or '—'} | {ref} | {dup} | {it['how']} |")
    if data["not_on_disk"]:
        out += ["", "## Вызывались, но на диске их нет (встроенные или уже удалённые)", ""]
        out += [f"- `{name}` — {count}" for name, count in data["not_on_disk"].items()]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", default=str(STATE), help="куда записать report.md и report.json")
    args = ap.parse_args()
    data = build(inventory())
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(markdown(data), encoding="utf-8")
    totals = Counter(it["grade"] for it in data["skills"])
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"Таблица: {(out / 'report.md').as_posix()}")
    print(f"Скиллов: {len(data['skills'])} — " + ", ".join(f"{g}: {n}" for g, n in totals.most_common()))


if __name__ == "__main__":
    main()
