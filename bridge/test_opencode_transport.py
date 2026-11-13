import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from opencode_transport import OpenCodeTransport, TransportError, loopback_url, sse_items


class TransportTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / 'work/bridge-v2/transport-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        assert Path(self.temp.name).resolve().is_relative_to(base.resolve())
        self.addCleanup(self.temp.cleanup)
        self.credential = Path(self.temp.name) / 'service.json'
        self.credential.write_text(json.dumps({'password': 'synthetic-secret'}))
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get('Authorization')))
                if self.path == '/openapi.json':
                    body = {'paths': {
                        '/api/session/{sessionID}': {'get': {'operationId': 'session.get', 'parameters': [{'name': 'sessionID', 'in': 'path', 'required': True}]}},
                        '/api/session': {'get': {'operationId': 'session.list', 'parameters': [{'name': 'location', 'in': 'query'}]}},
                        '/api/experimental/session/{sessionID}/log': {'get': {'operationId': 'experimental.session.log', 'parameters': [{'name': 'sessionID', 'in': 'path', 'required': True}, {'name': 'follow', 'in': 'query'}, {'name': 'after', 'in': 'query'}]}}}}
                    raw = json.dumps(body).encode()
                elif self.path.startswith('/api/experimental/'):
                    raw = b'event: message\ndata: {"type":"log.synced","aggregateID":"ses_test","seq":7}\n\n'
                elif self.path.startswith('/api/session/'):
                    raw = b'{"data":{"id":"ses_test"}}'
                else:
                    self.send_response(302)
                    self.send_header('Location', 'https://example.invalid/secret-receiver')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.transport = OpenCodeTransport({'service_url': f'http://127.0.0.1:{self.server.server_port}', 'service_config': str(self.credential)})

    def test_local_requests_authenticate_without_child_process(self):
        with patch('opencode_transport.subprocess.run') as process:
            self.assertEqual(self.transport.request('session.get', params={'sessionID': 'ses_test'})['data']['id'], 'ses_test')
            process.assert_not_called()
        self.assertEqual(len(self.requests), 2)
        expected = 'Basic ' + base64.b64encode(b'opencode:synthetic-secret').decode()
        self.assertTrue(all(auth == expected for _, auth in self.requests))

    def test_deep_object_chinese_space_query_and_path_are_encoded(self):
        _, path = self.transport.route('session.list', {'location': {'directory': 'C:\\中文 project'}})
        self.assertIn('location%5Bdirectory%5D=', path)
        self.assertIn('%E4%B8%AD%E6%96%87+project', path)
        _, path = self.transport.route('session.get', {'sessionID': '../wrong?x=y'})
        self.assertIn('..%2Fwrong%3Fx%3Dy', path)

    def test_stream_reads_exclusive_cursor_and_closes(self):
        items = list(self.transport.session_log('ses_test', after=6, follow=False))
        self.assertEqual(items, [{'type': 'log.synced', 'aggregateID': 'ses_test', 'seq': 7}])
        self.assertIn('after=6', self.requests[-1][0])
        self.assertIn('follow=false', self.requests[-1][0])

    def test_remote_credentials_urls_and_redirects_are_rejected(self):
        for url in ['https://127.0.0.1:1', 'http://api.deepseek.com', 'http://u:p@127.0.0.1', 'http://127.0.0.1/x', 'http://127.0.0.1?token=a']:
            with self.assertRaises(TransportError):
                loopback_url(url)
        with self.assertRaisesRegex(TransportError, '302') as caught:
            self.transport._json('GET', '/redirect')
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    def test_missing_credential_error_never_contains_its_value(self):
        self.credential.write_text('{"password":{"bad":"synthetic-secret"}}')
        with self.assertRaises(TransportError) as caught:
            self.transport.request('session.get', params={'sessionID': 'ses_test'})
        self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_sse_handles_comments_multiple_lines_and_double_encoding(self):
        stream = io.BytesIO(b': ping\r\nevent: message\r\ndata: {"type":\r\ndata: "log.synced"}\r\n\r\ndata: "{\\"type\\":\\"other\\"}"\n\n')
        self.assertEqual([x['type'] for x in sse_items(stream)], ['log.synced', 'other'])

    def test_sse_invalid_or_oversized_frame_fails_without_raw_payload(self):
        for raw in [b'data: synthetic-secret\n\n', b'event: effect/http-api/stream/failure\ndata: {"secret":1}\n\n']:
            with self.assertRaises(TransportError) as caught:
                list(sse_items(io.BytesIO(raw)))
            self.assertNotIn('secret', str(caught.exception))
        with self.assertRaises(TransportError):
            list(sse_items(io.BytesIO(b'data: "abcdefgh"\n\n'), max_frame_bytes=8))


if __name__ == '__main__':
    unittest.main()
