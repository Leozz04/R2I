"""v5a crit-rollup: audits -> criteria. Runs in the per-minute render round, switch `crit-rollup` (observe: 「本来会做」 lines only).

Per criterion the newest result audit (PASS, grade A/B, not taken back, with an `after`) is rolled up:
  - its reading goes to criteria.reading/reading_label/reading_at/reading_note (kind 验收 for a system audit on the approved
    criteria set, else 基线: another method's or a tool's number is shown as a baseline, never as our result);
  - status moves only for subject=system + on_crit_set=yes: A and the reading meets the target -> 满足, B -> at most 部分.
    Status never goes down and 被反证 is never touched. An unparseable `after` changes nothing.
An audit older than the criterion's last update is skipped (a later crit-read / crit-set / rollup wins), so a run that
changes nothing writes nothing.
"""
import re

from . import audits, db, modes

NAME = 'crit-rollup'
NUM_RE = re.compile(r'(\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?))?\s*(%)?')
RANK = {'未验证': 0, '部分': 1, '满足': 2}


def parse_reading(text, scale):
    """The last number of `text`: 「3/50」 -> 3 on a count scale (scale > 1) else 0.06; 「7.9%」 -> 0.079 on a 0-1 scale;
    「方向类 2/4=0.50」 -> 0.50; 「¥1.23/批」 -> 1.23. None when there is none (or 「无:理由」)."""
    found = NUM_RE.findall(text or '')
    if not found or (text or '').strip().startswith('无'):
        return None
    a, b, pct = found[-1]
    a, big = float(a), bool(scale and scale > 1)
    if b:
        return a if big else (a / float(b) if float(b) else None)
    return a / 100 if pct and not big else a


def meets(value, target, direction):
    return target is not None and (value <= target if direction == 'lower' else value >= target)


# audit-edit note marks: never a reading, not even a [基线] one
SKIP_MARKS = ('[开发集诊断 R-02]', '[留出集决策，数字不作证据]', '[冷启动检查，非实验]')


def plan(conn):
    """[{'crit', 'mainline', 'audit', 'set': {column: new value}, 'text'}] for every criterion the newest audit would change."""
    out = []
    for c in conn.execute('SELECT * FROM criteria ORDER BY id').fetchall():
        a = conn.execute("SELECT * FROM audits WHERE (',' || REPLACE(COALESCE(crit,''), ' ', '') || ',') LIKE '%,' || ? || ',%' AND kind='result' AND verdict='PASS' AND grade IN ('A','B') "
                         "AND undo_status!=? AND COALESCE(after,'')!='' AND after NOT LIKE '无%' "
                         + ''.join(" AND COALESCE(note,'') NOT LIKE '%s%%'" % m for m in SKIP_MARKS) +
                         " ORDER BY created_at DESC, id DESC LIMIT 1", (c['id'], audits.UNDONE)).fetchone()
        if not a or a['created_at'] < c['updated_at']:
            continue
        scale = c['scale_max'] or c['target']
        value = parse_reading(a['after'], scale)
        if value is None:
            continue
        mine = a['subject'] == 'system' and a['on_crit_set'] == 'yes'
        status = c['status']
        if mine and c['status'] in RANK:
            want = '满足' if a['grade'] == 'A' and meets(value, c['target'], c['direction']) else '部分' if a['grade'] == 'B' else status
            status = want if RANK[want] > RANK[status] else status
        new = {'reading': value, 'reading_label': a['after'], 'reading_at': audits.local_date(a['created_at']),
               'reading_note': '来自 %s（%s 级 %s）' % (a['id'], a['grade'], '系统验收' if mine else '基线'),
               'reading_kind': '验收' if mine else '基线', 'status': status}
        diff = {k: v for k, v in new.items() if (abs(v - c[k]) > 1e-12 if k == 'reading' and c[k] is not None else v != c[k])}
        if diff:
            text = '验收 %s 读数 %s → %s%s（%s）' % (c['id'], c['reading_label'] or c['reading'] or '—', a['after'],
                                              '，状态 %s → %s' % (c['status'], status) if 'status' in diff else '', a['id'])
            out.append({'crit': c['id'], 'mainline': c['mainline'], 'audit': a['id'], 'set': diff, 'text': text})
    return out


def run(conn, vault, log_change):
    mode = modes.get(conn, NAME)
    if mode == 'off':
        return []
    done = plan(conn)
    for p in done:
        rel = db.PROGRESS_REL[p['mainline']]
        if mode == 'observe':
            modes.would(conn, vault, log_change, NAME, rel, p['text'])
            continue
        cols = dict(p['set'], updated_at=db.now())
        conn.execute('UPDATE criteria SET %s WHERE id=?' % ', '.join(k + '=?' for k in cols), list(cols.values()) + [p['crit']])
        conn.commit()
        log_change(vault, modes.EDITOR, rel, p['text'])
    return done
