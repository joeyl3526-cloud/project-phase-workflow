"""Small JSON-RPC client for the existing Codex daemon's local stdio proxy.

No new app-server, model selection, thread creation or remote endpoint. Only
scoped thread reads and an explicitly authorized review thread's queue are used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time


class CodexError(RuntimeError):
    pass


def resolve_codex_executable(executable):
    """Preserve an existing explicit path; recover only the official app package."""
    if Path(executable).is_file():
        return str(Path(executable).resolve())
    if os.name != 'nt':
        raise CodexError('Configured Codex executable is unavailable')
    script=("$ErrorActionPreference='Stop'; Get-AppxPackage -Name OpenAI.Codex | "
        "Where-Object PackageFamilyName -EQ 'OpenAI.Codex_2p2nqsd0c76g0' | "
        "Select-Object -ExpandProperty InstallLocation")
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        package=Path(result.stdout.strip())
        if result.returncode or not re.fullmatch(r'OpenAI\.Codex_[0-9.]+_(?:x64|arm64)__2p2nqsd0c76g0',package.name):
            raise ValueError()
        candidate=package/'app/resources/codex.exe'
        if not candidate.is_file():
            raise ValueError()
        return str(candidate.resolve())
    except (OSError,subprocess.TimeoutExpired,ValueError):
        raise CodexError('Codex executable is unavailable; check the current official app installation') from None


class CodexRPC:
    ALLOWED = {'initialize', 'thread/read', 'thread/queue/list', 'thread/queue/add',
               'thread/queue/start', 'thread/queue/delete', 'thread/items/list', 'thread/turns/list'}

    def __init__(self, executable, timeout=12, mode='proxy'):
        self.executable, self.timeout = executable, timeout
        self.mode = mode
        self.process = None
        self.messages = queue.Queue(maxsize=64)
        self.mutex = threading.RLock()
        self.number = 0

    def close(self):
        process, self.process = self.process, None
        if process:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()  # Only this client's own proxy, never the daemon.
                process.wait(timeout=3)

    def _connect(self):
        if self.process and self.process.poll() is None:
            return
        self.close()
        self.messages = queue.Queue(maxsize=64)
        try:
            self.executable=resolve_codex_executable(self.executable)
            args = [self.executable, 'app-server', 'proxy' if self.mode == 'proxy' else '--stdio']
            process = subprocess.Popen(args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError:
            raise CodexError('Codex local proxy is unavailable') from None
        self.process = process
        messages = self.messages

        def consume():
            try:
                for line in process.stdout:
                    if len(line) > 16_000_000:
                        break
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if 'id' in item and ('result' in item or 'error' in item):
                        try:
                            messages.put(item, timeout=1)
                        except queue.Full:
                            break
            finally:
                try:
                    messages.put_nowait({'_closed': True})
                except queue.Full:
                    pass

        threading.Thread(target=consume, daemon=True).start()
        self._request('initialize', {'clientInfo': {'name': 'phase-workflow',
                        'title': 'Phase workflow local bridge', 'version': '1.2.0'},
                        'capabilities': {'experimentalApi': True}})
        process.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n')
        process.stdin.flush()

    def _request(self, method, params):
        self.number += 1
        request_id = self.number
        try:
            self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': request_id,
                'method': method, 'params': params}, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                item = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
                if item.get('_closed'):
                    raise CodexError('Codex local proxy closed; review remains pending')
                if item.get('id') != request_id:
                    continue
                if 'error' in item:
                    # Never export an arbitrary RPC error body or original chat contents.
                    raise CodexError('Codex RPC ' + method + ' failed (' + str(item['error'].get('code')) + ')')
                return item['result']
        except (OSError, ValueError, queue.Empty):
            raise CodexError('Codex local RPC timed out or disconnected') from None
        raise CodexError('Codex local RPC timed out')

    def request(self, method, params):
        if method not in self.ALLOWED:
            raise ValueError('Operation is outside the bridge Codex RPC allowlist')
        if self.mode != 'proxy' and method == 'thread/queue/start':
            raise ValueError('A storage reader must never start an inference turn')
        with self.mutex:
            try:
                self._connect()
                return self._request(method, params)
            except (CodexError, OSError):
                self.close()
                raise

    def inspect(self, thread_id, callback_id):
        """Check only the exact authorized thread; absence is not a delivery ACK."""
        thread = self.request('thread/read', {'threadId': thread_id})['thread']
        cursor = None
        for _ in range(10):
            params = {'threadId': thread_id, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            queued = self.request('thread/queue/list', params)
            for item in queued.get('data', queued.get('items', [])):
                if item.get('clientUserMessageId') == callback_id:
                    return {'state': 'QUEUED', 'queue_id': item.get('id', item.get('queuedSubmissionId')),
                            'thread_status': thread.get('status', {}).get('type')}
            cursor = queued.get('nextCursor')
            if not cursor:
                break
        if cursor:
            return {'state': 'UNKNOWN', 'reason': 'Queue inspection bound reached'}
        # Bounded, scoped history read; raw chat content is never persisted by this client.
        history = self.request('thread/items/list', {'threadId': thread_id,
                    'limit': 100, 'sortDirection': 'desc'})
        for entry in history.get('data', []):
            item = entry.get('item', entry)
            if item.get('type') == 'userMessage' and callback_id in json.dumps(item.get('content', []), ensure_ascii=False):
                return {'state': 'RECEIVED', 'thread_status': thread.get('status', {}).get('type')}
        return {'state': 'ABSENT', 'thread_status': thread.get('status', {}).get('type'),
                'history_truncated': bool(history.get('nextCursor'))}

    def enqueue(self, thread_id, callback_id, message):
        return self.request('thread/queue/add', {'threadId': thread_id,
            'clientUserMessageId': callback_id,
            'input': [{'type': 'text', 'text': message, 'text_elements': []}]})

    def start(self, thread_id, queue_id=None):
        params = {'threadId': thread_id}
        if queue_id:
            params['queuedSubmissionId'] = queue_id
        return self.request('thread/queue/start', params)
