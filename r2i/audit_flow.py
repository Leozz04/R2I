"""E1-E3: the audit loop around 「已运行待复核」 experiment rows.

E1 `cli audit-wait --mainline M`: like reply-wait, blocks until this mainline gets a new 「已运行待复核」 row in the
   experiments table or a note-add with --exp-ids, prints one line per event and returns. The cursor (meta
   'audit-wait:<M>') remembers what was already handed over, so a restarted wait does not repeat or miss anything.
E2 after audit-add: the thread that owns the experiment (the editor of the newest note-add naming it, then that editor's
   cmsg in waits) gets a report_replies row of kind 'audit' that its reply-wait hands over: 「AUD-… 结论 …」. Mode audit-wake.
E3 a 「已运行待复核」 row older than 24 hours with no audit record: a line in 首页's folded 「给线程看」. Mode review-overdue."""
import json
from datetime import datetime, timedelta, timezone

from . import db, modes

REVIEW_STAGE = db.REVIEW_STAGE
OVERDUE_HOURS = 24
EDITOR = 'r2i-sync 脚本'


def _experiments(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='experiments'").fetchone():
        return []
    return [dict(r) for r in conn.execute('SELECT * FROM experiments WHERE stage=? ORDER BY local_id', (REVIEW_STAGE,))]


def _code(row):
    from .exp_index import split_title
    return split_title(row.get('title') or '')[0]


def pending(conn, mainline=None):
    """「已运行待复核」 rows, of one mainline or all."""
    from .exp_index import mainline_of
    return [r for r in _experiments(conn) if mainline is None or mainline_of(r, _code(r)) == mainline]


def _notes_since(conn, mainline, since):
    return [dict(r) for r in conn.execute("SELECT * FROM task_notes WHERE mainline=? AND COALESCE(exp_ids,'')!='' AND created_at>? "
                                          'ORDER BY created_at, id', (mainline, since or ''))]


def events(conn, mainline, cursor):
    """([lines], new cursor). cursor None = first run: nothing fires, the current state becomes the cursor."""
    rows = pending(conn, mainline)
    ids = [r['local_id'] for r in rows]
    last = conn.execute("SELECT MAX(created_at) FROM task_notes WHERE mainline=? AND COALESCE(exp_ids,'')!=''", (mainline,)).fetchone()[0]
    if cursor is None:
        return [], {'rows': ids, 'note_at': last or ''}
    seen = set(cursor.get('rows') or [])
    out = ['待复核 %s · %s · 复核人 %s · 截止 %s' % (r['local_id'], r.get('title') or '—', r.get('reviewer') or '—', r.get('review_due') or '—')
           for r in rows if r['local_id'] not in seen]
    out += ['note-add %s · %s · exp-ids %s · %s' % (n['id'], n['title'], n['exp_ids'], n.get('editor') or '—')
            for n in _notes_since(conn, mainline, cursor.get('note_at'))]
    return out, {'rows': ids, 'note_at': last or cursor.get('note_at') or ''}


def audit_wait(conn, mainline, timeout, every, sleep, clock):
    """Returns the lines printed (E1). Keeps the cursor current on every poll (a row that left the stage can fire again)."""
    key = 'audit-wait:' + mainline
    end = clock() + max(0, timeout)
    while True:
        cur = db.meta_get(conn, key)
        lines, new = events(conn, mainline, cur)
        db.meta_set(conn, key, new)
        if lines:
            return lines
        if clock() >= end:
            return ['%d 分钟没有新的待复核行或 note-add --exp-ids' % (timeout // 60)]
        sleep(max(1, min(every, end - clock())))


def owner_thread(conn, experiment):
    """The cmsg of the thread that ran `experiment`: the editor of the newest note naming it, then that editor's waits."""
    if not experiment:
        return None, None
    from .autowait import find_thread
    for n in conn.execute("SELECT editor, exp_ids FROM task_notes WHERE COALESCE(exp_ids,'')!='' ORDER BY created_at DESC, id DESC"):
        if experiment in [x.strip() for x in (n['exp_ids'] or '').replace('，', ',').split(',')]:
            return find_thread(conn, n['editor']) or None, n['editor']
    return None, None


def wake_after_audit(conn, aid):
    """E2: after audit-add. Returns {'woke': thread, 'reply': RR id} / {'would': …} / {'skipped': why}."""
    a = conn.execute('SELECT * FROM audits WHERE id=?', (aid,)).fetchone()
    mode = modes.get(conn, 'audit-wake')
    if a is None or mode == 'off':
        return {'skipped': 'off' if a is not None else 'no audit %s' % aid}
    thread, editor = owner_thread(conn, a['experiment'])
    if not thread:
        return {'skipped': 'no thread for experiment %s' % (a['experiment'] or '—')}
    text = '%s 结论 %s%s · %s' % (aid, a['verdict'] or '—', (' ' + a['grade']) if a['grade'] else '', a['title'])
    if mode == 'observe':
        db.add_change(conn, EDITOR, '审计记录/' + aid, '本来会做：叫醒 %s（%s）：%s（audit-wake 在 observe）' % (editor, thread, text))
        return {'would': thread}
    rid = db._next_id(conn, 'report_replies', 'RR-%s-' % datetime.now(db.LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute('INSERT INTO report_replies(id, page, choice, text, received_at, thread, kind) VALUES (?,?,?,?,?,?,?)',
                 (rid, '审计 ' + aid, a['verdict'], text, db.now(), thread, 'audit'))
    conn.commit()
    return {'woke': thread, 'reply': rid}


def overdue(conn, now=None):
    """E3: [(local_id, title, hours)] 「已运行待复核」 rows older than 24 h with no audit naming them."""
    now = now or datetime.now(timezone.utc)
    named = set()
    for r in conn.execute('SELECT experiment FROM audits'):
        named.update(x.strip() for x in (r['experiment'] or '').replace('，', ',').split(',') if x.strip())
    out = []
    for r in _experiments(conn):
        # shortcut: the stage start is completed_at (a date, stamped on entering an ended stage) else updated_at; a row
        # edited later keeps its completed_at. Good enough for a 24-hour mark; a stage-change log would be exact.
        start = r.get('completed_at') or r.get('updated_at') or ''
        try:
            t = db.parse_time(start) if 'T' in start else datetime.strptime(start[:10], '%Y-%m-%d').replace(tzinfo=db.LOCAL_TZ)
        except (TypeError, ValueError):
            continue
        hours = (now - t).total_seconds() / 3600
        if hours > OVERDUE_HOURS and not ({r['local_id'], _code(r)} & named):
            out.append((r['local_id'], r.get('title') or '—', int(hours)))
    return out


def overdue_lines(conn, md, now=None):
    if modes.get(conn, 'review-overdue') != 'auto':
        return []
    return ['待复核超过 %d 小时没审计：%s · %s（约 %d 小时）' % (OVERDUE_HOURS, md(i), md(t), h) for i, t, h in overdue(conn, now)]
