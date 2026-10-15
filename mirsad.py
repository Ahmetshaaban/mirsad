#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mirsad (مرصد) — محلي بالكامل، بلا حساب وبلا مفاتيح.
Mirsad is a self-hosted page-change watcher: it freezes a clean snapshot of a
web page, re-checks it on demand, and tells you exactly what changed — with a
local dashboard and JSON webhooks for n8n / anything else.

Inspiration studied: Visualping, Distill.io, changedetection.io.
Implementation: 100% original, stdlib-only (no pip install needed).

Version: 0.1.0 — MIT License
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from html import escape as _esc
from html.parser import HTMLParser
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse_mod
from urllib import request as urlrequest

VERSION = "0.1.0"
UA = f"Mirsad/{VERSION} (self-hosted page watcher)"

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
SNAPS_DIR = DATA_DIR / "snapshots"
DIFFS_DIR = DATA_DIR / "diffs"

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template",
             "nav", "header", "footer", "aside", "form", "button", "select",
             "option", "canvas", "video", "audio"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "tr", "td",
              "th", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
              "table", "br", "dt", "dd", "figcaption", "main"}


# --------------------------------------------------------------------------- #
# utils
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def local_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso or "-"


def make_name(url: str) -> str:
    p = urlparse_mod.urlparse(url)
    host = (p.netloc or "page").lower().replace(":", "-")
    bits = [b for b in p.path.split("/") if b]
    base = host if not bits else host + "-" + bits[0]
    slug = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-") or "page"
    name, i = slug, 2
    existing = set(config_targets().keys())
    while name in existing:
        name, i = f"{slug}-{i}", i + 1
    return name


def h(s) -> str:
    return _esc(str(s), quote=True)


# --------------------------------------------------------------------------- #
# DOM (tolerant parser + tiny selector engine + readability-lite)
# --------------------------------------------------------------------------- #

class Node:
    __slots__ = ("tag", "attrs", "children", "texts")

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.texts = []

    def cls_set(self):
        return set((self.attrs.get("class") or "").split())


class DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data and len(self.stack) > 1:
            self.stack[-1].texts.append(data)


def parse_html(html_text: str) -> Node:
    b = DomBuilder()
    try:
        b.feed(html_text)
        b.close()
    except Exception:
        pass
    return b.root


def iter_nodes(root):
    for c in root.children:
        yield c
        yield from iter_nodes(c)


def node_text(n: Node) -> str:
    parts = []

    def walk(x: Node):
        if x.tag in SKIP_TAGS:
            return
        for t in x.texts:
            t = " ".join(t.split())
            if t:
                parts.append(t + " ")
        for c in x.children:
            if c.tag in BLOCK_TAGS:
                parts.append("\n")
            walk(c)
            if c.tag in BLOCK_TAGS:
                parts.append("\n")

    walk(n)
    return "".join(parts)


def _parse_compound(part: str):
    tag = ""
    ident = None
    classes = set()
    m = re.match(r"[a-z0-9_-]+", part)
    if m:
        tag = m.group(0)
        part = part[m.end():]
    while part:
        if part.startswith("#"):
            m = re.match(r"#([\w-]+)", part)
            if not m:
                raise ValueError(f"bad selector part: {part}")
            ident = m.group(1)
            part = part[m.end():]
        elif part.startswith("."):
            m = re.match(r"\.([\w-]+)", part)
            if not m:
                raise ValueError(f"bad selector part: {part}")
            classes.add(m.group(1))
            part = part[m.end():]
        else:
            raise ValueError(f"bad selector part: {part}")
    return tag, ident, classes


def parse_selector(selector: str):
    chain = []
    for raw in selector.strip().lower().split():
        chain.append(_parse_compound(raw))
    if not chain:
        raise ValueError("empty selector")
    return chain


def _node_matches(n: Node, compound) -> bool:
    tag, ident, classes = compound
    if tag and n.tag != tag:
        return False
    if ident is not None and (n.attrs.get("id") or "") != ident:
        return False
    if classes and not classes.issubset(n.cls_set()):
        return False
    return True


def select(root: Node, selector: str):
    """CSS-lite: 'tag', '#id', '.class', compounds, descendant chains ('a b')."""
    cur = [root]
    for compound in parse_selector(selector):
        nxt = []
        for base in cur:
            for n in iter_nodes(base):
                if _node_matches(n, compound):
                    nxt.append(n)
        cur = nxt
        if not cur:
            break
    return cur


def extract_main(root: Node):
    """Readability-style: score blocks by text mass, minus link density."""
    best, best_score, best_text = None, -1.0, ""
    for n in iter_nodes(root):
        if n.tag not in ("article", "main", "div", "section", "td", "body"):
            continue
        text = node_text(n)
        ln = len(text.strip())
        if ln < 30:
            continue
        links = 0
        for a in iter_nodes(n):
            if a.tag == "a":
                links += len(node_text(a).strip())
        density = links / max(ln, 1)
        ps = sum(1 for c in iter_nodes(n) if c.tag == "p")
        score = ln * (1 - min(density, 1.0) * 1.2) + min(ps, 20) * 25
        if n.tag in ("article", "main"):
            score += 60
        if score > best_score:
            best, best_score, best_text = n, score, text
    title = ""
    for n in iter_nodes(root):
        if n.tag == "title":
            title = node_text(n).strip()
            break
    if best is None:
        return title, node_text(root)
    return title, best_text


def normalize_text(text: str, max_lines: int = 4000) -> str:
    out, prev = [], None
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or line == prev:
            continue
        out.append(line)
        prev = line
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def extract_content(html_text: str, selector: str = ""):
    root = parse_html(html_text)
    if selector:
        found = select(root, selector)
        if not found:
            return "", f"selector matched nothing: {selector}"
        return normalize_text(node_text(found[0])), None
    title, body = extract_main(root)
    text = normalize_text(body)
    if title:
        lines = text.splitlines()
        if lines and lines[0].strip() == title:
            lines = lines[1:]  # root fallback already contained the <title> text
        text = "\n".join(lines)
        text = f"[{title}]\n{text}" if text else f"[{title}]"
    return text, None


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #

def robots_allows(url: str, timeout: int = 15) -> bool:
    try:
        from urllib import robotparser
        p = urlparse_mod.urlparse(url)
        rp = robotparser.RobotFileParser()
        rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt")
        rp.read()
        if rp.can_fetch(UA, url):
            return True
        # Some robots files only define a '*' group we can still honor explicitly.
        return bool(rp.can_fetch("*", url))
    except Exception:
        return True  # unreachable/absent robots.txt -> allow


def decode_bytes(data: bytes) -> str:
    m = re.search(rb'charset=["\']?([\w-]+)', data[:4096], re.I)
    encodings = []
    if m:
        encodings.append(m.group(1).decode("ascii", "ignore"))
    encodings += ["utf-8", "windows-1256", "iso-8859-1"]
    for enc in encodings:
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def fetch_html(url: str, timeout: int = 30, ignore_robots: bool = False):
    """-> (status_code|None, html|None, error|None)"""
    if not url.lower().startswith(("http://", "https://")):
        return None, None, "unsupported URL scheme"
    if not ignore_robots and not robots_allows(url, timeout):
        return None, None, "blocked-by-robots (use --ignore-robots to override)"
    req = urlrequest.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.8",
        "Accept-Encoding": "identity",
    })
    last_err = "unknown error"
    for attempt in range(3):
        try:
            with urlrequest.urlopen(req, timeout=timeout) as r:
                data = r.read(3_000_000)
                return getattr(r, "status", 200), decode_bytes(data), None
        except urlerror.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                last_err = f"HTTP {e.code}"
                time.sleep(2 + attempt * 3)
                continue
            return e.code, None, f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
    return None, None, last_err


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def _ensure_dirs():
    for d in (DATA_DIR, SNAPS_DIR, DIFFS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    _ensure_dirs()
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}
    cfg.setdefault("settings", {
        "timeout": 30,
        "keep_snapshots": 30,
        "webhook": "",
    })
    cfg.setdefault("targets", {})
    return cfg


def save_config(cfg: dict):
    _ensure_dirs()
    cfg["saved_at"] = now_iso()
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def config_targets() -> dict:
    try:
        _ensure_dirs()
        if CONFIG_PATH.exists():
            return (json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                    .get("targets") or {})
    except Exception:
        pass
    return {}


def snapshot_dir(name: str) -> Path:
    return SNAPS_DIR / name


def diff_dir(name: str) -> Path:
    return DIFFS_DIR / name


def read_latest_snapshot(name: str):
    p = snapshot_dir(name) / "latest.txt"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def read_latest_diff(name: str):
    p = diff_dir(name) / "latest.diff.txt"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def write_snapshot(name: str, text: str, keep: int):
    d = snapshot_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    hist = d / f"{stamp}.txt"
    n = 2
    while hist.exists():  # same-second checks must not overwrite history
        hist = d / f"{stamp}_{n}.txt"
        n += 1
    hist.write_text(text, encoding="utf-8")
    (d / "latest.txt").write_text(text, encoding="utf-8")
    files = sorted(d.glob("2*.txt"))
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def write_diff(name: str, diff_text: str):
    d = diff_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{now_stamp()}.diff.txt").write_text(diff_text, encoding="utf-8")
    (d / "latest.diff.txt").write_text(diff_text, encoding="utf-8")


def diff_texts(old: str, new: str):
    old_lines = old.splitlines() if old else []
    new_lines = new.splitlines() if new else []
    diff = list(difflib.unified_diff(old_lines, new_lines,
                                     fromfile="before", tofile="after", lineterm="", n=1))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return "\n".join(diff), added, removed


# --------------------------------------------------------------------------- #
# webhook
# --------------------------------------------------------------------------- #

def fire_webhook(url: str, payload: dict, timeout: int = 15):
    if not url:
        return "none"
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(url, data=body, headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": UA,
        })
        with urlrequest.urlopen(req, timeout=timeout) as r:
            return f"ok:{getattr(r, 'status', 200)}"
    except Exception as e:  # noqa: BLE001
        return f"fail:{type(e).__name__}"


# --------------------------------------------------------------------------- #
# check engine
# --------------------------------------------------------------------------- #

def do_check(cfg: dict, name: str, t: dict, quiet: bool = False) -> dict:
    settings = cfg.get("settings", {})
    timeout = int(settings.get("timeout", 30))
    keep = int(settings.get("keep_snapshots", 30))
    res = {"name": name, "url": t.get("url", ""), "status": "error",
           "added": 0, "removed": 0, "message": ""}

    status, html, err = fetch_html(t["url"], timeout=timeout,
                                   ignore_robots=bool(t.get("ignore_robots")))
    t["last_check_ts"] = now_iso()
    t["checks"] = int(t.get("checks", 0)) + 1

    if err:
        t["last_status"] = "error"
        t["last_error"] = err
        res["message"] = err
        return res

    text, xerr = extract_content(html or "", t.get("selector") or "")
    if xerr:
        t["last_status"] = "error"
        t["last_error"] = xerr
        res["message"] = xerr
        return res
    if not text.strip():
        t["last_status"] = "empty"
        t["last_error"] = "no text extracted (JS-rendered page? try --selector)"
        res["status"] = "empty"
        res["message"] = t["last_error"]
        return res

    t.pop("last_error", None)
    prev = read_latest_snapshot(name)
    if prev is None:
        write_snapshot(name, text, keep)
        t["last_status"] = "baseline"
        res["status"] = "baseline"
        res["message"] = f"baseline saved ({len(text)} chars)"
        return res

    if prev == text:
        t["last_status"] = "unchanged"
        res["status"] = "unchanged"
        res["message"] = "no change"
        return res

    diff, added, removed = diff_texts(prev, text)
    write_snapshot(name, text, keep)
    write_diff(name, diff)
    t["last_status"] = "changed"
    t["last_changed_ts"] = now_iso()
    t["changes"] = int(t.get("changes", 0)) + 1
    t["last_added"], t["last_removed"] = added, removed
    res.update(status="changed", added=added, removed=removed,
               message=f"+{added} / -{removed} lines")

    webhook = t.get("webhook") or settings.get("webhook") or ""
    if webhook:
        excerpt = "\n".join(diff.splitlines()[:60])
        t["webhook_status"] = fire_webhook(webhook, {
            "tool": "mirsad",
            "event": "changed",
            "name": name,
            "url": t.get("url", ""),
            "checked_at": t["last_check_ts"],
            "added": added,
            "removed": removed,
            "diff_excerpt": excerpt,
            "dashboard_url": f"http://127.0.0.1:8787/diff/{urlparse_mod.quote(name)}",
        })
        res["message"] += f" (webhook {t['webhook_status']})"
    return res


def cmd_add(args) -> int:
    cfg = load_config()
    url = args.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        print("error: URL must start with http:// or https://")
        return 1
    name = args.name.strip() if args.name else make_name(url)
    if name in cfg["targets"]:
        print(f"error: target '{name}' already exists (remove it first)")
        return 1
    if args.selector:
        try:  # validate early
            parse_selector(args.selector)
        except ValueError as e:
            print(f"error: invalid selector: {e}")
            return 1
    cfg["targets"][name] = {
        "url": url,
        "selector": args.selector or "",
        "webhook": args.webhook or "",
        "ignore_robots": bool(args.ignore_robots),
        "note": args.note or "",
        "created": now_iso(),
        "checks": 0,
        "changes": 0,
        "last_status": "new",
    }
    save_config(cfg)
    print(f"added: {name}  ->  {url}")
    if not args.no_check:
        res = do_check(cfg, name, cfg["targets"][name])
        save_config(cfg)
        print(f"  initial check: [{res['status']}] {res['message']}")
    return 0


def cmd_list(args) -> int:
    cfg = load_config()
    targets = cfg["targets"]
    if args.json:
        print(json.dumps(targets, ensure_ascii=False, indent=2))
        return 0
    if not targets:
        print("(no targets yet — add one:  python mirsad.py add <url>)")
        return 0
    print(f"{'NAME':<22} {'STATUS':<10} {'CHANGES':>7}  {'LAST CHECK':<17} URL")
    print("-" * 100)
    for name, t in sorted(targets.items()):
        print(f"{name:<22} {str(t.get('last_status','?')):<10} "
              f"{int(t.get('changes',0)):>7}  "
              f"{local_dt(t.get('last_check_ts','')):<17} {t.get('url','')}")
    return 0


def cmd_remove(args) -> int:
    cfg = load_config()
    name = args.name
    if name not in cfg["targets"]:
        print(f"error: no target named '{name}'")
        return 1
    del cfg["targets"][name]
    save_config(cfg)
    print(f"removed: {name} (snapshots/diffs kept under data/)")
    return 0


def cmd_set(args) -> int:
    cfg = load_config()
    if args.name not in cfg["targets"]:
        print(f"error: no target named '{args.name}'")
        return 1
    t = cfg["targets"][args.name]
    changed = []
    if args.url:
        t["url"] = args.url
        changed.append("url")
    if args.selector is not None:
        try:
            if args.selector:
                parse_selector(args.selector)
        except ValueError as e:
            print(f"error: invalid selector: {e}")
            return 1
        t["selector"] = args.selector
        changed.append("selector")
    if args.webhook is not None:
        t["webhook"] = args.webhook
        changed.append("webhook")
    if args.note is not None:
        t["note"] = args.note
        changed.append("note")
    if args.ignore_robots:
        t["ignore_robots"] = True
        changed.append("ignore_robots=true")
    if args.no_ignore_robots:
        t["ignore_robots"] = False
        changed.append("ignore_robots=false")
    save_config(cfg)
    print(f"updated {args.name}: {', '.join(changed) or 'nothing'}")
    return 0


def cmd_check(args) -> int:
    cfg = load_config()
    settings = cfg.get("settings", {})
    names = list(cfg["targets"].keys()) if (args.all or not args.names) else args.names
    missing = [n for n in names if n not in cfg["targets"]]
    if missing:
        print(f"error: unknown target(s): {', '.join(missing)}")
        return 1
    results = []
    for i, name in enumerate(names):
        if i:
            time.sleep(1.0)  # polite spacing between requests
        res = do_check(cfg, name, cfg["targets"][name])
        results.append(res)
        if not args.json:
            icon = {"changed": "** CHANGE **", "unchanged": "ok",
                    "baseline": "baseline", "error": "ERROR", "empty": "EMPTY"}.get(res["status"], res["status"])
            print(f"[{icon}] {name}: {res['message']}")
    save_config(cfg)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    if any(r["status"] == "error" for r in results):
        return 1
    if any(r["status"] == "changed" for r in results):
        return 2
    return 0


def cmd_show(args) -> int:
    cfg = load_config()
    names = list(cfg["targets"].keys()) if not args.name else [args.name]
    for name in names:
        if name not in cfg["targets"]:
            print(f"error: no target named '{name}'")
            return 1
        print(f"=== {name} — latest diff ===")
        d = read_latest_diff(name)
        if d:
            print(d)
        else:
            print("(no diff stored yet — showing current snapshot head)")
            s = read_latest_snapshot(name) or "(empty)"
            print("\n".join(s.splitlines()[:40]))
    return 0


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #

DASH_CSS = """
:root{color-scheme:dark}
body{background:#0e1116;color:#e6edf3;font-family:'Segoe UI',Tahoma,Arial,sans-serif;margin:0;padding:26px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8b949e;font-size:13px;margin-bottom:18px}
.flash{background:#12233d;color:#c9e2ff;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px;display:inline-block}
table{width:100%;border-collapse:collapse;font-size:14px;background:#0d1017;border:1px solid #21262d;border-radius:10px;overflow:hidden}
th,td{padding:9px 12px;border-bottom:1px solid #21262d;text-align:right;vertical-align:middle}
th{background:#161b22;color:#8b949e;font-weight:600;font-size:12.5px}
tr:last-child td{border-bottom:none}
.badge{padding:2px 10px;border-radius:12px;font-size:12px;white-space:nowrap}
.b-changed{background:#3a2c07;color:#f0b429}
.b-ok{background:#0f2d1a;color:#3fb950}
.b-err{background:#3d1214;color:#f85149}
.b-base{background:#12233d;color:#58a6ff}
.url{color:#8b949e;direction:ltr;display:inline-block;max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom}
a{color:#58a6ff;text-decoration:none}
a:hover{text-decoration:underline}
.actions{display:flex;gap:10px;margin:14px 0}
.btn{background:#1f6feb;color:#fff!important;padding:7px 14px;border-radius:8px;font-size:13px;display:inline-block}
.btn.gray{background:#21262d}
.mono{font-family:Consolas,monospace;font-size:12px;color:#8b949e;direction:ltr;display:inline-block}
pre.diff{direction:ltr;text-align:left;background:#0d1017;border:1px solid #21262d;border-radius:10px;padding:16px;font-size:12.5px;line-height:1.5;overflow:auto;max-height:75vh}
pre.diff .add{color:#3fb950;background:rgba(63,185,80,.08);display:block}
pre.diff .del{color:#f85149;background:rgba(248,81,73,.08);display:block}
pre.diff .hunk{color:#58a6ff;display:block}
pre.diff .ctx{color:#8b949e;display:block}
footer{color:#8b949e;font-size:12px;margin-top:16px}
"""


def _badge(status: str) -> str:
    cls = {"changed": "b-changed", "unchanged": "b-ok", "error": "b-err",
           "baseline": "b-base", "empty": "b-err", "new": "b-base"}.get(status, "b-base")
    label = {"changed": "تغيّرت", "unchanged": "لا تغيير", "error": "خطأ",
             "baseline": "أساس", "empty": "فارغة", "new": "جديد"}.get(status, status)
    return f'<span class="badge {cls}">{h(label)}</span>'


def render_dashboard(cfg: dict, flash: str = "") -> str:
    rows = []
    for name, t in sorted(cfg["targets"].items()):
        qn = urlparse_mod.quote(name)
        webhook = t.get("webhook_status", "")
        wh = f' <span title="webhook: {h(webhook)}">{"🔔" if webhook.startswith("ok") else ("🔕" if webhook else "")}</span>' if webhook else ""
        rows.append(f"""<tr>
<td><a href="/diff/{qn}"><b>{h(name)}</b></a>{wh}</td>
<td><span class="url" title="{h(t.get('url',''))}">{h(t.get('url',''))}</span></td>
<td>{_badge(t.get('last_status','?'))}</td>
<td>{int(t.get('changes',0))}</td>
<td class="mono">{h(local_dt(t.get('last_changed_ts','')))}</td>
<td class="mono">{h(local_dt(t.get('last_check_ts','')))}</td>
<td class="actions" style="margin:0"><a href="/check/{qn}">فحص</a> <a href="/diff/{qn}">الفرق</a> <a href="/text/{qn}">النص</a></td>
</tr>""")
    table = "\n".join(rows) or '<tr><td colspan="7" style="color:#8b949e">ما في أهداف بعد — أضف واحد: <code>python mirsad.py add URL</code></td></tr>'
    flash_html = f'<div class="flash">{h(flash)}</div><br>' if flash else ""
    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>مرصد — Mirsad</title><style>{DASH_CSS}</style></head><body>
<h1>🔭 مرصد <span class="mono" style="font-size:12px">Mirsad v{VERSION}</span></h1>
<div class="sub">يراقب صفحات الويب ويخبرك بالتغيير — محلي بالكامل، بلا حساب وبلا مفاتيح.</div>
{flash_html}
<div class="actions">
<a class="btn" href="/check-all">فحص الكل الآن</a>
<a class="btn gray" href="/">تحديث</a>
</div>
<table><tr><th>الهدف</th><th>الرابط</th><th>الحالة</th><th>التغييرات</th><th>آخر تغيير</th><th>آخر فحص</th><th></th></tr>
{table}</table>
<footer>Mirsad v{VERSION} — webhook: JSON لكل تغيير (جاهز لـ n8n). البيانات: <span class="mono">{h(str(DATA_DIR))}</span></footer>
</body></html>"""


def render_diff(name: str, cfg: dict) -> str:
    d = read_latest_diff(name)
    if d is None:
        lines = ['(لسا ما في فرق محفوظ — نفّذ "فحص" أولاً أو غيّر شي بالصفحة)']
        body = f'<pre class="diff"><span class="ctx">{h(lines[0])}</span></pre>'
    else:
        out = []
        for line in d.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                out.append(f'<span class="add">{h(line)}</span>')
            elif line.startswith("-") and not line.startswith("---"):
                out.append(f'<span class="del">{h(line)}</span>')
            elif line.startswith("@@"):
                out.append(f'<span class="hunk">{h(line)}</span>')
            else:
                out.append(f'<span class="ctx">{h(line)}</span>')
        body = '<pre class="diff">' + "".join(out) + "</pre>"
    t = cfg["targets"].get(name, {})
    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>فرق — {h(name)}</title><style>{DASH_CSS}</style></head><body>
<h1>الفرق: {h(name)}</h1>
<div class="sub"><span class="url">{h(t.get('url',''))}</span> — آخر تغيير: <span class="mono">{h(local_dt(t.get('last_changed_ts','')))}</span></div>
<div class="actions"><a class="btn" href="/check/{urlparse_mod.quote(name)}">فحص الآن</a>
<a class="btn gray" href="/">رجوع للوحة</a></div>
{body}</body></html>"""


def render_text(name: str) -> str:
    s = read_latest_snapshot(name) or "(ما في نسخة محفوظة بعد)"
    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>النص — {h(name)}</title><style>{DASH_CSS}</style></head><body>
<h1>النص المحفوظ: {h(name)}</h1>
<div class="actions"><a class="btn gray" href="/">رجوع للوحة</a> <a class="btn" href="/check/{urlparse_mod.quote(name)}">فحص الآن</a></div>
<pre class="diff" style="white-space:pre-wrap">{h(s)}</pre></body></html>"""


def serve(port: int, open_browser: bool = False) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = f"Mirsad/{VERSION}"

        def log_message(self, fmt, *args):  # keep the console clean
            pass

        def _send(self, code, body: str, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, target: str):
            self.send_response(303)
            self.send_header("Location", target)
            self.end_headers()

        def do_GET(self):  # noqa: N802
            parsed = urlparse_mod.urlparse(self.path)
            path = parsed.path
            qs = urlparse_mod.parse_qs(parsed.query)
            cfg = load_config()
            try:
                if path == "/":
                    self._send(200, render_dashboard(cfg, qs.get("flash", [""])[0]))
                elif path.startswith("/diff/"):
                    name = urlparse_mod.unquote(path[len("/diff/"):])
                    if name not in cfg["targets"]:
                        self._send(404, f"<body style='background:#0e1116;color:#f85149'>no such target: {h(name)}</body>")
                        return
                    self._send(200, render_diff(name, cfg))
                elif path.startswith("/text/"):
                    name = urlparse_mod.unquote(path[len("/text/"):])
                    self._send(200, render_text(name))
                elif path.startswith("/check/"):
                    name = urlparse_mod.unquote(path[len("/check/"):])
                    if name in cfg["targets"]:
                        res = do_check(cfg, name, cfg["targets"][name])
                        save_config(cfg)
                        flash = f"{name}: {res['message']}"
                    else:
                        flash = f"no such target: {name}"
                    self._redirect("/?flash=" + urlparse_mod.quote(flash))
                elif path == "/check-all":
                    msgs = []
                    for i, (name, t) in enumerate(list(cfg["targets"].items())):
                        if i:
                            time.sleep(1.0)
                        res = do_check(cfg, name, t)
                        msgs.append(f"{name}: {res['message']}")
                    save_config(cfg)
                    self._redirect("/?flash=" + urlparse_mod.quote(" | ".join(msgs) or "no targets"))
                else:
                    self._send(404, "<body style='background:#0e1116;color:#8b949e'>not found</body>")
            except Exception as e:  # noqa: BLE001
                self._send(500, f"<body style='background:#0e1116;color:#f85149'>internal error: {h(e)}</body>")

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    print(f"Mirsad dashboard: {url}  (Ctrl+C to stop)")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    return 0


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #

SAMPLE_HTML = """<html><head><title>صفحة تجريبية</title></head><body>
<nav><a href="/a">Home</a><a href="/b">About</a><a href="/c">Contact</a></nav>
<div id="wrap">
  <div class="sidebar"><a href="/1">link one</a><a href="/2">link two</a><a href="/3">link three</a></div>
  <article><h1>عنوان تجريبي</h1>
    <p>هذا نص المقال الرئيسي الأول وهو مهم جدا.</p>
    <p>فقرة ثانية مهمة تحتوي تفاصيل أكثر عن الموضوع.</p>
    <p>فقرة ثالثة تضاف لتقوية النص الرئيسي.</p>
    <div id="price"><span class="value">1,299 TL</span></div>
  </article>
  <footer>footer junk should be skipped</footer>
</div></body></html>"""


def cmd_selftest(args) -> int:
    passed, failed = [], []

    def check(label, cond):
        (passed if cond else failed).append(label)

    root = parse_html(SAMPLE_HTML)
    hits = select(root, "#price .value")
    check("selector #price .value", len(hits) == 1 and "1,299" in node_text(hits[0]))
    hits2 = select(root, "article h1")
    check("selector article h1", len(hits2) == 1 and "عنوان" in node_text(hits2[0]))
    check("selector no-match", select(root, ".nope") == [])

    title, body = extract_main(root)
    text = normalize_text(body)
    check("readability picks article", "نص المقال الرئيسي" in text)
    check("readability skips nav/footer", "Home" not in text and "footer junk" not in text)
    check("title found", "صفحة تجريبية" in title)

    content, err = extract_content(SAMPLE_HTML, "#price")
    check("extract_content via selector", err is None and "1,299" in content)

    d, a, r = diff_texts("one\ntwo\nthree", "one\nTWO\nthree\nfour")
    check("diff counts", a >= 2 and r >= 1)

    n = normalize_text("a\n\n\nb\nb\n c ")
    check("normalize dedupe", n == "a\nb\nc")

    import tempfile
    global DATA_DIR, CONFIG_PATH, SNAPS_DIR, DIFFS_DIR
    saved = (DATA_DIR, CONFIG_PATH, SNAPS_DIR, DIFFS_DIR)
    with tempfile.TemporaryDirectory() as td:
        DATA_DIR = Path(td) / "data"
        CONFIG_PATH = DATA_DIR / "config.json"
        SNAPS_DIR = DATA_DIR / "snapshots"
        DIFFS_DIR = DATA_DIR / "diffs"
        cfg = load_config()
        cfg["targets"]["t1"] = {"url": "https://example.com"}
        save_config(cfg)
        cfg2 = load_config()
        check("config roundtrip", "t1" in cfg2["targets"])
        write_snapshot("t1", "hello", 30)
        check("snapshot roundtrip", read_latest_snapshot("t1") == "hello")
        write_diff("t1", "+x")
        check("diff roundtrip", read_latest_diff("t1") == "+x")
    DATA_DIR, CONFIG_PATH, SNAPS_DIR, DIFFS_DIR = saved

    total = len(passed) + len(failed)
    for label in passed:
        print(f"  PASS  {label}")
    for label in failed:
        print(f"  FAIL  {label}")
    print(f"\n{len(passed)}/{total} passed")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="mirsad",
        description="Mirsad (مرصد) — self-hosted page-change watcher. Local, no account, no keys.",
        epilog="Examples:\n"
               "  python mirsad.py add https://example.com\n"
               "  python mirsad.py add https://shop.example.com/item --selector '#price' --webhook http://127.0.0.1:5678/webhook/mirsad\n"
               "  python mirsad.py check --all\n"
               "  python mirsad.py serve --open",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"Mirsad {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="add a page to watch")
    a.add_argument("url")
    a.add_argument("--name", help="target name (default: derived from URL)")
    a.add_argument("--selector", help="CSS-lite selector to watch one element (e.g. '#price' or '.stock .value')")
    a.add_argument("--webhook", help="URL to POST JSON to whenever this page changes (n8n, etc.)")
    a.add_argument("--note", help="free-form note")
    a.add_argument("--ignore-robots", action="store_true", help="skip robots.txt check for this target")
    a.add_argument("--no-check", action="store_true", help="skip the initial baseline check")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="list targets")
    l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("remove", help="remove a target")
    r.add_argument("name")
    r.set_defaults(func=cmd_remove)

    se = sub.add_parser("set", help="update a target's settings")
    se.add_argument("name")
    se.add_argument("--url")
    se.add_argument("--selector", default=None, help="new selector ('' to clear)")
    se.add_argument("--webhook", default=None, help="new webhook URL ('' to clear)")
    se.add_argument("--note", default=None)
    se.add_argument("--ignore-robots", action="store_true")
    se.add_argument("--no-ignore-robots", action="store_true")
    se.set_defaults(func=cmd_set)

    c = sub.add_parser("check", help="check one/all targets for changes")
    c.add_argument("names", nargs="*", help="target name(s); default: all")
    c.add_argument("--all", action="store_true", help="check all targets")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("show", help="show the latest diff (or snapshot) for a target")
    s.add_argument("name", nargs="?")
    s.set_defaults(func=cmd_show)

    v = sub.add_parser("serve", help="run the local dashboard")
    v.add_argument("--port", type=int, default=8787)
    v.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    v.set_defaults(func=lambda a: serve(a.port, a.open))

    st = sub.add_parser("selftest", help="run offline self-tests")
    st.set_defaults(func=cmd_selftest)
    return p


def main(argv=None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
