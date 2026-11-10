"""HTTPS native chat session. Password/cookie are never included in the local cache."""

import httpx


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def origin_url(value):
    url = httpx.URL(value.strip())
    if (url.scheme != 'https' or not url.host or url.userinfo or url.query or url.fragment
            or url.path not in {'', '/'}):
        raise ValueError('请填写 HTTPS 服务地址，不含路径、账号或参数。')
    return str(url).rstrip('/')


class Api:
    def __init__(self, origin, *, transport=None):
        self.origin = origin_url(origin)
        self.http = httpx.Client(base_url=self.origin, transport=transport,
                                 timeout=20, follow_redirects=False)
        self.csrf = None

    def request(self, method, path, *, body=None, key=None, params=None):
        headers = {'Origin': self.origin}
        if self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        if key:
            headers['Idempotency-Key'] = key
        response = self.http.request(method, path, json=body, params=params, headers=headers)
        if not 200 <= response.status_code < 300:
            try:
                code = response.json().get('error', 'HTTP_' + str(response.status_code))
            except ValueError:
                code = 'HTTP_' + str(response.status_code)
            raise ApiError(response.status_code, str(code))
        return response.json()

    def login(self, password, public_key):
        self.csrf = None
        self.http.cookies.clear()
        result = self.request('POST', '/v1/console/session',
            body={'password': password, 'device_public_key': public_key})
        self.csrf = result['csrf_token']
        return self.request('POST', '/v1/console/chat/devices/preview',
                            body={'public_key': public_key})

    def authorize(self, public_key, preview):
        return self.request('POST', '/v1/console/chat/devices/authorize',
            body={'public_key': public_key, **{name: preview[name]
                    for name in ('expected_revision', 'preview_sha256')}}, key='native-enrollment')

    def history(self, *, before=None):
        return self.request('GET', '/v1/console/chat/messages',
                            params={} if before is None else {'before': before})

    def status(self, event_id):
        return self.request('GET', '/v1/console/chat/messages/' + event_id)

    def submit(self, packet):
        return self.request('POST', '/v1/console/chat/messages', body=packet)

    def close(self):
        self.http.close()
