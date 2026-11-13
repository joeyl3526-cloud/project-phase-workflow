import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from phase_bridge import Bridge, atomic_json, lock


class FakeBridge(Bridge):
    def __init__(self, config):
        super().__init__(config)
        self.sessions = {}
        self.dispatch_count = 0
        self.fail_prompt = False

    def check_schema(self):
        return {'compatible': True}

    def start_watcher(self, run):
        return run

    def api(self, operation, data=None, params=None, timeout=30):
        if operation == 'session.create':
            self.dispatch_count += 1
            sid = 'ses_test_' + str(self.dispatch_count)
            session = {'id': sid, 'time': {'created': 1, 'busy': 2}, 'location': data['location']}
            self.sessions[sid] = session
            return {'data': session.copy()}
        if operation == 'session.prompt':
            if self.fail_prompt:
                raise TimeoutError('Unknown admission')
            return {'data': {'id': data['id']}}
        if operation == 'session.get':
            return {'data': self.sessions[params['sessionID']].copy()}
        if operation == 'session.inbox.list':
            return {'data': []}
        if operation == 'session.interrupt':
            self.sessions[params['sessionID']]['outcome'] = 'interrupted'
            self.sessions[params['sessionID']]['time']['idle'] = 3
            return {}
        raise AssertionError(operation)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / 'work/阶段2-n安装/test-runs'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        assert Path(self.temp.name).resolve().is_relative_to(base.resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / '中文 project'
        self.workspace.mkdir()
        (self.workspace / 'requirements.md').write_text('AC-01: sample task', encoding='utf-8')
        self.config = self.root / 'config.json'
        atomic_json(self.config, {'allowed_roots': [str(self.workspace)], 'state_dir': str(self.root / 'state'),
                                  'opencode_exe': 'unused', 'codex_exe': 'unused'})
        self.bridge = FakeBridge(self.config)

    def create(self, mode='A', count=1, reviewer=False):
        stages = [{'id': str(i + 2), 'goal': 'approved task ' + str(i), 'required_files': ['requirements.md']} for i in range(count)]
        return self.bridge.create_run(str(self.workspace), mode, stages,
                                      reviewer_thread_id='11111111-1111-1111-1111-111111111111' if reviewer else None)

    def finish(self, run, receipt=True):
        current = run['current']
        self.bridge.sessions[current['session_id']].update(outcome='succeeded', time={'created': 1, 'busy': 2, 'idle': 3})
        (self.workspace / 'result.txt').write_text('actual artifact', encoding='utf-8')
        if receipt:
            atomic_json(current['receipt'], {'task_id': current['task_id'], 'summary': 'done', 'changed_files': ['result.txt'], 'checks': []})
        return self.bridge.collect(run['run_id'])

    def report(self):
        path = self.workspace / 'review.md'
        path.write_text('AC-01 independently checked against actual artifact.', encoding='utf-8')
        return str(path)

    def test_paths_and_missing_inputs_rejected_before_dispatch(self):
        with self.assertRaises(ValueError):
            self.bridge.create_run(str(self.root), 'A', [{'id': '2', 'goal': 'x', 'required_files': []}])
        with self.assertRaises(FileNotFoundError):
            self.bridge.create_run(str(self.workspace), 'A', [{'id': '2', 'goal': 'x', 'required_files': ['missing.md']}])
        with self.assertRaises(ValueError):
            self.bridge.create_run(str(self.workspace), 'A', [{'id': '2', 'goal': 'x', 'required_files': ['../config.json']}])
        with self.assertRaises(ValueError):
            self.bridge.load('../../config')
        self.assertEqual(self.bridge.dispatch_count, 0)

    def test_duplicate_dispatch_rejected(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        with self.assertRaises(ValueError):
            self.bridge.dispatch(run['run_id'])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_visual_instruction_is_sent_verbatim_on_initial_repair_and_next_stage(self):
        instruction = '有能力时执行视觉检查，能力不足时交付可检查的成果，并明确哪些检查未完成'
        for mode in ('A', 'B'):
            with self.subTest(mode=mode), patch.object(self.bridge, 'api', wraps=self.bridge.api) as api:
                run = self.bridge.dispatch(self.create(mode=mode, count=2)['run_id'])
                tasks = [run['current']['task_id']]
                run = self.finish(run)
                run = self.bridge.record_review(run['run_id'], 'FAIL', self.report(), repair_instructions='Repair AC-01')
                if mode == 'B':
                    run = self.bridge.continue_run(run['run_id'], '批准修复当前阶段')
                tasks.append(run['current']['task_id'])
                run = self.finish(run)
                run = self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
                if mode == 'B':
                    run = self.bridge.continue_run(run['run_id'], '批准下一阶段')
                tasks.append(run['current']['task_id'])
                prompts = [call.args[1] for call in api.call_args_list if call.args[0] == 'session.prompt']
                self.assertEqual(len(prompts), 3)
                for task, payload in zip(tasks, prompts):
                    self.assertEqual(payload['text'].splitlines().count(instruction), 1)
                    self.assertIn('GPT 必须参与视觉评审', payload['text'])
                    self.assertEqual(payload['metadata']['phase_workflow_task'], task)

    def test_success_without_receipt_not_ready_for_acceptance(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']), receipt=False)
        self.assertEqual(run['status'], 'EVIDENCE_MISSING')
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'PASS', self.report(), 'invented')

    def test_malformed_receipt_is_reported_instead_of_crashing_monitor(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        self.bridge.sessions[run['current']['session_id']].update(outcome='succeeded', time={'created': 1, 'idle': 3})
        for content in ['{invalid JSON', '[]']:
            Path(run['current']['receipt']).write_text(content, encoding='utf-8')
            result = self.bridge.collect(run['run_id'])
            self.assertEqual(result['status'], 'EVIDENCE_MISSING')
            self.assertEqual(self.bridge.watch(run['run_id'])['status'], 'EVIDENCE_MISSING')

    def test_stale_artifact_cannot_pass_then_recollects_new_fingerprint(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        old = run['current']['fingerprint']
        (self.workspace / 'result.txt').write_text('changed after review', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'PASS', self.report(), old)
        run = self.bridge.collect(run['run_id'])
        self.assertNotEqual(old, run['current']['fingerprint'])
        self.assertEqual(self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])['status'], 'COMPLETED')

    def test_a_advances_only_approved_stages_and_finishes(self):
        run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
        run = self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
        self.assertEqual(run['stage_index'], 1)
        self.assertEqual(run['status'], 'RUNNING')
        self.assertEqual(self.bridge.dispatch_count, 2)
        run = self.finish(run)
        run = self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
        self.assertEqual(run['status'], 'COMPLETED')
        self.assertEqual(self.bridge.dispatch_count, 2)

    def test_b_waits_even_after_pass_and_preserves_mode(self):
        run = self.finish(self.bridge.dispatch(self.create(mode='B', count=2)['run_id']))
        run = self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
        self.assertEqual(run['status'], 'WAITING_USER')
        self.assertEqual(self.bridge.dispatch_count, 1)
        persisted = Bridge(self.config).status(run['run_id'])
        self.assertEqual(persisted['status'], 'WAITING_USER')
        with self.assertRaises(ValueError):
            self.bridge.continue_run(run['run_id'], '')
        run = self.bridge.continue_run(run['run_id'], '批准阶段 3')
        self.assertEqual(run['mode'], 'B')
        self.assertEqual(self.bridge.dispatch_count, 2)

    def test_b_fail_never_automatically_repairs(self):
        run = self.finish(self.bridge.dispatch(self.create(mode='B')['run_id']))
        run = self.bridge.record_review(run['run_id'], 'FAIL', self.report(), repair_instructions='Repair AC-01')
        self.assertEqual(run['status'], 'WAITING_USER')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_a_fail_is_bounded(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        for index in range(3):
            run = self.finish(run)
            run = self.bridge.record_review(run['run_id'], 'FAIL', self.report(), repair_instructions='Repair AC-01')
        self.assertEqual(run['status'], 'BLOCKED')
        self.assertEqual(self.bridge.dispatch_count, 3)

    def test_unknown_dispatch_is_not_repeated(self):
        run = self.create()
        self.bridge.fail_prompt = True
        with self.assertRaises(RuntimeError):
            self.bridge.dispatch(run['run_id'])
        self.assertEqual(self.bridge.load(run['run_id'])['status'], 'DISPATCH_UNCERTAIN')
        with self.assertRaises(ValueError):
            self.bridge.dispatch(run['run_id'])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_pause_suppresses_monitor_callback(self):
        run = self.bridge.dispatch(self.create(reviewer=True)['run_id'])
        self.bridge.pause(run['run_id'])
        with patch('phase_bridge.subprocess.run') as process:
            self.bridge.watch(run['run_id'])
            self.bridge.callback(run['run_id'])
            process.assert_not_called()
        self.assertEqual(self.bridge.load(run['run_id'])['status'], 'PAUSED')

    def test_old_review_cannot_complete_a_new_attempt(self):
        run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
        old_fingerprint = run['current']['fingerprint']
        self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'PASS', self.report(), old_fingerprint)
        self.assertEqual(self.bridge.dispatch_count, 2)

    def test_queue_exit_zero_does_not_claim_review(self):
        run = self.finish(self.bridge.dispatch(self.create(reviewer=True)['run_id']))
        with patch('phase_bridge.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'queued', '')) as process:
            run = self.bridge.callback(run['run_id'])
            self.assertEqual(run['current']['callback'], 'QUEUED_UNVERIFIED')
            self.assertEqual(run['status'], 'REVIEW_PENDING')
            self.bridge.callback(run['run_id'])
            self.assertEqual(process.call_count, 1)

    def test_interrupted_run_stays_paused_after_late_event(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        run = self.bridge.pause(run['run_id'], interrupt=True)
        self.assertTrue(run['paused'])
        self.assertEqual(run['status'], 'PAUSED')
        run = self.bridge.resume(run['run_id'], '继续检查当前任务状态')
        self.assertEqual(run['status'], 'INTERRUPTED')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_monitor_detects_real_state_transition_without_redispatch(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        def finish_on_wait(_):
            self.finish(run)
        with patch('phase_bridge.time.sleep', side_effect=finish_on_wait):
            result = self.bridge.watch(run['run_id'])
        self.assertEqual(result['status'], 'REVIEW_PENDING')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_paused_recovery_does_not_restart_work(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        self.bridge.pause(run['run_id'])
        result = self.bridge.recover(run['run_id'])
        self.assertEqual(result['status'], 'PAUSED')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_duplicate_monitor_exits_without_poll_or_dispatch(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        with lock(self.bridge.state / 'watchers' / (run['run_id'] + '.lock')):
            self.assertEqual(self.bridge.watch(run['run_id']), {'ignored': 'monitor already active'})
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_receipt_path_outside_workspace_rejected(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        self.bridge.sessions[run['current']['session_id']].update(outcome='succeeded', time={'created': 1, 'busy': 2, 'idle': 3})
        run['current']['receipt'] = str(self.config)
        self.bridge.save(run)
        with self.assertRaises(ValueError):
            self.bridge.collect(run['run_id'])

    def test_receipt_change_invalidates_old_review(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        data = json.loads(Path(run['current']['receipt']).read_text(encoding='utf-8'))
        data['summary'] = 'updated evidence'
        atomic_json(run['current']['receipt'], data)
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
