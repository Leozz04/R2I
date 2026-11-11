"""Night watchdog: every 30 minutes from a launchd job. No model calls.

It acts outside Claude's permission system, so it may do only what the permissions doc lists:
  - SIGTERM a registered night run (night_gate) when its deadline passed, its item was withdrawn, the internal disk
    is below 10 GB, or the external disk is below 40 GB or not mounted;
  - read the DeepSeek balance (only when [keys] deepseek names a Keychain item; sent only to DeepSeek) and queue it for 资源.md;
  - write the 「## 巡检记录」 section at the end of agent/管理/今晚可跑-历史.md, the changelog row and its heartbeat; the
    first check after the report hour lists every paid_run started that night (R-21), read from the paid_run ledger.
It never deletes files, rents or destroys instances, pushes, or sends messages.
"""
from datetime import datetime, timedelta
from pathlib import Path
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import urllib.request

from . import db, night, queue, render
from . import config

STATE = Path(os.environ.get('R2I_NIGHT_DIR') or config.state_dir() / 'night')
PAGE = night.HISTORY  # the section lives at the end of 今晚可跑-历史; render owns only the auto block above it
SEC_START = '<!-- r2i-sync:watchdog:start 本节由夜间巡检写，手改会被覆盖 -->'
SEC_END = '<!-- r2i-sync:watchdog:end -->'
HEARTBEAT = 'agent/管理/.night-watchdog-heartbeat'
EDITOR = '夜间巡检脚本'
MIN_INTERNAL_GB = float(os.environ.get('R2I_MIN_INTERNAL_GB', 10))
MIN_EXTERNAL_GB = float(os.environ.get('R2I_MIN_EXTERNAL_GB', 40))
MIN_DEEPSEEK = float(os.environ.get('R2I_MIN_DEEPSEEK_CNY', 10))
LEDGER = Path(os.environ.get('R2I_PAID_LEDGER') or config.paid_dir() / 'ledger.jsonl')


def external_mount():
    """The mount point of runs_root when it is on an external volume (/Volumes/<name>), else None: no external check."""
    parts = config.get().runs_root.parts
    return str(Path(*parts[:3])) if len(parts) > 2 and parts[1] == 'Volumes' else None


REPORT_HOUR = 9  # the report time, local; the night is the configured quiet window
BALANCE_REFRESH_HOURS = 12
LOG_KEEP = 30


def _load(name, default):
    try:
        return json.loads((STATE / name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _save(name, data):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / ('.' + name)
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, STATE / name)


def _stamp():
    return datetime.now(db.LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')


# ---- checks ----

def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


def check_disks(mounted=os.path.ismount, free=free_gb):
    """Problems that should stop night runs: list of (key, text)."""
    problems = []
    internal = free('/')
    if internal < MIN_INTERNAL_GB:
        problems.append(('disk:internal', '内置盘只剩 %.1f GB，低于 %g GB' % (internal, MIN_INTERNAL_GB)))
    external = external_mount()
    if external is None:
        pass
    elif not mounted(external):
        problems.append(('disk:external', '外置盘 %s 没有挂载' % external))
    else:
        ext = free(external)
        if ext < MIN_EXTERNAL_GB:
            problems.append(('disk:external', '外置盘 %s 只剩 %.1f GB，低于 %g GB' % (external, ext, MIN_EXTERNAL_GB)))
    return problems


def _cmdline(pid):
    try:
        return subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def check_runs(items, disk_problems, cmdline=_cmdline, kill=os.kill, now=None):
    """Registered night runs: stop the ones that must stop, retire the ones that ended. Returns actions."""
    actions = []
    runs = STATE / 'runs'
    if not runs.exists():
        return actions
    now = now or datetime.now(db.LOCAL_TZ)
    status = {r['id']: r['status'] for r in items}
    for f in sorted(runs.glob('NQ-*.json')):
        rec = json.loads(f.read_text(encoding='utf-8'))
        cmd = cmdline(rec['pid'])
        alive = 'paid_run' in cmd and rec['item'] in cmd  # guards against a reused pid
        if not alive:
            (runs / 'ended').mkdir(exist_ok=True)
            os.replace(f, runs / 'ended' / f.name)
            actions.append({'kind': '任务结束', 'item': rec['item'], 'text': '%s 已结束（paid_run 账本有花费记录）' % rec['item']})
            continue
        reason = None
        if status.get(rec['item']) == '撤回':
            reason = config.say('{owner} 撤回了这一项')
        elif db.parse_time(rec['deadline']) <= now:
            reason = '过了截止时间'
        elif disk_problems:
            reason = disk_problems[0][1]
        if reason:
            try:
                kill(rec['pid'], signal.SIGTERM)  # paid_run forwards it to the child's process group and writes the end record
                actions.append({'kind': '停止任务', 'item': rec['item'], 'text': '停止 %s：%s' % (rec['item'], reason)})
            except ProcessLookupError:
                pass
    return actions


def deepseek_balance():
    item = config.get().need_key('deepseek', 'DeepSeek balance check')
    key = subprocess.run(['security', 'find-generic-password', '-s', item, '-w'], capture_output=True,
                         text=True, timeout=15).stdout.strip()
    if not key:
        raise RuntimeError('钥匙串里读不到 %s' % item)
    req = urllib.request.Request('https://api.deepseek.com/user/balance',
                                 headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    infos = data.get('balance_infos') or [{}]
    # the account holds a CNY and a USD entry in no fixed order; the CNY one is the real balance
    info = next((i for i in infos if i.get('currency') == 'CNY'), infos[0])
    return float(info.get('total_balance')), info.get('currency') or 'CNY'


def night_spend(night_date, ledger=None):
    """Every paid_run started in the night of `night_date` (local, 00:00-09:00) or charged to its NIGHT- budget.
    Returns rows: time, budget, purpose, cap, spent (settled > ended > None), state."""
    start = datetime.combine(night_date, datetime.min.time(), tzinfo=db.LOCAL_TZ)
    end = start + timedelta(hours=REPORT_HOUR)
    night_id = 'NIGHT-' + night_date.strftime('%Y%m%d')
    runs = {}
    try:
        lines = Path(ledger or LEDGER).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        rid, ev = rec.get('run_id'), rec.get('event')
        if ev == 'start':
            try:
                t = db.parse_time(rec['time'])
            except (KeyError, ValueError):
                continue
            if start <= t < end or rec.get('budget') == night_id:
                runs[rid] = {'time': t, 'budget': rec.get('budget'), 'purpose': rec.get('purpose') or '',
                             'cap': rec.get('cap'), 'end': None, 'settled': None}
        elif rid in runs and ev == 'end':
            runs[rid]['end'] = rec
        elif rid in runs and ev == 'settlement':
            runs[rid]['settled'] = rec
    rows = []
    for r in sorted(runs.values(), key=lambda r: r['time']):
        src = r['settled'] or r['end']
        spent = src.get('spent_cny') if src and src.get('spend_known') else None
        if r['end'] is None:
            state = '还在跑（按上限占用）'
        elif spent is None:
            state = '花费未知，按上限占用'
        else:
            state = '已结算' if r['settled'] else ('退出码 %s' % r['end'].get('exit_code'))
        rows.append({'time': r['time'].astimezone(db.LOCAL_TZ).strftime('%H:%M'), 'budget': r['budget'],
                     'purpose': r['purpose'], 'cap': r['cap'], 'spent': spent, 'state': state})
    return rows


def night_report(now=None, ledger=None):
    """Once a day, at the first check from 09:00 local: (action, report) for last night, else (None, None)."""
    local = (now or datetime.now().astimezone()).astimezone(db.LOCAL_TZ)
    if local.hour < REPORT_HOUR or _load('night-report.json', {}).get('date') == local.strftime('%Y-%m-%d'):
        return None, None
    rows = night_spend(local.date(), ledger)
    known = sum(float(r['spent']) for r in rows if r['spent'] is not None)
    held = sum(float(r['cap'] or 0) for r in rows if r['spent'] is None)
    text = '昨夜（%s 00:00–09:00）付费运行 %d 笔，已知花费合计 ¥%.2f' % (local.strftime('%m-%d'), len(rows), known)
    if held:
        text += '，另有 %d 笔花费未知或还在跑，按上限占用 ¥%.2f' % (sum(r['spent'] is None for r in rows), held)
    report = {'date': local.strftime('%Y-%m-%d'), 'rows': rows, 'summary': text}
    _save('night-report.json', report)
    return {'kind': '夜里花费', 'item': 'NIGHT-' + local.strftime('%Y%m%d'), 'text': text}, report


def _report_lines(report):
    if not report:
        return []
    out = ['', '## 夜里的花费（%s 00:00–09:00）' % report['date'][5:], '', render._md(report['summary'])]
    if report['rows']:
        out += ['', '| 开始 | 预算 | 用途 | 上限 | 花费 | 状态 |', '|---|---|---|---:|---:|---|']
        out += ['| %s | `%s` | %s | ¥%s | %s | %s |' % (
            r['time'], r['budget'], render._cell(r['purpose']), r['cap'],
            '—' if r['spent'] is None else '¥%.2f' % float(r['spent']), r['state']) for r in report['rows']]
    return out


# ---- outputs ----

def queue_balance(service, value, unit, source, kind):
    """Only when the number moved or the last write is old, so 资源.md does not change every 30 minutes."""
    last = _load('last-balances.json', {})
    prev = last.get(service)
    stamp = datetime.now().timestamp()
    if prev and abs(prev[0] - value) < 0.01 and stamp - prev[1] < BALANCE_REFRESH_HOURS * 3600:
        return False
    queue.enqueue('res-set', {'service': service, 'kind': kind, 'unit': unit, 'balance': '%.2f' % value,
                              'source': source})
    last[service] = [value, stamp]
    _save('last-balances.json', last)
    return True


def alerts_once(current):
    """Turn conditions into actions only when they start or clear, not on every check."""
    before = set(_load('alerts.json', []))
    now_keys = {k for k, _ in current}
    actions = [{'kind': '告警', 'item': k, 'text': t} for k, t in current if k not in before]
    actions += [{'kind': '恢复', 'item': k, 'text': '%s 恢复正常' % k} for k in sorted(before - now_keys)]
    _save('alerts.json', sorted(now_keys))
    return actions


def write_page(vault, actions, status_lines):
    """Rewrites the 「## 巡检记录」 section of 今晚可跑-历史 (between SEC_START and SEC_END, appended at the end the first time);
    text above it and the owner's own text are kept. Only when something happened, and every rewrite is signed in the changelog.
    A page touched in the last minute is left alone: returns False and the caller retries at the next check."""
    path = Path(vault) / PAGE
    log = _load('actions.json', [])
    log = [dict(a, at=_stamp()) for a in actions] + log
    log = log[:LOG_KEEP]
    _save('actions.json', log)  # kept even when the page is busy: the retry writes it
    summary = '；'.join(x for x in (_load('page-pending-summary', ''), '；'.join(a['text'] for a in actions)) if x)
    if render._busy(path):
        _save('page-pending-summary', summary[:200])
        return False
    if not log:
        return True
    body = ['### 最近一次有事的巡检（%s）' % _stamp(), ''] + ['- ' + render._md(s) for s in status_lines]
    body += [l.replace('## ', '### ', 1) if l.startswith('## ') else l for l in _report_lines(_load('night-report.json', None))]
    body += ['', '### 记录（最近 %d 条）' % LOG_KEEP]
    for a in log:
        body += ['', '> [!warning] %s · %s' % (a['at'][5:16], render._md(a['kind'])), '> ' + render._md(a['text'])]
    section = '\n'.join([SEC_START, '## 巡检记录', '', '> [!summary] 最近一次事件 %s · 共 %d 条' % (log[0]['at'][5:16], len(log)), ''] + body
                        + ['', SEC_END])
    existing = path.read_text(encoding='utf-8') if path.exists() else None
    if existing is None:
        text = night.AGENT_HEAD + section + '\n'
    elif SEC_START in existing and SEC_END in existing:
        pre, rest = existing.split(SEC_START, 1)
        text = pre + section + rest.split(SEC_END, 1)[1]
    else:
        text = existing.rstrip('\n') + '\n\n' + section + '\n'
    render._write(path, text)
    if summary:
        render.log_change(vault, EDITOR, PAGE, summary[:200])
        _save('page-pending-summary', '')
    return True


def run(vault, dry_run=False):
    status, actions = [], []
    disk = check_disks()
    status.append('磁盘：' + ('；'.join(t for _, t in disk) if disk else '正常（内置盘 ≥ %g GB，外置盘 ≥ %g GB）'
                              % (MIN_INTERNAL_GB, MIN_EXTERNAL_GB)))
    try:
        items = json.loads(queue.SNAPSHOT.read_text(encoding='utf-8')).get('night_items', [])
    except (OSError, ValueError):
        items = []
    conditions = list(disk)
    if dry_run:
        return {'status': status, 'disk': disk}
    actions += check_runs(items, disk)
    try:
        if not config.get().key('deepseek'):
            raise config.ConfigError('DeepSeek balance check is off: set [keys] deepseek in r2i.toml')
        bal, cur = deepseek_balance()
        status.append('DeepSeek 余额：%.2f %s' % (bal, cur))
        queue_balance('DeepSeek', bal, cur, 'api.deepseek.com/user/balance（巡检）', '预付余额')
        if bal < MIN_DEEPSEEK:
            conditions.append(('balance:deepseek', 'DeepSeek 余额 %.2f %s，低于 %g' % (bal, cur, MIN_DEEPSEEK)))
    except Exception as e:
        status.append('DeepSeek：%s' % str(e)[:120] if isinstance(e, config.ConfigError) else 'DeepSeek：查询失败（%s）' % str(e)[:120])
    try:
        report_action, _report = night_report()
        if report_action:
            actions.append(report_action)
    except Exception as e:
        status.append('夜里花费：读账本失败（%s）' % str(e)[:120])
    actions += alerts_once(conditions)
    if actions or _load('page-pending', False):  # a busy page is retried at the next check
        _save('page-pending', not write_page(vault, actions, status))
    render._write(Path(vault) / HEARTBEAT, json.dumps({'at': datetime.now(db.LOCAL_TZ).isoformat(timespec='seconds'),
                                                       'status': status}, ensure_ascii=False) + '\n')
    return {'status': status, 'actions': actions}


def main(argv=None):
    p = argparse.ArgumentParser(prog='watchdog')
    p.add_argument('--vault', required=True)
    p.add_argument('--dry-run', action='store_true', help='only check the disks and print; touch nothing')
    a = p.parse_args(argv)
    out = run(a.vault, a.dry_run)
    if a.dry_run or out.get('actions'):
        print('%s %s' % (_stamp(), json.dumps(out, ensure_ascii=False)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
