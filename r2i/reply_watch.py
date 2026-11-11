"""the owner answers on a report page (汇报/*.md, section 「## 回复」) and the answer reaches sqlite within seconds.

Runs on the Mac only, kept alive by launchd com.r2i.<project>.report-reply. Every 2 seconds it
stats 汇报/*.md; a page whose mtime changed is parsed once it has been still for 2 seconds (Obsidian saves every 2 s
while the owner types). An answer is: a ticked option line, or the text after 「回复：」 together with a ticked 「发出」.
Text written without 「发出」 is never read, so half a sentence is not sent.

On an answer: one report_replies row; the page's wait closes (closed_by 汇报页, waits.answer = 「选 B · 原话」); the
section becomes 「> [!done] 已收到 …」 and the page property 状态 becomes 已回; a changelog line; then the routine
「汇报回复转发」 (Haiku) relays it to the asking thread: replies within 2 minutes go in one run. A failed wake is logged and shown as one
Mac notification, never retried (the API has no idempotency key). The token is read from the Keychain item
named in r2i.toml [keys] wake_token (feature off when unset) and is never printed or logged.

Money: ticking 「批」 on a page whose properties carry 预算 and 上限 writes that
budget through the same checks as budget-add (approval 「汇报页 <页名> 勾选『批』 <时间>」, quote 「批」); the receipt keeps
one checkbox 「批 · 取消勾选 = 撤回预算 X」 and unticking it is budget-revoke. A page without those properties: 「批」 is a
plain answer. Each written budget gets one phone push (ntfy); at night they wait and go as one after 09:00.
Claude never ticks a reply section.

Run by hand on the Mac:  python3 -B -m r2i.reply_watch --vault "<vault>" --once"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import db, notify, render, waits
from .ids import is_thread_id
from . import config

CONFIG = config.state_dir() / 'reply_watch.json'
LOG = config.state_dir() / 'logs' / 'reply.log'
EDITOR = '汇报回复监听'
POLL_SECONDS = 2
SETTLE_SECONDS = 2
HEARTBEAT_SECONDS = 30
TOKEN_KEY = 'wake_token'  # [keys] name in r2i.toml; suggested item name '<keychain_prefix>wake-token'
FIRE_URL = 'https://api.anthropic.com/v1/claude_code/routines/%s/fire'
API_VERSION = '2023-06-01'  # as documented for Claude Code routines
API_BETA = 'experimental-cc-routine-2026-04-01'

SECTION_RE = re.compile(r'^## 回复[ \t]*\n(.*?)(?=^## |\Z)', re.M | re.S)
DECIDE_RE = re.compile(r'^## 要你定[ \t]*\n(.*?)(?=^## |\Z)', re.M | re.S)


class _Span:
    """A match-like span (group/start/end of group 1) for a reply section that runs past a heading the owner wrote."""
    def __init__(self, text, a, b):
        self.text, self.a, self.b = text, a, b

    def group(self, i=0):
        return self.text[self.a:self.b]

    def start(self, i=0):
        return self.a

    def end(self, i=0):
        return self.b


ANSWER_HEAD = '### 我的回答'
SEND_BOX_RE = re.compile(r'^[ \t]*[-*+] \[.\][ \t]*发出\b.*$\n?', re.M)


def _section(text):
    """「## 回复」; on a page without one, 「## 要你定」 (ticks made there are read too).
    With 「### 我的回答」 the section runs to that block's 发出 box, so headings and tables the owner writes there stay inside."""
    m = SECTION_RE.search(text) or DECIDE_RE.search(text)
    if m and ANSWER_HEAD in m.group(1) and '[!done]' not in m.group(1):
        s = SEND_BOX_RE.search(text, text.index(ANSWER_HEAD, m.start(1)))
        if s and s.end() > m.end(1):
            return _Span(text, m.start(1), s.end())
    return m
BOX_RE = re.compile(r'^[ \t]*[-*+] \[(.)\][ \t]*(.*?)[ \t]*$', re.M)
TEXT_RE = re.compile(r'^[ \t]*回复[：:][ \t]*(.*?)[ \t]*$', re.M)
SEND = '发出'
APPROVE, REJECT = '批', '不批'
PUSH_KEY = 'page_budget_pushes'
REVOKE_RE = re.compile(r'^([ \t]*[-*+] )\[(.)\][ \t]*批 · 取消勾选 = 撤回预算 (\S+?)(?:[ \t]*✅[ \t]*\d{4}-\d{2}-\d{2})?[ \t]*$', re.M)


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write('%s %s\n' % (datetime.now(db.LOCAL_TZ).strftime('%F %T'), line))


def front(text):
    """The page properties as {name: value} (plain `name: value` lines only)."""
    if not text.startswith('---\n') or '\n---' not in text[3:]:
        return {}
    block = text[4:text.index('\n---', 3)]
    out = {}
    for line in block.splitlines():
        m = re.match(r'^([^\s:#][^:]*):[ \t]*(.*)$', line)
        if m:
            out[m.group(1).strip()] = m.group(2).strip().strip('"\'')
    return out


QKEY_RE = re.compile(r'^(\d{1,2})[ \t]*[.、．:：)）]?[ \t]*([A-Za-z])(?![A-Za-z])')  # 「1A」「1. B」「2、A」
QHEAD_RE = re.compile(r'^###[ \t]*(?:第[ \t]*)?(\d{1,2})(?!\d)')  # 「### 1」「### 第2题」 inside 「## 回复」


THREAD_PART_RE = re.compile(r'^>[ \t]*\[!info\][ \t]*已在线程里答[^：\n]*：([^（\n]*)', re.M)  # thread-answer wrote 「已在线程里答 2/5：1A 2B（消息 …）」
Q_TOKEN_RE = re.compile(r'(?<!\d)(\d{1,2})[ \t]*([A-Za-z])(?![A-Za-z])')


def thread_answered(sec):
    """Questions the owner already answered in the thread chat (thread-answer): {number: '1A'}."""
    return {m.group(1): m.group(1) + m.group(2).upper() for line in THREAD_PART_RE.findall(sec) for m in Q_TOKEN_RE.finditer(line)}


def _boxes(sec):
    """[(ticked, label, question number or None)] of the reply section. A box belongs to a question by its label (1A, 2B)
    or by the 「### N」 heading above it."""
    key, out = None, []
    for line in sec.split('\n'):
        h = QHEAD_RE.match(line.strip())
        if h:
            key = h.group(1)
            continue
        b = BOX_RE.match(line)
        if not b:
            continue
        label = render.DONE_RE.sub('', b.group(2)).strip()  # Tasks may add its ✅ date when they tick
        k = QKEY_RE.match(label)
        out.append((b.group(1) in 'xX', label, k.group(1) if k else (key if label != SEND else None)))
    return out


SLOT_RE = re.compile(r'^[ \t]*(?:[-*+][ \t]+)?((?:[A-Za-z]{1,2})?\d{1,2})[：:][ \t]*(.*)$')
HINT = '> 在下面写你的回答'


def answer_text(sec):
    """The owner's words in 「### 我的回答」 up to its 发出 box, verbatim (headings, tables, blank lines inside kept);
    the hint line and empty slots (「- E3：」) are dropped, a filled slot loses its 「- 」."""
    if ANSWER_HEAD not in sec:
        return ''
    body = sec[sec.index(ANSWER_HEAD) + len(ANSWER_HEAD):]
    s = SEND_BOX_RE.search(body)
    out = []
    for line in body[:s.start() if s else len(body)].split('\n')[1:]:
        k = SLOT_RE.match(line)
        if line.startswith(HINT) or (k and not k.group(2).strip()):
            continue
        out.append(re.sub(r'^[ \t]*[-*+][ \t]+', '', line.rstrip()) if k else line.rstrip())
    return '\n'.join(out).strip('\n')


def parse(text):
    """the owner's answer in 「## 回复」, or None (no section, already received, nothing ticked, text without 发出).
    A page with two or more questions (options 1A, 2B… or 「### 1」「### 2」 headings) also carries multi, total,
    answered {n: [labels]} and complete: every question ticked, 发出 ticked, or a box outside the questions (撤回) ticked."""
    m = _section(text)
    if not m or '[!done]' in m.group(1):
        return None
    sec = m.group(1)
    boxes = _boxes(sec)
    qs = sorted({q for _, _, q in boxes if q}, key=int)
    via = thread_answered(sec)
    choices, send, answered, whole = [], False, {}, False
    for ticked, label, q in boxes:
        if not ticked or not label:
            continue
        if label == SEND:
            send = True
            continue
        if q and not QKEY_RE.match(label):
            label = q + label  # 「### 2」 + 「A 计入」 -> 「2A 计入」
        choices.append(label)
        if q:
            answered.setdefault(q, []).append(label)
        else:
            whole = True
    t = TEXT_RE.search(sec)
    words = '\n'.join(x for x in (render.DONE_RE.sub('', t.group(1)).strip() if t else '', answer_text(sec)) if x)
    if words and not send:
        return None  # a sentence counts only with 发出 ticked, even when an option is ticked too
    if not choices and not (words and send):
        return None
    out = {'choice': '、'.join(choices) or None, 'text': words or None}
    if len(qs) >= 2:
        out.update(multi=True, total=len(qs), answered=answered,
                   complete=send or whole or all(q in answered or q in via for q in qs))
    return out


FILE_LINK_RE = re.compile(r'!?\[\[([^\]|#\n]+\.(?!md\b)[A-Za-z0-9]{1,5})(?:[#|][^\]\n]*)?\]\]')
ATTACH_LINE = '附件（页里链接的文件，绝对路径）：'


def linked_files(vault, page, sec):
    """Absolute paths of the non-md files the reply section links ([[x.zip]], ![[附件/x.csv|x]]), resolved the way
    Obsidian does: the exact vault path, then <report dir>/附件, then the page's folder, then any file of that name in the vault."""
    vault, out = Path(vault), []
    for name in dict.fromkeys(m.strip() for m in FILE_LINK_RE.findall(sec)):
        cands = [vault / name, vault / db.REPORT_DIR / '附件' / name, Path(page).parent / name]
        hit = next((c for c in cands if c.is_file()), None)
        if not hit:  # walks the vault once per unresolved link; fine for a few links per reply
            hit = next((c for c in vault.rglob(Path(name).name) if c.is_file() and '.obsidian' not in c.parts
                        and '.trash' not in c.parts), None)
        out.append(str(hit) if hit else '%s（vault 里没找到）' % name)
    return out


def with_files(reply, vault, page, text):
    """The reply with an 附件 block (absolute paths) appended to its text, so the ledger row and the wake carry the files."""
    m = _section(text)
    files = linked_files(vault, page, m.group(1)) if m else []
    if not files:
        return reply
    block = ATTACH_LINE + '\n' + '\n'.join('- ' + f for f in files)
    return dict(reply, text=(reply['text'] + '\n\n' + block) if reply['text'] else block)


def progress(text):
    """(answered, total) for a multi-question page that is still partly answered, else None (首页 「已答 n/m」)."""
    r = parse(text)
    m = _section(text)
    via = thread_answered(m.group(1)) if m else {}
    if r and r.get('multi') and not r['complete']:
        return len(set(r['answered']) | set(via)), r['total']
    if not r and m and '[!done]' not in m.group(1):
        qs = {q for _, _, q in _boxes(m.group(1)) if q}
        if len(qs) >= 2:
            return len(qs & set(via)), len(qs)
    return None


def record_questions(conn, page, wait, reply, mtime):
    """One report_replies row per answered question (kind question, no thread: the record, nobody is woken)."""
    new = []
    for q in sorted(reply['answered'], key=int):
        rid = db.add_question_reply(conn, page=page, wait_id=wait['id'] if wait else None,
                                    choice='、'.join(reply['answered'][q]), page_mtime=mtime)
        if rid:
            new.append(rid)
    return new


def received(text, reply, at, budget=None):
    """The page with its reply section replaced by the receipt and 状态 set to 已回. A written budget keeps one
    checkbox below the receipt: unticking it withdraws the budget."""
    parts, full = [], False
    if reply['choice']:
        parts.append('选 ' + reply['choice'])
    if reply['text']:
        lines = [l for l in reply['text'].split('\n') if l.strip()]
        if len(lines) <= 3 and len(reply['text']) <= 200 and '|' not in reply['text']:
            parts.append('「%s」' % ' / '.join(lines))  # the receipt is one quote line; the ledger keeps the lines
        else:
            parts.append('「%s…」（全文 %d 行，原样留在下面）' % (lines[0][:40], len(lines)))
            full = True
    tail = ''
    if budget and budget.get('error'):
        parts.append('预算没写入：%s' % budget['error'])
    elif budget:
        parts.append('预算 %s ¥%g 已写入，%s 前有效' % (budget['id'], budget['cap'], budget['expires']))
        tail = '- [x] 批 · 取消勾选 = 撤回预算 %s\n\n' % budget['id']
    if reply['text'] and full:
        tail += '### 你写的回答（已收到）\n\n%s\n\n' % reply['text']  # long answers stay readable on the page
    receipt = '\n> [!done] 已收到 %s：%s\n\n%s' % (at.strftime('%m-%d %H:%M'), ' · '.join(parts), tail)
    m = _section(text)
    text = text[:m.start(1)] + receipt + text[m.end(1):]
    if text.startswith('---\n'):
        end = text.index('\n---', 3)
        head, rest = text[:end], text[end:]
        if re.search(r'^状态:', head, re.M):
            head = re.sub(r'^状态:.*$', '状态: 已回', head, count=1, flags=re.M)
        else:
            head += '\n状态: 已回'
        text = head + rest
    return text


def _token_item():
    """The Keychain item named in [keys] wake_token, or None (wake is then off)."""
    return config.get().key(TOKEN_KEY)


def _token():
    item = _token_item()
    if not item:
        return None
    try:
        p = subprocess.run(['security', 'find-generic-password', '-s', item, '-w'],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def _notify(text):
    if waits.quiet(datetime.now(timezone.utc)):  # nothing for the owner at night; the log has it
        return
    try:
        waits.send('%s 汇报回复' % config.get().project, text)
    except (OSError, subprocess.SubprocessError):
        pass


def _cap(text):
    t = re.sub(r'[¥￥元\s]|CNY|RMB', '', str(text or ''), flags=re.I)
    try:
        return float(t)
    except ValueError:
        raise db.HSError('上限 must be an amount in CNY such as ¥40, got %r' % text)


def page_budget(conn, page, props, at):
    """the owner ticked 「批」: write the page's budget (预算/上限/用途). None when the page carries no budget."""
    bid, cap = props.get('预算'), props.get('上限')
    if not bid or not cap:
        log('%s: 批 ticked but the page has no 预算/上限 properties: plain answer, no budget' % page)
        return None
    try:
        amount = _cap(cap)
        db.add_verbal_budget(conn, id=bid, cap_cny=amount, purpose=props.get('用途') or page,
                             approval_msg='%s%s 勾选『批』 %s' % (db.PAGE_APPROVAL_PREFIX, page, at.strftime('%Y-%m-%d %H:%M')),
                             approval_at=db.now(), approval_quote=APPROVE, editor=EDITOR, page=True)
    except db.HSError as e:
        log('%s: budget %s not written: %s' % (page, bid, e))
        _notify('汇报页的「批」没写成预算 %s' % bid)
        return {'id': bid, 'error': str(e)}
    row = conn.execute('SELECT expires_at FROM verbal_budgets WHERE id=?', (bid,)).fetchone()
    expires = db.parse_time(row['expires_at']).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M')
    pending = db.meta_get(conn, PUSH_KEY, []) + [{'id': bid, 'cap': amount}]
    db.meta_set(conn, PUSH_KEY, pending)
    log('%s: budget %s ¥%g written from the page (expires %s)' % (page, bid, amount, expires))
    return {'id': bid, 'cap': amount, 'expires': expires}


def flush_pushes(conn, now=None, pusher=None):
    """Phone confirmations for page budgets: none at night; after 09:00 the waiting ones go as one push."""
    now = now or datetime.now(timezone.utc)
    pending = db.meta_get(conn, PUSH_KEY, [])
    if not pending or waits.quiet(now):
        return None
    if len(pending) == 1:
        text = '已按你在汇报页的勾选写入预算 %s ¥%g，撤回就在页上取消勾选' % (pending[0]['id'], pending[0]['cap'])
    else:
        text = '已按你在汇报页的勾选写入 %d 笔预算：%s；撤回就在各页上取消勾选' % (
            len(pending), '、'.join('%s ¥%g' % (p['id'], p['cap']) for p in pending))
    ok = (pusher or waits.push_text_raw)(text)
    db.meta_set(conn, PUSH_KEY, [])  # one attempt only, like the other pushes; the log keeps the result
    log('budget push %s: %d item(s)' % ('sent' if ok else 'not sent', len(pending)))
    return text


def revoke_unticked(conn, vault, path, mtime, text):
    """The 「批 · 取消勾选」 box of a received page was unticked: budget-revoke, once."""
    page = path.name[:-3]
    done = []
    for m in REVOKE_RE.finditer(text):
        if m.group(2) not in ' ':
            continue
        bid = m.group(3)
        row = conn.execute('SELECT * FROM verbal_budgets WHERE id=?', (bid,)).fetchone()
        if not row or not row['approval_msg'].startswith('%s%s ' % (db.PAGE_APPROVAL_PREFIX, page)):
            log('%s: unticked line names budget %s, which this page did not write: ignored' % (page, bid))
            continue
        if row['status'] == '有效':
            db.revoke_verbal_budget(conn, bid, EDITOR, '汇报页取消勾选')
        done.append((m, bid))
    if not done or path.stat().st_mtime != mtime:
        return [bid for _, bid in done] if done else None
    stamp = datetime.now(db.LOCAL_TZ).strftime('%m-%d %H:%M')
    for m, bid in reversed(done):
        text = text[:m.start()] + '%s已撤回 %s：预算 %s（你在页上取消了勾选）' % (m.group(1), stamp, bid) + text[m.end():]
    render._write(path, text)
    for _, bid in done:
        render.log_change(vault, EDITOR, path.relative_to(vault).as_posix(), '汇报页取消勾选，撤回预算 %s' % bid)
        log('%s: budget %s revoked (unticked on the page)' % (page, bid))
    return [bid for _, bid in done]


HOME_TICKS = False  # 首页 has no checkboxes; True brings the option cards back
WAKE_KEY = 'pending_wakes'
WAKE_WINDOW_SECONDS = 120  # replies arriving within this window share one merged wake


def wake_line(page, wait, reply, budget=None):
    choice = '选 ' + reply['choice'] if reply['choice'] else '没选'
    if budget and not budget.get('error'):
        choice += '（预算 %s ¥%g 已写入台账）' % (budget['id'], budget['cap'])
    return '｜'.join([page, wait['id'] if wait else '—', wait['thread'] if wait else '—', choice, reply['text'] or '—'])


def queue_wake(conn, page, wait, reply, budget=None):
    """Adds one reply to the next relay; the first one starts the 2-minute window."""
    pending = db.meta_get(conn, WAKE_KEY, {}) or {}
    lines = pending.get('lines', []) + [wake_line(page, wait, reply, budget)]
    db.meta_set(conn, WAKE_KEY, {'since': pending.get('since') or time.time(), 'lines': lines})
    return 'queued'


def flush_wakes(conn, now=None, firer=None):
    """When the oldest queued reply is 2 minutes old, fire the relay once with every queued line. One attempt only."""
    now = now or time.time()
    pending = db.meta_get(conn, WAKE_KEY, {}) or {}
    if not pending.get('lines') or now - pending['since'] < WAKE_WINDOW_SECONDS:
        return None
    db.meta_set(conn, WAKE_KEY, {})  # cleared before firing: a crash must not fire the same batch twice
    return (firer or fire)('\n'.join(pending['lines']), len(pending['lines']))


def fire(text, count=1, token=None, opener=urllib.request.urlopen):
    """Fire 「汇报回复转发」 once. Returns 'ok' or a short reason; never raises, never retries."""
    try:
        cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cfg = {}
    trig = cfg.get('trigger_id')
    if cfg.get('wake') is False:  # the API may refuse the routine (e.g. 403);
        # threads re-check with reply-status instead, so no wake and no failure notice
        log('wake off in reply_watch.json (%d replies stay in the ledger; threads read them with reply-status)' % count)
        return 'off'
    token = token or _token()
    if not trig or not token:
        why = 'no trigger id in reply_watch.json' if not trig else (
            'wake is off: set [keys] wake_token in r2i.toml' if not _token_item() else 'no Keychain item %s' % _token_item())
        log('wake skipped (%d replies): %s' % (count, why))
        _notify('%d 条回复已进台账，但没能叫醒线程（%s）' % (count, '没有转发任务编号' if not trig else '钥匙串里没有 token'))
        return why
    req = urllib.request.Request(FIRE_URL % trig, data=json.dumps({'text': text}, ensure_ascii=False).encode('utf-8'),
                                 method='POST', headers={'Authorization': 'Bearer ' + token, 'anthropic-version': API_VERSION,
                                                         'anthropic-beta': API_BETA, 'Content-Type': 'application/json',
                                                         'User-Agent': 'r2i-reply-watch/1.0'})
    try:
        with opener(req, timeout=20) as r:
            status = getattr(r, 'status', 200)
    except urllib.error.HTTPError as e:
        status = e.code
        try:  # the server's reason, one line (it never echoes the token)
            why = ' '.join(e.read(300).decode('utf-8', 'replace').split()).replace(token, '***')
        except (OSError, ValueError):
            why = ''
        if why:
            log('wake HTTP %s body: %s' % (status, why))
    except (OSError, ValueError) as e:
        log('wake failed (%d replies): %s' % (count, type(e).__name__))
        _notify('%d 条回复已进台账，但没能叫醒线程' % count)
        return 'error ' + type(e).__name__
    if 200 <= status < 300:
        log('woke 汇报回复转发 with %d replies' % count)
        return 'ok'
    log('wake failed (%d replies): HTTP %s' % (count, status))
    _notify('%d 条回复已进台账，但没能叫醒线程' % count)
    return 'HTTP %s' % status


def _wait_for(conn, page, props):
    wid = props.get('等你回')
    if wid:
        row = conn.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM waits WHERE report=? AND typed IS NULL ORDER BY (status='等待') DESC, asked_at DESC LIMIT 1",
                        (page,)).fetchone()


DEC_UNDO_RE = re.compile(r'^[ \t]*[-*+] \[(.)\][ \t]*选 (.+?) · 取消勾选 = 撤回(?:[ \t]*✅[ \t]*\d{4}-\d{2}-\d{2})?[ \t]*$', re.M)


def _rel(vault, path):
    return Path(path).relative_to(vault).as_posix()


def _page_thread(props):
    """The page's own 线程 property when it is a thread id (the owner may tick before autowait opens the wait)."""
    t = (props.get('线程') or '').strip()
    return t if is_thread_id(t) else None


def _page_wait_thread(conn, page):
    """The thread of the page's own wait (autowait opens one from 问你), or ''."""
    w = conn.execute("SELECT thread FROM waits WHERE report=? AND typed IS NULL AND thread GLOB 'cc_*' ORDER BY asked_at DESC, id DESC",
                     (page,)).fetchone()
    return w['thread'] if w else ''


def _close_page_waits(conn, page, answer):
    """The decision page's wait closes with its answer (one wait, one place to close)."""
    conn.execute("UPDATE waits SET status='已回', closed_at=?, closed_by=?, answer=? WHERE report=? AND typed IS NULL AND status='等待'",
                 (db.now(), db.REPLY_BY, answer, page))
    conn.commit()


def _reopen_page_wait(conn, page):
    """The answer was withdrawn: the page asks again, so its wait opens again (a new row, the old one stays as history)."""
    w = conn.execute("SELECT * FROM waits WHERE report=? AND typed IS NULL AND src=? ORDER BY asked_at DESC, id DESC",
                     (page, '页面属性')).fetchone()
    if w and not conn.execute("SELECT 1 FROM waits WHERE report=? AND typed IS NULL AND status='等待'", (page,)).fetchone():
        from . import autowait
        autowait._new_wait(conn, thread=w['thread'], ask=w['ask'], editor=w['editor'], report=page, typed=None)


def handle_dec(conn, vault, path, mtime, text, reply, did):
    """A decision page 汇报/待决/DEC-NN. One ticked option answers it; 「改」 under a thread's own
    decision reopens it; an unticked receipt withdraws the answer. Each event is one report_replies row for the asking
    thread (reply-wait hands it over). Returns what happened, or None."""
    from . import dec_pages
    d = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
    if not d:
        return None
    rel = _rel(vault, path)
    at = datetime.now(db.LOCAL_TZ)
    if not reply:  # already received: an unticked receipt withdraws
        m = DEC_UNDO_RE.search(text)
        if m and m.group(1) == ' ' and d['status'] == '已决' and d['answered_via'] == '页' and db.reopen_decision(conn, did):
            thread = d['thread'] or _page_wait_thread(conn, path.stem)
            rid = db.add_home_reply(conn, did, thread, '撤回', '撤回了「%s」' % d['answer'], page=path.stem) if thread else None
            _reopen_page_wait(conn, path.stem)
            render.log_change(vault, EDITOR, rel, config.say('%s {owner} 取消勾选，撤回答复「%s」，待决重新打开') % (did, d['answer']))
            return {'id': rid, 'dec': did, 'withdrawn': True, 'rewritten': True}
        return None
    picked = [c for c in (reply['choice'] or '').split('、') if c]
    if d['status'] == '已决' and d['answered_via'] == '线程' and picked == [dec_pages.CHANGE]:
        was = d['answer']
        if not db.revert_acted(conn, did):
            return None
        what = config.say('改：{owner} 不同意「%s」，重新待决') % was
        rid = db.add_home_reply(conn, did, d['thread'], '改', what, page=path.stem) if d['thread'] else None
        receipt = '\n> [!done] 已收到 %s：改 · 待决重新打开，%s会回滚「%s」\n\n' % (at.strftime('%m-%d %H:%M'), d['acted_by'] or '线程', was)
    elif d['status'] == '待决' and not picked and reply['text']:
        # words without an option (e.g. a question back about the decision): recorded for the asking thread, the
        # decision stays open, and the page comes back with their words quoted and the options untouched
        rid = db.add_home_reply(conn, did, d['thread'], None, reply['text'], page=path.stem)
        render.log_change(vault, EDITOR, rel, config.say('收到 {owner} 在待决页的话（没选项）：%s「%s」') % (did, reply['text']))
        if path.stat().st_mtime != mtime:
            return {'id': rid, 'dec': did, 'said': True, 'rewritten': False}
        render._write(path, dec_pages.set_prop(dec_pages.open_text(d, dec_pages.said(conn, d, vault)), '更新', at.strftime('%Y-%m-%d %H:%M')))
        return {'id': rid, 'dec': did, 'said': True, 'rewritten': True}
    elif d['status'] == '待决':
        if len(picked) != 1 or picked[0] not in dec_pages.labels(d):
            log(config.say('%s: %s ticked, not one option; left for {owner}') % (rel, reply['choice']))
            return None
        if not db.close_decision(conn, did, '已决', picked[0], via='页'):
            return None
        thread = d['thread'] or _page_wait_thread(conn, path.stem)
        rid = db.add_home_reply(conn, did, thread, picked[0], reply['text'], page=path.stem) if thread else None
        _close_page_waits(conn, path.stem, '选 ' + picked[0])
        words = ' · 「%s」' % reply['text'] if reply['text'] else ''
        receipt = '\n> [!done] 已收到 %s：选 %s%s\n\n- [x] 选 %s · %s\n\n' % (at.strftime('%m-%d %H:%M'), picked[0], words,
                                                                      picked[0], dec_pages.UNDO)
    else:
        return None
    render.log_change(vault, EDITOR, rel, config.say('收到 {owner} 在待决页的答复：%s %s') % (did, '、'.join(picked)))
    if path.stat().st_mtime != mtime:
        return {'id': rid, 'dec': did, 'rewritten': False}
    m = _section(text)
    text = text[:m.start(1)] + receipt + text[m.end(1):]
    text = re.sub(r'^状态:.*$', '状态: 已回', text, count=1, flags=re.M)
    render._write(path, text)
    return {'id': rid, 'dec': did, 'rewritten': True}


SEEN_RE = re.compile(r'^[ \t]*[-*+] \[([xX])\][ \t]*已看', re.M)


def handle_seen(conn, vault, path, mtime, text, props):
    """A 同步 page: the owner ticked 「- [x] 已看」. One report_replies row (kind seen, no thread, nobody is woken),
    milestones.seen_at when the page carries 里程碑: MS-…, and the page gets 状态 已看 and 已看于 (archive.py archives it an
    hour later). Returns what happened, or None."""
    from . import archive, modes
    if db.report_done(props.get('状态')) or modes.get(conn, 'kinds') == 'off' or not SEEN_RE.search(text):
        return None
    ms = props.get('里程碑') or ''
    rid = db.add_seen(conn, path.stem, ms if ms.startswith('MS-') else None)
    if path.stat().st_mtime != mtime:  # the owner is still editing: the page is rewritten next round (the row exists once)
        return {'id': rid, 'seen': True, 'rewritten': False}
    at = datetime.now(db.LOCAL_TZ).strftime(archive.FMT)
    text = archive.set_prop(archive.set_prop(text, '状态', '已看'), '已看于', at)
    render._write(path, text)
    render.log_change(vault, EDITOR, _rel(vault, path), config.say('收到 {owner} 在同步页勾的「已看」%s') % ('，里程碑 %s 记为已看' % ms if ms.startswith('MS-') else ''))
    return {'id': rid, 'seen': True, 'rewritten': True}


def handle(conn, vault, path, mtime, waker=None):
    """One settled page. Returns what happened, or None when the page holds no new answer."""
    text = path.read_text(encoding='utf-8')
    reply = parse(text)
    props0 = front(text)
    if props0.get('类别') == '同步' or (path.parent.name == '同步' and not props0.get('类别')):
        return handle_seen(conn, vault, path, mtime, text, props0)
    did = props0.get('待决')
    if did:
        return handle_dec(conn, vault, path, mtime, text, reply, did)
    if not reply:
        revoked = revoke_unticked(conn, vault, path, mtime, text)
        return {'revoked': revoked, 'rewritten': path.stat().st_mtime == mtime} if revoked else None
    reply = with_files(reply, vault, path, text)
    page = path.name[:-3]
    props = front(text)
    wait = _wait_for(conn, page, props)
    if reply.get('multi'):  # each question is recorded by itself; the page waits until all are answered (or 发出)
        rows = record_questions(conn, page, wait, reply, mtime)
        if not reply['complete']:
            if rows:
                log('%s 已答 %d/%d：%s' % (page, len(reply['answered']), reply['total'], '、'.join(rows)))
            return {'partial': '%d/%d' % (len(reply['answered']), reply['total']), 'rows': rows, 'rewritten': True}
    res = db.add_report_reply(conn, page=page, wait_id=wait['id'] if wait else None, choice=reply['choice'],
                              text=reply['text'], page_mtime=mtime, thread=(wait['thread'] if wait else None) or _page_thread(props))
    at = datetime.now(db.LOCAL_TZ)
    picked = (reply['choice'] or '').split('、')
    budget = None
    if APPROVE in picked and REJECT not in picked:
        prior = conn.execute('SELECT * FROM verbal_budgets WHERE id=?', (props.get('预算') or '',)).fetchone()
        if res['new']:
            budget = page_budget(conn, page, props, at)
        elif prior and prior['approval_msg'].startswith('%s%s ' % (db.PAGE_APPROVAL_PREFIX, page)):
            budget = {'id': prior['id'], 'cap': prior['cap_cny'], 'expires': db.parse_time(prior['expires_at'])
                      .astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M')}  # recorded last round, page not yet rewritten
    if budget:
        res['budget'] = budget
    # wake as soon as the answer is in the ledger (once: only for a new row), even if the page rewrite waits
    waker = waker or (lambda *a, **k: queue_wake(conn, *a, **k))
    res['woke'] = waker(page, wait, reply, budget=budget) if res['new'] else 'already woken'
    if path.stat().st_mtime != mtime:  # the owner is still editing: rewrite next round (the reply is recorded once)
        return dict(res, rewritten=False)
    render._write(path, received(text, reply, at, budget))
    render.log_change(vault, EDITOR, path.relative_to(vault).as_posix(),
                      config.say('收到 {owner} 在汇报页的回复 %s%s') % (res['id'], '，关掉等你回 ' + wait['id'] if res['closed'] else ''))
    res['rewritten'] = True
    return res


def home(conn, vault, seen, now=None):
    """首页 option cards: read like a report page, once 首页 has been still for 2 seconds. A decision
    that names its thread (dec-add --thread) wakes that thread through the same merged relay."""
    from . import home_ticks
    path = Path(vault) / home_ticks.PAGE
    now = now or time.time()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if seen.get(home_ticks.PAGE) == mtime or now - mtime < SETTLE_SECONDS:
        return []
    seen[home_ticks.PAGE] = mtime
    done = home_ticks.readback(conn, vault, log=log)
    for oid, what in done:
        log('首页: %s %s' % (oid, what))
        if oid.startswith('DEC-'):
            d = conn.execute('SELECT * FROM decisions WHERE id=?', (oid,)).fetchone()
            if d and d['thread']:
                title = d['title'] or d['question'][:25]
                if what.startswith('改：'):
                    choice = config.say('{owner} 勾了「改」：请回滚你按 R-10 做的事（%s），等 {owner} 在首页另选') % what[2:]
                else:
                    choice = ('选 ' + d['answer']) if d['status'] == '已决' else '撤回了刚才的答复'
                pending = db.meta_get(conn, WAKE_KEY, {}) or {}
                db.meta_set(conn, WAKE_KEY, {'since': pending.get('since') or time.time(), 'lines': pending.get('lines', []) + [
                    '｜'.join(['首页待决 %s %s' % (oid, ' '.join(title.split())), '—', d['thread'], choice, '—'])]})
    return done


AUDIT_DIR = '每日'
AUDIT_DAYS = 3  # daily notes of the last days are read for 「改」; older ticks are not acted on


def handle_daily(conn, vault, path, mtime):
    """The owner ticked 「改」 on a decision the audit made itself (a row `- [x] 改 · … ^aud-YYYYMMDD-NN` on 首页, or
    in a daily note). Per audit, once: undo_status -> 已勾改, one report_replies row and a queued wake for the audit thread
    (it undoes the decision), and one wait (WAIT-visible) that audit-undo-done closes. The note is the 07:00 agent's text:
    nothing is written to it, and a note touched since `mtime` is read again next round. Returns [audit ids]."""
    from . import audits
    text = path.read_text(encoding='utf-8')
    if path.stat().st_mtime != mtime:
        return None
    done = []
    for mark, num in audits.TICK_RE.findall(text):
        aid = 'AUD-' + num
        if mark not in 'xX':
            continue
        row = audits.mark_ticked(conn, aid)
        if not row:
            continue
        what = config.say('改：{owner} 不同意审计自己定的「%s」，请撤回') % row['decision']
        page = '每日审计 %s' % aid
        rid = db.add_home_reply(conn, aid, row['thread'], '改', what, page=page)  # before the wait: it must not count as its answer
        wid = db.add_wait(conn, row['thread'], (config.say('%s {owner} 勾了改：撤回「%s」后跑 audit-undo-done') % (aid, row['decision']))[:120],
                          row['editor'])
        conn.execute('UPDATE waits SET src=? WHERE id=?', (audits.SRC, wid)); conn.commit()
        queue_wake(conn, page, {'id': wid, 'thread': row['thread']}, {'choice': '改', 'text': what})
        render.log_change(vault, EDITOR, path.relative_to(vault).as_posix(), config.say('%s {owner} 勾了「改」，通知线程撤回，等你回 %s') % (aid, wid))
        log('%s: %s ticked 改 -> %s, wait %s' % (path.name, aid, rid, wid))
        done.append(aid)
    return done or None


def scan(conn, vault, seen, now=None):
    """One pass over 汇报/*.md and 首页. `seen` maps page file name -> the mtime already handled."""
    now = now or time.time()
    done = [('首页', {'home': x}) for x in home(conn, vault, seen, now)] if HOME_TICKS else []
    for path in db.report_pages(vault):  # 要批/, 同步/, the retired 待决/ and the 汇报/ root
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if seen.get(path.name) == mtime or now - mtime < SETTLE_SECONDS:
            continue
        try:
            res = handle(conn, vault, path, mtime)
        except (OSError, UnicodeDecodeError, db.HSError) as e:
            log('could not read %s: %s' % (path.name, e))
            res = None
        seen[path.name] = path.stat().st_mtime if path.exists() else mtime
        if res and not res.get('rewritten'):
            seen.pop(path.name, None)
        if res:
            log('%s: %s' % (path.name, json.dumps({k: v for k, v in res.items() if k != 'answer'}, ensure_ascii=False)))
            done.append((path.name, res))
    sources = [(Path(vault) / render.HOME, '首页.md#aud', '首页.md')]  # 「改」 ticks on 首页, and in the daily notes
    sources += [(p, '每日/' + p.name, '每日/' + p.name) for p in sorted((Path(vault) / AUDIT_DIR).glob('*.md'))[-AUDIT_DAYS:]]
    for path, key, name in sources:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if seen.get(key) == mtime or now - mtime < SETTLE_SECONDS:
            continue
        try:
            res = handle_daily(conn, vault, path, mtime)
        except (OSError, UnicodeDecodeError, db.HSError) as e:
            log('could not read %s: %s' % (path.name, e))
            res = None
        if res is not None or path.stat().st_mtime == mtime:  # a note changed under us is read again
            seen[key] = mtime
        if res:
            log('%s: %s' % (name, json.dumps(res, ensure_ascii=False)))
            done.append((name, {'audits': res}))
    return done


def beat(vault):
    render._write(Path(vault) / render.REPLY_HEARTBEAT, datetime.now(db.LOCAL_TZ).isoformat(timespec='seconds') + '\n')


def main(argv=None):
    p = argparse.ArgumentParser(prog='reply_watch')
    p.add_argument('--vault', required=True)
    p.add_argument('--db', default=None)
    p.add_argument('--once', action='store_true', help='one pass, then exit')
    args = p.parse_args(argv)
    conn = db.connect(args.db)
    seen, last_beat = {}, 0.0
    log('started (pid %s)' % __import__('os').getpid())
    while True:
        try:
            scan(conn, args.vault, seen)
            flush_pushes(conn)
            notify.flush(conn)
            flush_wakes(conn)
        except Exception as e:  # keep watching; launchd would restart us anyway
            log('scan error: %s: %s' % (type(e).__name__, e))
        if time.time() - last_beat >= HEARTBEAT_SECONDS:
            try:
                beat(args.vault)
            except OSError as e:
                log('heartbeat failed: %s' % e)
            last_beat = time.time()
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    sys.exit(main())
