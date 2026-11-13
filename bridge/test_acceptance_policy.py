"""Protect the user's daily-use acceptance decision across regeneration."""
import unittest
from build_v12_acceptance import daily_use_passed


class AcceptancePolicyTests(unittest.TestCase):
    def record(self, count, status='PASS'):
        return {'workspace':'C:/synthetic/0d4d616d', 'status':status,
            'cycles_completed':count, 'elapsed_seconds':30, 'model_calls':0,
            'error_code':'STOP_REQUESTED',
            'cycles':[{'index':i,'status':'PASS'} for i in range(count)]}

    policy = {'minimum_completed_cycles':10,'minimum_elapsed_seconds':0,
        'accepted_workspace':'0d4d616d','accepted_stop_code':'STOP_REQUESTED'}

    def test_ten_completed_cycles_pass_without_four_hour_threshold(self):
        record = self.record(10)
        self.assertTrue(daily_use_passed(record, record, self.policy))

    def test_user_stopped_eleven_cycle_result_is_accepted(self):
        record = self.record(11, 'FAIL_OR_ABORTED')
        self.assertTrue(daily_use_passed(record, record, self.policy))

    def test_incomplete_or_failed_evidence_cannot_pass(self):
        record = self.record(9)
        self.assertFalse(daily_use_passed(record, record, self.policy))
        record = self.record(10)
        self.assertFalse(daily_use_passed(record, None, self.policy))
        record['cycles'][2]['status'] = 'FAIL'
        self.assertFalse(daily_use_passed(record, record, self.policy))
