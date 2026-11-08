"""Linux test host: a named native LISTEN frame and failure-triggered thread dump.

The early Wakeup import and forwarding frame can perturb startup or scheduling.
The real CLI, native watcher, operation parameters and lifetime remain unchanged.
"""

import faulthandler
import runpy
import signal
import sys


class NativeWatch:
    def __init__(self, native):
        self.native = native

    def watch(self, *args, **kwargs):
        yield from self.native.notifies(*args, **kwargs)

    def notifies(self, *args, **kwargs):
        yield from self.watch(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.native, name)

    def close(self):
        return self.native.close()


def main():
    signal.signal(signal.SIGUSR1, signal.SIG_DFL)
    faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)
    from companion.worker_wakeup import Wakeup

    original_enter = Wakeup.__enter__

    def enter(self):
        result = original_enter(self)
        self.listener = NativeWatch(self.listener)
        return result

    Wakeup.__enter__ = enter
    sys.argv = ["companion", "worker"]
    runpy.run_module("companion", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
