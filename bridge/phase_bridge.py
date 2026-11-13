"""Local phase-workflow bridge. Python stdlib; visible CLI and local service."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from opencode_transport import OpenCodeTransport, TransportError

VERSION = '1.3.2'
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def read_json(path):
    # Windows can briefly deny opening a state file during concurrent native
    # handle/replacement activity. Retry only bounded access denial; malformed
    # JSON, missing files and persistent permission failures remain errors.
    for attempt in range(5):
        try:
            text = Path(path).read_text(encoding='utf-8-sig')
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    return json.loads(text)


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data, *, ensure_ascii=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=ensure_ascii, indent=2), encoding='utf-8')
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def lock(path, timeout=45):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        if os.fstat(file.fileno()).st_size == 0:
            file.write(b'0')
            file.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                file.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('State is busy; retry status before any new dispatch')
                time.sleep(0.1)
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)


class Bridge:
    def __init__(self, config):
        self.config_path = Path(config).resolve()
        self.config = read_json(self.config_path)
        self.state = Path(self.config['state_dir']).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.transport = OpenCodeTransport(self.config)

    def api(self, operation, data=None, params=None, timeout=30):
        return self.transport.request(operation, data, params, timeout)

    def ensure_service(self):
        if self.config.get('execution_client') == 'cli':
            if self.config.get('visible_session_client') != 'cli':
                raise ValueError('CLI execution requires the visible CLI client')
            if self.config.get('service_auto_start', True):
                from desktop_runtime import ensure_cli_service
                return ensure_cli_service(self.config, self.transport)
            return None
        # Compatibility for older profiles; the installed CLI-only profile
        # never enters this branch and does not require a Desktop path.
        if self.config.get('desktop_auto_start'):
            from desktop_runtime import ensure_desktop
            return ensure_desktop(self.config, self.transport)

    def check_schema(self):
        schema = self.api('GET /openapi.json')
        operations = {entry.get('operationId') for item in schema.get('paths', {}).values()
                      for entry in item.values() if isinstance(entry, dict)}
        required = {'session.create', 'session.get', 'session.prompt', 'session.inbox.list', 'session.interrupt'}
        if self.config.get('event_monitoring'):
            required.add('experimental.session.log')
        missing = sorted(required - operations)
        props = schema.get('paths', {}).get('/api/session/{sessionID}/prompt', {}).get('post', {}).get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema', {}).get('properties', {})
        compatible = not missing and {'text', 'id', 'resume'}.issubset(props)
        result = {'at': now(), 'compatible': compatible, 'missing_operations': missing,
                  'sha256': hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                  'openapi': schema.get('openapi')}
        atomic_json(self.state / 'schema-check.json', result)
        if not compatible:
            raise RuntimeError('OpenCode schema is incompatible; no task was sent')
        return result

    def workspace(self, value):
        path = Path(value).resolve(strict=True)
        if not path.is_dir():
            raise ValueError('Workspace must be a directory')
        if not any(path.is_relative_to(Path(root).resolve()) for root in self.config['allowed_roots']):
            raise ValueError('Workspace is outside explicitly configured roots')
        return path

    @staticmethod
    def inside(workspace, value, exists=False):
        path = Path(value)
        path = (path if path.is_absolute() else Path(workspace) / path).resolve(strict=exists)
        if not path.is_relative_to(Path(workspace).resolve()):
            raise ValueError('Path escapes this run workspace')
        return path

    def runpath(self, run_id):
        if not re.fullmatch(r'run_[a-f0-9]{24}', run_id):
            raise ValueError('Invalid run id')
        return self.state / 'runs' / (run_id + '.json')

    def load(self, run_id):
        return read_json(self.runpath(run_id))

    def save(self, run):
        run['updated_at'] = now()
        run['revision'] = run.get('revision', 0) + 1
        atomic_json(self.runpath(run['run_id']), run)
        if run.get('runtime_managed'):
            index = self.state / 'active-runs' / (run['run_id'] + '.json')
            if run['status'] in ('COMPLETED', 'BLOCKED', 'WAITING_USER', 'PAUSED'):
                index.unlink(missing_ok=True)
            else:
                atomic_json(index, {'run_id': run['run_id']})

    def status(self, run_id=None):
        runtime_path = self.state / 'coordinator.json'
        runtime = read_json(runtime_path) if runtime_path.is_file() else {'state': 'NOT_STARTED'}
        if runtime.get('state') == 'RUNNING' and time.time() - runtime.get('heartbeat_epoch', 0) > 30:
            runtime['state'] = 'STALE_HEARTBEAT'
        if run_id:
            return {**self.load(run_id), 'runtime_health': runtime}
        schema = self.state / 'schema-check.json'
        return {'bridge_version': VERSION, 'allowed_roots': self.config['allowed_roots'],
                'transport': 'Python stdlib direct local HTTP',
                'session_log': bool(self.config.get('event_monitoring')),
                'reuse_session': bool(self.config.get('reuse_session')),
                'state_dir': str(self.state), 'schema_check': read_json(schema) if schema.exists() else None,
                'runs': [{'run_id': p.stem, 'state': read_json(p)['status']} for p in sorted((self.state / 'runs').glob('*.json'))],
                'runtime_health': runtime,
                'automatic_review': 'Requires a user-authorized reviewer_thread_id; queued is not reviewed'}

    def create_run(self, workspace, mode, stages, reviewer_thread_id=None, model=None, max_attempts=3, context=None):
        root = self.workspace(workspace)
        if mode not in ('A', 'B') or not 1 <= max_attempts <= 5:
            raise ValueError('Choose A/B and 1..5 attempts per stage')
        if not stages or len(stages) > 50:
            raise ValueError('Provide 1..50 approved stages')
        seen = set()
        for stage in stages:
            if not {'id', 'goal', 'required_files'}.issubset(stage) or not stage['goal'].strip():
                raise ValueError('Each stage needs id, goal and required_files')
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,50}', stage['id']) or stage['id'] in seen:
                raise ValueError('Stage ids must be unique and filesystem safe')
            seen.add(stage['id'])
            for path in stage['required_files']:
                if not self.inside(root, path, exists=True).is_file():
                    raise ValueError('Required reference is not a file')
        if reviewer_thread_id and not re.fullmatch(r'[a-f0-9-]{36}', reviewer_thread_id):
            raise ValueError('Use an exact authorized Codex thread UUID')
        if model and not {'providerID', 'id'}.issubset(model):
            raise ValueError('Model must be an OpenCode model reference')
        if self.config.get('require_explicit_model') and not model:
            raise ValueError('This installation requires the explicitly approved implementation model')
        if context is not None and (not isinstance(context, dict) or len(json.dumps(context)) > 128_000):
            raise ValueError('Context must be a bounded object of confirmed requirements and constraints')
        for value in (context or {}).get('protected_files', []):
            self.inside(root, value, exists=True)
        run = {'run_id': 'run_' + uuid.uuid4().hex[:24], 'workspace': str(root), 'mode': mode,
               'stages': stages, 'stage_index': 0, 'attempt_number': 0, 'max_attempts': max_attempts,
               'reviewer_thread_id': reviewer_thread_id, 'model': model, 'status': 'PREPARED',
               'paused': False, 'history': [], 'created_at': now(),
               'context': context or {}, 'context_version': 1,
               'strict_context': bool(self.config.get('require_context')),
               'runtime_managed': bool(self.config.get('coordinator'))}
        self.save(run)
        return run

    def _session(self, run):
        session_id = run.get('session_id') if self.config.get('reuse_session') else run.pop('prepared_session_id', None)
        if session_id:
            session = self.api('session.get', params={'sessionID': session_id})['data']
            if session.get('time', {}).get('archived'):
                raise ValueError('The dedicated OpenCode session is archived; restore it before dispatch')
            if run.get('model') and any(session.get('model', {}).get(k) != v for k, v in run['model'].items()):
                raise ValueError('Dedicated session model changed; restore the approved model in OpenCode')
            if Path(session.get('location', {}).get('directory', '')).resolve() != Path(run['workspace']).resolve():
                raise ValueError('Dedicated session workspace changed; reconcile before dispatch')
            return session
        body = {'title': 'Phase workflow: ' + run['run_id'], 'location': {'directory': run['workspace']},
                'metadata': {'phase_workflow': {'run_id': run['run_id']}}}
        if run.get('model'):
            body['model'] = run['model']
        session = self.api('session.create', body)['data']
        if self.config.get('reuse_session'):
            run['session_id'] = session['id']
        return session

    def prepare_desktop(self, run_id, open_window=True):
        """Create only an empty dedicated session; never submit a prompt."""
        self.runpath(run_id)
        with lock(self.state / 'dispatch-locks' / (run_id + '.lock'), timeout=0.2):
            return self._prepare_desktop(run_id, open_window)

    def _prepare_desktop(self, run_id, open_window):
        if open_window or self.config.get('execution_client') == 'cli':
            self.ensure_service()
        snapshot = self.load(run_id)
        self.workspace(snapshot['workspace'])
        if snapshot['status'] != 'PREPARED' or snapshot['paused']:
            raise ValueError('Session preparation requires an unpaused PREPARED run')
        self.check_schema()
        session = self._session(snapshot)
        with lock(self.state / 'state.lock'):
            run = self.load(run_id)
            if run['status'] != 'PREPARED' and not (run['paused'] and run.get('before_pause') == 'PREPARED'):
                raise ValueError('Run changed during session preparation; no prompt was sent')
            if snapshot.get('session_id'):
                run['session_id'] = snapshot['session_id']
            run['prepared_session_id'] = session['id']
            run['desktop'] = {'session_id': session['id'], 'title': session.get('title'),
                              'status': 'CLI_SESSION_READY' if self.config.get('execution_client') == 'cli' else 'SELECTION_REQUIRED',
                              'prepared_at': now(),
                              'instructions': 'Open the visible CLI for this exact session. No prompt has been submitted.' if self.config.get('execution_client') == 'cli' else 'Select this dedicated session once in OpenCode Desktop. Later stages reuse it. No prompt has been submitted.'}
            self.save(run)
        if open_window and not run['paused']:
            from desktop_runtime import open_session_console
            viewer = open_session_console(self.config, run['workspace'], session['id'], run.get('session_viewer'))
            if viewer:
                with lock(self.state / 'state.lock'):
                    latest = self.load(run_id)
                    latest['session_viewer'] = viewer
                    latest['desktop']['status'] = 'CLI_VISIBLE'
                    latest['desktop']['instructions'] = 'The visible CLI is bound to this exact session.'
                    self.save(latest)
                return self.load(run_id)
            if self.config.get('execution_client') == 'cli':
                raise RuntimeError('The visible CLI is unavailable; no prompt was sent')
            executable = self.config.get('desktop_exe', '')
            if not Path(executable).is_file():
                raise RuntimeError('Dedicated session is ready, but the OpenCode Desktop executable is unavailable')
            # Verified registered URI only raises/focuses the app; do not invent a session URI.
            subprocess.Popen([executable, 'opencode://'], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN)
        return self.load(run_id)

    def context_packet(self, run, stage, task_id):
        references = {}
        total = 0
        for value in stage['required_files']:
            path = self.inside(run['workspace'], value, exists=True)
            size = path.stat().st_size
            total += size
            if size > 2_000_000 or total > 4_000_000:
                raise ValueError('Context references exceed 4 MB; provide focused references')
            references[str(path)] = {'sha256': file_digest(path)}
        protected = {}
        for value in run.get('context', {}).get('protected_files', []):
            path = self.inside(run['workspace'], value, exists=True)
            protected[str(path)] = file_digest(path)
        packet = {'schema': 1, 'run_id': run['run_id'], 'task_id': task_id,
            'context_version': run.get('context_version', 1), 'workspace': run['workspace'],
            'mode': run['mode'], 'model': run.get('model'), 'stage': stage,
            'confirmed_context': run.get('context', {}), 'references': references,
            'protected_files': protected, 'repair_instructions': run.get('repair_instructions'),
            'previous_review': run['history'][-1].get('review') if run['history'] else None}
        if self.config.get('language_policy'):
            packet['language_policy'] = self.config['language_policy'].copy()
        digest = hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return packet, digest

    def check_context(self, run):
        current = run['current']
        if run.get('strict_context'):
            packet = read_json(self.inside(run['workspace'], Path(current['directory']) / 'context.json', exists=True))
            claimed = packet.pop('context_hash', None)
            actual = hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if claimed != current['context_hash'] or actual != current['context_hash']:
                raise ValueError('Context packet changed; reconcile before review')
        for path, digest in current.get('context_packet', {}).get('protected_files', {}).items():
            if file_digest(self.inside(run['workspace'], path, exists=True)) != digest:
                raise ValueError('Protected requirement changed; reconcile context before review or advance')
        return True

    def log_events(self, session_id, after=None, follow=False):
        return self.transport.session_log(session_id, after, follow,
                                          timeout=self.config.get('event_wait_seconds', 15))

    @staticmethod
    def apply_event(current, event):
        """Keep only a cursor and lifecycle facts, never event text/reasoning."""
        sid = current['session_id']
        if event.get('type') == 'log.synced':
            aggregate, seq = event.get('aggregateID'), event.get('seq')
        else:
            aggregate, seq = event.get('durable', {}).get('aggregateID'), event.get('durable', {}).get('seq')
            if event.get('data', {}).get('sessionID') != sid:
                return False
        if aggregate != sid or not isinstance(seq, int) or isinstance(seq, bool):
            return False
        if seq <= current.get('log_cursor', -1):
            return False
        current['log_cursor'] = seq
        kind = event.get('type', '')
        if kind == 'session.inbox.delivered' and event.get('data', {}).get('inboxID') == current['message_id']:
            current['delivered_seq'] = seq
        if kind.startswith('session.execution.'):
            current['last_execution_event'] = kind
            if current.get('delivered_seq') is not None and seq > current['delivered_seq'] and kind.rsplit('.', 1)[-1] in ('succeeded', 'failed', 'interrupted'):
                current['attempt_outcome'] = kind.rsplit('.', 1)[-1]
        return True

    def sync_log(self, current):
        for event in self.log_events(current['session_id'], current.get('log_cursor'), follow=False):
            self.apply_event(current, event)

    def dispatch(self, run_id):
        self.runpath(run_id)
        guard = lock(self.state / 'dispatch-locks' / (run_id + '.lock'), timeout=0.2)
        try:
            guard.__enter__()
        except RuntimeError:
            return self.load(run_id)
        try:
            return self._dispatch(run_id)
        finally:
            guard.__exit__(None, None, None)

    def _dispatch(self, run_id):
        run = self.load(run_id)
        if run['paused'] or run['status'] != 'PREPARED':
            raise ValueError('Dispatch requires PREPARED and an unpaused run')
        self.ensure_service()
        self.workspace(run['workspace'])
        self.check_schema()
        revision = run.get('revision', 0)
        with lock(self.state / 'state.lock'):
            latest = self.load(run_id)
            if latest.get('revision', 0) != revision or latest['paused'] or latest['status'] != 'PREPARED':
                return latest
            if run['attempt_number'] >= run['max_attempts']:
                run['status'] = 'BLOCKED'; self.save(run)
                raise ValueError('Attempt limit reached; diagnose before extending scope')
        stage = run['stages'][run['stage_index']]
        for path in stage['required_files']:
            if not self.inside(run['workspace'], path, exists=True).is_file():
                raise ValueError('Required reference is not a regular file')
        run['attempt_number'] += 1
        task_id = f"{run_id}.{stage['id']}.{run['attempt_number']}"
        directory = self.inside(run['workspace'], '.phase-workflow/' + task_id)
        directory.mkdir(parents=True, exist_ok=True)
        receipt = directory / 'completion.json'
        packet, context_hash = self.context_packet(run, stage, task_id)
        atomic_json(directory / 'context.json', {'context_hash': context_hash, **packet})
        prompt = ('你是本轮实现者。只完成以下批准任务，不自行进入后续阶段。\n'
                      f'工作区：{run["workspace"]}\n任务标识：{task_id}\n阶段目标：{stage["goal"]}\n'
                      '必读文件：\n' + '\n'.join(str(self.inside(run['workspace'], p, exists=True)) for p in stage['required_files']) + '\n'
                      + ('审阅修复意见：' + run['repair_instructions'] + '\n' if run.get('repair_instructions') else '')
                      + '前端职责边界：所有前端设计、实现和修改由 GPT/Codex 使用 Design Dialogue 与 Frontend Design 完成。OpenCode 只做批准的非前端任务。\n'
                      + '不得修改页面、组件、样式、前端交互/状态/API 接入代码或前端测试，不调用 /design、designer 或设计子代理承担前端。\n'
                      + '先读取本轮上下文包和必读依据，遵守允许/禁止修改路径及共享文件归属；依赖接口或前端变更时交回具体阻塞和建议，由 GPT 评估。\n'
                      + '交回准确的实际差异、接口行为、自检证据及已知缺口供 GPT 严格审阅；不得自行放宽验收或通过修改前端使测试通过。\n'
                      + '完成实现和适当自检，说明实际检查及未测内容。保持其他已有改动。不要读取凭据或访问其他项目。\n'
                      + '有能力时执行视觉检查，能力不足时交付可检查的成果，并明确哪些检查未完成\n'
                      + '非前端视觉任务中，OpenCode 侧多模态模型可承担部分视觉评审，GPT 必须参与视觉评审；交回实际截图或渲染结果供复核。\n'
                      + '语言规则：面向用户的普通回复和解释使用中文；运行日志、Shell检查说明、完成回执的summary/checks/evidence、专业审阅和工作流日志使用英文。\n'
                      + '中文需求、代码、路径和测试数据保持原值，不翻译业务数据。Shell日志不要直接print中文样例或中文路径；用ascii(value)、JSON ensure_ascii=True或UTF-8字节hex表达原始数据，日志正文使用英文。\n'
                      + '优先以英文日志规避代码页问题；仍有替换字符或信息丢失时如实记录ENC-01，不宣称修复。\n'
                      + f'最后写入 {receipt}，使用 JSON：'
                      + json.dumps({'task_id': task_id, 'summary': 'Actual results in English', 'changed_files': ['workspace-relative artifact path'],
                                    'checks': [{'name': 'Executed check in English', 'status': 'PASS/FAIL/NOT TESTED', 'evidence': 'English evidence; escape non-ASCII sample data'}]}, ensure_ascii=False)
                      + '\nchanged_files 只列实际成果文件，不列完成回执本身。写回执前确保内容已保存。执行成功不等于验收通过。')
        if run.get('strict_context'):
            prompt += (f'\n本轮上下文包：{directory / "context.json"}。必须先读取并遵守其中的有效需求、约束与上次审阅。'
                    f'\n完成回执必须另含 context_hash={context_hash} 和 context_version={run.get("context_version", 1)}；'
                    'context_checks列出实际落实的关键约束及结果。不得修改protected_files。')
        (directory / 'task.md').write_text(prompt, encoding='utf-8')
        with lock(self.state / 'state.lock'):
            latest = self.load(run_id)
            if latest.get('revision', 0) != revision or latest['paused'] or latest['status'] != 'PREPARED':
                return latest
            run['current'] = {'task_id': task_id, 'receipt': str(receipt), 'message_id': 'msg_' + uuid.uuid4().hex,
                              'directory': str(directory), 'callback': 'NOT_SENT', 'events': [],
                              'admission_state': 'NOT_SENT',
                              'context_packet': packet, 'context_hash': context_hash,
                              'callback_id': str(uuid.uuid5(uuid.NAMESPACE_URL, task_id + '/review'))}
            run.pop('advance_requested', None)
            run['status'] = 'DISPATCHING'; self.save(run)
        try:
            session = self._session(run)
            session_id = session['id']
            run['current']['session_id'] = session_id
            run['current']['model'] = session.get('model')
            run['current']['executor_baseline_time'] = session.get('time', {}).copy()
            from desktop_runtime import open_session_console
            viewer = open_session_console(self.config, run['workspace'], session_id, run.get('session_viewer'))
            if self.config.get('execution_client') == 'cli' and not viewer:
                raise RuntimeError('The visible CLI is required before prompt admission')
            if self.config.get('event_monitoring'):
                # Capture the existing watermark before admission. Old outcomes cannot finish this attempt.
                self.sync_log(run['current'])
                run['current']['attempt_start_seq'] = run['current'].get('log_cursor', -1)
                run['current'].pop('attempt_outcome', None)
                run['current'].pop('delivered_seq', None)
            with lock(self.state / 'state.lock'):
                latest = self.load(run_id)
                if latest.get('current', {}).get('task_id') != task_id:
                    return latest
                latest['current'].update(run['current'])
                if run.get('session_id'):
                    latest['session_id'] = run['session_id']
                if viewer:
                    latest['session_viewer'] = viewer
                latest.pop('prepared_session_id', None)
                registry_path = self.state / 'sessions.json'
                registry = read_json(registry_path) if registry_path.exists() else {}
                registry[session_id] = {'run_id': run_id, 'task_id': task_id, 'workspace': run['workspace']}
                atomic_json(registry_path, registry)
                if latest['paused']:
                    # No admission intent was committed: resume can safely send
                    # this approved task once, reusing the empty session.
                    latest['before_pause'] = 'PREPARED'
                    latest['advance_requested'] = True
                    latest['attempt_number'] -= 1
                    latest['prepared_session_id'] = session_id
                    self.save(latest)
                    return latest
                latest['status'] = 'DISPATCH_UNCERTAIN'
                latest['current']['admission_state'] = 'SENDING'
                self.save(latest)  # Intent and identity precede the remote mutation.
            admission = self.api('session.prompt', {'id': run['current']['message_id'], 'text': prompt,
                                                   'metadata': {'phase_workflow_task': task_id}}, {'sessionID': session_id})
        except Exception:
            with lock(self.state / 'state.lock'):
                latest = self.load(run_id)
                if latest.get('current', {}).get('task_id') == task_id:
                    state = 'EXECUTION_FAILED' if latest['current'].get('admission_state') == 'NOT_SENT' else 'DISPATCH_UNCERTAIN'
                    latest['current']['admission_state'] = 'NOT_SENT' if state == 'EXECUTION_FAILED' else 'UNKNOWN'
                    if latest['paused']:
                        latest['before_pause'] = state
                    else:
                        latest['status'] = state
                    self.save(latest)
            self._pending_interrupt(run_id, task_id)
            raise RuntimeError('Dispatch was not confirmed; inspect the persisted admission state before any retry') from None
        with lock(self.state / 'state.lock'):
            latest = self.load(run_id)
            if latest.get('current', {}).get('task_id') != task_id:
                return latest
            latest['current']['admission_id'] = admission.get('data', {}).get('id')
            latest['current']['admission_state'] = 'SENT'
            if latest['paused']:
                latest['before_pause'] = 'RUNNING'
            elif latest['status'] in ('DISPATCHING', 'DISPATCH_UNCERTAIN'):
                latest['status'] = 'RUNNING'
            self.save(latest)
        self._pending_interrupt(run_id, task_id)
        latest = self.load(run_id)
        return self.start_watcher(latest) if not latest['paused'] else latest

    def _pending_interrupt(self, run_id, task_id):
        run = self.load(run_id)
        current = run.get('current', {})
        if current.get('task_id') != task_id or not current.get('interrupt_pending') or not current.get('session_id'):
            return
        result = 'UNCONFIRMED'
        try:
            self.api('session.interrupt', params={'sessionID': current['session_id'], 'resume': 'false'})
            result = 'REQUESTED'
        except Exception:
            pass
        with lock(self.state / 'state.lock'):
            latest = self.load(run_id)
            if latest.get('current', {}).get('task_id') == task_id:
                latest['current']['interrupt_status'] = result
                latest['current']['interrupt_pending'] = result != 'REQUESTED'
                self.save(latest)

    def start_watcher(self, run):
        if self.config.get('coordinator'):
            from phase_runtime import ensure_coordinator
            ensure_coordinator(self.config_path)
            return self.load(run['run_id'])
        with lock(self.state / 'state.lock'):
            run = self.load(run['run_id'])
            if run['paused'] or run['status'] not in ('RUNNING', 'DISPATCH_UNCERTAIN'):
                return run
            directory = self.state / 'watchers'
            directory.mkdir(exist_ok=True)
            with (directory / (run['run_id'] + '.log')).open('ab') as log:
                process = subprocess.Popen([self.config['python_exe'], '-X', 'utf8', self.config['bridge_script'],
                                            '--config', str(self.config_path), 'watch', run['run_id']],
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           creationflags=HIDDEN | (subprocess.DETACHED_PROCESS if os.name == 'nt' else 0))
            run['current']['watcher_pid'] = process.pid
            self.save(run)
        return run

    def watch(self, run_id):
        # The lock belongs to this monitor only. A duplicate monitor exits without sending work.
        self.runpath(run_id)
        try:
            guard = lock(self.state / 'watchers' / (run_id + '.lock'), timeout=0.2)
            guard.__enter__()
        except RuntimeError:
            return {'ignored': 'monitor already active'}
        try:
            limit = self.config.get('max_watch_seconds', 0)
            deadline = time.monotonic() + limit if limit else float('inf')
            while time.monotonic() < deadline:
                run = self.load(run_id)
                if run['paused'] or run['status'] not in ('RUNNING', 'DISPATCH_UNCERTAIN', 'REVIEW_PENDING', 'EXECUTION_FAILED', 'EVIDENCE_MISSING', 'INTERRUPTED'):
                    return run
                try:
                    run = self.collect(run_id)
                except (RuntimeError, subprocess.TimeoutExpired):
                    # Reconcile after a transient disconnect. Never re-admit a prompt.
                    time.sleep(10)
                    continue
                if run['status'] in ('REVIEW_PENDING', 'EXECUTION_FAILED', 'EVIDENCE_MISSING', 'INTERRUPTED'):
                    old_task = run.get('current', {}).get('task_id')
                    result = self.callback(run_id)
                    # A fast review may already dispatch the next stage while we
                    # still own the run-level guard. Retain ownership in that case.
                    latest = self.load(run_id)
                    if latest.get('current', {}).get('task_id') != old_task and not latest['paused']:
                        continue
                    return result
                if run['status'] not in ('RUNNING', 'DISPATCH_UNCERTAIN'):
                    return run
                if self.config.get('event_monitoring'):
                    try:
                        # Blocking local read; no child CLI and no rapid poll loop while idle.
                        current = run['current']
                        for event in self.log_events(current['session_id'], current.get('log_cursor'), follow=True):
                            if not self.apply_event(current, event):
                                continue
                            with lock(self.state / 'state.lock'):
                                latest = self.load(run_id)
                                if latest['paused'] or latest.get('current', {}).get('task_id') != current['task_id']:
                                    return latest
                                # Pause/review fields belong to persisted state; copy only lifecycle facts.
                                for key in ('log_cursor', 'delivered_seq', 'attempt_outcome', 'last_execution_event'):
                                    if key in current:
                                        latest['current'][key] = current[key]
                                self.save(latest)
                            if current.get('attempt_outcome'):
                                break
                    except RuntimeError:
                        time.sleep(min(10, self.config.get('poll_seconds', 10)))
                else:
                    time.sleep(self.config.get('poll_seconds', 10))
            with lock(self.state / 'state.lock'):
                run = self.load(run_id)
                if not run['paused'] and run['status'] in ('RUNNING', 'DISPATCH_UNCERTAIN'):
                    run['status'] = 'MONITOR_TIMEOUT'
                    self.save(run)
            return run
        finally:
            guard.__exit__(None, None, None)

    def recover(self, run_id):
        with lock(self.state / 'state.lock'):
            run = self.load(run_id)
            if run['status'] == 'MONITOR_TIMEOUT' and not run['paused']:
                run['status'] = 'RUNNING'; self.save(run)
        run = self.collect(run_id)
        if not run['paused'] and run['status'] in ('RUNNING', 'DISPATCH_UNCERTAIN'):
            return self.start_watcher(run)
        return self.callback(run_id)

    def _collection_result(self, run, persist):
        if persist:
            self.save(run)
        return run

    def _collect(self, run, persist=True):
        current = run.get('current')
        if not current or not current.get('session_id'):
            return run
        if self.config.get('event_monitoring'):
            self.sync_log(current)
        session = self.api('session.get', params={'sessionID': current['session_id']})['data']
        current['executor_outcome'] = session.get('outcome')
        current['executor_time'] = session.get('time', {})
        # An old outcome can coexist with new active input. Require this attempt's inbox delivered.
        inbox = self.api('session.inbox.list', params={'sessionID': current['session_id']})
        current['inbox_pending'] = bool(inbox.get('data'))
        if current['inbox_pending'] or session.get('time', {}).get('busy', 0) > session.get('time', {}).get('idle', 0):
            return self._collection_result(run, persist)
        outcome = session.get('outcome')
        if (self.config.get('reuse_session') and not self.config.get('event_monitoring')
                and session.get('time', {}).get('idle', 0) <= current.get('executor_baseline_time', {}).get('idle', 0)):
            # Reused sessions retain the previous outcome until the new execution ends.
            # The native idle timestamp must advance, even if a fast execution was never polled busy.
            return self._collection_result(run, persist)
        if self.config.get('event_monitoring') and current.get('attempt_outcome') != outcome:
            # No terminal event after delivery of this message: the visible outcome is from older work.
            return self._collection_result(run, persist)
        if outcome in ('failed', 'interrupted'):
            if not run['paused']:
                run['status'] = 'EXECUTION_FAILED' if outcome == 'failed' else 'INTERRUPTED'
            return self._collection_result(run, persist)
        if outcome == 'succeeded' and run['status'] in ('RUNNING', 'DISPATCH_UNCERTAIN', 'EXECUTION_FAILED', 'EVIDENCE_MISSING', 'REVIEW_PENDING', 'MONITOR_TIMEOUT'):
            receipt_path = self.inside(run['workspace'], current['receipt'])
            if not receipt_path.is_file():
                run['status'] = 'EVIDENCE_MISSING'
            else:
                try:
                    receipt = read_json(receipt_path)
                except (OSError, ValueError):
                    current['evidence_error'] = 'Completion receipt is unreadable or invalid JSON'
                    run['status'] = 'EVIDENCE_MISSING'
                    return self._collection_result(run, persist)
                if not isinstance(receipt, dict) or receipt.get('task_id') != current['task_id'] or not isinstance(receipt.get('changed_files'), list) or not isinstance(receipt.get('checks'), list):
                    run['status'] = 'EVIDENCE_MISSING'
                else:
                    if run.get('strict_context') and (receipt.get('context_hash') != current.get('context_hash')
                            or receipt.get('context_version') != run.get('context_version', 1)):
                        current['evidence_error'] = 'Completion context does not match this task version'
                        run['status'] = 'EVIDENCE_MISSING'
                        return self._collection_result(run, persist)
                    files = {}
                    try:
                        for path in receipt['changed_files']:
                            if not isinstance(path, str) or not path.strip():
                                raise ValueError('Artifact path must be a nonempty string')
                            p = self.inside(run['workspace'], path, exists=True)
                            if not p.is_file():
                                raise ValueError('Artifact must be a regular file')
                            files[str(p)] = file_digest(p)
                    except (OSError, ValueError, TypeError) as error:
                        # Do not print arbitrary receipt paths or model text.
                        current['evidence_error'] = 'Declared artifact is missing, unreadable or invalid (' + type(error).__name__ + ')'
                        current.pop('fingerprint', None)
                        run['status'] = 'EVIDENCE_MISSING'
                        return self._collection_result(run, persist)
                    current['receipt_data'] = receipt
                    current['receipt_sha256'] = file_digest(receipt_path)
                    current['artifacts'] = files
                    current['fingerprint'] = hashlib.sha256(json.dumps({'receipt': receipt, 'files': files}, sort_keys=True).encode()).hexdigest()
                    run['status'] = 'REVIEW_PENDING' if not run['paused'] else 'PAUSED'
        return self._collection_result(run, persist)

    def collect(self, run_id, task_id=None, callback_id=None, context_hash=None):
        with lock(self.state / 'state.lock'):
            snapshot = self.load(run_id)
            if task_id:
                for item in snapshot['history']:
                    if item['task_id'] == task_id:
                        return {'run_id': run_id, 'status': snapshot['status'],
                                'disposition': 'ALREADY_REVIEWED_NOOP', 'task': item}
            if task_id and task_id != snapshot.get('current', {}).get('task_id'):
                raise ValueError('Unknown or stale task id')
            if snapshot['status'] in ('COMPLETED', 'WAITING_USER', 'BLOCKED'):
                # Reviewed outcomes belong to this run, regardless of later work
                # or interruptions in its reused native session.
                return snapshot
            revision = snapshot.get('revision', 0)
        # HTTP and hashing never hold the global state lock.
        run = snapshot if snapshot['status'] == 'INTERRUPTED' else self._collect(snapshot, persist=False)
        with lock(self.state / 'state.lock'):
            latest = self.load(run_id)
            if latest.get('revision', 0) != revision:
                return latest  # Pause/review/dispatch won; discard this stale collection.
            if callback_id:
                current = run.get('current', {})
                if not task_id or current.get('callback_id') != callback_id:
                    raise ValueError('Reception ACK must match the exact task and callback id')
                if run.get('strict_context') and context_hash != current.get('context_hash'):
                    raise ValueError('Reception ACK requires the exact context fingerprint')
                current['callback'] = 'RECEIVED'
                current.setdefault('received_at', now())
            self.save(run)
            return run

    def reconcile_callback(self, run):
        from codex_transport import CodexRPC, CodexError
        if not self.config.get('codex_queue_rpc'):
            return {'state': 'UNKNOWN'}
        client = CodexRPC(self.config['codex_exe'], mode='stdio')
        try:
            return client.inspect(run['reviewer_thread_id'], run['current']['callback_id'])
        except CodexError:
            return {'state': 'UNKNOWN'}
        finally:
            client.close()

    def try_wake(self, run, queue_id):
        from codex_transport import CodexRPC
        proxy = CodexRPC(self.config['codex_exe'], timeout=5)
        try:
            proxy.start(run['reviewer_thread_id'], queue_id)
            return 'ACCEPTED'
        except RuntimeError:
            return 'HOST_UNAVAILABLE_OR_BUSY'
        finally:
            proxy.close()

    def callback(self, run_id):
        # Reserve under lock; every CLI/RPC operation happens after releasing it.
        with lock(self.state / 'state.lock'):
            run = self.load(run_id)
            if run['paused'] or run['status'] not in ('REVIEW_PENDING', 'EXECUTION_FAILED', 'EVIDENCE_MISSING', 'INTERRUPTED') or not run.get('reviewer_thread_id'):
                return run
            current = run['current']
            current.setdefault('callback_id', str(uuid.uuid5(uuid.NAMESPACE_URL, current['task_id'] + '/review')))
            previous = current['callback']
            if previous in ('RECEIVED', 'REVIEW_OBSERVED'):
                return run
            if previous == 'SENDING' and current.get('delivery_lease_until', 0) > time.time():
                return run
            current['callback'] = 'SENDING'
            current['delivery_lease_until'] = time.time() + 45
            current['next_delivery_check'] = time.time() + 30
            self.save(run)
        changes = {}
        send = previous == 'NOT_SENT'
        if not send:
            delivery = self.reconcile_callback(run)
            changes['delivery_check'] = {'at': now(), **delivery}
            if delivery['state'] == 'RECEIVED':
                changes['callback'] = 'RECEIVED'
            elif delivery['state'] == 'QUEUED':
                changes['callback'] = 'QUEUED_UNVERIFIED'
                if self.config.get('codex_queue_rpc'):
                    changes['wake_request'] = self.try_wake(run, delivery.get('queue_id'))
            elif (delivery['state'] == 'ABSENT' and not delivery.get('history_truncated', True)
                    and current.get('callback_attempts', 0) < 3):
                send = True  # Only confirmed absence permits a bounded resend.
            else:
                changes['callback'] = 'CALLBACK_UNCERTAIN'
        if send:
            request = {'run_id': run_id, 'task_id': current['task_id'],
                       'callback_id': current['callback_id'], 'context_hash': current.get('context_hash')}
            request_path = Path(current['directory']) / 'review-request.json'
            atomic_json(request_path, request)
            message = (f'[phase-workflow 回传] run_id={run_id}，task_id={current["task_id"]}，'
                       f'callback_id={current["callback_id"]}，context_hash={current.get("context_hash")}。'
                       f'在工作区 {run["workspace"]} 调用 workflow_collect，精确参数见 {request_path}，记录实际接收确认。'
                       '当前聊天无MCP工具时，读取该JSON，通过已安装phase_bridge.py的call子命令调用同名工具。'
                       '若返回ALREADY_REVIEWED_NOOP，只确认迟到/重复回传。否则读取本轮context.json及原需求，'
                       '审阅实际成果，写可定位报告并用workflow_record_review记录结论。'
                       f'沿用 {run["mode"]} 模式；执行结束尚未表示验收通过。')
            changes['callback_attempts'] = current.get('callback_attempts', 0) + 1
            try:
                if self.config.get('codex_queue_rpc'):
                    from codex_transport import CodexRPC
                    client = CodexRPC(self.config['codex_exe'], mode='stdio')
                    try:
                        queued = client.enqueue(run['reviewer_thread_id'], current['callback_id'], message)
                        changes['queue_id'] = queued['queuedSubmission']['id']
                        changes['callback'] = 'QUEUED_UNVERIFIED'
                    finally:
                        client.close()
                    # Try only the EXISTING host. Never start a second inference host.
                    changes['wake_request'] = self.try_wake(run, changes['queue_id'])
                else:
                    executable = self.config.get('codex_exe') or shutil.which('codex')
                    proc = subprocess.run([executable, 'queue', '--thread', run['reviewer_thread_id'],
                        '--message', message, '--resume'], capture_output=True, text=True,
                        encoding='utf-8', errors='replace', timeout=25, creationflags=HIDDEN)
                    changes['callback'] = 'QUEUED_UNVERIFIED' if proc.returncode == 0 else 'CALLBACK_UNCERTAIN'
                    changes['callback_exit_code'] = proc.returncode
                    changes['callback_warning'] = bool(re.search(r'warn|fail|error', proc.stderr, re.I))
            except Exception:
                changes['callback'] = 'CALLBACK_UNCERTAIN'
        with lock(self.state / 'state.lock'):
            latest = self.load(run_id)
            if (latest.get('current', {}).get('task_id') == current['task_id'] and
                    latest['current'].get('callback') not in ('RECEIVED', 'REVIEW_OBSERVED')):
                latest['current'].update(changes)
                latest['current']['delivery_lease_until'] = 0
                self.save(latest)
            return latest

    def record_review(self, run_id, verdict, report_path, fingerprint=None, repair_instructions=None,
                      task_id=None, context_hash=None):
        if verdict not in ('PASS', 'FAIL', 'BLOCKED'):
            raise ValueError('Verdict must be PASS, FAIL or BLOCKED')
        advance = False
        with lock(self.state / 'state.lock'):
            run = self.load(run_id)
            paused = run['paused']
            review_status = run.get('before_pause') if paused else run['status']
            if review_status not in ('REVIEW_PENDING', 'EXECUTION_FAILED', 'EVIDENCE_MISSING', 'INTERRUPTED'):
                raise ValueError('Run is not awaiting review')
            current = run.get('current', {})
            if paused and (not task_id or not context_hash):
                raise ValueError('Paused review requires the exact task id and context hash')
            if (task_id and task_id != current.get('task_id')) or (context_hash and context_hash != current.get('context_hash')):
                raise ValueError('Review task or context identity does not match')
            if paused and current.get('fingerprint') and fingerprint != current['fingerprint']:
                raise ValueError('Paused review requires the collected artifact fingerprint')
            if review_status == 'INTERRUPTED' and verdict != 'BLOCKED':
                raise ValueError('Interrupted work requires a BLOCKED review; a fresh user decision is needed before retry')
            identity = self.review_identity(run)
        report = self.inside(run['workspace'], report_path, exists=True)
        if not report.is_file() or not report.stat().st_size:
            raise ValueError('Review needs a nonempty report')
        current = run['current']
        if verdict == 'PASS':
            if review_status != 'REVIEW_PENDING' or not fingerprint or fingerprint != current.get('fingerprint'):
                raise ValueError('PASS requires the collected artifact fingerprint')
            if not current.get('receipt_sha256'):
                raise ValueError('PASS requires the collected completion receipt')
        if verdict == 'PASS' or paused:
            self.check_context(run)
            self.check_review_artifacts(run)
        review = {'verdict': verdict, 'report': str(report), 'report_sha256': file_digest(report), 'at': now()}
        with lock(self.state / 'state.lock'):
            latest = self.load(run_id)
            if self.review_identity(latest) != identity:
                raise ValueError('Run changed during review; collect and review the current version')
            run = latest
            current = run['current']
            if paused:
                saved = {'review': review, 'task_id': current['task_id'], 'context_hash': current.get('context_hash'),
                         'fingerprint': fingerprint, 'repair_instructions': repair_instructions}
                existing = current.get('deferred_review')
                if existing:
                    previous = {**existing, 'review': {k: v for k, v in existing['review'].items() if k != 'at'}}
                    proposed = {**saved, 'review': {k: v for k, v in saved['review'].items() if k != 'at'}}
                    if previous != proposed:
                        raise ValueError('A different paused review is already registered')
                    return run
                current['deferred_review'] = saved
                self.save(run)
                return run
            advance = self.apply_review(run, review, repair_instructions)
            if advance:
                run['advance_requested'] = True
            self.save(run)
        return self.dispatch(run_id) if advance else self.load(run_id)

    def check_review_artifacts(self, run):
        current = run['current']
        if current.get('receipt_sha256'):
            receipt = self.inside(run['workspace'], current['receipt'], exists=True)
            if file_digest(receipt) != current['receipt_sha256']:
                raise ValueError('Completion receipt changed; collect and review it again')
        for path, digest in current.get('artifacts', {}).items():
            if file_digest(self.inside(run['workspace'], path, exists=True)) != digest:
                raise ValueError('Artifact changed since collection; collect/review the new version')

    def apply_review(self, run, review, repair_instructions):
        current = run['current']
        current['review'] = review
        current['callback'] = 'REVIEW_OBSERVED'
        run['history'].append(current.copy())
        verdict = review['verdict']
        run['last_verdict'] = verdict
        run['repair_instructions'] = repair_instructions
        if run['mode'] == 'B':
            run['status'] = 'WAITING_USER'
        elif verdict == 'BLOCKED':
            run['status'] = 'BLOCKED'
        elif verdict == 'PASS':
            return self._advance(run)
        elif repair_instructions and run['attempt_number'] < run['max_attempts']:
            run['status'] = 'PREPARED'
            return True
        else:
            run['status'] = 'BLOCKED'
        return False

    @staticmethod
    def review_identity(run):
        # Health, delivery progress and update timestamps may change while files
        # are checked. Only a change to the reviewed business state invalidates
        # that review; the commit merges into the latest operational state.
        current = run.get('current', {})
        return {'run': {key: run.get(key) for key in ('status', 'paused', 'before_pause', 'workspace', 'mode',
            'model', 'stages', 'stage_index', 'attempt_number', 'max_attempts', 'context',
            'context_version', 'strict_context', 'history')},
            'task': {key: current.get(key) for key in ('task_id', 'session_id', 'message_id',
                'context_hash', 'fingerprint', 'receipt_sha256', 'artifacts', 'executor_outcome',
                'executor_time', 'inbox_pending', 'deferred_review')}}

    @staticmethod
    def _advance(run):
        if run['stage_index'] + 1 >= len(run['stages']):
            run['status'] = 'COMPLETED'
            return False
        run['stage_index'] += 1
        run['attempt_number'] = 0
        run['repair_instructions'] = None
        run['status'] = 'PREPARED'
        return True

    def continue_run(self, run_id, approval):
        if not approval.strip():
            raise ValueError('Provide the actual user instruction approving this next scope')
        with lock(self.state / 'state.lock'):
            run = self.load(run_id)
            if run['status'] != 'WAITING_USER' or run['paused']:
                raise ValueError('Only WAITING_USER can continue')
            run['last_approval'] = {'text': approval, 'at': now()}
            verdict = run['last_verdict']
            if verdict == 'PASS':
                advance = self._advance(run)
            elif verdict == 'FAIL' and run.get('repair_instructions'):
                run['status'] = 'PREPARED'; advance = True
            else:
                raise ValueError('Resolve the blocking prerequisite first')
            self.save(run)
        return self.dispatch(run_id) if advance else self.load(run_id)

    def pause(self, run_id, interrupt=False):
        with lock(self.state / 'state.lock'):
            run = self.load(run_id)
            if run['status'] == 'COMPLETED':
                raise ValueError('Completed runs cannot be resumed')
            if not run['paused']:
                run['before_pause'] = run['status']
            run['paused'] = True; run['status'] = 'PAUSED'
            current = run.get('current', {})
            if interrupt and current:
                current['interrupt_pending'] = True
            self.save(run)
        # While admission is in flight, the dispatch owner issues the interrupt
        # after its response (including an uncertain response), outside the lock.
        if interrupt and current.get('session_id') and current.get('admission_state') != 'SENDING':
            self._pending_interrupt(run_id, current['task_id'])
        return self.load(run_id)

    def resume(self, run_id, approval):
        if not approval.strip():
            raise ValueError('Resume needs an explicit user instruction')
        with lock(self.state / 'state.lock'):
            run = self.load(run_id)
            if not run['paused']:
                raise ValueError('Run is not paused')
            deferred = run.get('current', {}).get('deferred_review')
            identity = self.review_identity(run)
        if deferred:
            try:
                current = run['current']
                if (deferred['task_id'] != current['task_id'] or deferred['context_hash'] != current.get('context_hash')
                        or deferred['fingerprint'] != current.get('fingerprint')):
                    raise ValueError('Paused review identity changed; collect and review again')
                report = self.inside(run['workspace'], deferred['review']['report'], exists=True)
                if file_digest(report) != deferred['review']['report_sha256']:
                    raise ValueError('Review report changed; review again')
                self.check_context(run)
                self.check_review_artifacts(run)
            except ValueError:
                with lock(self.state / 'state.lock'):
                    latest = self.load(run_id)
                    if self.review_identity(latest) == identity:
                        stale = latest['current'].pop('deferred_review')
                        latest['current'].setdefault('stale_deferred_reviews', []).append(stale)
                        self.save(latest)
                raise
        with lock(self.state / 'state.lock'):
            run = self.load(run_id)
            if self.review_identity(run) != identity or not run['paused']:
                raise ValueError('Run changed during resume; inspect the current state')
            run['paused'] = False
            run['status'] = run.pop('before_pause')
            advance = False
            if deferred:
                run['current'].pop('deferred_review')
                advance = self.apply_review(run, deferred['review'], deferred['repair_instructions'])
                if advance:
                    run['advance_requested'] = True
            self.save(run)
        if deferred:
            return self.dispatch(run_id) if advance else self.load(run_id)
        if run['status'] == 'PREPARED' and run.get('advance_requested'):
            return self.dispatch(run_id)
        if run['status'] in ('RUNNING', 'DISPATCH_UNCERTAIN'):
            run = self.collect(run_id)
        if run['status'] in ('RUNNING', 'DISPATCH_UNCERTAIN'):
            return self.start_watcher(run)
        if run['status'] in ('REVIEW_PENDING', 'EXECUTION_FAILED', 'EVIDENCE_MISSING', 'INTERRUPTED'):
            return self.callback(run_id)
        return run


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


S = {'type': 'string'}
RUN = {'run_id': S}
TOOLS = [
    ('workflow_status', 'Read bridge capability and persisted run state; queued is not reviewed.', schema(RUN), 'status', True),
    ('workflow_create_run', 'Bind an authorized workspace, A/B mode and approved stage plan. Does not dispatch.',
     schema({'workspace': S, 'mode': {'enum': ['A', 'B']}, 'stages': {'type': 'array', 'items': schema({'id': S, 'goal': S, 'required_files': {'type': 'array', 'items': S}}, ['id', 'goal', 'required_files'])},
             'reviewer_thread_id': S, 'model': schema({'providerID': S, 'id': S, 'variant': S}, ['providerID', 'id']), 'context': {'type': 'object'}, 'max_attempts': {'type': 'integer', 'minimum': 1, 'maximum': 5}}, ['workspace', 'mode', 'stages']), 'create_run', False),
    ('workflow_prepare_desktop', 'Prepare an empty dedicated visible CLI session and its local service. The historical tool name is retained for compatibility. Does not submit a prompt or call a model.',
     schema({**RUN, 'open_window': {'type': 'boolean'}}, ['run_id']), 'prepare_desktop', False),
    ('workflow_dispatch', 'Execute this run current approved stage once; never retry uncertain dispatch.', schema(RUN, RUN), 'dispatch', False),
    ('workflow_collect', 'Collect exact task artifacts; optional task/callback/context identities record reception ACK. Old reviewed tasks return NOOP.', schema({**RUN, 'task_id': S, 'callback_id': S, 'context_hash': S}, RUN), 'collect', False),
    ('workflow_recover', 'Reconcile and reattach monitoring for an already approved run, without submitting its task again.', schema(RUN, RUN), 'recover', False),
    ('workflow_record_review', 'Record an evidence-based review. While paused, task_id and context_hash are required, plus fingerprint when collected; recording never dispatches. A may advance after explicit resume; B waits.',
     schema({**RUN, 'verdict': {'enum': ['PASS', 'FAIL', 'BLOCKED']}, 'report_path': S, 'fingerprint': S, 'repair_instructions': S, 'task_id': S, 'context_hash': S}, ['run_id', 'verdict', 'report_path']), 'record_review', False),
    ('workflow_continue', 'Only after a new human instruction approving the specific next B-mode scope.', schema({**RUN, 'approval': S}, ['run_id', 'approval']), 'continue_run', False),
    ('workflow_pause', 'Persist a pause and suppress callbacks; optionally interrupt the registered execution.', schema({**RUN, 'interrupt': {'type': 'boolean'}}, ['run_id']), 'pause', False),
    ('workflow_resume', 'Only after an explicit user resume instruction; preserves A/B and reconciles current work.', schema({**RUN, 'approval': S}, ['run_id', 'approval']), 'resume', False),
]


def call(bridge, name, args):
    for tool, _, _, method, _ in TOOLS:
        if tool == name:
            return getattr(bridge, method)(**args)
    raise ValueError('Unknown tool')


def mcp(bridge):
    for line in sys.stdin:
        request = None
        try:
            if len(line) > 2_000_000:
                raise ValueError('Request too large')
            request = json.loads(line)
            if 'id' not in request:
                continue
            method = request['method']
            if method == 'initialize':
                version = request.get('params', {}).get('protocolVersion', '2025-06-18')
                result = {'protocolVersion': version if version in ('2024-11-05', '2025-03-26', '2025-06-18') else '2025-06-18',
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'phase-workflow', 'version': VERSION},
                          'instructions': 'Use only user-authorized roots, stage scope, model and reviewer thread. Execution success is not acceptance. Collect artifacts, review original requirements, then record review. B always waits for a human instruction. Pause suppresses callbacks. Never repeat an uncertain dispatch or callback.'}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [{'name': n, 'description': d, 'inputSchema': s,
                                     'annotations': {'readOnlyHint': ro, 'destructiveHint': False, 'openWorldHint': not ro}} for n, d, s, _, ro in TOOLS]}
            elif method == 'tools/call':
                params = request['params']
                try:
                    output = call(bridge, params['name'], params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(output, ensure_ascii=False)}], 'structuredContent': output}
                except Exception as error:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}, ensure_ascii=False), flush=True)
        except Exception as error:
            print(json.dumps({'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                              'error': {'code': -32602, 'message': str(error)}}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['mcp', 'status', 'check-schema', 'call', 'watch', 'coordinator', 'start-runtime', 'stop-runtime'])
    parser.add_argument('name', nargs='?')
    parser.add_argument('--input')
    args = parser.parse_args()
    bridge = Bridge(args.config)
    if args.command in ('coordinator', 'start-runtime', 'stop-runtime'):
        if sys.stdout is None or sys.stderr is None:
            operational_log = (bridge.state / 'coordinator-startup.log').open('a', encoding='utf-8')
            sys.stdout = operational_log
            sys.stderr = operational_log
        from phase_runtime import coordinator, ensure_coordinator
        if args.command == 'coordinator':
            result = coordinator(args.config)
        elif args.command == 'start-runtime':
            result = ensure_coordinator(args.config)
        else:
            (bridge.state / 'coordinator.stop').write_text(now(), encoding='utf-8')
            result = {'status': 'STOP_REQUESTED'}
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command == 'mcp':
        if bridge.config.get('coordinator'):
            from phase_runtime import ensure_coordinator
            ensure_coordinator(args.config)
        return mcp(bridge)
    if args.command == 'watch':
        result = bridge.watch(args.name)
        # Watch logs contain only operational state, not the complete task or model result.
        result = {'run_id': result.get('run_id'), 'status': result.get('status'), 'ignored': result.get('ignored')}
    elif args.command == 'check-schema':
        result = bridge.check_schema()
    elif args.command == 'call':
        result = call(bridge, args.name, read_json(args.input) if args.input else {})
    else:
        result = bridge.status(args.name)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
