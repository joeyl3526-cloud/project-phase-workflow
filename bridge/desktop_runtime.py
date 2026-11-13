"""Visible CLI launch and bounded service startup; legacy Desktop compatibility."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import time
import re

from opencode_transport import OpenCodeTransport, TransportError


def ensure_cli_service(config, transport, timeout=None):
    """Reuse or start the native CLI background service without a GUI."""
    executable = Path(config.get('opencode_exe', ''))
    if not executable.is_file():
        raise RuntimeError('The configured OpenCode CLI executable is unavailable')
    selected_shell = config.get('cli_shell_exe')
    if selected_shell:
        selected_shell = Path(selected_shell)
        if not selected_shell.is_file():
            raise RuntimeError('The configured CLI shell is unavailable; no task was sent')
    def ready():
        schema = transport.schema(refresh=True)
        operations = {entry.get('operationId') for item in schema.get('paths', {}).values()
            for entry in item.values() if isinstance(entry, dict)}
        if not {'session.create', 'session.get', 'session.prompt'}.issubset(operations):
            raise RuntimeError('The CLI service schema is incompatible; no task was sent')
        return True
    try:
        ready()
        return {'client': 'cli', 'service_started': False, 'http_ready': True}
    except TransportError:
        transport.invalidate()
    try:
        environment = os.environ.copy()
        environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        if selected_shell:
            environment['SHELL'] = selected_shell.resolve().as_posix()
        result = subprocess.run([str(executable), 'service', 'start'], capture_output=True,
            timeout=config.get('service_start_seconds', 30),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=environment)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('CLI service startup is unconfirmed; inspect status before retry') from None
    if result.returncode:
        raise RuntimeError('The CLI background service could not start; no task was sent')
    transport.invalidate()
    deadline = time.monotonic() + (timeout if timeout is not None else config.get('service_ready_seconds', 60))
    while True:
        try:
            ready()
            return {'client': 'cli', 'service_started': True, 'http_ready': True}
        except TransportError:
            transport.invalidate()
            if time.monotonic() >= deadline:
                raise RuntimeError('The CLI local service is not ready; no task was sent') from None
            time.sleep(min(1, max(0, deadline-time.monotonic())))


def console_process(pid, executable):
    """Identify a live viewer by executable and process creation time."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        code = w.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        name, length = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
            return None
        if os.path.normcase(name.value) != os.path.normcase(str(Path(executable).resolve())):
            return None
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def open_session_console(config, workspace, session_id, previous=None):
    """Show an exact session using the user-approved visible CLI fallback."""
    if config.get('visible_session_client') != 'cli':
        return None
    if os.name != 'nt' or not re.fullmatch(r'ses_[A-Za-z0-9_-]+', session_id):
        raise ValueError('A valid native session is required for the visible client')
    executable = Path(config['opencode_exe']).resolve(strict=True)
    console_executable = Path(config['python_exe']).resolve(strict=True)
    host, port = OpenCodeTransport(config).address()
    server = 'http://' + (f'[{host}]' if ':' in host else host) + f':{port}'
    if previous and previous.get('session_id') == session_id and previous.get('server') == server:
        identity = console_process(previous['pid'], console_executable)
        if identity is not None and identity == previous.get('created'):
            return previous
    import ctypes
    from ctypes import wintypes as w
    class ExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', w.DWORD), ('fMask', w.ULONG), ('hwnd', w.HWND),
            ('lpVerb', w.LPCWSTR), ('lpFile', w.LPCWSTR), ('lpParameters', w.LPCWSTR),
            ('lpDirectory', w.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', w.HINSTANCE),
            ('lpIDList', ctypes.c_void_p), ('lpClass', w.LPCWSTR), ('hkeyClass', w.HKEY),
            ('dwHotKey', w.DWORD), ('hIcon', w.HANDLE), ('hProcess', w.HANDLE)]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ExecuteInfo)]
    shell.ShellExecuteExW.restype = w.BOOL
    kernel.GetProcessId.argtypes = [w.HANDLE]
    kernel.GetProcessId.restype = w.DWORD
    kernel.CloseHandle.argtypes = [w.HANDLE]
    # A console Python process gives the native CLI real console handles even
    # when the coordinator was launched by pythonw without standard streams.
    # The explicit server route needs local-service authentication. Read it
    # only inside the console launcher and pass via its child's environment;
    # never place a password in process arguments, logs, or the persisted run.
    credential = Path(config.get('service_config', Path.home()/'.config/opencode/service.json'))
    launcher = ('import json, os, pathlib, subprocess, sys\n'
        'env = os.environ.copy()\n'
        'password = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8-sig"))["password"]\n'
        'env["OPENCODE_PASSWORD"] = password\n'
        'env["OPENCODE_SERVER_PASSWORD"] = password\n'
        'raise SystemExit(subprocess.call(sys.argv[2:], env=env))')
    info = ExecuteInfo(cbSize=ctypes.sizeof(ExecuteInfo), fMask=0x40 | 0x400,
        lpVerb='open', lpFile=str(console_executable),
        lpParameters=subprocess.list2cmdline(['-B', '-X', 'utf8', '-c', launcher, str(credential),
            str(executable), '--server', server, '--session', session_id, str(Path(workspace).resolve())]),
        lpDirectory=str(workspace), nShow=1)
    if not shell.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        raise RuntimeError('The visible session client could not be opened')
    try:
        pid = kernel.GetProcessId(info.hProcess)
    finally:
        kernel.CloseHandle(info.hProcess)
    time.sleep(0.8)
    identity = console_process(pid, console_executable)
    if identity is None:
        raise RuntimeError('The visible session client exited before readiness')
    return {'status': 'CLI_VISIBLE', 'session_id': session_id, 'pid': pid,
            'created': identity, 'client': 'cli', 'server': server}


def desktop_running(executable):
    if os.name != 'nt':
        return False
    # Process inspection only. No UI input, injected code, ports or credentials.
    import ctypes
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('pid', w.DWORD),
            ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
            ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
            ('exe', w.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.OpenProcess.restype = w.HANDLE
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise RuntimeError('Desktop process inventory is unavailable')
    entry = Entry(dwSize=ctypes.sizeof(Entry))
    target = os.path.normcase(str(Path(executable).resolve()))
    try:
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.exe.lower() == Path(executable).name.lower():
                handle = kernel.OpenProcess(0x1000, False, entry.pid)
                if handle:
                    try:
                        buffer, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            if os.path.normcase(buffer.value) == target:
                                return True
                    finally:
                        kernel.CloseHandle(handle)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(snapshot)


def ensure_desktop(config, transport, timeout=None):
    executable = config.get('desktop_exe', '')
    if not Path(executable).is_file():
        raise RuntimeError('OpenCode Desktop executable is unavailable')
    launched = False
    if not desktop_running(executable):
        subprocess.Popen([executable, 'opencode://'], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        launched = True
        transport.invalidate()
    deadline = time.monotonic() + (timeout if timeout is not None else config.get('desktop_ready_seconds', 60))
    while True:
        try:
            schema = transport.schema(refresh=True)
            operations = {entry.get('operationId') for item in schema.get('paths', {}).values()
                          for entry in item.values() if isinstance(entry, dict)}
            if not {'session.create', 'session.get', 'session.prompt'}.issubset(operations):
                raise RuntimeError('Desktop local service has incompatible session operations')
            if desktop_running(executable):
                return {'desktop_running': True, 'launched': launched,
                        'http_ready': True, 'session_selection': 'NOT_VERIFIED'}
            if time.monotonic() >= deadline:
                raise RuntimeError('Desktop did not remain running; no task was dispatched')
            time.sleep(0.5)
        except TransportError:
            if time.monotonic() >= deadline:
                raise RuntimeError('Desktop local HTTP service is not ready; no task was dispatched') from None
            time.sleep(min(3, max(0, deadline - time.monotonic())))
