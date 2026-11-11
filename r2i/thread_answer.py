"""thread-answer: the owner answered in the thread chat instead of ticking the report page. The agent records it so they never tick
the page too (every ask still gets a page).

  cli thread-answer --wait wait-… --answer '<their choice or a summary>' --owner-msg cc_… --thread cc_… [--done] [--queue]
  cli thread-answer --wait wait-… --thread cc_… --done        (later: the agent has carried the answer out)

Answer: one report_replies row (kind 'thread', already delivered: the thread has it), the wait closes (closed_by
thread-answer), and the page's reply section gets 「> [!done] 已在线程里批 …」 with 状态 已回, so 首页 stops listing it and
archive.py moves it the usual way. No checkbox is ever ticked. A multi-question page (1A 2B …) takes
`--answer "1A 2B"`: one 'question' row per question like reply_watch, 「> [!info] 已在线程里答 n/m …」 on the page, and
the wait closes only when every question has an answer (ticked or in the chat). --done: done_at on the row and
「> [!done] 已办完 MM-DD HH:MM」 under the receipt."""
import re
from datetime import datetime
from pathlib import Path

from . import archive, db, render, reply_watch
from .ids import is_msg_id
from . import config

EDITOR = '线程答复'
BY = 'thread-answer'
ANSWER_MAX = 300
SKIP_KINDS = ('seen', 'question', 'wake', 'audit')
RECEIPT_RE = re.compile(r'^> \[!done\] 已(?:在线程里批|收到)[^\n]*\n', re.M)


def check(wait, answer, owner_msg, done, thread):
    """The checks that need no database (also run at queue time). Returns (answer, owner_msg, thread), cleaned."""
    if not db.WAIT_ID_RE.match((wait or '').strip()):
        raise db.HSError('--wait must be a wait id (wait-YYYYMMDD-N)')
    if not answer and not done:
        raise db.HSError('give --answer and --owner-msg, or --done')
    thread = db.check_wait_thread(thread)
    if answer:
        answer = ' '.join(answer.split())
        if len(answer) > ANSWER_MAX:
            raise db.HSError('--answer: at most %d characters' % ANSWER_MAX)
        if not is_msg_id((owner_msg or '').strip()):
            raise db.HSError(config.say('--owner-msg: the uuid of the transcript message where {owner} answered; required with --answer'))
        owner_msg = owner_msg.strip()
    return answer or None, owner_msg, thread


def _find_page(vault, name):
    """The report page: live, else (archived) under 汇报/归档/. None when the wait names no page or it is gone."""
    if not (vault and name):
        return None
    return db.report_path(vault, name) or next(iter(sorted((Path(vault) / db.REPORT_DIR / archive.ARCHIVE).rglob(name + '.md'))), None)


def _earlier(conn, w):
    """The first answer to this wait: a row of its own, or one on its page after it was asked (a tick, an earlier thread-answer)."""
    return conn.execute("SELECT * FROM report_replies WHERE (wait_id=? OR (page=? AND received_at>?)) "
                        "AND COALESCE(kind,'') NOT IN (%s) ORDER BY received_at, id" % ','.join('?' * len(SKIP_KINDS)),
                        (w['id'], w['report'] or '\0', w['asked_at']) + SKIP_KINDS).fetchone()


def _options(sec):
    """{(question, letter): full label} of a multi-question reply section, labelled like reply_watch does."""
    out = {}
    for _, label, q in reply_watch._boxes(sec):
        if q and label != reply_watch.SEND:
            full = label if reply_watch.QKEY_RE.match(label) else q + label
            k = reply_watch.QKEY_RE.match(full)
            out[(k.group(1), k.group(2).upper())] = full
    return out


def _recorded(conn, w, sec):
    """Questions already answered, {number: label}: ticked (reply_watch's question rows) or in the chat (callouts on the page)."""
    out = {}
    for r in conn.execute("SELECT choice FROM report_replies WHERE page=? AND COALESCE(wait_id,'')=? AND kind='question' ORDER BY id",
                          (w['report'], w['id'])):
        k = reply_watch.QKEY_RE.match(r['choice'] or '')
        if k:
            out[k.group(1)] = r['choice']
    for q, label in reply_watch.thread_answered(sec).items():
        out.setdefault(q, label)
    return out


def _plan(conn, vault, w, answer):
    """What --answer does, checked before anything is written. 'total' is 0 on a page with fewer than two questions (the
    answer is one free text) and when there is no page; else 'new' {q: label} are this call's questions, 'all' every answered one."""
    path = _find_page(vault, w['report'])
    plan = {'path': path, 'new': {}, 'all': {}, 'total': 0}
    if not path:
        return plan
    text = path.read_text(encoding='utf-8')
    if reply_watch.front(text).get('待决'):
        raise db.HSError('wait %s 属于待决页「%s」：用页面选项或 dec-act 答，thread-answer 不改待决' % (w['id'], w['report']))
    m = reply_watch._section(text)
    sec = m.group(1) if m else ''
    opts = _options(sec)
    qs = {q for q, _ in opts}
    if len(qs) < 2:
        return plan
    have = _recorded(conn, w, sec)
    for m in reply_watch.Q_TOKEN_RE.finditer(answer):
        q, letter = m.group(1), m.group(2).upper()
        if (q, letter) not in opts:
            raise db.HSError('page 「%s」 has no option %s%s' % (w['report'], q, letter))
        if q in have or q in plan['new']:
            raise db.HSError('question %s of 「%s」 is already answered (%s)' % (q, w['report'], have.get(q) or plan['new'][q]))
        plan['new'][q] = opts[(q, letter)]
    if not plan['new']:
        raise db.HSError('page 「%s」 has %d questions: --answer like "1A 2B" (question number + option letter)' % (w['report'], len(qs)))
    plan['all'] = dict(have, **plan['new'])
    plan['total'] = len(qs)
    return plan


def _put(text, line):
    """`line` at the top of the reply section (the checkboxes below it stay as they are); a page without one gets a new 「## 回复」."""
    m = reply_watch._section(text)
    if not m:
        return text.rstrip('\n') + '\n\n## 回复\n\n' + line + '\n'
    return text[:m.start(1)] + '\n' + line + '\n\n' + text[m.start(1):].lstrip('\n')


def _stamp():
    return datetime.now(db.LOCAL_TZ).strftime('%m-%d %H:%M')


def apply(conn, vault, wait, answer=None, owner_msg=None, done=False, thread=None):
    """Returns what was written; raises HSError (nothing written) on any guard."""
    answer, owner_msg, thread = check(wait, answer, owner_msg, done, thread)
    if answer:
        db.check_owner_msg(conn, owner_msg)
    w = conn.execute('SELECT * FROM waits WHERE id=?', (wait,)).fetchone()
    if not w:
        raise db.HSError('no wait %s' % wait)
    owner = (w['thread'] or '').strip()
    if owner and owner != thread:
        raise db.HSError('wait %s 登记在线程 %s，用 --thread %s，或先 wait-move' % (wait, owner, owner))
    plan, complete, out = None, True, {'wait': wait}
    if answer:
        earlier = _earlier(conn, w)
        if w['status'] != '等待' or earlier:
            raise db.HSError('wait %s 已经答过：%s' % (wait, '回复 %s（%s，%s）' % (earlier['id'], earlier['choice'] or earlier['text'], earlier['received_at'])
                                                      if earlier else '状态 %s，由 %s 关掉' % (w['status'], w['closed_by'])))
        plan = _plan(conn, vault, w, answer)
        complete = not plan['total'] or len(plan['all']) >= plan['total']
        if done and not complete:
            raise db.HSError('--done 要等所有问题都答完；现在 %d/%d' % (len(plan['all']), plan['total']))
    elif w['status'] != '已回':
        raise db.HSError('wait %s 还没答（状态 %s）：先 --answer' % (wait, w['status']))
    at = db.now()
    if answer:
        out['id'], out['complete'], out['page'] = _record(conn, w, plan, answer, owner_msg, thread, at, complete), complete, w['report']
        if plan['total']:
            out['answered'] = '%d/%d' % (len(plan['all']), plan['total'])
    finish = bool(done) and complete
    if finish:
        row = conn.execute("SELECT * FROM report_replies WHERE wait_id=? AND kind='thread'", (wait,)).fetchone() or _earlier(conn, w)
        if row and row['done_at'] and not answer:
            return dict(out, done='already', done_at=row['done_at'])
        if row:
            conn.execute('UPDATE report_replies SET done_at=? WHERE id=?', (at, row['id']))
            conn.commit()
        out['done'] = at
    _edit_page(vault, w, plan, answer, owner_msg, finish, out)
    return out


def _record(conn, w, plan, answer, owner_msg, thread, at, complete):
    """The rows and the wait. Returns the id of the row that carries the answer (the last question row while partial)."""
    rid = None
    for q in sorted(plan['new'], key=int):
        rid = db.add_question_reply(conn, page=w['report'], wait_id=w['id'], choice=plan['new'][q], page_mtime=None) or rid
        conn.execute('UPDATE report_replies SET owner_msg=? WHERE id=?', (owner_msg, rid))
    if complete:
        rid = db._next_id(conn, 'report_replies', 'RR-%s-' % datetime.now(db.LOCAL_TZ).strftime('%Y%m%d'))
        choice = ' '.join(plan['all'][q].split()[0] for q in sorted(plan['all'], key=int)) if plan['total'] else answer
        conn.execute("INSERT INTO report_replies(id,page,wait_id,choice,text,page_mtime,received_at,thread,delivered_at,kind,owner_msg) "
                     "VALUES (?,?,?,?,?,NULL,?,?,?,'thread',?)",
                     (rid, w['report'] or '线程答复 ' + w['id'], w['id'], choice, answer, at, thread, at, owner_msg))
        conn.execute("UPDATE waits SET status='已回', closed_at=?, closed_by=?, answer=?, thread=? WHERE id=? AND status='等待'",
                     (at, BY, choice, thread, w['id']))
    conn.commit()
    return rid


def _edit_page(vault, w, plan, answer, owner_msg, done, out):
    """Write the callouts and 状态 into the page, once. A typed wait leaves its page alone (a tick never closes those; the
    thread writes its own 「收到你打字」 update line, as after wait-done)."""
    if w['typed']:
        out['page_edited'] = False
        return
    path = plan['path'] if plan else _find_page(vault, w['report'])
    if not path:
        out['page_missing'] = bool(w['report'])
        return
    text, stamp = path.read_text(encoding='utf-8'), _stamp()
    if answer and plan['total'] and len(plan['all']) < plan['total']:
        text = _put(text, '> [!info] 已在线程里答 %d/%d：%s（消息 %s）' % (
            len(plan['all']), plan['total'], ' '.join(plan['new'][q].split()[0] for q in sorted(plan['new'], key=int)), owner_msg))
    elif answer:
        text = _put(archive.set_prop(text, '状态', '已回'), '> [!done] 已在线程里批 %s：%s（消息 %s）' % (stamp, answer, owner_msg))
    if done:
        m = reply_watch._section(text)
        r = RECEIPT_RE.search(text, m.start(1)) if m else None
        if r:
            rest = text[r.end():]
            text = text[:r.end()] + '\n> [!done] 已办完 %s\n' % stamp + ('' if rest.startswith('\n') else '\n') + rest
        else:
            text = _put(text, '> [!done] 已办完 %s' % stamp)
    render._write(path, text)
    render.log_change(vault, EDITOR, path.relative_to(vault).as_posix(),
                      '%s %s' % (w['id'], '办完' if done and not answer else config.say('线程里收到 {owner} 的答复') + ('并办完' if done else '')))
    out['page_edited'] = True
