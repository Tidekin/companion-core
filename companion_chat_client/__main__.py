"""Launch with python -m companion_chat_client --server https://your-core.example."""

import argparse
import tkinter as tk
from pathlib import Path

from companion_chat_client.api import origin_url
from companion_chat_client.ui import Window


def main():
    parser = argparse.ArgumentParser(description='Companion Windows 文本聊天客户端')
    parser.add_argument('--server', required=True, help='Core HTTPS origin')
    parser.add_argument('--data-dir', type=Path, default=Path('.local/native-chat-client'))
    arguments = parser.parse_args()
    try:
        origin = origin_url(arguments.server)
    except (ValueError, TypeError) as error:
        parser.error(str(error))
    root = tk.Tk()
    Window(root, origin, arguments.data_dir.resolve())
    root.mainloop()


if __name__ == '__main__':
    main()
