"""Windows desktop UI; every widget is owned by the main Tk thread."""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import httpx

from companion_chat_client.api import Api, ApiError
from companion_chat_client.cache import Cache
from companion_chat_client.engine import Engine

STATES = {'PENDING': '等待处理', 'RUNNING': '正在回复', 'RETRY_WAIT': '等待重试',
          'SUCCEEDED': '已完成', 'FAILED': '处理失败', 'CANCELLED': '已取消'}


def background(origin, directory, commands, events):
    engine = None
    try:
        engine = Engine(Api(origin), Cache(directory, origin),
                        lambda kind, body: events.put((kind, body)))
        engine.snapshot('请登录；设备密钥和本地消息由 Windows 保护')
        while True:
            try:
                command, value = commands.get(timeout=2 if engine.cache.pending() else 15)
            except queue.Empty:
                command, value = 'sync', False
            if command == 'close':
                return
            try:
                if command == 'login':
                    engine.login(value)
                    value = None
                elif command == 'approve':
                    engine.approve()
                elif command == 'send':
                    engine.send(value)
                elif command == 'discard':
                    engine.cache.discard_rejected()
                    engine.snapshot('已移除被拒绝且尚未接受的本地消息')
                elif command == 'sync':
                    engine.sync(full=bool(value))
            except httpx.TransportError:
                engine.connected = False
                engine.snapshot('连接中断 · 保留原消息，后台会继续尝试同步')
            except ApiError as error:
                if error.status in {401, 403}:
                    engine.connected = engine.session_active = False
                engine.snapshot('需要处理：' + error.code + '；可重新登录后同步')
            except (ValueError, OSError) as error:
                events.put(('error', str(error)))
    except Exception:
        events.put(('error', '客户端无法启动。请检查本地数据目录、Windows 账户和依赖。'))
    finally:
        if engine is not None:
            engine.close()


class Window:
    def __init__(self, root, origin, directory):
        self.root, self.origin = root, origin
        self.commands, self.events = queue.Queue(), queue.Queue()
        self.last_view = None
        self.ready = False
        self.public_key = ''
        self.queued_draft = None
        root.title('Companion · 私人聊天')
        root.geometry('960x760')
        root.minsize(720, 600)
        root.configure(bg='#f3f5f3')
        style = ttk.Style(root)
        style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')
        style.configure('TLabel', font=('Microsoft YaHei UI', 10))
        style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(12, 8))
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 20, 'bold'))
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        title = ttk.Frame(frame)
        title.grid(row=0, column=0, sticky='ew', pady=(0, 16))
        ttk.Label(title, text='Companion', style='Title.TLabel').pack(side='left')
        ttk.Label(title, text='私人聊天', foreground='#325e48').pack(side='left', padx=16)
        ttk.Button(title, text='同步全部历史', command=lambda: self.command('sync', True)).pack(
            side='right')
        ttk.Button(title, text='复制设备公钥', command=self.copy_key).pack(side='right', padx=12)
        ttk.Label(frame, text='Core  ' + origin, foreground='#405348').grid(
            row=1, column=0, sticky='w', pady=(0, 12))
        login = ttk.Frame(frame)
        login.grid(row=2, column=0, sticky='ew', pady=(0, 16))
        ttk.Label(login, text='主人凭据').pack(side='left', padx=(0, 12))
        self.password = ttk.Entry(login, show='•', width=32)
        self.password.pack(side='left', fill='x', expand=True)
        self.password.bind('<Return>', self.login)
        ttk.Button(login, text='登录设备', command=self.login).pack(side='left', padx=(12, 0))
        self.chat = scrolledtext.ScrolledText(frame, wrap='word', state='disabled',
            font=('Microsoft YaHei UI', 11), relief='flat', padx=20, pady=16,
            bg='#ffffff', fg='#172d24', spacing3=12, borderwidth=1)
        self.chat.grid(row=3, column=0, sticky='nsew')
        self.chat.tag_configure('owner', foreground='#335e4c',
                                font=('Microsoft YaHei UI', 10, 'bold'))
        self.chat.tag_configure('assistant', foreground='#172d24',
                                font=('Microsoft YaHei UI', 10, 'bold'))
        self.chat.tag_configure('state', foreground='#54645b', font=('Microsoft YaHei UI', 9))
        self.chat.tag_configure('error', foreground='#9c3328', font=('Microsoft YaHei UI', 10))
        self.status = tk.StringVar(value='正在加载设备与本地消息…')
        ttk.Label(frame, textvariable=self.status, wraplength=850).grid(
            row=4, column=0, sticky='w', pady=12)
        self.input = scrolledtext.ScrolledText(frame, height=4, wrap='word',
            font=('Microsoft YaHei UI', 11), relief='solid', borderwidth=1, padx=12, pady=8)
        self.input.grid(row=5, column=0, sticky='ew')
        self.input.bind('<Control-Return>', self.send)
        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, sticky='ew', pady=(12, 0))
        ttk.Label(actions, text='Ctrl + Enter 发送 · Enter 换行', foreground='#54645b').pack(
            side='left')
        ttk.Button(actions, text='移除被拒绝的待发送消息',
                   command=lambda: self.command('discard')).pack(side='left', padx=12)
        self.send_button = ttk.Button(actions, text='发送消息', command=self.send, state='disabled')
        self.send_button.pack(side='right')
        self.password.focus_set()
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.worker = threading.Thread(target=background,
            args=(origin, directory, self.commands, self.events), daemon=True)
        self.worker.start()
        root.after(100, self.drain)

    def command(self, name, value=None):
        self.commands.put((name, value))

    def login(self, event=None):
        value = self.password.get()
        if not value:
            self.status.set('请输入主人凭据；客户端只在本次登录时使用。')
            return 'break'
        self.password.delete(0, 'end')
        self.status.set('正在连接 Core 并检查设备…')
        self.command('login', value)
        return 'break'

    def send(self, event=None):
        if not self.ready:
            self.status.set('请先登录并登记此设备。')
            return 'break'
        text = self.input.get('1.0', 'end-1c')
        if not text.strip() or self.queued_draft is not None:
            return 'break'
        self.queued_draft = text
        self.send_button.configure(state='disabled')
        self.command('send', text)
        return 'break'

    def render(self, view):
        self.ready, self.public_key = view['ready'], view['public_key']
        self.status.set(view['status'])
        self.send_button.configure(state='normal' if view['ready'] else 'disabled')
        data = view['turns'], view['pending']
        if data == self.last_view:
            return
        bottom = self.chat.yview()[1] >= .98
        self.last_view = data
        self.chat.configure(state='normal')
        self.chat.delete('1.0', 'end')
        if not any(data):
            self.chat.insert('end', '和 Companion 开始新的对话\n', 'assistant')
            self.chat.insert('end', '文字与回复保存在自己的 Core，重连后可以恢复。\n', 'state')
        known = set()
        for turn in view['turns'][-200:]:
            known.add(turn['event_id'])
            self.chat.insert('end', '你  ·  ' + turn['created_at'] + '\n', 'owner')
            self.chat.insert('end', ('此消息已删除' if turn['redacted'] else turn['text']) + '\n')
            replies = [reply['text'] for reply in turn['replies'] if reply['text']]
            if replies:
                self.chat.insert('end', 'Companion\n', 'assistant')
                self.chat.insert('end', '\n'.join(replies) + '\n')
            label = STATES.get(turn['state'], turn['state'])
            if turn.get('uncertain'):
                label += ' · 模型结果尚未确认，不会自动重新生成'
            if turn.get('error'):
                label += ' · ' + turn['error']
                if 'UNKNOWN' in turn['error']:
                    label += ' · 结果尚未确认，不会自动重新生成'
            self.chat.insert('end', label + '\n\n', 'state')
        for item in view['pending']:
            if item['event_id'] not in known:
                self.chat.insert('end', '你\n', 'owner')
                self.chat.insert('end', item['packet']['text'] + '\n')
            if item['error']:
                self.chat.insert('end', '提交被拒绝 · ' + item['error'] + '\n\n', 'error')
            elif item['event_id'] not in known:
                self.chat.insert('end', ('Core 已接受' if item['admitted'] else '本机待发送')
                                 + '\n\n', 'state')
        self.chat.configure(state='disabled')
        if bottom:
            self.chat.see('end')

    def drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'snapshot':
                    self.render(value)
                elif kind == 'queued':
                    if self.input.get('1.0', 'end-1c') == self.queued_draft:
                        self.input.delete('1.0', 'end')
                    self.queued_draft = None
                    self.input.focus_set()
                elif kind == 'approval':
                    accepted = messagebox.askyesno('登记此设备',
                        '允许这台设备以主人身份聊天？\n\n登记将更新授权版本，'
                        '旧版本下尚未完成的任务可能失效。\nQQ 设备密钥保持独立。', parent=self.root)
                    if accepted:
                        self.command('approve')
                    else:
                        self.status.set('尚未登记设备，可重新登录后继续。')
                elif kind == 'error':
                    self.queued_draft = None
                    self.send_button.configure(state='normal' if self.ready else 'disabled')
                    self.status.set(value)
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def close(self):
        self.command('close')
        self.root.destroy()

    def copy_key(self):
        if self.public_key:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.public_key)
            self.status.set('设备公钥已复制；可用于核对设备身份。')
