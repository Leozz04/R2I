"""Project configuration: `r2i.toml` in the project root.

Found via env R2I_PROJECT_DIR, else the nearest `r2i.toml` walking up from the cwd. There are no built-in defaults
for any project value: a missing required key is an error that names the key. Optional: [keys], departments, maintainer.
"""
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os
import re
import tomllib

FILE = 'r2i.toml'
REQUIRED = ('project', 'owner', 'tz', 'quiet_hours', 'vault', 'ledger', 'runs_root', 'currency', 'approve_over',
            'night_budget', 'max_experiment_threads', 'mainlines')
KEY_NAMES = ('batch_api', 'deepseek', 'relay', 'relay_think', 'ntfy_topic', 'ntfy_wake_topic',
            'wake_token')  # [keys] values are Keychain item names
# [relay]: a Claude-compatible relay endpoint (paid_run fills [keys] relay, or relay_think when R2I_RELAY_KEY=think).
RELAY_FIELDS = ('base_url',)
# [worker]: the code worker (r2i worker). Unset fields take these defaults; model/budget have none (the worker refuses).
WORKER_DEFAULTS = {'base_url': '', 'model': '', 'effort': 'high', 'budget': '', 'cap': 10.0, 'headroom': 0.75,
                   'usd_rate': 7.3, 'max_turns': 60, 'timeout': 1800, 'out_dir': ''}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Mainline:
    id: str
    name: str
    short: str


@dataclass(frozen=True)
class Config:
    root: Path
    project: str
    owner: str
    tz: str
    quiet_hours: tuple  # (start_hour, end_hour), end exclusive; start > end wraps midnight
    vault: Path
    ledger: Path
    runs_root: Path
    currency: str
    approve_over: float
    night_budget: float
    max_experiment_threads: int
    mainlines: tuple
    keys: dict = field(default_factory=dict)
    departments: tuple = ()
    relay: dict = field(default_factory=dict)
    worker: dict = field(default_factory=lambda: dict(WORKER_DEFAULTS))
    keychain_prefix: str = ''  # every [keys] item name starts with this; '' = '<project>-'
    maintainer: str = ''  # editors whose name starts with this may edit any audit; '' = nobody

    @property
    def zone(self):
        return ZoneInfo(self.tz)

    @property
    def key_prefix(self):
        return self.keychain_prefix or '%s-' % self.project

    def key(self, name):
        """Keychain item name for a feature, or None (the feature is then disabled)."""
        return self.keys.get(name) or None

    def need_key(self, name, feature):
        item = self.key(name)
        if not item:
            raise ConfigError('%s is disabled: set [keys] %s in %s' % (feature, name, FILE))
        return item


def find_root(start=None):
    env = os.environ.get('R2I_PROJECT_DIR')
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / FILE).is_file():
            raise ConfigError('R2I_PROJECT_DIR=%s has no %s' % (root, FILE))
        return root
    here = Path(start or os.getcwd()).resolve()
    for d in (here, *here.parents):
        if (d / FILE).is_file():
            return d
    raise ConfigError('no %s found (set R2I_PROJECT_DIR or run inside a project; see r2i.example.toml)' % FILE)


def _hours(text):
    m = re.fullmatch(r'\s*(\d{1,2})(?::\d{2})?\s*-\s*(\d{1,2})(?::\d{2})?\s*', str(text))
    if not m or not all(0 <= int(g) <= 24 for g in m.groups()):
        raise ConfigError('quiet_hours must look like "00:00-09:00", got %r' % (text,))
    return int(m.group(1)), int(m.group(2))


def _path(root, text):
    p = Path(str(text)).expanduser()
    return p if p.is_absolute() else root / p


def parse(data, root):
    missing = [k for k in REQUIRED if k not in data or data[k] in ('', None, [])]
    if missing:
        raise ConfigError('%s: missing required key(s): %s' % (FILE, ', '.join(missing)))
    try:
        ZoneInfo(str(data['tz']))
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError('%s: unknown time zone %r' % (FILE, data['tz'])) from None
    mls = []
    for m in data['mainlines']:
        if not isinstance(m, dict) or not all(m.get(k) for k in ('id', 'name', 'short')):
            raise ConfigError('%s: every [[mainlines]] entry needs id, name and short' % FILE)
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', m['id']):
            raise ConfigError('%s: mainline id %r must be lowercase letters, digits and -' % (FILE, m['id']))
        mls.append(Mainline(m['id'], m['name'], m['short']))
    if len({m.id for m in mls}) != len(mls):
        raise ConfigError('%s: duplicate mainline id' % FILE)
    keys = {k: str(v) for k, v in (data.get('keys') or {}).items()}
    bad = sorted(set(keys) - set(KEY_NAMES))
    if bad:
        raise ConfigError('%s: unknown [keys] entry: %s (known: %s)' % (FILE, ', '.join(bad), ', '.join(KEY_NAMES)))
    prefix = str(data.get('keychain_prefix') or '%s-' % data['project'])
    not_prefixed = sorted('%s = %r' % (k, v) for k, v in keys.items() if v and not v.startswith(prefix))
    if not_prefixed:
        raise ConfigError('%s: [keys] item names must start with %r (keychain_prefix; default "<project>-"), refused: %s. '
                          'paid-run puts these Keychain items into a child command, so r2i only reads items it owns'
                          % (FILE, prefix, '; '.join(not_prefixed)))
    relay = dict(data.get('relay') or {})
    if set(relay) - set(RELAY_FIELDS):
        raise ConfigError('%s: unknown [relay] entry: %s' % (FILE, ', '.join(sorted(set(relay) - set(RELAY_FIELDS)))))
    worker = dict(data.get('worker') or {})
    if set(worker) - set(WORKER_DEFAULTS):
        raise ConfigError('%s: unknown [worker] entry: %s' % (FILE, ', '.join(sorted(set(worker) - set(WORKER_DEFAULTS)))))
    try:
        worker = {k: type(v)(worker.get(k, v)) for k, v in WORKER_DEFAULTS.items()}
    except (TypeError, ValueError) as exc:
        raise ConfigError('%s: bad [worker] value: %s' % (FILE, exc)) from None
    if not 0 < worker['headroom'] <= 1:
        raise ConfigError('%s: [worker] headroom must be in (0, 1]' % FILE)
    return Config(root=root, project=str(data['project']), owner=str(data['owner']), tz=str(data['tz']),
                  quiet_hours=_hours(data['quiet_hours']), vault=_path(root, data['vault']),
                  ledger=_path(root, data['ledger']), runs_root=_path(root, data['runs_root']),
                  currency=str(data['currency']), approve_over=float(data['approve_over']),
                  night_budget=float(data['night_budget']), max_experiment_threads=int(data['max_experiment_threads']),
                  mainlines=tuple(mls), keys=keys, departments=tuple(data.get('departments') or ()),
                  relay=relay, worker=worker, keychain_prefix=prefix, maintainer=str(data.get('maintainer') or '').strip())


_CACHE = {}


def load(root=None):
    root = Path(root).resolve() if root else find_root()
    try:
        data = tomllib.loads((root / FILE).read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError('%s: %s' % (root / FILE, e)) from None
    return parse(data, root)


def get():
    """The cached config of the current project (R2I_PROJECT_DIR or cwd). `reset()` after changing either."""
    root = find_root()
    if root not in _CACHE:
        _CACHE[root] = load(root)
    return _CACHE[root]


def reset():
    _CACHE.clear()


class LocalTZ(tzinfo):
    """A tzinfo that follows the configured zone, so modules can keep one module-level LOCAL_TZ and never read
    the config at import time."""

    def utcoffset(self, dt):
        return get().zone.utcoffset(dt)

    def dst(self, dt):
        return get().zone.dst(dt)

    def tzname(self, dt):
        return get().zone.tzname(dt)

    def fromutc(self, dt):
        z = get().zone
        return z.fromutc(dt.replace(tzinfo=z)).replace(tzinfo=self)


def owner():
    """The owner's name for user-facing text; 'the owner' when no project is found (e.g. `--help` outside one)."""
    try:
        return get().owner
    except ConfigError:
        return 'the owner'


def say(text):
    """Fill the {owner} placeholder of a user-facing string."""
    return text.replace('{owner}', owner())


def state_dir():
    """Runtime state (queue/, snapshot.json, jobs.json, ...) lives in the project, never next to the code. Outside a
    project (e.g. `--help`) this is just <cwd>/.r2i and nothing is written there."""
    try:
        d = find_root() / '.r2i'
    except ConfigError:
        return Path(os.getcwd()) / '.r2i'
    d.mkdir(exist_ok=True)
    return d


def label(job):
    """launchd label of a job: com.r2i.<project>.<job>."""
    try:
        project = get().project
    except ConfigError:
        project = 'project'
    return 'com.r2i.%s.%s' % (re.sub(r'[^A-Za-z0-9-]+', '-', project).strip('-').lower() or 'project', job)


def slim_root():
    """Where what left the vault goes: <runs_root>/_vault-slim."""
    return get().runs_root / '_vault-slim'


def paid_dir(root=None):
    """Budget file (BUDGETS.json) and spend ledger (ledger.jsonl) of the paid-run gate."""
    return (Path(root) / '.r2i' if root else state_dir()) / 'paid-budget'


def in_window(hour, a, b):
    """True when a local hour falls in [a, b); a > b wraps midnight."""
    return a <= hour < b if a <= b else (hour >= a or hour < b)


def in_quiet(hour):
    """True when a local hour falls in quiet_hours."""
    return in_window(hour, *get().quiet_hours)
