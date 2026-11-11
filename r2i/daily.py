"""Inputs for the 07:00 knowledge agent (DAILY.md): one JSON digest of what happened in a time window, and the check of
the note it wrote.

Host only. `inputs` reads the live database read-only (mode=ro), the paid-run ledger and the terminology table; writes
nothing except --out. Deterministic, so the agent spends its effort on writing, not on finding data.

    python3 -B -m r2i.daily inputs --vault "$VAULT" --out /path/inputs.json [--since ISO]
    python3 -B -m r2i.daily check --note "$VAULT/每日/2030-01-16.md" --vault "$VAULT"

`window`: from the end of the last successful report to now, local time. Start = `window_end` in the
daily heartbeat, else the previous day 07:00; `--since` overrides it for a manual rerun. Everything time-based is cut by it.
`date` is the day the note reports on (default yesterday). The note itself is named by the day it is published
: `note_date` (default: the day after `date`, which is today for the 07:00 run) and
`note_path` = 每日/<note_date>.md.
"""
import argparse
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
import json
import os
import re
import sqlite3
import sys

from . import db, exp_index, modes, terms
from . import config

LEDGER = Path(os.environ.get('R2I_LEDGER') or config.paid_dir() / 'ledger.jsonl')
OBSERVATION_LIMIT = 3000
BOOTSTRAP_LIMIT = 1500  # per experiment, when a mainline note is built for the first time from the whole ledger
STAMP_RE = re.compile(r'^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (\S+) \|')


def _local_date(stamp):
    """ISO time (UTC 'Z', offset, or bare local) -> local date string; '' when it does not parse."""
    if not stamp:
        return ''
    try:
        dt = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return str(stamp)[:10]
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=db.LOCAL_TZ)
    return dt.astimezone(db.LOCAL_TZ).strftime('%Y-%m-%d')


def connect_ro(path=None):
    path = path or config.get().ledger
    conn = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, *params):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:  # table not there yet on an older database
        return []


def _dt(stamp):
    try:
        return db.parse_time(stamp)
    except (ValueError, TypeError):
        return None


def window(vault, now=None, since=None):
    """{'start', 'end', 'label'} (local ISO times). Start: `since`, else the heartbeat's window_end when it parses and is
    before now, else the previous day 07:00."""
    now = (now or datetime.now(db.LOCAL_TZ)).astimezone(db.LOCAL_TZ)
    start = None
    if since:
        start = _dt(since)
        if not start:
            raise db.HSError('--since must be an ISO time, got %r' % since)
    else:
        from . import render
        try:
            start = _dt(json.loads((Path(vault) / render.DAILY_HEARTBEAT).read_text(encoding='utf-8'))['window_end'])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        if not start or start >= now:
            start = (now - timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    start = start.astimezone(db.LOCAL_TZ)
    return {'start': start.isoformat(timespec='seconds'), 'end': now.isoformat(timespec='seconds'),
            'label': '%s 到 %s' % (start.strftime('%m-%d %H:%M'), now.strftime('%m-%d %H:%M'))}


def _span(win):
    return _dt(win['start']), _dt(win['end'])


def _in(stamp, lo, hi):
    """`stamp` falls in [lo, hi]. A date-only value ('2030-01-15') counts when that local day overlaps the window."""
    t = _dt(stamp) if stamp else None
    if t is None:
        return False
    if len(str(stamp).strip()) == 10:
        return t < hi and t + timedelta(days=1) > lo
    return lo <= t <= hi


def paid_runs(lo, hi, ledger=LEDGER):
    """Runs that ended in [lo, hi]: budget, purpose, spent CNY, minutes."""
    starts, out = {}, []
    try:
        lines = Path(ledger).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('event') == 'start':
            starts[e.get('run_id')] = e
        elif e.get('event') == 'end' and not e.get('closed_late') and _in(e.get('time'), lo, hi):
            s = starts.get(e.get('run_id'), {})
            minutes = None
            try:
                minutes = round((datetime.fromisoformat(e['time'].replace('Z', '+00:00')) -
                                 datetime.fromisoformat(s['time'].replace('Z', '+00:00'))).total_seconds() / 60, 1)
            except (KeyError, ValueError, TypeError):
                pass
            out.append({'run_id': (e.get('run_id') or '')[:8], 'budget': s.get('budget'), 'group': s.get('group'),
                        'purpose': s.get('purpose'),
                        'conclusion': s.get('conclusion'), 'spent_cny': e.get('spent_cny'),
                        'spend_known': e.get('spend_known'), 'exit_code': e.get('exit_code'),
                        'stopped_by_cap': e.get('stopped_by_cap'), 'minutes': minutes})
    return out


def spend_without_row(runs, exps):
    """B4 (report only): paid runs with spend > 0 whose group/budget names no experiment row. A row matches when its code
    or local_id equals, or sits inside, the run's group, budget or purpose (case-insensitive)."""
    keys = {k.lower() for r in exps for k in (exp_index.split_title(r['title'])[0], r.get('local_id')) if k}
    costs = ' '.join(str(r.get('cost') or '') for r in exps).lower()  # a row whose 花费 names the budget books the run too
    out = []
    for r in runs:
        if not (r.get('spent_cny') or 0) > 0:
            continue
        hay = ' '.join(str(r.get(f) or '') for f in ('group', 'budget', 'purpose')).lower()
        if not any(k in hay for k in keys) and not (r.get('budget') and str(r['budget']).lower() in costs):
            out.append({f: r.get(f) for f in ('run_id', 'budget', 'group', 'purpose', 'spent_cny')})
    return out


def criteria_evidence(conn, win):
    """Per criterion of the current goals: ledger fields plus the audit readings in the window and the newest one before it
    (早报 v2). Evidence only: whether a line is met is the agent's call, many criteria are compound."""
    lo, hi = _span(win)
    cur = {(g['mainline'], g['version']) for g in _rows(conn, "SELECT * FROM goals WHERE status='生效'")}
    audits_ = _rows(conn, 'SELECT * FROM audits ORDER BY created_at, id')
    out = []
    for c in _rows(conn, 'SELECT * FROM criteria ORDER BY mainline, id'):
        if (c['mainline'], c['goal_version']) not in cur:
            continue
        au = [a for a in audits_ if a['crit'] == c['id'] and a.get('after')]
        pick = lambda a: {k: a.get(k) for k in ('after', 'verdict', 'title', 'created_at')}
        before = [a for a in au if (_dt(a['created_at']) or lo) < lo]
        out.append({k: c.get(k) for k in ('id', 'mainline', 'short', 'text', 'status', 'target_label', 'reading_label',
                                          'reading_kind', 'reading_at', 'reading_note')} | {
            'in_window': [pick(a) for a in au if _in(a['created_at'], lo, hi)],
            'last_before': pick(before[-1]) if before else None})
    return out


def note_rel(note_date):
    return '每日/%s.md' % note_date


def digest(conn, day, vault, ledger=LEDGER, note_date=None, now=None, since=None):
    Date.fromisoformat(day)
    note_date = note_date or (Date.fromisoformat(day) + timedelta(days=1)).isoformat()
    Date.fromisoformat(note_date)
    win = window(vault, now, since)
    lo, hi = _span(win)
    exps_all = _rows(conn, 'SELECT * FROM experiments')
    notes = {r['code']: r for r in _rows(conn, 'SELECT * FROM exp_notes')}
    links = exp_index.links(exps_all, notes)
    touched = []
    for r in exps_all:
        if not any(_in(r.get(k), lo, hi) for k in ('updated_at', 'completed_at', 'date_start')):
            continue
        code, desc = exp_index.split_title(r['title'])
        note = notes.get(code) or notes.get('id:%s' % r['local_id']) or {}
        touched.append({
            'code': code, 'title': r['title'], 'plain_name': note.get('plain_name'), 'data_layer': note.get('data_layer'),
            'mainline': exp_index.mainline_of(r, code), 'family': exp_index.series_of(code),
            'index_link': links.get(code), 'stage': r.get('stage'), 'verdict': r.get('verdict'),
            'date_start': r.get('date_start'), 'completed_at': r.get('completed_at'),
            'observation': (r.get('observation') or '')[:OBSERVATION_LIMIT], 'cost': r.get('cost'), 'path': r.get('path')})
    families_unnamed = sorted({t['family'] for t in touched if t['code'] and 'series:' + t['family'] not in notes
                               and sum(1 for x in exps_all if exp_index.series_of(exp_index.split_title(x['title'])[0])
                                       == t['family']) > 1})
    decisions = _rows(conn, 'SELECT * FROM decisions ORDER BY id')
    night = _rows(conn, 'SELECT * FROM night_items ORDER BY id')
    data = {
        'window': win,
        'date': day,  # the covered day
        'note_date': note_date,  # the publication day: the note's name, its frontmatter `date`
        'note_path': note_rel(note_date),
        'criteria_evidence': criteria_evidence(conn, win),
        'task_notes': [r for r in _rows(conn, 'SELECT * FROM task_notes ORDER BY id') if _in(r['created_at'], lo, hi)],
        'milestones': [r for r in _rows(conn, 'SELECT * FROM milestones WHERE superseded_by IS NULL ORDER BY id')
                       if _in(r['created_at'], lo, hi)],
        'experiments': touched,
        'families_without_name': families_unnamed,
        'decided': [d for d in decisions if d['status'] != '待决' and _in(d['decided_at'], lo, hi)],
        'open_decisions': [d for d in decisions if d['status'] == '待决'],
        'night_items': [n for n in night if day in (_local_date(n['created_at']), _local_date(n['decided_at']))
                        or n['status'] in ('待批', '已批')],
        'paid_runs': paid_runs(lo, hi, ledger),
        'audits': [{k: a.get(k) for k in ('id', 'mainline', 'kind', 'experiment', 'title', 'crit', 'verdict', 'editor',
                                           'thread', 'undo_status')}
                   for a in _rows(conn, 'SELECT * FROM audits ORDER BY id') if _in(a['created_at'], lo, hi)],
        'pages': {
            'mainline_notes': {ml: db.NOTE_REL[ml] for ml in db.MAINLINES},
            'progress_pages': {ml: db.PROGRESS_REL[ml] for ml in db.MAINLINES},
            'daily_note': note_rel(note_date),  # = note_path
            'recent_daily_notes': sorted(p.name for p in (Path(vault) / '每日').glob('*.md'))[-3:]
            if (Path(vault) / '每日').is_dir() else [],
            'task_record_page': task_record_rel(day),
        },
    }
    data['bootstrap'] = bootstrap(exps_all, notes, vault)
    data['paid_total_cny'] = round(sum(r['spent_cny'] or 0 for r in data['paid_runs']), 4)
    pairs = terms.load(vault)
    text = json.dumps({k: data[k] for k in ('task_notes', 'milestones', 'experiments', 'decided')}, ensure_ascii=False)
    data['old_names_in_inputs'] = sorted({(tok, now) for _n, tok, now, _l in terms.scan(text, pairs)})
    data['terminology_pairs'] = pairs
    return data


def bootstrap(exps_all, notes, vault):
    """{mainline: [every ended experiment]} for each mainline whose note does not exist yet, so the first run can
    build it from the whole ledger instead of one day."""
    out = {}
    for ml in db.MAINLINES:
        if (Path(vault) / db.NOTE_REL[ml]).exists():
            continue
        rows = []
        for r in exps_all:
            code, _desc = exp_index.split_title(r['title'])
            if exp_index.mainline_of(r, code) != ml or r.get('stage') not in db.ENDED_STAGES:
                continue
            note = notes.get(code) or {}
            rows.append({'code': code, 'title': r['title'], 'plain_name': note.get('plain_name'),
                         'stage': r.get('stage'), 'verdict': r.get('verdict'), 'date_start': r.get('date_start'),
                         'observation': (r.get('observation') or '')[:BOOTSTRAP_LIMIT], 'cost': r.get('cost')})
        out[ml] = sorted(rows, key=lambda x: (x['date_start'] or '', x['code']))
    return out


def task_record_rel(day):
    return '管理/任务记录 %s.md' % day[:7]  # no longer in the vault (task logs live on the external disk _vault-slim/task-log)


CHECK_MAX_LINES = 60
CHECK_BANNED = ('AUD-', 'TN-', 'lib:', '/Volumes', '^aud-')  # the note is for the owner: no ids, no paths
FOLD_RE = re.compile(r'^> \[!\w+\]-')
CALLOUT_RE = re.compile(r'^> \[!\w+\]')


def note_body(text):
    """The note minus its frontmatter and the folded callouts (`> [!x]-` and their `>` lines) at the end."""
    lines = text.split('\n')
    if lines and lines[0].strip() == '---' and '---' in [l.strip() for l in lines[1:]]:
        lines = lines[[l.strip() for l in lines[1:]].index('---') + 2:]
    while True:
        while lines and not lines[-1].strip():
            lines.pop()
        i = len(lines)
        while i and lines[i - 1].startswith('>'):
            i -= 1
        starts = [j for j in range(i, len(lines)) if CALLOUT_RE.match(lines[j])]
        if not starts or not FOLD_RE.match(lines[starts[-1]]):
            break
        lines = lines[:starts[-1]]
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def check_problems(text):
    body = note_body(text)
    out = []
    if len(body) > CHECK_MAX_LINES:
        out.append('正文 %d 行，超过 %d 行' % (len(body), CHECK_MAX_LINES))
    hit = [w for w in CHECK_BANNED if any(w in l for l in body)]
    if hit:
        out.append('正文里有不该出现的字样：' + '、'.join(hit))
    return out


def check(conn, note, vault):
    """Post-write check of the daily note (switch daily-check). Returns the exit code: auto and problems -> 1, else 0."""
    mode = modes.get(conn, 'daily-check')
    if mode == 'off':
        return 0
    problems = check_problems(Path(note).read_text(encoding='utf-8'))
    if not problems:
        print('ok')
        return 0
    from . import render
    try:
        rel = Path(note).relative_to(vault).as_posix()
    except ValueError:
        rel = Path(note).name
    print('；'.join(problems))
    try:
        modes.require(conn, render.log_change, 'daily-check', rel, '；'.join(problems))
    except db.HSError:
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='r2i.daily')
    sub = p.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('inputs')
    i.add_argument('--date', help='local date the note reports on; default yesterday')
    i.add_argument('--note-date', help='publication day (the note\'s name); default the day after --date')
    i.add_argument('--since', help='start of the window (ISO time); default the last report\'s window_end, else yesterday 07:00')
    c = sub.add_parser('check')
    c.add_argument('--note', required=True)
    c.add_argument('--db', default=None)
    for sp in (i, c):
        sp.add_argument('--vault', required=True)
    i.add_argument('--db', default=None)
    i.add_argument('--out')
    args = p.parse_args(argv)
    if args.cmd == 'check':
        conn = db.connect(args.db)
        try:
            return check(conn, args.note, args.vault)
        finally:
            conn.close()
    day = args.date or (datetime.now(db.LOCAL_TZ).date() - timedelta(days=1)).isoformat()
    try:
        conn = connect_ro(args.db)
        text = json.dumps(digest(conn, day, args.vault, note_date=args.note_date, since=args.since), ensure_ascii=False, indent=1)
    except db.HSError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
        print(json.dumps({'out': args.out, 'date': day, 'note_path': note_rel(args.note_date or (Date.fromisoformat(day) + timedelta(days=1)).isoformat())}, ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
