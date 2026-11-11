"""Cold-start cross-check: ids and numbers in a handover page against the ledger snapshot.

  python3 -B -m r2i.coldstart --vault PATH [--mainline M] [--snapshot FILE] [--all]

Reads vault agent/交接/<主线> 交接.md (or 交接/ before the vault move). By default only the sections that state the present
(heading with 冷启动 / 脚本生成 / 在等 / 叫停); --all reads the whole page, history included (history says old statuses on purpose).
Only what can be anchored is compared; prose is skipped:
  - DEC-nn, wait-D-n, AUD-D-n, RR-D-n, NQ-D-n, MS-D-n: the id must exist in snapshot.json (an id dated after the snapshot's
    export, or older than the snapshot's 7-day window for AUD/wait/RR/NQ, is skipped, not reported; a wait id counts as missing only
    when the page says 等待, because the snapshot lists only open waits and report waits);
  - a status word right after the id (「DEC-02 待决」「CODE-01（已运行待复核）」「method-X 有效」) must equal the ledger's;
  - a cap amount right after a budget / night-queue id (「CODE-01 预算 ¥1.23」, 「上限 ¥1.23」) must equal cap_cny; a ¥ amount right after
    an AUD id must equal cost_cny (an amount without 上限/预算 after a budget id is spend, not compared);
  - an experiment-or-budget code (name-YYYYMMDD) that is not in the snapshot is reported only when it carries a cap amount.
Prints one line per mismatch; exit 1 when there is any."""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db, handover

SNAPSHOT = config.state_dir() / 'snapshot.json'
SCOPE = ('冷启动', '脚本生成', '在等', '叫停')
HEAD = re.compile(r'^(#{1,6}) (.*)$')
PSEUDO_HEAD = re.compile(r'^\*\*([^*]*冷启动[^*]*)\*\*\s*$')  # 「**冷启动三问**」 as a bold line
ID_RES = {'DEC': r'DEC-\d+', 'wait': r'wait-\d{8}-\d+', 'AUD': r'AUD-\d{8}-\d+', 'RR': r'RR-\d{8}-\d+',
          'NQ': r'NQ-\d{8}-\d+', 'MS': r'MS-\d{8}-\d+'}
CODE_RE = r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{8}(?:-\d+)?'
SHAPES = ['(?:%s)' % r for r in ID_RES.values()] + [CODE_RE]


def token_rx(known):
    """Ids by shape, plus every code the snapshot knows by name (undated ones such as CODE-01 have no shape)."""
    names = sorted((t for t in known if re.fullmatch(r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+', t)), key=len, reverse=True)
    return re.compile(r'(?<![A-Za-z0-9-])(%s)(?![A-Za-z0-9-]|\.(?:md|json|jsonl|py|sqlite))' % '|'.join(SHAPES + list(map(re.escape, names))))


GAP = r'[\s*_`」』）)（(:：,，·]{0,4}'
NUM = r'[\s:：]*[¥￥]\s*(\d+(?:\.\d+)?)'
CAP = re.compile(GAP + r'(?:上限|预算|封顶|cap)' + NUM)  # a budget's amount: only when the page says it is the cap (otherwise it is spend)
COST = re.compile(GAP + r'(?:花费|实花|共花|花了)?' + NUM)
WINDOWED = ('AUD', 'wait', 'RR', 'NQ')  # snapshot.json keeps only the last 7 days of these (queue.py)
# the words a status can be written as, for every kind (plus whatever the snapshot has)
STATUS_WORDS = set(handover.RUNNING) | {'已复核', '失败/停止', '待决', '已决', '等待', '已回', '过期', '有效', '撤回', 'PASS', 'FAIL', '步骤错', '不确定'}


def _kind(tok):
    for k, rx in ID_RES.items():
        if re.fullmatch(rx, tok):
            return k
    return 'code'


def ledger(snap):
    """{token: {'kind', 'status', 'amount'}} for everything a handover can cite; codes known only by name get status None."""
    out = {}

    def put(tok, kind, status=None, amount=None):
        if tok:
            cur = out.setdefault(tok, {'kind': kind, 'status': None, 'amount': None})  # the same code can come from several tables
            cur['status'] = cur['status'] or status
            cur['amount'] = amount if cur['amount'] is None else cur['amount']

    for d in snap.get('open_decisions', []) + snap.get('recent_decided', []):
        put(d['id'], 'DEC', d.get('status'))
    for w in snap.get('open_waits', []) + snap.get('report_waits', []):
        put(w['id'], 'wait', w.get('status'))
    for a in snap.get('audits', []):
        put(a['id'], 'AUD', a.get('verdict'), a.get('cost_cny'))
        put(a.get('experiment'), 'code')
    for r in snap.get('report_replies', []):
        put(r['id'], 'RR')
    for n in snap.get('night_items', []):
        put(n['id'], 'NQ', n.get('status'), n.get('cap_cny'))
        put(n.get('exp_id'), 'code')
    for m in snap.get('milestones', []):
        put(m['id'], 'MS')
        put(m.get('exp_id'), 'code')
    for b in snap.get('verbal_budgets', []):
        put(b['id'], 'code', b.get('status'), b.get('cap_cny'))
    for e in snap.get('pending_experiments', []):
        put(e['local_id'], 'code', e.get('stage'))
    for e in snap.get('exp_notes', []):
        put(e.get('code'), 'code')
    return out


def sections(text, everything=False):
    """The text to read: the whole page, or the sections whose heading names the present (SCOPE). [(title, line)]."""
    out, level, title = [], None, None
    for line in text.split('\n'):
        m = HEAD.match(line)
        p = PSEUDO_HEAD.match(line)
        if m or p:
            lv = len(m.group(1)) if m else 9
            if level is not None and lv > level:
                pass  # a deeper heading stays inside the section
            else:
                level, title = None, None
            if level is None and any(w in (m.group(2) if m else p.group(1)) for w in SCOPE):
                level, title = lv, (m.group(2) if m else p.group(1)).strip()
        if everything or title:
            out.append((title or '全文', line))
    return out


def _unseen(tok, led, local_day):
    """Why a token missing from the snapshot may still be fine: made after the export, or older than the 7-day window."""
    day = re.search(r'-(\d{8})(?:-(\d+))?$', tok)
    if not day:
        return None
    d = day.group(1)
    if d > local_day:
        return '快照之后新建'
    if d == local_day and _kind(tok) != 'code':
        pre = tok[:day.start()]
        same = [int(re.search(r'-(\d+)$', t).group(1)) for t in led if t.startswith(pre + '-' + d + '-') and re.search(r'-\d+$', t)]
        if int(day.group(2) or 0) > max(same, default=0):
            return '快照之后新建'
    if _kind(tok) in WINDOWED and d < (datetime.strptime(local_day, '%Y%m%d') - timedelta(days=6)).strftime('%Y%m%d'):
        return '超出快照的 7 天范围'
    return None


def check(text, snap, everything=False):
    """([(种类, token, 说明, 原文片段)], skipped): `skipped` = tokens missing from the snapshot for a harmless reason."""
    led = ledger(snap)
    exported = datetime.strptime(snap['exported_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc) if snap.get('exported_at') else None
    local_day = exported.astimezone(db.LOCAL_TZ).strftime('%Y%m%d') if exported else '99999999'
    words = STATUS_WORDS | {v['status'] for v in led.values() if v['status']}
    status_rx = re.compile(GAP + r'(?:判)?\s*(%s)(?![\u4e00-\u9fff])' % '|'.join(sorted(map(re.escape, words), key=len, reverse=True)))
    bad, skipped, seen = [], {}, set()
    token = token_rx(led)
    for title, line in sections(text, everything):
        for m in token.finditer(line):
            tok, after = m.group(1), line[m.end():]
            known, kind = led.get(tok), _kind(tok)
            st = status_rx.match(after)
            found = []
            if known is None:
                why = _unseen(tok, led, local_day)
                if why:
                    skipped[tok] = why
                elif kind == 'wait':  # snapshot waits = open ones + report waits: only a claimed 「等待」 is checkable
                    if st and st.group(1) == '等待':
                        found.append(('不在快照', '交接说 %s 在等待，台账里没有这条等待' % tok))
                elif kind != 'code' or CAP.match(after):  # a bare unknown name-date code could be a file or run name
                    found.append(('不在快照', '台账快照里没有 %s' % tok))
            else:
                if st and known['status'] and st.group(1) != known['status']:
                    found.append(('状态不符', '交接写「%s」，台账是「%s」' % (st.group(1), known['status'])))
                amt = (COST if kind == 'AUD' else CAP).match(after)
                if amt and known['amount'] is not None and abs(float(amt.group(1)) - float(known['amount'])) > 0.005:
                    found.append(('金额不符', '交接写 ¥%s，台账是 ¥%g' % (amt.group(1), float(known['amount']))))
            for k, why in found:
                key = (k, tok, why)
                if key not in seen:
                    seen.add(key)
                    bad.append((k, tok, '%s（在「%s」）' % (why, title), line.strip()[max(0, m.start() - 20):m.end() + 40]))
    return bad, skipped


def page_path(vault, mainline):
    return Path(vault) / handover.PAGES[mainline]


def main(argv=None):
    ap = argparse.ArgumentParser(prog='r2i.coldstart')
    ap.add_argument('--vault', required=True)
    ap.add_argument('--mainline', choices=db.MAINLINES)
    ap.add_argument('--snapshot', default=str(SNAPSHOT))
    ap.add_argument('--all', action='store_true', help='read the whole page, not only the sections about the present')
    a = ap.parse_args(argv)
    try:
        snap = json.loads(Path(a.snapshot).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        print('读不了台账快照 %s：%s' % (a.snapshot, e), file=sys.stderr)
        return 2
    total = 0
    for ml in [a.mainline] if a.mainline else db.MAINLINES:
        path = page_path(a.vault, ml)
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            print('%s：没有交接页（%s）' % (ml, path))
            continue
        bad, skipped = check(text, snap, a.all)
        total += len(bad)
        print('%s（%s）：%d 条不一致%s' % (ml, path.name, len(bad), '，跳过 %d 个缺在快照里但情有可原的编号' % len(skipped) if skipped else ''))
        for kind, tok, why, ctx in bad:
            print('  [%s] %s：%s　…%s…' % (kind, tok, why, ctx))
    print('共 %d 条不一致' % total)
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())
