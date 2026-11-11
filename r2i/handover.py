"""Handover lint and generated handover sections. R-52: both start in observe.

handover-gen: the 「## 一、脚本生成（handover-gen 写，不要手改）」 half of each vault agent/交接/<主线> 交接.md, built from the
ledger: goal and criteria, running / pending experiments, decisions (open and last 3 days), open waits, budgets, stops.
  observe: the page is not touched; the text goes to <project>/.r2i/handover-preview/<主线>.md and the 变更 table
           gets 「本来会做：…」 once per change.   auto: the section is replaced in place.
handover-lint: required sections, 更新于 within 6 h, 「按规则 vN 开工」 = the rules version now, size <= 15k tokens.
  It only reports (变更 line when the findings change; `python3 -B -m r2i.handover lint [--file F]`).
Both run at most once an hour from render."""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, modes
from .ids import THREAD_FIND
from .audits import version_key
from . import config

PAGES = db.HANDOVER_REL
GEN_HEAD = '## 一、脚本生成（handover-gen 写，不要手改）'
PREVIEW = config.state_dir() / 'handover-preview'
RUNNING = ('进行中', '方案准备', '拟议未执行', '已运行待复核')
REQUIRED = ('现在的判断', '下一步候选', '已经证伪', '风险', '叫停或让等的事', '在等谁')
TOKEN_MAX = 15000
FRESH_HOURS = 6
EDITOR = 'r2i-sync 脚本'
EVERY = timedelta(hours=1)
BACKUP_DIR = config.state_dir() / 'page-backups'
APPEND_MAX = 8000
GEN_TITLE = '一、脚本生成'


def _rows(conn, sql, *p):
    return [dict(r) for r in conn.execute(sql, p).fetchall()]


def _short(s, n=80):
    s = ' '.join((s or '').split())
    return s if len(s) <= n else s[:n - 1] + '…'


LIVE_DAYS = 7  # 在跑 lists only experiments last touched this recently (see _last_touched)
AUDITS_SHOWN = 3


def _num(x, nd=3):
    return ('%.' + str(nd) + 'g') % x


def _distance(c):
    """'差 …' / '已达标' / '—': how far the reading is from the target, in the target label's own unit."""
    r, t, d, lab = c.get('reading'), c.get('target'), c.get('direction'), c.get('target_label') or ''
    if not isinstance(r, (int, float)) or not isinstance(t, (int, float)) or d not in ('higher', 'lower'):
        return '—'
    gap = t - r if d == 'higher' else r - t
    if gap <= 1e-9:
        return '已达标'
    m = re.fullmatch(r'\D*(\d+)\s*/\s*(\d+)\s*', lab)
    if m and c.get('scale_max') == float(m.group(2)):  # a count out of N: the values are the numerators
        return '差 %s/%s' % (_num(gap), m.group(2))
    if '%' in lab:  # a fraction ledger (scale_max <= 1) stores 0.05 for 5%
        return '差 %s 个百分点' % _num(gap * (100 if (c.get('scale_max') or 0) <= 1 else 1))
    return '差 %s%s' % ('¥' if '¥' in lab else '', _num(gap))


def _provenance(c, auds):
    """'system' (the reading is backed by a subject='system' audit) / '基线' / '来源不明'.
    The newest audit of this criterion that carries a subject decides; audits.subject may not exist yet (NULL = no subject),
    then the criterion's own reading_kind / reading_label / reading_note say whether it is a baseline."""
    subj = next((a['subject'] for a in auds if a.get('subject')), None)
    if subj == 'system':
        return 'system'
    if subj in ('baseline', 'tool') or c.get('reading_kind') == '基线' or any('基线' in (c.get(k) or '') for k in ('reading_label', 'reading_note')):
        return '基线'
    return '来源不明'


def _criterion_lines(conn, c, now, audits_shown=AUDITS_SHOWN, target=False):
    auds = [a for a in _rows(conn, 'SELECT * FROM audits WHERE crit LIKE ? ORDER BY created_at DESC, id DESC', '%' + c['id'] + '%')
            if c['id'] in [x.strip() for x in (a['crit'] or '').split(',')]]
    if c.get('reading') is None and not c.get('reading_label'):
        reading, dist = '—', '—'
    else:
        reading = c.get('reading_label') or _num(c['reading'])
        prov = _provenance(c, auds)
        if prov == 'system':
            dist = _distance(c)
        else:  # no system audit behind the number: no arithmetic against the target
            dist = '系统还没测'
            if '[%s]' % prov not in reading:
                reading += ' [%s]' % prov
    tgt = '　目标 %s' % (c.get('target_label') or (_num(c['target']) if c.get('target') is not None else '—')) if target else ''
    out = ['  - %s %s：%s%s　读数 %s　离目标 %s' % (c['id'], c['status'], _short(c['text'], 100), tgt, reading, dist)]  # 「N 天没动」 dropped (the progress page shows the audit reading instead)
    if auds[:audits_shown]:
        out.append('    - 审计：' + '；'.join('%s · %s · %s · %s' % (
            a['id'], a['verdict'] or '—', a['grade'] or '—',
            db.parse_time(a['created_at']).astimezone(db.LOCAL_TZ).strftime('%m-%d')) for a in auds[:audits_shown]))
    return out


def distance_lines(conn, now=None):
    """早报第一节「离目标还有多远」: the mainlines that have criteria,
    each current-goal criterion in the handover card's own wording, plus its target and newest audit."""
    now = now or datetime.now(timezone.utc)
    out = []
    for ml in PAGES:
        g = db.current_goal(conn, ml)
        crits = _rows(conn, 'SELECT * FROM criteria WHERE mainline=? AND goal_version=? ORDER BY id', ml, g['version']) if g else []
        out.append('- **%s**%s' % (db.SHORT_NAMES[ml], '' if g else '：台账里没有目标'))
        if g and not crits:
            out.append('  - 没有验收线')
        for c in crits:
            out += _criterion_lines(conn, c, now, audits_shown=1, target=True)
    return out


def _mention(text, code):
    return bool(code) and re.search(r'(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])' % re.escape(code), text or '') is not None


def _last_touched(conn, r, code):
    """Newest of: date_start, a task_note naming it in exp_ids, its exp_notes row, an audit on it, a wait / report reply mentioning it.
    experiments.updated_at is a sync stamp and does not count. A missing table counts as no rows. None = never touched."""
    ids = {x for x in (r.get('local_id'), code) if x}
    def tok(s):
        return {re.sub(r'-20\d{6}$', '', t) for t in re.split(r'[,\s;，、]+', s or '') if t} | {t for t in re.split(r'[,\s;，、]+', s or '') if t}
    def q(sql, *p):
        try:
            return _rows(conn, sql, *p)
        except sqlite3.OperationalError:
            return []
    times = [r.get('date_start')]
    times += [n['created_at'] for n in q('SELECT exp_ids, created_at FROM task_notes') if ids & tok(n['exp_ids'])]
    times += [n['updated_at'] for n in q('SELECT code, updated_at FROM exp_notes') if n['code'] in ids]
    times += [a['created_at'] for a in q('SELECT experiment, created_at FROM audits') if ids & tok(a['experiment'])]
    times += [w['asked_at'] for w in q('SELECT ask, asked_at FROM waits') if any(_mention(w['ask'], i) for i in ids)]
    times += [x['received_at'] for x in q('SELECT page, text, received_at FROM report_replies')
              if any(_mention((x['page'] or '') + ' ' + (x['text'] or ''), i) for i in ids)]
    ok = []
    for t in times:
        try:
            ok.append(db.parse_time(t))
        except (TypeError, ValueError):
            pass
    return max(ok) if ok else None


def generate(conn, ml, now=None):
    """The generated half as markdown lines (no heading)."""
    from . import exp_index
    now = now or datetime.now(timezone.utc)
    out = ['- 生成于 %s，来自实验台账；要改内容去改台账，不改这里。' % now.astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M')]
    g = db.current_goal(conn, ml)
    out += ['', '### 目标和验收线', '']
    if g:
        out.append('- 目标 v%s（%s）：%s' % (g['version'], g['status'], _short(g['goal'], 160)))
        for c in _rows(conn, 'SELECT * FROM criteria WHERE mainline=? AND goal_version=? ORDER BY id', ml, g['version']):
            out += _criterion_lines(conn, c, now)
    else:
        out.append('- 台账里没有目标')
    try:
        all_exps = _rows(conn, 'SELECT * FROM experiments')
    except sqlite3.OperationalError:  # a ledger without that table (tests, a fresh database)
        all_exps = []
    exps = [r for r in all_exps if r.get('stage') in RUNNING
            and exp_index.mainline_of(r, exp_index.split_title(r['title'])[0]) == ml]
    cut = now - timedelta(days=LIVE_DAYS)
    running = [r for r in exps if r['stage'] in RUNNING[:2]]
    touched = {r['local_id']: _last_touched(conn, r, exp_index.split_title(r['title'])[0]) for r in running}
    live = [r for r in running if touched[r['local_id']] and touched[r['local_id']] >= cut]
    stale = len(running) - len(live)
    rest = {st: sum(1 for r in exps if r['stage'] == st) for st in RUNNING[2:]}
    out += ['', '### 在跑和准备中的实验（%d）' % len(live), '']
    for r in sorted(live, key=lambda r: (RUNNING.index(r['stage']), r.get('date_start') or ''))[:20]:
        out.append('- %s · %s · 开始 %s' % (_short(r['title'], 70), r['stage'], (r.get('date_start') or '—')[:10]))
    out.append('- 另有 ' + '、'.join('%s %d 项' % kv for kv in rest.items()) + ('、超过 %d 天没动的在跑项 %d 项' % (LIVE_DAYS, stale) if stale else '') + '，见实验索引')
    since = (now - timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
    decs = _rows(conn, "SELECT * FROM decisions WHERE mainline=? AND (status='待决' OR decided_at>=?) ORDER BY id", ml, since)
    out += ['', '### 决定（待决和最近 3 天）', '']
    out += ['- %s %s：%s%s' % (d['id'], d['status'], _short(d['question'], 70), ('　→ ' + _short(d['answer'], 50)) if d.get('answer') else '')
            for d in decs] or ['- 无']
    name = db.SHORT_NAMES[ml]
    waits = [w for w in _rows(conn, "SELECT * FROM waits WHERE status='等待' ORDER BY id") if name.lower() in (w['editor'] or '').lower()]
    out += ['', config.say('### 在等 {owner} 的（等你回）'), '']
    out += ['- %s：%s' % (w['id'], _short(w['ask'], 80)) for w in waits] or ['- 无']
    buds = _rows(conn, "SELECT * FROM verbal_budgets WHERE status='有效' AND expires_at>=? ORDER BY id",
                 now.strftime('%Y-%m-%dT%H:%M:%SZ'))
    out += ['', '### 有效预算（全项目）', '']
    out += ['- %s ¥%g：%s（到 %s）' % (b['id'], b['cap_cny'], _short(b['purpose'], 50), b['expires_at'][:10]) for b in buds] or ['- 无']
    stops = [dict(s) for s in db.active_stops(conn)]
    out += ['', '### 叫停表（全项目）', '']
    out += ['- %s %s：%s%s' % (s['id'], s['target'], s['reason'], ('「%s」' % s['words']) if s.get('words') else '') for s in stops] or ['- 无']
    return out


def replace_section(text, lines):
    i = text.find(GEN_HEAD)
    if i < 0:
        return None
    j = text.find('\n## ', i + len(GEN_HEAD))
    body = '\n'.join(lines) + '\n'
    return text[:i] + GEN_HEAD + '\n\n' + body + ('\n' + text[j + 1:] if j >= 0 else '')


def _due(conn, key, now):
    last = db.meta_get(conn, key)
    if last and db.parse_time(last) > now - EVERY:
        return False
    db.meta_set(conn, key, now.strftime('%Y-%m-%dT%H:%M:%SZ'))
    return True


def gen_run(conn, vault, log_change, now=None, force=False):
    now = now or datetime.now(timezone.utc)
    mode = modes.get(conn, 'handover-gen')
    if mode == 'off' or not vault or (not force and not _due(conn, 'handover-gen-at', now)):
        return {}
    out = {}
    for ml, rel in PAGES.items():
        path = Path(vault) / rel
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            continue
        lines = generate(conn, ml, now)
        digest = hashlib.md5('\n'.join(lines[1:]).encode()).hexdigest()  # without the 生成于 line
        key = 'handover-gen:' + ml + (':observe' if mode == 'observe' else '')  # a preview must not count as written
        if db.meta_get(conn, key) == digest:
            out[ml] = 'same'
            continue
        db.meta_set(conn, key, digest)
        if mode == 'observe':
            PREVIEW.mkdir(exist_ok=True)
            (PREVIEW / ('%s.md' % ml)).write_text(GEN_HEAD + '\n\n' + '\n'.join(lines) + '\n', encoding='utf-8')
            log_change(vault, EDITOR, rel[:-3], '本来会做：重写「一、脚本生成」（预览 .r2i/handover-preview/%s.md）' % ml)
            out[ml] = 'previewed'
        else:
            new = replace_section(text, lines)
            if new is None:
                out[ml] = 'no section'
                continue
            from .render import _write
            _write(path, new)
            log_change(vault, EDITOR, rel[:-3], '交接「一、脚本生成」按台账重写')
            out[ml] = 'written'
    return out


def _tokens(text):
    cjk = len(re.findall(r'[　-鿿＀-￯]', text))
    return cjk + (len(text) - cjk) // 4


UPDATED_RE = re.compile(r'更新于\s*(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})\s*(UTC)?')
START_RE = re.compile(r'(?<!「)按规则 ?v(\d+(?:\.\d+)?) ?开工')


def lint(text, rules_version, now=None):
    """[problem] for one handover text (empty = passes)."""
    now = now or datetime.now(timezone.utc)
    bad = []
    heads = re.findall(r'(?m)^#{2,4} .*$', text)
    missing = [s for s in REQUIRED if not any(s in h for h in heads)]
    if missing:
        bad.append('缺节：' + '、'.join(missing))
    stamps = []
    for m in UPDATED_RE.finditer(text):
        t = datetime.strptime('%s %s:%s' % m.group(1, 2, 3), '%Y-%m-%d %H:%M')
        stamps.append(t.replace(tzinfo=timezone.utc) if m.group(4) == 'UTC' else t.replace(tzinfo=db.LOCAL_TZ))
    if not stamps:
        bad.append('没写「更新于 YYYY-MM-DD HH:MM」')
    elif now - max(stamps) > timedelta(hours=FRESH_HOURS):
        bad.append('更新于 %s，超过 %d 小时' % (max(stamps).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M'), FRESH_HOURS))
    started = START_RE.findall(text)
    if not started:
        bad.append('没写「按规则 vN 开工」')
    elif rules_version and version_key(started[0]) != version_key(rules_version):
        bad.append('按规则 v%s 开工，现行 %s' % (started[0], rules_version))
    n = _tokens(text)
    if n > TOKEN_MAX:
        bad.append('约 %d token，超过 %d' % (n, TOKEN_MAX))
    return bad


THREAD_RE = THREAD_FIND
AUD_RE = re.compile(r'AUD-\d{8}-\d+')
# only a sentence that states the verdict as such (审计结论 / 审计判…), not any line that mentions an audit and 通过
AUDIT_DONE_RE = re.compile(r'审计(?:结论|判)[^。\n]{0,20}(?:通过|不通过|没过|PASS|FAIL|WARN)')
MONEY_RE = re.compile(r'(?:已花|花了|花费|实花|共花)[^¥￥\d]{0,6}[¥￥]?\s*(\d+(?:\.\d+)?)')


def _unpushed(root):
    """Commits on this checkout not on its upstream (git log @{u}..); [] without git or upstream."""
    import subprocess
    try:
        r = subprocess.run(['git', '-C', str(root), 'log', '--oneline', '@{u}..'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [l for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else []


def _ledger_spend(root):
    """{budget id: committed CNY} from paid_run's own ledger reader, or None when it cannot be read."""
    from . import paid_run
    directory = config.paid_dir(root)
    try:
        closed = {}
        runs = paid_run._read_ledger(directory / 'ledger.jsonl', closed_budgets=closed)
    except (OSError, ValueError, paid_run.PaidRunError):
        return None
    return {b: float(paid_run._committed_spend(runs, b)) for b in {r['budget'] for r in runs.values() if r.get('budget')}}


def ledger_checks(conn, text, mainline, root=None, now=None):
    """D2: five checks against the ledger, the checkout and paid_run's account; [problem]."""
    from . import audit_flow
    now = now or datetime.now(timezone.utc)
    root = root or config.get().root
    bad = []
    cut = (now - timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    for t in dict.fromkeys(THREAD_RE.findall(text)):  # 1. this thread's ticks nobody picked up
        n = conn.execute("SELECT COUNT(*) FROM report_replies WHERE thread=? AND delivered_at IS NULL AND received_at<=? "
                         "AND COALESCE(kind,'')!='audit'", (t, cut)).fetchone()[0]
        if n:
            bad.append('线程 %s 有 %d 个勾送不到（reply-wait 没取）' % (t[:14] + '…', n))
    ahead = _unpushed(root)  # 2. local commits not pushed
    if ahead:
        bad.append('本机有 %d 个提交没推（git log @{u}..）' % len(ahead))
    missing = [r['local_id'] for r in audit_flow.pending(conn, mainline) if not (r.get('reviewer') and r.get('review_due'))]
    if missing:  # 3. 待复核 rows without a reviewer
        bad.append('待复核行缺 reviewer/review_due：%s' % '、'.join(missing[:8]))
    noaud = [l.strip()[:40] for l in text.splitlines() if AUDIT_DONE_RE.search(l) and not AUD_RE.search(l)]
    if noaud:  # 4. an audit result named without its AUD number
        bad.append('审计记录缺 AUD 号：%s' % '；'.join(noaud[:3]))
    spend = _ledger_spend(root)  # 5. amounts next to a budget id vs paid_run's ledger
    for line in text.splitlines() if spend else ():
        for bid in [b for b in spend if b in line]:
            m = MONEY_RE.search(line)
            if m and abs(float(m.group(1)) - spend[bid]) > 0.01:
                bad.append('交接写 %s 花了 ¥%s，paid_run 账本 ¥%.2f' % (bid, m.group(1), spend[bid]))
    return bad


def lint_page(conn, vault, mainline, now=None, root=None):
    """lint + ledger_checks for one mainline's 交接 page; None when the page is missing."""
    from .render import rules_info
    try:
        text = (Path(vault) / PAGES[mainline]).read_text(encoding='utf-8')
    except OSError:
        return None
    return lint(text, (rules_info(vault) or {}).get('version'), now) + ledger_checks(conn, text, mainline, root, now)


def close_gate(conn, vault, mainline, editor):
    """D3: close-report runs the 交接 lint first. Only the size problem (page over TOKEN_MAX) can block: mode handover-lint
    auto refuses (HSError), observe lets it through with one 「本来会拦：…」 change line; off or no vault: no check. Every other
    lint problem is advisory in both modes. Returns all the problems (the caller prints them as warnings)."""
    mode = modes.get(conn, 'handover-lint')
    if mode == 'off' or not vault:
        return []
    bad = lint_page(conn, vault, mainline) or []
    big = next((b for b in bad if b.startswith('约 ') and b.endswith('超过 %d' % TOKEN_MAX)), None)
    if big and mode == 'auto':
        raise db.HSError('交接页太长（%s，上限 %d token），不能结案：先压缩 %s' % (big, TOKEN_MAX, PAGES[mainline][:-3]))
    if big:
        db.add_change(conn, EDITOR, PAGES[mainline][:-3], '本来会拦：%s 的 close-report：交接页%s，上限 %d token' % (editor, big, TOKEN_MAX))
    return bad


def lint_run(conn, vault, log_change, now=None, force=False):
    now = now or datetime.now(timezone.utc)
    if modes.get(conn, 'handover-lint') == 'off' or not vault or (not force and not _due(conn, 'handover-lint-at', now)):
        return {}
    from .render import rules_info
    version = (rules_info(vault) or {}).get('version')
    out = {}
    for ml, rel in PAGES.items():
        try:
            text = (Path(vault) / rel).read_text(encoding='utf-8')
        except OSError:
            continue
        bad = lint(text, version, now) + ledger_checks(conn, text, ml, now=now)
        out[ml] = bad
        key = 'handover-lint:' + ml
        if db.meta_get(conn, key) != bad:
            db.meta_set(conn, key, bad)
            if bad:
                log_change(vault, EDITOR, rel[:-3], '本来会提醒：交接 lint 不过（%s）' % '；'.join(bad))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog='r2i.handover')
    sub = ap.add_subparsers(dest='cmd', required=True)
    li = sub.add_parser('lint'); li.add_argument('--file', action='append'); li.add_argument('--vault')
    li.add_argument('--rules-version')
    ge = sub.add_parser('gen'); ge.add_argument('--mainline', choices=db.MAINLINES, required=True)
    a = ap.parse_args(argv)
    if a.cmd == 'lint':
        version = a.rules_version
        if not version and a.vault:
            from .render import rules_info
            version = (rules_info(a.vault) or {}).get('version')
        files = a.file or ([str(Path(a.vault) / r) for r in PAGES.values()] if a.vault else [])
        res = {f: lint(Path(f).read_text(encoding='utf-8'), version) for f in files}
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 1 if any(res.values()) else 0
    conn = db.connect()
    print('\n'.join(generate(conn, a.mainline)))
    return 0


if __name__ == '__main__':
    sys.exit(main())


def check_append(mainline, section, text, editor):
    """handover-append input check; returns the cleaned text (trailing whitespace off). Nothing is written."""
    if mainline not in PAGES:
        raise db.HSError('--mainline must be one of %s' % ', '.join(PAGES))
    section = (section or '').strip('# \t')
    if not section or '\n' in section:
        raise db.HSError('--section must be one heading text')
    if section.startswith(GEN_TITLE):
        raise db.HSError('「%s…」 is written by handover-gen; pick another section' % GEN_TITLE)
    if not (editor or '').strip():
        raise db.HSError('--editor is required')
    text = (text or '').replace('\r\n', '\n').lstrip('\n').rstrip()
    if not text:
        raise db.HSError('the file is empty')
    if len(text) > APPEND_MAX:
        raise db.HSError('at most %d characters, got %d' % (APPEND_MAX, len(text)))
    return text


def _headings(text):
    """(line start, line end, level, title) of every markdown heading outside the frontmatter and code fences."""
    out, pos, fence = [], 0, False
    lines = text.split('\n')
    front = lines[0].strip() == '---' and any(l.strip() == '---' for l in lines[1:])
    for n, line in enumerate(lines):
        start, pos = pos, pos + len(line) + 1
        if front:
            front = n == 0 or line.strip() != '---'
            continue
        if line.lstrip().startswith(('```', '~~~')):
            fence = not fence
        m = None if fence else re.match(r'^(#{1,6})[ \t]+(.*)$', line)
        if m:
            out.append((start, start + len(line), len(m.group(1)), m.group(2).strip('# \t')))
    return out


def _section_span(heads, text, i):
    """(start of the heading line, start of the next heading of the same or a higher level, or end of text)."""
    end = next((h[0] for h in heads[i + 1:] if h[2] <= heads[i][2]), len(text))
    return heads[i][0], end


PLACEHOLDER_RE = re.compile(r'^（[^）\n]*待补）$')


def _drop_placeholders(body):
    """V5-PLACEHOLDER: remove each line that is only 「（… 待补）」, with one adjacent blank line (the one after it, else the one before)."""
    lines, i = body.splitlines(keepends=True), 0
    while i < len(lines):
        if not PLACEHOLDER_RE.match(lines[i].rstrip('\n')):
            i += 1
        elif i + 1 < len(lines) and not lines[i + 1].strip():
            del lines[i:i + 2]
        elif i and not lines[i - 1].strip():
            del lines[i - 1:i + 1]; i -= 1
        else:
            del lines[i]
    return ''.join(lines)


def append(vault, mainline, section, text, editor, log_change, now=None):
    """Add text at the end of a section of a vault handover page, or as a new 「## section」 at the end of the page. Never
    touches 「一、脚本生成」 (handover-gen's). The page is copied to .deploy/page-backups/ first; a page edited in the last
    minute is left alone (status busy); the same text already in the section is a no-op (already_present)."""
    from . import render
    text = check_append(mainline, section, text, editor)
    section = section.strip('# \t')
    rel = PAGES[mainline]
    path = Path(vault) / rel
    try:
        page = path.read_text(encoding='utf-8')
    except FileNotFoundError:  # a new project has no handover page yet: the first append starts it
        page = '# %s 交接\n' % db.SHORT_NAMES[mainline]
    except OSError as e:
        raise db.HSError('cannot read %s: %s' % (rel, e)) from None
    heads = _headings(page)
    gen = next((_section_span(heads, page, i) for i, h in enumerate(heads) if h[3].startswith(GEN_TITLE)), None)
    i = next((i for i, h in enumerate(heads) if h[3] == section), None)
    if i is not None and gen and gen[0] <= heads[i][0] < gen[1]:
        raise db.HSError('「%s」 lies inside 「%s…」 (handover-gen\'s part)' % (section, GEN_TITLE))
    out = {'page': rel, 'section': section, 'chars': len(text)}
    if i is not None:
        start, end = _section_span(heads, page, i)
        if text in page[start:end]:
            return {**out, 'status': 'already_present'}
        body_from = min(heads[i][1] + 1, len(page))  # the section's own body: up to the next heading of any level
        body_to = next((h[0] for h in heads[i + 1:]), len(page))
        body = page[body_from:body_to]
        clean = _drop_placeholders(body)
        page, end = page[:body_from] + clean + page[body_to:], end - (len(body) - len(clean))
        before, after = page[:end].rstrip('\n'), page[end:]
        new = before + '\n\n' + text + '\n' + ('\n' if after else '') + after
    else:
        new, out['new_section'] = page.rstrip('\n') + '\n\n## %s\n\n%s\n' % (section, text), True
    if render._busy(path):
        return {**out, 'status': 'busy'}
    stamp = (now or datetime.now(db.LOCAL_TZ)).strftime('%Y%m%d%H%M%S')
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / ('%s-%s.md' % (Path(rel).stem.replace(' ', '-'), stamp)) if path.exists() else None  # none for a new page
    if backup:
        shutil.copy2(path, backup)
    render._write(path, new)
    log_change(vault, editor, rel[:-3], 'handover-append：往「%s」%s追加 %d 字（%s）' % (section, '新建段并' if 'new_section' in out else '末尾', len(text), '备份 ' + backup.name if backup else '新建页'))
    return {**out, 'status': 'appended', 'backup': str(backup) if backup else None}
