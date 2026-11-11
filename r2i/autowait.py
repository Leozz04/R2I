"""report-page properties 问你 / 要你打 / 线程 open and close waits by themselves.

Runs inside render before the ⚠ scan. It reads 汇报/ (top level and 归档/, not the old 待决/ pages; a new decision page is a top-level page with 问你) and writes only the waits table
and the changelog: never a report page, never a tick, never the 回复 section. Busy and dirty pages are left alone.
Exception, follow_ledger: only the 状态 property of a page whose waits the ledger has all closed."""
from pathlib import Path
import re

from . import db, modes
from .ids import is_thread_id

SRC = '页面属性'
EDITOR = 'r2i-sync 脚本'
ASK_MAX = 120


def _pages(vault):
    folder = Path(vault) / db.REPORT_DIR
    if not folder.is_dir():
        return []
    # 要批/, 同步/ and the root, then every archive month; not the retired 待决/ (its pages keep dec_pages' own wait)
    return [p for p in db.report_pages(vault) if p.parent.name != db.RETIRED_DIR] + sorted((folder / '归档').rglob('*.md'))


def _finished(props):
    """状态 已结 or 已回, or an old 已看 / 已取代… (they count as 已结). 已回 counts too."""
    return db.report_done(props.get('状态'))


def _answered(conn, path, page):
    """No new 问你 wait on a page that is archived or already has a delivered reply."""
    return '归档' in path.parts or conn.execute(
        "SELECT 1 FROM report_replies WHERE page=? AND delivered_at IS NOT NULL AND delivered_at!='' LIMIT 1", (page,)).fetchone() is not None


def _ask_key(ask):
    return (ask or '').split('｜')[-1].strip()


def find_thread(conn, editor):
    """The thread id this editor used most recently in waits, or ''."""
    key = db._editor_key(editor)
    for r in conn.execute("SELECT thread, editor FROM waits WHERE thread GLOB 'cc_*' ORDER BY asked_at DESC, id DESC"):
        if db._editor_key(r['editor']) == key:
            return r['thread']
    return ''


def _new_wait(conn, *, thread, ask, editor, report, typed):
    at = db.now()
    wid = db._next_id(conn, 'waits', 'wait-' + at[:10].replace('-', '') + '-')
    conn.execute('INSERT INTO waits(id,thread,ask,editor,asked_at,report,typed,src) VALUES (?,?,?,?,?,?,?,?)',
                 (wid, thread or '', ask, editor, at, report, typed, SRC))
    conn.commit()
    return wid


def _close(conn, wid, status, by):
    conn.execute("UPDATE waits SET status=?, closed_at=?, closed_by=? WHERE id=? AND status='等待'", (status, db.now(), by, wid))
    conn.commit()


def follow_ledger(conn, vault, busy, log_change):
    """A live report page still 待你看 whose waits are all closed and at least one answered (wait-done, note-add,
    thread-answer, …) gets 状态 已结, so 要批 empties and archive moves it after QUIET. Returns rel paths."""
    from . import archive, dec_pages, reply_watch
    if modes.get(conn, 'autowait') != 'auto':
        return []
    out = []
    for path in db.report_pages(vault):
        try:
            if path.parent.name == db.RETIRED_DIR or busy(path):
                continue
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if dec_pages.dirty(text) or _finished(reply_watch.front(text)):
            continue
        ws = [r[0] for r in conn.execute('SELECT status FROM waits WHERE report=?', (path.stem,))]
        if not ws or '等待' in ws or not ({'已回', '已勾'} & set(ws)):
            continue
        tmp = path.with_name('.' + path.name + '.tmp')
        tmp.write_text(archive.set_prop(text, '状态', '已结'), encoding='utf-8')
        tmp.replace(path)
        rel = path.relative_to(vault).as_posix()
        out.append(rel)
        log_change(vault, EDITOR, rel, '台账里这页的等待都关了，页改成已结（归档按原规则）')
    return out


def sync(conn, vault, busy, log_change):
    """Returns {'opened': [wait ids], 'closed': [ids], 'withdrawn': [ids]}."""
    from . import dec_pages, reply_watch, unregistered
    out = {'opened': [], 'closed': [], 'withdrawn': [], 'would': []}
    mode = modes.get(conn, 'autowait')  # off = does not run; observe = writes no wait, logs 「本来会做」
    if mode == 'off':
        return out
    obs = mode == 'observe'
    new_wait = _new_wait
    close = _close
    if obs:
        def new_wait(conn, **k):
            return 'wait-观察'

        def close(conn, wid, status, by):
            return None

        def log_change(vault, editor, rel, text, _real=log_change):
            if modes.would(conn, vault, _real, 'autowait', rel, text):
                out['would'].append(rel)
    for path in _pages(vault):
        try:
            if busy(path):
                continue
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if dec_pages.dirty(text):
            continue
        props = reply_watch.front(text)
        page = path.stem
        rel = path.relative_to(vault).as_posix()
        ask = (props.get('问你') or '').strip()[:ASK_MAX]
        typed = ' '.join((props.get('要你打') or '').split())
        try:
            typed = db.check_typed(typed) if typed else None
        except db.HSError:
            typed = None
        mine = conn.execute('SELECT * FROM waits WHERE report=?', (page,)).fetchall()
        editor = props.get('来自') or SRC
        thread = (props.get('线程') or '').strip()
        if not is_thread_id(thread):
            thread = find_thread(conn, editor)
        # withdraw: a wait this mechanism opened whose property is gone or changed
        for w in mine:
            if w['status'] != '等待' or w['src'] != SRC:
                continue
            gone = (w['typed'] is not None and w['typed'] != typed) or (w['typed'] is None and _ask_key(w['ask']) != ask)
            if gone:
                close(conn, w['id'], '过期', '撤回')
                if not obs:
                    out['withdrawn'].append(w['id'])
                log_change(vault, EDITOR, rel, '页上的「%s」改了或删了，撤回 %s' % ('要你打' if w['typed'] is not None else '问你', w['id']))
        mine = conn.execute('SELECT * FROM waits WHERE report=?', (page,)).fetchall()
        # open: 问你 (page not 已结) and 要你打 (any state), once per page and wording, whatever became of the old one
        if ask and not _finished(props) and not _answered(conn, path, page) and not any(w['typed'] is None and _ask_key(w['ask']) == ask for w in mine):
            wid = new_wait(conn, thread=thread, ask=ask, editor=editor, report=page, typed=None)
            if not obs:
                out['opened'].append(wid)
            log_change(vault, EDITOR, rel, '页上有「问你」，登记 %s%s' % (wid, '' if thread else '（页上没写线程，也找不到）'))
        if typed and not any(w['typed'] == typed for w in mine):
            title = (re.search(r'(?m)^# (.+)$', text) or [None, page])[1]
            wid = new_wait(conn, thread=thread, ask=ask or title[:ASK_MAX], editor=editor, report=page, typed=typed)
            if not obs:
                out['opened'].append(wid)
            log_change(vault, EDITOR, rel, '页上有「要你打」，登记 %s（回「%s」）' % (wid, typed))
        # close: 要你打 once the 更新 section says it was received (wait-done also closes it; a tick on the page does not)
        if typed:
            sections = unregistered.update_sections(text)
            done = set(unregistered.RECEIVED_RE.findall('\n'.join(head + '\n' + body for head, body in sections)))
            if typed in done:
                for w in conn.execute("SELECT * FROM waits WHERE report=? AND typed=? AND status='等待'", (page, typed)).fetchall():
                    close(conn, w['id'], '已回', '页面更新')
                    if not obs:
                        out['closed'].append(w['id'])
                    log_change(vault, EDITOR, rel, '更新里写了收到你打字「%s」，关掉 %s' % (typed, w['id']))
    return out
