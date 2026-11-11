"""agent/实验索引.md: every experiment in the ledger. On top, the experiments whose own dates (start, end, completion) fall
in the last 7 days; below, one table per mainline, sorted by code family: 系列 | 代号 | 白话名 | 状态 | 日期 | 花费 | 链接.
Deterministic, from the `experiments` table plus `exp_notes` (plain names the 07:00 agent adds); no model calls.
Layout rules are in the vault's agent/管理/排版规范.md.

A family is the part of the code before the first `-` (ABC-02d -> ABC, XYZ-TEST-13 -> XYZ, V1-E07 -> V1). Its name
shows once, on its first row, as `**ABC** 白话名` once the 07:00 agent has named it (`series:ABC`), else `**ABC**`.
Families with one experiment and experiments without a code share one 「其他」 group at the end of their mainline.
A code links to the heading of its mainline table: use `cli exp-link CODE`, never guess the anchor.
"""
import json
import re
from datetime import datetime, timedelta

from . import db

PAGE = 'agent/实验索引.md'
HEAD = '---\ncssclasses: [wide, r2i-index]\n---\n\n'
SERIES_UNCODED = '未编号'
OTHER = '其他'
CODE_RE = re.compile(r'^[A-Z][A-Za-z0-9.]*(?:[-/][A-Za-z0-9.]+)*$')
FIRST_RE = re.compile(r'^([^\s｜：:]+)(.*)$', re.S)
COST_LIMIT = 24
RECENT_DAYS = 7
RECENT_LIMIT = 20  # the top table stays short; the rest are in the mainline tables
OWN_DATES = ('date_start', 'date_end', 'completed_at')


def split_title(title):
    """'EXP-01-20300115｜CLEAN…' -> ('EXP-01', 'CLEAN…'); 'ABC-02：空值率检查' -> ('ABC-02', '空值率检查');
    'T3 STEP-X｜字段清洗' -> ('T3', 'STEP-X：字段清洗'). A code starts with a capital letter and has a
    digit or a hyphen; anything else ('demo-v2｜…', 'Null rate check', '字段检查｜…') is ('', title)."""
    title = (title or '').strip()
    m = FIRST_RE.match(title)
    if not m:
        return '', title
    code = re.sub(r'-20\d{6}$', '', m.group(1))  # EXP-01-20300115 -> EXP-01
    if not CODE_RE.match(code) or not re.search(r'\d|-', code):
        return '', title
    desc = re.sub(r'^[\s｜：:+]+', '', m.group(2)).replace('｜', '：')
    return code, desc.strip()


def series_of(code):
    """Code family: ABC-02d -> ABC, XYZ-TEST-13 -> XYZ, V1-E07 -> V1, IDEA-01/04 -> IDEA, V12 -> V, T2 -> T."""
    if not code:
        return SERIES_UNCODED
    parts = re.split(r'[-/]', code)
    if len(parts) == 1:
        m = re.match(r'^[A-Za-z]+', code)
        return m.group(0) if m else code
    return parts[0]


def _list(value):
    try:
        out = json.loads(value or '[]')
        return out if isinstance(out, list) else [str(out)]
    except ValueError:
        return [value] if value else []


def mainline_of(row, code=None):
    """The first mainline the experiment row names; a row that names none belongs to the first configured mainline."""
    mls = _list(row['mainline']) if 'mainline' in row.keys() else []
    return next((m for m in mls if m in db.MAINLINES), db.MAINLINES[0])


def _status(r):
    parts = [p for p in (r['stage'], r['verdict']) if p]
    return ' · '.join(parts) or '—'


def _natural(code):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', code)]


def own_date(r):
    """The experiment's latest own date (start, end or completion) as YYYY-MM-DD, or ''. Not updated_at or the
    the row edit time: a backfill touches that without the experiment moving."""
    keys = r.keys()
    return max([(r[k] or '')[:10] for k in OWN_DATES if k in keys] + [''])


def build(rows, notes):
    """{mainline: [(family or None, family plain name, [(code, desc, row)])]} in page order. `rows` are mappings with
    the experiments columns (sqlite rows or experiments.json rows); `notes` maps exp_notes code -> row. Family None
    is the 「其他」 group: one-experiment families and experiments without a code."""
    fams = {}
    for r in rows:
        code, desc = split_title(r['title'])
        fams.setdefault(mainline_of(r, code), {}).setdefault(series_of(code), []).append((code, desc, r))
    out = {}
    for ml in db.MAINLINES:
        if ml not in fams:
            continue
        sections, other = [], []
        for fam in sorted(fams[ml], key=_natural):
            items = fams[ml][fam]
            if fam == SERIES_UNCODED or len(items) == 1:
                other += items
                continue
            sname = notes.get('series:' + fam)
            sections.append((fam, sname['plain_name'] if sname and sname['plain_name'] else '', items))
        if other:
            sections.append((None, '', other))
        out[ml] = [(fam, plain, sorted(items, key=lambda x: (x[0] == '', _natural(x[0]), x[2]['date_start'] or '')))
                   for fam, plain, items in sections]
    return out


def links(rows, notes):
    """{code: '[[实验索引#<mainline heading>|code]]'} for every coded experiment (inside a table, write the | as \\|)."""
    out = {}
    for ml, sections in build(rows, notes).items():
        for _fam, _plain, items in sections:
            for code, _desc, _r in items:
                if code:
                    out[code] = '[[%s#%s|%s]]' % (PAGE.rsplit('/', 1)[-1][:-3], db.MAINLINE_NAMES[ml], code)
    return out


def _notes(conn):
    return {r['code']: r for r in conn.execute('SELECT * FROM exp_notes').fetchall()}


def _plain(notes, code, desc, r):
    note = notes.get(code) or notes.get('id:%s' % r['local_id'])
    return (note['plain_name'] if note and note['plain_name'] else desc) or (r['title'] or '—')


def _today():
    return datetime.now(db.LOCAL_TZ).date()


def page(conn):
    from .render import _cell, _short
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='experiments'").fetchone():
        return HEAD, '实验台账还没有 experiments 表。'
    notes = _notes(conn)
    rows = conn.execute('SELECT * FROM experiments').fetchall()
    groups = build(rows, notes)
    since = (_today() - timedelta(days=RECENT_DAYS - 1)).isoformat()
    out = ['> [!summary] 怎么用',
           '> 最近 %d 天动过的在最上面；下面每条主线一张总表，按系列排序，用 Ctrl+F 找代号。共 %d 个实验。'
           % (RECENT_DAYS, len(rows)),
           '', '## 最近 %d 天' % RECENT_DAYS, '']
    recent = [(own_date(r), code, desc, r) for sections in groups.values() for _f, _p, items in sections
              for code, desc, r in items if own_date(r) >= since]
    recent.sort(key=lambda x: (x[1] == '', _natural(x[1])))
    recent.sort(key=lambda x: x[0], reverse=True)  # newest day first, by code within a day
    if recent:
        out += ['| 代号 | 白话名 | 状态 | 日期 |', '|---|---|---|---|']
        out += ['| %s | %s | %s | %s |' % ('`%s`' % code if code else '—', _cell(_plain(notes, code, desc, r)),
                                          _cell(_status(r)), d[5:]) for d, code, desc, r in recent[:RECENT_LIMIT]]
        if len(recent) > RECENT_LIMIT:
            out += ['', '最近 %d 天共 %d 个，这里只列最新的 %d 个，其余在下面各主线的表里。'
                    % (RECENT_DAYS, len(recent), RECENT_LIMIT)]
    else:
        out.append('最近 %d 天没有动过的实验。' % RECENT_DAYS)
    for ml, sections in groups.items():
        out += ['', '## ' + db.MAINLINE_NAMES[ml], '',
                '| 系列 | 代号 | 白话名 | 状态 | 日期 | 花费 | 路径 |', '|---|---|---|---|---|---:|---|']
        for fam, plain, items in sections:
            label = ('**%s** %s' % (fam, _cell(plain)) if plain else '**%s**' % fam) if fam else '**%s**' % OTHER
            for i, (code, desc, r) in enumerate(items):
                link = _cell(_short(r['path'], COST_LIMIT)) if r['path'] else '—'
                cost = _short(r['cost'], COST_LIMIT) if r['cost'] else ''
                out.append('| %s | %s | %s | %s | %s | %s | %s |' % (
                    label if i == 0 else '', '`%s`' % code if code else '—', _cell(_plain(notes, code, desc, r)),
                    _cell(_status(r)), (r['date_start'] or own_date(r))[5:] or '—', _cell(cost), link))
    return HEAD, '\n'.join(out)
