"""Acceptance regressions for the five failures in the 1.1 audit."""
from pathlib import Path
import os
import subprocess
import threading
import time
from unittest.mock import patch
import unittest

from phase_bridge import atomic_json, read_json
from opencode_transport import OpenCodeTransport, TransportError
import test_phase_bridge as baseline


class ResilienceTests(unittest.TestCase):
    setUp = baseline.BridgeTests.setUp
    create = baseline.BridgeTests.create
    finish = baseline.BridgeTests.finish
    report = baseline.BridgeTests.report

    @unittest.skipUnless(os.name == 'nt', 'Uses a real Windows exclusive file handle')
    def test_state_read_recovers_after_temporary_windows_share_denial(self):
        import ctypes
        from ctypes import wintypes
        run = self.create()
        path = self.bridge.runpath(run['run_id'])
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        def release():
            time.sleep(0.2)
            kernel.CloseHandle(handle)
        thread = threading.Thread(target=release)
        thread.start()
        try:
            self.assertEqual(read_json(path)['run_id'], run['run_id'])
        finally:
            thread.join(2)

    def test_persistent_state_read_denial_is_bounded_and_reported(self):
        with patch('phase_bridge.Path.read_text', side_effect=PermissionError('synthetic permanent denial')) as read, \
             patch('phase_bridge.time.sleep'):
            with self.assertRaises(PermissionError):
                read_json(self.root / 'state.json')
        self.assertLessEqual(read.call_count, 5)

    def test_invalid_run_id_cannot_create_lifecycle_lock_outside_state(self):
        for name, method in [('dispatch', self.bridge.dispatch), ('watch', self.bridge.watch),
                             ('prepare', lambda rid: self.bridge.prepare_desktop(rid, open_window=False))]:
            with self.subTest(entry=name):
                with self.assertRaises(ValueError):
                    method('../../escaped-' + name)
                self.assertFalse((self.root / ('escaped-' + name + '.lock')).exists())

    def test_missing_declared_artifact_becomes_evidence_error_and_notifies(self):
        run = self.bridge.dispatch(self.create(reviewer=True)['run_id'])
        self.bridge.sessions[run['current']['session_id']].update(outcome='succeeded', time={'idle': 3})
        atomic_json(run['current']['receipt'], {'task_id': run['current']['task_id'],
                    'changed_files': ['missing.txt'], 'checks': []})
        with patch.object(self.bridge, 'callback', side_effect=lambda rid: self.bridge.load(rid)) as cb:
            result = self.bridge.watch(run['run_id'])
        self.assertEqual(result['status'], 'EVIDENCE_MISSING')
        self.assertIn('evidence_error', result['current'])
        cb.assert_called_once()

    def test_uncertain_callback_is_reconciled_before_any_resend(self):
        run = self.finish(self.bridge.dispatch(self.create(reviewer=True)['run_id']))
        with patch('phase_bridge.subprocess.run', side_effect=subprocess.TimeoutExpired('synthetic', 25)) as send:
            self.bridge.callback(run['run_id'])
            with patch.object(self.bridge, 'reconcile_callback', create=True,
                              return_value={'state': 'RECEIVED'}) as reconcile:
                result = self.bridge.recover(run['run_id'])
        reconcile.assert_called_once()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result['current']['callback'], 'RECEIVED')
        self.assertEqual(result['status'], 'REVIEW_PENDING')

    def test_resume_after_paused_completion_notifies_without_redispatch(self):
        run = self.bridge.dispatch(self.create(reviewer=True)['run_id'])
        self.bridge.pause(run['run_id'])
        self.finish(run)
        with patch.object(self.bridge, 'callback', side_effect=lambda rid: self.bridge.load(rid)) as cb:
            result = self.bridge.resume(run['run_id'], '继续本任务')
        self.assertEqual(result['status'], 'REVIEW_PENDING')
        cb.assert_called_once()
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_resume_after_interruption_notifies_without_resubmission(self):
        run = self.bridge.dispatch(self.create(reviewer=True)['run_id'])
        self.bridge.pause(run['run_id'], interrupt=True)
        with patch.object(self.bridge, 'callback', side_effect=lambda rid: self.bridge.load(rid)) as cb:
            result = self.bridge.resume(run['run_id'], '继续核对本次被中断的执行')
        self.assertEqual(result['status'], 'INTERRUPTED')
        cb.assert_called_once()
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_interruption_can_be_reviewed_blocked_without_automatic_retry(self):
        run = self.bridge.dispatch(self.create(reviewer=True)['run_id'])
        self.bridge.api('session.interrupt', params={'sessionID':run['current']['session_id']})
        run = self.bridge.collect(run['run_id'])
        self.assertEqual(run['status'], 'INTERRUPTED')
        with patch('phase_bridge.subprocess.run', return_value=subprocess.CompletedProcess([],0,'queued','')):
            result = self.bridge.recover(run['run_id'])
        self.assertEqual(result['current']['callback'], 'QUEUED_UNVERIFIED')
        with self.assertRaises(ValueError):
            self.bridge.record_review(run['run_id'], 'FAIL', self.report(), repair_instructions='Retry it')
        result = self.bridge.record_review(run['run_id'], 'BLOCKED', self.report())
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_completed_run_cannot_be_reopened_by_later_executor_outcome(self):
        run = self.finish(self.bridge.dispatch(self.create()['run_id']))
        run = self.bridge.record_review(run['run_id'], 'PASS', self.report(),run['current']['fingerprint'])
        self.bridge.sessions[run['current']['session_id']]['outcome']='interrupted'
        before = self.bridge.runpath(run['run_id']).read_bytes()
        with patch.object(self.bridge,'api',side_effect=AssertionError('Completed run must not consult later executor work')):
            result = self.bridge.collect(run['run_id'])
        self.assertEqual(result['status'],'COMPLETED')
        self.assertEqual(self.bridge.runpath(run['run_id']).read_bytes(),before)

    def test_coordinator_reconciles_interruption_until_reviewed(self):
        from phase_runtime import tick
        run = self.bridge.dispatch(self.create(reviewer=True)['run_id'])
        run['runtime_managed'] = True
        self.bridge.save(run)
        self.bridge.api('session.interrupt', params={'sessionID': run['current']['session_id']})
        with patch.object(self.bridge, 'callback', side_effect=lambda rid: self.bridge.load(rid)) as callback:
            tick(self.bridge, run['run_id'])
            tick(self.bridge, run['run_id'])
        self.assertEqual(callback.call_count, 2)
        self.assertTrue((self.bridge.state / 'active-runs' / (run['run_id'] + '.json')).is_file())
        self.bridge.record_review(run['run_id'], 'BLOCKED', self.report())
        self.assertFalse((self.bridge.state / 'active-runs' / (run['run_id'] + '.json')).exists())
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_failed_connection_invalidates_discovered_address(self):
        import sys
        transport = OpenCodeTransport({'opencode_exe': sys.executable})
        responses = [subprocess.CompletedProcess([], 0, 'http://127.0.0.1:41001', ''),
                     subprocess.CompletedProcess([], 0, 'http://127.0.0.1:41002', '')]
        with patch('opencode_transport.subprocess.run', side_effect=responses) as discover, \
             patch.object(transport, '_authorization', return_value='Basic SYNTHETIC'), \
             patch('opencode_transport.http.client.HTTPConnection') as connection:
            self.assertEqual(transport.address()[1], 41001)
            connection.return_value.request.side_effect = ConnectionRefusedError()
            with self.assertRaises(TransportError):
                transport._open('GET', '/synthetic')
            self.assertEqual(transport.address()[1], 41002)
            self.assertEqual(discover.call_count, 2)

    def test_old_monitor_keeps_next_stage_if_new_monitor_loses_lock(self):
        run = self.finish(self.bridge.dispatch(self.create(count=2)['run_id']))
        attempts = []
        report = self.report()

        def new_monitor(next_run):
            attempts.append(self.bridge.watch(next_run['run_id']))
            return next_run

        def review(rid):
            current = self.bridge.collect(rid)
            result = self.bridge.record_review(rid, 'PASS', report, current['current']['fingerprint'])
            if result['status'] == 'RUNNING':
                result = self.finish(result)
            return result

        with patch.object(self.bridge, 'start_watcher', side_effect=new_monitor), \
             patch.object(self.bridge, 'callback', side_effect=review):
            result = self.bridge.watch(run['run_id'])
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(len(self.bridge.load(run['run_id'])['history']), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
