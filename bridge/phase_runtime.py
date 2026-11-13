"""One per-user coordinator shared by MCP entry points and Windows logon.

State remains versioned JSON for compatibility. I/O happens outside global locks;
revision checks reject stale writes. Only opt-in 1.2 runs are recovered at logon.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import re
from pathlib import Path
import subprocess
import time

from phase_bridge import Bridge, VERSION, atomic_json, lock, now, read_json
from opencode_transport import TransportError

ACTIVE = {'RUNNING', 'DISPATCH_UNCERTAIN', 'REVIEW_PENDING', 'EXECUTION_FAILED',
          'EVIDENCE_MISSING', 'MONITOR_TIMEOUT', 'INTERRUPTED', 'DISPATCHING'}


def ensure_coordinator(config_path):
    config_path = Path(config_path).resolve()
    config = read_json(config_path)
    state = Path(config['state_dir'])
    with lock(state / 'coordinator-start.lock'):
        try:
            with lock(state / 'coordinator.lock', timeout=0.1):
                pass
        except RuntimeError:
            return {'status': 'ALREADY_RUNNING'}
        (state / 'coordinator.stop').unlink(missing_ok=True)
        task_name = config.get('startup_task_name')
        requested_at = time.time()
        process = None
        if task_name:
            if os.name != 'nt' or not re.fullmatch(r'PhaseWorkflow-PythonBridge-\d+', task_name):
                raise ValueError('Invalid owned coordinator task name')
            try:
                result = subprocess.run(['schtasks.exe', '/Run', '/TN', task_name],
                    capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except (OSError, subprocess.TimeoutExpired):
                raise RuntimeError('Coordinator task launch is unconfirmed; inspect task and runtime status') from None
            if result.returncode:
                raise RuntimeError('Coordinator task could not start; inspect the installed task definition')
        else:
            log_path = state / 'coordinator.log'
            with log_path.open('ab') as log:
                process = subprocess.Popen([config['python_exe'], '-B', '-X', 'utf8',
                    config['bridge_script'], '--config', str(config_path), 'coordinator'],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) |
                                  getattr(subprocess, 'DETACHED_PROCESS', 0))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status_file = state / 'coordinator.json'
            if status_file.is_file():
                status = read_json(status_file)
                owned = status.get('heartbeat_epoch', 0) >= requested_at if task_name else status.get('pid') == process.pid
                if owned and status.get('state') == 'RUNNING':
                    return {'status': 'RUNNING', 'pid': status['pid'],
                            'owner': 'task_scheduler' if task_name else 'standalone'}
            if process is not None and process.poll() is not None:
                raise RuntimeError('Coordinator exited before readiness; inspect its operational log')
            time.sleep(0.1)
        raise RuntimeError('Coordinator readiness is unconfirmed; inspect status before retry')


def tick(bridge, run_id):
    run = bridge.load(run_id)
    if run['paused'] or not run.get('runtime_managed'):
        return
    if run['status'] == 'PREPARED' and run.get('advance_requested'):
        bridge.dispatch(run_id)
        return
    if run['status'] not in ACTIVE:
        return
    if run['status'] == 'DISPATCHING':
        # A live owner may be waiting for native empty-session creation. If it
        # exited, no prompt intent was persisted, so review the local failure.
        try:
            with lock(bridge.state / 'dispatch-locks' / (run_id + '.lock'), timeout=0.01):
                with lock(bridge.state / 'state.lock'):
                    latest = bridge.load(run_id)
                    if latest['status'] == 'DISPATCHING' and not latest['paused']:
                        latest['status'] = 'EXECUTION_FAILED'
                        latest['current']['dispatch_error'] = 'Dispatch owner exited before prompt admission intent; no automatic resend'
                        bridge.save(latest)
                    run = latest
        except RuntimeError:
            return
    if run['status'] in ('RUNNING', 'DISPATCH_UNCERTAIN', 'MONITOR_TIMEOUT'):
        if run.get('monitor', {}).get('state') == 'WAITING_SERVICE':
            bridge.ensure_service()
        run = bridge.collect(run_id)
    if run['status'] in ('REVIEW_PENDING', 'EXECUTION_FAILED', 'EVIDENCE_MISSING', 'INTERRUPTED'):
        if run['current'].get('callback') not in ('RECEIVED', 'REVIEW_OBSERVED'):
            due = run['current'].get('next_delivery_check', 0)
            if time.time() >= due:
                bridge.callback(run_id)
    with lock(bridge.state / 'state.lock'):
        latest = bridge.load(run_id)
        if latest['status'] not in ACTIVE or latest['paused']:
            return
        latest['monitor'] = {'state': 'HEALTHY', 'observed_at': now(), 'owner': 'coordinator'}
        bridge.save(latest)


def coordinator(config_path, stop_after=None):
    config_path = Path(config_path).resolve()
    config = read_json(config_path)
    state = Path(config['state_dir'])
    started = time.monotonic()
    startup_epoch = time.time()
    try:
        guard = lock(state / 'coordinator.lock', timeout=0.2)
        guard.__enter__()
    except RuntimeError:
        return {'status': 'ALREADY_RUNNING'}
    # A direct Windows logon action is an explicit fresh start too. Only the
    # guard owner may consume an OLD stop signal; a duplicate must not cancel
    # another process's stop, and a stop requested during startup still wins.
    stop_file = state / 'coordinator.stop'
    if stop_file.is_file() and stop_file.stat().st_mtime <= startup_epoch:
        stop_file.unlink(missing_ok=True)
    bridges, pending, scheduled = {}, {}, {}
    heartbeat = 0
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='phase-io') as pool:
            while not (state / 'coordinator.stop').exists():
                if stop_after is not None and time.monotonic() - started >= stop_after:
                    break
                for run_id, future in list(pending.items()):
                    if not future.done():
                        continue
                    try:
                        future.result()
                    except Exception as error:
                        bridge = bridges[run_id]
                        with lock(state / 'state.lock'):
                            run = bridge.load(run_id)
                            previous = run.get('monitor', {})
                            run['monitor'] = {'state': 'WAITING_SERVICE' if isinstance(error, TransportError) else 'ERROR',
                                'error_type': type(error).__name__, 'observed_at': now(),
                                'failure_count': previous.get('failure_count', 0) + 1,
                                'owner': 'coordinator'}
                            bridge.save(run)
                        scheduled[run_id] = time.monotonic() + min(30, 2 ** min(previous.get('failure_count', 0) + 1, 5))
                    del pending[run_id]
                active = 0
                live_ids = set()
                for entry in sorted((state / 'active-runs').glob('*.json')):
                    path = state / 'runs' / entry.name
                    try:
                        run = read_json(path)
                    except (OSError, ValueError):
                        continue
                    if not run.get('runtime_managed') or run['paused']:
                        continue
                    if run['status'] not in ACTIVE and not (run['status'] == 'PREPARED' and run.get('advance_requested')):
                        continue
                    active += 1
                    run_id = run['run_id']
                    live_ids.add(run_id)
                    if run_id in pending or time.monotonic() < scheduled.get(run_id, 0):
                        continue
                    if len(pending) >= 2:
                        break  # Bounded work queue, no unbounded executor backlog.
                    if run_id not in bridges:
                        bridges[run_id] = Bridge(config_path)
                    bridge = bridges[run_id]
                    pending[run_id] = pool.submit(tick, bridge, run_id)
                    scheduled[run_id] = time.monotonic() + config.get('poll_seconds', 10)
                for run_id in list(bridges):
                    if run_id not in live_ids and run_id not in pending:
                        del bridges[run_id]
                        scheduled.pop(run_id, None)
                if time.monotonic() >= heartbeat:
                    atomic_json(state / 'coordinator.json', {'version': VERSION, 'pid': os.getpid(),
                        'state': 'RUNNING', 'heartbeat_at': now(), 'heartbeat_epoch': time.time(),
                        'active_runs': active, 'inflight': len(pending)})
                    heartbeat = time.monotonic() + 5
                time.sleep(0.25 if active else 2)
        return {'status': 'STOPPED'}
    finally:
        atomic_json(state / 'coordinator.json', {'version': VERSION, 'pid': os.getpid(),
                    'state': 'STOPPED', 'heartbeat_at': now(), 'heartbeat_epoch': time.time()})
        guard.__exit__(None, None, None)
