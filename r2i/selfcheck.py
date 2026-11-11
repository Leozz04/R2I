"""`cli status`, the hourly self-check (com.r2i.<project>.selfcheck) and the post-reboot checklist.

status is read-only. Exit code: 0 all fine; 1 a job is down and can be restarted; 2 something is broken, do not restart
(ledger quick_check fails, vault missing); 3 the owner has to act (disk below the floor). With several problems the first hit
in the order 2, 3, 1, 0 wins. The self-check restarts only the level-1 jobs that are not on the manual-stop list, and writes
agent/管理/重启恢复.md after a reboot. The Mac runs it; a VM cannot (no launchctl)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import sys
import re
import sqlite3
import subprocess
import shutil
import time

from . import db, modes, waits
from . import config

JOBS_FILE = config.state_dir() / 'jobs.json'  # the project's resident-job registry; absent = the shipped jobs.default.json
DEFAULT_JOBS = Path(__file__).with_name('jobs.default.json')


def load_jobs(path=None):
    """The registry of LaunchAgents (jobs.json): (observe_until, [{'label','what','schedule','heartbeat','max_age_seconds',
    'kind','restart','install','runner','target_thread','why_model', 'mode'?}]). 'mode' names the modes.py automation that governs restarting a job added later
    (jobs-v2)."""
    path = Path(path or (JOBS_FILE if JOBS_FILE.exists() else DEFAULT_JOBS))
    data = json.loads(path.read_text(encoding='utf-8'))
    for j in data['jobs']:  # "@name" = this project's launchd label for that job
        if j['label'].startswith('@'):
            j['label'] = config.label(j['label'][1:])
    return data.get('observe_until', ''), data['jobs']


def load_threads(conn):
    """The threads table: {thread id: status ('open'|'closed')}."""
    return {r['id']: r['status'] for r in conn.execute('SELECT id, status FROM threads')}


OBSERVE_UNTIL, _JOBS = load_jobs()
# the six original jobs keep their old handling; jobs with a 'mode' are the new ones (selfcheck, repo-sync, wake-bridge)
HEARTBEATS = tuple((j['label'], j['heartbeat'], j['max_age_seconds']) for j in _JOBS if j['heartbeat'] and not j.get('mode'))
KEEPALIVE = tuple(j['label'] for j in _JOBS if j['kind'] == 'keepalive' and not j.get('mode'))  # launchd shows these as running all the time
NEW_JOBS = {j['label']: j for j in _JOBS if j.get('mode')}
REGISTERED = {j['label'] for j in _JOBS}
SELF = config.label('selfcheck')
SELF_BEAT = 'agent/管理/.selfcheck-heartbeat'
STATUS_PAGE = 'agent/管理/常驻脚本.md'
AGENTS_DIR = Path.home() / 'Library/LaunchAgents'
QUEUE_MAX = 5 * 60
MIN_INTERNAL_GB = 10.0
MIN_EXTERNAL_GB = 40.0
RECOVERY = 'agent/管理/重启恢复.md'
STOP_KEY, BOOT_KEY, STATE_KEY, NOTIFIED_KEY, RESTART_KEY = 'selfcheck-stopped', 'boot-time', 'selfcheck-state', 'selfcheck-notified', 'restart'
OK = '正常'


# ---- probes (patched in tests) ----

def launchd_state(label):
    """'running', 'not running', or None when launchd does not know the job (or there is no launchctl)."""
    try:
        out = subprocess.run(['launchctl', 'print', 'gui/%d/%s' % (os.getuid(), label)], capture_output=True, text=True,
                             timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    return 'running' if re.search(r'state = running', out.stdout) else 'not running'


def launchd_last_exit(label):
    """None when launchd does not know the job; else its 「last exit code」 (0 when it has never exited or is unparsable)."""
    try:
        out = subprocess.run(['launchctl', 'print', 'gui/%d/%s' % (os.getuid(), label)], capture_output=True, text=True,
                             timeout=15)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if out.returncode or not isinstance(out.stdout, str):
        return None
    m = re.search(r'last exit code\s*=\s*(\d+)', out.stdout)
    return int(m.group(1)) if m else 0


def installed_labels():
    """Labels of ~/Library/LaunchAgents/com.r2i.<project>.*.plist (patched in tests)."""
    try:
        return sorted(p.name[:-len('.plist')] for p in AGENTS_DIR.glob(config.label('*') + '.plist'))
    except OSError:
        return []


def process_running(name):
    try:
        return subprocess.run(['pgrep', '-x', name], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


def mounted(path):
    return os.path.ismount(path)


def quick_check(db_path):
    try:
        c = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, timeout=5)
        try:
            return c.execute('PRAGMA quick_check').fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error as e:
        return 'error: %s' % e


def boot_time():
    """Epoch seconds of the last boot (macOS kern.boottime, else /proc/stat), or None."""
    try:
        out = subprocess.run(['sysctl', '-n', 'kern.boottime'], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'sec\s*=\s*(\d+)', out)
        if m:
            return int(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        for line in Path('/proc/stat').read_text().splitlines():
            if line.startswith('btime '):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def kickstart(label):
    r = subprocess.run(['launchctl', 'kickstart', '-k', 'gui/%d/%s' % (os.getuid(), label)], capture_output=True, text=True,
                       timeout=30)
    return r.returncode == 0


# ---- restart after a code deploy ----

STALE_KEY = 'stale_code_restart_at'
STALE_GAP = 600


def _etime_seconds(text):
    """ps etime `[[dd-]hh:]mm:ss` -> seconds."""
    text = text.strip()
    days = 0
    if '-' in text:
        d, text = text.split('-', 1)
        days = int(d)
    parts = [int(x) for x in text.split(':')]
    if not 2 <= len(parts) <= 3:
        raise ValueError(text)
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return days * 86400 + sec


def restart_if_stale_code(conn, vault, log_change, label=None, now=None, run=subprocess.run, pkg=None):
    label = label or config.label('report-reply')
    """A KeepAlive job keeps running the code it started with. If the package's .py files are newer than the job's
    start (and the copy has finished), kickstart it once; at most once per 10 minutes. Any error: quietly nothing."""
    try:
        mode = modes.get(conn, 'selfcheck-kickstart')
        if mode == 'off':
            return None
        now_ts = (now or datetime.now(timezone.utc)).timestamp()
        out = run(['launchctl', 'print', 'gui/%d/%s' % (os.getuid(), label)], capture_output=True, text=True, timeout=30)
        m = re.search(r'^\s*pid = (\d+)', out.stdout or '', re.M)
        if out.returncode != 0 or not m:
            return None
        et = run(['ps', '-o', 'etime=', '-p', m.group(1)], capture_output=True, text=True, timeout=30)
        if et.returncode != 0 or not (et.stdout or '').strip():
            return None
        started = now_ts - _etime_seconds(et.stdout)
        pkg = Path(pkg) if pkg else Path(__file__).resolve().parent
        newest = max((f.stat().st_mtime for f in pkg.glob('*.py')), default=0)
        if newest <= started + 2 or now_ts - newest < 10:
            return None
        last = db.meta_get(conn, STALE_KEY)
        if isinstance(last, (int, float)) and now_ts - last < STALE_GAP:
            return None
        if mode == 'observe':
            modes.would(conn, vault, log_change, 'selfcheck-kickstart', '首页', '代码更新后重启 %s' % label)
            return 'would'
        db.meta_set(conn, STALE_KEY, now_ts)
        ok = False
        try:
            ok = kickstart(label) if run is subprocess.run else \
                run(['launchctl', 'kickstart', '-k', 'gui/%d/%s' % (os.getuid(), label)], capture_output=True, text=True,
                    timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
        log_change(vault, '自检', '首页', '代码更新后重启 %s%s' % (label, '' if ok else '（没成功）'))
        return 'restarted' if ok else 'failed'
    except Exception:
        return None


# ---- status ----

def _ago(seconds):
    if seconds is None:
        return '没有记录'
    if seconds < 90:
        return '%d 秒前' % seconds
    if seconds < 5400:
        return '%d 分钟前' % round(seconds / 60)
    if seconds < 172800:
        return '%d 小时前' % round(seconds / 3600)
    return '%d 天前' % round(seconds / 86400)


def _beat(path, now):
    try:
        m = path.stat().st_mtime
    except OSError:
        return None, None
    return datetime.fromtimestamp(m, db.LOCAL_TZ).isoformat(timespec='seconds'), max(0, int(now.timestamp() - m))


def _part(name, status, level, detail='', last=None, age=None, restart=None):
    return {'name': name, 'status': status, 'level': level, 'detail': detail, 'last': last, 'age_seconds': age,
            'restart': restart}


def _new_job_part(j, vault, now):
    """A job added after the original six. Not installed (launchd does not know it and it never beat) is level 0, so nothing
    alarms before the owner installs it. Restarts of these go through modes 'jobs-v2'; selfcheck itself is watched by render
    (watch_selfcheck): a kickstart from inside its own run would kill the run."""
    label = j['label']
    restart = label if j['restart'] == 'auto' and label != SELF else None
    last, age = _beat(Path(vault) / j['heartbeat'], now) if vault and j['heartbeat'] else (None, None)
    if j['heartbeat']:
        state = launchd_state(label)
        if age is None and (state is None or label not in installed_labels()):
            return _part(label, '没装', 0, '还没装')
        if age is None:
            return _part(label, '没有心跳', 1, '没有心跳', restart=restart)
        if age > j['max_age_seconds']:
            return _part(label, '心跳过期', 1, _ago(age), last, age, restart)
        if j['kind'] == 'keepalive' and state != 'running':
            return _part(label, '没在跑', 1, 'launchd 显示没在跑', last, age, restart)
        return _part(label, OK, 0, _ago(age), last, age)
    code = launchd_last_exit(label)  # no heartbeat: loaded + last exit code
    if code is None:
        return _part(label, '没装', 0, '还没装')
    if code:
        return _part(label, '上次失败', 1, '退出码 %d' % code)
    return _part(label, OK, 0, '已加载，上次正常退出')


def status(vault, db_path, now=None):
    """Every part with its state, and the overall exit code."""
    from . import queue, watchdog
    now = now or datetime.now(timezone.utc)
    parts = []
    for label, rel, limit in HEARTBEATS:
        last, age = _beat(Path(vault) / rel, now) if vault else (None, None)
        state = launchd_state(label) if label in KEEPALIVE else 'running'
        if age is None:
            parts.append(_part(label, '没有心跳', 1, '没有心跳', restart=label))
        elif age > limit:
            parts.append(_part(label, '心跳过期', 1, _ago(age), last, age, label))
        elif state != 'running':
            parts.append(_part(label, '没在跑', 1, 'launchd 显示没在跑', last, age, label))
        else:
            parts.append(_part(label, OK, 0, _ago(age), last, age))
    for j in _JOBS:
        if j.get('mode'):
            parts.append(_new_job_part(j, vault, now))
    for label in installed_labels():
        if label not in REGISTERED:
            parts.append(_part(label, '没登记', 1, '没在 jobs.json 里登记'))
    stuck = []
    try:
        for f in Path(queue.QUEUE).glob('*.json'):
            age = now.timestamp() - f.stat().st_mtime
            if age > QUEUE_MAX:
                stuck.append(age)
    except OSError:
        pass
    parts.append(_part('queue', OK if not stuck else '有积压', 0 if not stuck else 1,
                       '' if not stuck else '%d 个文件 %s' % (len(stuck), _ago(max(stuck))), restart=config.label('r2i-sync') if stuck else None))
    qc = quick_check(db_path)
    parts.append(_part('台账', OK if qc == 'ok' else '坏了', 0 if qc == 'ok' else 2, '' if qc == 'ok' else qc[:80]))
    vault_ok = bool(vault) and Path(vault).is_dir() and os.access(vault, os.W_OK)
    parts.append(_part('vault', OK if vault_ok else '不在或不可写', 0 if vault_ok else 2, '' if vault_ok else str(vault)))
    low = []
    try:
        internal = free_gb('/')
        if internal < MIN_INTERNAL_GB:
            low.append('内置盘只剩 %.1f GB' % internal)
        external = watchdog.external_mount()
        if external is None:
            pass
        elif not mounted(external):
            low.append('外置盘没挂载')
        else:
            ext = free_gb(external)
            if ext < MIN_EXTERNAL_GB:
                low.append('外置盘只剩 %.1f GB' % ext)
    except OSError as e:
        low.append('读不到磁盘：%s' % e)
    parts.append(_part('磁盘', OK if not low else '不足', 0 if not low else 3, '；'.join(low)))
    levels = {p['level'] for p in parts}
    code = 2 if 2 in levels else 3 if 3 in levels else 1 if 1 in levels else 0
    return {'code': code, 'checked_at': now.astimezone(db.LOCAL_TZ).isoformat(timespec='seconds'), 'components': parts}


def stopped(conn):
    return list(db.meta_get(conn, STOP_KEY, []))


def stop(conn, label, resume=False):
    cur = [x for x in stopped(conn) if x != label]
    if not resume:
        cur.append(label)
    db.meta_set(conn, STOP_KEY, cur)
    return cur


# ---- the hourly self-check ----

def run(conn, vault, db_path, log_change, now=None, kick=None, sender=None):
    """One hourly pass: status, restart jobs that died (not the manually stopped), notice a reboot, notify once."""
    now = now or datetime.now(timezone.utc)
    kick = kick or kickstart
    if vault:  # beat first: this pass is the proof selfcheck runs, so its own row must not read 没有心跳 / 过期
        try:
            from . import render
            render._write(Path(vault) / SELF_BEAT, datetime.now(db.LOCAL_TZ).isoformat(timespec='seconds') + '\n')
        except OSError:
            pass
    st = status(vault, db_path, now)
    out = {'code': st['code'], 'restarted': [], 'skipped': [], 'rebooted': False, 'notified': []}
    manual = stopped(conn)
    kmode = modes.get(conn, 'selfcheck-kickstart')  # off = no restarts, observe = only say what it would do
    vmode = modes.get(conn, 'jobs-v2')  # the same switch for the jobs added later (selfcheck, repo-sync, wake-bridge)
    out['would'] = []
    failed = set()
    if st['code'] == 1:
        for p in st['components']:
            label = p['restart']
            if p['level'] != 1 or not label or label in out['restarted'] or label in out['skipped']:
                continue
            if label in manual:
                out['skipped'].append(label); continue
            name, mode = ('jobs-v2', vmode) if label in NEW_JOBS else ('selfcheck-kickstart', kmode)
            if mode == 'off':
                continue
            if mode == 'observe':
                out['would'].append(label)
                modes.would(conn, vault, log_change, name, '首页', 'kickstart -k %s（%s %s）' % (label, p['name'], p['status']))
                continue
            ok = False
            try:
                ok = kick(label)
            except (OSError, subprocess.SubprocessError):
                pass
            out['restarted'].append(label)
            if not ok:
                failed.add(label)
            log_change(vault, '自检', '首页', '%s %s，kickstart -k %s'
                       % (label, p['status'], '成功' if ok else '没成功'))
    boot = boot_time()
    last = db.meta_get(conn, BOOT_KEY)
    if boot is not None and last is not None and abs(boot - last) > 5:
        out['rebooted'] = True
        start_recovery(conn, vault, boot, log_change, now)
    if boot is not None:
        db.meta_set(conn, BOOT_KEY, boot)
    problems = [p for p in st['components'] if p['level']]
    db.meta_set(conn, STATE_KEY, {'at': st['checked_at'], 'code': st['code'], 'problems': [
        {'name': p['name'], 'status': p['status'], 'detail': p['detail'] or _ago(p['age_seconds']), 'level': p['level'],
         'failed': bool(p['restart'] and p['restart'] in failed)} for p in problems]})
    # one notification per problem until it clears; none at night, the held ones go together after 09:00
    notified = db.meta_get(conn, NOTIFIED_KEY, {})
    keys = {'%s|%s' % (p['name'], p['status']) for p in problems}
    notified = {k: v for k, v in notified.items() if k in keys}
    fresh = [p for p in problems if '%s|%s' % (p['name'], p['status']) not in notified]
    if fresh and not waits.quiet(now):
        text = '；'.join('%s %s' % (p['name'].replace(config.label(''), ''), p['status']) for p in fresh)
        try:
            if sender is not None or not os.environ.get('R2I_NO_PUSH'):
                (sender or waits.send)('%s：系统自检' % config.get().project, text)
        except Exception:
            pass
        if vmode == 'auto':  # the phone too (names and status only); in observe the Mac notification stays the only one
            try:
                waits.push_text_raw(text)
            except Exception:
                pass
        elif vmode == 'observe':
            modes.would(conn, vault, log_change, 'jobs-v2', '首页', '会推 ntfy: %s' % text)
        for p in fresh:
            notified['%s|%s' % (p['name'], p['status'])] = now.isoformat(timespec='seconds')
        out['notified'] = [p['name'] for p in fresh]
    db.meta_set(conn, NOTIFIED_KEY, notified)
    extra_checks(conn, vault, log_change, now, sender, out)
    try:
        write_status_page(conn, vault, st, log_change)
    except OSError:
        pass
    return out


# ---- C1 / F1 / F2 / I2: the extra hourly checks ----

PAID_STALE = 15 * 60    # C1: a paid_run beat older than this with no end file
PAID_NOTIFIED_KEY = 'paid-heartbeat-notified'
IDLE_SECONDS = 7 * 86400  # I2
UNKNOWN = '无法判断'


def stale_paid_runs(now):
    """C1: [(run_id, seconds since its last beat)] for paid_run beats older than 15 min that have no end file."""
    from . import watchdog
    d = Path(watchdog.LEDGER).parent / 'heartbeat'
    out = []
    for beat in sorted(d.glob('*.beat')) if d.is_dir() else []:
        try:
            age = now.timestamp() - beat.stat().st_mtime
        except OSError:
            continue
        if age > PAID_STALE and not beat.with_suffix('.end').exists():
            out.append((beat.stem, int(age)))
    return out


def jobs_thread_problems(jobs=None, threads=None, conn=None):
    """F1: [(label, target_thread, 'missing'|'closed')] for jobs whose target_thread is not an open row in the threads table.
    `threads` is {id: status}; without it and without `conn` nothing is known and no job is a problem."""
    threads = threads if threads is not None else load_threads(conn) if conn is not None else None
    if threads is None:
        return []
    out = []
    for j in _JOBS if jobs is None else jobs:
        t = j.get('target_thread')
        if t and t not in threads:
            out.append((j['label'], t, 'missing'))
        elif t and threads[t] == 'closed':
            out.append((j['label'], t, 'closed'))
    return out


def idle_jobs(vault, now, jobs=None):
    """I2: ([labels with no output for a week], [labels that cannot be judged]). Evidence is the heartbeat file, else the job's
    optional 'log' path; a job with neither (or whose file is absent) is 无法判断, never flagged."""
    idle, unknown = [], []
    for j in _JOBS if jobs is None else jobs:
        path = Path(vault) / j['heartbeat'] if vault and j.get('heartbeat') else \
            Path(j['log']).expanduser() if j.get('log') else None
        try:
            age = now.timestamp() - path.stat().st_mtime if path else None
        except OSError:
            age = None
        if age is None:
            unknown.append(j['label'])
        elif age > IDLE_SECONDS:
            idle.append(j['label'])
    return idle, unknown


def _report(conn, vault, log_change, name, rel, text):
    """A report-only item: observe = 「本来会做」 line, auto = one change-log line, each at most once per distinct text per day."""
    mode = modes.get(conn, name)
    if mode == 'observe':
        return modes.would(conn, vault, log_change, name, rel, text)
    if mode != 'auto':
        return False
    day = datetime.now(db.LOCAL_TZ).strftime('%Y-%m-%d')
    key = 'reported:%s' % name
    if db.meta_get(conn, key) == [day, text]:
        return False
    db.meta_set(conn, key, [day, text])
    log_change(vault, '自检', rel, text)
    return True


MS_STALE_DAYS = 3
QUEUE_STALE_MIN = 10


def milestone_gaps(conn, vault, now=None):
    """One line per mainline whose newest live milestone is older than 3 days, and per mainline whose newest 版本变更
    milestone is newer than its last approved structure while no page 「<主线> 结构改成 v<N+1>」 exists anywhere in the vault."""
    now = now or datetime.now(timezone.utc)
    names = {p.stem for p in Path(vault).rglob('*结构改成 v*.md')} if vault else set()
    out = []
    for ml in db.MAINLINES:
        last = conn.execute('SELECT MAX(created_at) FROM milestones WHERE mainline=? AND superseded_by IS NULL', (ml,)).fetchone()[0]
        if not last or db.parse_time(last) < now - timedelta(days=MS_STALE_DAYS):
            out.append('%s 超过 %d 天没有新里程碑（最近 %s）' % (db.SHORT_NAMES[ml], MS_STALE_DAYS, (last or '无')[:10]))
        ver, at = conn.execute('SELECT MAX(version), MAX(created_at) FROM structures WHERE mainline=?', (ml,)).fetchone()
        frz = conn.execute("SELECT id, created_at FROM milestones WHERE mainline=? AND kind='版本变更' AND superseded_by IS NULL "
                           'ORDER BY created_at DESC LIMIT 1', (ml,)).fetchone()
        nxt = (ver or 0) + 1
        if frz and (not at or frz['created_at'] > at) and not any(n.endswith('%s 结构改成 v%d' % (db.SHORT_NAMES[ml], nxt)) for n in names):
            out.append('%s 有新冻结 %s，但没开「结构改成 v%d」页' % (db.SHORT_NAMES[ml], frz['id'], nxt))
    return out


def budget_mismatches(conn, path=None):
    """[(id, what)] for 有效 verbal budgets whose BUDGETS.json entry (what paid_run enforces) has another expiry or cap,
    or is missing although the row says it was written."""
    from . import night
    try:
        file = json.loads(Path(path or night.BUDGETS).read_text(encoding='utf-8')).get('budgets') or {}
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for r in conn.execute("SELECT id, cap_cny, expires_at, budget_written_at FROM verbal_budgets WHERE status='有效'"):
        b = file.get(r['id'])
        if b is None:
            if r['budget_written_at']:
                out.append((r['id'], 'BUDGETS.json 里没有'))
            continue
        if (r['expires_at'] or '') != (b.get('expires_at') or ''):
            out.append((r['id'], '到期 台账 %s ≠ 文件 %s' % (r['expires_at'], b.get('expires_at'))))
        total = b.get('orig_total_cny', b.get('total_cny'))
        if total is not None and float(r['cap_cny']) != float(total):
            out.append((r['id'], '金额 台账 ¥%g ≠ 文件 ¥%g' % (r['cap_cny'], float(total))))
    return out


def queue_stale(now_ts=None, folder=None):
    """Names of queue files older than QUEUE_STALE_MIN minutes in the queue root (applied ones go to done/ or failed/)."""
    from . import queue
    folder = Path(folder or queue.QUEUE)
    now_ts = now_ts or time.time()
    return sorted(f.name for f in (folder.glob('*.json') if folder.is_dir() else [])
                  if now_ts - f.stat().st_mtime > QUEUE_STALE_MIN * 60)


def reply_wait_alive(conn, thread):
    """A reply-wait for this thread started and has not ended, and its process (pid kept in the meta row) still exists."""
    m = db.meta_get(conn, 'reply-wait:' + thread)
    if not isinstance(m, dict) or m.get('end'):
        return False
    try:
        os.kill(int(m['pid']), 0)
    except ProcessLookupError:
        return False
    except (KeyError, ValueError, TypeError, OSError):
        return True  # a wait started before pids were kept counts as alive
    return True


def unwatched(conn):
    """[(thread, open waits, undelivered answers)] for threads that have something for them and no live reply-wait."""
    rows = {}
    for t, n in conn.execute("SELECT thread, COUNT(*) FROM waits WHERE status='等待' AND thread!='' GROUP BY thread"):
        rows.setdefault(t, [0, 0])[0] = n
    for t, n in conn.execute("SELECT thread, COUNT(*) FROM report_replies WHERE delivered_at IS NULL AND thread IS NOT NULL "
                             "AND thread!='' GROUP BY thread"):
        rows.setdefault(t, [0, 0])[1] = n
    return [(t, w, u) for t, (w, u) in sorted(rows.items()) if not reply_wait_alive(conn, t)]


def extra_checks(conn, vault, log_change, now, sender, out):
    """C1, F1, I2 and F2 of the hourly pass; each in its own try so one broken check cannot stop the others or the pass."""
    from . import watchdog

    def guard(fn):
        try:
            fn()
        except Exception as e:  # keep the pass going, but say what broke (a launchd-only failure is invisible otherwise)
            out.setdefault('extra_errors', []).append('%s: %s: %s' % (fn.__name__, type(e).__name__, e))
            print('selfcheck %s failed: %r' % (fn.__name__, e), file=sys.stderr)

    def paid():
        stale = stale_paid_runs(now)
        done = [r for r in db.meta_get(conn, PAID_NOTIFIED_KEY, [])
                if r in {x for x, _ in stale}]  # forget runs whose beat is gone
        fresh = [(r, a) for r, a in stale if r not in done]
        mode = modes.get(conn, 'paid-heartbeat-push')
        if mode == 'off' or not fresh:
            return
        for r, age in fresh:
            text = '付费运行 %s 的心跳停了 %d 分钟，也没有结束记录' % (r[:8], age // 60)
            if mode == 'observe':
                modes.would(conn, vault, log_change, 'paid-heartbeat-push', '首页', '会推 ntfy: %s' % text)
                continue
            try:
                if sender is not None or not os.environ.get('R2I_NO_PUSH'):
                    (sender or waits.send)('%s：付费运行' % config.get().project, text)
            except Exception:
                pass
            waits.push_text_raw(text)
            done.append(r)
            out.setdefault('paid_stale', []).append(r)
        if mode == 'auto':
            db.meta_set(conn, PAID_NOTIFIED_KEY, done)

    def threads():
        bad = jobs_thread_problems(conn=conn)
        out['thread_problems'] = bad
        if bad:
            _report(conn, vault, log_change, 'jobs-thread-check', '首页', 'jobs.json 的目标线程有问题：' + '；'.join(
                '%s → %s %s' % (_short_label(lab), t[:12], '不在 threads 表' if why == 'missing' else '已关闭') for lab, t, why in bad))

    def idle():
        idle, unknown = idle_jobs(vault, now)
        out['idle'], out['idle_unknown'] = idle, unknown
        if idle:
            _report(conn, vault, log_change, 'jobs-idle', '首页', '常驻脚本一周没产出：%s%s' % (
                '、'.join(_short_label(x) for x in idle), '（%s：%s）' % (UNKNOWN, '、'.join(_short_label(x) for x in unknown)) if unknown else ''))

    def backup():
        mode = modes.get(conn, 'vault-backup')
        if mode == 'off' or not vault:
            return
        from . import vault_backup
        if mode == 'observe':
            modes.would(conn, vault, log_change, 'vault-backup', '首页', '备份各主线交接和项目规则到 %s' % vault_backup.default_dest())
        elif watchdog.external_mount() is None or mounted(watchdog.external_mount()):  # never fall back to the internal disk
            out['backup'] = str(vault_backup.backup_with_timeout(vault, vault_backup.default_dest(), now))  # 60 s cap; a hang is skipped

    def counts():
        daily_counts(conn, vault, log_change, now)

    def budgets():
        bad = budget_mismatches(conn)
        out['budget_mismatch'] = bad
        if bad:
            _report(conn, vault, log_change, 'budget-mismatch', '首页', '预算台账和 BUDGETS.json 对不上（paid_run 按文件执行）：'
                    + '；'.join('%s %s' % (i, w) for i, w in bad))

    def queue_age():
        old = queue_stale()
        out['queue_stale'] = old
        if old:
            _report(conn, vault, log_change, 'queue-stale', '首页', '队列里有 %d 个文件超过 %d 分钟没应用（最早 %s）：看 host 的运行日志'
                    % (len(old), QUEUE_STALE_MIN, old[0][:15]))

    def watchers():
        rows = unwatched(conn)
        out['unwatched'] = [t for t, _, _ in rows]
        if rows:
            _report(conn, vault, log_change, 'reply-wait-unwatched', '首页', '这些线程有等答复或没送到的答复，但没挂 reply-wait（调度者叫醒它们重挂）：'
                    + '；'.join('%s 等 %d · 未送 %d' % (t[:14], w, u) for t, w, u in rows))

    def milestones():
        gaps = milestone_gaps(conn, vault, now)
        out['milestone_gaps'] = gaps
        if gaps:
            _report(conn, vault, log_change, 'milestone-gap', '首页', '主线进度：' + '；'.join(gaps))

    for fn in (paid, threads, idle, backup, counts, queue_age, watchers, budgets, milestones):
        guard(fn)




# ---- V5-SELFCHECK-COUNTS: one count a day of the things that tend to grow ----

COUNTS_KEY = 'selfcheck-counts'
LIMIT_RULES, LIMIT_OBSERVE = 35, 12
# what may sit in the vault root (dot-entries such as .obsidian/ are not pages and are ignored)
ROOT_PAGES = {'首页.md', '知识库.md', '项目规则.md', '资源.md', '今晚可跑.md', 'CLAUDE.md', 'sortspec.md'}
ROOT_DIRS = {'agent', '会议', '术语', '每日', '汇报', '进度'}
MEMORY_NOTE = '每周查一次'


def rule_count(vault):
    """Rules in the 总则 part of the rule table (from '# 总则' to the next top-level heading); None without the page."""
    from . import rulecheck
    rel = rulecheck.rule_page(vault) if vault else None
    if not rel:
        return None
    text = (Path(vault) / rel).read_text(encoding='utf-8')
    m = re.search(r'(?m)^# 总则[^\n]*\n(.*?)(?=^# |\Z)', text, re.S)
    return len(rulecheck.rule_blocks(m.group(1))) if m else None


def count_all(conn, vault):
    """{'rules', 'memory', 'observe', 'handover': {mainline: tokens}, 'root': [pages]} and the over-limit lines."""
    from . import handover
    out = {'rules': rule_count(vault), 'memory': MEMORY_NOTE, 'observe': len(modes.observing(conn)), 'handover': {}, 'root': []}
    for ml, rel in handover.PAGES.items():
        try:
            out['handover'][ml] = handover._tokens((Path(vault) / rel).read_text(encoding='utf-8'))
        except OSError:
            pass
    root = Path(vault) if vault else None
    out['root'] = sorted(f.name + ('/' if f.is_dir() else '') for f in root.iterdir()
                         if not f.name.startswith('.') and (f.is_dir() or f.suffix == '.md')) if root and root.is_dir() else []
    over = []
    if out['rules'] is not None and out['rules'] > LIMIT_RULES:
        over.append('项目规则总则 %d 条，超过上限 %d' % (out['rules'], LIMIT_RULES))
    over += ['交接 %s 约 %d token，超过上限 %d' % (ml, n, handover.TOKEN_MAX) for ml, n in out['handover'].items() if n > handover.TOKEN_MAX]
    if out['observe'] > LIMIT_OBSERVE:
        over.append('observe 中的开关 %d 个，超过上限 %d' % (out['observe'], LIMIT_OBSERVE))
    over += ['vault 根目录多了%s：%s（不在允许的根目录清单里）' % ('文件夹' if n.endswith('/') else '页', n.rstrip('/'))
             for n in out['root'] if not ((n[:-1] in ROOT_DIRS) if n.endswith('/') else (n in ROOT_PAGES))]
    return out, over


def daily_counts(conn, vault, log_change, now=None):
    """Once per local day: count, keep the result for 首页's 「给线程看」 (count_lines) and write one 变更 line."""
    if not vault:
        return None
    day = (now or datetime.now(timezone.utc)).astimezone(db.LOCAL_TZ).strftime('%Y-%m-%d')
    prev = db.meta_get(conn, COUNTS_KEY) or {}
    if prev.get('day') == day:
        return None
    out, over = count_all(conn, vault)
    db.meta_set(conn, COUNTS_KEY, {'day': day, 'over': over})
    log_change(vault, '自检', '首页', '每日计数：总则 %s 条 · 记忆主题文件 %s · observe 开关 %d 个 · 交接 token %s · 根目录页 %d 个%s' % (
        out['rules'] if out['rules'] is not None else '—', out['memory'], out['observe'],
        '/'.join(str(n) for n in out['handover'].values()) or '—', len(out['root']), '；超限：' + '；'.join(over) if over else ''))
    return out


def count_lines(conn):
    """The lines for 首页's 「给线程看」: what the last daily count found over its limit."""
    return ['⚠ ' + t for t in (db.meta_get(conn, COUNTS_KEY) or {}).get('over', [])]


# ---- agent/管理/常驻脚本.md ----

def _short_label(label):
    return label.replace(config.label(''), '')


def _hhmm(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M')
    except (TypeError, ValueError):
        return '—'


def status_page(conn, st):
    """Byte-stable text: the last heartbeat is shown to the minute (local), no 「N 分钟前」."""
    comp = {p['name']: p for p in st['components']}
    vmode = modes.get(conn, 'jobs-v2')
    rows, good = [], 0
    for j in _JOBS:
        p = comp.get(j['label'])
        status_ = p['status'] if p else '—'
        good += status_ in (OK, '无变化')
        if j['restart'] == 'alert':
            fix = '只报警'
        elif j.get('mode') and vmode == 'observe':
            fix = '观察中（到 %s）' % OBSERVE_UNTIL if OBSERVE_UNTIL else '观察中'
        elif j.get('mode') and vmode == 'off':
            fix = '已关'
        else:
            fix = '自动拉起'
        if j['label'] == SELF and fix == '自动拉起':
            fix = '自动拉起（由首页那一轮看着）'
        rows.append('| %s | %s | %s | %s | %s | %s |' % (_short_label(j['label']), j['what'], j['schedule'], status_,
                                                        _hhmm(p['last']) if p and p.get('last') else '—', fix))
    for label, p in sorted((n, c) for n, c in comp.items() if c['status'] == '没登记'):
        rows.append('| %s | — | — | 没登记 | — | 只报警 |' % _short_label(label))
    total = len(_JOBS)
    return '\n'.join(['---', 'cssclasses: [wide]', '---', '', '# 常驻脚本', '',
                      '> [!summary] %d/%d 正常' % (good, total), '',
                      '本页由自检脚本写，不手改；清单在项目的 `.r2i/jobs.json`（没有就用包里的 jobs.default.json）。', '',
                      '| 脚本 | 干什么 | 多久一次 | 状态 | 最后心跳 | 掉了怎么办 |', '| --- | --- | --- | --- | --- | --- |']
                     + rows + [''])


def write_status_page(conn, vault, st, log_change):
    """Write the page only when its content changed, one log line per change. Written in every mode (it is read-only info)."""
    if not vault:
        return False
    db.meta_set(conn, 'jobs-status', {p['name']: [p['status'], _hhmm(p['last']) if p.get('last') else '—'] for p in st['components']})  # for programs.rows
    from . import render
    page = Path(vault) / STATUS_PAGE
    text = status_page(conn, st)
    try:
        if page.exists():
            cur = page.read_text(encoding='utf-8')
            if cur == text or render.AUTO_START in cur:  # V5-PROGRAM-LIST: render's hourly inventory owns the page now
                return False
    except (OSError, UnicodeDecodeError):
        pass
    render._write(page, text)
    log_change(vault, '自检', STATUS_PAGE, '常驻脚本状态有变化')
    return True


# ---- watching the watcher (called by the 60 s render pass) ----

WATCH_KEY, WATCH_MAX_AGE, WATCH_GAP = 'selfcheck-watch-at', 7200, 3600


def watch_selfcheck(conn, vault, log_change, now=None, kick=None):
    """The self-check's own heartbeat is older than 2 h: kickstart com.r2i.<project>.selfcheck, at most once per hour. Governed by
    modes 'jobs-v2' (observe: only says what it would do). Returns 'restarted' / 'failed' / 'would' / None; never raises."""
    try:
        beat = Path(vault) / SELF_BEAT
        if not beat.exists():
            return None
        now_ts = (now or datetime.now(timezone.utc)).timestamp()
        if now_ts - beat.stat().st_mtime <= WATCH_MAX_AGE:
            return None
        mode = modes.get(conn, 'jobs-v2')
        if mode == 'off':
            return None
        last = db.meta_get(conn, WATCH_KEY)
        if isinstance(last, (int, float)) and now_ts - last < WATCH_GAP:
            return None
        if mode == 'observe':
            modes.would(conn, vault, log_change, 'jobs-v2', '首页', '自检的心跳超过 2 小时，kickstart -k %s' % SELF)
            return 'would'
        db.meta_set(conn, WATCH_KEY, now_ts)
        ok = False
        try:
            ok = (kick or kickstart)(SELF)
        except (OSError, subprocess.SubprocessError):
            pass
        log_change(vault, '自检', '首页', '自检的心跳超过 2 小时，kickstart -k %s%s' % (SELF, '' if ok else '（没成功）'))
        return 'restarted' if ok else 'failed'
    except Exception:
        return None


def _quiet_problem(p):
    """A job the hourly pass restarts by itself (level 1) and has not failed to restart: no reason to bother the owner at the top."""
    return p.get('level', 2) == 1 and not p.get('failed')


def system_lines(conn):
    """首页 lines above 「要你定」: nothing when all is well. Only what the owner may need to know: disk, ledger, vault, a failed restart."""
    state = db.meta_get(conn, STATE_KEY)
    if not state:
        return []
    return ['- ⚠ 系统：%s %s · %s' % (p['name'].replace(config.label(''), ''), p['status'], p['detail'] or '—')
            for p in state.get('problems', []) if not _quiet_problem(p)]


def quiet_rows(conn):
    """Rows for 首页's folded 「转发中」 block: problems the self-check handles by itself (byte-stable, no times)."""
    state = db.meta_get(conn, STATE_KEY)
    return ['系统 · %s %s · 已自动重启，不用你管' % (p['name'].replace(config.label(''), ''), p['status'])
            for p in (state or {}).get('problems', []) if _quiet_problem(p)]


# ---- after a reboot ----

def _iso_to_dt(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None


def _unfinished_runs(boot):
    """paid_run starts before `boot` that have no end record: [{'run_id','code','time'}]."""
    from . import watchdog
    runs, order = {}, []
    try:
        lines = Path(watchdog.LEDGER).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('event') == 'start':
            runs[r.get('run_id')] = {'run_id': r.get('run_id'), 'code': r.get('group') or 'budget:%s' % r.get('budget'),
                                     'time': r.get('time'), 'ended': False}
            order.append(r.get('run_id'))
        elif r.get('event') == 'end' and r.get('run_id') in runs:
            runs[r['run_id']]['ended'] = True
    out = []
    for rid in order:
        r = runs[rid]
        t = _iso_to_dt(r['time'])
        if not r['ended'] and t and t.timestamp() < boot:
            out.append(r)
    return out


def _ledger_ended(run_id):
    """True when the ledger has an end record for this run."""
    from . import watchdog
    try:
        lines = Path(watchdog.LEDGER).read_text(encoding='utf-8').splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('event') == 'end' and r.get('run_id') == run_id:
            return True
    return False


def _hanging_threads(conn, boot):
    out = []
    for key, value in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'reply-wait:%'").fetchall():
        try:
            m = json.loads(value)
        except ValueError:
            continue
        start = _iso_to_dt(m.get('start'))
        if not start or m.get('end'):
            continue
        if start.timestamp() <= boot <= start.timestamp() + float(m.get('timeout') or 0):
            out.append(key.split(':', 1)[1])
    return out


def _night_runs(boot):
    from . import watchdog
    out = []
    for f in sorted((Path(watchdog.STATE) / 'runs').glob('NQ-*.json')) if (Path(watchdog.STATE) / 'runs').exists() else []:
        try:
            rec = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        t = _iso_to_dt(rec.get('started_at'))
        if t and t.timestamp() < boot:
            out.append({'id': rec.get('item') or f.stem, 'started_at': rec.get('started_at')})
    return out


def start_recovery(conn, vault, boot, log_change, now=None):
    """The Mac rebooted: remember what was running at `boot` and write agent/管理/重启恢复.md."""
    now = now or datetime.now(timezone.utc)
    state = {'boot': boot, 'at': now.isoformat(timespec='seconds'), 'manual_done': False, 'wait': None,
             'runs': _unfinished_runs(boot), 'threads': _hanging_threads(conn, boot), 'night': _night_runs(boot)}
    db.meta_set(conn, RESTART_KEY, state)
    log_change(vault, '自检', RECOVERY, 'Mac 在 %s 重启过，生成重启恢复清单' % datetime.fromtimestamp(boot, db.LOCAL_TZ).strftime('%m-%d %H:%M'))
    sync(conn, vault, lambda p: False, log_change, now)
    return state


def _open_items(conn, state):
    """What is still to check: ([unfinished runs], [threads not hanging again], manual_done)."""
    boot = state['boot']
    runs = [r for r in state['runs'] if not _ledger_ended(r['run_id'])]
    threads = []
    for t in state['threads']:
        m = db.meta_get(conn, 'reply-wait:' + t)
        start = _iso_to_dt(m.get('start')) if m else None
        if not (start and start.timestamp() > boot):
            threads.append(t)
    return runs, threads, bool(state.get('manual_done'))


def pending_count(conn):
    state = db.meta_get(conn, RESTART_KEY)
    if not state:
        return 0
    runs, threads, manual = _open_items(conn, state)
    return len(runs) + len(threads) + (0 if manual else 1)


def home_line(conn):
    n = pending_count(conn)
    return ['- ⚠ Mac 刚重启 · %d 件要查 · [[agent/管理/重启恢复|去看]]' % n] if n else []


def _page(conn, state):
    runs, threads, manual = _open_items(conn, state)
    boot = datetime.fromtimestamp(state['boot'], db.LOCAL_TZ)
    n = len(runs) + len(threads) + (0 if manual else 1)
    out = ['---', 'cssclasses: [wide]', '---', '', '# 重启恢复', '',
           '> [!warning] Mac 在 %s 重启过 · 还有 %d 件要查' % (boot.strftime('%m-%d %H:%M %Z'), n), '',
           '本页由脚本写，不手改；每件查完会自动划掉，只有最后一项要你自己勾。', '',
           '## 1. 开机前没结束的付费运行', '']
    if state['runs']:
        out.append('先查开机前启动的机器还在不在，再决定续还是毁；不要直接重跑。重跑同一代号要加 `--after-restart-checked`。')
        out.append('')
        open_ids = {r['run_id'] for r in runs}
        for r in state['runs']:
            t = _iso_to_dt(r['time'])
            when = t.astimezone(db.LOCAL_TZ).strftime('%m-%d %H:%M') if t else str(r['time'])
            line = '`%s` · 开始 %s · %s' % (r['code'], when, r['run_id'][:8])
            out.append('- %s' % line if r['run_id'] in open_ids else '- ~~%s~~ 已有结束记录' % line)
    else:
        out.append('无。')
    out += ['', '## 2. 开机前在跑的夜间项', '']
    out += ['- `%s` · 开始 %s（夜间巡检会处理，不计入上面的件数）' % (r['id'], r['started_at']) for r in state['night']] or ['无。']
    out += ['', '## 3. 开机前挂着 reply-wait 的线程', '']
    if state['threads']:
        out += ['要在 Claude 里重开这几条线程的 RC 会话。', '']
        for t in state['threads']:
            name = db.thread_name(conn, t)
            out.append('- %s' % (name if t in threads else '~~%s~~ 已重新挂上' % name))
    else:
        out.append('无。')
    out += ['', '## 4. 手动步骤', '', '- [%s] 重启后的手动步骤做完了' % ('x' if manual else ' '), '']
    return '\n'.join(out)


TICK_RE = re.compile(r'^[ \t]*- \[[xX]\][ \t]*重启后的手动步骤做完了', re.M)


def sync(conn, vault, busy, log_change, now=None):
    """Every render round: read the owner's tick of the manual-step box, refresh the page, open or close the one wait for the threads."""
    from . import render
    state = db.meta_get(conn, RESTART_KEY)
    if not state:
        return None
    page = Path(vault) / RECOVERY
    if page.exists() and busy(page):
        return 'busy'
    if page.exists() and not state.get('manual_done'):
        try:
            if TICK_RE.search(page.read_text(encoding='utf-8')):
                state['manual_done'] = True
        except (OSError, UnicodeDecodeError):
            pass
    _runs, threads, _manual = _open_items(conn, state)
    if threads and not state.get('wait'):
        from . import autowait
        state['wait'] = autowait._new_wait(conn, thread='', ask='重启后在 Claude 里重开 %d 条线程的 RC 会话（见 agent/管理/重启恢复）' % len(threads),
                                           editor='r2i-sync 脚本', report=None, typed=None)
        log_change(vault, '自检', RECOVERY, '登记 %s：重启后要重开 %d 条线程的 RC 会话' % (state['wait'], len(threads)))
    elif not threads and state.get('wait'):
        conn.execute("UPDATE waits SET status='已回', closed_at=?, closed_by='线程已重新挂上' WHERE id=? AND status='等待'",
                     (db.now(), state['wait']))
        conn.commit()
        state['wait'] = None
    db.meta_set(conn, RESTART_KEY, state)
    text = _page(conn, state)
    if not page.exists() or page.read_text(encoding='utf-8') != text:
        render._write(page, text)
    return 'ok'
