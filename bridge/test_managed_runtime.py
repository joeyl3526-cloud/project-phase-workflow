"""Regressions for scheduler-owned coordinator startup."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from phase_bridge import atomic_json
from phase_runtime import ensure_coordinator


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / 'work/bridge-v3/process-checks'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config = self.root / 'config.json'
        self.settings = {'state_dir': str(self.state), 'python_exe': sys.executable,
            'bridge_script': 'unused', 'startup_task_name': 'PhaseWorkflow-PythonBridge-900001'}
        atomic_json(self.config, self.settings)

    def test_mcp_entry_starts_task_owned_coordinator(self):
        def launch(args, **kwargs):
            self.assertEqual(args, ['schtasks.exe', '/Run', '/TN', self.settings['startup_task_name']])
            atomic_json(self.state / 'coordinator.json', {'pid': 12345, 'state': 'RUNNING',
                'heartbeat_epoch': __import__('time').time()})
            return subprocess.CompletedProcess(args, 0)
        with patch('phase_runtime.subprocess.run', side_effect=launch), \
             patch('phase_runtime.subprocess.Popen') as independent:
            result = ensure_coordinator(self.config)
        self.assertEqual(result['owner'], 'task_scheduler')
        self.assertEqual(result['pid'], 12345)
        independent.assert_not_called()

    def test_failed_task_launch_preserves_single_owner_policy(self):
        with patch('phase_runtime.subprocess.run', return_value=subprocess.CompletedProcess([], 1)), \
             patch('phase_runtime.subprocess.Popen') as independent:
            with self.assertRaises(RuntimeError):
                ensure_coordinator(self.config)
        independent.assert_not_called()

    def test_unowned_task_name_is_rejected(self):
        self.settings['startup_task_name'] = 'OtherApplication'
        atomic_json(self.config, self.settings)
        with patch('phase_runtime.subprocess.run') as launch:
            with self.assertRaises(ValueError):
                ensure_coordinator(self.config)
        launch.assert_not_called()
