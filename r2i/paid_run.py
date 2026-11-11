#!/usr/bin/env python3
"""Run a command under an approved CNY budget and append an audit ledger.
Spend files report `spent_cny` and may include `unknown_calls` plus a positive
`per_call_cap_cny`; `--call-cap` supplies the fallback placeholder amount.
While the child runs, paid_run polls spend and stops its process group at the cap.
Relay: when [relay] base_url (r2i.toml) has its host in the child's ANTHROPIC_BASE_URL, the child gets
ANTHROPIC_AUTH_TOKEN from the Keychain item [keys] relay (relay_think when R2I_RELAY_KEY=think) and no ANTHROPIC_API_KEY.
add_spend(cny, calls, unknown_calls, path): flock-protected, atomic addition to the spend file, for children that
make calls from several threads or processes (path defaults to $PAID_RUN_SPEND_FILE).
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from typing import Any

try:  # also runnable as a script: put the package's parent on the path
    from r2i import config
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from r2i import config




class PaidRunError(Exception):
    """A refusal or local accounting error that must prevent launch."""


def _relay_host() -> str:
    base_url = config.get().relay.get("base_url")
    return (urllib.parse.urlparse(str(base_url)).hostname or "").lower() if base_url else ""


def _is_relay(env: dict[str, str]) -> bool:
    host = _relay_host()
    base = env.get("ANTHROPIC_BASE_URL", "")
    return bool(host) and host in (urllib.parse.urlparse(base).hostname or base).lower()


def _keychain_env(env: dict[str, str]) -> dict[str, str]:
    """env var -> Keychain item (names only, from r2i.toml [keys]). A configured relay gets its own key
    ([keys] relay, or relay_think when R2I_RELAY_KEY=think). DeepSeek through its Anthropic-compatible
    endpoint gets its own key, and never the Anthropic key (it would be sent to DeepSeek). No item set: nothing is filled."""
    if _is_relay(env):
        item = config.get().key("relay_think" if env.get("R2I_RELAY_KEY") == "think" else "relay")
        return {"ANTHROPIC_AUTH_TOKEN": item} if item else {}
    if "deepseek.com" in env.get("ANTHROPIC_BASE_URL", "").lower():
        item = config.get().key("deepseek")
        return {"ANTHROPIC_AUTH_TOKEN": item} if item else {}
    item = config.get().key("batch_api")
    return {"ANTHROPIC_API_KEY": item} if item else {}


def _fill_keys_from_keychain(env: dict[str, str]) -> None:
    """Fill unset API keys from the macOS Keychain into the child env only; never print a value."""
    wanted = _keychain_env(env)
    if _is_relay(env):
        env.pop("ANTHROPIC_API_KEY", None)  # the official key must never reach the relay
    if not wanted:
        print("paid_run: no Keychain item configured for this endpoint ([keys] in r2i.toml); no key filled", file=sys.stderr)
    for var, service in wanted.items():
        if env.get(var):
            continue
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            print(f"paid_run: {var} not set and Keychain lookup unavailable", file=sys.stderr)
            continue
        value = out.stdout.strip()
        if out.returncode == 0 and value:
            env[var] = value
            print(f"paid_run: {var} filled from Keychain item {service}", file=sys.stderr)
        else:
            print(f"paid_run: {var} not set and Keychain item {service} not readable", file=sys.stderr)


def add_spend(cny: float, calls: int = 1, unknown_calls: int = 0, path: str | os.PathLike | None = None) -> None:
    """Add to the run's spend file under an exclusive lock on `<file>.lock`; safe across threads and processes."""
    for name, value in (("cny", cny), ("calls", calls), ("unknown_calls", unknown_calls)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite number >= 0")
    target = Path(path if path is not None else os.environ["PAID_RUN_SPEND_FILE"])
    with open(target.with_name(target.name + ".lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                document = json.loads(target.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                document = {}
            if not isinstance(document, dict):
                document = {}
            document["spent_cny"] = round(float(document.get("spent_cny") or 0) + cny, 4)
            document["calls"] = int(document.get("calls") or 0) + int(calls)
            document["unknown_calls"] = int(document.get("unknown_calls") or 0) + int(unknown_calls)
            tmp = target.with_name("." + target.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(document), encoding="utf-8")
            os.replace(tmp, target)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _positive_finite_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number greater than 0") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return value


def _nonempty(raw: str) -> str:
    if not raw.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return raw


def _parse_cli(argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw:
        raise PaidRunError("missing '-- <command> [args...]' separator and command")
    sep = raw.index("--")
    option_args, command = raw[:sep], raw[sep + 1 :]
    parser = argparse.ArgumentParser(
        description="Run one command under an approved paid-experiment budget."
    )
    parser.add_argument("--budget", required=True, help="approved budget id")
    parser.add_argument("--cap", type=_positive_finite_float,
                        help="run cap in CNY")
    parser.add_argument("--purpose", required=True, type=_nonempty)
    parser.add_argument("--conclusion", required=True, type=_nonempty)
    parser.add_argument("--group", default=None)
    parser.add_argument("--call-cap", type=_positive_finite_float,
                        help="per-call CNY placeholder for calls with unknown usage")
    parser.add_argument("--receipts", action="append", default=[],
                        help="receipts.jsonl of this run's API calls (repeatable); without it paid_run looks in the "
                             "command's directory arguments")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--after-restart-checked", action="store_true",
                        help="the machine restarted while an earlier run of this code was unfinished, and you checked "
                             "that nothing it started is still running and billing")
    options = parser.parse_args(option_args)
    if not command:
        raise PaidRunError("missing command after '--'")
    if options.cap is None:
        raise PaidRunError("--cap is required")
    return options, command


def _project_root() -> Path:
    override = os.environ.get("PAID_RUN_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_iso(value: dt.datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _as_cny(value: Any, field: str, *, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaidRunError(f"invalid {field}: expected a number")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (not allow_zero and number == 0):
        qualifier = "non-negative" if allow_zero else "greater than 0"
        raise PaidRunError(f"invalid {field}: expected a finite {qualifier} amount")
    return number


def _load_budget(budgets_path: Path, budget_id: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = budgets_path.read_bytes()
    except FileNotFoundError as exc:
        raise PaidRunError(f"budget file is missing: {budgets_path}") from exc
    except OSError as exc:
        raise PaidRunError(f"cannot read budget file {budgets_path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaidRunError(f"budget file is not valid JSON: {budgets_path}") from exc
    budgets = document.get("budgets") if isinstance(document, dict) else None
    if not isinstance(budgets, dict):
        raise PaidRunError("budget file must contain a 'budgets' object")
    if budget_id not in budgets:
        raise PaidRunError(f"budget id not found: {budget_id}")
    budget = budgets[budget_id]
    if not isinstance(budget, dict):
        raise PaidRunError(f"budget entry is invalid: {budget_id}")
    return budget, raw


def _check_expiry(budget: dict[str, Any], budget_id: str) -> None:
    expires_at = budget.get("expires_at")
    if expires_at is None:
        return
    if not isinstance(expires_at, str) or not expires_at.strip():
        raise PaidRunError(f"budget expiry is invalid: {budget_id}")
    try:
        expiry = dt.datetime.fromisoformat(expires_at.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaidRunError(f"budget expiry is not a valid ISO timestamp: {budget_id}") from exc
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=dt.timezone.utc)
    if _utc_now() >= expiry.astimezone(dt.timezone.utc):
        raise PaidRunError(f"budget has expired: {budget_id} (expires_at={expires_at})")


def _record_sha256(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _settleable_end(end: dict[str, Any]) -> bool:
    unknown_calls = end.get("unknown_calls")
    return (end.get("spend_known") is False
            or (isinstance(unknown_calls, int) and not isinstance(unknown_calls, bool)
                and unknown_calls > 0))


def _read_ledger(ledger_path: Path, *, closed_budgets: dict | None = None) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    closed: dict[str, dict[str, Any]] = {}
    try:
        ledger = ledger_path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return runs
    except OSError as exc:
        raise PaidRunError(f"cannot read ledger {ledger_path}: {exc}") from exc
    with ledger:
        for line_number, line in enumerate(ledger, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PaidRunError(f"ledger line {line_number} is not valid JSON") from exc
            if not isinstance(record, dict):
                raise PaidRunError(f"ledger line {line_number} must be a JSON object")
            event = record.get("event")
            if event == "budget_closed":
                budget_id = record.get("budget")
                if not isinstance(budget_id, str) or not budget_id or budget_id in closed:
                    raise PaidRunError(f"ledger line {line_number} has an invalid or duplicate budget closure")
                run = runs.get(record.get("run_id"))
                if (not run or run["budget"] != budget_id or run["end"] is None
                        or not record.get("reason") or not record.get("proof_sha256")):
                    raise PaidRunError(f"ledger line {line_number} has an unbound budget closure")
                closed[budget_id] = record
                continue
            run_id = record.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise PaidRunError(f"ledger line {line_number} has no valid run_id")
            if event == "start":
                if run_id in runs:
                    raise PaidRunError(f"ledger has duplicate start records for run {run_id}")
                if record.get("budget") in closed:
                    raise PaidRunError(f"ledger starts run {run_id} after its budget was closed")
                try:
                    cap = _as_cny(record.get("cap"), f"ledger cap on line {line_number}", allow_zero=False)
                except PaidRunError:
                    raise
                runs[run_id] = {"budget": record.get("budget"), "cap": cap, "end": None,
                                "settlement": None, "group": record.get("group"), "time": record.get("time")}
            elif event == "end":
                if run_id not in runs or runs[run_id]["end"] is not None:
                    raise PaidRunError(f"ledger has an unmatched or duplicate end record for run {run_id}")
                runs[run_id]["end"] = record
            elif event == "settlement":
                run = runs.get(run_id)
                if (not run or run["end"] is None or run["settlement"] is not None
                        or not (_settleable_end(run["end"]) or record.get("correction") is True)
                        or record.get("budget") != run["budget"]
                        or record.get("original_end_sha256") != _record_sha256(run["end"])
                        or not record.get("proof_sha256") or not record.get("reviewed_by")
                        or not record.get("reason")):
                    raise PaidRunError(f"ledger has an invalid or duplicate settlement for run {run_id}")
                _as_cny(record.get("spent_cny"), "settled spend")
                if record.get("spend_known") is not True:
                    raise PaidRunError(f"settlement must state known spend for run {run_id}")
                run["settlement"] = record
            else:
                raise PaidRunError(f"ledger line {line_number} has an unknown event")
    if closed_budgets is not None:
        closed_budgets.update(closed)
    return runs


def _committed_spend(runs: dict[str, dict[str, Any]], budget_id: str) -> Decimal:
    committed = Decimal("0")
    for run in runs.values():
        if run["budget"] != budget_id:
            continue
        end = run.get("settlement") or run["end"]
        if (
            isinstance(end, dict)
            and end.get("spend_known") is True
            and end.get("spent_cny") is not None
        ):
            try:
                reported = _as_cny(end["spent_cny"], "reported spend")
                committed += Decimal(str(reported))
                continue
            except PaidRunError:
                pass
        committed += Decimal(str(run["cap"]))
    return committed


def _budget_family(budget_bytes: bytes, budget_id: str, closed: dict) -> list[str]:
    """A replacement shares its closed predecessors' cap and actual/reserved spending."""
    budgets = json.loads(budget_bytes)["budgets"]
    family: list[str] = []
    current = budget_id
    while True:
        if current in family or current not in budgets:
            raise PaidRunError("invalid or cyclic supersedes_budget chain")
        family.append(current)
        budget = budgets[current]
        if not isinstance(budget, dict):
            raise PaidRunError("invalid supersedes_budget entry")
        parent = budget.get("supersedes_budget")
        if parent is None:
            return family
        if not isinstance(parent, str) or parent not in closed or parent not in budgets:
            raise PaidRunError("supersedes_budget must name an existing closed budget")
        if not isinstance(budgets[parent], dict):
            raise PaidRunError("invalid predecessor budget entry")
        children = [key for key, value in budgets.items()
                    if isinstance(value, dict) and value.get("supersedes_budget") == parent]
        if len(children) != 1:
            raise PaidRunError("a closed budget must not fund multiple replacement budgets")
        child_total = _as_cny(budget.get("total_cny"), "replacement total_cny")
        parent_total = _as_cny(budgets[parent].get("total_cny"), "predecessor total_cny")
        if child_total > parent_total:
            raise PaidRunError("replacement must not increase the original task budget")
        current = parent


def _append_record(ledger_path: Path, record: dict[str, Any]) -> None:
    try:
        with ledger_path.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())
    except OSError as exc:
        raise PaidRunError(f"cannot append ledger {ledger_path}: {exc}") from exc


def _accounting_lock(lock_path: Path):
    """Open and lock the shared accounting lock file for one critical section."""
    try:
        lock_file = lock_path.open("a+")
    except OSError as exc:
        raise PaidRunError(f"cannot open accounting lock {lock_path}: {exc}") from exc
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        lock_file.close()
        raise PaidRunError(f"cannot lock accounting file {lock_path}: {exc}") from exc
    return lock_file


def _boot_epoch() -> float | None:
    """When this machine booted (epoch seconds). PAID_RUN_BOOT_TIME overrides it (tests); None when unknown."""
    override = os.environ.get("PAID_RUN_BOOT_TIME")
    if override:
        try:
            return float(override)
        except ValueError:
            return None
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
        import re
        m = re.search(r"sec\s*=\s*(\d+)", out)
        if m:
            return float(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return None


def _run_code(budget: Any, group: Any) -> str:
    """What counts as the same code: the --group, else the budget id."""
    return str(group) if group else f"budget:{budget}"


def _check_restart(options: argparse.Namespace, runs: dict[str, dict[str, Any]]) -> None:
    """refuse a new run of a code whose earlier run had not finished when the Mac last rebooted: the
    machine it started may still be alive and billing. Checked by hand, then --after-restart-checked."""
    if getattr(options, "after_restart_checked", False):
        return
    boot = _boot_epoch()
    if boot is None:
        return
    code = _run_code(options.budget, options.group)
    for run_id, run in runs.items():
        if run["end"] is not None or _run_code(run["budget"], run.get("group")) != code:
            continue
        try:
            started = dt.datetime.fromisoformat(str(run.get("time")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if started < boot:
            raise PaidRunError(
                f"run {run_id} of {code} started {run.get('time')} and never finished before the Mac rebooted: "
                "check for a machine still running (do not just rerun), then add "
                "--after-restart-checked"
            )


def _check_stops(options: argparse.Namespace, root: Path) -> None:
    """The stop table (the ledger stops table, cli stop-add). A run whose budget id, or a word of its
    purpose/conclusion, matches an active stop is refused when mode paid-stops is auto; in observe (the default for a
    week, R-52) it only prints what it would have refused. Any problem reading the table: the run goes on."""
    import re
    import sqlite3
    path = Path(os.environ.get("PAID_RUN_STOP_DB") or config.get().ledger)
    if not path.exists():
        return
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stops'").fetchone():
                return
            stops = conn.execute("SELECT id, target, reason FROM stops WHERE lifted_at IS NULL").fetchall()
            row = conn.execute("SELECT value FROM meta WHERE key='mode:paid-stops'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return
    try:
        mode = json.loads(row[0]) if row else "observe"
    except ValueError:
        mode = row[0]
    keys = [options.budget] + re.findall(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+", "%s %s" % (options.purpose, options.conclusion))
    keys = [k.lower() for k in keys]
    hits = []
    for sid, target, reason in stops:
        t = target.lower()
        if any(k == t or (t.endswith("*") and k.startswith(t[:-1])) for k in keys):
            hits.append("%s（%s：%s）" % (sid, target, reason))
    if not hits:
        return
    if mode == "auto":
        raise PaidRunError("refusing run: the owner stopped it: " + "; ".join(hits))
    if mode != "off":
        print("paid_run: on the stop table (observe, would refuse): " + "; ".join(hits), file=sys.stderr)


def _prepare_start(
    options: argparse.Namespace,
    command: list[str],
    root: Path,
) -> tuple[dict[str, Any], Path, float, Decimal, Decimal]:
    budget_dir = config.paid_dir()
    budgets_path = budget_dir / "BUDGETS.json"
    ledger_path = budget_dir / "ledger.jsonl"
    lock_path = budget_dir / "ledger.jsonl.lock"

    if not budgets_path.exists():
        _load_budget(budgets_path, options.budget)
    # A dry run must remain read-only, including when the lock file does not yet exist.
    lock_file = None if options.dry_run else _accounting_lock(lock_path)
    try:
        budget, budget_bytes = _load_budget(budgets_path, options.budget)
        _check_expiry(budget, options.budget)
        _check_stops(options, root)
        total = _as_cny(budget.get("total_cny"), f"total_cny for budget {options.budget}")
        closed: dict[str, dict[str, Any]] = {}
        runs = _read_ledger(ledger_path, closed_budgets=closed)
        if options.budget in closed:
            raise PaidRunError(f"budget is closed: {options.budget}")
        _check_restart(options, runs)
        family = _budget_family(budget_bytes, options.budget, closed)
        committed = sum((_committed_spend(runs, key) for key in family), Decimal("0"))
        remaining = Decimal(str(total)) - committed
        print(
            f"Budget {options.budget}: total={total:.6f} CNY, "
            f"committed={committed:.6f} CNY, remaining={remaining:.6f} CNY"
        )
        if Decimal(str(options.cap)) > remaining:
            raise PaidRunError(
                f"refusing run: cap {options.cap:.6f} CNY exceeds remaining budget "
                f"{remaining:.6f} CNY"
            )

        run_id = str(uuid.uuid4())
        spend_file = budget_dir / "spend" / f"{run_id}.json"
        start_record = {
            "event": "start",
            "run_id": run_id,
            "time": _utc_iso(),
            "budget": options.budget,
            "cap": options.cap,
            "purpose": options.purpose,
            "conclusion": options.conclusion,
            "group": options.group,
            "argv": command,
            "cwd": os.getcwd(),
            "budgets_sha256": hashlib.sha256(budget_bytes).hexdigest(),
        }
        if options.call_cap is not None:
            start_record["call_cap"] = options.call_cap
        if not options.dry_run:
            try:
                spend_file.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PaidRunError(f"cannot create spend directory {spend_file.parent}: {exc}") from exc
            _append_record(ledger_path, start_record)
        return start_record, spend_file, total, committed, remaining
    finally:
        if lock_file is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def _read_effective_spend(
    spend_file: Path,
    default_call_cap: float | None,
) -> tuple[float | None, dict[str, Any]]:
    def reject_nonfinite_json_constant(value: str):
        raise ValueError(f"invalid JSON number: {value}")

    try:
        document = json.loads(
            spend_file.read_text(encoding="utf-8"), parse_constant=reject_nonfinite_json_constant
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return None, {}
    if not isinstance(document, dict):
        return None, {}

    has_unknown_calls = "unknown_calls" in document
    unknown_calls_raw = document.get("unknown_calls")
    reported_spend = None
    try:
        if "spent_cny" in document:
            reported_spend = _as_cny(document["spent_cny"], "spent_cny")
    except (PaidRunError, OverflowError, ValueError):
        pass

    has_file_call_cap = "per_call_cap_cny" in document
    file_call_cap = None
    if has_file_call_cap:
        try:
            file_call_cap = _as_cny(
                document["per_call_cap_cny"], "per_call_cap_cny", allow_zero=False
            )
        except (PaidRunError, OverflowError, ValueError):
            # An invalid explicit value must not silently fall back to a smaller cap.
            if has_unknown_calls:
                return None, {
                    "reported_spent_cny": reported_spend,
                    "unknown_calls": unknown_calls_raw,
                    "per_call_cap_cny": document["per_call_cap_cny"],
                }
            return None, {}
    selected_call_cap = file_call_cap if has_file_call_cap else default_call_cap

    if not has_unknown_calls:
        return reported_spend, {}

    details = {
        "reported_spent_cny": reported_spend,
        "unknown_calls": unknown_calls_raw,
        "per_call_cap_cny": selected_call_cap,
    }
    if isinstance(unknown_calls_raw, bool) or not isinstance(unknown_calls_raw, int) or unknown_calls_raw < 0:
        return None, details
    if reported_spend is None:
        return None, details
    if unknown_calls_raw and selected_call_cap is None:
        return None, details
    try:
        effective_spend = reported_spend + unknown_calls_raw * (selected_call_cap or 0.0)
    except (OverflowError, ValueError):
        return None, details
    if not math.isfinite(effective_spend):
        return None, details
    return effective_spend, details


NO_SUCCESS_EXIT = 4
RECEIPTS_NAME = "receipts.jsonl"
RECEIPT_SEARCH_DEPTH = 4
RECEIPT_SEARCH_DIRS = 5000  # shortcut: a huge output tree is only partly searched; pass --receipts for those


def _parse_utc(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _find_receipts(command: list[str], root: Path) -> list[Path]:
    """receipts.jsonl under the command's directory arguments (not the project root itself), at most 4 levels down."""
    skip = {root.resolve(), Path.home().resolve(), Path("/")}
    found: list[Path] = []
    visited = 0
    for token in command:
        for part in (token, token.split("=", 1)[-1]):
            path = Path(part)
            if not path.is_absolute() or not path.is_dir() or path.resolve() in skip:
                continue
            base = len(path.parts)
            for directory, subdirs, files in os.walk(path):
                visited += 1
                if len(Path(directory).parts) - base >= RECEIPT_SEARCH_DEPTH or visited > RECEIPT_SEARCH_DIRS:
                    subdirs[:] = []
                if RECEIPTS_NAME in files:
                    found.append(Path(directory) / RECEIPTS_NAME)
    return list(dict.fromkeys(p.resolve() for p in found))


def _call_status(paths: list[Path], started: dt.datetime) -> dict[str, Any] | None:
    """C2: this run's call receipts -> counts and the upper bounds still booked for failed calls.
    None when there are no receipt files. Lines completed before the run started belong to earlier runs."""
    calls = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            done = _parse_utc(rec.get("completed_at")) if isinstance(rec, dict) else None
            if isinstance(rec, dict) and (done is None or done >= started.replace(microsecond=0)):
                calls.append(rec)
    if not paths:
        return None
    ok = [c for c in calls if isinstance(c.get("http_status"), int) and 200 <= c["http_status"] < 300]
    failed = [c for c in calls if isinstance(c.get("http_status"), int) and not 200 <= c["http_status"] < 300]
    # A provider refused the call (401, 429, 5xx...): nothing is owed. Its upper bound stays booked only when the
    # client had no usage to price it (cost_cny null); that reservation is released here.
    refund = sum(float(c.get("upper_cny") or 0) for c in failed if c.get("cost_cny") is None)
    return {"receipts": [str(p) for p in paths], "calls": len(calls), "http_2xx": len(ok),
            "http_failed": len(failed), "no_status": len(calls) - len(ok) - len(failed),
            "released_upper_cny": round(refund, 8)}


def _env_positive_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _signal_process_group(process_group_id: int, signum: int) -> None:
    try:
        os.killpg(process_group_id, signum)
    except ProcessLookupError:
        pass
    except OSError:
        # The group may have exited between checking it and delivering the signal.
        pass


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _stop_process_group(
    process: subprocess.Popen,
    signum: int,
    grace_seconds: float,
) -> None:
    process_group_id = process.pid
    _signal_process_group(process_group_id, signum)
    deadline = time.monotonic() + grace_seconds
    while _process_group_exists(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        process.poll()  # Reap the child leader while descendants finish.
        time.sleep(min(0.05, remaining))
    if _process_group_exists(process_group_id):
        _signal_process_group(process_group_id, signal.SIGKILL)
    process.wait()


def _run_child(
    command: list[str],
    cwd: str,
    env: dict[str, str],
    spend_file: Path,
    cap: float,
    call_cap: float | None,
    process_holder: list[subprocess.Popen | None],
    received_signal: list[int | None],
) -> tuple[int, bool, float | None]:
    poll_seconds = _env_positive_seconds("PAID_RUN_POLL_SECONDS", 2.0)
    grace_seconds = _env_positive_seconds("PAID_RUN_KILL_GRACE_SECONDS", 15.0)
    process: subprocess.Popen | None = None
    stopped_by_cap = False
    cap_stop_spend = None
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
    except OSError as exc:
        print(f"paid_run: could not launch command: {exc}", file=sys.stderr)
        return 127, stopped_by_cap, cap_stop_spend
    process_holder[0] = process

    while process.poll() is None:
        if received_signal[0] is not None:
            _stop_process_group(process, received_signal[0], grace_seconds)
            break
        effective_spend, _details = _read_effective_spend(spend_file, call_cap)
        if received_signal[0] is not None:
            _stop_process_group(process, received_signal[0], grace_seconds)
            break
        if effective_spend is not None and effective_spend >= cap and process.poll() is None:
            stopped_by_cap = True
            cap_stop_spend = effective_spend
            _stop_process_group(process, signal.SIGTERM, grace_seconds)
            break
        try:
            process.wait(timeout=poll_seconds)
        except subprocess.TimeoutExpired:
            pass

    exit_code = process.wait()
    if received_signal[0] is not None:
        # The parent signal takes precedence even if the child handles it and exits 0.
        exit_code = 128 + received_signal[0]
    elif stopped_by_cap and exit_code == 0:
        exit_code = 124
    return exit_code, stopped_by_cap, cap_stop_spend


HEARTBEAT_DIR = "heartbeat"  # under the paid-run state directory: <run_id>.beat while the command runs, <run_id>.end when paid_run is done


def _write_marker(path: Path, run_id: str) -> None:
    """Best-effort marker file (its mtime is the signal); a full disk must never stop a paid run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("." + path.name + ".tmp")
        tmp.write_text(json.dumps({"run_id": run_id, "time": _utc_iso()}) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


class _Heartbeat:
    """C1: touch <run_id>.beat every 5 minutes (PAID_RUN_HEARTBEAT_SECONDS) while the command runs; stop() writes <run_id>.end.
    selfcheck reads a beat older than 15 minutes with no end file as a dead paid_run."""

    def __init__(self, directory: Path, run_id: str):
        self.beat, self.end = directory / f"{run_id}.beat", directory / f"{run_id}.end"
        self.run_id = run_id
        self.every = _env_positive_seconds("PAID_RUN_HEARTBEAT_SECONDS", 300.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while True:
            _write_marker(self.beat, self.run_id)
            if self._stop.wait(self.every):
                return

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        _write_marker(self.end, self.run_id)


def _install_signal_forwarding(
    process_holder: list[subprocess.Popen | None],
    received_signal: list[int | None],
) -> dict[int, Any]:
    def forward_signal(signum, _frame):
        received_signal[0] = signum
        process = process_holder[0]
        if process is not None:
            _signal_process_group(process.pid, signum)

    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward_signal)
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    for signum, previous in previous_handlers.items():
        signal.signal(signum, previous)


def _append_end(ledger_path: Path, record: dict[str, Any]) -> None:
    lock_file = _accounting_lock(ledger_path.with_name(ledger_path.name + ".lock"))
    try:
        _append_record(ledger_path, record)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


CLOSE_UNENDED_MIN_AGE_HOURS = 24  # a younger run may still be alive


def _close_unended(options: argparse.Namespace, root: Path, directory: Path, ledger_path: Path) -> int:
    """PAID-CLOSE: a run with a start and no end (paid_run killed before it wrote one) gets a late `end`, appended, never
    rewritten. spent_cny in the proof -> known spend; without it spend_known=false, so the run keeps reserving its cap."""
    if not options.proof or options.budget or options.close_budget:
        raise PaidRunError("--close-unended requires only --proof (and --apply)")
    proof_path = (root / options.proof).resolve()
    try:
        relative = proof_path.relative_to(root)
    except ValueError as exc:
        raise PaidRunError("close proof must stay inside the project") from exc
    try:
        proof_bytes = proof_path.read_bytes()
        proof = json.loads(proof_bytes)
    except (OSError, ValueError) as exc:
        raise PaidRunError("close proof is unreadable or invalid JSON") from exc
    if (not isinstance(proof, dict) or proof.get("run_id") != options.close_unended
            or not all(isinstance(proof.get(key), str) and proof[key].strip() for key in ("budget", "reviewed_by", "reason"))):
        raise PaidRunError("proof must bind run_id, budget, reviewed_by and reason")
    spent = _as_cny(proof["spent_cny"], "closed spent_cny") if proof.get("spent_cny") is not None else None
    lock_file = _accounting_lock(directory / "ledger.jsonl.lock") if options.apply else None
    try:
        run = _read_ledger(ledger_path).get(options.close_unended)
        if not run or run["end"] is not None or proof["budget"] != run["budget"]:
            raise PaidRunError("--close-unended needs a started run of this budget with no end record")
        started = dt.datetime.fromisoformat(str(run["time"]).replace("Z", "+00:00"))
        if dt.datetime.now(dt.timezone.utc) - started < dt.timedelta(hours=CLOSE_UNENDED_MIN_AGE_HOURS):
            raise PaidRunError(f"run started less than {CLOSE_UNENDED_MIN_AGE_HOURS} h ago; it may still be running")
        record = {"event": "end", "run_id": options.close_unended, "time": _utc_iso(), "exit_code": None,
                  "spent_cny": spent, "spend_known": spent is not None, "stopped_by_cap": False, "closed_late": True,
                  "proof": str(relative), "proof_sha256": hashlib.sha256(proof_bytes).hexdigest(),
                  "reviewed_by": proof["reviewed_by"], "reason": proof["reason"]}
        if options.apply:
            _append_record(ledger_path, record)
        print(json.dumps({"run_id": options.close_unended, "budget": run["budget"], "spent_cny": spent,
                          "spend_known": spent is not None, "apply": options.apply}, ensure_ascii=False))
        return 0
    finally:
        if lock_file is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def _accounting_command(argv: list[str], root: Path) -> int:
    parser = argparse.ArgumentParser(description="Record reviewed spending without rewriting run history.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--settle", metavar="RUN_ID")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--close-unended", metavar="RUN_ID",
                      help="append a late end for a run that has a start and no end (proof: run_id, budget, reviewed_by, reason[, spent_cny])")
    parser.add_argument("--budget")
    parser.add_argument("--proof", help="reviewed settlement JSON, inside the project")
    parser.add_argument("--close-budget", action="store_true")
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args(argv)
    directory = config.paid_dir()
    ledger_path = directory / "ledger.jsonl"
    if options.status:
        if not options.budget or options.apply or options.proof or options.close_budget:
            raise PaidRunError("--status requires only --budget")
        budget, raw = _load_budget(directory / "BUDGETS.json", options.budget)
        closed: dict[str, dict[str, Any]] = {}
        runs = _read_ledger(ledger_path, closed_budgets=closed)
        family = _budget_family(raw, options.budget, closed)
        committed = sum((_committed_spend(runs, key) for key in family), Decimal("0"))
        total = _as_cny(budget.get("total_cny"), "total_cny")
        print(json.dumps({"budget": options.budget, "closed": options.budget in closed,
                          "family": family, "total_cny": total,
                          "committed_cny": str(committed),
                          "remaining_cny": str(Decimal(str(total)) - committed)}))
        return 0
    if options.close_unended:
        return _close_unended(options, root, directory, ledger_path)
    if not options.proof or options.budget:
        raise PaidRunError("--settle requires --proof; budget is bound by the original run")
    proof_path = (root / options.proof).resolve()
    try:
        relative = proof_path.relative_to(root)
    except ValueError as exc:
        raise PaidRunError("settlement proof must stay inside the project") from exc
    try:
        proof_bytes = proof_path.read_bytes()
        proof = json.loads(proof_bytes)
    except (OSError, ValueError) as exc:
        raise PaidRunError("settlement proof is unreadable or invalid JSON") from exc
    if (not isinstance(proof, dict) or proof.get("run_id") != options.settle
            or not all(isinstance(proof.get(key), str) and proof[key].strip()
                       for key in ("budget", "original_end_sha256", "reviewed_by", "reason"))):
        raise PaidRunError("proof must bind run_id, budget, original_end_sha256, reviewed_by and reason")
    spent = _as_cny(proof.get("spent_cny"), "settled spent_cny")
    proof_sha = hashlib.sha256(proof_bytes).hexdigest()
    lock_file = _accounting_lock(directory / "ledger.jsonl.lock") if options.apply else None
    try:
        closed = {}
        runs = _read_ledger(ledger_path, closed_budgets=closed)
        run = runs.get(options.settle)
        # correction: a run whose known spend was booked wrongly (user-approved, proof bound to the original end).
        if (not run or run["end"] is None
                or not (_settleable_end(run["end"]) or proof.get("correction") is True)
                or proof["budget"] != run["budget"]
                or proof["original_end_sha256"] != _record_sha256(run["end"])):
            raise PaidRunError(
                "settlement needs the exact original ended run with unknown or placeholder spend"
            )
        if run.get("settlement") and run["settlement"]["proof_sha256"] != proof_sha:
            raise PaidRunError("run already has a different settlement; refusing replacement")
        budget_id = run["budget"]
        _load_budget(directory / "BUDGETS.json", budget_id)
        if options.close_budget and any(r["budget"] == budget_id and r["end"] is None for r in runs.values()):
            raise PaidRunError("cannot close budget while another run is unfinished")
        closure_needed = options.close_budget and budget_id not in closed
        settlement_needed = run.get("settlement") is None
        common = {"run_id": options.settle, "time": _utc_iso(), "budget": budget_id,
                  "proof": str(relative), "proof_sha256": proof_sha,
                  "reviewed_by": proof["reviewed_by"], "reason": proof["reason"]}
        if options.apply:
            # Close first: interruption between the two appends must never reopen spending.
            if closure_needed:
                _append_record(ledger_path, {**common, "event": "budget_closed"})
            if settlement_needed:
                _append_record(ledger_path, {**common, "event": "settlement", "spent_cny": spent,
                    "spend_known": True, "original_end_sha256": proof["original_end_sha256"],
                    **({"correction": True} if proof.get("correction") is True else {})})
        print(json.dumps({"run_id": options.settle, "budget": budget_id, "spent_cny": spent,
                          "apply": options.apply, "budget_closed": budget_id in closed or bool(options.apply and closure_needed),
                          "settlement_appended": bool(options.apply and settlement_needed),
                          "closure_appended": bool(options.apply and closure_needed)}))
        return 0
    finally:
        if lock_file is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def main(argv: list[str] | None = None) -> int:
    try:
        raw = list(sys.argv[1:] if argv is None else argv)
        option_args = raw[:raw.index("--")] if "--" in raw else raw
        if "--settle" in option_args or "--status" in option_args or "--close-unended" in option_args:
            if "--" in raw:
                raise PaidRunError("accounting commands never launch a child command")
            return _accounting_command(option_args, _project_root())
        options, command = _parse_cli(argv)
        root = _project_root()
        start_record, spend_file, _total, _committed, _remaining = _prepare_start(
            options, command, root
        )
    except PaidRunError as exc:
        print(f"paid_run: {exc}", file=sys.stderr)
        return 3
    except SystemExit as exc:
        return int(exc.code or 0)

    if options.dry_run:
        print(f"DRY RUN: would launch {json.dumps(command, ensure_ascii=False)}")
        print(f"cwd: {start_record['cwd']}")
        return 0

    budget_dir = config.paid_dir()
    run_id = start_record["run_id"]
    child_env = os.environ.copy()
    _fill_keys_from_keychain(child_env)
    child_env["PAID_RUN_ID"] = run_id
    child_env["PAID_RUN_CAP_CNY"] = str(options.cap)
    child_env["PAID_RUN_SPEND_FILE"] = str(spend_file)
    if options.call_cap is not None:
        child_env["PAID_RUN_CALL_CAP_CNY"] = str(options.call_cap)
    process_holder: list[subprocess.Popen | None] = [None]
    received_signal: list[int | None] = [None]
    previous_handlers = _install_signal_forwarding(process_holder, received_signal)
    heartbeat = _Heartbeat(budget_dir / HEARTBEAT_DIR, run_id)
    heartbeat.start()
    try:
        try:
            exit_code, stopped_by_cap, cap_stop_spend = _run_child(
                command, start_record["cwd"], child_env, spend_file,
                options.cap, options.call_cap, process_holder, received_signal,
            )
        except Exception as exc:
            print(f"paid_run: child monitoring failed: {exc}", file=sys.stderr)
            exit_code, stopped_by_cap, cap_stop_spend = 1, False, None
            process = process_holder[0]
            if process is not None and process.poll() is None:
                try:
                    _stop_process_group(
                        process,
                        received_signal[0] or signal.SIGTERM,
                        _env_positive_seconds("PAID_RUN_KILL_GRACE_SECONDS", 15.0),
                    )
                except Exception as stop_exc:
                    print(f"paid_run: could not stop child process group: {stop_exc}", file=sys.stderr)

        spent_cny, spend_details = _read_effective_spend(spend_file, options.call_cap)
        receipts = [Path(p).resolve() for p in options.receipts] or _find_receipts(command, root)
        calls = _call_status(receipts, _parse_utc(start_record["time"]))
        if calls and calls["released_upper_cny"] and spent_cny is not None:
            spent_cny = round(max(0.0, spent_cny - calls["released_upper_cny"]), 8)
        # explicit --receipts with no call at all also counts; an auto-found file with no line from this run does not
        no_success = calls is not None and not calls["http_2xx"] and bool(calls["calls"] or options.receipts)
        if no_success and exit_code == 0:
            exit_code = NO_SUCCESS_EXIT
        if received_signal[0] is not None:
            exit_code = 128 + received_signal[0]
        end_record = {
            "event": "end",
            "run_id": run_id,
            "time": _utc_iso(),
            "exit_code": exit_code,
            "spent_cny": spent_cny,
            "spend_known": spent_cny is not None,
            "stopped_by_cap": stopped_by_cap,
        }
        if stopped_by_cap:
            end_record["cap_stop_spend_cny"] = cap_stop_spend
        end_record.update(spend_details)
        if calls is not None:
            end_record["calls"] = calls
        if received_signal[0] is not None:
            end_record["exit_code"] = 128 + received_signal[0]
        _append_end(budget_dir / "ledger.jsonl", end_record)
    except PaidRunError as exc:
        print(f"paid_run: {exc}", file=sys.stderr)
        return 3
    finally:
        heartbeat.stop()  # every exit path, exceptions included
        _restore_signal_handlers(previous_handlers)

    if calls is None:
        print("paid_run: no receipts.jsonl found; call statuses not checked (pass --receipts)", file=sys.stderr)
    elif no_success:
        print(f"paid_run: ERROR no call returned HTTP 2xx ({calls['calls']} calls, {calls['http_failed']} HTTP errors, "
              f"{calls['no_status']} without status); failed calls settled at 0 CNY. Check the API key.", file=sys.stderr)
    if spent_cny is None:
        print(f"Spend unknown; this run reserves its full cap of {options.cap:.6f} CNY.")
    elif spent_cny > options.cap:
        label = "effective spend" if "unknown_calls" in spend_details else "reported spend"
        print(f"WARNING: {label} {spent_cny:.6f} CNY exceeds run cap "
              f"{options.cap:.6f} CNY.", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
