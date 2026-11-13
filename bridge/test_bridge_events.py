from pathlib import Path
from unittest.mock import patch
import unittest

from phase_bridge import atomic_json
import test_phase_bridge as baseline


class EventBridge(baseline.FakeBridge):
    def __init__(self, config):
        super().__init__(config)
        self.log = {}
        self.admissions = 0

    def event(self, sid, kind, **data):
        log = self.log.setdefault(sid, [])
        item = {'type': kind, 'durable': {'aggregateID': sid, 'seq': len(log) + 1},
                'data': {'sessionID': sid, **data}}
        log.append(item)
        return item

    def api(self, operation, data=None, params=None, timeout=30):
        result = super().api(operation, data, params, timeout)
        if operation == 'session.create':
            sid = result['data']['id']
            self.sessions[sid]['model'] = data.get('model')
            self.sessions[sid]['title'] = data['title']
            result['data'] = self.sessions[sid].copy()
            self.event(sid, 'session.created')
        if operation == 'session.prompt':
            self.admissions += 1
            # Simulate no execution yet: old outcome/time is still present.
        return result

    def log_events(self, session_id, after=None, follow=False):
        items = [event for event in self.log[session_id] if event['durable']['seq'] > (-1 if after is None else after)]
        return iter(items + [{'type': 'log.synced', 'aggregateID': session_id, 'seq': len(self.log[session_id])}])


class EventTests(unittest.TestCase):
    # Reuse only the test workspace setup/helpers, not the baseline test methods.
    setUp = baseline.BridgeTests.setUp
    create = baseline.BridgeTests.create
    report = baseline.BridgeTests.report

    def setup_event_bridge(self):
        self.bridge = EventBridge(self.config)
        self.bridge.config.update(reuse_session=True, event_monitoring=True)

    def complete(self, run):
        sid = run['current']['session_id']
        self.bridge.event(sid, 'session.inbox.delivered', inboxID=run['current']['message_id'])
        self.bridge.event(sid, 'session.execution.succeeded')
        return baseline.BridgeTests.finish(self, run)

    def test_two_stages_reuse_session_and_old_success_cannot_finish_next_message(self):
        self.setup_event_bridge()
        run = self.bridge.dispatch(self.create(count=2)['run_id'])
        run = self.complete(run)
        run = self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
        self.assertEqual(self.bridge.dispatch_count, 1)
        self.assertEqual(self.bridge.admissions, 2)
        run = self.bridge.collect(run['run_id'])
        self.assertEqual(run['status'], 'RUNNING')
        self.assertNotIn('attempt_outcome', run['current'])
        run = self.complete(run)
        run = self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
        self.assertEqual(run['status'], 'COMPLETED')
        self.assertEqual(len({row['session_id'] for row in run['history']}), 1)

    def test_wrong_message_and_other_session_cannot_complete_attempt(self):
        self.setup_event_bridge()
        run = self.bridge.dispatch(self.create()['run_id'])
        sid = run['current']['session_id']
        self.bridge.event(sid, 'session.inbox.delivered', inboxID='msg_wrong')
        self.bridge.event(sid, 'session.execution.succeeded')
        self.bridge.sessions[sid].update(outcome='succeeded', time={'created': 1, 'idle': 3})
        run = self.bridge.collect(run['run_id'])
        self.assertEqual(run['status'], 'RUNNING')
        current = run['current'].copy()
        self.assertFalse(self.bridge.apply_event(current, {'type': 'session.execution.succeeded', 'durable': {'aggregateID': 'other', 'seq': 999}, 'data': {'sessionID': 'other'}}))
        self.assertEqual(current, run['current'])

    def test_replay_is_idempotent_and_keeps_no_text_or_reasoning(self):
        current = {'session_id': 'ses_a', 'message_id': 'msg_a'}
        event = {'type': 'session.text.ended', 'durable': {'aggregateID': 'ses_a', 'seq': 1}, 'data': {'sessionID': 'ses_a', 'text': 'private synthetic text'}}
        self.assertTrue(EventBridge.apply_event(current, event))
        self.assertFalse(EventBridge.apply_event(current, event))
        self.assertEqual(current, {'session_id': 'ses_a', 'message_id': 'msg_a', 'log_cursor': 1})

    def test_prepare_is_empty_repeatable_and_dispatch_uses_that_session(self):
        self.setup_event_bridge()
        run = self.create()
        with patch('phase_bridge.subprocess.Popen') as process:
            first = self.bridge.prepare_desktop(run['run_id'], open_window=False)
            second = self.bridge.prepare_desktop(run['run_id'], open_window=False)
            process.assert_not_called()
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual(second['status'], 'PREPARED')
        self.assertEqual(self.bridge.admissions, 0)
        run = self.bridge.dispatch(run['run_id'])
        self.assertEqual(run['current']['session_id'], first['session_id'])
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_model_changed_in_desktop_stops_before_admission(self):
        self.setup_event_bridge()
        run = self.create()
        run['model'] = {'providerID': 'deepseek', 'id': 'selected'}
        self.bridge.save(run)
        run = self.bridge.prepare_desktop(run['run_id'], open_window=False)
        self.bridge.sessions[run['session_id']]['model'] = {'providerID': 'other', 'id': 'wrong'}
        with self.assertRaises(RuntimeError):
            self.bridge.dispatch(run['run_id'])
        self.assertEqual(self.bridge.admissions, 0)

    def test_timed_out_monitor_can_recover_without_resubmission(self):
        self.setup_event_bridge()
        run = self.bridge.dispatch(self.create()['run_id'])
        run['status'] = 'MONITOR_TIMEOUT'
        self.bridge.save(run)
        result = self.bridge.recover(run['run_id'])
        self.assertEqual(result['status'], 'RUNNING')
        self.assertEqual(self.bridge.admissions, 1)

    def test_event_watcher_reconnects_and_finishes_without_resubmission(self):
        self.setup_event_bridge()
        run = self.bridge.dispatch(self.create()['run_id'])
        original = self.bridge.log_events
        disconnected = []
        def reconnect(sid, after=None, follow=False):
            if follow and not disconnected:
                disconnected.append(True)
                raise RuntimeError('synthetic disconnect')
            if follow:
                self.complete(self.bridge.load(run['run_id']))
            return original(sid, after, follow)
        with patch.object(self.bridge, 'log_events', side_effect=reconnect), patch('phase_bridge.time.sleep'):
            result = self.bridge.watch(run['run_id'])
        self.assertEqual(result['status'], 'REVIEW_PENDING')
        self.assertEqual(self.bridge.admissions, 1)
        self.assertEqual(self.bridge.dispatch_count, 1)

    def test_event_watcher_pause_before_late_completion_never_calls_back(self):
        self.setup_event_bridge()
        run = self.bridge.dispatch(self.create(reviewer=True)['run_id'])
        original = self.bridge.log_events
        def late(sid, after=None, follow=False):
            if follow:
                self.bridge.pause(run['run_id'])
                self.complete(self.bridge.load(run['run_id']))
            return original(sid, after, follow)
        with patch.object(self.bridge, 'log_events', side_effect=late), patch.object(self.bridge, 'callback') as callback:
            result = self.bridge.watch(run['run_id'])
            callback.assert_not_called()
        self.assertEqual(result['status'], 'PAUSED')

    def test_polling_reused_session_requires_a_new_native_idle_timestamp(self):
        self.setup_event_bridge()
        self.bridge.config['event_monitoring'] = False
        run = self.bridge.dispatch(self.create(count=2)['run_id'])
        run = baseline.BridgeTests.finish(self, run)
        run = self.bridge.record_review(run['run_id'], 'PASS', self.report(), run['current']['fingerprint'])
        self.assertEqual(self.bridge.collect(run['run_id'])['status'], 'RUNNING')
        # A fast completion can occur between polls: no need to have observed busy first.
        self.bridge.sessions[run['current']['session_id']]['time']['idle'] = 4
        (self.workspace / 'result.txt').write_text('stage 3', encoding='utf-8')
        atomic_json(run['current']['receipt'], {'task_id': run['current']['task_id'], 'changed_files': ['result.txt'], 'checks': []})
        run = self.bridge.collect(run['run_id'])
        self.assertEqual(run['status'], 'REVIEW_PENDING')
        self.assertEqual(self.bridge.dispatch_count, 1)


if __name__ == '__main__':
    unittest.main()
