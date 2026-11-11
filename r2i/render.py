"""Deterministic render of the r2i-sync tables into the Obsidian vault. No model calls.

Owns only the region between AUTO_START and AUTO_END in each page it writes (plus the frontmatter of 进度/ pages);
text the owner writes outside that region is kept byte for byte. Every page change adds one row to the ledger's 变更 table
(the vault no longer holds 管理/变更记录.md; read it with `cli log-read`).
"""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import json
import os
import re
import sqlite3

from . import archive, audit_flow, audits, autowait, crit_rollup, db, exp_index, kinds, modes, night, programs, rulecheck, selfcheck, structure, unclaimed_check, unregistered, waits
from . import config

AUTO_START = '<!-- r2i-sync:auto:start 本段由脚本生成，手改会被覆盖 -->'
AUTO_END = '<!-- r2i-sync:auto:end -->'
EDITOR = 'r2i-sync 脚本'
TIMELINE_RECENT = 8
OVERVIEW_EVENTS = 20
STALE_HOURS = 24
# Decisions are Tasks-plugin checkboxes ending in an Obsidian block id (^dec-07): hidden in reading view, kept by
# Tasks when the owner ticks the item from 待办.md, and Tasks puts its ✅ done date before it.
DEC_RE = re.compile(r'^[ \t]*- \[(.)\][ \t]*(.*?)[ \t]*\^dec-(\d+)[ \t]*$', re.M)
DONE_RE = re.compile(r'\s*✅\s*\d{4}-\d{2}-\d{2}')
RECENT_DECIDED_DAYS = 7
COOLDOWN_SECONDS = 60  # a page touched this recently may be mid-edit in Obsidian: leave it for the next run


def _busy(path):
    import time
    return path.exists() and time.time() - path.stat().st_mtime < COOLDOWN_SECONDS


def _days_ago(n):
    from datetime import timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec='seconds').replace('+00:00', 'Z')


WIDE_HEAD = '---\ncssclasses: [wide]\n---\n\n'


def _one_line(text):
    return ' '.join(str(text or '').split())


def _md(text):
    """One line, with bare $ escaped so Obsidian does not read '$4 … $10' as inline math."""
    return re.sub(r'(?<!\\)\$', r'\\$', _one_line(text))


def _quote(line):
    """One line of a free-text detail as a blockquote line: indentation kept, $ escaped, headings defused."""
    line = re.sub(r'(?<!\\)\$', r'\\$', line.rstrip())
    if line.lstrip().startswith('#'):
        line = line.replace('#', '\\#', 1)
    return '> ' + line if line.strip() else '>'


def _cell(text):
    return _md(text).replace('|', '／') or '—'


def _short(text, limit=30):
    """Whole text when it fits; else up to the last 。？！ within the limit; else cut with …. (It is not split at ：；, which would cut
    「（现有类型：…）」 in half.)"""
    text = _one_line(text)
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [head.rfind(c) for c in '。？！?!']
    end = max(ends)
    return head[:end + 1] if end > 0 else text[:limit - 1] + '…'


def _mermaid(text):
    return _one_line(text).replace(':', '：').replace('#', '＃').replace(';', '；')


# ---- readback: the owner's ticks in 进度/ pages -> decisions ----

def readback(conn, vault):
    closed = []
    for ml in db.MAINLINES:
        page = Path(vault) / db.PROGRESS_REL[ml]
        if not page.exists() or _busy(page):
            continue
        for mark, text, num in DEC_RE.findall(page.read_text(encoding='utf-8')):
            did = 'DEC-' + num
            status = {'x': '已决', 'X': '已决', '-': '撤回'}.get(mark)
            if not status:
                continue
            answer = None
            if '答：' in text:
                answer = DONE_RE.sub('', text.split('答：', 1)[1]).strip() or None
            if db.close_decision(conn, did, status, answer):
                closed.append(did)
    return closed


# ---- page bodies ----

def _milestones(conn, mainline=None, limit=None):
    sql = 'SELECT * FROM milestones WHERE superseded_by IS NULL' + (' AND mainline=?' if mainline else '')
    sql += ' ORDER BY date DESC, id DESC' + (' LIMIT %d' % limit if limit else '')
    return conn.execute(sql, (mainline,) if mainline else ()).fetchall()


def _evidence(conn, e):
    """Human-side evidence: the experiment path when the milestone points at a recorded experiment."""
    parts = []
    if e['exp_id'] and conn.execute("SELECT 1 FROM sqlite_master WHERE name='experiments'").fetchone():
        row = conn.execute('SELECT path FROM experiments WHERE local_id=?', (e['exp_id'],)).fetchone()
        if row and row['path']:
            parts.append('`%s`' % _one_line(row['path']).replace('|', '／'))
    if e['evidence']:
        parts.append(_cell(e['evidence']))
    if not parts and e['exp_id']:
        parts.append('`%s`' % _one_line(e['exp_id']).replace('|', '／'))
    if e['cost']:
        parts.append(_cell(e['cost']))
    return ' · '.join(parts) or '—'


def _criteria(conn, mainline, version):
    return conn.execute('SELECT * FROM criteria WHERE mainline=? AND goal_version=? ORDER BY id',
                        (mainline, version)).fetchall()


def _open_decisions(conn, mainline):
    return conn.execute("SELECT * FROM decisions WHERE mainline=? AND status='待决' ORDER BY id", (mainline,)).fetchall()


def mainline_state(conn, ml):
    goal = db.current_goal(conn, ml)
    crit = _criteria(conn, ml, goal['version'])
    met = sum(1 for c in crit if c['status'] == '满足')
    return goal, crit, met


def _flag(n):
    """A count marked for the eye only when it asks the owner for something: ==K== when K > 0."""
    return '==%d==' % n if n else '0'


def progress_page(conn, ml):
    """Open decisions as Tasks checkboxes, then tables for the decided ones, the timeline
    and the acceptance criteria. The summary uses only fields that are also in the frontmatter."""
    goal, crit, met = mainline_state(conn, ml)
    decs = _open_decisions(conn, ml)
    events = _milestones(conn, ml, TIMELINE_RECENT)
    front = ['---', 'cssclasses: [wide]', 'mainline: ' + ml, 'goal_version: %d' % goal['version'], 'goal_status: ' + goal['status'],
             'criteria_met: %d' % met, 'criteria_total: %d' % len(crit), 'open_decisions: %d' % len(decs),
             'last_milestone: ' + (events[0]['date'] if events else '""'), '---']
    out = ['## 待决', '']
    out += ['- %s' % _md(d['question']) for d in decs] or ['暂无。']
    if decs:  # answered on each decision's page
        out += ['', '点每件的页去勾选，这里只列出来。']
    recent = conn.execute("SELECT * FROM decisions WHERE mainline=? AND status!='待决' AND decided_at >= ? ORDER BY decided_at DESC",
                          (ml, _days_ago(RECENT_DECIDED_DAYS))).fetchall()
    if recent:
        out += ['', '| 最近 %d 天已决 | 日期 |' % RECENT_DECIDED_DAYS, '|---|---|']
        out += ['| %s%s%s | %s |' % ('撤回：' if r['status'] == '撤回' else '', _cell(r['question']),
                                    '（答：%s）' % _cell(r['answer']) if r['answer'] else '', r['decided_at'][5:10])
                for r in recent]
    out += ['', '## 时间线（最近 %d 条）' % TIMELINE_RECENT, '']
    if events:
        out += ['| 日期 | 类型 | 事件 | 对主线的影响 | 证据与花费 |', '|---|---|---|---|---|']
        out += ['| %s | %s | %s | %s | %s |' % (e['date'][5:], e['kind'], _cell(e['summary']), _cell(e['impact']),
                                                _evidence(conn, e)) for e in events]
    else:
        out.append('暂无。')
    out += ['', '## 目标（v%d）与验收 %d/%d' % (goal['version'], met, len(crit)), '']
    if goal['status'] != '待定义':
        out += [_md(goal['goal']), '']
    out += ['| 状态 | 验收条件 | 证据 |', '|---|---|---|']
    out += ['| %s | %s | %s |' % (c['status'], _cell(c['text']), _cell(c['evidence'])) for c in crit] or ['| 未验证 | 待定义 | — |']
    summary = '> [!summary] 验收 %d/%d · 待决 %s 项' % (met, len(crit), _flag(len(decs)))
    head = '\n'.join(front) + '\n\n%s\n\n> 知识：[[%s]] · 首页：[[首页]] · 代号：[[实验索引]]\n\n' % (
        summary, db.NOTE_REL[ml][:-3])
    return head, '\n'.join(out)

WATCHDOG_HEARTBEAT = 'agent/管理/.night-watchdog-heartbeat'
WATCHDOG_STALE_MINUTES = 90


def watchdog_stale(vault):
    """True when the night watchdog is installed (heartbeat exists) but has not run for 90 minutes."""
    import time
    hb = Path(vault) / WATCHDOG_HEARTBEAT
    return hb.exists() and time.time() - hb.stat().st_mtime > WATCHDOG_STALE_MINUTES * 60


DAILY_HEARTBEAT = 'agent/管理/.daily-heartbeat'
DAILY_STALE_HOURS = 26


def daily_stale(vault):
    """True when the 07:00 knowledge agent is installed (heartbeat exists) but has not finished for 26 hours."""
    import time
    hb = Path(vault) / DAILY_HEARTBEAT
    return hb.exists() and time.time() - hb.stat().st_mtime > DAILY_STALE_HOURS * 3600


REPLY_HEARTBEAT = 'agent/管理/.reply-watch-heartbeat'
REPLY_STALE_MINUTES = 5


def reply_stale(vault):
    """True when the report-reply watcher is installed (heartbeat exists) but has not beaten for 5 minutes."""
    import time
    hb = Path(vault) / REPLY_HEARTBEAT
    return hb.exists() and time.time() - hb.stat().st_mtime > REPLY_STALE_MINUTES * 60


def overview_page(conn, watchdog_late=False, daily_late=False):
    out = []
    if watchdog_late:
        out += ['> [!warning] 夜间巡检超过 %d 分钟没有运行：检查 launchd 任务 %s。' % (WATCHDOG_STALE_MINUTES, config.label('night-watchdog')), '']
    if daily_late:
        out += ['> [!warning] 每日知识整理超过 %d 小时没有完成：看 %s。' % (DAILY_STALE_HOURS, config.state_dir() / 'logs' / 'daily.log'), '']
    open_total = sum(len(_open_decisions(conn, ml)) for ml in db.MAINLINES)
    out += ['## 各主线', '', '| 主线 | 目标 | 验收 | 待决 | 最近一条 |', '|---|---|---|---|---|']
    for ml in db.MAINLINES:
        goal, crit, met = mainline_state(conn, ml)
        last = _milestones(conn, ml, 1)
        out.append('| [[%s\\|%s]] | v%d %s | %d/%d | %d | %s |' % (
            db.PROGRESS_REL[ml][:-3], db.MAINLINE_NAMES[ml], goal['version'], goal['status'], met, len(crit), len(_open_decisions(conn, ml)),
            _cell('%s %s' % (last[0]['date'], last[0]['summary'])) if last else '—'))
    events = _milestones(conn, None, OVERVIEW_EVENTS)
    out += ['', '## 时间线（最近 %d 条）' % OVERVIEW_EVENTS, '']
    if events:
        out += ['```mermaid', 'timeline']
        by_date = {}
        for e in sorted(events, key=lambda e: (e['date'], e['id'])):
            by_date.setdefault(e['date'], []).append('%s %s' % (e['kind'], _mermaid(_short(e['summary']))))
        for d, items in by_date.items():
            out.append('    %s : %s' % (d, ' : '.join(items)))
        out.append('```')
    else:
        out.append('暂无。')
    head = WIDE_HEAD + '> [!summary] 各主线 · 待决 %s 项\n\n> 知识：[[知识库]] · 代号：[[实验索引]]\n\n' % _flag(open_total)
    return head, '\n'.join(out)


# ---- v2 layout: 首页, acceptance bars, short decisions, one-line timeline ----

# Rollout switch: mainlines whose 进度 page uses the v2 layout. The others keep progress_page; to roll out, add them here.
REDESIGN_B = db.MAINLINES  # bars on every 进度 page
HOME = '首页.md'
HOME_TICKS = False  # 首页 has no checkboxes; True brings back the option cards
HOME_ON = True  # 首页 is written; 进度/总览 is no longer written
LEGEND = '灰条是对照方法的读数，不是我们的结果；彩条是我们的读数，绿过线、琥珀没过；竖线是要过的线。'
CHIPS = {'正结果': 'pos', '发现': 'pos', '负结果': 'neg', '版本变更': 'ver', '里程碑': 'ver', '大跑完成': 'ver',
         'pivot': 'ver', '实战教训': 'warn'}
HOME_DECISIONS = 3
STAMP_RE = re.compile(r' · 更新 \d{2}:\d{2}')  # the 首页 time alone is no reason to rewrite the page
CRIT_LABEL_RE = re.compile(r'^([A-Z]\d{1,2})\s')


def _esc(text):
    import html
    return html.escape(_one_line(text), quote=True)


def _num(x):
    return '%g' % x


def _pct(x, scale):
    return '%.1f%%' % max(0.0, min(100.0, 100.0 * x / scale))


def _crit_label(c, i):
    m = CRIT_LABEL_RE.match(c['text'] or '')
    return m.group(1) if m else str(i)


def _passes(c):
    if c['reading'] is None or c['target'] is None:
        return False
    return c['reading'] <= c['target'] if c['direction'] == 'lower' else c['reading'] >= c['target']


def _over_line(crit):
    """Our readings (not baselines) that already meet their line while the ledger status is not 满足 yet: the mainline
    thread still has to accept them (crit-set). Otherwise 首页 would say 0/7 while the bars are green."""
    return sum(1 for c in crit if c['status'] != '满足' and c['reading_kind'] != '基线' and _passes(c))


def _met_text(crit, met):
    n = _over_line(crit)
    return '验收 %d/%d%s' % (met, len(crit), ' · 读数过线 %d 条（还没验收）' % n if n else '')


def crit_line(c, i):
    """One acceptance criterion as a bar: fill = reading / scale_max, black tick = target / scale_max (both 0-100%)."""
    lab = '<b>%s</b> %s' % (_esc(_crit_label(c, i)), _esc(c['short'] or _short(CRIT_LABEL_RE.sub('', c['text']), 12)))
    if c['direction'] == 'lower':
        lab += ' <span class="r2i-lower">越低越好</span>'
    base = c['reading_kind'] == '基线'
    cls = ['r2i-bar']
    if c['reading'] is None or not c['scale_max']:
        if c['status'] == '满足':
            cls.append('is-pass'); style = ' style="--v:100%"'; num = '满足'
        else:
            cls.append('is-empty'); style = ''; num = '还没测'
    else:
        cls.append('is-pass' if c['status'] == '满足' or _passes(c) else 'is-fail')
        style = ' style="--v:%s%s"' % (_pct(c['reading'], c['scale_max']),
                                     ';--t:%s' % _pct(c['target'], c['scale_max']) if c['target'] is not None else '')
        num = _esc(c['reading_label'] or _num(c['reading']))
        if c['target'] is not None and not base and not _passes(c) and c['status'] != '满足':  # 「差 N」 after
            # our reading only (a baseline is not our result)
            gap = (c['reading'] - c['target']) if c['direction'] == 'lower' else (c['target'] - c['reading'])
            num += ' 差 %g' % round(gap, 4)
        if c['target'] is not None:
            num += ' · 线 ' + _esc(c['target_label'] or _num(c['target']))
        if not base and c['status'] != '满足' and _passes(c):
            num += ' · 过线，还没验收'
        if base:
            cls.append('is-base'); num += ' <small class="r2i-base">基线</small>'
        if not (c['evidence'] or '').strip() or c['evidence'].strip() == '—':  
            num += ' <small class="r2i-src">缺出处</small>'
    out = ['<div class="r2i-crit"><span class="r2i-lab">%s</span><span class="%s"%s></span><span class="r2i-num">%s</span></div>'
           % (lab, ' '.join(cls), style, num)]
    if c['reading_note']:
        out.append('<div class="r2i-note">%s</div>' % _esc(c['reading_note']))
    return out


def _fold(title, lines):
    """A folded note callout. Never holds a checkbox (Tasks plugin): '- [ ]' at the start of a line is defused."""
    return ['> [!note]- ' + title] + [_quote(re.sub(r'^(\s*[-*+] )\[(.)\]', r'\1\\[\2]', l)) for l in lines]


def _dec_title(d):
    return _md(d['title']) if d['title'] else _md(_short(d['question'], 40))


def _latest(conn, ml=None):
    last = _milestones(conn, ml, 1)
    return _md(_short(last[0]['summary'], 30)) if last else '—'


def _reading_line(crit):
    """「读数最后更新：MM-DD HH:MM」 (local) from the newest criterion that carries a reading. It changes only when a reading does,
    so the page stays untouched between ledger changes (a clock line would rewrite it every minute)."""
    stamps = [c['updated_at'] for c in crit if c['reading'] is not None and c['updated_at']]
    if not stamps:
        return '读数最后更新：—'
    return '读数最后更新：' + db.parse_time(max(stamps)).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M')


YESTERDAY_ROWS = 12


def _cut(text, limit):
    text = _one_line(text)
    return text if len(text) <= limit else text[:limit] + '…'


def _step_key(note):
    m = re.fullmatch(r'步(\d+)', (note['step'] or '').strip())
    return (0, int(m.group(1)), note['id']) if m else (1, 0, note['id'])  # no step: last


def yesterday_section(conn, ml, vault=None, now=None):
    """「## 昨天（MM-DD）」: the mainline's task notes of the previous local day, built from the ledger, then a link to the
    daily note named by today's date (item B) when that file exists. Changes only with the ledger and the day."""
    today = (now or _local_now()).astimezone(db.LOCAL_TZ)
    day = (today - timedelta(days=1)).strftime('%Y-%m-%d')
    notes = sorted(conn.execute('SELECT * FROM task_notes WHERE mainline=? AND date=?', (ml, day)).fetchall(), key=_step_key)
    out = ['## 昨天（%s）' % day[5:], '']
    if not notes:
        out.append('昨天这条主线没有任务记录。')
    else:
        out += ['| 步骤 | 做了什么 | 结果 | 花费 |', '|---|---|---|---|']
        for n in notes[:YESTERDAY_ROWS]:
            code = ' '.join((n['exp_ids'] or '').replace(',', ' ').split()) or n['id']
            out.append('| %s | %s（%s） | %s | %s |' % (_cell(n['step']), _cell(n['title']), _cell(code),
                                                      _cell(_cut(n['result'], 60)), _cell(n['cost'])))
        if len(notes) > YESTERDAY_ROWS:
            out += ['', '另有 %d 条，见早报。' % (len(notes) - YESTERDAY_ROWS)]
    name = today.strftime('%Y-%m-%d')
    if vault and modes.get(conn, 'kinds') == 'auto':  # 同步 pages seen yesterday keep a link here
        out += [''] + kinds.yesterday_lines(vault, ml, day, _md)
    out += ['', ('早报：[[每日/%s|%s 早报]]' % (name, name)) if vault and (Path(vault) / '每日' / (name + '.md')).is_file() else '早报还没出']
    return out


def latest_audit_reading(conn, c):
    """The newest grade-A audit (留出/测试集) of this criterion with an `after`, else the
    newest one of any grade, any verdict, as 「读数 · 结论 · A档 · MM-DD」 (local time; no 档 part when grade is empty). A newer dev-set
    reading must not hide the held-out one. Empty when the ledger reading is it, or starts with it followed by a space,
    「=」 or 「（」. crit may list several ids (same match as crit_rollup)."""
    sql = ("SELECT * FROM audits WHERE (',' || REPLACE(COALESCE(crit,''), ' ', '') || ',') LIKE '%,' || ? || ',%' "
           "AND kind='result' AND undo_status!=? AND COALESCE(after,'')!=''")
    order = ' ORDER BY created_at DESC, id DESC LIMIT 1'
    a = (conn.execute(sql + " AND grade='A'" + order, (c['id'], audits.UNDONE)).fetchone()
         or conn.execute(sql + order, (c['id'], audits.UNDONE)).fetchone())
    led = c['reading_label'] or ''
    if not a or led == a['after'] or (led.startswith(a['after']) and led[len(a['after'])] in ' =（'):  # 「23/30 = 0.77」 is 23/30; no number parsing
        return ''
    parts = [a['after'], a['verdict'] or '—'] + (['%s档' % a['grade']] if a['grade'] else []) + [audits.local_date(a['created_at'])[5:]]
    return _cell(' · '.join(parts))


def progress_page_v2(conn, ml, vault=None):
    """Acceptance bars first, open decisions as one short checkbox line each with the options folded
    below, then the timeline as one line per event. The old tables move verbatim into folded callouts."""
    goal, crit, met = mainline_state(conn, ml)
    decs = _open_decisions(conn, ml)
    events = _milestones(conn, ml, TIMELINE_RECENT)
    front = ['---', 'cssclasses: [wide]', 'mainline: ' + ml, 'goal_version: %d' % goal['version'], 'goal_status: ' + goal['status'],
             'criteria_met: %d' % met, 'criteria_total: %d' % len(crit), 'open_decisions: %d' % len(decs),
             'last_milestone: ' + (events[0]['date'] if events else '""'), '---']
    out = [_reading_line(crit), '', '## 验收', '', '%s（目标 v%d）。' % (_met_text(crit, met), goal['version']), '']  # heading never changes: 首页/主线 embed `#验收`
    if goal['status'] != '待定义':  # first sentence only, the whole goal folded
        first = re.split(r'(?<=。)', _one_line(goal['goal']), 1)[0]
        out += ['目标：' + _md(first), '']
        if first != _one_line(goal['goal']):
            out += _fold('目标全文（v%d）' % goal['version'], [_md(goal['goal'])]) + ['']
    out += [LEGEND, '']
    for i, c in enumerate(crit, 1):
        out += crit_line(c, i)
    if not crit:
        out.append('待定义。')
    table = ['| 状态 | 验收条件 | 证据 | 最新审计读数 |', '|---|---|---|---|']
    table += ['| %s | %s | %s | %s |' % (c['status'], _cell(c['text']), _cell(c['evidence']), latest_audit_reading(conn, c))
              for c in crit] or ['| 未验证 | 待定义 | — | |']
    out += [''] + _fold('每条验收的原文和证据', table)
    out += [''] + yesterday_section(conn, ml, vault)
    out += ['', '## 待决', '']
    if decs:  # read-only here; the one place to answer is each decision's page
        out += ['点每件的「去定」到那一页勾选，这里只列出来。', '']
    from . import dec_pages
    for d in decs:
        out.append('- %s · %s' % (_dec_title(d), dec_pages.link(d, vault=vault)))
        body = ['- ' + re.sub(r'^[-*+]\s+', '', _md(o)) for o in (d['options'] or '').splitlines() if o.strip()]
        body += ['原文：' + _md(d['question'])]
        if d['source']:
            body.append('依据：' + _md(d['source']))
        out += _fold('选项和依据', body) + ['']
    if not decs:
        out.append('暂无。')
    recent = conn.execute("SELECT * FROM decisions WHERE mainline=? AND status!='待决' AND decided_at >= ? ORDER BY decided_at DESC",
                          (ml, _days_ago(RECENT_DECIDED_DAYS))).fetchall()
    if recent:
        out += ['', '| 最近 %d 天已决 | 日期 |' % RECENT_DECIDED_DAYS, '|---|---|']
        out += ['| %s%s%s | %s |' % ('撤回：' if r['status'] == '撤回' else '', _cell(r['question']),
                                    '（答：%s）' % _cell(r['answer']) if r['answer'] else '', r['decided_at'][5:10])
                for r in recent]
    out += ['', '## 时间线（最近 %d 条）' % TIMELINE_RECENT, '']
    if events:
        out += ['- %s <span class="r2i-chip %s">%s</span> %s' % (e['date'][5:], CHIPS.get(e['kind'], 'ver'), _esc(e['kind']),
                                                               _md(_short(e['summary'], 40))) for e in events]
        table = ['| 日期 | 类型 | 事件 | 对主线的影响 | 证据与花费 |', '|---|---|---|---|---|']
        table += ['| %s | %s | %s | %s | %s |' % (e['date'][5:], e['kind'], _cell(e['summary']), _cell(e['impact']),
                                                  _evidence(conn, e)) for e in events]
        out += [''] + _fold('每条的影响、证据和花费', table)
    else:
        out.append('暂无。')
    summary = '> [!summary] 验收 %d/%d 达标 · 待决 %s · 最近：%s' % (met, len(crit), _flag(len(decs)), _latest(conn, ml))
    head = '\n'.join(front) + '\n\n%s\n\n> 知识：[[%s]] · 首页：[[首页]] · 代号：[[实验索引]]\n\n' % (
        summary, db.NOTE_REL[ml][:-3])
    return head, '\n'.join(out)


def _local_now():
    return datetime.now(db.LOCAL_TZ)


def open_reports(vault, conn=None):
    """汇报 pages that still wait for the owner: a 「## 回复」 section not yet received and 状态 not finished (已回, 已结, or an old
    已看 / 已取代…, db.report_done)."""
    from . import reply_watch
    out = []
    folder = Path(vault) / db.REPORT_DIR if vault else None
    if not folder or not folder.is_dir():
        return out
    for path in db.report_pages(vault):  # 要批/, the retired 待决/, 同步/ and the 汇报/ root 
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        m = reply_watch.SECTION_RE.search(text)
        props = reply_watch.front(text)
        if not m or '[!done]' in m.group(1) or db.report_done(props.get('状态')):
            continue
        q = re.search(r'^## 要你定[ \t]*\n(?:.*\n)*?>[ \t]*\[!owner\][-+]?[ \t]*(.+)$', text, re.M)
        name = path.name[:-3]
        since = props.get('日期') or ''
        try:
            at = datetime.strptime(since[:16], '%Y-%m-%d %H:%M').replace(tzinfo=db.LOCAL_TZ)
        except ValueError:
            at = datetime.fromtimestamp(path.stat().st_mtime, db.LOCAL_TZ)
        if conn is not None and props.get('待决') and archive.settled_by_thread(conn, props.get('待决')):
            continue  # a thread settled it itself; the owner can still tick 改 on the page, but it is not their to decide
        out.append({'type': db.report_type(props.get('类型')), 'dec': props.get('待决'), 'page': name,
                    'rel': path.relative_to(vault).with_suffix('').as_posix(), 'question': q.group(1) if q else re.sub(r'^\d{4}-\d{2}-\d{2} ', '', name),
                    'mainline': props.get('主线') or '', 'cap': props.get('上限') if props.get('预算') else None,
                    'since': at, 'wait': props.get('等你回'), 'progress': reply_watch.progress(text),
                    'paused': (props.get('状态') or '').strip() == '暂缓'})
    return out


def _since(ts):
    return db.parse_time(ts).astimezone(db.LOCAL_TZ)


def _ml_name(ml):
    return db.SHORT_NAMES.get(ml, ml or '—')


def NO_THREAD(w):
    return '' if w['thread'] or not w['report'] else '（页上没写线程，答了叫不醒）'


KIND = {'money': 'k-money', 'pick': 'k-pick', 'do': 'k-do', 'warn': 'k-warn'}
TYPED_NO_BOX = ('删', '删除', '对外', '发送', '发布', '上传', 'push', 'merge', '改账')  # these stay typed, no 首页 box
DO_PREFIX = ('做：', '做:', '提醒：', '提醒:')
RUN_PREFIX = ('在 Mac 终端跑', '在 Mac 上跑', '在终端跑', '跑一下')  # a wait whose ask starts so is a one-line reminder (一句提醒), not a question


def _row(kind, label, text, ml='', tail=''):
    """「- <span class="r2i-k k-…">标签</span>问题 · 主线 · 链接」 (no space after the chip; 主线 left out when unknown)."""
    return '- <span class="r2i-k %s">%s</span>%s%s%s' % (KIND[kind], label, _md(text), ' · ' + _md(ml) if ml else '', tail)


def _money_label(cap):
    cap = (cap or '').strip()
    return '批钱 ' + (cap if cap[:1] in ('¥', '￥', '$') else '¥' + cap)


def _page_props(vault, page):
    if not (vault and page):
        return {}
    try:
        from . import reply_watch
        return reply_watch.front(db.report_path(vault, page).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, AttributeError):
        return {}


def _wait_item(w, vault):
    """One open wait without an open page listing it -> ('decide' | 'do', row). The 主线 comes from its page when it has one."""
    props = _page_props(vault, w['report'])
    ml = _ml_name(props.get('主线')) if props.get('主线') else ''
    ask = (w['ask'] or '').split('｜')[-1]
    link = lambda label: ' · [[%s|%s]]' % (db.report_rel(vault, w['report']), label) if w['report'] else ''
    nothread = NO_THREAD(w)
    # typed asks and reminders show in full (they carry the command), no …
    if w['typed']:  # no checkbox, even when its page is listed above
        row = _row('pick', '打字批', ask, ml, ' · 在聊天里回「%s」%s%s' % (w['typed'], link('去看'), nothread))
        if not any(k in ask for k in TYPED_NO_BOX):  # a tickable box too, except the things that stay typed
            from . import home_ticks
            label = '%s · 勾了等于回「%s」' % ('做完了' if ask.startswith(('做', '提醒')) else '批', w['typed'])
            row += '\n' + home_ticks._box(' ', label, 'wt-' + w['id'].split('-', 1)[1], '').rstrip('-')
        return 'decide', row
    if db.report_type(props.get('类型')) == '要你做' and w['report']:
        return 'do', _row('do', '有步骤', _short(ask, 40), ml, link('看步骤') + nothread)
    if ask.startswith(DO_PREFIX):
        return 'do', _row('do', '一句提醒', re.sub(r'^[^：:]+[：:]\s*', '', ask), ml, link('看步骤') + nothread)
    if ask.startswith(RUN_PREFIX):  # 「在 Mac 终端跑 …」 is a step for the owner, not a choice 
        where = link('看步骤') if w['report'] else ' · 做完在线程「%s」里说一声' % _md(w['editor'])
        return 'do', _row('do', '去跑', ask, ml, where + nothread)
    where = link('去定') if w['report'] else ' · 在线程「%s」里回' % _md(w['editor'])  # a chat ask has no page to link
    return 'decide', _row('pick', '选', _short(ask, 60), ml, where + nothread)


def decide_items(conn, vault, flagged=()):
    """首页「要你定」 and 「要你做」 (report pages, open waits, open decisions and night
    items, money first, then the longest waiting. Returns [(costs_money, since, section, line)], section 'decide' or 'do'."""
    items = []
    reports = open_reports(vault, conn)
    linked = {r['wait'] for r in reports if r['wait']} | {r['page'] for r in reports}
    page_decs = {r['dec'] for r in reports if r['dec']}
    skip = {f['page'] for f in flagged if f.get('reason') not in ('dup', 'res')}  # a page marked 「⚠ 没登记」 has its row there, not a second one
    for r in reports:
        if r['page'] in skip:
            continue
        nothread = conn.execute("SELECT 1 FROM waits WHERE report=? AND status='等待' AND thread=''", (r['page'],)).fetchone()
        tail = '（页上没写线程，答了叫不醒）' if nothread and not r['dec'] else ''  # a decision without --thread is fine
        ml = _ml_name(r['mainline']) if r['mainline'] else ''
        done = ' · 已答 %d/%d' % r['progress'] if r.get('progress') else ''  # a multi-question page, partly answered
        link = lambda label: '%s · [[%s|%s]]%s' % (done, r['rel'], label, tail)
        if r.get('paused'):  # 状态: 暂缓 — the asking thread said 「先别定」; listed last, nothing to tick yet
            items.append((False, datetime.max.replace(tzinfo=db.LOCAL_TZ), 'decide',
                          _row('pick', '暂缓', '先别定：' + _short(r['question'], 60), ml, link('看'))))
        elif r['type'] == '要你做':
            items.append((False, r['since'], 'do', _row('do', '有步骤', _short(r['question'], 40), ml, link('看步骤'))))
        elif r['cap']:
            items.append((True, r['since'], 'decide', _row('money', _money_label(r['cap']), _short(r['question'], 60), ml, link('去定'))))
        else:
            items.append((False, r['since'], 'decide', _row('pick', '选', _short(r['question'], 60), ml, link('去定'))))
    for w in waits.due(conn):
        if not w['typed'] and (w['id'] in linked or (w['report'] and w['report'] in linked)):
            continue
        section, row = _wait_item(w, vault)
        items.append((False, _since(w['asked_at']), section, row))
    from . import dec_pages
    for d in conn.execute("SELECT * FROM decisions WHERE status='待决' ORDER BY id").fetchall():  
        if d['id'] in page_decs:
            continue  # its page is listed above
        items.append((False, _since(d['created_at']), 'decide', _row('pick', '选', _dec_title(d), _ml_name(d['mainline']),
                                                                    ' · ' + dec_pages.link(d, vault=vault))))
    for r in conn.execute("SELECT * FROM night_items WHERE status='待批' AND deadline>? ORDER BY id", (db.now(),)).fetchall():
        items.append((True, _since(r['created_at']), 'decide', _row('money', _money_label('%g' % r['cap_cny']), '今晚可跑：' + _short(r['task'], 25),
                                                                    _ml_name(r['mainline']), ' · [[%s|去定]]' % night.PAGE[:-3])))
    items.sort(key=lambda x: (not x[0], x[1]))
    return items


def decided_cards(conn, vault=None):
    """Plain lines, no checkbox. Answers on decision pages in the last day and approved night items before
    their deadline. Decisions a thread settled itself (「已按推荐做」) are not listed here any more (they
    go to the daily note's audit section)."""
    from . import dec_pages
    out = []
    for d in conn.execute("SELECT * FROM decisions WHERE status='已决' AND answered_via='页' AND decided_at>=? "
                          "ORDER BY decided_at DESC", (_days_ago(1),)).fetchall():
        out.append('- %s · 选 %s · %s' % (_dec_title(d), _md(d['answer']), dec_pages.link(d, '撤回', vault)))
    for r in conn.execute("SELECT * FROM night_items WHERE status='已批' AND deadline>? ORDER BY id", (db.now(),)).fetchall():
        out.append('- 今晚可跑：%s · 已批 ¥%g · 截止 %s · [[%s|撤回]]' % (
            _md(_short(r['task'], 25)), r['cap_cny'], night._local(r['deadline']), night.PAGE[:-3]))
    return out


def decided_cards_dloop(conn):
    """首页 cards (kept for a rollback: reply_watch.HOME_TICKS / render.HOME_TICKS)."""
    from . import home_ticks
    out = []
    for d in conn.execute("SELECT * FROM decisions WHERE status='已决' AND answered_via='首页' AND decided_at>=? "
                          "ORDER BY decided_at DESC", (_days_ago(1),)).fetchall():
        out += home_ticks.dec_received(d, '- **%s** · %s' % (_dec_title(d), db.SHORT_NAMES[d['mainline']]))
    for d in conn.execute("SELECT * FROM decisions WHERE status='已决' AND answered_via='线程' AND decided_at>=? "
                          "ORDER BY decided_at DESC", (_days_ago(3),)).fetchall():
        out += home_ticks.dec_acted(d, '- **%s** · %s' % (_dec_title(d), db.SHORT_NAMES[d['mainline']]))
    for r in conn.execute("SELECT * FROM night_items WHERE status='已批' AND deadline>? ORDER BY id", (db.now(),)).fetchall():
        out += home_ticks.nq_received(r, '- **今晚可跑：%s** · 上限 ¥%g · 截止 %s' % (
            _md(_short(r['task'], 25)), r['cap_cny'], night._local(r['deadline'])))
    return out


def forwarding_block(conn):
    """「> [!note]- 转发中（N 条）」: undelivered report replies (10 minutes, no reply-wait picked them up) and jobs the hourly
    self-check restarts itself. Byte-stable: no times. Returns [] when there are none."""
    try:
        rows = ['%s · %s · 线程还没取，值班检查会转给它' % (_md(t), _md(p)) for t, p in db.unclaimed_pages(conn)]
    except sqlite3.Error:
        rows = []
    rows += selfcheck.quiet_rows(conn)
    return ['', '> [!note]- 转发中（%d 条）' % len(rows)] + ['> - ' + r for r in rows] if rows else []


def thread_block(vault, conn=None):
    """「> [!note]- 给线程看 · N 件」: marks for threads, not for the owner (specs or code put in 汇报/附件, pages of a
    type that is not opened). Returns [] when there are none."""
    if not vault:
        return []
    lines = unregistered.stray_lines(unregistered.stray_attachments(vault), _md) + unregistered.nopage_lines(
        unregistered.nopage_pages(vault), _md)
    if conn is not None:
        try:
            lines += audit_flow.overdue_lines(conn, _md)  # E3
            lines += selfcheck.count_lines(conn)  # V5-SELFCHECK-COUNTS
        except sqlite3.Error:
            pass
    return ['', '> [!note]- 给线程看 · %d 件' % len(lines)] + ['> - ' + l for l in lines] if lines else []


AUDIT_HOURS, AUDIT_MAX = 72, 15


def unbooked_spend_lines(conn, now):
    """⚠ lines for paid runs that ended in the last 24 h and cost money but name no experiment row (it used to be a key of
    the 07:00 inputs). Never breaks the page."""
    from . import daily
    try:
        runs = daily.paid_runs(now - timedelta(hours=24), now, daily.LEDGER)
        orphans = daily.spend_without_row(runs, daily._rows(conn, 'SELECT * FROM experiments'))
        by = {}  # one line per budget, not per call (20 lines of ¥0.01 would push 要你定 off the screen)
        for r in orphans:
            n, s = by.get(r['budget'] or r['group'] or r['run_id'], (0, 0.0))
            by[r['budget'] or r['group'] or r['run_id']] = (n + 1, s + r['spent_cny'])
        return ['- ⚠ 花了钱台账没行：%s ¥%.2f%s' % (_md(k), s, '（%d 笔）' % n if n > 1 else '') for k, (n, s) in by.items()]
    except Exception:
        return []


def audit_block(conn, vault, now):
    """「## 审计结论（可改判）」 (早报 v2): decisions the audit threads took themselves in the last 72 h, newest first, at most 15,
    one `- [ ] 改 · …` line each; reply_watch reads the ticks. A line the owner has ticked and nobody has processed yet stays ticked
    (the page is rewritten from the ledger, so their tick is read from the current page first); a processed one is gone."""
    try:
        ticked = {n for m, n in audits.TICK_RE.findall((Path(vault) / HOME).read_text(encoding='utf-8')) if m in 'xX'} if vault else set()
    except (OSError, UnicodeDecodeError):
        ticked = set()
    cut = now - timedelta(hours=AUDIT_HOURS)
    rows = [r for r in conn.execute("SELECT * FROM audits WHERE kind='decided' AND undo_status='' ORDER BY created_at DESC, id DESC")
            if r['id'][4:] in ticked or db.parse_time(r['created_at']) >= cut]
    shown = [r for i, r in enumerate(rows) if i < AUDIT_MAX or r['id'][4:] in ticked]
    if not shown:
        return []
    out = ['', '## 审计结论（可改判）', ''] + ['- [%s] 改 · %s · %s %s' % ('x' if r['id'][4:] in ticked else ' ', db.SHORT_NAMES[r['mainline']],
                                                                    _md(r['title']), audits.block_id(r['id'])) for r in shown]
    return out + (['还有 %d 条' % (len(rows) - len(shown))] if len(rows) > len(shown) else [])


def home_page(conn, watchdog_late=False, daily_late=False, now=None, reply_late=False, vault=None):
    """首页: 「要你定」 is the owner's one inbox; small decisions and night items are cards whose
    option checkboxes are the only place in the vault to answer them.. No links to 待办 or 等你回复."""
    now = now or _local_now()
    out = []
    if watchdog_late:
        out += ['> [!warning] 夜间巡检超过 %d 分钟没有运行：检查 launchd 任务 %s。' % (WATCHDOG_STALE_MINUTES, config.label('night-watchdog')), '']
    if daily_late:
        out += ['> [!warning] 每日知识整理超过 %d 小时没有完成：看 %s。' % (DAILY_STALE_HOURS, config.state_dir() / 'logs' / 'daily.log'), '']
    if reply_late:
        out += ['> [!warning] 汇报页回复监听超过 %d 分钟没有心跳：汇报页上的回复暂时进不了台账，检查 launchd 任务 '
                '%s。' % (REPLY_STALE_MINUTES, config.label('report-reply')), '']
    top = selfcheck.system_lines(conn) + selfcheck.home_line(conn) + unbooked_spend_lines(conn, now)  # nothing when all is well
    if top:
        out += top + ['']
    flagged = unregistered.scan(conn, vault) if vault else []  # marks only, last rows of 「要你定」
    items = decide_items(conn, vault, flagged)
    decide = [it for it in items if it[2] == 'decide']
    todo = [it for it in items if it[2] == 'do']
    money = sum(1 for it in decide if it[0])
    unclaimed = []
    if modes.get(conn, 'unclaimed-push') == 'auto':  # A2: 勾了 10 分钟没人取
        try:
            unclaimed = unclaimed_check.home_lines(conn, unclaimed_check.rows_from_db(conn, vault=vault), _md)
        except sqlite3.Error:
            unclaimed = []
    out += ['## 要你定', ''] + [it[3] for it in decide] + unregistered.home_lines(flagged, _md) + unclaimed  # compact: no blank lines between rows
    if not decide and not flagged and not unclaimed:
        out.append('暂无。')
    out += audit_block(conn, vault, now)
    if todo:
        out += ['', '## 要你做', ''] + [it[3] for it in todo]
    done = decided_cards(conn, vault)
    if done:
        out += ['', '### 刚定的', ''] + done
    if vault and modes.get(conn, 'kinds') == 'auto':  # unseen 同步 pages, one line each
        out += [''] + kinds.home_block(vault, _md)
    out += ['', '## 各主线', '', '> [!r2i-grid]']
    for i, ml in enumerate(db.MAINLINES):
        goal, crit, met = mainline_state(conn, ml)
        segs = ''.join('<i class="on"></i>' if c['status'] == '满足' else '<i></i>' for c in crit)
        if i:
            out.append('>')
        out += ['> > [!r2i-line] [[%s|%s]]' % (db.PROGRESS_REL[ml][:-3], db.MAINLINE_NAMES[ml]),
                '> > <span class="r2i-segs">%s</span>' % segs,
                '> > %s · 待决 %s' % (_met_text(crit, met), _flag(len(_open_decisions(conn, ml)))),
                '> > 最近：' + _latest(conn, ml)]
    out += ['', '## 结构图', '']
    for ml in db.MAINLINES:  # the frozen diagrams, embedded from agent/结构/ (collapsed; written by struct-set only)
        out += ['> [!r2i-struct]- %s 结构 %s' % (db.SHORT_NAMES[ml], structure.shown_version(conn, ml)),
                '> ' + structure.embed(ml), '']
    out.pop()
    out += ['', '## 新汇报', '', '![[汇报/汇报.base#没看完的]]', '',
            '[[知识库]] · [[实验索引]] · [[项目规则|规则]] · [[资源|资源]] · 交接：'
            + ' · '.join('[[agent/交接/%s 交接|%s]]' % (n, n) for n in db.SHORT_NAMES.values())]
    rules = rules_info(vault)  # 「规则 vN · MM-DD 改了 R-xx」
    if rules:
        out += ['', rules['line']]
    obs = modes.footer(conn)  # one line while an automation only observes; nothing when none does
    if obs:
        out += ['', obs]
    out += waits.closed_block(conn, _md, vault)  # 已过期 / 已撤回, folded at the very bottom
    out += thread_block(vault, conn)  # the ⚠ lines meant for threads, folded, last
    out += forwarding_block(conn)  # nothing here is the owner's to do; the very bottom, folded
    title = '%d 月 %d 日 · 周%s' % (now.month, now.day, '一二三四五六日'[now.weekday()])
    bar = '要你定 **%d**%s%s · 更新 %s' % (len(decide) + len([f for f in flagged if f.get('reason') != 'res']), ' · 要花钱 **%d**' % money if money else '',
                                        ' · 要你做 **%d**' % len(todo) if todo else '', now.strftime('%H:%M'))
    head = '---\ncssclasses: [wide, r2i-home]\n---\n\n# %s\n\n> [!summary] %s\n\n' % (title, bar)
    return head, '\n'.join(out)


RULES_REL = '项目规则.md'


def rules_info(vault):
    """The rules table's version, last change and md5: {'version', 'line', 'md5'}, or None when the page is missing.
    The version is the 属性 `version`; the last change is the final row of 「## 版本记录」 (time column, 改了哪几条)."""
    import hashlib
    path = Path(vault) / RULES_REL if vault else None
    try:
        raw = path.read_bytes()
        text = raw.decode('utf-8')
    except (OSError, AttributeError, UnicodeDecodeError):
        return None
    from . import reply_watch
    version = reply_watch.front(text).get('version') or '?'
    sec = re.search(r'(?ms)^## 版本记录[ \t]*\n(.*?)(?=^## |\Z)', text)
    rows = [l for l in (sec.group(1).split('\n') if sec else []) if l.strip().startswith('|')
            and not re.match(r'\|\s*(版本\s*\||-)', l.strip())]
    line = '规则 %s' % version
    if rows:
        cells = [c.strip() for c in rows[-1].strip().strip('|').split('|')]
        day = re.match(r'(\d{2}-\d{2})', cells[1]) if len(cells) > 1 else None
        ids = list(dict.fromkeys(re.findall(r'R-\d\d(?![\dx])', cells[2]))) if len(cells) > 2 else []  # R-0x (a class) is not a rule id
        if day:
            line += ' · %s 改了 %s' % (day.group(1), '、'.join(ids[:6]) + ('等' if len(ids) > 6 else '') if ids else '规则')
    return {'version': version, 'line': line, 'md5': hashlib.md5(raw).hexdigest()}


def rules_check(conn, vault, log=None):
    """The rules table changed but `version` did not: one changelog line per md5 (kept in meta)."""
    info = rules_info(vault)
    if not info:
        return None
    seen = db.meta_get(conn, 'rules-state')
    db.meta_set(conn, 'rules-state', {'md5': info['md5'], 'version': info['version']})
    if seen and seen.get('md5') != info['md5'] and seen.get('version') == info['version']:
        (log or log_change)(vault, EDITOR, RULES_REL, '规则表改了没升版本（md5 %s，仍是 %s）' % (info['md5'][:8], info['version']))
        return 'unversioned'
    return 'ok'


def log_wait_move(vault, result, editor):
    """Changelog line for wait-move (handover)."""
    if not vault:
        return 'no vault: changelog line skipped'
    log_change(vault, editor, HOME, '等你回 %s 改指向接班线程 %s（原 %s）' % (
        '、'.join(result['moved']) or '（无）', result['thread'][:16] + '…', (result['from'] or '—')[:16] + '…'))
    return 'ok'


def log_ledger_change(vault, op, result, editor):
    """Changelog line for a crit-read / dec-retitle / dec-act / dec-detail write (the page itself changes at the next render)."""
    if not vault:
        return 'no vault: changelog line skipped'
    if op == 'crit-read':
        what = '验收读数 %s：%s' % (result['id'], '、'.join(result['set']))
    elif op == 'dec-detail':
        from . import dec_pages
        found = dec_pages.locate(vault, result['id'])  # the page's real place (an old 汇报/待决/ page or a new one in 汇报/)
        log_change(vault, editor, found[0].relative_to(vault).as_posix() if found else '%s/%s.md' % (db.REPORT_DIR, result['page']), '待决 %s 写了正文（%d 字），待决页下一分钟换上' % (result['id'], result['chars']))
        return 'ok'
    elif op == 'dec-act':
        log_change(vault, editor, HOME, '待决 %s 线程自己定（R-10）：已按「%s」做 · 不同意勾「改」' % (result['id'], result['answer']))
        return 'ok'
    else:
        what = '待决 %s 短标题：%s' % (result['id'], result['title'])
    log_change(vault, editor, db.PROGRESS_REL[result['mainline']], what)
    return 'ok'


def _money(unit, value):
    sym = {'CNY': '¥', 'USD': '\\$'}.get((unit or '').upper())
    return '%s%s' % (sym, value) if sym else '%s %s' % (value, unit or '')


def _low_lines():
    """Balances the night watchdog alerts on (it is the single source of the thresholds)."""
    from . import watchdog
    return {'DeepSeek': watchdog.MIN_DEEPSEEK}


CATEGORIES = ('付费 API', 'GPU 与集群', '数据与订阅')


def resources_page(conn):
    rows = conn.execute('SELECT * FROM resources ORDER BY service').fetchall()
    low, top, groups = _low_lines(), [], {}
    nowdt = datetime.now(db.LOCAL_TZ)
    for r in rows:
        checked = r['checked_at'] or ''
        stale = True
        if checked:
            try:
                stale = (nowdt - datetime.fromisoformat(checked.replace('Z', '+00:00'))).total_seconds() > STALE_HOURS * 3600
            except ValueError:
                pass
        money = _money(r['unit'], _md(r['balance'])) if r['balance'] else ''
        try:
            under = r['service'] in low and float(r['balance']) < low[r['service']]
        except (TypeError, ValueError):
            under = False
        if money:
            top.append('%s %s%s' % (_md(r['service']), '==%s==' % money if under else money, '（已过期）' if stale else ''))
        note = r['limit_note'] or ''
        state = (money + ('（已过期）' if stale else '') + ('；' + note if note else '')) if money else (note or '—')
        groups.setdefault(r['category'] or '其他', []).append('| %s | %s | %s | %s | %s | %s |' % tuple(_cell(x) for x in (
            r['service'], r['purpose'], r['runs_on'], r['check_method'], state, checked[:16].replace('T', ' '))))  # no 凭据 column
    out = []
    for cat in [c for c in CATEGORIES if c in groups] + sorted(set(groups) - set(CATEGORIES)):
        out += ['## ' + cat, '', '| 资源 | 用途 | 在哪跑 | 怎么查 | 余额或状态 | 查询时间 |', '|---|---|---|---|---|---|']
        out += groups[cat] + ['']
    if not rows:
        out.append('还没有资源记录。')
    summary = '> [!summary] ' + (' · '.join(top) if top else '还没有余额记录')
    return WIDE_HEAD + summary + '\n\n', '\n'.join(out).rstrip()


TASK_LOG_REL = 'task-log/'  # under db.slim_root() on the external disk, not in the vault
TASK_RECORD_PREFIX = TASK_LOG_REL + '任务记录 '


def task_record_pages(conn):
    """task-log/任务记录 YYYY-MM.md under db.slim_root(): every end-of-task record of the month, newest first, verbatim. The 07:00 agent writes
    from these; daily notes link each block as [[任务记录 2030-01#TN-20300115-01|TN-20300115-01]]."""
    months = [r[0] for r in conn.execute('SELECT DISTINCT substr(date,1,7) FROM task_notes ORDER BY 1').fetchall()]
    pages = []
    labels = (('did', '做了什么'), ('result', '结果'), ('meaning', '意味着'), ('data', '数据'), ('limits', '限制'))
    for month in months:
        notes = conn.execute("SELECT * FROM task_notes WHERE substr(date,1,7)=? ORDER BY id DESC", (month,)).fetchall()
        out = ['原始记录，由各线程在任务结束时写入，脚本照原文排版。每天 07:00 的整理从这里读。']
        for n in notes:
            meta = ['`%s`' % n['mainline'], n['date'][5:], '写入：' + _md(_one_line(n['editor']))]
            out += ['', '## ' + n['id'], '**%s**' % _md(_one_line(n['title'])), ' · '.join(meta), '']
            if n['result']:
                out.append('- **结果**：' + _md(_one_line(n['result'])))
            out += ['- %s：%s' % (label, _md(_one_line(n[k]))) for k, label in labels if n[k] and k != 'result']
            if n['exp_ids']:
                out.append('- 实验：' + ' '.join('`%s`' % c.strip() for c in re.split(r'[,，\s]+', n['exp_ids']) if c.strip()))
            cost = ' · '.join(x for x in (('花费：' + _md(_one_line(n['cost']))) if n['cost'] else '',
                                         ('用时：' + _md(_one_line(n['duration']))) if n['duration'] else '') if x)
            if cost:
                out.append('- ' + cost)
            if n['links']:
                out.append('- 链接：' + _md(_one_line(n['links'])))
            if n['detail']:
                out += ['', '详情：'] + [_quote(line) for line in n['detail'].splitlines()]
        head = '# 任务记录 %s\n\n> 本月 %d 条记录 · 最近一条 %s\n\n' % (month, len(notes), max(n['date'] for n in notes)[5:])
        pages.append((TASK_RECORD_PREFIX + month + '.md', (head, '\n'.join(out)), [n['id'] for n in notes]))
    return pages


def write_task_logs(conn, dry_run=False):
    """Render the monthly task records under db.slim_root() (plain files, no vault, no busy guard). Returns the changed rel paths.
    The disk not mounted: nothing is written and None is returned (the caller says so once a day; next round tries again)."""
    changed = []
    if not db.slim_ready():
        return None
    root = db.slim_root()
    for rel, (head, body), ids in task_record_pages(conn):
        path = root / rel
        existing = path.read_text(encoding='utf-8') if path.exists() else None
        text = _merge(existing, head, body)
        if text == existing:
            continue
        changed.append(rel)
        if dry_run:
            continue
        _write(path, text)
        log_change(None, EDITOR, rel, _summary(conn, rel, ids, created=existing is None))
    return changed


# ---- writing ----

def _merge(existing, head, body):
    block = AUTO_START + '\n' + body.rstrip() + '\n' + AUTO_END
    if existing is None:
        return head + block + '\n\n（这条线以下由你自由书写，脚本不改。）\n'
    if AUTO_START in existing and AUTO_END in existing:
        pre, rest = existing.split(AUTO_START, 1)
        post = rest.split(AUTO_END, 1)[1]
        if head and pre.startswith('---\n'):  # script owns the frontmatter + header of its own pages
            pre = head
        return pre + block + post
    return head + block + '\n\n' + (existing.split('---\n', 2)[2] if head and existing.startswith('---\n') else existing)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.%s.tmp' % path.name)
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def log_change(vault, editor, rel_path, summary):
    """One row in the ledger's 变更 table; nothing is written in the vault. The connection is the one the
    process opened last (db.connect); where the ledger cannot be opened (a VM) the line goes through the queue."""
    editor, summary = _one_line(editor), _one_line(summary)
    rel_path = rel_path[:-3] if rel_path.endswith('.md') else rel_path
    conn = db.CURRENT['conn']
    try:
        if conn is None:
            raise sqlite3.ProgrammingError('no connection')
        db.add_change(conn, editor, rel_path, summary)
    except sqlite3.Error:  # closed, or never opened in this process
        try:
            db.add_change(db.connect(db.CURRENT['path']), editor, rel_path, summary)
        except (db.HSError, sqlite3.Error):
            from . import queue
            queue.enqueue('log-change', {'editor': editor, 'path': rel_path, 'summary': summary, 'at': db.now()})


CHANGES_FEED = 'agent/管理/.changes.json'  # hidden: the r2i-attribution plugin reads it (it used to read 管理/变更记录.md)
FEED_DAYS = 30


def write_changes_feed(conn, vault, dry_run=False):
    """The last 30 days of the 变更 table as JSON for the plugin. The plugin uses stamp, editor, path (no .md) and summary of each
    entry, in order; `id` lets it remember what it has shown. Rewritten only when the entries changed. Returns True if written."""
    rows = db.get_changes(conn, since=_days_ago(FEED_DAYS), limit=5000)
    entries = [{'id': r['id'], 'at': r['at'], 'stamp': db.parse_time(r['at']).astimezone(db.LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z'),
                'editor': r['editor'], 'path': r['path'], 'summary': r['summary']} for r in rows]
    text = json.dumps({'entries': entries}, ensure_ascii=False, indent=0) + '\n'
    path = Path(vault) / CHANGES_FEED
    if path.exists() and path.read_text(encoding='utf-8') == text:
        return False
    if not dry_run:
        _write(path, text)
    return True


def changelog_text(conn, path=None, since=None, limit=200):
    """The old 变更记录 lines (「- 时间 | 编辑者 | [[笔记]] | 摘要」) from the 变更 table."""
    return ''.join(db.change_line(r) for r in db.get_changes(conn, path, since, limit))


def _summary(conn, rel, ids_now, created=False):
    key = 'rendered:' + rel
    before = set(db.meta_get(conn, key, []))
    new = [i for i in ids_now if i not in before]
    db.meta_set(conn, key, ids_now)
    table, noun = ('night_items', '项今晚可跑') if rel == night.PAGE else ('milestones', '条里程碑')
    if rel.startswith(TASK_RECORD_PREFIX):
        table, noun = 'task_notes', '条任务记录'
    if rel == exp_index.PAGE:
        return '新建页面' if created else '实验台账有更新'
    if created:
        return '新建页面' + ('，含 %d %s' % (len(new), noun) if new else '')
    if not new:
        return '更新数字或待决事项'
    rows = conn.execute('SELECT editor FROM %s WHERE id IN (%s)' % (table, ','.join('?' * len(new))), new).fetchall()
    editors = sorted({r['editor'] for r in rows})
    return '新增 %d %s，写入者：%s' % (len(new), noun, '、'.join(editors) or '未知')


HOURLY = None  # the hourly block: None = run unless under unittest; tests set True to run it


def render(conn, vault, dry_run=False, sync_entries=False):
    vault = Path(vault)
    closed = [] if dry_run else readback(conn, vault)
    nightly = {'ticks': [], 'budgets': [], 'error': None}
    waited = []
    if not dry_run:
        waited = []  # 管理/等你回复.md is gone, so there are no ticks to read
        waits.expire(conn)
        db.expire_night_items(conn)
        nightly['ticks'] = night.readback(conn, vault, _busy)
        if HOME_ON and HOME_TICKS:  # 首页 option cards (off by default)
            from . import home_ticks
            for oid, what in home_ticks.readback(conn, vault, _busy):
                if oid.startswith('NQ-'):
                    nightly['ticks'].append((oid, '已批' if what == '已批' else '撤回'))
                else:
                    closed.append(oid)
        try:  # a budget-file problem must not stop the pages from rendering
            nightly['budgets'] = night.write_budgets(conn)
        except (db.HSError, OSError, ValueError, KeyError) as e:
            nightly['error'] = str(e)
        for bid in nightly['budgets']:
            if bid.startswith('NIGHT-'):
                log_change(vault, EDITOR, night.PAGE, '夜间统一预算 %s ¥%g 写入 BUDGETS.json（R-21，09:00 作废）'
                           % (bid, config.get().night_budget))
                continue
            v = conn.execute('SELECT * FROM verbal_budgets WHERE id=?', (bid,)).fetchone()
            if v:
                what = ('撤回口头批准的预算 %s（%s）' % (bid, v['revoke_reason']) if v['status'] == '撤回' else
                        (config.say('口头批准的预算 %s ¥%g 写入 BUDGETS.json（{owner} %s「%s」）')
                         % (bid, v['cap_cny'], night._local(v['approval_at']), v['approval_quote'])
                         if v['approval_msg'] != db.V4_MSG else
                         '预算 %s ¥%g 写入 BUDGETS.json（%s）' % (bid, v['cap_cny'], v['approval_quote'])))
                log_change(vault, v['revoked_by'] if v['status'] == '撤回' else v['editor'], night.PAGE, what)
    if not dry_run:  # finished report pages leave 汇报/ before 首页 lists 汇报 pages
        from . import archive, dec_pages
        try:  # pages go to 汇报/要批/ or 汇报/同步/ by 类别 before archive looks at them; --sync-page skeletons
            nightly['kinds'] = {'moved': kinds.run(conn, vault, _busy, log_change), 'skeletons': kinds.write_skeletons(conn, vault, log_change),
                                'close_reports': kinds.write_close_reports(conn, vault, log_change),
                                'note_syncs': kinds.write_note_syncs(conn, vault, log_change)}
        except (OSError, sqlite3.Error) as e:
            nightly['kinds'] = 'error: %s' % e
        try:
            nightly['archived'] = archive.run(vault, _busy, log_change, conn=conn)
        except OSError as e:
            nightly['archived'] = 'error: %s' % e
        try:  # the 「已同步」 block of each 主线笔记
            nightly['kinds_notes'] = kinds.sync_notes(conn, vault, _busy, log_change)
        except OSError as e:
            nightly['kinds_notes'] = 'error: %s' % e
        try:  # one page per open decision, before 首页 links to them
            closed += dec_pages.readback(conn, vault, _busy)
            nightly['dec_pages'] = dec_pages.sync(conn, vault, _busy, log_change)
        except OSError as e:
            nightly['dec_pages'] = 'error: %s' % e
    if not dry_run:  # embeds on 首页 must not break: an empty page per mainline until struct-set writes v1
        try:
            made = structure.ensure_placeholders(vault, log_change)
            if made:
                nightly['struct_placeholders'] = made
        except OSError as e:
            nightly['struct_placeholders'] = 'error: %s' % e
    if not dry_run and (HOURLY if HOURLY is not None else 'unittest' not in sys.modules):
        from . import handover
        for name, step in (  # deployed code must take effect in the long-lived reply job; a dead self-check is kicked
                ('restart_if_stale_code', lambda: selfcheck.restart_if_stale_code(conn, vault, log_change)),
                ('watch_selfcheck', lambda: selfcheck.watch_selfcheck(conn, vault, log_change)),
                ('handover.gen_run', lambda: handover.gen_run(conn, vault, log_change)),  # handover gen + lint (hourly)
                ('handover.lint_run', lambda: handover.lint_run(conn, vault, log_change))):
            try:
                step()
            except Exception as e:  # one failing step must not stop render, but it is no longer silent
                nightly.setdefault('hourly_errors', []).append('%s: %s: %s' % (name, type(e).__name__, e))
                print('render hourly %s failed: %r' % (name, e), file=sys.stderr)
    if not dry_run:  # the checklist after a reboot (only once the self-check has noticed one)
        try:
            selfcheck.sync(conn, vault, _busy, log_change)
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['recovery'] = 'error: %s' % e
    if not dry_run:  # 问你 / 要你打 on a page open and close their waits, before the ⚠ scan
        try:
            nightly['autowait'] = autowait.sync(conn, vault, _busy, log_change)
            nightly['autowait']['followed'] = autowait.follow_ledger(conn, vault, _busy, log_change)
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['autowait'] = {'error': str(e)}
    if not dry_run:  # before 首页 renders; notifies/logs once per page per day, writes no report page
        try:
            nightly['unregistered'] = unregistered.run(conn, vault, _busy, log_change)
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['unregistered'] = {'error': str(e)}
    if not dry_run:
        try:
            nightly['rules'] = rules_check(conn, vault)
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['rules'] = 'error: %s' % e
        try:  # audits written under an older rules version, naming a changed class (never on 首页)
            nightly['audit_recheck'] = audits.flag_stale(conn, vault, log_change)
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['audit_recheck'] = 'error: %s' % e
        try:  # E3 observe: one 「本来会做」 line per day for the overdue rows
            if modes.get(conn, 'review-overdue') == 'observe':
                late = audit_flow.overdue(conn)
                if late:
                    modes.would(conn, vault, log_change, 'review-overdue', HOME,
                                '「给线程看」写待复核超 24 小时没审计：%s' % '、'.join(i for i, _, _ in late))
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['review_overdue'] = 'error: %s' % e
        try:  # A2: one ntfy push per answer no thread picked up in 10 minutes (observe: one 「本来会做」 line)
            rows = unclaimed_check.rows_from_db(conn, vault=vault)
            mode = modes.get(conn, 'unclaimed-push')
            if mode == 'auto':
                nightly['unclaimed_pushed'] = unclaimed_check.push_new(conn, rows)
            elif mode == 'observe' and rows:
                modes.would(conn, vault, log_change, 'unclaimed-push', HOME,
                            '首页「要你定」下写一行并推送：%s' % '、'.join(r['id'] for r in rows))
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['unclaimed_pushed'] = 'error: %s' % e
        try:  # v5a: the newest PASS A/B audit per criterion -> its reading (and, for system audits, status); observe: 「本来会做」 only
            nightly['crit_rollup'] = [p['text'] for p in crit_rollup.run(conn, vault, log_change)]
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['crit_rollup'] = 'error: %s' % e
        try:  # mechanical text-rule checks, reminders in the 变更 table only
            nightly['rulecheck'] = rulecheck.run(conn, vault, log_change)
        except (OSError, db.HSError, sqlite3.Error) as e:
            nightly['rulecheck'] = {'error': str(e)}
    pages = [(db.PROGRESS_REL[ml], (progress_page_v2(conn, ml, vault) if ml in REDESIGN_B else progress_page(conn, ml)),
              [e['id'] for e in _milestones(conn, ml, TIMELINE_RECENT)]) for ml in db.MAINLINES]
    # 进度/总览.md is no longer written (首页's mainline grid replaces it)
    pages.append((HOME, home_page(conn, watchdog_stale(vault), daily_stale(vault), reply_late=reply_stale(vault),
                                  vault=vault), []))
    pages.append(('资源.md', resources_page(conn), []))
    nhead, nbody, nids = night.page(conn)
    pages.append((night.PAGE, (nhead, nbody), nids))
    pages.append((night.HISTORY, night.history_page(conn), []))
    pages.append((exp_index.PAGE, exp_index.page(conn), []))
    if not dry_run and (HOURLY if HOURLY is not None else 'unittest' not in sys.modules) and programs.due(conn):  # V5-PROGRAM-LIST: hourly
        pages.append((programs.PAGE, programs.page(conn), []))
    changed = []
    for rel, (head, body), ids in pages:
        path = vault / rel
        if _busy(path) and not dry_run:
            continue
        existing = path.read_text(encoding='utf-8') if path.exists() else None
        if rel == programs.PAGE and existing is not None and AUTO_START not in existing:
            existing = None  # the self-check used to own this whole file; its old text is replaced, not kept below the block
        text = _merge(existing, head, body)
        if text == existing or (existing is not None and STAMP_RE.sub('', text) == STAMP_RE.sub('', existing)):
            continue
        changed.append(rel)
        if dry_run:
            continue
        _write(path, text)
        log_change(vault, EDITOR, rel, _summary(conn, rel, ids, created=existing is None))
    if not dry_run:  # proves each hourly run could write the vault; dotfiles are not shown or synced by Obsidian
        _write(vault / 'agent' / '管理' / '.r2i-sync-heartbeat', datetime.now(db.LOCAL_TZ).isoformat(timespec='seconds') + '\n')
        nightly['notified'] = waits.notify(conn)
    nightly['waits_ticked'] = waited
    if not dry_run:
        write_changes_feed(conn, vault)
    try:  # the task records live on the external disk now
        logs = write_task_logs(conn, dry_run)
        if logs is None:
            day = datetime.now(db.LOCAL_TZ).strftime('%Y-%m-%d')
            if db.meta_get(conn, 'slim-skip') != day:  # one line per day in the run log, not one per minute
                nightly['task_log'] = db.slim_skip_line('任务记录')
                if not dry_run:
                    db.meta_set(conn, 'slim-skip', day)
        else:
            changed += logs
    except OSError as e:
        nightly['task_log'] = 'error: %s' % e
    out = {'changed': changed, 'decisions_closed': closed, 'night': nightly}
    if sync_entries and not dry_run:
        from . import entries
        try:  # an entry-file problem must not stop the pages from rendering
            out['entries'] = entries.sync(vault)
        except OSError as e:
            out['entries'] = {'changed': [], 'skipped': [], 'error': str(e)}
    return out
