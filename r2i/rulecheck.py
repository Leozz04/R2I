"""mechanical checks of text rules. They only remind (R-52: observe first); nothing is ever blocked.

Mode 'rulecheck' (modes.py; default observe): off = does not run; observe and auto = the same thing, one 变更 line per
item per local day, 「本来会提醒：R-xx …」.

Implemented (data that exists today):
  R-03  a top-level 汇报/ page touched in the last 3 days whose 「## 结果」 table lacks a 时间 or a 花费 column.
  R-32  two .md files with the same name in the vault (not in hidden folders, 归档/, _old/); scanned once per day.
  R-31  a paid_run start record (the paid_run ledger) whose cwd and argv paths are all outside
        <runs_root>/ (the start record has no output field; cwd + argv is all we know).
        (The record does not say which host; a rented GPU host may legitimately differ, hence only a reminder.)
  规则格式  (mode 'rule-format', I1) the format of 项目规则.md, one hit per rule or per version row: a rule is
        at most 3 physical lines; it ends with exactly one backticked tag, 文字 / 程序 / 权限 (v2: no
        department, no date); every row of the 版本记录 table has a 谁批的; version numbers are
        consecutive (v1.1, v1.2, v1.3; a merge may go to (x+1).0).
The ledger path: env R2I_LEDGER, else the live one; the first look only records the end of the file. At most 20 lines a day.
TODO (no clear data source yet, not implemented): R-02 (code name first seen without a plain name: needs a code ->
plain-name table).
The ledger is read incrementally (byte offset kept in meta), the vault-wide scan runs once a day."""
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import os
import re
import time

from . import config, db, modes

EDITOR = 'r2i-sync 规则检查'
RECENT_DAYS = 3
MAX_LINES_PER_DAY = 20  # all rules together; the rest is summed up in one line
LEDGER_ENV = 'R2I_LEDGER'


def _front(text):
    from . import reply_watch
    return reply_watch.front(text)


def result_table_header(text):
    """Header cells of the first table under 「## 结果」, or None when there is no such table."""
    m = re.search(r'(?ms)^## 结果[ \t]*\n(.*?)(?=^## |\Z)', text)
    if not m:
        return None
    lines = m.group(1).split('\n')
    for i, l in enumerate(lines):
        if l.strip().startswith('|') and i + 1 < len(lines) and re.match(r'\s*\|[\s:|-]+\|?\s*$', lines[i + 1]):
            return [c.strip() for c in l.strip().strip('|').split('|')]
    return None


def check_r04(text):
    head = result_table_header(text)
    if head is None:
        return None
    missing = [w for w in ('时间', '花费') if not any(w in c for c in head)]
    return '结果表缺「%s」列' % '」「'.join(missing) if missing else None


def report_hits(vault, now):
    folder = Path(vault) / db.REPORT_DIR
    out = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob('*.md')):
        try:
            if now - path.stat().st_mtime > RECENT_DAYS * 86400:
                continue
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        rel = '%s/%s' % (db.REPORT_DIR, path.name)
        props = _front(text)
        for rule, why in (('R-03', check_r04(text)),):
            if why:
                out.append((rule, rel, why))
    return out


def duplicate_names(vault):
    seen = {}
    root = Path(vault)
    for p in root.rglob('*.md'):
        parts = p.relative_to(root).parts[:-1]
        if any(x.startswith('.') or x.startswith('_') or x == '归档' for x in parts):
            continue
        seen.setdefault(p.name, []).append(p.relative_to(root).as_posix())
    return {n: ps for n, ps in seen.items() if len(ps) > 1}


def ledger_hit(record):
    """[(rule, why)] for one paid_run start record."""
    out = []
    argv = [str(a) for a in (record.get('argv') or [])]
    paths = [str(record.get('cwd') or '')] + [a for a in argv if a.startswith('/')]
    if not any(p.startswith(str(config.get().runs_root) + '/') for p in paths):
        out.append(('R-31', '付费运行 %s 的 cwd 和参数里的路径都不在 %s/ 下' % (str(record.get('run_id'))[:8], config.get().runs_root)))
    return out


def ledger_hits(conn, ledger=None):
    """Start records appended since the last look (offset kept in meta). Returns [(rule, rel, why)]."""
    path = Path(ledger or os.environ.get(LEDGER_ENV) or config.paid_dir() / 'ledger.jsonl')
    try:
        size = path.stat().st_size
    except OSError:
        return []
    pos = db.meta_get(conn, 'rulecheck-ledger-pos')
    if pos is None:  # first look: only records appended from now on are checked, never the history
        db.meta_set(conn, 'rulecheck-ledger-pos', size)
        return []
    if pos > size:
        pos = 0
    if pos == size:
        return []
    out = []
    try:
        with path.open('rb') as fh:
            fh.seek(pos)
            data = fh.read()
    except OSError:
        return []
    end = data.rfind(b'\n') + 1  # a half-written last line waits for the next round
    for line in data[:end].decode('utf-8', 'replace').split('\n'):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get('event') == 'start':
            out += [(rule, 'paid_run ledger', why) for rule, why in ledger_hit(rec)]
    db.meta_set(conn, 'rulecheck-ledger-pos', pos + end)
    return out


RULE_PAGES = ('项目规则.md',)
RULE_ID = re.compile(r'^- \*\*([A-Z]-\d+[a-z]?) ')
MAX_RULE_LINES = 3
TAG_WORDS = ('文字', '程序', '权限')
VER = re.compile(r'v(\d+)(?:\.(\d+))?')


def rule_page(vault):
    for rel in RULE_PAGES:
        if (Path(vault) / rel).is_file():
            return rel
    return None


def rule_blocks(text):
    """[(id, [physical lines])]: a rule is its '- **ID ' line plus the following lines up to a blank line, bullet, heading, table or quote."""
    out, cur = [], None
    for l in text.split('\n'):
        m = RULE_ID.match(l)
        if m:
            cur = (m.group(1), [l])
            out.append(cur)
        elif cur and l.strip() and not re.match(r'\s*([-#|>]|\d+\.)', l):
            cur[1].append(l)
        else:
            cur = None
    return out


def check_rule(lines):
    """Reasons (strings) why one rule breaks the format."""
    why = []
    if len(lines) > MAX_RULE_LINES:
        why.append('占 %d 行（最多 %d 行）' % (len(lines), MAX_RULE_LINES))
    m = re.search(r'`([^`]*)`\s*$', ' '.join(l.strip() for l in lines))
    tag = m.group(1) if m else ''
    if not tag:
        why.append('末尾没有执行方式标签（文字、程序或权限）')
    elif tag not in TAG_WORDS:
        why.append('末尾标签「%s」不是文字、程序或权限之一（只写一个，不写部门和日期）' % tag)
    return why


def _ver(cell):
    vs = VER.findall(cell)
    return (int(vs[-1][0]), int(vs[-1][1] or 0)) if vs else None  # a range 「v1–v1.4」 counts as its upper end


def check_versions(text):
    """[(label, why)] for the 版本记录 table: a row without 谁批的, a duplicate version, a jump."""
    m = re.search(r'(?ms)^## 版本记录[ \t]*\n(.*?)(?=^#{2,6} |\Z)', text)  # not the 「### … 并掉的条」 table under it
    if not m:
        return []
    rows = [[c.strip() for c in l.strip().strip('|').split('|')] for l in m.group(1).split('\n') if l.strip().startswith('|')]
    if len(rows) < 3:
        return []
    ai = next((i for i, c in enumerate(rows[0]) if '谁批' in c or '批准' in c), None)
    out, vers = [], []
    for r in rows[2:]:
        if ai is None or len(r) <= ai or r[ai].strip('—-– ') == '':
            out.append((r[0], '版本记录这一行没有批准来源（谁批的）'))
        v = _ver(r[0])
        if v:
            vers.append((v, r[0]))
    vers.sort()
    for (a, _), (b, label) in zip(vers, vers[1:]):
        if a == b:
            out.append((label, '版本号重复'))
        elif b != (a[0], a[1] + 1) and not (b[1] == 0 and b[0] == a[0] + 1):
            out.append((label, '版本号不连续：v%d.%d 的下一个是 v%d.%d' % (a + b)))
    return out


def format_hits(vault):
    """[(rule, rel, why)] for the rule page (I1)."""
    rel = rule_page(vault)
    if not rel:
        return []
    try:
        text = (Path(vault) / rel).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for rid, lines in rule_blocks(text):
        why = check_rule(lines)
        if why:
            out.append(('规则格式', rel, '%s：%s' % (rid, '；'.join(why))))
    return out + [('规则格式', rel, '%s：%s' % (label, why)) for label, why in check_versions(text)]


def run(conn, vault, log_change, now=None, ledger=None):
    """Returns {'mode', 'hits': [(rule, rel, why)], 'logged': n}. Writes only 变更 lines."""
    mode = modes.get(conn, 'rulecheck')
    out = {'mode': mode, 'hits': [], 'logged': 0}
    fmt = modes.get(conn, 'rule-format')
    if (mode == 'off' and fmt == 'off') or not vault:
        return out
    now = now or time.time()
    day = datetime.now(db.LOCAL_TZ).strftime('%Y-%m-%d')
    hits = []
    if mode != 'off':
        hits = report_hits(vault, now)
        if db.meta_get(conn, 'rulecheck-r37-day') != day:  # the vault-wide walk: once a day
            db.meta_set(conn, 'rulecheck-r37-day', day)
            for name, ps in sorted(duplicate_names(vault).items()):
                ps = sorted(ps)  # walk order differs by filesystem; name the same file everywhere
                hits.append(('R-32', ps[0], '文件名「%s」在库里出现 %d 次：%s' % (name, len(ps), '、'.join(ps))))
        hits += ledger_hits(conn, ledger)
    if fmt != 'off':  # observe and auto alike: it only ever reminds
        hits += format_hits(vault)
    count = db.meta_get(conn, 'rulecheck-count') or {}
    used = count.get('n', 0) if count.get('day') == day else 0
    over = 0
    for rule, rel, why in hits:
        out['hits'].append((rule, rel, why))
        key = 'rulecheck:' + hashlib.md5(('%s|%s|%s' % (rule, rel, why)).encode('utf-8')).hexdigest()[:16]
        if db.meta_get(conn, key) == day:
            continue
        db.meta_set(conn, key, day)
        if used >= MAX_LINES_PER_DAY:  # seen, but not written one by one
            over += 1
            continue
        used += 1
        log_change(vault, EDITOR, rel, '本来会提醒：%s %s' % (rule, why))
        out['logged'] += 1
    if over:
        log_change(vault, EDITOR, '项目规则', '另有 %d 条，没逐条写（规则检查每天最多写 %d 条）' % (over, MAX_LINES_PER_DAY))
        out['over'] = over
    db.meta_set(conn, 'rulecheck-count', {'day': day, 'n': used})
    return out
