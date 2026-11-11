"""Thread layer: the threads table (`r2i thread`), program-to-thread messages (`r2i send`, msgs table) and the Claude Code
hooks that log the owner's prompts (`r2i hook`, owner_msgs table). Threads are background agents a dispatcher session runs;
live agents talk through the native SendMessage, `r2i send` is for programs."""
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from . import config, db, ids

KINDS = ('推进', '审计', '讨论', '调度')  # 调度: the dispatcher; answers that name it are copied to it
BODY_MAX = 2000
SEND_LIMIT_MINUTES = 60
FIRST_MESSAGE = ('Your thread id is %s. Read agent/必带清单.md in the vault first, then start work under the rules '
                 'it points to. Use this id as --thread in every r2i command.')


def new(conn, title, kind=None, model=None, mainline=None, agent=None):
    """Insert a thread row; returns {'id', 'first_message'}."""
    if not (title or '').strip():
        raise db.HSError('--title is required')
    if kind is not None:
        db._check(kind, KINDS, 'kind')
    if mainline is not None:
        db._check(mainline, db.MAINLINES, 'mainline')
    tid = ids.new_thread_id()
    conn.execute('INSERT INTO threads(id,title,kind,model,mainline,agent,created_at) VALUES (?,?,?,?,?,?,?)',
                 (tid, title.strip(), kind, model, mainline, agent, db.now()))
    conn.commit()
    return {'id': tid, 'first_message': FIRST_MESSAGE % tid}


def set_fields(conn, tid, agent=None, title=None, status=None):
    if not ids.is_thread_id(tid):
        raise db.HSError('--id must be %s' % ids.THREAD_HINT)
    if status is not None:
        db._check(status, ('closed',), 'status')
    if not conn.execute('SELECT 1 FROM threads WHERE id=?', (tid,)).fetchone():
        raise db.HSError('no thread %s' % tid)
    if title is not None and not title.strip():
        raise db.HSError('--title must not be empty')
    for col, val in (('agent', agent), ('title', title and title.strip())):
        if val is not None:
            conn.execute('UPDATE threads SET %s=? WHERE id=?' % col, (val, tid))
    if status:
        conn.execute('UPDATE threads SET status=?, closed_at=COALESCE(closed_at, ?) WHERE id=?', (status, db.now(), tid))
    conn.commit()
    return dict(conn.execute('SELECT * FROM threads WHERE id=?', (tid,)).fetchone())


def list_threads(conn, all_=False):
    sql = 'SELECT * FROM threads %sORDER BY created_at, id' % ('' if all_ else "WHERE status='open' ")
    return [dict(r) for r in conn.execute(sql)]


def send(conn, to, body, from_=None, now=None):
    """Queue a message for a thread's next reply-wait. A thread (--from) may leave only one undelivered message per
    recipient per hour; program messages (no --from) are not limited."""
    if not ids.is_thread_id(to):
        raise db.HSError('--to must be %s' % ids.THREAD_HINT)
    if from_ is not None and not ids.is_thread_id(from_):
        raise db.HSError('--from must be %s' % ids.THREAD_HINT)
    body = (body or '').strip()
    if not body or len(body) > BODY_MAX:
        raise db.HSError('--body: 1 to %d characters' % BODY_MAX)
    now = now or datetime.now(timezone.utc)
    if from_:
        cut = (now - timedelta(minutes=SEND_LIMIT_MINUTES)).strftime('%Y-%m-%dT%H:%M:%SZ')
        if conn.execute('SELECT 1 FROM msgs WHERE from_thread=? AND to_thread=? AND delivered_at IS NULL AND created_at>?',
                        (from_, to, cut)).fetchone():
            raise db.HSError('%s already has an undelivered message from %s within %d minutes; wait for it to be read'
                             % (to, from_, SEND_LIMIT_MINUTES))
    cur = conn.execute('INSERT INTO msgs(to_thread,from_thread,body,created_at) VALUES (?,?,?,?)',
                       (to, from_, body, now.strftime('%Y-%m-%dT%H:%M:%SZ')))
    conn.commit()
    return {'id': cur.lastrowid, 'to': to}


def undelivered_msgs(conn, thread):
    return conn.execute('SELECT * FROM msgs WHERE to_thread=? AND delivered_at IS NULL ORDER BY id', (thread,)).fetchall()


def msg_line(m):
    return '｜'.join(['MSG-%d' % m['id'], '消息', '—', m['from_thread'] or '程序', ' '.join(m['body'].split())])


def mark_msgs_delivered(conn, rows):
    for m in rows:
        conn.execute('UPDATE msgs SET delivered_at=? WHERE id=? AND delivered_at IS NULL', (db.now(), m['id']))
    conn.commit()


def hook(kind, stdin=None, env=None, db_path=None):
    """`r2i hook user-prompt|pre-compact`. Never blocks the prompt: any error is a stderr message and exit 0."""
    env = os.environ if env is None else env
    try:
        if kind == 'pre-compact':
            print('Context is about to be compacted: write your handover now (r2i handover-append), then run r2i handover-check --mainline <id> until it passes, so the next session can continue.')
            return 0
        try:
            data = json.loads((sys.stdin if stdin is None else stdin).read() or '{}')
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        owner = 0 if env.get('R2I_DISPATCH') == '1' else 1
        mid = str(uuid.uuid4())
        conn = db.connect(db_path)
        try:
            conn.execute('INSERT INTO owner_msgs(id,session,agent,prompt_sha,is_owner,created_at) VALUES (?,?,?,?,?,?)',
                         (mid, 'cc_' + str(data.get('session_id') or 'unknown'), data.get('agent_id') or None,
                          hashlib.sha256(str(data.get('prompt') or '').encode('utf-8')).hexdigest(), owner, db.now()))
            conn.commit()
        finally:
            conn.close()
        if owner:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                                     'additionalContext': 'owner_msg id: ' + mid}}))
    except Exception as e:  # a hook must never stop the owner's prompt
        print('r2i hook %s: %s' % (kind, e), file=sys.stderr)
    return 0
