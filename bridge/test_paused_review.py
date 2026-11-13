import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_phase_bridge as baseline


class PausedReviewTests(unittest.TestCase):
    setUp = baseline.BridgeTests.setUp
    create = baseline.BridgeTests.create
    finish = baseline.BridgeTests.finish
    report = baseline.BridgeTests.report

    def test_paused_review_can_be_recorded_without_dispatch(self):
        run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        report = self.report()
        options = dict(task_id=current['task_id'], context_hash=current['context_hash'],
                       fingerprint=current['fingerprint'], repair_instructions='Fix the synthetic case')
        result = self.bridge.record_review(run['run_id'], 'FAIL', report, **options)
        self.assertTrue(result['paused'])
        self.assertEqual(result['status'], 'PAUSED')
        self.assertEqual(result['current']['deferred_review']['review']['verdict'], 'FAIL')
        self.assertEqual(result['history'], [])
        self.assertEqual(self.bridge.dispatch_count, 1)
        self.bridge.record_review(run['run_id'], 'FAIL', report, **options)
        self.assertEqual(self.bridge.dispatch_count, 1)
        result = self.bridge.resume(run['run_id'], 'Resume the approved repair')
        self.assertEqual(result['status'], 'RUNNING')
        self.assertEqual(self.bridge.dispatch_count, 2)
        self.assertEqual(len(result['history']), 1)

    def test_paused_pass_preserves_a_and_b_mode(self):
        for mode in ('A', 'B'):
            with self.subTest(mode=mode):
                run = self.finish(self.bridge.dispatch(self.create(mode=mode, count=2)['run_id']))
                before = self.bridge.dispatch_count
                self.bridge.pause(run['run_id'])
                current = run['current']
                result = self.bridge.record_review(run['run_id'], 'PASS', self.report(),
                    fingerprint=current['fingerprint'], task_id=current['task_id'], context_hash=current['context_hash'])
                self.assertEqual(result['status'], 'PAUSED')
                self.assertEqual(self.bridge.dispatch_count, before)
                result = self.bridge.resume(run['run_id'], 'Resume within the existing mode')
                self.assertEqual(result['status'], 'RUNNING' if mode == 'A' else 'WAITING_USER')
                self.assertEqual(self.bridge.dispatch_count, before + (mode == 'A'))
                self.assertEqual(result['mode'], mode)

    def test_paused_registration_requires_current_identity(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        for options in ({}, {'task_id':'stale','context_hash':current['context_hash']},
                        {'task_id':current['task_id'],'context_hash':'stale'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.bridge.record_review(run['run_id'], 'FAIL', self.report(), **options)
        self.assertEqual(self.bridge.load(run['run_id'])['status'], 'PAUSED')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_changed_report_or_artifact_remains_paused(self):
        for changed in ('report', 'artifact'):
            with self.subTest(changed=changed):
                run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
                before = self.bridge.dispatch_count
                self.bridge.pause(run['run_id'])
                current = run['current']
                report = self.report()
                self.bridge.record_review(run['run_id'], 'FAIL', report,
                    fingerprint=current['fingerprint'], task_id=current['task_id'],
                    context_hash=current['context_hash'], repair_instructions='Fix it')
                path = report if changed == 'report' else self.workspace / 'result.txt'
                Path(path).write_text('changed after registration', encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.bridge.resume(run['run_id'], 'Resume')
                state = self.bridge.load(run['run_id'])
                self.assertTrue(state['paused'])
                self.assertNotIn('deferred_review', state['current'])
                self.assertEqual(len(state['current']['stale_deferred_reviews']), 1)
                self.assertEqual(self.bridge.dispatch_count, before)

    def test_paused_blocked_and_fail_without_repair_do_not_dispatch(self):
        for mode in ('A', 'B'):
            for verdict in ('BLOCKED', 'FAIL'):
                with self.subTest(mode=mode, verdict=verdict):
                    run = self.finish(self.bridge.dispatch(self.create(mode=mode, count=2)['run_id']))
                    before = self.bridge.dispatch_count
                    self.bridge.pause(run['run_id'])
                    current = run['current']
                    self.bridge.record_review(run['run_id'], verdict, self.report(),
                        fingerprint=current['fingerprint'], task_id=current['task_id'], context_hash=current['context_hash'])
                    result = self.bridge.resume(run['run_id'], 'Resume within the existing mode')
                    self.assertEqual(result['status'], 'BLOCKED' if mode == 'A' else 'WAITING_USER')
                    self.assertEqual(self.bridge.dispatch_count, before)

    def test_paused_interrupted_review_requires_blocked(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        self.bridge.pause(run['run_id'], interrupt=True)
        run = self.bridge.resume(run['run_id'], 'Check the interrupted task')
        self.assertEqual(run['status'], 'INTERRUPTED')
        self.bridge.pause(run['run_id'])
        current = run['current']
        options = dict(task_id=current['task_id'], context_hash=current['context_hash'])
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'FAIL', self.report(), **options)
        self.bridge.record_review(run['run_id'], 'BLOCKED', self.report(), **options)
        result = self.bridge.resume(run['run_id'], 'Resume only to register the block')
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_pause_during_active_review_rejects_stale_commit(self):
        from phase_bridge import file_digest
        run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
        report = self.report()
        paused = []
        def digest(path):
            if Path(path) == Path(report) and not paused:
                paused.append(self.bridge.pause(run['run_id']))
            return file_digest(path)
        with patch('phase_bridge.file_digest', side_effect=digest), self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'PASS', report, run['current']['fingerprint'])
        state = self.bridge.load(run['run_id'])
        self.assertTrue(state['paused'])
        self.assertEqual(state['history'], [])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_registered_review_survives_reload_without_callback_or_dispatch(self):
        run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        self.bridge.record_review(run['run_id'], 'FAIL', self.report(),
            fingerprint=current['fingerprint'], task_id=current['task_id'],
            context_hash=current['context_hash'], repair_instructions='Repair')
        from phase_bridge import Bridge
        reloaded = Bridge(self.config).status(run['run_id'])
        self.assertTrue(reloaded['paused'])
        self.assertEqual(reloaded['current']['deferred_review']['review']['verdict'], 'FAIL')
        with patch('phase_bridge.subprocess.run') as process:
            self.bridge.callback(run['run_id'])
            process.assert_not_called()
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_stale_review_can_be_recollected_after_explicit_resume(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        self.bridge.record_review(run['run_id'], 'PASS', self.report(),
            fingerprint=current['fingerprint'], task_id=current['task_id'], context_hash=current['context_hash'])
        (self.workspace / 'result.txt').write_text('new artifact', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.bridge.resume(run['run_id'], 'Resume')
        state = self.bridge.load(run['run_id'])
        self.assertTrue(state['paused'])
        self.assertEqual(len(state['current']['stale_deferred_reviews']), 1)
        state = self.bridge.resume(run['run_id'], 'Recheck the changed result')
        self.assertEqual(state['status'], 'REVIEW_PENDING')
        state = self.bridge.collect(run['run_id'])
        self.assertNotEqual(state['current']['fingerprint'], current['fingerprint'])
        state = self.bridge.record_review(run['run_id'], 'PASS', self.report(), state['current']['fingerprint'])
        self.assertEqual(state['status'], 'COMPLETED')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_paused_registration_rejects_uncaptured_or_stale_context(self):
        run = self.bridge.dispatch(self.create()['run_id'])
        self.bridge.pause(run['run_id'])
        current = run['current']
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'BLOCKED', self.report(),
                task_id=current['task_id'], context_hash=current['context_hash'])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_deferred_review_never_becomes_completed_during_pause(self):
        from phase_runtime import tick
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        self.bridge.record_review(run['run_id'], 'PASS', self.report(),
            fingerprint=current['fingerprint'], task_id=current['task_id'], context_hash=current['context_hash'])
        run = self.bridge.load(run['run_id'])
        run['runtime_managed'] = True
        self.bridge.save(run)
        with patch.object(self.bridge, 'dispatch', side_effect=AssertionError('unexpected dispatch')):
            tick(self.bridge, run['run_id'])
            self.bridge.callback(run['run_id'])
        result = self.bridge.load(run['run_id'])
        self.assertEqual(result['status'], 'PAUSED')
        self.assertEqual(result['history'], [])

    def test_transient_report_read_error_preserves_registered_review(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        report = self.report()
        self.bridge.record_review(run['run_id'], 'PASS', report,
            fingerprint=current['fingerprint'], task_id=current['task_id'], context_hash=current['context_hash'])
        with patch('phase_bridge.file_digest', side_effect=PermissionError('temporary read denial')):
            with self.assertRaises(PermissionError):
                self.bridge.resume(run['run_id'], 'Resume')
        state = self.bridge.load(run['run_id'])
        self.assertTrue(state['paused'])
        self.assertIn('deferred_review', state['current'])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_tool_entry_accepts_new_paused_identity_fields(self):
        from phase_bridge import TOOLS, call
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        schema = next(entry[2] for entry in TOOLS if entry[0] == 'workflow_record_review')
        self.assertIn('task_id', schema['properties'])
        self.assertIn('context_hash', schema['properties'])
        state = call(self.bridge, 'workflow_record_review', {
            'run_id':run['run_id'], 'verdict':'PASS', 'report_path':self.report(),
            'task_id':current['task_id'], 'context_hash':current['context_hash'], 'fingerprint':current['fingerprint']})
        self.assertEqual(state['status'], 'PAUSED')
        self.assertEqual(state['current']['deferred_review']['review']['verdict'], 'PASS')

    def test_changed_protected_requirement_invalidates_deferred_review(self):
        self.bridge.config['require_context'] = True
        run = self.bridge.create_run(str(self.workspace), 'A', [
            {'id':'2', 'goal':'synthetic', 'required_files':['requirements.md']}],
            context={'summary':'synthetic', 'protected_files':['requirements.md']})
        run = self.bridge.dispatch(run['run_id'])
        current = run['current']
        self.bridge.sessions[current['session_id']].update(outcome='succeeded', time={'created':1,'idle':3})
        from phase_bridge import atomic_json
        atomic_json(current['receipt'], {'task_id':current['task_id'], 'context_hash':current['context_hash'],
            'context_version':1, 'changed_files':[], 'checks':[]})
        run = self.bridge.collect(run['run_id'])
        self.bridge.pause(run['run_id'])
        current = run['current']
        self.bridge.record_review(run['run_id'], 'PASS', self.report(), fingerprint=current['fingerprint'],
            task_id=current['task_id'], context_hash=current['context_hash'])
        (self.workspace / 'requirements.md').write_text('requirement changed', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.bridge.resume(run['run_id'], 'Resume')
        state = self.bridge.load(run['run_id'])
        self.assertTrue(state['paused'])
        self.assertEqual(len(state['current']['stale_deferred_reviews']), 1)
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_fresh_cli_records_and_applies_review_in_isolated_state(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        command = [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('phase_bridge.py')),
                   '--config', str(self.config), 'call']
        payload = self.root / 'arguments.json'
        payload.write_text(json.dumps({'run_id':run['run_id'], 'verdict':'PASS', 'report_path':self.report(),
            'task_id':current['task_id'], 'context_hash':current['context_hash'],
            'fingerprint':current['fingerprint']}), encoding='utf-8')
        result = subprocess.run([*command, 'workflow_record_review', '--input', str(payload)],
                                capture_output=True, text=True, encoding='utf-8', check=True)
        state = json.loads(result.stdout)
        self.assertEqual(state['status'], 'PAUSED')
        self.assertEqual(state['current']['deferred_review']['review']['verdict'], 'PASS')
        payload.write_text(json.dumps({'run_id':run['run_id'], 'approval':'Resume this synthetic run'}), encoding='utf-8')
        result = subprocess.run([*command, 'workflow_resume', '--input', str(payload)],
                                capture_output=True, text=True, encoding='utf-8', check=True)
        state = json.loads(result.stdout)
        self.assertEqual(state['status'], 'COMPLETED')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_fresh_mcp_exposes_paused_review_identity(self):
        from phase_bridge import VERSION
        entry = Path(os.environ.get('PHASE_BRIDGE_TEST_ENTRY', Path(__file__).with_name('phase_bridge.py')))
        messages = [
            {'jsonrpc':'2.0', 'id':1, 'method':'initialize', 'params':{'protocolVersion':'2025-06-18'}},
            {'jsonrpc':'2.0', 'id':2, 'method':'tools/list', 'params':{}},
            {'jsonrpc':'2.0', 'id':3, 'method':'tools/call', 'params':{'name':'workflow_status','arguments':{}}},
        ]
        result = subprocess.run([sys.executable, '-X', 'utf8', str(entry), '--config', str(self.config), 'mcp'],
            input=''.join(json.dumps(message)+'\n' for message in messages), capture_output=True,
            text=True, encoding='utf-8', check=True, timeout=15)
        replies = {item['id']: item for item in map(json.loads, result.stdout.splitlines())}
        self.assertEqual(replies[1]['result']['serverInfo']['version'], VERSION)
        tools = replies[2]['result']['tools']
        schema = next(item['inputSchema'] for item in tools if item['name'] == 'workflow_record_review')
        self.assertIn('task_id', schema['properties'])
        self.assertIn('context_hash', schema['properties'])
        self.assertEqual(replies[3]['result']['structuredContent']['bridge_version'], VERSION)

    def test_late_ack_keeps_review_paused_and_does_not_redispatch(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        self.bridge.record_review(run['run_id'], 'PASS', self.report(), fingerprint=current['fingerprint'],
            task_id=current['task_id'], context_hash=current['context_hash'])
        state = self.bridge.collect(run['run_id'], current['task_id'], current['callback_id'], current['context_hash'])
        self.assertEqual(state['status'], 'PAUSED')
        self.assertEqual(state['current']['callback'], 'RECEIVED')
        self.assertIn('deferred_review', state['current'])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_conflicting_registration_is_rejected(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        self.bridge.pause(run['run_id'])
        current = run['current']
        options = dict(fingerprint=current['fingerprint'], task_id=current['task_id'], context_hash=current['context_hash'])
        report = self.report()
        self.bridge.record_review(run['run_id'], 'FAIL', report, **options)
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'BLOCKED', report, **options)
        self.assertEqual(self.bridge.dispatch_count, 1)


if __name__ == '__main__':
    unittest.main()
