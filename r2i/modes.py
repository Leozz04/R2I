"""(R-52) every automation has a mode in meta: off / observe / auto.

  off      the automation does not run at all.
  observe  it changes nothing (no page, no wait, no job); the 变更 table gets a line 「本来会做：…」 instead.
  auto     it runs as before.
Automations already running keep auto; a new one starts in observe (DEFAULTS). 首页 shows one footer line
「observe 中：…」 while any automation is observing (footer()). `cli mode-set NAME MODE`, `cli mode-list`."""
import hashlib
from datetime import date, datetime, timedelta

from . import db

VALID = ('off', 'observe', 'auto')
DEFAULTS = {
    'selfcheck-kickstart': 'auto',  # the hourly self-check restarts a dead launchd job
    'autowait': 'auto',             # 问你 / 要你打 on a report page open and close waits
    'archive': 'auto',              # 汇报/ pages move into 归档 and back; the 更新 stamp; attachment sweep
    'reopen': 'auto',               # a finished page whose newest update asks the owner goes back to 待你看
    'kinds': 'observe',             # 汇报/ pages sorted into 要批/ and 同步/ by 类别; R-52: observe until the owner says live
    'rulecheck': 'observe',         # rulecheck.py: mechanical checks of text rules (R-03, R-31, ...)
    'paid-stops': 'observe',        # paid_run refuses a run whose budget/code/purpose is on the stop table
    'handover-gen': 'observe',      # the generated half of vault agent/交接/ pages (preview only while observing)
    'handover-lint': 'observe',     # lint is advisory; auto blocks close-report only when the page is over the token limit
    'jobs-v2': 'observe',
    'unclaimed-push': 'observe',
    'audit-wake': 'observe',        # E2: audit-add wakes the thread that ran the experiment (a reply its reply-wait hands over)
    'review-overdue': 'observe',    # E3: 已运行待复核 for 24 h with no audit -> a line in 首页's folded 「给线程看」    # A2: an answer no thread picked up in 10 minutes -> a line under 首页「要你定」 and one ntfy push           # restarting selfcheck / repo-sync / wake-bridge / unregistered jobs, and the ntfy push of a self-check alert
    'paid-heartbeat-push': 'observe',  # C1: a paid_run heartbeat older than 15 min with no end file -> one push
    'jobs-thread-check': 'observe',    # F1: a jobs.json entry whose target_thread is not an open row of the threads table -> reported
    'vault-backup': 'observe',         # F2: hourly copy of the three handovers and the rule table to a backup folder
    'budget-mismatch': 'auto',         # a verbal budget whose expiry/cap differs from BUDGETS.json -> reported
    'queue-stale': 'auto',             # a queue file older than 10 minutes -> reported (the host stopped applying)
    'milestone-gap': 'auto',           # a mainline without a milestone for 3 days, or a 版本变更 with no 结构改成 vN page
    'reply-wait-unwatched': 'auto',    # a thread with open waits / undelivered answers and no live reply-wait -> reported
    'jobs-idle': 'observe',            # I2: a jobs.json job that produced nothing for a week -> reported
    'rule-format': 'observe',          # I1: rulecheck format checks of the rule table (lines, tags, owners, versions)
    'crit-rollup': 'observe',          # v5a: the newest PASS A/B audit of a criterion rolls its reading (and, for system audits on the criterion set, its status) into criteria
    'crit-required': 'observe',        # v5a: audit-add (result) must carry crit/before/after/subject/on-crit-set and exp-upsert a crit field, or 「无:理由」
    'daily-check': 'observe',          # 早报 v2: after the 07:00 agent writes the note, daily.py check (≤ 60 lines in the body, no AUD-/TN-/lib:/path text); observe logs 「would refuse」, auto exits 1
}
EDITOR = 'r2i-sync 脚本'
# V5-OBSERVE-UNTIL: a switch in observe carries an end date (table mode_state); entering observe stores at most 7 days ahead, a
# switch with no stored date (the defaults, rows from before) counts as the ledger's first day + MAX_OBSERVE_DAYS.
MAX_OBSERVE_DAYS = 7


def check(name, mode=None):
    if name not in DEFAULTS:
        raise db.HSError('unknown automation %r; one of: %s' % (name, ', '.join(DEFAULTS)))
    if mode is not None and mode not in VALID:
        raise db.HSError('mode must be one of: %s' % ', '.join(VALID))


def get(conn, name):
    """The mode of `name`; without a connection (or a stored garbage value) the default."""
    default = DEFAULTS[name]
    if conn is None:
        return default
    try:
        v = db.meta_get(conn, 'mode:' + name)
    except Exception:
        return default
    return v if v in VALID else default


def check_until(until, now=None):
    """The ISO date for `until`: today..today+7 (local days); None = today+7. Raises HSError for anything else."""
    today = (now or datetime.now(db.LOCAL_TZ)).date()
    if until is None:
        return (today + timedelta(days=MAX_OBSERVE_DAYS)).isoformat()
    try:
        d = date.fromisoformat(str(until))
    except ValueError:
        raise db.HSError('--until must be a date YYYY-MM-DD, got %r' % until)
    if d < today or d > today + timedelta(days=MAX_OBSERVE_DAYS):
        raise db.HSError('observe_until must be between %s and %s (observe lasts at most %d days)' % (
            today, today + timedelta(days=MAX_OBSERVE_DAYS), MAX_OBSERVE_DAYS))
    return d.isoformat()


def default_until(conn):
    """A switch with no stored end date observes until the ledger's first day + MAX_OBSERVE_DAYS ('' without a ledger)."""
    first = db.meta_get(conn, 'created') if conn is not None else None
    return (date.fromisoformat(first) + timedelta(days=MAX_OBSERVE_DAYS)).isoformat() if first else ''


def observe_until(conn, name):
    """The stored end date of `name`'s observe period, else the default end (see default_until)."""
    row = None
    if conn is not None:
        try:
            row = conn.execute('SELECT observe_until FROM mode_state WHERE name=?', (name,)).fetchone()
        except Exception:
            pass
    return (row[0] if row and row[0] else default_until(conn))


def set_mode(conn, name, mode, until=None, now=None):
    """Entering observe (or giving --until) stores observe_until; leaving observe clears it."""
    check(name, mode)
    now = now or datetime.now(db.LOCAL_TZ)
    old = get(conn, name)
    if mode != 'observe':
        if until is not None:
            raise db.HSError('--until only goes with mode observe')
        new_until = None
    elif old != 'observe' or until is not None:
        new_until = check_until(until, now)
    else:  # observe -> observe without a date: keep what is stored
        new_until = observe_until(conn, name)
    db.meta_set(conn, 'mode:' + name, mode)
    conn.execute('INSERT INTO mode_state(name,changed_at,observe_until) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET '
                 'changed_at=excluded.changed_at, observe_until=excluded.observe_until', (name, db.now(), new_until))
    conn.commit()
    return {'name': name, 'mode': mode, 'was': old, 'observe_until': new_until}


def listing(conn):
    return {n: get(conn, n) for n in DEFAULTS}


def until_map(conn):
    return {n: observe_until(conn, n) for n in DEFAULTS if get(conn, n) == 'observe'}


def table(conn, modes_=None, untils=None):
    """mode-list: {name: {'mode', 'observe_until'}}; observe_until only for switches in observe. Snapshot values may be passed in."""
    modes_ = modes_ if modes_ is not None else listing(conn)
    untils = untils if untils is not None else until_map(conn)
    return {n: {'mode': m, 'observe_until': (untils.get(n) or default_until(conn)) if m == 'observe' else None} for n, m in modes_.items()}


def observing(conn):
    return [n for n in DEFAULTS if get(conn, n) == 'observe']


def footer(conn):
    names = observing(conn)
    return 'observe 中：' + '、'.join(names) if names else ''


def would(conn, vault, log_change, name, rel, text):
    """Observe mode: one 「本来会做：…」 line per distinct text per local day (a minute-by-minute run must not repeat it)."""
    if log_change is None:
        return False
    if conn is not None:
        day = db.datetime.now(db.LOCAL_TZ).strftime('%Y-%m-%d') if hasattr(db, 'datetime') else ''
        key = 'would:' + hashlib.md5(('%s|%s|%s' % (name, rel, text)).encode('utf-8')).hexdigest()[:16]
        if db.meta_get(conn, key) == day:
            return False
        db.meta_set(conn, key, day)
    log_change(vault, EDITOR, rel, '本来会做：%s（%s 在 observe）' % (text, name))
    return True


def require(conn, log_change, name, rel, problem, label=''):
    """A required-field switch (crit-required): auto refuses with `problem`, observe logs 「would refuse: …」, off ignores it."""
    if not problem:
        return
    mode = get(conn, name)
    if mode == 'auto':
        raise db.HSError(problem)
    if mode == 'observe':
        would(conn, None, log_change, name, rel, 'would refuse: %s%s' % (problem, '（%s）' % label if label else ''))
