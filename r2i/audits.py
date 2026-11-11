"""审计记录.

One row of `audits` = one thing the audit threads did: `result` (audited one experiment) or `decided` (the audit decided
something itself). audit-add / audit-undo-done write it (directly, --queue or the inbox); daily.py hands the previous
local day to the 07:00 agent as ready-made markdown lines (sections()), and reply_watch reads the owner's 「改」 ticks back
(TICK_RE). The 每日/ note stays the agent's text: this module never writes a vault page.
"""
import random
import re
from datetime import timedelta, timezone
from pathlib import Path

from . import db, modes
from . import config

KINDS = ('result', 'decided')
VERDICTS = ('PASS', 'FAIL', '步骤错', '不确定')
GRADES = ('A', 'B', 'C', 'D')
TICKED, UNDONE = '已勾改', '已撤回'
SRC = '审计改'  # waits.src of the wait a ticked 「改」 opens
SAMPLE_N = 2
BEFORE_AFTER_RE = re.compile(r'^(\d+/\d+|无[:：].+)$')  # N/M, or 「无:理由」 where there is no number
SUBJECTS = ('system', 'tool', 'baseline')  # what the audited result is about: our system, a tool, or another method's baseline
EDITABLE = ('before', 'after', 'title', 'note', 'editor', 'decision')  # audit-edit; decision only on kind=decided (作废 voids a duplicate)
VOID = '作废'
# 「- [x] 改 · … ^aud-20300115-01」: Tasks may put its ✅ date before the block id
TICK_RE = re.compile(r'^[ \t]*- \[(.)\][ \t]*改\b.*?\^aud-(\d{8}-\d+)[ \t]*$', re.M)
TITLE_MAX = 60
DECISION_MAX = 120


def _clean(text):
    return ' '.join(str(text).split()) if text is not None and str(text).strip() else None


def check(v):
    """Validate and normalise audit-add arguments (also at queue time, so a bad item fails now). Returns a new dict."""
    out = {k: _clean(v.get(k)) for k in ('mainline', 'experiment', 'title', 'crit', 'before', 'after', 'verdict', 'grade',
                                          'decision', 'editor', 'thread', 'subject', 'on_crit_set')}
    ev = v.get('evidence')
    items = ev if isinstance(ev, (list, tuple)) else str(ev or '').split('\n')
    out['evidence'] = '\n'.join(x for x in (_clean(i) for i in items) if x) or None
    out['kind'] = (v.get('kind') or 'result').strip()
    db._check(out['mainline'], db.MAINLINES, 'mainline')
    db._check(out['kind'], KINDS, 'kind')
    if not out['title'] or len(out['title']) > TITLE_MAX:
        raise db.HSError('--title: one plain sentence, at most %d characters' % TITLE_MAX)
    if not out['editor']:
        raise db.HSError('--editor is required (the audit thread title)')
    for k in ('before', 'after'):
        if out[k] and not BEFORE_AFTER_RE.match(out[k]):
            raise db.HSError('--%s must look like N/M (or 无:<理由>), got %r' % (k, out[k]))
    if out['subject']:
        db._check(out['subject'], SUBJECTS, 'subject')
    if out['on_crit_set']:
        db._check(out['on_crit_set'], ('yes', 'no'), 'on-crit-set')
    if out['verdict']:
        db._check(out['verdict'], VERDICTS, 'verdict')
    if out['grade']:
        db._check(out['grade'], GRADES, 'grade')
    cost = v.get('cost_cny', v.get('cost'))
    try:
        out['cost_cny'] = None if cost in (None, '') else float(str(cost).replace('¥', '').replace('￥', '').strip())
    except ValueError:
        raise db.HSError('--cost must be an amount in CNY, got %r' % cost)
    if out['cost_cny'] is not None and out['cost_cny'] < 0:
        raise db.HSError('--cost cannot be negative')
    if out['kind'] == 'result':
        missing = [k for k in ('experiment', 'crit', 'verdict', 'grade') if not out[k]]
        if missing:
            raise db.HSError('kind=result needs --%s' % ', --'.join(missing))
        if out['decision']:
            raise db.HSError('--decision is only for kind=decided')
    else:
        if not out['decision'] or len(out['decision']) > DECISION_MAX:
            raise db.HSError('kind=decided needs --decision: what was decided, one sentence of at most %d characters' % DECISION_MAX)
        if not out['thread']:
            raise db.HSError(config.say("kind=decided needs --thread (cc_<uuid>): {owner}'s 「改」 reaches this thread"))
    if out['thread']:
        db.check_wait_thread(out['thread'])
    return out


REQUIRED = (('crit', '--crit'), ('before', '--before'), ('after', '--after'), ('subject', '--subject'), ('on_crit_set', '--on-crit-set'))


def required(v):
    """crit-required: why a normalised result audit is incomplete (None when it is complete or not a result)."""
    miss = [flag for k, flag in REQUIRED if not v[k]] if v['kind'] == 'result' else []
    return 'audit-add (result) needs %s (--before/--after may be 无:<理由>)' % ', '.join(miss) if miss else None


def add(conn, *, at=None, rules_version=None, **vals):
    v = check(vals)
    at = at or db.now()
    day = db.parse_time(at).astimezone(db.LOCAL_TZ).strftime('%Y%m%d')
    aid = db._next_id(conn, 'audits', 'AUD-%s-' % day)
    from .render import log_change
    modes.require(conn, log_change, 'crit-required', '审计记录/' + aid, required(v), aid)  # auto: refused before anything is written
    conn.execute('INSERT INTO audits(id,mainline,experiment,title,crit,before,after,verdict,grade,cost_cny,kind,decision,'
                 'evidence,editor,thread,created_at,rules_version,subject,on_crit_set) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (aid, v['mainline'], v['experiment'], v['title'], v['crit'], v['before'], v['after'], v['verdict'],
                  v['grade'], v['cost_cny'], v['kind'], v['decision'], v['evidence'], v['editor'], v['thread'], at,
                  rules_version or None, v['subject'], v['on_crit_set']))
    conn.commit()
    return aid


def check_edit(fields, reason):
    """audit-edit: only before/after/title/note, one-line values (empty clears; the title cannot be empty). Returns {field: value|None}."""
    if not fields:
        raise db.HSError('audit-edit needs at least one --field k=v (one of: %s)' % ', '.join(EDITABLE))
    bad = [k for k in fields if k not in EDITABLE]
    if bad:
        raise db.HSError('audit-edit cannot change %s; editable: %s' % (', '.join(bad), ', '.join(EDITABLE)))
    out = {k: _clean(v) for k, v in fields.items()}
    if 'title' in out and (not out['title'] or len(out['title']) > TITLE_MAX):
        raise db.HSError('title: one plain sentence, at most %d characters, not empty' % TITLE_MAX)
    if 'editor' in out and not out['editor']:
        raise db.HSError('editor cannot be empty')
    if 'decision' in out and (not out['decision'] or len(out['decision']) > DECISION_MAX):
        raise db.HSError('decision: one sentence, at most %d characters, not empty (作废 voids a duplicate)' % DECISION_MAX)
    if len(' '.join(str(reason or '').split())) < 8:
        raise db.HSError('--reason is required: why the record is wrong, at least 8 characters')
    return out


def _is_maintainer(editor):
    """True when `editor` starts with the configured `maintainer` (empty config = nobody has the extra right)."""
    m = config.get().maintainer
    return bool(m and editor and editor.startswith(m))


def edit(conn, aid, fields, reason, editor=None, thread=None):
    """Fix before/after/title/note/editor of one audit. Only the audit thread that wrote it (--thread = the row's thread, or --editor =
    the row's editor) or an editor whose name starts with the config `maintainer` (when set) may; every changed field gets a
    变更 line with the old value and the reason."""
    vals = check_edit(fields, reason)
    row = conn.execute('SELECT * FROM audits WHERE id=?', (aid,)).fetchone()
    if not row:
        raise db.HSError('no audit %s' % aid)
    editor, thread = _clean(editor), _clean(thread)
    if not ((thread and thread == row['thread']) or (editor and editor == row['editor']) or _is_maintainer(editor)):
        raise db.HSError('audit-edit: only the audit thread that wrote %s (--thread %s / --editor %s) or the maintainer thread (config `maintainer`) may edit it'
                         % (aid, row['thread'] or '—', row['editor']))
    if 'decision' in vals and row['kind'] != 'decided':
        raise db.HSError('decision is only for kind=decided; %s is %s' % (aid, row['kind']))
    why, done = _clean(reason), []
    for k, new in vals.items():
        if new == row[k]:
            continue
        conn.execute('UPDATE audits SET %s=? WHERE id=?' % k, (new, aid))
        db.add_change(conn, editor or thread, '审计记录/' + aid, 'audit-edit %s 的 %s：「%s」→「%s」（理由：%s）' % (aid, k, row[k] or '', new or '', why))
        done.append(k)
    conn.commit()
    return {'id': aid, 'changed': done}


RULES_REL = '项目规则.md'
RECHECK_DAYS = 3  # an audit counts as still running for this long after it was written
RULE_REF_RE = re.compile(r'R-(\d)[\dx]')


def version_key(text):
    """'v2.1' -> Decimal('2.1'): rules versions read as decimals (v1.4 < v2.0 == v2 < v2.1), None without digits."""
    from decimal import Decimal
    m = re.search(r'\d+(?:\.\d+)?', text or '')
    return Decimal(m.group()) if m else None


def rules_state(vault):
    """{'version': 'v9.1', 'n': Decimal('9.1'), 'classes': {'0', '8'}} from 项目规则.md, or None.
    The classes are those of the LAST row of 「## 版本记录」: after 「改动类：」 the R-Nx tokens (R-0x -> class 0); without that
    marker the first digits of the R-NN ids in the 「改了哪几条」 cell."""
    from . import reply_watch
    try:
        text = (Path(vault) / RULES_REL).read_text(encoding='utf-8')
    except (OSError, TypeError, UnicodeDecodeError):
        return None
    version = reply_watch.front(text).get('version') or ''
    n = version_key(version)
    if n is None:
        return None
    sec = re.search(r'(?ms)^## 版本记录[ \t]*\n(.*?)(?=^## |\Z)', text)
    rows = [l for l in (sec.group(1).split('\n') if sec else []) if l.strip().startswith('|')
            and not re.match(r'\|\s*(版本\s*\||-)', l.strip())]
    classes = set()
    if rows:
        cells = [c.strip() for c in rows[-1].strip().strip('|').split('|')]
        cell = cells[2] if len(cells) > 2 else ''
        mk = re.search(r'改动类[:：]\s*([^。；;]*)', cell)
        classes = {d for d in RULE_REF_RE.findall(mk.group(1))} if mk else set()
        classes = classes or set(RULE_REF_RE.findall(cell))
    return {'version': 'v%s' % re.search(r'\d+(?:\.\d+)?', version).group(), 'n': n, 'classes': classes}


def uses_class(row, classes):
    """The audit's own text (title, experiment, crit, decision, evidence) names a rule R-NN / R-Nx of a changed class.
    Audits carry no rule-class field, so this is the whole test: no rule named, no flag."""
    text = ' '.join(str(row[k] or '') for k in ('title', 'experiment', 'crit', 'decision', 'evidence', 'verdict'))
    return bool(set(RULE_REF_RE.findall(text)) & classes)


def flag_stale(conn, vault, log_change, now=None):
    """The rules table was bumped. An audit written under an older version (rules_version) in the last
    RECHECK_DAYS days, not taken back, that names a rule of a changed class gets recheck = 「要按 vN 复核」. It is written
    to the audits row and to the 变更 table, once per version; 首页 never shows it and nothing running is cancelled.
    Returns the flagged audit ids."""
    st = rules_state(vault)
    if not st or not st['classes']:
        return []
    label = '要按 %s 复核' % st['version']
    since = (db.parse_time(now or db.now()) - timedelta(days=RECHECK_DAYS)).astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    out = []
    for r in conn.execute("SELECT * FROM audits WHERE undo_status!=? AND created_at>=? AND COALESCE(rules_version,'')!='' "
                          "AND COALESCE(recheck,'')!=? ORDER BY id", (UNDONE, since, label)).fetchall():
        n = version_key(r['rules_version'])
        if n is None or n >= st['n'] or not uses_class(r, st['classes']):
            continue
        conn.execute('UPDATE audits SET recheck=? WHERE id=?', (label, r['id']))
        out.append(r['id'])
        log_change(vault, '人审计', '审计记录/' + r['id'], '%s 写于规则 %s，规则升到 %s（改动类 %s），%s'
                   % (r['id'], r['rules_version'], st['version'], '、'.join('R-%sx' % c for c in sorted(st['classes'])), label))
    conn.commit()
    return out


def undo_done(conn, aid):
    """The audit thread finished taking a decision back: 已勾改 -> 已撤回, and the wait the tick opened closes."""
    row = conn.execute('SELECT * FROM audits WHERE id=?', (aid,)).fetchone()
    if not row:
        raise db.HSError('no audit %s' % aid)
    if row['undo_status'] != TICKED:
        raise db.HSError(config.say('%s is not waiting for an undo (undo_status=%r; {owner} has not ticked 「改」)') % (aid, row['undo_status']))
    conn.execute('UPDATE audits SET undo_status=? WHERE id=?', (UNDONE, aid))
    closed = conn.execute("UPDATE waits SET status='已回', closed_at=?, closed_by='audit-undo-done' "
                          "WHERE status='等待' AND src=? AND ask LIKE ?", (db.now(), SRC, '%' + aid + '%')).rowcount
    conn.commit()
    return {'id': aid, 'undo_status': UNDONE, 'closed_waits': closed}


def mark_ticked(conn, aid):
    """the owner ticked 「改」. Returns the audit row (dict) the first time only, else None (unknown, not decided, already ticked)."""
    row = conn.execute('SELECT * FROM audits WHERE id=?', (aid,)).fetchone()
    if not row or row['kind'] != 'decided' or row['undo_status']:
        return None
    conn.execute('UPDATE audits SET undo_status=? WHERE id=?', (TICKED, aid))
    conn.commit()
    return dict(row)


def local_date(ts):  # the configured local date
    return db.parse_time(ts).astimezone(db.LOCAL_TZ).strftime('%Y-%m-%d')


def block_id(aid):
    return '^' + aid.lower()


def _cell(text):
    return (' '.join(str(text or '').split()).replace('|', '／')) or '—'


def _links(evidence):
    out = []
    for i, e in enumerate((evidence or '').split('\n'), 1):
        if e.startswith('http'):
            out.append('[链接%d](%s)' % (i, e))
        elif e:
            out.append(e)
    return '；'.join(out)


def sample(ids, day, n=SAMPLE_N):
    """The same `n` ids for the same day and the same set of ids (seed = the date)."""
    ids = sorted(ids)
    return sorted(random.Random(day).sample(ids, min(n, len(ids))))


def _recheck(r):
    try:
        return r['recheck'] or ''
    except (KeyError, IndexError):
        return ''


def sections(rows, day):
    """The two 每日笔记 sections for local date `day`, from audits rows (dicts). Returns
    {'lines': [markdown lines], 'results': {mainline: [ids]}, 'decided': [ids], 'sample': [ids]}."""
    mine = sorted((r for r in rows if local_date(r['created_at']) == day), key=lambda r: r['id'])
    results = [r for r in mine if r['kind'] == 'result']
    decided = [r for r in mine if r['kind'] == 'decided' and not (r['decision'] or '').startswith(VOID)]  # a voided duplicate is not a decision
    lines = ['## 昨天审计了什么', '']
    for ml in db.MAINLINES:
        lines.append('### ' + db.SHORT_NAMES[ml])
        rs = [r for r in results if r['mainline'] == ml]
        if not rs:
            lines += ['没动', '']
            continue
        lines += ['', '| 实验 | 对着哪条目标 | 结论 | 证据档 | 花费 | 链接 |', '|---|---|---|---|---:|---|']
        for r in rs:
            ba = ' %s → %s' % (r['before'] or '—', r['after'] or '—') if r['before'] or r['after'] else ''
            cost = '¥%g' % r['cost_cny'] if r['cost_cny'] is not None else '未记录'
            rc = _recheck(r)
            lines.append('| %s | %s | %s | %s | %s | %s |' % (
                _cell('`%s` %s' % (r['experiment'], r['title'])), _cell(r['crit']), _cell((r['verdict'] or '') + ba + (' · ' + rc if rc else '')),
                _cell(r['grade']), cost, _cell(_links(r['evidence']))))
        lines.append('')
    lines += ['## 审计自己定了什么', '']
    picked = sample([r['id'] for r in decided], day)
    if not decided:
        lines += ['昨天审计没有自己定事情。', '']
    for r in decided:
        ev = _links(r['evidence'])
        lines.append('- [ ] 改 · %s · %s（%s）%s %s' % (db.SHORT_NAMES[r['mainline']], _cell(r['decision']), _cell(r['title']),
                                                      ' · ' + ev if ev else '', block_id(r['id'])))
    if decided:
        by = {r['id']: r for r in decided}
        lines += ['', '> [!question] 请抽查（随机 %d 件，种子 %s）' % (len(picked), day)]
        lines += ['> - %s · %s' % (i, _cell(by[i]['decision'])) for i in picked]
        lines.append('')
    return {'lines': lines, 'results': {ml: [r['id'] for r in results if r['mainline'] == ml] for ml in db.MAINLINES},
            'decided': [r['id'] for r in decided], 'sample': picked}
