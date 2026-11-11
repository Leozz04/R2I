"""`cli notify`: one ntfy push telling the owner which words they must type in which thread (things only they can type: sending,
uploading, deleting). No wait is opened. The push carries the thread title and the exact words, never question text.

Night quiet hours (R-13, waits.quiet) defer the push into meta `typed_notify_pending`; reply_watch's loop flushes it
after 09:00. The same (thread, type) is pushed at most once per 30 minutes."""
from datetime import datetime, timedelta, timezone
import base64
import re

from . import db, waits
from . import config

SENT_KEY = 'typed_notify_sent'        # {"<thread>|<type>": iso time of the last accepted push}
PENDING_KEY = 'typed_notify_pending'  # [{thread, editor, type, why}] waiting for 09:00
RATE_MINUTES = 30
TYPE_MAX, WHY_MAX = 80, 60
CLICK = 'https://claude.ai/'
PATH = '首页'


def check(thread, editor, typed, why=None):
    """Same checks for the CLI/queue side and the host."""
    for name, v in (('--thread', thread), ('--editor', editor), ('--type', typed), ('--why', why)):
        if v is not None and re.search(r'[\r\n]', v):
            raise db.HSError('%s must be one line' % name)
    db.check_wait_thread(thread)
    if not (editor or '').strip():
        raise db.HSError('--editor (the thread title) is required')
    if not (typed or '').strip():
        raise db.HSError(config.say('--type (the exact words {owner} types) is required'))
    if len(typed) > TYPE_MAX:
        raise db.HSError('--type must be at most %d characters' % TYPE_MAX)
    if why is not None and len(why) > WHY_MAX:
        raise db.HSError('--why must be at most %d characters' % WHY_MAX)


def texts(editor, typed, why=None):
    title = '要你打字：%s' % editor.strip()[:40]
    body = '去这条线程打：「%s」' % typed.strip()
    if (why or '').strip():
        body = '%s · %s' % (why.strip(), body)
    return title, body


def _key(thread, typed):
    return '%s|%s' % (thread.strip(), typed.strip())


def _recent(sent, key, now):
    t = sent.get(key)
    return bool(t) and db.parse_time(t) > now - timedelta(minutes=RATE_MINUTES)


def _push(item, pusher):
    title, body = texts(item['editor'], item['type'], item.get('why'))
    return (pusher or waits.push_text_raw)(body, title=title, click=CLICK)


def run(conn, thread, editor, typed, why=None, now=None, pusher=None, vault=None):
    """Returns {'ok': True, 'sent'|'deferred'|'skipped': reason}."""
    check(thread, editor, typed, why)
    now = now or datetime.now(timezone.utc)
    key = _key(thread, typed)
    sent = db.meta_get(conn, SENT_KEY, {})
    if _recent(sent, key, now):
        return {'ok': True, 'skipped': 'same thread and words pushed in the last %d minutes' % RATE_MINUTES}
    item = {'thread': thread.strip(), 'editor': editor.strip(), 'type': typed.strip(), 'why': (why or '').strip()}
    horizon = now - timedelta(days=2)  # forget old rate-limit entries
    sent = {k: v for k, v in sent.items() if db.parse_time(v) > horizon}
    sent[key] = now.isoformat(timespec='seconds').replace('+00:00', 'Z')
    db.meta_set(conn, SENT_KEY, sent)
    if waits.quiet(now):
        pending = db.meta_get(conn, PENDING_KEY, [])
        pending.append(item)
        db.meta_set(conn, PENDING_KEY, pending)
        out = {'ok': True, 'deferred': 'quiet hours (00:00-09:00 local); pushed after 09:00'}
    else:
        ok = _push(item, pusher)
        out = {'ok': True, 'sent': 'pushed'} if ok else {'ok': True, 'sent': 'attempted, push not delivered (see run.log)'}
    if vault is not None:
        from .render import log_change
        log_change(vault, editor, PATH, '要你打字提醒（%s）：「%s」' % ('推送' if 'sent' in out else '夜间推迟', typed.strip()))
    return out


def flush(conn, now=None, pusher=None):
    """After 09:00 the deferred pushes go out, one each (one attempt, like the other pushes)."""
    now = now or datetime.now(timezone.utc)
    pending = db.meta_get(conn, PENDING_KEY, [])
    if not pending or waits.quiet(now):
        return 0
    db.meta_set(conn, PENDING_KEY, [])
    for item in pending:
        _push(item, pusher)
    return len(pending)


def _hdr(text):
    """HTTP header values are ASCII: ntfy reads RFC 2047 =?UTF-8?B?...?= ."""
    return text if text.isascii() else '=?UTF-8?B?%s?=' % base64.b64encode(text.encode('utf-8')).decode()
