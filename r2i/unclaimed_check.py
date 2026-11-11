"""Cheap 「勾选没人取」 check (no model call, reads only snapshot.json).

  python3 -B -m r2i.unclaimed_check [--snapshot F] [--state F] [--minutes 15] [--all]

Prints one line per answer the owner gave on a page that no thread has picked up (report_replies with a thread, delivered_at
empty, received more than --minutes ago, within 24 h), each answer ONCE (ids already printed are kept in --state).
Prints nothing when there is nothing new, so the small 值班 session that runs it messages the coordinator only when
there is output. --all prints every open one again (ignores the state)."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db

STATE = config.state_dir()
SKIP_KINDS = ('seen', 'question', 'audit')


def _t(s):
    return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)


NO_THREAD = '没线程（页上没写「线程」，也没登记 wait）'


def page_threads(snap):
    """page name -> thread of the newest wait that points at it (report_waits / open_waits carry the page as 'report')."""
    best = {}
    for w in list(snap.get('report_waits', [])) + list(snap.get('open_waits', [])):
        if w.get('report') and w.get('thread'):
            k = w['report']
            if k not in best or (w.get('asked_at') or '') > (best[k].get('asked_at') or ''):
                best[k] = w
    return {k: w['thread'] for k, w in best.items()}


def settled(vault, page, choice):
    """True for an answer no thread has to act on (avoids false alarms): the page sits under 汇报/归档/ with 状态 已结
    (the work is closed), or it is a 汇报/同步/ page and the tick is only 「已看」. Needs the vault; False without it."""
    if not vault or not page:
        return False
    hits = sorted(Path(vault, '汇报').rglob(page + '.md'))
    if len(hits) != 1:
        return False
    rel = hits[0].relative_to(vault).as_posix()
    if rel.startswith('汇报/同步/'):
        return choice_label(choice) == '已看'
    if rel.startswith('汇报/归档/'):
        try:
            head = hits[0].read_text(encoding='utf-8').split('\n---', 1)[0]
        except OSError:
            return False
        return any(l.strip() in ('状态: 已结', '状态：已结') for l in head.splitlines())
    return False


def open_rows(snap, now, minutes=15, vault=None):
    # A reply whose page has no 线程 prop (thread None) is not skipped: the thread is looked up from the waits on that
    # page, and a reply with no thread at all is still printed (marked NO_THREAD) instead of dropped.
    out = []
    by_page = page_threads(snap)
    for r in snap.get('report_replies', []):
        if r.get('delivered_at') or (r.get('kind') or '') in SKIP_KINDS:
            continue
        if settled(vault, r.get('page'), r.get('choice')):
            continue
        if not r.get('thread'):
            r = dict(r, thread=by_page.get(r.get('page') or '') or NO_THREAD)
        if (r.get('page') or '').startswith('每日审计 '):
            continue
        at = _t(r['received_at'])
        if now - timedelta(hours=24) <= at <= now - timedelta(minutes=minutes):
            out.append(r)
    return out


def choice_label(c):
    """Option labels can carry a note after 「·」 (e.g. 「批 · 取消勾选 = 撤回预算 …」); the choice is only the part before it."""
    return (c or '').split('·')[0].strip()


def line(r):
    what = ' · '.join(x for x in (('选 ' + choice_label(r['choice'])) if choice_label(r.get('choice')) else '', ('「%s」' % r['text'][:60]) if r.get('text') else '') if x)
    return '%s %s「%s」%s → %s' % (r['id'], _t(r['received_at']).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M'), r.get('page') or '—',
                                   (' ' + what) if what else '', r['thread'])


# The same check on the Mac, inside render: one line under 首页「要你定」 and one ntfy push per answer
# (none in local 00-09; the push goes out after 09:00 if the answer is still not picked up). Mode 'unclaimed-push'.
HOME_MINUTES = 10
PUSHED_KEY = 'unclaimed-pushed'


def rows_from_db(conn, now=None, minutes=HOME_MINUTES, vault=None):
    """open_rows over the live ledger (the same three snapshot tables)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
    q = lambda sql, *p: [dict(r) for r in conn.execute(sql, p).fetchall()]
    snap = {'report_replies': q('SELECT * FROM report_replies WHERE received_at>=? ORDER BY id', since),
            'report_waits': q('SELECT * FROM waits WHERE report IS NOT NULL AND asked_at>=? ORDER BY id', since),
            'open_waits': q("SELECT * FROM waits WHERE status='等待' ORDER BY id")}
    return open_rows(snap, now, minutes, vault)


def current_thread(thread, threads):
    """A closed thread's open successor (same title, latest created_at); anything else comes back unchanged.
    `threads` is a list of dicts from the threads table."""
    me = next((t for t in threads if t.get('id') == thread), None)
    if not me or me.get('status') != 'closed':
        return thread
    alive = [t for t in threads if t.get('title') == me.get('title') and t.get('status') == 'open']
    return max(alive, key=lambda t: t.get('created_at') or '')['id'] if alive else thread


def snapshot_rows(conn, now=None, vault=None):
    """The 「勾选没人取」 rows for snapshot.json: where the answer was addressed, and the thread that now carries that work."""
    threads = [dict(t) for t in conn.execute('SELECT id, title, status, created_at FROM threads')]
    return [{'rr': r['id'], 'page': r.get('page'), 'thread': r['thread'], 'current_thread': current_thread(r['thread'], threads),
             'received_at': r['received_at']} for r in rows_from_db(conn, now, vault=vault)]


def home_lines(conn, rows, md):
    """[] or one line for 首页 under 「要你定」."""
    if not rows:
        return []
    from . import db
    pages = list(dict.fromkeys((r.get('page') or '—', r['thread']) for r in rows))
    who = lambda t: t if t == NO_THREAD else db.thread_name(conn, t)
    return ['- ⚠ **你勾了的 %d 页超过 %d 分钟没线程取**：%s' % (
        len(pages), HOME_MINUTES, '；'.join('%s → %s' % (md(p), md(who(t))) for p, t in pages))]


def push_new(conn, rows, now=None, pusher=None):
    """One push for the answers not pushed yet; quiet hours push nothing and mark nothing. Returns the ids pushed."""
    from . import db, waits
    now = now or datetime.now(timezone.utc)
    pushed = db.meta_get(conn, PUSHED_KEY, {})
    new = [r for r in rows if r['id'] not in pushed]
    if not new or waits.quiet(now):
        return []
    names = list(dict.fromkeys(r['thread'] if r['thread'] == NO_THREAD else db.thread_name(conn, r['thread']) for r in new))
    body = '%d 条回复超过 %d 分钟没线程取（%s），看首页「要你定」' % (len(new), HOME_MINUTES, '、'.join(names))  # no question text
    (pusher or waits.push_text_raw)(body, title='勾选没人取')
    horizon = (now - timedelta(days=2)).isoformat()
    pushed = {k: v for k, v in pushed.items() if v > horizon}
    pushed.update({r['id']: now.isoformat() for r in new})
    db.meta_set(conn, PUSHED_KEY, pushed)
    return [r['id'] for r in new]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--snapshot', default=str(STATE / 'snapshot.json'))
    ap.add_argument('--state', default=str(STATE / 'unclaimed-reported.json'))
    ap.add_argument('--minutes', type=int, default=15)
    ap.add_argument('--all', action='store_true')
    a = ap.parse_args(argv)
    snap = json.loads(Path(a.snapshot).read_text(encoding='utf-8'))
    now = datetime.now(timezone.utc)
    st = Path(a.state)
    try:
        seen = json.loads(st.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        seen = {}
    rows = open_rows(snap, now, a.minutes)
    new = [r for r in rows if a.all or r['id'] not in seen]
    for r in new:
        print(line(r))
    if new and not a.all:
        horizon = (now - timedelta(days=2)).isoformat()
        seen = {k: v for k, v in seen.items() if v > horizon}
        seen.update({r['id']: now.isoformat() for r in new})
        st.write_text(json.dumps(seen), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
