from unittest.mock import Mock, patch
from pathlib import Path
import tempfile
import unittest
from codex_transport import CodexRPC, CodexError, resolve_codex_executable


class CodexTransportTests(unittest.TestCase):
    def test_existing_explicit_codex_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'codex.exe';target.touch()
            with patch('codex_transport.subprocess.run') as command:
                self.assertEqual(resolve_codex_executable(str(target)),str(target.resolve()))
            command.assert_not_called()

    def test_missing_path_recovers_current_official_package(self):
        with tempfile.TemporaryDirectory() as folder:
            package=Path(folder)/'OpenAI.Codex_26.1103.7773.0_x64__2p2nqsd0c76g0'
            target=package/'app/resources/codex.exe'
            target.parent.mkdir(parents=True);target.touch()
            with patch('codex_transport.subprocess.run',return_value=Mock(returncode=0,stdout=str(package))):
                self.assertEqual(resolve_codex_executable(str(Path(folder)/'removed.exe')),str(target.resolve()))

    def test_missing_path_does_not_accept_an_unrelated_package(self):
        with patch('codex_transport.subprocess.run',return_value=Mock(returncode=0,stdout='C:/OtherApp')):
            with self.assertRaises(CodexError):
                resolve_codex_executable('C:/missing/codex.exe')

    def test_storage_reader_cannot_start_inference(self):
        client = CodexRPC('unused', mode='stdio')
        with self.assertRaises(ValueError):
            client.request('thread/queue/start', {'threadId': 'own'})
        with self.assertRaises(ValueError):
            client.request('thread/start', {})

    def test_queue_pagination_finds_existing_callback_without_resend(self):
        client = CodexRPC('unused')
        client.request = Mock(side_effect=[{'thread': {'status': {'type': 'active'}}},
            {'data': [{'id': 'other', 'clientUserMessageId': 'other'}], 'nextCursor': 'next'},
            {'data': [{'id': 'own-queue', 'clientUserMessageId': 'callback'}], 'nextCursor': None}])
        result = client.inspect('own-thread', 'callback')
        self.assertEqual(result['state'], 'QUEUED')
        self.assertEqual(result['queue_id'], 'own-queue')
        self.assertEqual(client.request.call_args.args[1]['cursor'], 'next')

    def test_tool_echo_is_not_a_reception_ack(self):
        client = CodexRPC('unused')
        client.request = Mock(side_effect=[{'thread': {}}, {'data': []},
            {'data': [{'item': {'type': 'mcpToolCall', 'content': ['callback']}}], 'nextCursor': 'older'}])
        result = client.inspect('own-thread', 'callback')
        self.assertEqual(result['state'], 'ABSENT')
        self.assertTrue(result['history_truncated'])

    def test_only_user_message_can_confirm_native_receipt(self):
        client = CodexRPC('unused')
        client.request = Mock(side_effect=[{'thread': {}}, {'data': []},
            {'data': [{'item': {'type': 'userMessage', 'content': [{'text': 'callback_id=callback'}]}}]}])
        self.assertEqual(client.inspect('own-thread', 'callback')['state'], 'RECEIVED')


if __name__ == '__main__':
    unittest.main()
