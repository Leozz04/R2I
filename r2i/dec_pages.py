"""One page per open decision. It is an ordinary report page.

dec-add no longer writes 汇报/待决/DEC-NN.md. A small decision DEC-NN gets one page 汇报/YYYY-MM-DD <白话标题>.md, written
here by render in the shape of agent/模板/汇报模板.md: 类型 要你定, the question, the options with their reasons, a 「## 回复」 section
with one checkbox per option, and the property 问你, so autowait opens the one wait that 首页 and the page both follow.
It keeps the property 待决: DEC-NN, so reply_watch reads one ticked option as the answer (unticking the receipt withdraws
it, the wait opens again). Old pages under 汇报/待决/ stay where they are, are still kept up to date and are archived as
before. The page is the script's: render rewrites it while the decision is open, never while the owner is editing it or while it
holds a tick reply_watch has not read yet. A decision closed elsewhere turns its page 已结 and closes its wait; archive.py
moves the page to 汇报/归档/ like any finished report page. A decision a thread settles itself (dec-act, R-10) opens no
page any more (it goes to the audit record); only an old page that already exists keeps its 「改」 for 3 days."""
import re
from datetime import datetime
from pathlib import Path

from . import db
from .ids import is_thread_id
from . import config
from .archive import set_prop  # noqa: F401  (reply_watch uses dec_pages.set_prop)

DIR = db.REPORT_DIR + '/待决'  # where the old DEC-NN pages are; new pages are in db.REPORT_DIR itself
PROP = '待决'
UNDO = '取消勾选 = 撤回'
CHANGE = '改'
ACTED_DAYS = 3
EDITOR = 'r2i-sync 待决页'
BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')


def _title(d):
    from .render import _short
    return d['title'] or _short(' '.join((d['question'] or '').split()), 25)


def page_name(d):
    """The name a new page is given: 「YYYY-MM-DD 白话标题」 (the date is the day the decision was asked, local)."""
    return '%s %s' % (_local(d['created_at'])[:10], BAD.sub(' ', _title(d)).strip())


def link(d, label='去定', vault=None):
    """Link to the decision's page: where it is when the vault is known (old 待决/ page or a new one), else the new name."""
    found = locate(vault, d['id']) if vault else []
    if found:
        return '[[%s|%s]]' % (found[0].relative_to(vault).with_suffix('').as_posix(), label)
    return '[[%s|%s]]' % (db.report_rel(vault, page_name(d)), label)  # page without the 待决 property: by its name; else 汇报/<name>


def labels(d):
    from .home_ticks import option_labels
    return [l for _, l in option_labels(d)]


def _local(ts):
    return db.parse_time(ts).astimezone(db.LOCAL_TZ).strftime('%Y-%m-%d %H:%M')


def _head(d, status):
    from .render import _md
    out = ['---', 'cssclasses: [research]', '类型: 要你定', '主线: ' + d['mainline'], '状态: ' + status,
           '日期: ' + _local(d['created_at']), '来自: ' + writer(d)]
    if is_thread_id(d['thread'] or ''):
        out.append('线程: ' + d['thread'])
    return out + ['问你: ' + _md(_title(d)), '%s: %s' % (PROP, d['id']), '类别: 要批', '---', '']  # 类别 last: archive.py's 更新 follows it


def _details(d):
    from .render import _md
    out = ['## 详情', '', '- 原文：' + _md(' '.join((d['question'] or '').split()))]
    if d['source']:
        out.append('- 依据：' + _md(d['source']))
    return out + ['', config.say('<!-- 本页由脚本按台账 %s 生成，不要手改；{owner} 在「回复」里勾。Claude 不勾。 -->') % d['id'], '']


def writer(d):
    ml = {m.id: m.short + ' 推进' for m in config.get().mainlines}.get(d['mainline'])
    return d['acted_by'] or ml or '提出线程'


def _reply(d):
    from .render import _md
    out = ['## 回复', '']
    out += ['- [ ] %s' % _md(l).replace('[', '［').replace(']', '］') for l in labels(d)]
    return out + ['', '回复：', '', '- [ ] 发出', '']


def said(conn, d, vault=None):
    """the owner's words on this decision's page without a ticked option (a question, a remark): [(local time, text)]."""
    names = {page_name(d)} | {p.stem for p in find(vault, d['id'])} if vault else {page_name(d)}
    rows = conn.execute("SELECT received_at, text FROM report_replies WHERE (page IN (%s) OR page=? OR page LIKE ?) AND choice IS NULL "
                        "AND text IS NOT NULL ORDER BY received_at" % ','.join('?' * len(names)),
                        (*names, d['id'], d['id'] + ' %')).fetchall()
    return [(db.parse_time(r[0]).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M'), r[1]) for r in rows]


def _said_block(d, words):
    from .render import _md
    if not words:
        return []
    out = ['> [!quote] 你在这页说过（已记下，%s把正文写清楚时回答；选项照样能勾）' % ('已转给「%s」，等它' % _md(writer(d)) if d['thread'] else '请「%s」' % _md(writer(d)))]
    out += ['> - %s：%s' % (t, _md(' '.join(w.split()))) for t, w in words]
    return out + ['']


def open_text(d, words=()):
    from .render import _md, _short
    detail = d['detail'] if 'detail' in d.keys() else None
    if detail:  # the asking thread's body; the script keeps title, properties and 回复
        out = _head(d, '待你看') + ['# %s' % _md(_title(d)), ''] + _said_block(d, words) + [detail.strip(), ''] + _reply(d)
        return '\n'.join(out + [config.say('<!-- 正文由提出线程写（dec-detail），标题、属性和「回复」由脚本写；{owner} 在「回复」里勾。Claude 不勾。 -->'), ''])
    opts = [re.sub(r'^[-*+]\s+(?:\[.\]\s*)?', '', o.strip()) for o in (d['options'] or '').splitlines() if o.strip()]
    out = _head(d, '待你看') + ['# %s' % _md(_title(d)), '', '> [!summary] 结论',
                                '> **%s**' % _md(_short(' '.join((d['question'] or '').split()), 60)), '',
                                '> [!warning] 这条还没写清楚，已请「%s」补。' % _md(writer(d)), ''] + _said_block(d, words) + ['## 要你定', '',
                                '> [!owner] %s' % _md(_title(d))]
    out += ['> - %s' % _md(o) for o in opts] or ['> - 同意 / 不同意']
    out += ['>', '> 在下面「回复」里勾一个就行；勾了以后可以取消勾选撤回。', ''] + _reply(d)
    return '\n'.join(out + _details(d))


def acted_text(d):
    from .render import _md
    who = d['acted_by'] or '线程'
    out = _head(d, '待你看') + ['# %s' % _md(_title(d)), '', '> [!summary] 结论',
                                '> **已按「%s」做（%s）。**' % (_md(d['answer']), _md(who)), '', '## 要你定', '',
                                '> [!owner] 不同意就勾「改」', '> 这件事不花钱、能撤回，线程按 R-10 先做了。勾「改」：待决重新打开，'
                                '线程回滚，你再选。', '', '## 回复', '', '- [ ] %s' % CHANGE, '']
    return '\n'.join(out + _details(d))


def closed_text(text, d):
    """Decided elsewhere: 状态 已结 (archive moves it next run) and one line saying so."""
    text = re.sub(r'^状态:.*$', '状态: 已结', text, count=1, flags=re.M)
    if '## 更新' in text:
        return text
    from .kinds import _add_updates  # v15: newest on top, under the summary
    return _add_updates(text, ['- %s 已在别处定了：%s' % (datetime.now(db.LOCAL_TZ).strftime('%m-%d %H:%M'), d['answer'] or d['status'])])


def pages(vault):
    """Every decision page: [(path, 待决 value)], the old ones under 汇报/待决/ and the new ones in 汇报/."""
    from .reply_watch import front
    out = []
    for path in db.report_pages(vault):
        try:
            did = front(path.read_text(encoding='utf-8')).get(PROP)
        except (OSError, UnicodeDecodeError):
            continue
        if did:
            out.append((path, did))
    return out


def new_dir(conn):
    """Where a new decision page is written: 汇报/要批/ when mode 'kinds' is auto, else the 汇报/ root."""
    from . import modes
    return db.REPORT_DIR + '/要批' if modes.get(conn, 'kinds') == 'auto' else db.REPORT_DIR


def find(vault, did):
    return [p for p, i in pages(vault) if i == did]


def locate(vault, did):
    """Where the decision's page is now: the live page (汇报/待决/ or 汇报/) first, else the archived one
    (汇报/归档/YYYY-MM/[待决/]), so a 撤回 link still opens it after archive.py moved it."""
    from .reply_watch import front
    live = find(vault, did)
    if live:
        return live
    out = []
    arch = Path(vault) / db.REPORT_DIR / '归档'
    for path in sorted(arch.rglob('*.md')) if arch.is_dir() else []:
        try:
            if front(path.read_text(encoding='utf-8')).get(PROP) == did:
                out.append(path)
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _days_ago(n):
    from .render import _days_ago as f
    return f(n)


def wanted(conn, vault=None):
    """{id: (decision, text)} for every decision that should have a live page now. A decision a thread settled itself
    keeps its 「改」 page only when an old one already exists (none is opened now)."""
    out = {}
    for d in conn.execute("SELECT * FROM decisions WHERE status='待决' ORDER BY id").fetchall():
        out[d['id']] = (d, open_text(d, said(conn, d, vault)))
    for d in conn.execute("SELECT * FROM decisions WHERE status='已决' AND answered_via='线程' AND decided_at>=? ORDER BY id",
                          (_days_ago(ACTED_DAYS),)).fetchall():
        if vault and any(p.parent.name == '待决' for p in find(vault, d['id'])):
            out[d['id']] = (d, acted_text(d))
    return out


def _open_wait(conn, d, page, vault, log_change=None):
    """The wait that 问你 would open (same words, so autowait later sees it and opens no second one)."""
    from . import autowait
    from .render import _md
    ask = _md(_title(d)).strip()[:autowait.ASK_MAX]
    if conn.execute('SELECT 1 FROM waits WHERE report=? AND typed IS NULL', (page,)).fetchone():
        return
    thread = d['thread'] if is_thread_id(d['thread'] or '') else autowait.find_thread(conn, writer(d))
    wid = autowait._new_wait(conn, thread=thread, ask=ask, editor=writer(d), report=page, typed=None)
    if log_change:
        log_change(vault, autowait.EDITOR, db.report_rel(vault, page) + '.md', '待决页有「问你」，登记 %s' % wid)


def sync(conn, vault, busy=None, log_change=None):
    """Write the decision pages. Returns the relative paths written."""
    from .reply_watch import parse, front
    vault = Path(vault)
    busy = busy or (lambda p: False)
    written = []
    want = wanted(conn, vault)
    for did, (d, text) in want.items():
        old = find(vault, did)
        cur = old[0] if old else None
        if not cur and locate(vault, did):
            continue  # its page is in 汇报/归档/: archive.py moves it back (the decision is open again), no second page
        # a page never changes its name once written (its wait follows it). New pages go to 汇报/要批/ once the 'kinds'
        # automation is live (else the 汇报/ root, as before); a name taken by any live page or any 归档 month gets 「 · 2」.
        target = cur or vault / new_dir(conn) / (db.unique_stem(vault, page_name(d)) + '.md')
        if cur and (busy(cur) or _pending(cur, parse)):
            continue  # the owner is editing it, or reply_watch has not read their tick yet
        if cur:  # keep the 更新 property archive.py maintains, and compare without it
            from .archive import set_prop
            have = cur.read_text(encoding='utf-8')
            upd = front(have).get('更新')
            if upd:
                text = set_prop(text, '更新', upd)
            if have == text:
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        written.append(target.relative_to(vault).as_posix())
        if not cur and d['status'] == '待决':  # register its wait now: autowait skips a page written a moment ago (busy)
            _open_wait(conn, d, target.stem, vault, log_change)
    for path, did in pages(vault):
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if did in want or db.report_done(front(text).get('状态')) or busy(path) or _pending(path, parse):
            continue
        d = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
        if d:
            path.write_text(closed_text(text, d), encoding='utf-8')
            conn.execute("UPDATE waits SET status='已回', closed_at=?, closed_by='待决已在别处定' WHERE report=? AND status='等待'",
                         (db.now(), path.stem))
            conn.commit()
            rel = path.relative_to(vault).as_posix()
            written.append(rel)
            if log_change:
                log_change(vault, EDITOR, rel, '%s 已在别处定了，页改成已结，等你回关掉' % did)
    return written


def readback(conn, vault, busy=None):
    """Fallback for reply_watch (it reads these pages every 2 s): answers on decision pages the owner left alone for a minute.
    Same code path as reply_watch, so a page read twice is still one answer."""
    from . import reply_watch
    done = []
    for path, did in pages(vault):
        if busy and busy(path):
            continue
        try:
            text = path.read_text(encoding='utf-8')
            res = reply_watch.handle_dec(conn, vault, path, path.stat().st_mtime, text, reply_watch.parse(text), did)
            if res:
                done.append(did)
        except (OSError, UnicodeDecodeError, db.HSError):
            continue
    return done


def dirty(text):
    """the owner has started answering here: a ticked box or words after 「回复：」 in a reply section not yet received. Such a
    page is never rewritten, even when the words have no 「发出」 yet (half-written answers must survive)."""
    from .reply_watch import SECTION_RE, BOX_RE, TEXT_RE
    m = SECTION_RE.search(text)
    if not m or '[!done]' in m.group(1):
        return False
    sec = m.group(1)
    t = TEXT_RE.search(sec)
    return any(mark in 'xX' for mark, _ in BOX_RE.findall(sec)) or bool(t and t.group(1).strip())


def _pending(path, parse=None):
    try:
        return dirty(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError):
        return True
