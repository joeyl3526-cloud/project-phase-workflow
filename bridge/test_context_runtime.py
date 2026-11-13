from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import Mock, patch
import unittest
from phase_bridge import atomic_json, file_digest, read_json
from desktop_runtime import ensure_desktop, open_session_console
from opencode_transport import TransportError
import test_phase_bridge as baseline


class ContextTests(unittest.TestCase):
    setUp = baseline.BridgeTests.setUp
    report = baseline.BridgeTests.report

    def strict(self, count=1):
        self.bridge.config['require_context'] = True
        run = self.bridge.create_run(str(self.workspace), 'A', [
            {'id': str(i+2), 'goal': 'implement only the current approved goal',
             'required_files': ['requirements.md']} for i in range(count)],
            context={'summary': 'confirmed synthetic purpose', 'constraints': ['preserve Chinese'],
                     'protected_files': ['requirements.md']})
        return self.bridge.dispatch(run['run_id'])

    def finish(self, run):
        self.bridge.sessions[run['current']['session_id']].update(outcome='succeeded', time={'idle': 3})
        (self.workspace / 'result.txt').write_text('中文', encoding='utf-8')
        atomic_json(run['current']['receipt'], {'task_id': run['current']['task_id'],
            'context_hash': run['current']['context_hash'], 'context_version': 1,
            'changed_files': ['result.txt'], 'checks': []})
        return self.bridge.collect(run['run_id'])

    def test_context_packet_contains_constraints_and_versioned_references(self):
        run = self.strict()
        packet = read_json(Path(run['current']['directory']) / 'context.json')
        self.assertEqual(packet['context_hash'], run['current']['context_hash'])
        self.assertEqual(packet['confirmed_context']['constraints'], ['preserve Chinese'])
        self.assertEqual(packet['mode'], 'A')
        self.assertEqual(packet['task_id'], run['current']['task_id'])

    def test_wrong_context_receipt_cannot_be_reviewed(self):
        run = self.finish(self.strict())
        receipt = read_json(run['current']['receipt']); receipt['context_hash'] = 'old-context'
        atomic_json(run['current']['receipt'], receipt)
        result = self.bridge.collect(run['run_id'])
        self.assertEqual(result['status'], 'EVIDENCE_MISSING')
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])

    def test_changed_requirement_cannot_advance(self):
        run = self.finish(self.strict(count=2))
        (self.workspace / 'requirements.md').write_text('new user correction', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'requirement changed'):
            self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_reception_ack_is_explicit_and_identity_checked(self):
        run = self.finish(self.strict())
        self.assertEqual(run['current']['callback'], 'NOT_SENT')
        with self.assertRaises(ValueError):
            self.bridge.collect(run['run_id'], run['current']['task_id'], 'wrong', run['current']['context_hash'])
        run = self.bridge.collect(run['run_id'], run['current']['task_id'],
                                 run['current']['callback_id'], run['current']['context_hash'])
        self.assertEqual(run['current']['callback'], 'RECEIVED')
        self.assertEqual(run['status'], 'REVIEW_PENDING')

    def test_final_duplicate_callback_is_noop(self):
        run = self.finish(self.strict())
        current = run['current'].copy()
        self.bridge.record_review(run['run_id'], 'PASS', self.report(), current['fingerprint'])
        with patch.object(self.bridge, 'api', side_effect=AssertionError('No executor read needed')):
            result = self.bridge.collect(run['run_id'], current['task_id'], current['callback_id'], current['context_hash'])
        self.assertEqual(result['disposition'], 'ALREADY_REVIEWED_NOOP')
        self.assertEqual(self.bridge.load(run['run_id'])['current']['callback'], 'REVIEW_OBSERVED')

    def test_pause_during_slow_collection_wins_revision_check(self):
        run = self.strict()
        original = self.bridge.api
        def concurrent(operation, *args, **kwargs):
            if operation == 'session.get':
                self.bridge.pause(run['run_id'])
            return original(operation, *args, **kwargs)
        with patch.object(self.bridge, 'api', side_effect=concurrent):
            result = self.bridge.collect(run['run_id'])
        self.assertTrue(result['paused'])
        self.assertEqual(result['status'], 'PAUSED')

    def test_repair_packet_contains_previous_review_and_exact_correction(self):
        run = self.finish(self.strict())
        run = self.bridge.record_review(run['run_id'], 'FAIL', self.report(),
                                      repair_instructions='preserve duplicate Chinese lines')
        packet = run['current']['context_packet']
        self.assertEqual(packet['repair_instructions'], 'preserve duplicate Chinese lines')
        self.assertEqual(packet['previous_review']['verdict'], 'FAIL')


class DispatchConcurrencyTests(unittest.TestCase):
    setUp = baseline.BridgeTests.setUp
    create = baseline.BridgeTests.create
    finish = baseline.BridgeTests.finish
    report = baseline.BridgeTests.report

    def pause_while_remote_call_is_pending(self, run_id, interrupt=False):
        completed = threading.Event()
        errors = []
        def pause():
            try:
                self.bridge.pause(run_id, interrupt=interrupt)
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                completed.set()
        thread = threading.Thread(target=pause)
        thread.start()
        self.addCleanup(thread.join, 5)
        return completed.wait(0.5), completed, errors

    def test_slow_dispatch_does_not_block_another_run_pause(self):
        first, other = self.create(), self.create()
        original = self.bridge.api
        observed = []
        def remote(operation, *args, **kwargs):
            if operation == 'session.create':
                observed.append(self.pause_while_remote_call_is_pending(other['run_id']))
            return original(operation, *args, **kwargs)
        with patch.object(self.bridge, 'api', side_effect=remote):
            self.bridge.dispatch(first['run_id'])
        self.assertTrue(observed[0][0], 'Unrelated pause waited for the remote service')
        self.assertFalse(observed[0][2])
        self.assertTrue(self.bridge.load(other['run_id'])['paused'])

    def test_pause_before_admission_sends_nothing_then_resume_sends_once(self):
        run = self.create()
        original = self.bridge.api
        observed, admissions = [], []
        def remote(operation, *args, **kwargs):
            if operation == 'session.create':
                observed.append(self.pause_while_remote_call_is_pending(run['run_id']))
            if operation == 'session.prompt':
                admissions.append(operation)
            return original(operation, *args, **kwargs)
        with patch.object(self.bridge, 'api', side_effect=remote):
            result = self.bridge.dispatch(run['run_id'])
            self.assertTrue(observed[0][0])
            self.assertTrue(result['paused'])
            self.assertEqual(admissions, [])
            result = self.bridge.resume(run['run_id'], '继续尚未派发的原批准任务')
        self.assertEqual(result['status'], 'RUNNING')
        self.assertEqual(len(admissions), 1)

    def test_interrupt_during_admission_preserves_pause_and_stops_new_execution(self):
        run = self.create()
        original = self.bridge.api
        observed = []
        def remote(operation, *args, **kwargs):
            if operation == 'session.prompt':
                observed.append(self.pause_while_remote_call_is_pending(run['run_id'], interrupt=True))
            return original(operation, *args, **kwargs)
        with patch.object(self.bridge, 'api', side_effect=remote):
            result = self.bridge.dispatch(run['run_id'])
        self.assertTrue(observed[0][0])
        self.assertTrue(result['paused'])
        self.assertEqual(self.bridge.sessions[result['current']['session_id']]['outcome'], 'interrupted')
        result = self.bridge.resume(run['run_id'], '核对本次中断结果')
        self.assertEqual(result['status'], 'INTERRUPTED')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_slow_review_cannot_commit_after_a_pause(self):
        run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
        report = self.report()
        observed = []
        def digest(path):
            if Path(path) == Path(report):
                observed.append(self.pause_while_remote_call_is_pending(run['run_id']))
            return file_digest(path)
        with patch('phase_bridge.file_digest', side_effect=digest):
            with self.assertRaises(ValueError):
                self.bridge.record_review(run['run_id'], 'PASS', report, run['current']['fingerprint'])
        self.assertTrue(observed[0][0])
        self.assertEqual(self.bridge.load(run['run_id'])['history'], [])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_monitor_or_reception_update_does_not_invalidate_same_review(self):
        run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
        report = self.report()
        def digest(path):
            if Path(path) == Path(report):
                latest = self.bridge.load(run['run_id'])
                latest['monitor'] = {'state': 'HEALTHY', 'observed_at': 'synthetic newer heartbeat'}
                latest['current']['callback'] = 'RECEIVED'
                self.bridge.save(latest)
            return file_digest(path)
        with patch('phase_bridge.file_digest', side_effect=digest):
            result = self.bridge.record_review(run['run_id'], 'PASS', report, run['current']['fingerprint'])
        self.assertEqual(result['status'], 'RUNNING')
        self.assertEqual(len(result['history']), 1)
        self.assertEqual(result['history'][0]['review']['verdict'], 'PASS')

    def test_pause_during_empty_desktop_preparation_remains_effective(self):
        run = self.create()
        original = self.bridge.api
        observed = []
        def remote(operation, *args, **kwargs):
            if operation == 'session.create':
                observed.append(self.pause_while_remote_call_is_pending(run['run_id']))
            return original(operation, *args, **kwargs)
        with patch.object(self.bridge, 'api', side_effect=remote):
            result = self.bridge.prepare_desktop(run['run_id'], open_window=False)
        self.assertTrue(observed[0][0])
        self.assertTrue(result['paused'])
        self.assertIn('prepared_session_id', result)

    def test_owner_exit_before_admission_becomes_reviewable_failure(self):
        from phase_runtime import tick
        run = self.create(reviewer=True)
        run['runtime_managed'] = True
        self.bridge.save(run)
        with patch.object(self.bridge, '_session', side_effect=SystemExit('simulated process exit')):
            with self.assertRaises(SystemExit):
                self.bridge.dispatch(run['run_id'])
        with patch.object(self.bridge, 'callback', side_effect=lambda rid: self.bridge.load(rid)) as callback:
            tick(self.bridge, run['run_id'])
        callback.assert_called_once()
        self.assertEqual(self.bridge.load(run['run_id'])['status'], 'EXECUTION_FAILED')
        self.assertEqual(self.bridge.dispatch_count, 0)
        self.assertEqual(self.bridge.record_review(run['run_id'], 'BLOCKED', self.report())['status'], 'BLOCKED')

    def test_live_dispatch_owner_is_not_mistaken_for_a_crash(self):
        from phase_runtime import tick
        run = self.create()
        run['runtime_managed'] = True
        self.bridge.save(run)
        original = self.bridge._session
        def slow_session(snapshot):
            tick(self.bridge, run['run_id'])
            self.assertEqual(self.bridge.load(run['run_id'])['status'], 'DISPATCHING')
            return original(snapshot)
        with patch.object(self.bridge, '_session', side_effect=slow_session):
            result = self.bridge.dispatch(run['run_id'])
        self.assertEqual(result['status'], 'RUNNING')


class DesktopTests(unittest.TestCase):
    def config(self):
        return {'desktop_exe': sys.executable, 'desktop_ready_seconds': 0.5}

    def schema(self):
        return {'paths': {'/api/session': {'post': {'operationId': 'session.create'}},
                '/api/session/{id}': {'get': {'operationId': 'session.get'},
                                      'post': {'operationId': 'session.prompt'}}}}

    def test_desktop_launch_precedes_http_when_both_are_down(self):
        events = []
        transport = Mock()
        transport.schema.side_effect = lambda **_: events.append('http') or self.schema()
        with patch('desktop_runtime.desktop_running', side_effect=[False, True]), \
             patch('desktop_runtime.subprocess.Popen', side_effect=lambda *a, **kw: events.append('launch')):
            result = ensure_desktop(self.config(), transport)
        self.assertEqual(events, ['launch', 'http'])
        self.assertTrue(result['http_ready'])

    def test_running_desktop_is_reused_without_another_launch(self):
        transport = Mock(); transport.schema.return_value = self.schema()
        with patch('desktop_runtime.desktop_running', return_value=True), patch('desktop_runtime.subprocess.Popen') as process:
            result = ensure_desktop(self.config(), transport)
        process.assert_not_called()
        self.assertFalse(result['launched'])

    def test_unready_http_stops_before_dispatch(self):
        transport = Mock(); transport.schema.side_effect = TransportError('synthetic')
        with patch('desktop_runtime.desktop_running', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'no task was dispatched'):
                ensure_desktop(self.config(), transport, timeout=0)

    def test_existing_visible_client_is_reused_for_same_session(self):
        config = {'visible_session_client':'cli', 'opencode_exe':sys.executable,
                  'python_exe':sys.executable, 'service_url':'http://127.0.0.1:12345'}
        previous = {'session_id':'ses_fixture_1','pid':1234,'created':42,'status':'CLI_VISIBLE',
                    'server':'http://127.0.0.1:12345'}
        with patch('desktop_runtime.console_process', return_value=42):
            self.assertEqual(open_session_console(config, '.', 'ses_fixture_1', previous), previous)

    def test_invalid_native_session_cannot_launch_a_client(self):
        with self.assertRaises(ValueError):
            open_session_console({'visible_session_client':'cli'}, '.', 'invalid session & command')


if __name__ == '__main__':
    unittest.main(verbosity=2)
