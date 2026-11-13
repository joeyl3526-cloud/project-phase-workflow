"""Small stdlib-only transport for the existing local OpenCode service.

Credentials stay in memory. No shell, proxy, redirect, remote host, or model call
is involved in discovery. Operation paths come from the running service schema.
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
from pathlib import Path
import subprocess
from urllib.parse import quote, urlencode, urlsplit


class TransportError(RuntimeError):
    pass


def loopback_url(value):
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        port = parsed.port or 80
    except (ValueError, AttributeError, TypeError):
        raise TransportError('OpenCode service address is invalid') from None
    if (parsed.scheme != 'http' or not local or parsed.username is not None
            or parsed.password is not None or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise TransportError('Only an explicit local HTTP OpenCode service is supported')
    return host, port


def sse_items(response, max_frame_bytes=8_000_000):
    """Decode Effect HTTP API SSE frames without retaining text/other sessions."""
    data = []
    event = ''
    size = 0
    while True:
        line = response.readline(max_frame_bytes + 1)
        if not line:
            return
        size += len(line)
        if size > max_frame_bytes:
            raise TransportError('OpenCode event exceeds the supported size')
        try:
            value = line.decode('utf-8').rstrip('\r\n')
        except UnicodeDecodeError:
            raise TransportError('OpenCode event encoding is invalid') from None
        if not value:
            if event == 'effect/http-api/stream/failure':
                raise TransportError('OpenCode session event stream reported failure')
            if data:
                try:
                    item = json.loads('\n'.join(data))
                    # Some Effect clients serialize the JSON string once more.
                    if isinstance(item, str):
                        item = json.loads(item)
                except (ValueError, TypeError):
                    raise TransportError('OpenCode event payload is invalid') from None
                if not isinstance(item, dict):
                    raise TransportError('OpenCode event payload must be an object')
                yield item
            data, event, size = [], '', 0
        elif value.startswith('data:'):
            data.append(value[5:].removeprefix(' '))
        elif value.startswith('event:'):
            event = value[6:].strip()


class OpenCodeTransport:
    def __init__(self, config):
        self.config = config
        self._address = None
        self._schema = None
        self._operations = {}

    def invalidate(self):
        """Rediscover on the NEXT request; never retry a mutation implicitly."""
        self._address = None
        self._schema = None
        self._operations = {}

    def address(self):
        if self._address is None:
            value = self.config.get('service_url')
            if not value:
                executable = self.config.get('opencode_exe', '')
                if not Path(executable).is_file():
                    raise TransportError('Set a local service_url or a valid OpenCode executable')
                try:
                    result = subprocess.run([executable, 'service', 'status'], capture_output=True,
                                            text=True, encoding='utf-8', errors='replace', timeout=15,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except (OSError, subprocess.TimeoutExpired):
                    raise TransportError('Cannot discover the local OpenCode service') from None
                if result.returncode:
                    raise TransportError('OpenCode local service is unavailable')
                value = result.stdout.strip()
            self._address = loopback_url(value)
        return self._address

    def _authorization(self):
        file = Path(self.config.get('service_config', Path.home() / '.config/opencode/service.json'))
        try:
            if file.stat().st_size > 64_000:
                raise ValueError()
            password = json.loads(file.read_text(encoding='utf-8-sig')).get('password')
            if not isinstance(password, str) or not password:
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise TransportError('OpenCode local service credential is unavailable; configure OpenCode locally') from None
        return 'Basic ' + base64.b64encode(('opencode:' + password).encode('utf-8')).decode('ascii')

    def _open(self, method, path, body=None, timeout=30, stream=False):
        connection = http.client.HTTPConnection(*self.address(), timeout=timeout)
        headers = {'Authorization': self._authorization(), 'Accept': 'text/event-stream' if stream else 'application/json'}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                # Never return the body, headers, or a URL that may contain a secret.
                raise TransportError(f'OpenCode local HTTP returned {response.status}; inspect OpenCode locally')
            return connection, response
        except (OSError, http.client.HTTPException, TransportError) as error:
            connection.close()
            self.invalidate()
            if isinstance(error, TransportError):
                raise
            raise TransportError('OpenCode local connection failed; reconcile before repeating a mutation') from None

    def _json(self, method, path, data=None, timeout=30):
        connection, response = self._open(method, path, data, timeout)
        try:
            raw = response.read(16_000_001)
            if len(raw) > 16_000_000:
                raise TransportError('OpenCode response exceeds the supported size')
            return json.loads(raw) if raw else {}
        except (ValueError, OSError, http.client.HTTPException):
            self.invalidate()
            raise TransportError('OpenCode response could not be decoded; reconcile before repeating a mutation') from None
        finally:
            response.close()
            connection.close()

    def schema(self, refresh=False):
        if self._schema is None or refresh:
            schema = self._json('GET', '/openapi.json')
            operations = {}
            for path, item in schema.get('paths', {}).items():
                if not path.startswith('/api/'):
                    continue
                for method, entry in item.items():
                    if method in ('get', 'post', 'patch', 'delete', 'put') and isinstance(entry, dict) and entry.get('operationId'):
                        operations[entry['operationId']] = (method.upper(), path, entry)
            self._schema, self._operations = schema, operations
        return self._schema

    def route(self, operation, params):
        self.schema()
        if operation not in self._operations:
            raise TransportError('Required OpenCode operation is unavailable')
        method, path, entry = self._operations[operation]
        params = dict(params or {})
        query = []
        for parameter in entry.get('parameters', []):
            name = parameter.get('name')
            if name not in params:
                if parameter.get('required'):
                    raise ValueError('Missing required OpenCode request parameter: ' + name)
                continue
            value = params.pop(name)
            if parameter.get('in') == 'path':
                path = path.replace('{' + name + '}', quote(str(value), safe=''))
            elif parameter.get('in') == 'query':
                if isinstance(value, dict):
                    query.extend((f'{name}[{child}]', val) for child, val in value.items())
                else:
                    query.append((name, str(value).lower() if isinstance(value, bool) else str(value)))
        if params:
            raise ValueError('Unknown OpenCode request parameter')
        return method, path + ('?' + urlencode(query) if query else '')

    def request(self, operation, data=None, params=None, timeout=30):
        if operation == 'GET /openapi.json':
            return self.schema(refresh=True)
        method, path = self.route(operation, params)
        if operation == 'experimental.session.log':
            raise ValueError('Use session_log for streaming responses')
        return self._json(method, path, data, timeout)

    def session_log(self, session_id, after=None, follow=True, timeout=15):
        params = {'sessionID': session_id, 'follow': follow}
        if after is not None:
            params['after'] = str(after)
        method, path = self.route('experimental.session.log', params)
        connection, response = self._open(method, path, timeout=timeout, stream=True)
        try:
            for item in sse_items(response):
                yield item
        except (OSError, http.client.HTTPException):
            # The owner reconnects from its last durable cursor, without resubmission.
            return
        finally:
            response.close()
            connection.close()
