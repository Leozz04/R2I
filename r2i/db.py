"""Writes for r2i-sync (the DDL is r2i/schema/schema.sql). Owns: goals, criteria, milestones, milestone_reviews, decisions, resources,
night_items, verbal_budgets."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping, Sequence
import json
import math
import os
import re
import sqlite3
import sys

from . import config, experiments
from .ids import MSG_RE, THREAD_HINT, is_thread_id


class _Mainlines(Sequence):
    """The configured mainline ids, read lazily so importing this module needs no project."""
    def __getitem__(self, i):
        return tuple(m.id for m in config.get().mainlines)[i]

    def __len__(self):
        return len(config.get().mainlines)

    def __repr__(self):
        return repr(tuple(self))


class _MainlineMap(Mapping):
    def __init__(self, fn):
        self.fn = fn

    def _d(self):
        return {m.id: self.fn(m) for m in config.get().mainlines}

    def __getitem__(self, k):
        return self._d()[k]

    def __iter__(self):
        return iter(self._d())

    def __len__(self):
        return len(self._d())


MAINLINES = _Mainlines()
MAINLINE_NAMES = _MainlineMap(lambda m: m.name)
SHORT_NAMES = _MainlineMap(lambda m: m.short)
# Vault file names are unique across folders: Obsidian's graph labels nodes by file name only.
PROGRESS_REL = _MainlineMap(lambda m: '进度/%s 进度.md' % m.short)
NOTE_REL = _MainlineMap(lambda m: 'agent/主线/%s.md' % m.name)
HANDOVER_REL = _MainlineMap(lambda m: 'agent/交接/%s 交接.md' % m.short)
KINDS = ('里程碑', '正结果', '负结果', '大跑完成', '版本变更', 'pivot', '实战教训')
CRITERIA_STATUS = ('未验证', '部分', '满足', '被反证')
GOAL_STATUS = ('待定义', '生效', '已取代')
DECISION_STATUS = ('待决', '已决', '撤回')
REVIEW_COLUMNS = ('reviewer', 'review_due')  # B1: who reviews an 「已运行待复核」 row, and by when
REVIEW_STAGE = '已运行待复核'
ENDED_STAGES = experiments.ENDED_STAGES
# Night queue: a paid task a thread wants to run overnight. the owner's tick in 今晚可跑 approves that one item.
NIGHT_STATUS = ('待批', '已批', '撤回', '过期')
LOCAL_TZ = config.LocalTZ()  # the configured zone; deadlines are "tomorrow 09:00" there
NIGHT_DEADLINE_HOUR = 9
NIGHT_MAX_HOURS = 36  # a night item cannot reach further than the next morning
# Verbal budgets: a thread records a paid budget
# the owner approved in words, citing their message; the host writes it into BUDGETS.json. Never NQ- ids, never overwrites.
VERBAL_STATUS = ('有效', '撤回')
WAIT_STATUS = ('等待', '已回', '已勾', '过期')
IDEA_STATUS = ('新', '在试', '已证伪', '已采纳')  # ideas / hypotheses
KEYLIKE = re.compile(r'[A-Za-z0-9_\-+=]{32,}')  # a key value, not a name or path ('/' splits paths)
RESOURCE_FIELDS = ('kind', 'unit', 'balance', 'limit_note', 'reset_at', 'source',
                   'category', 'purpose', 'credential_ref', 'check_method', 'runs_on')
VERBAL_ID_RE = re.compile(r'^[A-Z][A-Za-z0-9._-]{2,63}$')
VERBAL_DEFAULT_HOURS = 72
VERBAL_MAX_DAYS = 7


class HSError(Exception):
    pass


READING_FIELDS = ('short', 'reading', 'reading_label', 'target', 'target_label', 'scale_max', 'direction',
                  'reading_kind', 'reading_note', 'reading_at')
READING_NUMBERS = ('reading', 'target', 'scale_max')
READING_DIRECTIONS = ('higher', 'lower')
READING_KINDS = ('基线', '验收')
SHORT_MAX = 12
DEC_TITLE_MAX = 25


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


CURRENT = {'conn': None, 'path': None}  # the connection log_change writes the 变更 table through


SCHEMA_FILE = Path(__file__).parent / 'schema' / 'schema.sql'
MIGRATIONS_DIR = Path(__file__).parent / 'schema' / 'migrations'


def _migrate(conn):
    """Apply migrations/NNNN_*.sql above the ledger's schema_version, in order, bumping the version after each.
    shortcut: a script that fails halfway leaves its earlier statements applied, keep each migration small."""
    ver = meta_get(conn, 'schema_version', 1)
    for f in sorted(MIGRATIONS_DIR.glob('[0-9][0-9][0-9][0-9]_*.sql')):
        n = int(f.name[:4])
        if n > ver:
            conn.executescript(f.read_text(encoding='utf-8'))
            meta_set(conn, 'schema_version', ver := n)


def connect(path=None):
    path = path or config.get().ledger
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA foreign_keys=ON')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():  # a new ledger
        conn.executescript(SCHEMA_FILE.read_text(encoding='utf-8'))
        meta_set(conn, 'schema_version', 1)
    else:
        _migrate(conn)
    conn.executemany('INSERT INTO mainlines(id,name,short) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET '
                     'name=excluded.name, short=excluded.short',
                     [(m.id, m.name, m.short) for m in config.get().mainlines])  # before anything that references it
    for ml in MAINLINES:  # every mainline starts with an undefined goal v0 until the owner defines it
        conn.execute("INSERT OR IGNORE INTO goals(mainline,version,goal,status,created_at) VALUES (?,0,'待定义','待定义',?)",
                     (ml, now()))
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created', ?)", (json.dumps(datetime.now(LOCAL_TZ).date().isoformat()),))
    conn.commit()
    CURRENT['conn'], CURRENT['path'] = conn, path
    return conn


def slim_root():
    """Where what left the vault goes: task-log/, _old/<YYYYMMDD>/<vault path>, MOVES.tsv, under <runs_root>/_vault-slim.
    $R2I_SLIM_ROOT overrides it (tests). No other place is ever tried."""
    return Path(os.environ.get('R2I_SLIM_ROOT') or config.slim_root())


def slim_ready():
    """True when the runs disk is mounted: /Volumes/<disk> is a mount point (when the root is on /Volumes) and
    the runs_root folder exists. False means: skip this round and try again next round."""
    root = slim_root()
    parts = root.parts
    if len(parts) > 2 and parts[1] == 'Volumes' and not (Path('/Volumes') / parts[2]).is_mount():
        return False
    return root.parent.is_dir()


def slim_skip_line(what):
    return '%s：%s 不在（外置盘没挂载？），这一轮先不做，下一轮再试' % (what, slim_root().parent)


def add_change(conn, editor, path, summary, at=None):
    path = path[:-3] if path.endswith('.md') else path
    conn.execute('INSERT INTO changes(at,editor,path,summary) VALUES (?,?,?,?)', (at or now(), editor, path, summary))
    conn.commit()


def change_line(r):
    """One 变更 row as the old 变更记录 line: 「- 时间 | 编辑者 | [[笔记]] | 摘要」 (local time)."""
    return '- %s | %s | [[%s]] | %s\n' % (parse_time(r['at']).astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z'),
                                          r['editor'].replace('|', '／'), r['path'], r['summary'].replace('|', '／'))


def get_changes(conn, path=None, since=None, limit=200):
    """Change-table rows, oldest first. `since`: YYYY-MM-DD (local day start) or an ISO time; `path`: the page, with
    or without .md."""
    sql, p = 'SELECT * FROM changes WHERE 1=1', []
    if path:
        base = path[:-3] if path.endswith('.md') else path
        sql += ' AND (path=? OR path=?)'; p += [base, base + '.md']
    if since:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', since):
            since = datetime.strptime(since, '%Y-%m-%d').replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat(
                timespec='seconds').replace('+00:00', 'Z')
        sql += ' AND at>=?'; p.append(since)
    rows = conn.execute('SELECT * FROM (%s ORDER BY id DESC LIMIT ?) ORDER BY id' % sql, p + [limit]).fetchall()
    return [dict(r) for r in rows]


def _check(value, allowed, name):
    if value not in allowed:
        raise HSError('%s must be one of %s, got %r' % (name, '/'.join(allowed), value))


def _date(text):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text or ''):
        raise HSError('date must be YYYY-MM-DD, got %r' % text)
    return text


def _next_id(conn, table, prefix):
    rows = conn.execute('SELECT id FROM %s WHERE id LIKE ?' % table, (prefix + '%',)).fetchall()
    n = max([int(r['id'].rsplit('-', 1)[1]) for r in rows] or [0]) + 1
    return '%s%02d' % (prefix, n)


def current_goal(conn, mainline):
    return conn.execute('SELECT * FROM goals WHERE mainline=? AND status!=? ORDER BY version DESC LIMIT 1',
                        (mainline, '已取代')).fetchone()


def check_goal(mainline, goal, criteria, approved_by):
    """Goals and acceptance criteria change only with the owner's approval, and never with ‹…› placeholders left in."""
    _check(mainline, MAINLINES, 'mainline')
    if not goal or not goal.strip():
        raise HSError('goal text is required')
    crit = [c.strip() for c in (criteria or []) if c and c.strip()]
    if not crit:
        raise HSError('at least one --criterion is required')
    if any('‹' in t or '›' in t for t in [goal] + crit):
        raise HSError('placeholders ‹…› are still in the goal or criteria; fill them in first')
    if not approved_by or config.owner() not in approved_by:
        raise HSError(config.say("goals change only with {owner}'s approval: --approved-by must name {owner} and where they approved"))
    return crit


def set_goal(conn, *, mainline, goal, criteria, approved_by, evidence=None):
    """New goal version with its criteria. Earlier versions become 已取代 and stay for history."""
    crit = check_goal(mainline, goal, criteria, approved_by)
    last = conn.execute('SELECT MAX(version) FROM goals WHERE mainline=?', (mainline,)).fetchone()[0]
    version = (last if last is not None else -1) + 1
    t = now()
    conn.execute("UPDATE goals SET status='已取代' WHERE mainline=? AND status!='已取代'", (mainline,))
    conn.execute("INSERT INTO goals(mainline,version,goal,status,trigger_evidence,approved_by,approved_at,created_at) "
                 "VALUES (?,?,?,'生效',?,?,?,?)", (mainline, version, goal.strip(), evidence, approved_by, t, t))
    ids = []
    for i, text in enumerate(crit, 1):
        ids.append('%s-v%d-%02d' % (mainline, version, i))
        conn.execute("INSERT INTO criteria(id,mainline,goal_version,text,status,updated_at) VALUES (?,?,?,?,'未验证',?)",
                     (ids[-1], mainline, version, text, t))
    conn.commit()
    return {'mainline': mainline, 'version': version, 'criteria': ids}


def set_criterion(conn, cid, status, evidence=None):
    """Progress on one acceptance criterion. Any status other than 未验证 needs evidence."""
    _check(status, CRITERIA_STATUS, 'status')
    row = conn.execute('SELECT * FROM criteria WHERE id=?', (cid,)).fetchone()
    if not row:
        raise HSError('no criterion %r' % cid)
    if status != '未验证' and not (evidence or row['evidence']):
        raise HSError('status %s needs --evidence' % status)
    conn.execute('UPDATE criteria SET status=?, evidence=COALESCE(?, evidence), updated_at=? WHERE id=?',
                 (status, evidence, now(), cid))
    conn.commit()
    return {'id': cid, 'status': status}


def add_milestone(conn, *, date, mainline, kind, summary, editor, impact=None, exp_id=None, evidence=None,
                  cost=None, duration=None, sync_page=False):
    _date(date)
    _check(mainline, MAINLINES, 'mainline')
    _check(kind, KINDS, 'kind')
    if not summary or len(summary) > 120:
        raise HSError('summary must be one sentence of at most 120 characters')
    if exp_id:
        dup = conn.execute('SELECT id FROM milestones WHERE exp_id=? AND kind=? AND superseded_by IS NULL',
                           (exp_id, kind)).fetchone()
        if dup:
            raise HSError('milestone for %s/%s already exists: %s (use supersede)' % (exp_id, kind, dup['id']))
    goal = current_goal(conn, mainline)
    mid = _next_id(conn, 'milestones', 'MS-%s-' % date.replace('-', ''))
    conn.execute('INSERT INTO milestones(id,date,mainline,kind,summary,impact,exp_id,evidence,cost,duration,'
                 'goal_version,editor,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (mid, date, mainline, kind, summary, impact, exp_id, evidence, cost, duration,
                  goal['version'] if goal else None, editor, now()))
    if sync_page:
        conn.execute("UPDATE milestones SET sync_page='1' WHERE id=?", (mid,))
    if exp_id:
        conn.execute('INSERT OR REPLACE INTO milestone_reviews VALUES (?,?,?,?,?)',
                     (exp_id, 'milestone', mid, editor, now()))
    conn.commit()
    return mid


def supersede(conn, old_id, new_id):
    for i in (old_id, new_id):
        if not conn.execute('SELECT 1 FROM milestones WHERE id=?', (i,)).fetchone():
            raise HSError('unknown milestone ' + i)
    conn.execute('UPDATE milestones SET superseded_by=? WHERE id=?', (new_id, old_id))
    conn.commit()


def skip_experiment(conn, exp_id, reviewer):
    conn.execute('INSERT OR REPLACE INTO milestone_reviews VALUES (?,?,?,?,?)', (exp_id, 'skip', None, reviewer, now()))
    conn.commit()


def pending_experiments(conn):
    """Ended experiments nobody has judged yet (milestone or skip). Empty list if experiments table is absent."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='experiments'").fetchone():
        return []
    marks = ','.join('?' * len(ENDED_STAGES))
    return conn.execute(
        'SELECT local_id,title,stage,verdict,completed_at,date_start FROM experiments e WHERE stage IN (%s) '
        'AND NOT EXISTS (SELECT 1 FROM milestone_reviews r WHERE r.exp_id=e.local_id) '
        'ORDER BY COALESCE(completed_at,date_start) DESC' % marks, ENDED_STAGES).fetchall()


def check_decision_text(title=None, options=None):
    """A decision's short title (<= 25 characters, one line) and its options (one per line, 「做什么：后果」)."""
    if title is not None:
        title = ' '.join(title.split())
        if not title:
            raise HSError('--title is empty')
        if len(title) > DEC_TITLE_MAX:
            raise HSError('--title must be at most %d characters, got %d' % (DEC_TITLE_MAX, len(title)))
    opts = [' '.join(o.split()) for o in (options or []) if o and o.strip()]
    return title, ('\n'.join(opts) or None)


def check_thread(thread):
    if thread is not None and not is_thread_id(thread):
        raise HSError('--thread must be the asking thread id, %s; got %r' % (THREAD_HINT, thread))
    return thread


def add_decision(conn, mainline, question, source=None, title=None, options=None, thread=None):
    _check(mainline, MAINLINES, 'mainline')
    title, opts = check_decision_text(title, options)
    thread = check_thread(thread)
    did = _next_id(conn, 'decisions', 'DEC-')
    conn.execute('INSERT INTO decisions(id,mainline,question,source,created_at,title,options,thread) VALUES (?,?,?,?,?,?,?,?)',
                 (did, mainline, question, source, now(), title, opts, thread))
    conn.commit()
    return did


def retitle_decision(conn, did, title, options=None):
    """Adds or changes the short title (and options) of a decision; the question text and the status stay as they are."""
    title, opts = check_decision_text(title, options)
    row = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
    if not row:
        raise HSError('no decision %r' % did)
    conn.execute('UPDATE decisions SET title=?, options=COALESCE(?, options) WHERE id=?', (title, opts, did))
    conn.commit()
    return {'id': did, 'mainline': row['mainline'], 'title': title}


DETAIL_MAX = 4000


def clean_detail(text):
    """dec-detail body: the decision page's text between its title and 「## 回复」. Front matter,
    a leading 「# 标题」 and any 「## 回复」 section are dropped (the script writes those)."""
    t = (text or '').replace('\r\n', '\n').strip()
    if t.startswith('---\n') and '\n---' in t[3:]:
        t = t[t.index('\n---', 3) + 4:].strip()
    t = re.sub(r'\A# [^\n]*\n+', '', t)
    t = re.sub(r'^## 回复[ \t]*\n.*?(?=^## |\Z)', '', t, flags=re.M | re.S).strip()
    if not t:
        raise HSError('--file is empty (after dropping front matter, title and 「## 回复」)')
    if len(t) > DETAIL_MAX:
        raise HSError('detail must be at most %d characters, got %d' % (DETAIL_MAX, len(t)))
    if re.search(r'^[ \t]*[-*+] \[.\]', t, re.M):
        raise HSError('no checkboxes in the body: the script writes the 「## 回复」 options (dec-retitle --option)')
    return t


def set_detail(conn, did, text):
    t = clean_detail(text)
    row = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
    if not row:
        raise HSError('no decision %r' % did)
    conn.execute('UPDATE decisions SET detail=? WHERE id=?', (t, did))
    conn.commit()
    from .dec_pages import page_name
    return {'id': did, 'mainline': row['mainline'], 'chars': len(t), 'page': page_name(row)}


def check_reading(fields):
    """crit-read values: direction higher/lower, kind 基线/验收, numbers for reading/target, scale_max > 0, short <= 12."""
    out = {}
    for k, v in fields.items():
        if v is None:
            continue
        if k in READING_NUMBERS:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise HSError('--%s must be a number, got %r' % (k.replace('_', '-'), v))
            if v != v or v in (float('inf'), float('-inf')):
                raise HSError('--%s must be a finite number' % k.replace('_', '-'))
        else:
            v = ' '.join(str(v).split())
        out[k] = v
    if 'direction' in out:
        _check(out['direction'], READING_DIRECTIONS, 'direction')
    if 'reading_kind' in out:
        _check(out['reading_kind'], READING_KINDS, 'kind')
    if 'scale_max' in out and out['scale_max'] <= 0:
        raise HSError('--scale-max must be greater than 0')
    if 'reading_at' in out:
        _date(out['reading_at'])
    if len(out.get('short') or '') > SHORT_MAX:
        raise HSError('--short must be at most %d characters' % SHORT_MAX)
    return out


def set_reading(conn, cid, **fields):
    """The current reading of one criterion (crit-read). Leaves its text and status alone, so it needs no approval;
    only the fields given change. A reading needs a scale_max, given now or earlier."""
    row = conn.execute('SELECT * FROM criteria WHERE id=?', (cid,)).fetchone()
    if not row:
        raise HSError('no criterion %r' % cid)
    vals = check_reading({k: fields.get(k) for k in READING_FIELDS})
    if not vals:
        raise HSError('nothing to set')
    merged = dict(row)
    merged.update(vals)
    if merged.get('reading') is not None and not merged.get('scale_max'):
        raise HSError('a reading needs --scale-max')
    vals['updated_at'] = now()  # 进度页「读数最后更新」reads it
    conn.execute('UPDATE criteria SET %s WHERE id=?' % ', '.join('%s=?' % k for k in vals), list(vals.values()) + [cid])
    conn.commit()
    return {'id': cid, 'mainline': row['mainline'], 'set': sorted(k for k in vals if k != 'updated_at')}


def close_decision(conn, did, status, answer=None, via=None):
    _check(status, DECISION_STATUS, 'status')
    cur = conn.execute('UPDATE decisions SET status=?, answer=COALESCE(?,answer), decided_at=?, answered_via=? '
                       'WHERE id=? AND status=?', (status, answer, now(), via, did, '待决'))
    conn.commit()
    return cur.rowcount


ACT_MAX = 40


def act_decision(conn, did, done, editor, thread=None):
    """A thread settles an open decision itself when the step costs nothing, can be undone and
    changes no goal or rule. 首页 then shows 「已按 X 做 · 不同意勾「改」」; the owner's 改 reopens it and wakes the thread."""
    done = ' '.join((done or '').split())
    if not done or len(done) > ACT_MAX:
        raise HSError('--done: what you did, in at most %d characters (e.g. 「先只登记」)' % ACT_MAX)
    if not (editor or '').strip():
        raise HSError('--editor (your thread title) is required')
    check_thread(thread)
    d = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
    if not d:
        raise HSError('no decision %s' % did)
    if d['status'] != '待决':
        raise HSError('%s is %s, not 待决' % (did, d['status']))
    reg = (d['thread'] or '').strip()
    if reg and (thread or '').strip() != reg:
        raise HSError('决策 %s 登记在线程 %s，用 --thread %s 来做，或先 wait-move 交接' % (did, reg, reg))
    if not (thread or d['thread']):
        raise HSError(config.say('%s has no thread: pass --thread cc_<uuid> so {owner}\'s 改 can wake you') % did)
    conn.execute("UPDATE decisions SET status='已决', answer=?, decided_at=?, answered_via='线程', acted_by=?, "
                 "thread=COALESCE(?,thread) WHERE id=? AND status='待决'", (done, now(), editor.strip(), thread, did))
    conn.commit()
    return {'id': did, 'status': '已决', 'answer': done, 'answered_via': '线程', 'acted_by': editor.strip()}


def revert_acted(conn, did):
    """the owner ticked 改 under a thread's own decision: the question is open again for them (the thread rolls back)."""
    cur = conn.execute("UPDATE decisions SET status='待决', answer=NULL, decided_at=NULL, answered_via=NULL "
                       "WHERE id=? AND status='已决' AND answered_via='线程'", (did,))
    conn.commit()
    return cur.rowcount


def reopen_decision(conn, did):
    """the owner unticked the option they had ticked on 首页: the answer is withdrawn and the question is open again."""
    cur = conn.execute("UPDATE decisions SET status='待决', answer=NULL, decided_at=NULL, answered_via=NULL "
                       "WHERE id=? AND status='已决' AND answered_via IN ('首页','页')", (did,))
    conn.commit()
    return cur.rowcount


def set_resource(conn, service, **fields):
    bad = set(fields) - set(RESOURCE_FIELDS)
    if bad:
        raise HSError('unknown resource fields: %s' % ','.join(sorted(bad)))
    if fields.get('credential_ref') and KEYLIKE.search(fields['credential_ref']):
        raise HSError('credential_ref names where a key lives (Keychain item, file, variable), never the key itself')
    conn.execute('INSERT OR IGNORE INTO resources(service) VALUES (?)', (service,))
    for k, v in fields.items():
        if v is not None:
            conn.execute('UPDATE resources SET %s=? WHERE service=?' % k, (v, service))
    if fields.get('balance') is not None or (fields.get('limit_note') is not None and fields.get('source') is not None):
        # checked_at = when the balance or state was last read, not when catalog metadata changed
        conn.execute('UPDATE resources SET checked_at=? WHERE service=?', (now(), service))
    conn.commit()


def baseline_reviews(conn, reviewer):
    """One-time: mark every already-ended, unjudged experiment as reviewed so `pending` starts from today."""
    rows = pending_experiments(conn)
    conn.executemany('INSERT OR IGNORE INTO milestone_reviews VALUES (?,?,?,?,?)',
                     [(r['local_id'], 'skip', None, reviewer, now()) for r in rows])
    conn.commit()
    return len(rows)


def meta_get(conn, key, default=None):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def meta_set(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


# ---- night queue ----

def _utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_time(text):
    dt = datetime.fromisoformat(str(text).strip().replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)  # a bare time means the Mac's local time


def default_deadline(at=None):
    """The next 09:00 the local zone after `at`: the morning a night item stops being valid."""
    local = (at or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    cut = local.replace(hour=NIGHT_DEADLINE_HOUR, minute=0, second=0, microsecond=0)
    if cut <= local:
        cut = (local + timedelta(days=1)).replace(hour=NIGHT_DEADLINE_HOUR, minute=0, second=0, microsecond=0)
    return cut


def check_night_item(mainline, task, cap_cny, stop_if, deadline=None, at=None):
    """Validate before writing (also used by --queue, so a bad item fails in the thread that wrote it)."""
    _check(mainline, MAINLINES, 'mainline')
    if not task or len(task) > 120:
        raise HSError('task must be one sentence of at most 120 characters')
    if not stop_if or not str(stop_if).strip():
        raise HSError('stop_if is required: when should the run stop besides the cap?')
    try:
        cap = float(cap_cny)
    except (TypeError, ValueError):
        raise HSError('cap must be a number of CNY, got %r' % cap_cny)
    if not math.isfinite(cap) or cap <= 0:
        raise HSError('cap must be greater than 0')
    at = at or datetime.now(timezone.utc)
    try:
        due = parse_time(deadline) if deadline else default_deadline(at)
    except ValueError:
        raise HSError('deadline must be an ISO time such as 2030-01-15T09:00, got %r' % deadline)
    if due <= at or due - at > timedelta(hours=NIGHT_MAX_HOURS):
        raise HSError('deadline must be in the future and within %d hours' % NIGHT_MAX_HOURS)
    return cap, due


def _small_max():
    return config.get().approve_over  # below this a thread may record a budget itself, on a basis and an audit record
V4_MSG = 'small-小额'  # approval_msg of such a row; approval_quote reads 「小额 · AUD-…」
AUDIT_ID_RE = re.compile(r'^AUD-\d{8}-\d+$')
PAGE_APPROVAL_PREFIX = '汇报页 '  # the owner's 「批」 tick on a report page


def check_owner_msg(conn, msg_id):
    """The owner's message id must be an owner_msgs row the UserPromptSubmit hook logged as the owner's own (is_owner=1).
    The id may carry the cc_ prefix MSG_RE allows; the row is found by the bare uuid."""
    row = conn.execute('SELECT is_owner FROM owner_msgs WHERE id=?', ((msg_id or '').strip().removeprefix('cc_'),)).fetchone()
    if not row:
        raise HSError(config.say('no owner_msgs row %s: use the id from the hook line "owner_msg id: …" of {owner}\'s own message') % msg_id)
    if not row[0]:
        raise HSError(config.say('owner_msgs row %s is not {owner}\'s message (is_owner=0: a dispatcher or program prompt)') % msg_id)


def small_route(cap_cny, approval_msg, approval_at, approval_quote, basis, audit):
    """True when budget-add takes the small-spend route: cap under ¥10, none of the owner's approval fields, and a --basis or
    --audit given. At ¥10 and over this is always False, so the usual approval checks (and their errors) apply."""
    try:
        small = float(cap_cny) < _small_max()
    except (TypeError, ValueError):
        return False
    return small and not (approval_msg or approval_at or approval_quote) and bool(basis or audit)


def check_verbal_budget(bid, cap_cny, purpose, approval_msg, approval_at, approval_quote, expires=None, at=None,
                        page=False, basis=None, audit=None, conn=None):
    """Validate a verbal budget (also used by --queue, so a bad one fails in the thread that wrote it).
    Returns (cap, approved, expires) with aware datetimes."""
    if not bid or not VERBAL_ID_RE.match(bid) or bid.upper().startswith(('NQ-', 'NIGHT-')):  # NIGHT-: the night job only
        raise HSError('budget id must look like EXP-X-01-20300115 (letters, digits, - . _; not NQ- or NIGHT-), got %r' % bid)
    try:
        cap = float(cap_cny)
    except (TypeError, ValueError):
        raise HSError('cap must be a number of CNY, got %r' % cap_cny)
    if not math.isfinite(cap) or cap <= 0:
        raise HSError('cap must be greater than 0')
    if not purpose or not str(purpose).strip() or len(purpose) > 200:
        raise HSError('purpose is required: one sentence of at most 200 characters')
    small = small_route(cap, approval_msg, approval_at, approval_quote, basis, audit)
    if small:  # small route: under ¥10 the thread records it itself; no message id / time / quote, but a basis and an audit record
        if not (basis or '').strip().startswith('small') or len(basis.strip()) > 100:
            raise HSError("--basis must start with 'small' (e.g. 'small R-21'), at most 100 characters")
        if not audit or not AUDIT_ID_RE.match(audit.strip()):
            raise HSError('--audit must be an audit id such as AUD-20300115-01 (audit-add first)')
        if conn is not None and not conn.execute('SELECT 1 FROM audits WHERE id=?', (audit.strip(),)).fetchone():
            raise HSError('no audit %s: write the audit record (audit-add) before the budget' % audit.strip())
        approval_at = (at or datetime.now(timezone.utc)).isoformat()
    elif page:  # only reply_watch on the Mac passes page=True; the cli and --queue cannot
        if not (approval_msg or '').startswith(PAGE_APPROVAL_PREFIX) or len(approval_msg) > 200:
            raise HSError('a report-page approval must read 「汇报页 <页名> 勾选『批』 <时间>」')
    elif not approval_msg or not MSG_RE.match(approval_msg):
        raise HSError(config.say("approval-msg must be the id of {owner}'s own message (the transcript message uuid), got %r") % approval_msg)
    else:
        if conn is not None:
            check_owner_msg(conn, approval_msg)
    if not small and (not approval_quote or not str(approval_quote).strip() or len(approval_quote) > 200):
        raise HSError(config.say("approval-quote is required: {owner}'s words, at most 200 characters"))
    at = at or datetime.now(timezone.utc)
    try:
        approved = parse_time(approval_at)
    except (TypeError, ValueError):
        raise HSError('approval-at must be an ISO time such as 2030-01-15T05:01:17Z, got %r' % approval_at)
    if approved > at + timedelta(minutes=5) or approved < at - timedelta(days=VERBAL_MAX_DAYS):
        raise HSError('approval-at must be in the past %d days' % VERBAL_MAX_DAYS)
    try:
        due = parse_time(expires) if expires else approved + timedelta(hours=VERBAL_DEFAULT_HOURS)
    except ValueError:
        raise HSError('expires must be an ISO time such as 2030-01-15T00:00Z, got %r' % expires)
    if due <= at or due > approved + timedelta(days=VERBAL_MAX_DAYS):
        raise HSError('expires must be in the future and at most %d days after the approval' % VERBAL_MAX_DAYS)
    return cap, approved, due


V4_SPLIT_HOURS = 24
CODE_SUFFIX_RE = re.compile(r'(?:-\d{8}|-\d{1,3})+$')


def budget_code(bid):
    """The code of a budget id: the id without its trailing -YYYYMMDD / -NN suffixes (CODE-01-20300115 -> CODE-01,
    EXP-X-01-20300115 -> EXP-X)."""
    return CODE_SUFFIX_RE.sub('', bid or '')


def check_small_split(conn, bid, cap, editor, basis, at=None):
    """Anti-splitting for the small-spend route: a budget is refused when the same editor (_editor_key) recorded small-route
    budgets in the last 24 hours that, together with this one, reach CNY 10 -- counting only rows with the same code
    (budget_code); the basis text is not compared (everyone writes a similar basis). Revoked budgets still
    count. Raises HSError naming the ids."""
    since = _utc((at or datetime.now(timezone.utc)) - timedelta(hours=V4_SPLIT_HOURS))
    key, code = _editor_key(editor), budget_code(bid)
    rows = [r for r in conn.execute("SELECT * FROM verbal_budgets WHERE approval_msg=? AND created_at>=?", (V4_MSG, since))
            if _editor_key(r['editor']) == key and budget_code(r['id']) == code]
    if sum(r['cap_cny'] for r in rows) + cap >= _small_max():
        raise HSError(config.say('合计已到 ¥%g（%s），要 {owner} 批') % (_small_max(), '、'.join([r['id'] for r in rows] + [bid])))


REPLY_ID_RE = re.compile(r'^RR-\d{8}-\d+$')
AMOUNT_RE = re.compile(r'[¥￥$]\s*(\d+(?:,\d{3})*(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*元')
NO_AMOUNT = '页上没写到这个金额'


def _amounts(text):
    out = []
    for m in AMOUNT_RE.finditer(text or ''):
        out.append(float((m.group(1) or m.group(2)).replace(',', '')))  # a $ amount counts at its face number (stricter than converting)
    return out


def reply_approval(conn, vault, rid, cap):
    """Budget route 「--approval-reply RR-…」: the owner ticked a choice on a report page whose properties had no 预算/上限, so
    reply_watch wrote no budget. Returns (approval_msg, approval_at, approval_quote), formatted like reply_watch's tick budgets.
    Refused unless the reply exists, holds a tick, was not withdrawn, has no budget yet, and the page (live or archived) names an
    amount >= cap in the ticked option's line / 选项 table row, the title or the [!owner] block."""
    rid = (rid or '').strip()
    if not REPLY_ID_RE.match(rid):
        raise HSError('approval-reply must be a report reply id such as RR-20300115-01, got %r' % rid)
    r = conn.execute('SELECT * FROM report_replies WHERE id=?', (rid,)).fetchone()
    if not r:
        raise HSError('no report reply %s' % rid)
    choice = (r['choice'] or '').strip()
    if not choice or (r['kind'] or '') == 'seen':
        raise HSError('reply %s holds no ticked option (a typed sentence is not an approval)' % rid)
    picked = [c.strip() for c in choice.split('、') if c.strip()]
    if any(c.startswith('撤回') or c == '不批' for c in picked):
        raise HSError('reply %s is a withdrawal or a 不批, not an approval' % rid)
    page = r['page']
    if not page or page.startswith('首页小卡'):
        raise HSError('reply %s is not on a report page' % rid)
    later = [x['id'] for x in conn.execute("SELECT id, choice FROM report_replies WHERE page=? AND received_at>? AND id!=?",
                                           (page, r['received_at'], rid)) if (x['choice'] or '').startswith('撤回')]
    if later:
        raise HSError('reply %s was withdrawn later (%s)' % (rid, later[0]))
    at = parse_time(r['received_at'])
    msg = '%s%s 勾选『%s』 %s' % (PAGE_APPROVAL_PREFIX, page, choice, at.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M'))
    if len(msg) > 200:
        raise HSError('the ticked option is too long to record (%d characters)' % len(msg))
    if conn.execute('SELECT 1 FROM verbal_budgets WHERE approval_reply=? OR approval_msg=?', (rid, msg)).fetchone():
        raise HSError('reply %s already has a budget; one budget per reply' % rid)
    text = _page_text(vault, page)
    if text is None:
        raise HSError('report page %s not found in 汇报/ or 汇报/归档/' % page)
    seg = [page]
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith('# ') or (ln.lstrip().startswith('|') and any(c in ln for c in picked)) or any(c in ln for c in picked if len(c) > 1):
            seg.append(ln)
        if ln.lstrip().startswith('> [!owner]'):
            for x in lines[i:]:
                if not x.lstrip().startswith('>'):
                    break
                seg.append(x)
    for c in picked:  # a one-character label such as 批 only counts on its own option line (checkbox or table cell)
        seg += [ln for ln in lines if len(c) == 1 and re.search(r'^\s*(?:[-*+] \[.\]\s*|\|\s*)%s\b' % re.escape(c), ln)]
    amounts = _amounts('\n'.join(seg))
    if not amounts or cap > max(amounts) + 1e-9:
        raise HSError('%s: cap %g, page %s has %s' % (NO_AMOUNT, cap, page, ('amounts up to %g' % max(amounts)) if amounts else 'no amount'))
    return msg, _utc(at), choice


def add_verbal_budget(conn, *, id, cap_cny, purpose, approval_msg=None, approval_at=None, approval_quote=None, editor=None,
                      expires=None, page=False, basis=None, audit=None, approval_reply=None, vault=None):
    if approval_reply:
        if approval_msg or approval_at or approval_quote or basis or audit:
            raise HSError('--approval-reply replaces --approval-msg/-at/-quote and the small-spend route; give only one way')
        try:
            capf = float(cap_cny)
        except (TypeError, ValueError):
            raise HSError('cap must be a number of CNY, got %r' % cap_cny)
        approval_msg, approval_at, approval_quote = reply_approval(conn, vault, approval_reply, capf)
        page = True
    cap, approved, due = check_verbal_budget(id, cap_cny, purpose, approval_msg, approval_at, approval_quote, expires,
                                             page=page, basis=basis, audit=audit, conn=conn)
    if not editor:
        raise HSError('editor is required')
    if small_route(cap, approval_msg, approval_at, approval_quote, basis, audit):
        approval_msg, approval_quote = V4_MSG, '小额 · ' + audit.strip()
        basis, audit = basis.strip(), audit.strip()
    else:
        basis = audit = None
    if conn.execute('SELECT 1 FROM verbal_budgets WHERE id=?', (id,)).fetchone():
        raise HSError('verbal budget %s is already recorded; budgets are never changed, add a new id' % id)
    if basis:  # the small-spend route (basis is None otherwise)
        check_small_split(conn, id, cap, editor, basis)
    conn.execute('INSERT INTO verbal_budgets(id,cap_cny,purpose,approval_msg,approval_at,approval_quote,expires_at,editor,'
                 'created_at,basis,audit_id,approval_reply) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (id, cap, purpose.strip(), approval_msg, _utc(approved), approval_quote.strip(), _utc(due), editor, now(), basis, audit,
                  (approval_reply or '').strip() or None))
    conn.commit()
    return id


def revoke_verbal_budget(conn, bid, editor, reason):
    """Withdrawing only narrows spending, so it needs no approval message; it needs a reason."""
    if not reason or not str(reason).strip():
        raise HSError('reason is required')
    cur = conn.execute("UPDATE verbal_budgets SET status='撤回', revoked_at=?, revoked_by=?, revoke_reason=? "
                       "WHERE id=? AND status='有效'", (now(), editor, reason.strip(), bid))
    conn.commit()
    if not cur.rowcount:
        raise HSError('no active verbal budget %s' % bid)
    return bid


def add_night_item(conn, *, mainline, task, cap_cny, stop_if, editor, deadline=None, exp_id=None):
    cap, due = check_night_item(mainline, task, cap_cny, stop_if, deadline)
    nid = _next_id(conn, 'night_items', 'NQ-%s-' % datetime.now(LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute('INSERT INTO night_items(id,mainline,task,cap_cny,stop_if,deadline,exp_id,editor,created_at) '
                 'VALUES (?,?,?,?,?,?,?,?,?)', (nid, mainline, task, cap, stop_if, _utc(due), exp_id, editor, now()))
    conn.commit()
    return nid


def close_night_item(conn, nid, status):
    """the owner's tick: 已批 only for a waiting item before its deadline; 撤回 for a waiting or an approved item."""
    _check(status, ('已批', '撤回'), 'status')
    if status == '已批':
        cur = conn.execute("UPDATE night_items SET status='已批', decided_at=? WHERE id=? AND status='待批' AND deadline>?",
                           (now(), nid, now()))
    else:
        cur = conn.execute("UPDATE night_items SET status='撤回', decided_at=? WHERE id=? AND status IN ('待批','已批')",
                           (now(), nid))
    conn.commit()
    return cur.rowcount


def expire_night_items(conn):
    cur = conn.execute("UPDATE night_items SET status='过期' WHERE status='待批' AND deadline<=?", (now(),))
    conn.commit()
    return cur.rowcount


# ---- end-of-task records (read by the 07:00 daily agent) ----

NOTE_FIELDS = ('mainline', 'title', 'did', 'result', 'meaning', 'editor', 'date', 'detail', 'data', 'limits',
               'exp_ids', 'cost', 'duration', 'links', 'step', 'sync')
DETAIL_LIMIT = 4000  # the process and the judgments behind the result; the 07:00 agent reads it, readers never do


# V5-NOTE-LINK-AUDIT: a note queued in the same round as its audit cannot know the audit's id; it writes @<tag> in --links.
TAG_RE = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
LINK_TAG_RE = re.compile(r'(?<![A-Za-z0-9_@-])@([A-Za-z0-9_-]{1,32})(?![A-Za-z0-9_-])')
AUDIT_ID_RE = re.compile(r'AUD-\d{8}-\d+')
TAGS_KEY, TAGS_KEEP_DAYS = 'audit-tags', 2


def check_tag(tag):
    if tag is not None and not TAG_RE.match(tag):
        raise HSError('--tag must be 1-32 characters of A-Z a-z 0-9 _ -, got %r' % tag)


def _tag_key(editor, tag):
    return '%s|%s' % (' '.join(str(editor or '').split()), tag)  # tags are per thread: two threads may both say @a1


def remember_audit_tag(conn, editor, tag, aid):
    cut = (datetime.now(timezone.utc) - timedelta(days=TAGS_KEEP_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    tags = {k: v for k, v in (meta_get(conn, TAGS_KEY) or {}).items() if v[1] >= cut}
    tags[_tag_key(editor, tag)] = [aid, now()]
    meta_set(conn, TAGS_KEY, tags)


def resolve_audit_tags(conn, editor, links):
    """(links with every @tag replaced by its AUD id, [tags not known yet])."""
    if not links:
        return links, []
    tags = meta_get(conn, TAGS_KEY) or {}
    unknown = []

    def sub(m):
        hit = tags.get(_tag_key(editor, m.group(1)))
        if not hit:
            unknown.append(m.group(1))
            return m.group(0)
        return hit[0]
    return LINK_TAG_RE.sub(sub, links or ''), unknown


def _tokens(text):
    return {t for t in re.split(r'[\s,;，；、]+', text or '') if t}


def check_note_audit_links(links, exp_ids, experiment_of):
    """Refuse a note whose literal AUD-… link names an audit of an experiment that is not among the note's exp-ids.
    `experiment_of(aid)` -> the audit's experiment, or None when that audit is not visible (then nothing is checked)."""
    want = _tokens(exp_ids)
    if not want:
        return
    for aid in dict.fromkeys(AUDIT_ID_RE.findall(links or '')):
        exp = experiment_of(aid)
        if exp and not (_tokens(exp) & want):
            raise HSError('这条审计的实验是 %s，跟 exp-ids 对不上；同一轮排的审计请写 --links @tag' % exp)


def ledger_audit_experiment(conn, aid):
    r = conn.execute('SELECT experiment FROM audits WHERE id=?', (aid,)).fetchone()
    return r['experiment'] if r else None


SYNC_VALUES = ('是', '否')
PUSH_EDITOR_RE = re.compile(r'(?:^|[｜|])\s*推进')  # 「甲组｜推进 方法X 评测」, 「乙组｜推进 接班」


def check_sync(value):
    if value is not None and value not in SYNC_VALUES:
        raise HSError('--sync 只能是「是」或「否」，收到 %r' % value)
    return value


def note_sync_wanted(sync, exp_ids, editor):
    """--sync 是/否 decides; without it, a note with exp-ids from a 推进 thread opens a 同步 page."""
    if check_sync(sync) is not None:
        return sync == '是'
    return bool((exp_ids or '').strip()) and bool(PUSH_EDITOR_RE.search(' '.join(str(editor or '').split())))


def add_task_note(conn, *, mainline, title, did, result, meaning, editor, date=None, detail=None, data=None,
                  limits=None, exp_ids=None, cost=None, duration=None, links=None, step=None, sync=None):
    _check(mainline, MAINLINES, 'mainline')
    sync = '1' if note_sync_wanted(sync, exp_ids, editor) else 'no'
    step = check_step(step)
    date = _date(date or datetime.now(LOCAL_TZ).strftime('%Y-%m-%d'))
    for name, value in (('title', title), ('did', did), ('result', result), ('meaning', meaning)):
        if not value or not str(value).strip():
            raise HSError(name + ' is required')
    check_note_sizes(title, detail)
    nid = _next_id(conn, 'task_notes', 'TN-%s-' % date.replace('-', ''))
    conn.execute('INSERT INTO task_notes(id,date,mainline,title,did,result,meaning,detail,data,limits,exp_ids,cost,duration,'
                 'links,editor,created_at,step,sync) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (nid, date, mainline, title, did, result, meaning, detail, data, limits, exp_ids, cost, duration, links,
                  editor, now(), step, sync))
    conn.commit()
    return nid


NOTE_EDITABLE = ('links', 'exp_ids')
NOTE_ID_RE = re.compile(r'^TN-\d{8}-\d+$')


def check_note_edit(fields, reason):
    """note-edit: only links / exp_ids, one-line values (empty clears). Returns {field: value|None}."""
    if not fields:
        raise HSError('note-edit needs at least one --field k=v (one of: %s)' % ', '.join(NOTE_EDITABLE))
    bad = [k for k in fields if k not in NOTE_EDITABLE]
    if bad:
        raise HSError('note-edit cannot change %s; editable: %s' % (', '.join(bad), ', '.join(NOTE_EDITABLE)))
    if not ' '.join(str(reason or '').split()):
        raise HSError('--reason is required')
    return {k: (' '.join(str(v).split()) or None) for k, v in fields.items()}


def edit_task_note(conn, nid, fields, reason, editor):
    """Change links / exp_ids of one task note. The UPDATE only matches the old value read a moment ago, so a concurrent change
    fails; every changed field gets a 变更 row (a value that stays the same is skipped)."""
    vals = check_note_edit(fields, reason)
    if not (editor or '').strip():
        raise HSError('--editor is required')
    row = conn.execute('SELECT * FROM task_notes WHERE id=?', (nid,)).fetchone()
    if not row:
        raise HSError('no task note %s' % nid)
    why, done = ' '.join(reason.split()), []
    for k, new in vals.items():
        old = row[k]
        if new == old:
            continue
        if conn.execute('UPDATE task_notes SET %s=? WHERE id=? AND %s IS ?' % (k, k), (new, nid, old)).rowcount != 1:
            conn.rollback()
            raise HSError('%s changed while editing %s; read it again and retry' % (k, nid))
        add_change(conn, editor, '任务记录/' + nid, '%s：%s → %s（原因：%s）' % (k, old or '', new or '', why))
        done.append(k)
    conn.commit()
    return {'id': nid, 'changed': done}


def add_idea(conn, table, *, mainline, content, editor, source_exp=None, status='新', ra_checked=0, deploy_data=None):
    """B2: one row of `ideas` or `hypotheses` (same columns); id IDEA-/HYP-YYYYMMDD-NN (local day)."""
    _check(mainline, MAINLINES, 'mainline')
    _check(status, IDEA_STATUS, 'status')
    if not (content or '').strip():
        raise HSError('content is required')
    if not (editor or '').strip():
        raise HSError('editor is required')
    ra_checked = 1 if str(ra_checked).strip().lower() in ('1', 'true', 'yes') else 0
    pre = {'ideas': 'IDEA', 'hypotheses': 'HYP'}[table]
    nid = _next_id(conn, table, '%s-%s-' % (pre, datetime.now(LOCAL_TZ).strftime('%Y%m%d')))
    conn.execute('INSERT INTO %s(id,mainline,content,source_exp,status,ra_checked,deploy_data,editor,created_at) '
                 'VALUES (?,?,?,?,?,?,?,?,?)' % table,
                 (nid, mainline, content.strip(), source_exp, status, ra_checked, deploy_data, editor, now()))
    conn.commit()
    return nid


def check_step(step):
    """A frozen-structure step number: 「步3」 or B3, stored as 「步3」. S (acceptance) and P (下一步候选) numbers are refused."""
    if step is None:
        return None
    t = step.strip()
    if re.match(r'[SsPp]', t):
        raise HSError('--step takes 「步N」 (e.g. 步3, or B3), not %r: S numbers are acceptance criteria, P numbers are 下一步候选' % step)
    m = re.fullmatch(r'(?:步|B)(\d+)', t)
    if not m:
        raise HSError('--step must look like 步3 (or B3), the node number of the frozen structure, got %r' % step)
    return '步' + m.group(1)


def check_note_sizes(title, detail=None):
    if len(title) > 80:
        raise HSError('title must be at most 80 characters')
    if detail and len(detail) > DETAIL_LIMIT:
        raise HSError('detail must be at most %d characters (put the rest in a library file and link it)' % DETAIL_LIMIT)


# ---- plain names for 实验索引 (written by the 07:00 agent; code 'series:XY' names a whole series) ----

def set_exp_note(conn, code, editor, plain_name=None, data_layer=None):
    if not code or not (plain_name or data_layer):
        raise HSError('exp-note needs a code and a plain name or a data layer')
    if plain_name and len(plain_name) > 40:
        raise HSError('plain name must be at most 40 characters')
    conn.execute('INSERT OR IGNORE INTO exp_notes(code,editor,updated_at) VALUES (?,?,?)', (code, editor, now()))
    for k, v in (('plain_name', plain_name), ('data_layer', data_layer)):
        if v:
            conn.execute('UPDATE exp_notes SET %s=?, editor=?, updated_at=? WHERE code=?' % k, (v, editor, now(), code))
    conn.commit()


def add_wait(conn, thread, ask, editor, asked_at=None, report=None, typed=None):
    if not (thread or '').strip() or not (ask or '').strip():
        raise HSError('thread and ask are required')
    check_wait_thread(thread)
    if len(ask) > 120:
        raise HSError('ask must be one sentence of at most 120 characters')
    at = asked_at or now()
    wid = _next_id(conn, 'waits', 'wait-' + at[:10].replace('-', '') + '-')
    report = check_report_name(report)
    typed = check_typed(typed)
    conn.execute('INSERT INTO waits(id,thread,ask,editor,asked_at,report,typed) VALUES (?,?,?,?,?,?,?)',
                 (wid, thread, ask, editor, at, report, typed))
    conn.commit()
    return wid


def check_typed(typed):
    """--typed: the words the owner must type in chat (a checkbox does not count for these), e.g. 「批 CODE-01 改账」."""
    if typed is None:
        return None
    t = ' '.join(typed.split())
    if not t or len(t) > 60 or any(c in t for c in '「」“”|[]'):
        raise HSError(config.say('--typed: the words {owner} types, at most 60 characters, without 「」 | [ ]'))
    return t


def _editor_key(editor):
    """Thread titles are written with both the full-width ｜ and the ASCII |."""
    return (editor or '').replace('|', '｜').strip()


WAIT_ID_RE = re.compile(r'^wait-\d{8}-\d+$')


def _page_text(vault, report):
    """The report page's text, from 汇报/ or (archived) 汇报/归档/*/, or None."""
    if not vault or not report:
        return None
    base = Path(vault) / REPORT_DIR
    for path in [Path(vault) / d / (report + '.md') for d in report_dirs()] + sorted((base / '归档').rglob('%s.md' % report)):
        try:
            return path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
    return None


def wait_answered(conn, w, vault=None):
    """The owner has answered this wait: a report_replies row (same wait, same thread or same report page,
    received after asked_at), or its report page is 已回/已结 and the page no longer asks them to type something."""
    for r in conn.execute('SELECT * FROM report_replies WHERE received_at>?', (w['asked_at'],)):
        if r['kind'] == 'wake':  # the 「叫醒」 button on a push is a nudge, not an answer: the wait stays open
            continue
        if r['wait_id'] == w['id'] or (r['thread'] and r['thread'] == w['thread'] and not r['page'] and not r['wait_id']) or (w['report'] and r['page'] == w['report']):
            return True
    text = _page_text(vault, w['report'])
    if text is None:
        return False
    from . import reply_watch, unregistered
    return report_done(reply_watch.front(text).get('状态')) and unregistered.waiting_reason(text) != 'b'


def wait_answered_before(conn, w, vault=None):
    """The reply the owner gave on this wait's report page BEFORE the wait was registered (note-add / budget-add must not close
    such a wait: the thread never saw that answer). Returns the report_replies row, or None. Only when the page has such
    a still-undelivered reply and no reply at or after asked_at (that one would be wait_answered's)."""
    if not w['report']:
        return None
    asked = parse_time(w['asked_at'])
    rows = [r for r in conn.execute('SELECT * FROM report_replies WHERE page=? ORDER BY received_at, id', (w['report'],))
            if r['kind'] != 'wake']
    if any(parse_time(r['received_at']) > asked for r in rows):
        return None  # answered after the wait was registered: wait_answered's case
    prior = [r for r in rows if r['kind'] not in ('seen', 'question') and not r['wait_id'] and not r['delivered_at']]
    return prior[-1] if prior else None


def check_close_ids(closes):
    """The id format of --closes, checkable without a database (queue time)."""
    ids = [i.strip() for i in (closes or '').split(',') if i.strip()]
    for i in ids:
        if not WAIT_ID_RE.match(i):
            raise HSError('--closes: %r is not a wait id (wait-YYYYMMDD-N)' % i)
    return ids


def check_closes(conn, editor, closes):
    """--closes wait-…[,wait-…]: ids this editor opened. Unknown ids, other threads' waits and typed waits are refused."""
    ids = check_close_ids(closes)
    for i in ids:
        row = conn.execute('SELECT * FROM waits WHERE id=?', (i,)).fetchone()
        if not row:
            raise HSError('--closes: no wait %s' % i)
        if _editor_key(row['editor']) != _editor_key(editor):
            raise HSError('--closes: %s belongs to %s, not %s' % (i, row['editor'], editor))
        if row['typed']:
            raise HSError('--closes: %s waits for typed words (「%s」); only wait-done closes it' % (i, row['typed']))
    return ids


def close_waits_of_editor(conn, editor, before, by, vault=None, closes=None, answered=True):
    """A thread finished (note-add) or the owner approved its money (budget-add): close that thread's open waits asked at or
    before `before` that the owner has already answered (wait_answered), plus the ones named in `closes`. Typed waits, other
    threads', later and unanswered waits are left open. answered=False (note-add): close only the
    waits named in `closes`. Returns the closed ids."""
    key, cutoff = _editor_key(editor), parse_time(before)
    if not key:
        return []
    named = check_closes(conn, editor, closes)
    rows = conn.execute("SELECT * FROM waits WHERE status='等待'").fetchall()
    ids = [r['id'] for r in rows if _editor_key(r['editor']) == key and not r['typed']
           and ((answered and parse_time(r['asked_at']) <= cutoff and wait_answered(conn, r, vault)
                 and wait_answered_before(conn, r, vault) is None) or r['id'] in named)]
    stamp = now()
    conn.executemany("UPDATE waits SET status='已回', closed_at=?, closed_by=? WHERE id=? AND status='等待'",
                     [(stamp, by, i) for i in ids])
    conn.commit()
    return ids


def still_open_waits(conn, editor):
    """The editor's waits still open after a close: [{'id', 'ask'}], for the command output. A wait registered after
    the owner had already answered on its page also carries 'answered_before' {reply, choice, text, at}: the thread acts on
    that answer and then closes the wait itself (wait-done)."""
    key = _editor_key(editor)
    out = []
    for r in conn.execute("SELECT * FROM waits WHERE status='等待' ORDER BY id").fetchall():
        if _editor_key(r['editor']) != key:
            continue
        item = {'id': r['id'], 'ask': r['ask']}
        prior = wait_answered_before(conn, r)
        if prior is not None:
            item['answered_before'] = {'reply': prior['id'], 'choice': prior['choice'], 'text': prior['text'], 'at': prior['received_at']}
        out.append(item)
    return out


def check_wait_thread_match(conn, wid, thread):
    """CLI wait-done: a wait that was registered with a thread is closed only with that thread.
    A wait without a thread (old ones) is closed as before. Internal closes (page ticks, note-add, autowait) skip this."""
    row = conn.execute('SELECT thread, status FROM waits WHERE id=?', (wid,)).fetchone()
    reg = ((row['thread'] if row else '') or '').strip()
    if reg and row['status'] == '等待' and (thread or '').strip() != reg:
        raise HSError('wait %s 登记在线程 %s，用 --thread %s 关，或先 wait-move' % (wid, reg, reg))


def close_wait(conn, wid, status='已回', by='wait-done'):
    """Close one wait by id, never a whole thread."""
    _check(status, WAIT_STATUS, 'status')
    if not wid:
        raise HSError('wait-done 只按编号关：给等待编号（--thread 单独用只列出这条线程的等待）')
    cur = conn.execute("UPDATE waits SET status=?, closed_at=?, closed_by=? WHERE status='等待' AND id=?",
                       (status, now(), by, wid))
    conn.commit()
    return cur.rowcount


REPORT_DIR = '汇报'
REPLY_BY = '汇报页'
# Pages live in 汇报/要批/, 汇报/同步/, the retired 汇报/待决/ and (not yet migrated) the 汇报/ root.
KIND_DIRS = ('要批', '同步')
RETIRED_DIR = '待决'


def report_dirs(vault=None):
    """Vault-relative folders that hold live report pages, in the order they are read. With a vault: only those that exist."""
    dirs = [REPORT_DIR + '/' + d for d in (RETIRED_DIR,) + KIND_DIRS] + [REPORT_DIR]
    return [d for d in dirs if (Path(vault) / d).is_dir()] if vault else dirs


def report_pages(vault):
    """Every live report page (not 归档/, not 附件/): [Path], folder by folder, sorted by name inside each."""
    out = []
    for d in report_dirs(vault):
        out += sorted((Path(vault) / d).glob('*.md'))
    return out


def report_path(vault, name):
    """The live page called `name` (no .md), or None."""
    for d in report_dirs(vault):
        p = Path(vault) / d / (name + '.md')
        if p.is_file():
            return p
    return None


def report_rel(vault, name):
    """Link target (vault-relative, no .md) of a report page, live or (newest month first) in 汇报/归档/; 汇报/<name> when it is not found."""
    p = report_path(vault, name) if vault else None
    if vault and not p:
        p = next((q for q in sorted((Path(vault) / REPORT_DIR / '归档').rglob('*.md'), reverse=True) if q.name == name + '.md'), None)
    return p.relative_to(vault).with_suffix('').as_posix() if p else '%s/%s' % (REPORT_DIR, name)


def all_report_stems(vault, exclude=None):
    """Stems of every page under 汇报/ (live folders and every 归档 month), except the file `exclude`."""
    base = Path(vault) / REPORT_DIR
    out = {}
    if base.is_dir():
        for p in base.rglob('*.md'):
            if '附件' in p.relative_to(base).parts or p == exclude:
                continue
            out.setdefault(p.stem, []).append(p)
    return out


def unique_stem(vault, stem, exclude=None):
    """`stem`, or 「stem · 2」, 「stem · 3」 … when another page under 汇报/ (live or archived, any month) already has the name."""
    taken = all_report_stems(vault, exclude)
    if stem not in taken:
        return stem
    n = 2
    while '%s · %d' % (stem, n) in taken:
        n += 1
    return '%s · %d' % (stem, n)


def rename_report_page(conn, old, new):
    """A page got a new name: the ledger rows that name it follow."""
    if conn is None or old == new:
        return
    conn.execute('UPDATE waits SET report=? WHERE report=?', (new, old))
    conn.execute('UPDATE report_replies SET page=? WHERE page=?', (new, old))
    conn.commit()


def add_seen(conn, page, milestone=None):
    """the owner ticked 「已看」 on a 同步 page: one report_replies row (kind 'seen', no thread, already delivered: nothing is woken)
    and, when the page carries 里程碑: MS-…, milestones.seen_at. Once per page. Returns the row id, or None if already recorded."""
    at = now()
    if milestone:
        conn.execute('UPDATE milestones SET seen_at=? WHERE id=? AND seen_at IS NULL', (at, milestone))
    if conn.execute("SELECT 1 FROM report_replies WHERE page=? AND kind='seen'", (page,)).fetchone():
        conn.commit()
        return None
    rid = _next_id(conn, 'report_replies', 'RR-%s-' % datetime.now(LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute("INSERT INTO report_replies(id,page,wait_id,choice,text,page_mtime,received_at,thread,delivered_at,kind) "
                 "VALUES (?,?,NULL,'已看','已看',NULL,?,NULL,?,'seen')", (rid, page, at, at))
    conn.commit()
    return rid
# 3 page types, 3 statuses. Old values stay in the vault (no batch rewrite): code maps them.
REPORT_TYPES = ('要你定', '要你做', '叫停报告')
OLD_REPORT_TYPES = {'结果和要你定': '要你定', '待决': '要你定'}
NO_PAGE_TYPES = ('结果', '只看', '已按推荐做')  # audit records, no page
REPORT_STATUS = ('待你看', '已回', '已结')


def report_type(t):
    t = (t or '').strip()
    return OLD_REPORT_TYPES.get(t, t)


def report_done(status):
    """已回 / 已结, and the old 已看 and 已取代… which now count as 已结."""
    s = (status or '').strip()
    return s in ('已回', '已结', '已看') or s.startswith('已取代')


def check_report_name(report):
    """--report: a page name in 汇报/ (no folder, no .md), e.g. 「2030-01-15 方法X怎么跑」."""
    if report is None:
        return None
    name = report.strip()
    if name.startswith(REPORT_DIR + '/'):
        name = name[len(REPORT_DIR) + 1:]
    if name.endswith('.md'):
        name = name[:-3]
    if not name or '/' in name or '|' in name or '[' in name or ']' in name:
        raise HSError('--report must be a page name in 汇报/, got %r' % report)
    return name


def find_reply(conn, page, wait_id):
    return conn.execute('SELECT * FROM report_replies WHERE page=? AND COALESCE(wait_id, "")=? '
                        'AND COALESCE(kind, "") NOT IN (\'seen\', \'question\', \'copy\')', (page, wait_id or '')).fetchone()


DISPATCHER_KIND = '调度'
DISPATCHER_WORD = '调度者'


def dispatcher_thread(conn):
    """Id of the newest open thread of kind 调度 in the threads table, or None."""
    r = conn.execute("SELECT id FROM threads WHERE kind=? AND status='open' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                     (DISPATCHER_KIND,)).fetchone()
    return r['id'] if r else None


def copy_to_dispatcher(conn, rid, dispatcher=None):
    """An answer whose words name 调度者 also goes to the dispatcher, as its own row of kind 'copy' (the asking
    thread still gets the original). Returns the copy's id or None."""
    r = conn.execute('SELECT * FROM report_replies WHERE id=?', (rid,)).fetchone()
    d = dispatcher or dispatcher_thread(conn)
    if not r or DISPATCHER_WORD not in (r['text'] or '') or not d or r['thread'] == d:
        return None
    cid = _next_id(conn, 'report_replies', 'RR-%s-' % datetime.now(LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute('INSERT INTO report_replies(id,page,wait_id,choice,text,page_mtime,received_at,thread,kind) VALUES (?,?,?,?,?,?,?,?,?)',
                 (cid, r['page'], r['wait_id'], r['choice'], r['text'], r['page_mtime'], now(), d, 'copy'))
    conn.commit()
    return cid


def add_question_reply(conn, *, page, wait_id, choice, page_mtime):
    """One question of a multi-question report page answered: a row of kind 'question' (no thread, the wait stays open).
    The same page + wait + choice is recorded once (returns None then); a changed tick is a new row."""
    if conn.execute("SELECT 1 FROM report_replies WHERE page=? AND COALESCE(wait_id, '')=? AND kind='question' AND choice=?",
                    (page, wait_id or '', choice)).fetchone():
        return None
    rid = _next_id(conn, 'report_replies', 'RR-%s-' % datetime.now(LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute("INSERT INTO report_replies(id,page,wait_id,choice,text,page_mtime,received_at,thread,kind) "
                 "VALUES (?,?,?,?,?,?,?,?,'question')", (rid, page, wait_id, choice, None, page_mtime, now(), None))
    conn.commit()
    return rid


def add_report_reply(conn, *, page, wait_id, choice, text, page_mtime, thread=None):
    """the owner answered on a report page: one report_replies row, and the wait (if any) closes with their answer.
    The same page + wait is recorded once; a second call returns the first row's id."""
    old = find_reply(conn, page, wait_id)
    if old:
        return {'id': old['id'], 'new': False, 'closed': 0}
    at = now()
    rid = _next_id(conn, 'report_replies', 'RR-%s-' % datetime.now(LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute('INSERT INTO report_replies(id,page,wait_id,choice,text,page_mtime,received_at,thread) VALUES (?,?,?,?,?,?,?,?)',
                 (rid, page, wait_id, choice, text, page_mtime, at, thread))
    answer = ' · '.join(x for x in (('选 ' + choice) if choice else '', text or '') if x)
    closed = 0
    if wait_id:
        closed = conn.execute("UPDATE waits SET status='已回', closed_at=?, closed_by=?, answer=? WHERE id=? AND status='等待'",
                              (at, REPLY_BY, answer, wait_id)).rowcount
    conn.commit()
    copy = copy_to_dispatcher(conn, rid)
    return dict({'id': rid, 'new': True, 'closed': closed, 'answer': answer}, **({'copy': copy} if copy else {}))


HOME_CARD = '首页小卡'


def add_home_reply(conn, did, thread, choice, text=None, page=None):
    """An answer (or withdrawal, or 改) on a 首页 card for a decision that names its thread: one report_replies row
    (page 「首页小卡 DEC-NN」) so reply-wait hands it to that thread like a report-page answer."""
    rid = _next_id(conn, 'report_replies', 'RR-%s-' % datetime.now(LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute('INSERT INTO report_replies(id,page,wait_id,choice,text,page_mtime,received_at,thread) VALUES (?,?,?,?,?,?,?,?)',
                 (rid, page or '%s %s' % (HOME_CARD, did), None, choice, text, None, now(), thread))
    conn.commit()
    copy_to_dispatcher(conn, rid)
    return rid


def undelivered(conn, thread):
    return conn.execute('SELECT * FROM report_replies WHERE thread=? AND delivered_at IS NULL ORDER BY received_at, id',
                        (thread,)).fetchall()


def mark_delivered(conn, ids):
    at = now()
    for rid in ids:
        conn.execute('UPDATE report_replies SET delivered_at=? WHERE id=? AND delivered_at IS NULL', (at, rid))
    conn.commit()
    return at


def thread_name(conn, thread):
    """The asking thread's title as it gave it (wait-add --editor, or dec-act --editor), else its id."""
    r = conn.execute('SELECT editor FROM waits WHERE thread=? ORDER BY asked_at DESC LIMIT 1', (thread,)).fetchone()
    if r and r[0]:
        return r[0]
    r = conn.execute('SELECT acted_by FROM decisions WHERE thread=? AND acted_by IS NOT NULL ORDER BY decided_at DESC LIMIT 1',
                     (thread,)).fetchone()
    return r[0] if r and r[0] else thread[:14] + '…'


def unclaimed(conn, minutes=10):
    """[(thread, name, count)] answers for a thread that no reply-wait has picked up within `minutes`."""
    cut = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = conn.execute('SELECT thread, COUNT(*) FROM report_replies WHERE thread IS NOT NULL AND delivered_at IS NULL '
                        'AND received_at<=? AND page NOT LIKE ? AND COALESCE(kind,\'\')!=\'audit\' GROUP BY thread ORDER BY MIN(received_at)',
                        (cut, '每日审计 %')).fetchall()  # an audit 改 tick is for the thread, not a warning for the owner
    return [(t, thread_name(conn, t), n) for t, n in rows]


def unclaimed_pages(conn, minutes=10):
    """[(thread name, page)] undelivered answers older than `minutes` (same filter as unclaimed), one row per page."""
    cut = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = conn.execute('SELECT DISTINCT thread, page FROM report_replies WHERE thread IS NOT NULL AND delivered_at IS NULL '
                        'AND received_at<=? AND page NOT LIKE ? AND COALESCE(kind,\'\')!=\'audit\' ORDER BY thread, page', (cut, '每日审计 %')).fetchall()
    return [(thread_name(conn, t), p or '—') for t, p in rows]


def threadless_open(conn, page):
    """The owner's answer recorded with no thread wakes nobody, so its page stays out of 归档 until a
    thread takes it (reply-ack --id)."""
    return conn is not None and conn.execute(
        "SELECT 1 FROM report_replies WHERE page=? AND COALESCE(thread,'')='' AND delivered_at IS NULL "
        "AND COALESCE(kind,'') NOT IN ('seen','question','audit')", (page,)).fetchone() is not None


def check_wait_thread(thread):
    """A wait points at a thread id, never at a thread title."""
    if not is_thread_id((thread or '').strip()):
        raise HSError('--thread must be the thread id, %s, not its title; got %r' % (THREAD_HINT, thread))
    return thread.strip()


STOP_MAX = 120


def add_stop(conn, target, reason, editor, words=None):
    """One row of the stop table. `target` is a budget id, an experiment code,
    or a prefix ending in * (EXP-*). `words` = the owner's own words. paid_run checks the table before a run."""
    target = ' '.join((target or '').split())
    if not target or len(target) > 60 or ' ' in target:
        raise HSError('--target: one budget id, experiment code or prefix ending in * (no spaces)')
    if not (reason or '').strip() or len(reason) > STOP_MAX:
        raise HSError('--reason: one sentence of at most %d characters' % STOP_MAX)
    rid = _next_id(conn, 'stops', 'STOP-%s-' % datetime.now(LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute('INSERT INTO stops(id,target,reason,words,editor,created_at) VALUES (?,?,?,?,?,?)',
                 (rid, target, reason.strip(), (words or '').strip() or None, editor, now()))
    conn.commit()
    return rid


def lift_stop(conn, sid, editor):
    n = conn.execute('UPDATE stops SET lifted_at=?, lifted_by=? WHERE id=? AND lifted_at IS NULL', (now(), editor, sid)).rowcount
    conn.commit()
    if not n:
        raise HSError('no active stop %r (stop-list shows them)' % sid)
    return {'id': sid, 'lifted': True}


def stop_matches(target, keys):
    t = target.lower()
    for k in keys:
        k = (k or '').lower()
        if k and (k == t or (t.endswith('*') and k.startswith(t[:-1]))):
            return True
    return False


def active_stops(conn, keys=None):
    rows = conn.execute('SELECT * FROM stops WHERE lifted_at IS NULL ORDER BY id').fetchall()
    return [r for r in rows if keys is None or stop_matches(r['target'], keys)]


def ack_replies(conn, thread, rid=None):
    """A cloud thread read the owner's answer with reply-status: mark it 已送到 so 首页 stops warning."""
    check_wait_thread(thread)
    if rid:  # a reply filed with no thread: whichever thread acks it by id takes it
        conn.execute("UPDATE report_replies SET thread=? WHERE id=? AND COALESCE(thread,'')='' AND delivered_at IS NULL",
                     (thread, rid))
    sql, p = 'UPDATE report_replies SET delivered_at=? WHERE thread=? AND delivered_at IS NULL', [now(), thread]
    if rid:
        sql += ' AND id=?'; p.append(rid)
    n = conn.execute(sql, p).rowcount
    conn.commit()
    out = {'thread': thread, 'id': rid, 'acked': n}
    if rid and not n:  # queued but matched nothing: say why instead of silently landing in done/
        row = conn.execute('SELECT thread, delivered_at FROM report_replies WHERE id=?', (rid,)).fetchone()
        if not row:
            hint = ''
            m = re.match(r'^RR-(\d{1,3})$', rid.strip())
            if m:
                hint = '; ids are RR-YYYYMMDD-NN, e.g. RR-%s-%02d' % (datetime.now(LOCAL_TZ).strftime('%Y%m%d'), int(m.group(1)))
            raise HSError('no answer with id %r%s (see reply-status for the ids)' % (rid, hint))
        if row['thread'] and row['thread'] != thread:
            raise HSError('answer %s belongs to thread %s, not %s' % (rid, row['thread'], thread))
        out['already'] = row['delivered_at']  # acked before: nothing to do, not an error
    return out


def move_waits(conn, thread, wid=None, from_thread=None, report=None):
    """Handover: point open waits (one by id, or all of from_thread) at the successor thread; status
    stays. Undelivered answers and decisions of those waits/that thread follow, so the new reply-wait gets them."""
    thread = check_wait_thread(thread)
    if bool(wid) == bool(from_thread):
        raise HSError('give --id or --from-thread (one of them)')
    if report and not wid:
        raise HSError('--report only with --id')
    report = check_report_name(report)
    rows = (conn.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchall() if wid else
            conn.execute('SELECT * FROM waits WHERE thread=?', (from_thread,)).fetchall())
    if wid and not rows:
        raise HSError('no wait %s' % wid)
    ids = [r['id'] for r in rows]
    for r in rows:
        conn.execute('UPDATE waits SET thread=?, report=COALESCE(?,report) WHERE id=?', (thread, report, r['id']))
        conn.execute('UPDATE report_replies SET thread=? WHERE wait_id=? AND delivered_at IS NULL', (thread, r['id']))
    decs = 0
    if from_thread:
        conn.execute('UPDATE report_replies SET thread=? WHERE thread=? AND delivered_at IS NULL', (thread, from_thread))
        decs = conn.execute('UPDATE decisions SET thread=? WHERE thread=?', (thread, from_thread)).rowcount
    conn.commit()
    return {'moved': ids, 'thread': thread, 'decisions': decs, 'from': from_thread or (rows[0]['thread'] if rows else None)}
