"""A single background owner for HTTP and SQLite; Tk only consumes immutable snapshots."""

import uuid
from datetime import UTC, datetime

from companion_chat_client.api import ApiError
from companion_delivery_protocol.authority import capture_signing_payload
from companion_delivery_protocol.native_chat import capture


class Engine:
    def __init__(self, api, cache, emit, *, clock=None):
        self.api, self.cache, self.emit = api, cache, emit
        self.key = cache.key()
        self.public_key = self.key.public_key().public_bytes_raw().hex()
        self.authority = cache.get('authority')
        self.connected = False
        self.session_active = False
        self.preview = None
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def snapshot(self, status):
        self.emit('snapshot', {'turns': self.cache.turns(), 'pending': self.cache.pending(),
            'status': status, 'ready': self.authority is not None, 'connected': self.connected,
            'public_key': self.public_key})

    def _ready(self, result):
        self.authority = result['authority']
        self.cache.set('authority', self.authority)
        self.connected = True
        self.session_active = True
        self.preview = None
        self.sync(full=True)

    def login(self, password):
        self.connected = False
        self.session_active = False
        self.preview = self.api.login(password, self.public_key)
        if self.preview['registered']:
            self._ready(self.preview)
        else:
            self.emit('approval', self.preview)

    def approve(self):
        self._ready(self.api.authorize(self.public_key, self.preview))

    def send(self, text):
        if not self.authority:
            raise ValueError('请先登录并登记此设备。')
        if not text.strip() or len(text) > 20000:
            raise ValueError('请输入 1 至 20000 个字符。')
        value = capture(self.public_key, self.authority, uuid.uuid4().hex, text, self.clock())
        value['signature'] = self.key.sign(capture_signing_payload(value)).hex()
        self.cache.enqueue({'capture': value, 'text': text})
        self.emit('queued', None)
        self.snapshot('消息已保存在本机，等待 Core 接收')
        if self.connected:
            self.sync()

    def sync(self, *, full=False):
        if not self.session_active:
            self.snapshot('离线 · 登录后同步；待发送消息保留原身份')
            return
        # Refresh only future captures. Existing queued packets are never re-signed.
        preview = self.api.request('POST', '/v1/console/chat/devices/preview',
                                   body={'public_key': self.public_key})
        if not preview['registered']:
            raise ApiError(403, 'NATIVE_DEVICE_APPROVAL_REQUIRED')
        self.authority = preview['authority']
        self.cache.set('authority', self.authority)
        for item in self.cache.pending():
            if item['error']:
                continue
            try:
                if not item['admitted']:
                    self.api.submit(item['packet'])
                    self.cache.acknowledge(item['event_id'])
                self.cache.merge(self.api.status(item['event_id'])['items'])
            except ApiError as error:
                if error.status in {400, 403, 409, 410, 422}:
                    self.cache.reject(item['event_id'], error.code)
                else:
                    raise
        seen, before = set(), None
        while True:
            page = self.api.history(before=before)
            self.cache.merge(page['items'])
            seen.update(item['event_id'] for item in page['items'])
            before = page['next_before']
            if not full or before is None:
                break
        if full:
            self.cache.reconcile(seen)
        self.connected = True
        self.snapshot('已连接 · 回复由 Core 持久保存')

    def close(self):
        self.api.close()
        self.cache.close()
