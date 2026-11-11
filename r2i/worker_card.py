"""Every worker run ends with a result card, and the ledger gets one line pointing at it.

  python3 -B -m r2i.worker_card --run-dir <运行目录> --worker <name> --task '<一句>' \\
      --editor '<父线程标题>' [--thread cc_…] [--files a.py,b.py] [--tests '<命令 → 结果>'] [--cost '<花费>'] \\
      [--compare '<option A vs option B, roughly what each costs>'] [--open '<没解决的>'] [--status SUCCESS|FAILED]

Writes <run-dir>/RESULT-CARD.md (改了什么、文件和 md5、测试、花费、比价、没解决的) and queues one 变更 line
「worker 结果卡：…」 (path = the card), so the parent thread links the card in its handover. A worker run dir containing result.json
(keys exit_code, cli_reported_total_cost_usd, elapsed_seconds, worker_model) fills status, cost and elapsed time from its result.json when the flags leave them out."""
import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import db
from . import config

CARD = 'RESULT-CARD.md'


def _md5(p):
    try:
        return hashlib.md5(Path(p).read_bytes()).hexdigest()[:8]
    except OSError:
        return '（读不到）'


def _from_worker_dir(run_dir):
    try:
        r = json.loads((Path(run_dir) / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    cost = r.get('cli_reported_total_cost_usd')
    return {'status': 'SUCCESS' if not r.get('exit_code') else 'FAILED',
            'cost': ('约 $%s（CLI 估算）' % cost) if cost is not None else None,
            'elapsed': r.get('elapsed_seconds'), 'model': r.get('worker_model')}


def _walk(run_dir, depth=5):
    base = len(Path(run_dir).parts)
    for d, subs, files in os.walk(run_dir):
        if len(Path(d).parts) - base >= depth:
            subs.clear()
        yield Path(d), files


def _unverified(value):
    if isinstance(value, dict):
        return sum(_unverified(v) for v in value.values())
    if isinstance(value, list):
        return sum(_unverified(v) for v in value)
    return int(isinstance(value, str) and (value == '未核实' or value.endswith('unverified')))


def call_status(run_dir):
    """One line from the run's receipts.jsonl: http_status / error distribution, success rate, empty outputs
    (2xx calls whose result.json has no public_content) and how many result values read 未核实 / *unverified."""
    receipts, results, unverified = [], [], 0
    for d, files in _walk(run_dir):
        for name in files:
            p = d / name
            if name == 'receipts.jsonl':
                for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
                    try:
                        receipts.append(json.loads(line))
                    except ValueError:
                        pass
            elif name.endswith(('.json', '.jsonl')) and name != CARD and p.stat().st_size < 20_000_000:
                # shortcut: 未核实 is counted as any JSON string value 「未核实」 or 「…unverified」; a pipeline with another label is missed
                text = p.read_text(encoding='utf-8', errors='replace')
                for chunk in (text.splitlines() if name.endswith('.jsonl') else [text]):
                    try:
                        doc = json.loads(chunk)
                    except ValueError:
                        continue
                    unverified += _unverified(doc)
                    if name == 'result.json' and isinstance(doc, dict) and 'http_status' in doc:
                        results.append(doc)
    if not receipts:
        return '没有 receipts.jsonl（这次没有 API 调用，或记在运行目录外）' + ('；未核实 %d' % unverified if unverified else '')
    ok = lambda r: isinstance(r.get('http_status'), int) and 200 <= r['http_status'] < 300
    dist = Counter('%s%s' % (r.get('http_status') if r.get('http_status') is not None else '无状态',
                             '/' + r['error_type'] if not ok(r) and r.get('error_type') else '') for r in receipts)
    n_ok = sum(ok(r) for r in receipts)
    empty = sum(1 for r in results if ok(r) and not str(r.get('public_content') or '').strip())
    return '%d 次，成功 %d（%.0f%%）；%s；空输出 %d；未核实 %d%s' % (
        len(receipts), n_ok, 100.0 * n_ok / len(receipts), '、'.join('%s×%d' % kv for kv in dist.most_common()),
        empty, unverified, '；⚠ 没有一次成功，先查钥匙' if not n_ok else '')


def card_text(a, root=None):
    root = root or config.get().root
    auto = _from_worker_dir(a.run_dir)
    status = a.status or auto.get('status') or '未写'
    files = [f.strip() for f in (a.files or '').split(',') if f.strip()]
    lines = ['# worker 结果卡', '',
             '- 时间：%s' % datetime.now(db.LOCAL_TZ).strftime('%Y-%m-%d %H:%M'),
             '- worker：%s%s' % (a.worker, ('（%s）' % auto['model']) if auto.get('model') else ''),
             '- 父线程：%s%s' % (a.editor, (' · ' + a.thread) if a.thread else ''),
             '- 任务：%s' % a.task, '- 结果：%s' % status, '- 调用状态：%s' % call_status(a.run_dir)]
    if auto.get('elapsed') is not None:
        lines.append('- 用时：%.0f 秒' % auto['elapsed'])
    lines += ['', '## 改了什么', '']
    lines += ['| 文件 | md5 |', '|---|---|'] + ['| `%s` | %s |' % (f, _md5(Path(root) / f if not Path(f).is_absolute() else f))
                                                for f in files] if files else ['没有改文件。']
    lines += ['', '## 测试', '', a.tests or '没写（算不完整）', '',
              '## 花费', '', a.cost or auto.get('cost') or '没写', '',
              '## 方案比较', '', a.compare or '没写', '',
              '## 没解决的', '', a.open or '无', '']
    return '\n'.join(lines), status


def main(argv=None):
    ap = argparse.ArgumentParser(prog='r2i.worker_card')
    for k in ('run-dir', 'worker', 'task', 'editor'):
        ap.add_argument('--' + k, required=True)
    for k in ('thread', 'files', 'tests', 'cost', 'compare', 'open'):
        ap.add_argument('--' + k)
    ap.add_argument('--status', choices=('SUCCESS', 'FAILED'))
    a = ap.parse_args(argv)
    run = Path(a.run_dir)
    if not run.is_dir():
        print(json.dumps({'error': 'no run dir %s' % run}, ensure_ascii=False)); return 2
    text, status = card_text(a)
    (run / CARD).write_text(text, encoding='utf-8')
    from . import queue
    summary = 'worker 结果卡：%s · %s · %s' % (a.worker, a.task[:60], status)
    name = queue.enqueue('log-change', {'editor': a.editor, 'path': str(run / CARD), 'summary': summary})
    print(json.dumps({'card': str(run / CARD), 'status': status, 'queued': name}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
