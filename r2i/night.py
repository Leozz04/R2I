"""Night queue: page 今晚可跑.md, the user's ticks read back, and the NQ- budgets they turn into.

A thread adds an item (task, CNY cap, stop condition, deadline = next 09:00 local). the user ticks it on a phone; the
next host render marks it 已批 and writes one budget `NQ-…` into BUDGETS.json, the file paid_run.py reads. Unticking
an approved item withdraws it: its budget expires at once and the watchdog stops a run already going. Only this
module writes NQ- budgets, and only from a tick.

R-21: one unified night budget per night, NIGHT-YYYYMMDD (the local date of that night), written by
the first render after 00:00 local and expiring at 09:00; unused money lapses. Its amount is NIGHT_CAP_CNY, the
fixed figure the user chose; change it only on the user's words. Only this module writes NIGHT- budgets.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import re

from . import db
from . import config

PAGE = '今晚可跑.md'
HISTORY = 'agent/管理/今晚可跑-历史.md'  # 口头批准的预算 (render) and 巡检记录 (watchdog) live here; 今晚可跑 keeps tonight's items
HISTORY_LINK = '历史在 [[今晚可跑-历史]]（口头批准的预算、巡检记录、夜里的花费）。\n\n'
AGENT_HEAD = '---\ncssclasses: [wide, reader-agent]\n读者: agent\n---\n\n'  # faint in the sidebar
BUDGETS = Path(os.environ.get('R2I_BUDGETS') or config.paid_dir() / 'BUDGETS.json')
NQ_RE = re.compile(r'^[ \t]*- \[(.)\][ \t]*(.*?)[ \t]*\^nq-(\d{8}-\d+)[ \t]*$', re.M)
RECENT_DAYS = 3
VERBAL_DAYS = 7


def _night_hours():
    return config.get().quiet_hours  # the night is the quiet window


def _approval_cell(r):
    """the owner's words, or for a small-spend budget 「小额 · <audit id>」."""
    from .render import _cell
    if r['approval_msg'] == db.V4_MSG:
        return _cell(r['approval_quote'])
    return '%s「%s」' % (_local(r['approval_at']), _cell(r['approval_quote']))


def _verbal_state(r, now):
    if r['status'] == '撤回':
        return '撤回（%s）' % r['revoke_reason']
    if r['error']:
        return '没写入：' + r['error']
    if r['expires_at'] <= now:
        return '过期'
    return '已写入' if r['budget_written_at'] else '等写入（1 分钟内）'


def _local(ts):
    return db.parse_time(ts).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M')


def _line(r, mark):
    from .render import _md
    # approved here again (首页 only links to this page)
    return '- [%s] %s｜%s｜上限 ¥%g｜停止：%s｜截止 %s ^nq-%s' % (
        mark, db.MAINLINE_NAMES[r['mainline']], _md(r['task']), r['cap_cny'], _md(r['stop_if']),
        _local(r['deadline']), r['id'].split('-', 1)[1])


def readback(conn, vault, busy):
    """[x] on a waiting item = 已批; [-] = 撤回; an approved item set back to [ ] or [-] = 撤回."""
    page = Path(vault) / PAGE
    if not page.exists() or busy(page):
        return []
    changed = []
    for mark, _text, num in NQ_RE.findall(page.read_text(encoding='utf-8')):
        nid = 'NQ-' + num
        row = conn.execute('SELECT status FROM night_items WHERE id=?', (nid,)).fetchone()
        if not row:
            continue
        if row['status'] == '待批' and mark in ('x', 'X', '-'):
            status = '撤回' if mark == '-' else '已批'
        elif row['status'] == '已批' and mark in (' ', '-'):
            status = '撤回'
        else:
            continue
        if db.close_night_item(conn, nid, status):
            changed.append((nid, status))
    return changed


def page(conn):
    rows = lambda sql, *p: conn.execute(sql, p).fetchall()
    now = db.now()
    waiting = rows("SELECT * FROM night_items WHERE status='待批' AND deadline>? ORDER BY id", now)
    live = rows("SELECT * FROM night_items WHERE status='已批' AND deadline>? ORDER BY id", now)
    out = ['## 等你批准',
           '勾选一项 = 批准这一项在上限内跑（只经 paid_run.py），到截止时间作废。只有你勾。', '']
    out += [_line(r, ' ') for r in waiting] or ['暂无。']
    out += ['', '## 已批准（截止前有效）', '取消勾选 = 撤回：预算立刻作废，已经在跑的会在下一次巡检（30 分钟内）停下。', '']
    out += [_line(r, 'x') for r in live] or ['暂无。']
    from .render import _cell, _days_ago
    recent = rows('SELECT * FROM night_items WHERE created_at>=? ORDER BY id DESC', _days_ago(RECENT_DAYS))
    out += ['', '## 最近 %d 天' % RECENT_DAYS]
    if recent:
        out += ['| 编号 | 主线 | 任务 | 上限 | 状态 | 批准或撤回 | 截止 | 提出者 |', '|---|---|---|---|---|---|---|---|']
        out += ['| %s | %s | %s | ¥%g | %s | %s | %s | %s |' % (
            r['id'], db.MAINLINE_NAMES[r['mainline']], _cell(r['task']), r['cap_cny'], r['status'],
            _local(r['decided_at']) if r['decided_at'] else '—', _local(r['deadline']), _cell(r['editor']))
            for r in recent]
    else:
        out.append('暂无。')
    from .render import _flag
    summary = '> [!summary] 等你批准 %s 项 · 已批准 %d 项\n\n' % (_flag(len(waiting)), len(live))
    return AGENT_HEAD + summary + HISTORY_LINK, '\n'.join(out), [r['id'] for r in waiting]


def history_page(conn):
    """The auto block of agent/管理/今晚可跑-历史.md: 口头批准的预算. The watchdog's 巡检记录 section sits below it."""
    from .render import _cell, _days_ago
    rows = lambda sql, *p: conn.execute(sql, p).fetchall()
    now = db.now()
    verbal = rows("SELECT * FROM verbal_budgets WHERE created_at>=? OR (status='有效' AND expires_at>?) "
                  "ORDER BY created_at DESC", _days_ago(VERBAL_DAYS), now)
    out = ['从 [[今晚可跑]] 挪来的历史。今晚要批的项还在 [[今晚可跑]]。', '',
           '## 口头批准的预算（最近 %d 天）' % VERBAL_DAYS, '',
            '你在线程里批准、线程记录、脚本写进预算文件的付费预算。要撤回，对任一线程说「撤回 <编号>」。', '']
    if verbal:
        out += ['| 预算 | 上限 | 用途 | 你的批准 | 截止 | 记录者 | 状态 |', '|---|---:|---|---|---|---|---|']
        out += ['| `%s` | ¥%g | %s | %s | %s | %s | %s |' % (
            r['id'], r['cap_cny'], _cell(r['purpose']), _approval_cell(r),
            _local(r['expires_at']), _cell(r['editor']), _cell(_verbal_state(r, now))) for r in verbal]
    else:
        out.append('暂无。')
    return AGENT_HEAD, '\n'.join(out)


# ---- budgets ----

def _note(r):
    return (config.say('%s｜%s。上限 ¥%g，停止条件：%s。{owner} 在 Obsidian「今晚可跑」勾选，r2i-sync 于 %s 读回后写入；'
            '截止 %s（本地时间）。只由 r2i-sync 写，Claude 不手写 NQ- 预算。')
            % (r['id'], r['task'], r['cap_cny'], r['stop_if'], r['decided_at'], _local(r['deadline'])))


def _verbal_note(r):
    return (config.say('%s｜%s。上限 ¥%g。{owner} 口头批准：消息 %s（%s）「%s」，由 %s 记录，r2i-sync 于 %s 写入；截止 %s。'
            '只由 r2i-sync 写，Claude 不直接改 BUDGETS.json。')
            % (r['id'], r['purpose'], r['cap_cny'], r['approval_msg'], r['approval_at'], r['approval_quote'], r['editor'],
               db.now(), r['expires_at'])) if r['approval_msg'] != db.V4_MSG else (
        '%s｜%s。上限 ¥%g。小额（R-21，依据 %s，审计 %s），由 %s 记录，r2i-sync 于 %s 写入；截止 %s。'
        '只由 r2i-sync 写，Claude 不直接改 BUDGETS.json。'
        % (r['id'], r['purpose'], r['cap_cny'], r['basis'], r['audit_id'], r['editor'], db.now(), r['expires_at']))


def night_budget(now=None):
    """(id, entry) of the unified budget for the night `now` falls in, or None between 09:00 and 24:00 local."""
    local = (now or datetime.now(timezone.utc)).astimezone(db.LOCAL_TZ)
    if not config.in_window(local.hour, *_night_hours()):
        return None
    end = local.replace(hour=_night_hours()[1] % 24, minute=0, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    cap = config.get().night_budget
    bid = 'NIGHT-' + local.strftime('%Y%m%d')
    expires = end.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    return bid, {'total_cny': cap, 'orig_total_cny': cap, 'approved_by': 'user',
                 'approved_at': db.now(), 'expires_at': expires, 'night_budget': local.strftime('%Y-%m-%d'),
                 'note': (config.say('%s｜%s 夜里的统一预算 ¥%g（R-21：每晚 ¥%g，来自配置的 night_budget）。r2i-sync 于 %s 自动写入，'
                          '%s 早上作废，用不完不累积，超出的排到 09:00 以后报批。'
                          '只由 r2i-sync 写，Claude 不手写 NIGHT- 预算。')
                          % (bid, local.strftime('%m-%d'), cap, cap, db.now(), local.strftime('%m-%d')))}


def write_budgets(conn, path=None, now=None):
    """Approved night items and verbal budgets get a budget; withdrawn ones that had a budget expire now; during the
    night the unified NIGHT- budget is added once. Atomic replace; returns ids touched."""
    path = Path(path or BUDGETS)
    tonight = night_budget(now)
    todo_new = conn.execute("SELECT * FROM night_items WHERE status='已批' AND budget_written_at IS NULL "
                            "AND deadline>? ORDER BY id", (db.now(),)).fetchall()
    todo_cancel = conn.execute("SELECT * FROM night_items WHERE status='撤回' AND budget_written_at IS NOT NULL "
                               "AND decided_at>=budget_written_at ORDER BY id").fetchall()
    verbal_new = conn.execute("SELECT * FROM verbal_budgets WHERE status='有效' AND budget_written_at IS NULL AND error IS NULL "
                              "AND expires_at>? ORDER BY created_at", (db.now(),)).fetchall()
    verbal_cancel = conn.execute("SELECT * FROM verbal_budgets WHERE status='撤回' AND budget_written_at IS NOT NULL "
                                 "AND revoke_written_at IS NULL ORDER BY created_at").fetchall()
    if not (todo_new or todo_cancel or verbal_new or verbal_cancel or tonight):
        return []
    if not path.exists():  # a new project has no budget file until the first budget is written
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"budgets": {}}\n', encoding='utf-8')
    before = path.stat()
    raw = path.read_text(encoding='utf-8')
    doc = json.loads(raw)
    budgets = doc['budgets']
    touched = []
    if tonight and tonight[0] not in budgets:  # an existing entry, ours or not, is never overwritten
        budgets[tonight[0]] = tonight[1]
        touched.append(tonight[0])
    for r in todo_new:
        entry = {'total_cny': r['cap_cny'], 'orig_total_cny': r['cap_cny'], 'approved_by': 'user',
                 'approved_at': r['decided_at'], 'expires_at': r['deadline'], 'night_item': r['id'], 'note': _note(r)}
        old = budgets.get(r['id'])
        if old is not None and old.get('night_item') != r['id']:
            raise db.HSError('BUDGETS.json already has %s written by someone else; not overwriting' % r['id'])
        budgets[r['id']] = entry
        touched.append(r['id'])
    for r in todo_cancel:
        old = budgets.get(r['id'])
        if old is not None and old.get('night_item') == r['id'] and old.get('expires_at') != r['decided_at']:
            old['expires_at'] = r['decided_at']
            old['note'] = old['note'] + ' 已于 %s 撤回。' % r['decided_at']
            touched.append(r['id'])
    refused, verbal_written, verbal_revoked = [], [], []
    for r in verbal_new:
        if r['id'] in budgets:  # someone else's budget, or an old one with the same id: never overwritten
            refused.append(r['id'])
            continue
        budgets[r['id']] = {'total_cny': r['cap_cny'], 'orig_total_cny': r['cap_cny'],
                            'approved_by': 'small-rule' if r['approval_msg'] == db.V4_MSG else 'user',  # 小额: the rule, not the owner
                            'approved_at': r['approval_at'], 'expires_at': r['expires_at'],
                            'verbal_approval': {'message_id': r['approval_msg'], 'quote': r['approval_quote'],
                                                'recorded_by': r['editor'], 'recorded_at': r['created_at'],
                                                **({'basis': r['basis'], 'audit': r['audit_id']} if r['approval_msg'] == db.V4_MSG else {})},
                            'note': _verbal_note(r)}
        verbal_written.append(r['id'])
    for r in verbal_cancel:
        old = budgets.get(r['id'])
        if old is not None and (old.get('verbal_approval') or {}).get('message_id') == r['approval_msg']:
            old['expires_at'] = r['revoked_at']
            old['note'] = old['note'] + ' 已于 %s 撤回（%s：%s）。' % (r['revoked_at'], r['revoked_by'], r['revoke_reason'])
        verbal_revoked.append(r['id'])
    if refused:
        conn.executemany('UPDATE verbal_budgets SET error=? WHERE id=?',
                         [('BUDGETS.json 里已经有同名预算，没有覆盖；换一个编号重新记录', i) for i in refused])
        conn.commit()
    touched += verbal_written + verbal_revoked
    if not touched:
        return []
    if path.stat().st_mtime_ns != before.st_mtime_ns:  # someone else wrote it meanwhile: try again next run
        return []
    tmp = path.with_name('.%s.r2i-sync.tmp' % path.name)
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + ('\n' if raw.endswith('\n') else ''), encoding='utf-8')
    os.chmod(tmp, before.st_mode & 0o777)
    os.replace(tmp, path)
    stamp = db.now()
    conn.executemany('UPDATE night_items SET budget_written_at=? WHERE id=?', [(stamp, i) for i in touched])
    conn.executemany('UPDATE verbal_budgets SET budget_written_at=? WHERE id=?', [(stamp, i) for i in verbal_written])
    conn.executemany('UPDATE verbal_budgets SET revoke_written_at=? WHERE id=?', [(stamp, i) for i in verbal_revoked])
    conn.commit()
    return touched
