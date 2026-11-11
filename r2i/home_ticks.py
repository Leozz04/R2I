"""首页「要你定」cards: one checkbox per option, read back here.

A card is a plain list item (no checkbox, so Tasks and 待办 never list it twice) with one child checkbox per option:

    - **DEC-02：方法 X 要不要启用？** · 甲组 ^dec-02
        - [ ] 要 ^dec-02-a
        - [ ] 不要 ^dec-02-b

        > [!note]- 选项和依据
        > ...

Ticking one option answers (decision 已决 with that option; night item 批 = 已批, 不批 = 撤回). Ticking two is not read.
After the answer the card shows only the chosen option, ticked, with 「取消勾选 = 撤回」: unticking it withdraws the
answer (the decision opens again; the night budget is withdrawn). Each thing has exactly this one tickable place.
A decision a thread settled itself (dec-act, R-10) shows one box 「改 · 已按「X」做（线程）· 不同意就勾」: ticking it opens
the question again and wakes the thread to roll back.
reply_watch reads these every 2 seconds; render reads them too, before it rewrites 首页 (a fallback, idempotent)."""
import re
from datetime import datetime
from pathlib import Path

from . import db
from . import config

PAGE = '首页.md'
UNDO = '取消勾选 = 撤回'
OPT_RE = re.compile(r'^[ \t]+[-*+] \[(.)\][ \t]*(.*?)[ \t]*\^(dec-\d+|nq-\d{8}-\d+|wt-\d{8}-\d+)(?:-([a-z]))?[ \t]*$', re.M)
EDITOR = '首页勾选'
YES_NO = (('a', '同意'), ('b', '不同意'))
NIGHT = (('y', '批'), ('n', '不批'))


def option_labels(d):
    """[(key, label)] for a decision: its options' 「做什么」 part, or 同意/不同意 when it has none (at most 26)."""
    from .render import _short
    opts = [o.strip() for o in (d['options'] or '').splitlines() if o.strip()]
    if not opts:
        return list(YES_NO)
    labels = [_short(re.split(r'[：:]', re.sub(r'^[-*+]\s+(?:\[.\]\s*)?', '', o), 1)[0], 20) for o in opts]
    return [(chr(ord('a') + i), l) for i, l in enumerate(labels[:26])]


def _key_of(d):
    for k, l in option_labels(d):
        if l == (d['answer'] or ''):
            return k
    return 'a'


def _box(mark, label, ref, key):
    from .render import _md
    return '    - [%s] %s ^%s-%s' % (mark, _md(label).replace('[', '［').replace(']', '］'), ref, key)


def dec_ref(d):
    return 'dec-' + d['id'].split('-', 1)[1]


def nq_ref(r):
    return 'nq-' + r['id'].split('-', 1)[1]


def dec_card(d, head):
    return [head + ' ^' + dec_ref(d)] + [_box(' ', l, dec_ref(d), k) for k, l in option_labels(d)]


def dec_received(d, head):
    at = db.parse_time(d['decided_at']).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M')
    return [head + ' ^' + dec_ref(d), _box('x', '%s · 已收到 %s · %s' % (d['answer'], at, UNDO), dec_ref(d), _key_of(d))]


CHANGE = 'z'


def dec_acted(d, head):
    """A decision a thread settled itself (R-10): one 改 box, ticked = the owner disagrees, roll back and ask them again."""
    return [head + ' ^' + dec_ref(d), _box(' ', '改 · 已按「%s」做（%s）· 不同意就勾' % (d['answer'], d['acted_by'] or '线程'),
                                           dec_ref(d), CHANGE)]


def nq_card(r, head):
    return [head + ' ^' + nq_ref(r)] + [_box(' ', l, nq_ref(r), k) for k, l in NIGHT]


def nq_received(r, head):
    at = db.parse_time(r['decided_at']).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M') if r['decided_at'] else ''
    return [head + ' ^' + nq_ref(r), _box('x', '批 · 已批准 %s · %s' % (at, UNDO), nq_ref(r), 'y')]


def parse(text):
    """{ref: {key: (ticked, is_receipt)}} for every option line on the page."""
    out = {}
    for mark, label, ref, key in OPT_RE.findall(text):
        out.setdefault(ref, {})[key] = (mark in 'xX', UNDO in label)
    return out


def readback(conn, vault, busy=None, log=None):
    """Apply the option ticks on 首页. Returns [(id, what)]; every change gets a changelog line."""
    from .render import log_change
    path = Path(vault) / PAGE
    if not path.exists() or (busy and busy(path)):
        return []
    done = []
    for ref, opts in parse(path.read_text(encoding='utf-8')).items():
        ticked = [k for k, (t, receipt) in opts.items() if t and not receipt]
        receipts = {k: t for k, (t, receipt) in opts.items() if receipt}
        if ref.startswith('wt-'):  # a 打字批 row's box: ticking = replying its typed words
            wid = 'wait-' + ref[3:]
            w = conn.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchone()
            if w and w['status'] == '等待' and any(t and not receipt for t, receipt in opts.values()):
                if db.close_wait(conn, wid=wid, status='已勾', by=EDITOR):
                    done.append((wid, '已勾：回「%s」' % w['typed']))
                    if w['thread']:
                        db.add_home_reply(conn, wid, w['thread'], w['typed'], '首页勾了「%s」' % w['typed'], page='首页勾选 ' + wid)
            continue
        if ref.startswith('dec-'):
            did = 'DEC-' + ref[4:]
            d = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
            if not d:
                continue
            if d['status'] == '待决' and len(ticked) == 1:
                label = dict(option_labels(d)).get(ticked[0])
                if label and db.close_decision(conn, did, '已决', label, via='首页'):
                    done.append((did, '已决：' + label))
            elif d['status'] == '待决' and len(ticked) > 1 and log:
                log('%s: %d options ticked on 首页, not read' % (did, len(ticked)))
            elif d['status'] == '已决' and d['answered_via'] == '线程' and opts.get(CHANGE, (False,))[0]:
                was = d['answer']
                if db.revert_acted(conn, did):
                    done.append((did, config.say('改：{owner} 不同意「%s」，重新待决') % was))
            elif d['status'] == '已决' and d['answered_via'] == '首页' and receipts and not any(receipts.values()):
                if db.reopen_decision(conn, did):
                    done.append((did, '撤回答复（首页取消勾选）'))
        else:
            nid = 'NQ-' + ref[3:]
            r = conn.execute('SELECT * FROM night_items WHERE id=?', (nid,)).fetchone()
            if not r:
                continue
            if r['status'] == '待批' and len(ticked) == 1:
                status = '已批' if ticked[0] == 'y' else '撤回'
                if db.close_night_item(conn, nid, status):
                    done.append((nid, status))
            elif r['status'] == '待批' and len(ticked) > 1 and log:
                log('%s: both 批 and 不批 ticked on 首页, not read' % nid)
            elif r['status'] == '已批' and receipts and not any(receipts.values()):
                if db.close_night_item(conn, nid, '撤回'):
                    done.append((nid, '撤回（首页取消勾选）'))
    for oid, what in done:
        log_change(vault, EDITOR, PAGE, '%s %s' % (oid, what))
        if oid.startswith('DEC-'):  # the asking thread's reply-wait picks this up
            d = conn.execute('SELECT * FROM decisions WHERE id=?', (oid,)).fetchone()
            if d and d['thread']:
                choice = d['answer'] if d['status'] == '已决' else ('改' if what.startswith('改：') else '撤回')
                db.add_home_reply(conn, oid, d['thread'], choice, what if choice in ('改', '撤回') else None)
    return done
