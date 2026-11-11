"""agent/管理/常驻脚本.md, an inventory of every program that runs by itself: the modes.py switches, the jobs.json
LaunchAgents, and any com.r2i.<project>.*.plist that jobs.json does not know. Written by render (auto block) at most once an hour.
Columns: 名字 / 管什么 / 开关状态 / 状态 / 最后心跳 / 上线时间 ('—' when unknown: switches use mode_state.changed_at, LaunchAgents the plist mtime;
状态 and 最后心跳 are the self-check's last hourly result for a job, kept in meta 'jobs-status')."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, modes
from .night import AGENT_HEAD
from . import config

PAGE = 'agent/管理/常驻脚本.md'
JOBS_FILE = config.state_dir() / 'jobs.json'
AGENTS_DIR = Path.home() / 'Library/LaunchAgents'
EVERY = timedelta(hours=1)
SWITCH_RE = re.compile(r"^\s*'([a-z0-9-]+)':\s*'(?:off|observe|auto)',\s*#\s*([^#]*?)\s*(?:#.*)?$", re.M)


def due(conn, now=None):
    """True at most once an hour (the same pattern as handover._due)."""
    now = now or datetime.now(db.LOCAL_TZ)
    last = db.meta_get(conn, 'programs-page-at')
    if last and db.parse_time(last) > now - EVERY:
        return False
    db.meta_set(conn, 'programs-page-at', now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    return True


def _day(iso):
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M')
    except (AttributeError, ValueError):
        return '—'


def _mtime(path):
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime, db.LOCAL_TZ).strftime('%m-%d %H:%M')
    except OSError:
        return '—'


def _cell(t):
    return str(t).replace('|', '／').replace('\n', ' ')


def _mode_cell(conn, name):
    m = modes.get(conn, name)
    return '%s（到 %s）' % (m, modes.observe_until(conn, name)) if m == 'observe' else m


def rows(conn, launch_dir=None, jobs_path=None):
    """[(类型, 名字, 管什么, 开关状态, 状态, 最后心跳, 上线时间)]"""
    launch_dir = Path(launch_dir) if launch_dir is not None else AGENTS_DIR
    health = db.meta_get(conn, 'jobs-status') or {}  # {label: [状态, 最后心跳]} written by selfcheck.run
    out = []
    say = dict(SWITCH_RE.findall(Path(modes.__file__).read_text(encoding='utf-8')))
    for n in modes.DEFAULTS:
        try:
            r = conn.execute('SELECT changed_at FROM mode_state WHERE name=?', (n,)).fetchone()
        except Exception:
            r = None
        out.append(('开关', n, say.get(n, '—'), _mode_cell(conn, n), '—', '—', _day(r[0]) if r and r[0] else '—'))
    try:
        jobs = json.loads(Path(jobs_path or JOBS_FILE).read_text(encoding='utf-8'))['jobs']
    except (OSError, ValueError, KeyError):
        jobs = []
    known = set()
    for j in jobs:
        known.add(j['label'])
        state = _mode_cell(conn, j['mode']) if j.get('mode') else ('只报警' if j.get('restart') == 'alert' else '自动拉起')
        out.append(('常驻', j['label'].replace(config.label(''), ''), '%s（%s）' % (j['what'], j['schedule']), state,
                    *health.get(j['label'], ['—', '—']), _mtime(launch_dir / (j['label'] + '.plist'))))
    for p in sorted(launch_dir.glob(config.label('*') + '.plist')) if launch_dir.is_dir() else []:
        if p.stem not in known:
            out.append(('常驻', p.stem.replace(config.label(''), ''), '没登记（不在 jobs.json）', '只报警', *health.get(p.stem, ['—', '—']), _mtime(p)))
    return out


def page(conn, launch_dir=None, jobs_path=None):
    r = rows(conn, launch_dir, jobs_path)
    body = ['# 常驻脚本', '', '所有自己跑的程序：开关（modes.py）、常驻任务（jobs.json）、LaunchAgents 里没登记的。本页由脚本每小时写一次，不手改。', '',
            '| 类型 | 名字 | 管什么 | 开关状态 | 状态 | 最后心跳 | 上线时间 |', '|---|---|---|---|---|---|---|']
    body += ['| %s |' % ' | '.join(_cell(c) for c in row) for row in r]
    return AGENT_HEAD, '\n'.join(body)
