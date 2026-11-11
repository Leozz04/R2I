"""Program steps of the nightly tidy-up: threads (the day's transcript text), sync-stale (close 同步 pages nobody looked
at), repo-list (untracked files of a repo), report-check (a report note's links open, its body has no internal codes).

Usage: python -m r2i.nightly <sub> ...; every subcommand ends by printing one JSON summary line on stdout.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import archive, config, db

PER_MSG = 2000  # one huge paste must not crowd out the rest of the thread
PER_THREAD = 30000  # one chatty thread must not crowd out the other threads
TOTAL = 60000  # the whole dump feeds one paid summarising worker: past this size it runs out of budget
SYNC_DIR = '同步'  # the 同步 folder under db.REPORT_DIR; db.KIND_DIRS holds the same name
assert SYNC_DIR in db.KIND_DIRS
WAITING = '等待'  # waits.status of an open wait
UNSEEN = '待你看'  # 状态 of a page nobody has looked at
CLOSED = '已结'  # 状态 of a closed page


def _zone():
    return config.get().zone


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _texts(role, content):
    """Plain text pieces of one transcript row: a user string, or the text blocks (tool calls and results are skipped)."""
    if isinstance(content, str):
        return [content] if role == 'user' else []
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str):
            out.append(b['text'])
    return out


def mangle(path):
    """Claude Code's transcript folder name for a project directory: the path with '/' replaced by '-'."""
    return str(path).replace('/', '-')


def default_projects_dirs():
    """~/.claude/projects/<mangled project root>; Claude Code also turns other non-alphanumeric characters into '-',
    so that variant is a second candidate. Only folders that exist are returned."""
    root = config.find_root()
    base = Path('~/.claude/projects').expanduser()
    names = [mangle(root), re.sub(r'[^A-Za-z0-9-]', '-', str(root))]
    out = []
    for n in names:
        if (base / n).is_dir() and base / n not in out:
            out.append(base / n)
    return out


def _ro(dbpath):
    p = Path(dbpath)
    if not p.exists():
        return None
    return sqlite3.connect('file:%s?mode=ro' % p.resolve(), uri=True, timeout=5.0)


def _ledger_threads(dbpath):
    """{key: (title, kind)} from the ledger's threads table; keys are the thread id and its agent name."""
    conn = _ro(dbpath)
    if conn is None:
        return {}
    try:
        rows = conn.execute('SELECT id, title, kind, agent FROM threads').fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out = {}
    for tid, title, kind, agent in rows:
        out[tid] = (title, kind or 'thread')
        if agent:
            out.setdefault(agent, (title, kind or 'thread'))
    return out


def _title(path, known):
    """(title, kind) of a transcript file, or None when it is not a thread of this project (e.g. a code worker)."""
    stem = path.stem
    for key in ('cc_' + stem, stem, stem.removeprefix('agent-')):
        if key in known:
            return known[key]
    if '/subagents/' in path.as_posix():
        return None
    return 'session %s' % stem[:8], 'session'


def _cap_total(threads):
    size = lambda t: sum(len(m['text']) for m in t['messages'])
    while sum(size(t) for t in threads) > TOTAL:
        big = max(threads, key=size)
        over = sum(size(t) for t in threads) - TOTAL
        keep, acc = [], 0
        target = max(0, size(big) - over)
        for m in reversed(big['messages']):
            if acc + len(m['text']) > target:
                room = target - acc
                if room > 0:
                    keep.append(dict(m, text=m['text'][-room:]))
                break
            keep.append(m); acc += len(m['text'])
        big['messages'] = list(reversed(keep))
        if not big['messages']:
            threads.remove(big)
    return threads


def threads(date, projects=None, dbpath=None):
    """The text of every thread's messages on local `date` (configured zone), capped by PER_MSG / PER_THREAD / TOTAL."""
    day = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=_zone())
    start, end = day, day + timedelta(days=1)
    start_u, end_u = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    known = _ledger_threads(dbpath or config.get().ledger)
    out = []
    for root in projects or default_projects_dirs():
        for path in sorted(Path(root).rglob('*.jsonl')):
            try:
                if path.stat().st_mtime < start.timestamp():
                    continue
            except OSError:
                continue
            head = _title(path, known)
            if not head:
                continue
            msgs = []
            try:
                lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                    role = row.get('type')
                    if role not in ('user', 'assistant'):
                        continue
                    at = datetime.fromisoformat(str(row['timestamp']).replace('Z', '+00:00'))
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    if not (start_u <= at < end_u):
                        continue
                    for t in _texts(role, (row.get('message') or {}).get('content')):
                        if t.strip():
                            msgs.append({'at': row['timestamp'], 'role': role, 'text': t[:PER_MSG]})
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
            kept, acc = [], 0
            for m in reversed(msgs):  # keep the end of the day
                if acc + len(m['text']) > PER_THREAD:
                    room = PER_THREAD - acc
                    if room > 0:
                        kept.append(dict(m, text=m['text'][-room:]))
                    break
                kept.append(m); acc += len(m['text'])
            if kept:
                out.append({'title': head[0], 'type': head[1], 'file': str(path), 'messages': list(reversed(kept))})
    return _cap_total(out)


def _open_wait_reports(dbpath):
    conn = _ro(dbpath)
    if conn is None:
        return set()
    try:
        return {r[0] for r in conn.execute('SELECT report FROM waits WHERE status=? AND report IS NOT NULL', (WAITING,))}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def sync_stale(vault, days, apply=False, dbpath=None, now=None):
    """同步 pages still marked unseen for `days` days (and not held by an open wait): list them, or close them with --apply."""
    vault = Path(vault)
    now = now or datetime.now(db.LOCAL_TZ)
    held = _open_wait_reports(dbpath or config.get().ledger)
    folder = vault / db.REPORT_DIR / SYNC_DIR
    cands = []
    for path in sorted(folder.glob('*.md')) if folder.is_dir() else []:
        try:
            text, mtime = path.read_text(encoding='utf-8'), path.stat().st_mtime
        except (OSError, UnicodeDecodeError):
            continue
        if archive._front(text).get('状态', '').strip() != UNSEEN or path.stem in held:
            continue
        upd = archive.last_update(text, mtime, now)
        if now - upd >= timedelta(days=days):
            cands.append((path, text, upd))
    applied = 0
    if apply:
        stamp = now.astimezone(_zone()).strftime('%Y-%m-%d %H:%M')
        note = '> [!note] Nightly tidy %s: not looked at for over %s days, key points kept in the knowledge base, closed automatically' % (stamp, days)
        for path, text, _ in cands:
            new = archive.set_prop(text, '状态', CLOSED).rstrip('\n') + '\n\n' + note + '\n'
            archive._write_keep_mtime(path, new)
            applied += 1
    return {'days': days, 'candidates': [{'page': p.relative_to(vault).as_posix(), 'updated': u.strftime(archive.FMT)}
                                         for p, _, u in cands], 'applied': applied}


def _stat(p):
    st = p.stat()
    return st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()


def repo_list(repo):
    """Untracked (not ignored) files of a git repo with size and mtime, plus its top-level entries."""
    repo = Path(repo)
    r = subprocess.run(['git', '-C', str(repo), 'ls-files', '--others', '--exclude-standard'],
                       capture_output=True, text=True, check=True)
    untracked = []
    for rel in filter(None, r.stdout.splitlines()):
        try:
            size, mt = _stat(repo / rel)
        except OSError:
            continue
        untracked.append({'path': rel, 'size': size, 'mtime': mt})
    top = []
    for p in sorted(repo.iterdir()):
        try:
            size, mt = _stat(p)
        except OSError:
            continue
        top.append({'name': p.name, 'is_dir': p.is_dir(), 'size': size, 'mtime': mt})
    return {'repo': str(repo), 'untracked': untracked, 'top_level': top}


_LINK = re.compile(r'\[\[([^\]\|#]*?)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]')
_INLINE_CODE = re.compile(r'(`+)(?!`).*?(?<!`)\1(?!`)')
_FOLD = re.compile(r'^\s*>\s*\[!(?:note\]-\s*%s|term\]-)' % re.escape(archive.FOR_THREAD.split()[-1]))  # folded blocks are for threads
_A = r'(?<![A-Za-z0-9_])'
_Z = r'(?![A-Za-z0-9_])'
_CODE = re.compile('|'.join([
    _A + r'[a-z][a-z-]*-v\d+-\d+' + _Z,           # acceptance line id, e.g. check-v2-02
    _A + r'[A-Z][A-Z0-9]{0,5}-\d+(?:-\d+)*',      # AUD-20300115-01, R-10, S8-1, AB-43
    _A + r'wait-\d[0-9A-Za-z_-]*',
    _A + r'RR-\d[0-9A-Za-z_-]*',
    _A + r'cc_[0-9a-f][0-9a-f-]*',                # thread / message ids
    _A + r'c\d+' + _Z,                            # c3
    _A + r'[A-Z]{1,4}\d+' + _Z,                   # S11
]))

NAMES_OK = {'Z3'}  # real tool names that look like codes (Z3 = the SMT solver)


def _blank(m):
    return ' ' * len(m.group(0))


def _mask_code(text):
    """(lines, folded): frontmatter, fenced code and inline code blanked out (line numbers kept); folded[i] is True
    when line i sits in a folded block."""
    lines = text.split('\n')
    out, folded = [], []
    in_front = bool(lines) and lines[0].strip() == '---'
    in_fence = False
    in_fold = False
    for i, line in enumerate(lines):
        if in_front:
            out.append(''); folded.append(False)
            if i > 0 and line.strip() == '---':
                in_front = False
            continue
        st = line.lstrip()
        if st.startswith('```') or st.startswith('~~~'):
            in_fence = not in_fence
            out.append(''); folded.append(False)
            continue
        if in_fence:
            out.append(''); folded.append(False)
            continue
        if _FOLD.match(line):
            in_fold = True
        elif not st.startswith('>'):
            in_fold = False
        out.append(_INLINE_CODE.sub(_blank, line)); folded.append(in_fold)
    return out, folded


def _resolve_ok(vault, index, target):
    rel = target.strip().lstrip('/')
    if not rel:
        return True
    if os.path.isfile(os.path.join(vault, rel)) or os.path.isfile(os.path.join(vault, rel + '.md')):
        return True
    base = os.path.basename(rel)
    stem = base[:-3] if base.endswith('.md') else base
    found = {path for name, paths in index.items()
             if name == base or os.path.splitext(name)[0] == stem for path in paths}
    return len(found) == 1


def report_check(vault, note):
    """Every [[link]] of the note opens (exactly one match in the vault) and its visible text has no internal codes."""
    vault = str(vault)
    text = Path(note).read_text(encoding='utf-8')
    lines, folded = _mask_code(text)
    index = {}
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in ('.obsidian', '.trash')]
        for f in files:
            index.setdefault(f, []).append(os.path.join(root, f))
    links, bad, codes = 0, [], []
    for i, line in enumerate(lines):
        for m in _LINK.finditer(line):
            links += 1
            t = m.group(1).rstrip('\\').strip()
            if t and t not in bad and not _resolve_ok(vault, index, t):
                bad.append(t)
        if folded[i]:
            continue
        plain = _LINK.sub(_blank, line)
        for m in _CODE.finditer(plain):
            if m.group(0) in NAMES_OK:
                continue
            codes.append({'line': i + 1, 'match': m.group(0), 'text': text.split('\n')[i][:80]})
    return {'links': links, 'bad_links': bad, 'codes': codes, 'ok': not bad and not codes}


def _dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='r2i.nightly', description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('threads', help="dump a day's thread text")
    a.add_argument('--date', required=True, help='YYYY-MM-DD in the project time zone')
    a.add_argument('--out', required=True)
    a.add_argument('--projects-dir', action='append', help='transcript folder (repeatable); default ~/.claude/projects/<mangled project root>')
    a.add_argument('--db', help='ledger path (default: r2i.toml ledger)')
    b = sub.add_parser('sync-stale', help='list or close stale 同步 pages')
    b.add_argument('--vault', help='default: r2i.toml vault')
    b.add_argument('--days', type=int, required=True)
    b.add_argument('--apply', action='store_true')
    b.add_argument('--out')
    b.add_argument('--db', help='ledger path (default: r2i.toml ledger)')
    c = sub.add_parser('repo-list', help='untracked files of a repo')
    c.add_argument('--repo', required=True)
    c.add_argument('--out', required=True)
    d = sub.add_parser('report-check', help='links open, no internal codes')
    d.add_argument('--vault', help='default: r2i.toml vault')
    d.add_argument('--note', required=True)
    d.add_argument('--out')
    args = ap.parse_args(argv)
    if args.cmd == 'report-check':
        summary = report_check(args.vault or config.get().vault, args.note)
        if args.out:
            _dump(args.out, summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if summary['ok'] else 1
    if args.cmd == 'threads':
        res = threads(args.date, args.projects_dir, args.db)
        _dump(args.out, res)
        summary = {'date': args.date, 'threads': len(res), 'messages': sum(len(t['messages']) for t in res),
                   'chars': sum(len(m['text']) for t in res for m in t['messages']), 'out': args.out}
    elif args.cmd == 'sync-stale':
        summary = sync_stale(args.vault or config.get().vault, args.days, args.apply, args.db)
        if args.out:
            _dump(args.out, summary)
    else:
        res = repo_list(args.repo)
        _dump(args.out, res)
        summary = {'untracked': len(res['untracked']), 'top_level': len(res['top_level']), 'out': args.out}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
