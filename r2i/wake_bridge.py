"""Wake via notification: the 「叫醒」 button on a 等你回 push posts `wake <cmsg>` to a private ntfy topic (Keychain item
`wake topic item`). This long-running job (com.r2i.<project>.wake-bridge) streams that topic and, for each valid message, writes ONE
report_replies row (kind 'wake') for the thread. It wakes nothing itself and never sets delivered_at: a reply-wait hanging on
that thread picks the row up within 20 s; with none hanging the maintenance thread's cron relays it to the coordinator. If the
row is still undelivered after 3 minutes, one push tells the owner it did not wake (with a button to open the thread).

  python3 -B -m r2i.wake_bridge --vault <vault> [--once-from-file F]

--once-from-file reads ntfy JSON lines from F (test seam), handles them and exits. The topic is never printed or logged."""
import argparse
import json
import queue as _queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import db, render, waits
from .ids import THREAD_PAT
from . import config

LOG = config.state_dir() / 'logs' / 'wake.log'
HEARTBEAT = 'agent/管理/.wake-bridge-heartbeat'
HEARTBEAT_SECONDS = 30
LAST_ID_KEY = 'wake-bridge-last-id'
RATE_KEY = 'wake-last:'
RATE_SECONDS = 120
FOLLOWUP_SECONDS = 180
BACKOFF = (5, 60)
WAKE_RE = re.compile(r'^wake (%s)$' % THREAD_PAT)
PAGE, CHOICE, TEXT = '通知叫醒', '叫醒', config.say('{owner} 点了通知上的「叫醒」')


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write('%s %s\n' % (datetime.now(db.LOCAL_TZ).strftime('%F %T'), line))


def parse(line):
    """One ntfy JSON line -> (message id, text) for an event 'message', else None."""
    try:
        m = json.loads(line)
    except ValueError:
        return None
    if not isinstance(m, dict) or m.get('event') != 'message':
        return None
    return str(m.get('id') or ''), str(m.get('message') or '').strip()


def add_wake(conn, thread, at=None):
    """The one row: delivered_at stays NULL (see the module doc). Returns its id."""
    rid = db._next_id(conn, 'report_replies', 'RR-%s-' % datetime.now(db.LOCAL_TZ).strftime('%Y%m%d'))
    conn.execute("INSERT INTO report_replies(id,page,wait_id,choice,text,page_mtime,received_at,thread,delivered_at,kind) "
                 "VALUES (?,?,NULL,?,?,NULL,?,?,NULL,'wake')", (rid, PAGE, CHOICE, TEXT, at or db.now(), thread))
    conn.commit()
    return rid


def handle(conn, text, state, now=None):
    """One message text. Returns the new row id, or None (not a wake / bad thread / rate-limited)."""
    now = time.time() if now is None else now
    m = WAKE_RE.match(text or '')
    if not m:
        return None
    thread = m.group(1)
    try:
        db.check_thread(thread)
    except db.HSError:
        log('ignored: not a thread id')
        return None
    last = db.meta_get(conn, RATE_KEY + thread)
    if isinstance(last, (int, float)) and now - last < RATE_SECONDS:
        log('rate-limited %s' % thread[:14])
        return None
    db.meta_set(conn, RATE_KEY + thread, now)
    rid = add_wake(conn, thread)
    state.setdefault('followups', []).append((rid, thread, now + FOLLOWUP_SECONDS))
    log('wake %s for %s' % (rid, thread[:14]))
    return rid


def process_line(conn, line, state, now=None):
    msg = parse(line)
    if not msg:
        return None
    mid, text = msg
    if mid:
        state['last_id'] = mid
        db.meta_set(conn, LAST_ID_KEY, mid)
    return handle(conn, text, state, now)


def followups(conn, state, now=None, pusher=None):
    """A wake row still undelivered 3 minutes on: one push (once), then forget it."""
    now = time.time() if now is None else now
    keep = []
    for rid, thread, due in state.get('followups', []):
        if now < due:
            keep.append((rid, thread, due))
            continue
        row = conn.execute('SELECT delivered_at FROM report_replies WHERE id=?', (rid,)).fetchone()
        if row and row['delivered_at'] is None:
            name = db.thread_name(conn, thread)
            ok = (pusher or waits.push_text_raw)('没叫醒 %s，已转调度；急的话打开这个线程打一个字' % name)
            log('no pickup for %s, follow-up push %s' % (rid, 'sent' if ok else 'not sent'))
    state['followups'] = keep


def _reader(state, out, stop):
    """Thread: stream the topic with curl, push lines on `out`; reconnect with backoff 5 -> 60 s."""
    delay = BACKOFF[0]
    while not stop.is_set():
        topic = waits._topic(waits.WAKE_KEY)
        if not topic:
            log('wake topic off: set [keys] %s in r2i.toml' % waits.WAKE_KEY)
        else:
            url = '%s%s/json?since=%s' % (waits.NTFY_URL, topic, state.get('last_id') or '10m')
            try:
                p = subprocess.Popen(['curl', '-sN', url], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                     encoding='utf-8', errors='replace')
                for line in p.stdout:
                    out.put(line)
                    delay = BACKOFF[0]
                    if stop.is_set():
                        break
                p.kill(); p.wait()
            except OSError as e:
                log('curl failed: %s' % type(e).__name__)
        stop.wait(delay)
        delay = min(BACKOFF[1], delay * 2)


def beat(vault):
    render._write(Path(vault) / HEARTBEAT, datetime.now(db.LOCAL_TZ).isoformat(timespec='seconds') + '\n')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='wake_bridge')
    ap.add_argument('--vault', required=True)
    ap.add_argument('--db', default=None)
    ap.add_argument('--once-from-file', help='handle ntfy JSON lines from this file, then exit (test seam)')
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    state = {'last_id': db.meta_get(conn, LAST_ID_KEY), 'followups': []}
    if args.once_from_file:
        for line in Path(args.once_from_file).read_text(encoding='utf-8').splitlines():
            process_line(conn, line, state)
        return 0
    log('started')
    lines, stop = _queue.Queue(), threading.Event()
    threading.Thread(target=_reader, args=(state, lines, stop), daemon=True).start()
    last_beat = 0.0
    while True:
        try:
            line = lines.get(timeout=1)
        except _queue.Empty:
            line = None
        try:
            if line:
                process_line(conn, line, state)
            followups(conn, state)
        except Exception as e:  # keep running; launchd would restart us anyway
            log('error: %s' % type(e).__name__)
        if time.time() - last_beat >= HEARTBEAT_SECONDS:
            try:
                beat(args.vault)
            except OSError as e:
                log('heartbeat failed: %s' % e)
            last_beat = time.time()


if __name__ == '__main__':
    sys.exit(main())
