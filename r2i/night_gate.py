"""Hold a night task until the owner ticks it, then run it under paid_run with that item's NQ- budget and cap.

Start it from a session on the Mac, in the background, right after `nq-add` (or `--queue nq-add`):
  nohup python3 -B -m r2i.night_gate NQ-20300115-01 --purpose '…' --conclusion '…' -- CMD ARGS \
    > <runs_root>/<项目>/<run>/night-gate.log 2>&1 &
It reads only snapshot.json (written every minute by the host render), never the live sqlite. It exits without
running when the item is withdrawn, expires, or is still unknown after 10 minutes. Once approved it registers its
pid for the watchdog and replaces itself with paid_run.py, which enforces the cap.
"""
from pathlib import Path
import argparse
import json
import os
import sys
import time

from . import db, queue
from . import config

POLL_SECONDS = float(os.environ.get('NIGHT_GATE_POLL', 60))
UNKNOWN_GIVE_UP = 600
STATE = Path(os.environ.get('R2I_NIGHT_DIR') or config.state_dir() / 'night')
PAID_RUN = Path(__file__).with_name('paid_run.py')


def _item(nid):
    try:
        snap = json.loads(queue.SNAPSHOT.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return next((r for r in snap.get('night_items', []) if r['id'] == nid), None)


def wait_for_approval(nid, sleep=time.sleep, clock=time.time):
    """Return the approved item, or None with a reason printed. Polls snapshot.json."""
    started = clock()
    while True:
        item = _item(nid)
        if item is None:
            if clock() - started > UNKNOWN_GIVE_UP:
                print('night_gate: %s 不在 snapshot.json 里（10 分钟内没有出现），不跑' % nid, flush=True)
                return None
        else:
            if item['status'] in ('撤回', '过期'):
                print('night_gate: %s 已%s，不跑' % (nid, item['status']), flush=True)
                return None
            if db.parse_time(item['deadline']).timestamp() <= clock():
                print('night_gate: %s 过了截止时间，不跑' % nid, flush=True)
                return None
            if item['status'] == '已批' and item.get('budget_written_at'):
                return item
        sleep(POLL_SECONDS)


def register(item):
    runs = STATE / 'runs'
    runs.mkdir(parents=True, exist_ok=True)
    rec = {'item': item['id'], 'pid': os.getpid(), 'started_at': db.now(), 'deadline': item['deadline'],
           'cap_cny': item['cap_cny'], 'cwd': os.getcwd()}
    path = runs / ('%s-%d.json' % (item['id'], os.getpid()))
    tmp = path.with_name('.' + path.name)
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)
    return path


def paid_run_argv(item, args, command):
    argv = [sys.executable, '-B', str(PAID_RUN), '--budget', item['id'], '--cap', repr(float(item['cap_cny'])),
            '--purpose', args.purpose, '--conclusion', args.conclusion]
    if args.group:
        argv += ['--group', args.group]
    if args.call_cap:
        argv += ['--call-cap', args.call_cap]
    return argv + ['--'] + command


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    if '--' not in raw or raw.index('--') == len(raw) - 1:
        print("night_gate: 需要 '-- 命令 参数'", file=sys.stderr)
        return 2
    sep = raw.index('--')
    p = argparse.ArgumentParser(prog='night_gate')
    p.add_argument('item', help='NQ-YYYYMMDD-NN')
    p.add_argument('--purpose', required=True)
    p.add_argument('--conclusion', required=True)
    p.add_argument('--group')
    p.add_argument('--call-cap')
    args = p.parse_args(raw[:sep])
    command = raw[sep + 1:]
    item = wait_for_approval(args.item)
    if item is None:
        return 3
    register(item)
    print('night_gate: %s 已批准，交给 paid_run（上限 ¥%g）' % (item['id'], item['cap_cny']), flush=True)
    full = paid_run_argv(item, args, command)
    os.execv(full[0], full)  # same pid, so the watchdog can stop paid_run, which forwards the signal to the child


if __name__ == '__main__':
    sys.exit(main())
