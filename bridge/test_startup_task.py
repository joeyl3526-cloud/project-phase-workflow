from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from startup_task import NS, task_xml, add_recovery_trigger


class StartupTests(unittest.TestCase):
    def test_logon_task_uses_current_user_and_least_privilege(self):
        sid = 'S-1-5-21-100-200-300-1001'
        tree = ET.fromstring(task_xml(sid, 'C:/Python314/pythonw.exe',
            'C:/中文 project/phase_bridge.py', 'C:/中文 project/config.json'))
        def text(path):
            return tree.find(path, {'t': NS}).text
        self.assertEqual(text('t:Triggers/t:LogonTrigger/t:UserId'), sid)
        self.assertEqual(text('t:Principals/t:Principal/t:UserId'), sid)
        self.assertEqual(text('t:Principals/t:Principal/t:LogonType'), 'InteractiveToken')
        self.assertEqual(text('t:Principals/t:Principal/t:RunLevel'), 'LeastPrivilege')
        self.assertEqual(text('t:Settings/t:MultipleInstancesPolicy'), 'IgnoreNew')
        self.assertEqual(text('t:Settings/t:ExecutionTimeLimit'), 'PT0S')
        self.assertIn('coordinator', text('t:Actions/t:Exec/t:Arguments'))
        self.assertIn('"C:/中文 project/phase_bridge.py"', text('t:Actions/t:Exec/t:Arguments'))
        self.assertNotIn('dispatch', text('t:Actions/t:Exec/t:Arguments'))

    def test_recovery_timer_is_unbounded_and_idempotent(self):
        tree=ET.fromstring(task_xml('S-1-5-21-1','pythonw','bridge','config'))
        add_recovery_trigger(tree)
        timers=tree.findall('t:Triggers/t:TimeTrigger',{'t':NS})
        self.assertEqual(len(timers),1)
        repetition=timers[0].find('t:Repetition',{'t':NS})
        self.assertEqual(repetition.find('t:Interval',{'t':NS}).text,'PT1M')
        self.assertIsNone(repetition.find('t:Duration',{'t':NS}))

    def test_xml_escapes_paths_and_rejects_invalid_identity(self):
        tree = ET.fromstring(task_xml('S-1-5-21-1', 'C:/a&b/pythonw.exe',
                                    'C:/a&b/phase.py', 'C:/a&b/config.json'))
        self.assertEqual(tree.find('t:Actions/t:Exec/t:Command', {'t': NS}).text, 'C:/a&b/pythonw.exe')
        with self.assertRaises(ValueError):
            task_xml('SYSTEM', 'python', 'bridge', 'config')


if __name__ == '__main__':
    unittest.main()
