"""R-10: a question to the owner that waits 30 minutes shows on 首页「要你定」 (管理/等你回复.md is no longer written)
and one Mac notification, plus one iPhone push through ntfy. Quiet hours (config quiet_hours): those notify together
when they end.

The ntfy topic is read from the Keychain item named in [keys] ntfy_topic (the owner creates it) and is never printed or logged.
The push carries only thread names and a count, never the question text.
Manual test (on the Mac): PYTHONPATH=<repo> python3 -B -m r2i.waits ntfy-test"""
from datetime import datetime, timedelta, timezone
import os
import re
import subprocess
import sys

from . import config, db
from .ids import is_thread_id

PAGE = '管理/等你回复.md'  # retired: render no longer writes or reads it
DUE_MINUTES = 30
EXPIRE_HOURS = 72
NTFY_KEY = 'ntfy_topic'  # [keys] names in r2i.toml: the Keychain item holding the ntfy topic
WAKE_KEY = 'ntfy_wake_topic'  # the topic wake_bridge listens on; the 「叫醒」 button of a push posts to it
NTFY_URL = 'https://ntfy.sh/'
TOPIC_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
WITHDRAWN = '撤回'  # waits.closed_by of a wait autowait withdrew
THREAD_SRC = '审计改'  # = audits.SRC: waits on a thread, never on the owner
WAIT_RE = re.compile(r'^[ \t]*- \[(.)\][ \t]*(.*?)[ \t]*\^(wait-\d{8}-\d+)[ \t]*$', re.M)


def _at(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _ago(minutes, now):
    return (now - timedelta(minutes=minutes)).isoformat(timespec='seconds').replace('+00:00', 'Z')


def readback(conn, vault, busy):
    page = vault / PAGE
    if not page.exists() or busy(page):
        return []
    ticked = [wid for mark, _, wid in WAIT_RE.findall(page.read_text(encoding='utf-8')) if mark in 'xX']
    return [wid for wid in ticked if db.close_wait(conn, wid=wid, status='已勾', by='页面勾选')]


def expire(conn, now=None):
    now = now or datetime.now(timezone.utc)
    cur = conn.execute("UPDATE waits SET status='过期', closed_at=? WHERE status='等待' AND asked_at<?",
                       (db.now(), _ago(EXPIRE_HOURS * 60, now)))
    conn.commit()
    return cur.rowcount


def due(conn, now=None):
    """Open waits the owner owes an answer to, 30 minutes old. A wait the audit 改 tick opens (src 审计改) waits on the thread,
    not on the owner, so it is not listed here: not on 首页, not in 等你回复.md, not in the notification."""
    now = now or datetime.now(timezone.utc)
    return conn.execute("SELECT * FROM waits WHERE status='等待' AND COALESCE(src,'')!=? AND asked_at<=? ORDER BY id",
                        (THREAD_SRC, _ago(DUE_MINUTES, now))).fetchall()


def _link(thread):
    return '[线程](%s)' % thread if thread.startswith('http') else '[[%s]]' % thread


def row_text(r, md, vault=None):
    """「问题 · 线程名 · [[汇报/页|去回复]]」. No [[cc_…]] link, which Obsidian
    reads as a missing note; the thread is named by its title (the wait's editor), the link goes to the report page."""
    from .render import _short
    ask = (r['ask'] or '').split('｜')[-1]
    out = '%s · %s' % (md(_short(ask, 40)), md(r['editor']).replace('[', '［').replace(']', '］'))
    report = r['report'] if 'report' in r.keys() else None
    typed = r['typed'] if 'typed' in r.keys() else None
    if typed:  # a tick does not count for these (R-10): the owner types the words in chat
        return out + ' · 在聊天里回「%s」' % typed + (' · [[%s|去看]]' % db.report_rel(vault, report) if report else '')
    return out + (' · [[%s|去回复]]' % db.report_rel(vault, report) if report else '')


def closed_block(conn, md, vault=None):
    """首页's last block (it used to be two sections of 管理/等你回复.md): waits that expired and waits
    autowait withdrew in the last 7 days, folded. A withdrawn wait is status 过期 + closed_by 撤回 (no new status).
    Returns [] when there are none."""
    recent = conn.execute("SELECT * FROM waits WHERE status='过期' AND closed_at>=? ORDER BY id DESC",
                          (_ago(7 * 24 * 60, datetime.now(timezone.utc)),)).fetchall()
    old = [r for r in recent if r['closed_by'] != WITHDRAWN]
    gone = [r for r in recent if r['closed_by'] == WITHDRAWN]
    if not recent:
        return []
    rows = lambda rs: ['> - %s · %s' % (r['asked_at'][5:10], row_text(r, md, vault)) for r in rs] or ['> 无。']
    return (['', '> [!note]- 已过期 %d · 已撤回 %d（最近 7 天，不再提醒）' % (len(old), len(gone)), '> **已过期**（超过 %d 小时没回）' % EXPIRE_HOURS]
            + rows(old) + ['>', '> **已撤回**（页上改了或删了这一问）'] + rows(gone))


def quiet(now):
    return config.in_quiet(now.astimezone(db.LOCAL_TZ).hour)


def send(title, text):
    script = 'display notification %s with title %s' % (_as(text), _as(title))
    subprocess.run(['osascript', '-e', script], capture_output=True, timeout=15)


def _as(text):
    return '"%s"' % text.replace('\\', '\\\\').replace('"', '\\"')


def _log(line):
    # render runs with --quiet and run.sh appends its stdout+stderr to run.log
    print('ntfy: ' + line, file=sys.stderr)


def _topic(key=NTFY_KEY):
    """The ntfy topic from the Keychain item named in [keys], or None (no item set: pushes are off). The value never leaves
    this function except in the URL."""
    item = config.get().key(key)
    if not item:
        return None
    try:
        p = subprocess.run(['security', 'find-generic-password', '-s', item, '-w'],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    topic = p.stdout.strip() if p.returncode == 0 else ''
    return topic if TOPIC_RE.fullmatch(topic) else None


def push_text(names, count):
    """Only thread names and a count: never the question text."""
    names = [n[:40] for n in dict.fromkeys(n for n in names if n)]
    shown = '、'.join(names[:3]) + ('等' if len(names) > 3 else '')
    if count == 1:
        return '等你回：%s' % (shown or '1 条')
    return '等你回 %d 条：%s' % (count, shown) if shown else '等你回 %d 条' % count


def push_actions(threads):
    """(click url, ntfy Actions string) for the threads of a push: per thread a 「叫醒」 button when the wake topic is set
    in [keys], 3 actions at most. (None, None) without a usable thread id or wake topic."""
    threads = [t for t in dict.fromkeys(threads or []) if is_thread_id(t)]
    wake = _topic(WAKE_KEY) if threads else None
    if not wake:
        return None, None
    acts = ['http, 叫醒, %s%s, method=POST, body=wake %s, clear=true' % (NTFY_URL, wake, t) for t in threads]
    return None, '; '.join(acts[:3])


def push(names, count, threads=None):
    """Send one ntfy push. Never raises; a skipped or failed push only writes one line to run.log."""
    click, actions = push_actions(threads)
    return push_text_raw(push_text(names, count), click=click, actions=actions)


def push_text_raw(text, click=None, actions=None, title=None):
    """One ntfy push of `text` (callers keep question text out of it). `title` / `click` / `actions` become the ntfy headers
    Title / Click / Actions (no line breaks allowed). Never raises."""
    if os.environ.get('R2I_NO_PUSH'):  # dry runs never reach a phone
        return False
    try:
        extra = []
        for head, value in (('Click', click), ('Actions', actions), ('Title', title)):
            if value is None:
                continue
            if '\n' in value or '\r' in value:
                _log('推送没发出去（%s 里有换行）' % head)
                return False
            if not value.isascii():  # ntfy decodes RFC 2047 encoded words in headers; raw UTF-8 may be mangled
                import base64
                value = '=?UTF-8?B?%s?=' % base64.b64encode(value.encode('utf-8')).decode('ascii')
            extra += ['-H', '%s: %s' % (head, value)]
        topic = _topic()
        if not topic:
            _log('跳过推送：r2i.toml 的 [keys] 没设 %s，或钥匙串里读不到' % NTFY_KEY)
            return False
        head = [] if title is not None else ['-H', 'Title: ' + config.get().project]
        r = subprocess.run(['curl', '-fsS', '-m', '10'] + head + ['-d', text, NTFY_URL + topic] + extra,
                           capture_output=True, timeout=20)
        if r.returncode:
            _log('推送没发出去（curl 退出码 %d）' % r.returncode)  # curl's own message may contain the URL: not logged
            return False
        return True
    except Exception as e:  # a push must never stop rendering
        _log('推送没发出去（%s）' % type(e).__name__)
        return False


def notify(conn, now=None, sender=None, pusher=None):
    """One notification for every due wait not yet announced; nothing during quiet hours."""
    now = now or datetime.now(timezone.utc)
    if quiet(now):
        return []
    fresh = [r for r in due(conn, now) if not r['notified_at']]
    if not fresh:
        return []
    first = '%s：%s' % (fresh[0]['editor'], fresh[0]['ask'])
    text = first if len(fresh) == 1 else '%s 等 %d 条' % (first[:60], len(fresh))
    (sender or send)('%s：等你回 %d 条' % (config.get().project, len(fresh)), text)
    names = [r['editor'] for r in fresh]
    if pusher:
        pusher(names, len(fresh))
    else:
        push(names, len(fresh), threads=[r['thread'] for r in fresh])
    conn.executemany('UPDATE waits SET notified_at=? WHERE id=?', [(db.now(), r['id']) for r in fresh])
    conn.commit()
    return [r['id'] for r in fresh]


if __name__ == '__main__':
    if sys.argv[1:] == ['ntfy-test']:
        ok = push(['手动测试'], 1)
        print('已推送，看 iPhone 上的 ntfy' if ok else '没推出去，原因见上一行')
        sys.exit(0 if ok else 1)
    print('usage: python3 -B -m r2i.waits ntfy-test')
    sys.exit(2)
