import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_runtime_health import (
    HEARTBEAT_PATH,
    HEARTBEAT_STALE_SECONDS,
    is_bot_heartbeat_stale,
    is_schedule_health_unhealthy,
    read_bot_heartbeat,
)


StopBot = Callable[[], None]
StartBot = Callable[[], None]
ProcessExists = Callable[[int], bool]

WATCHDOG_PID_PATH = Path(".runtime") / "bot_watchdog.pid"
WATCHDOG_LOG_PATH = Path("logs") / "watchdog" / "watchdog.log"
WATCHDOG_LOG_MAX_BYTES = 2 * 1024 * 1024
WATCHDOG_LOG_BACKUP_COUNT = 3
WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def default_stop_bot_processes(target_pid: int | None = None) -> None:
    pid_literal = int(target_pid or 0)
    script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$processes = @(Get-CimInstance Win32_Process)
$targetPid = {pid_literal}
$runnerPids = @($processes |
  Where-Object {{
    $_.Name -eq 'cmd.exe' -and
    $_.CommandLine -and
    $_.CommandLine -like '*啟動機器人_runner.bat*'
  }} | Select-Object -ExpandProperty ProcessId)
$toStop = @()
if ($targetPid -gt 0) {{ $toStop += $targetPid }}
$frontier = @($runnerPids)
while ($frontier.Count -gt 0) {{
  $toStop += $frontier
  $parents = @($frontier)
  $frontier = @($processes |
    Where-Object {{ $parents -contains $_.ParentProcessId }} |
    Select-Object -ExpandProperty ProcessId)
}}
$toStop | Sort-Object -Unique -Descending | ForEach-Object {{
  Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
}}
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=PROJECT_ROOT,
        creationflags=WINDOWS_CREATE_NO_WINDOW,
        check=False,
    )


def default_start_bot(launch_script: Path) -> None:
    subprocess.Popen(
        ["cmd", "/c", "start", "", str(launch_script)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=WINDOWS_CREATE_NO_WINDOW,
    )


def default_process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=WINDOWS_CREATE_NO_WINDOW,
        check=False,
    )
    return result.returncode == 0


def default_watchdog_process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    script = rf"""
$process = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' -ErrorAction SilentlyContinue
if ($process -and $process.CommandLine -like '*bot_watchdog.py*' -and $process.CommandLine -notlike '*--stop*') {{
  exit 0
}}
exit 1
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=WINDOWS_CREATE_NO_WINDOW,
        check=False,
    )
    return result.returncode == 0


def _read_pid_file(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def acquire_watchdog_instance(
    pid_path: Path,
    *,
    pid: int | None = None,
    process_exists: ProcessExists = default_watchdog_process_exists,
) -> tuple[bool, int | None]:
    """Atomically claim the watchdog pid file, replacing a stale claim."""
    current_pid = int(pid or os.getpid())
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            with pid_path.open("x", encoding="ascii") as handle:
                handle.write(str(current_pid))
            return True, current_pid
        except FileExistsError:
            existing_pid = _read_pid_file(pid_path)
            if existing_pid and process_exists(existing_pid):
                return False, existing_pid
            try:
                pid_path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                return False, existing_pid
    return False, _read_pid_file(pid_path)


def release_watchdog_instance(pid_path: Path, *, pid: int | None = None) -> None:
    current_pid = int(pid or os.getpid())
    if _read_pid_file(pid_path) != current_pid:
        return
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def _rotate_watchdog_log(path: Path, *, max_bytes: int, backup_count: int) -> None:
    try:
        should_rotate = path.exists() and path.stat().st_size >= max_bytes
    except OSError:
        should_rotate = False
    if not should_rotate or backup_count <= 0:
        return
    oldest = path.with_name(f"{path.name}.{backup_count}")
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            source.replace(target)
    path.replace(path.with_name(f"{path.name}.1"))


def append_watchdog_log(
    message: str,
    *,
    path: Path = WATCHDOG_LOG_PATH,
    now: datetime | None = None,
    max_bytes: int = WATCHDOG_LOG_MAX_BYTES,
    backup_count: int = WATCHDOG_LOG_BACKUP_COUNT,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_watchdog_log(path, max_bytes=max_bytes, backup_count=backup_count)
    current = now or datetime.now().astimezone()
    timestamp = current.isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def emit_watchdog_status(message: str, *, log_path: Path = WATCHDOG_LOG_PATH) -> None:
    try:
        append_watchdog_log(message, path=log_path)
    except OSError as exc:
        if sys.stderr is not None:
            print(f"[watchdog] log write failed: {exc}", file=sys.stderr, flush=True)
    if sys.stdout is not None:
        print(f"[watchdog] {message}", flush=True)


def stop_managed_processes(
    *,
    heartbeat_path: Path,
    pid_path: Path,
    stop_bot: Callable[[int | None], None] = default_stop_bot_processes,
    stop_process: Callable[[int], None] | None = None,
) -> str:
    payload = read_bot_heartbeat(heartbeat_path)
    bot_pid = _payload_pid(payload)
    watchdog_pid = _read_pid_file(pid_path)
    stop_bot(bot_pid)

    def terminate(pid: int) -> None:
        if stop_process is not None:
            stop_process(pid)
            return
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=WINDOWS_CREATE_NO_WINDOW,
            check=False,
        )

    if watchdog_pid and watchdog_pid != os.getpid():
        terminate(watchdog_pid)
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass
    return f"stopped: bot_pid={bot_pid or 'none'} watchdog_pid={watchdog_pid or 'none'}"


def _payload_pid(payload: dict) -> int | None:
    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _next_check_text(now: datetime | None, check_interval: int | None) -> str:
    if not check_interval:
        return ""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    next_check = current + timedelta(seconds=check_interval)
    return f" next_check={next_check.isoformat(timespec='seconds')}"


def run_watchdog_once(
    *,
    heartbeat_path: Path,
    stale_seconds: int,
    launch_script: Path,
    stop_bot: StopBot = default_stop_bot_processes,
    start_bot: StartBot | None = None,
    process_exists: ProcessExists = default_process_exists,
    check_interval: int | None = None,
    now: datetime | None = None,
) -> str:
    payload = read_bot_heartbeat(heartbeat_path)
    stale = is_bot_heartbeat_stale(heartbeat_path, stale_seconds=stale_seconds, now=now)
    pid = _payload_pid(payload)
    pid_alive = bool(pid and process_exists(pid))
    schedule_unhealthy, schedule_reason = is_schedule_health_unhealthy(payload, now=now)
    next_text = _next_check_text(now, check_interval)

    if not stale and pid_alive and not schedule_unhealthy:
        return f"healthy: pid={pid} updated_at={payload.get('updated_at')}{next_text}"

    if stop_bot is default_stop_bot_processes:
        default_stop_bot_processes(pid)
    else:
        stop_bot()
    starter = start_bot or (lambda: default_start_bot(launch_script))
    starter()
    if not payload:
        reason = "heartbeat missing"
    elif stale:
        reason = "heartbeat stale"
    elif not pid:
        reason = "pid missing"
    elif schedule_unhealthy:
        reason = f"schedule unhealthy: {schedule_reason}"
    else:
        reason = f"pid not running ({pid})"
    return f"restarted: {reason}; launched={launch_script.name}{next_text}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal watchdog for stock_ai_bot heartbeat.")
    parser.add_argument("--heartbeat", default=str(HEARTBEAT_PATH), help="heartbeat json path")
    parser.add_argument("--stale-seconds", type=int, default=HEARTBEAT_STALE_SECONDS)
    parser.add_argument("--check-interval", type=int, default=300)
    parser.add_argument("--launch", default="啟動機器人_runner.bat")
    parser.add_argument("--pid-file", default=str(WATCHDOG_PID_PATH))
    parser.add_argument("--log", default=str(WATCHDOG_LOG_PATH))
    parser.add_argument("--stop", action="store_true", help="stop the managed runner and watchdog")
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    heartbeat_path = Path(args.heartbeat)
    pid_path = Path(args.pid_file)
    log_path = Path(args.log)
    launch_script = Path(args.launch)
    if not launch_script.is_absolute():
        launch_script = PROJECT_ROOT / launch_script

    if args.stop:
        result = stop_managed_processes(heartbeat_path=heartbeat_path, pid_path=pid_path)
        emit_watchdog_status(result, log_path=log_path)
        return

    acquired, existing_pid = acquire_watchdog_instance(pid_path)
    if not acquired:
        emit_watchdog_status(
            f"already running: pid={existing_pid or 'unknown'}",
            log_path=log_path,
        )
        return

    try:
        emit_watchdog_status(f"started: pid={os.getpid()}", log_path=log_path)
        while True:
            result = run_watchdog_once(
                heartbeat_path=heartbeat_path,
                stale_seconds=args.stale_seconds,
                launch_script=launch_script,
                check_interval=max(10, int(args.check_interval)),
            )
            emit_watchdog_status(result, log_path=log_path)
            if args.once:
                return
            time.sleep(max(10, int(args.check_interval)))
    finally:
        release_watchdog_instance(pid_path)


if __name__ == "__main__":
    main()
