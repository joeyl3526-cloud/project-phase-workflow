"""Focused development checks for the CLI-only policy; no live services."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

from phase_bridge import Bridge,atomic_json,read_json
from desktop_runtime import ensure_cli_service
from opencode_transport import TransportError
import test_phase_bridge as baseline


class CLIServiceChecks(unittest.TestCase):
    def schema(self):
        return {'paths':{'/api/session':{'post':{'operationId':'session.create'}},
            '/api/session/id':{'get':{'operationId':'session.get'},'post':{'operationId':'session.prompt'}}}}

    def config(self):
        return {'opencode_exe':sys.executable,'service_ready_seconds':0}

    def test_ready_service_is_reused_without_starting_a_process(self):
        transport=Mock();transport.schema.return_value=self.schema()
        with patch('desktop_runtime.subprocess.run') as command,patch('desktop_runtime.subprocess.Popen') as gui:
            result=ensure_cli_service(self.config(),transport)
        self.assertTrue(result['http_ready']);self.assertFalse(result['service_started'])
        command.assert_not_called();gui.assert_not_called()

    def test_unavailable_service_starts_cli_once_then_rechecks_http(self):
        transport=Mock();transport.schema.side_effect=[TransportError('not ready'),self.schema()]
        with patch('desktop_runtime.subprocess.run',return_value=Mock(returncode=0)) as command,patch('desktop_runtime.subprocess.Popen') as gui:
            result=ensure_cli_service(self.config(),transport)
        self.assertTrue(result['service_started'])
        self.assertEqual(command.call_args.args[0],[sys.executable,'service','start'])
        command.assert_called_once();gui.assert_not_called()

    def test_cli_service_inherits_environment_with_utf8_python_io(self):
        transport=Mock();transport.schema.side_effect=[TransportError('not ready'),self.schema()]
        original={'PATH':'C:\\preserved-path','BRIDGE_KEEP':'preserved',
            'PYTHONUTF8':'0','PYTHONIOENCODING':'cp936'}
        with patch.dict('desktop_runtime.os.environ',original,clear=True),patch(
                'desktop_runtime.subprocess.run',return_value=Mock(returncode=0)) as command:
            ensure_cli_service(self.config(),transport)
            environment=command.call_args.kwargs['env']
            self.assertEqual(environment['PYTHONUTF8'],'1')
            self.assertEqual(environment['PYTHONIOENCODING'],'utf-8')
            self.assertEqual(environment['PATH'],original['PATH'])
            self.assertEqual(environment['BRIDGE_KEEP'],original['BRIDGE_KEEP'])
            import os
            self.assertEqual(os.environ['PYTHONUTF8'],'0')

    def test_selected_shell_overrides_inherited_shell_only_for_service(self):
        transport=Mock();transport.schema.side_effect=[TransportError('not ready'),self.schema()]
        config={**self.config(),'cli_shell_exe':sys.executable}
        with patch.dict('desktop_runtime.os.environ',{'SHELL':'old-shell','KEEP':'value'},clear=True),patch(
                'desktop_runtime.subprocess.run',return_value=Mock(returncode=0)) as command:
            ensure_cli_service(config,transport)
            environment=command.call_args.kwargs['env']
            self.assertEqual(environment['SHELL'],Path(sys.executable).resolve().as_posix())
            self.assertEqual(environment['KEEP'],'value')
            import os
            self.assertEqual(os.environ['SHELL'],'old-shell')

    def test_missing_selected_shell_fails_without_starting_service(self):
        transport=Mock();transport.schema.side_effect=TransportError('not ready')
        config={**self.config(),'cli_shell_exe':str(Path(sys.executable).parent/'missing-shell-7a34.exe')}
        with patch('desktop_runtime.subprocess.run') as command:
            with self.assertRaisesRegex(RuntimeError,'configured CLI shell'):
                ensure_cli_service(config,transport)
        command.assert_not_called()

    def test_failed_service_start_is_reported_before_a_prompt(self):
        transport=Mock();transport.schema.side_effect=TransportError('not ready')
        with patch('desktop_runtime.subprocess.run',return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError,'no task was sent'):
                ensure_cli_service(self.config(),transport)

    def test_incompatible_http_schema_does_not_start_another_service(self):
        transport=Mock();transport.schema.return_value={'paths':{}}
        with patch('desktop_runtime.subprocess.run') as command:
            with self.assertRaisesRegex(RuntimeError,'incompatible'):
                ensure_cli_service(self.config(),transport)
        command.assert_not_called()

    def test_cli_profile_never_selects_legacy_desktop_startup(self):
        bridge=object.__new__(Bridge)
        bridge.config={'execution_client':'cli','visible_session_client':'cli','desktop_auto_start':True}
        bridge.transport=Mock()
        with patch('desktop_runtime.ensure_cli_service',return_value={'client':'cli'}) as cli,patch('desktop_runtime.ensure_desktop') as gui:
            self.assertEqual(bridge.ensure_service(),{'client':'cli'})
        cli.assert_called_once();gui.assert_not_called()

    def test_cli_profile_rejects_a_missing_visible_client(self):
        bridge=object.__new__(Bridge);bridge.config={'execution_client':'cli'};bridge.transport=Mock()
        with self.assertRaisesRegex(ValueError,'visible CLI'):
            bridge.ensure_service()

    def test_machine_log_serialization_preserves_escaped_original_data(self):
        base=Path(__file__).resolve().parents[1]/'work/bridge-v3'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            path=Path(folder)/'record.json'
            atomic_json(path,{'summary':'Sample bytes verified','sample':'甲\n乙\n'},ensure_ascii=True)
            self.assertTrue(path.read_text(encoding='utf-8').isascii())
            self.assertEqual(read_json(path)['sample'],'甲\n乙\n')


class CLILanguageChecks(unittest.TestCase):
    setUp=baseline.BridgeTests.setUp

    def test_task_packet_keeps_chinese_context_and_english_log_policy(self):
        policy={'conversation':'zh-CN','logs':'en','non_ascii_log_data':'unicode_escape_or_utf8_hex'}
        self.bridge.config['language_policy']=policy
        self.bridge.config['require_context']=True
        run=self.bridge.create_run(str(self.workspace),'A',[
            {'id':'2','goal':'只完成批准的任务','required_files':['requirements.md']}],
            context={'summary':'保留当前有效中文需求','constraints':['保留中文数据']})
        with patch.object(self.bridge,'api',wraps=self.bridge.api) as calls:
            run=self.bridge.dispatch(run['run_id'])
        packet=run['current']['context_packet']
        self.assertEqual(packet['confirmed_context']['summary'],'保留当前有效中文需求')
        self.assertEqual(packet['language_policy'],policy)
        prompt=next(call.args[1]['text'] for call in calls.call_args_list if call.args[0]=='session.prompt')
        self.assertIn('Actual results in English',prompt)
        self.assertIn('面向用户的普通回复和解释使用中文',prompt)
        self.assertIn('ensure_ascii=True',prompt)
