"""Native global activation shortcuts; callbacks are delivered to the UI queue."""
import ctypes
from ctypes import wintypes
import os
import threading


class GlobalHotkeys:
    def __init__(self, on_listen, on_show, on_stop, on_error=lambda text: None):
        self.callbacks = {1: on_listen, 2: on_show, 3: on_stop}
        self.on_error = on_error
        self.thread = None
        self.thread_id = 0
        self.stopped = threading.Event()

    def start(self):
        if os.name == "nt":
            self.thread = threading.Thread(target=self._run, name="Jarvis-hotkeys", daemon=True)
            self.thread.start()

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        self.thread_id = kernel32.GetCurrentThreadId()
        message = wintypes.MSG()
        # Create this thread's message queue before publishing registration.
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        registered = []
        try:
            for identifier, vk, label in ((1, 0x20, "Ctrl+Alt+Space"),
                                          (2, 0x4A, "Ctrl+Alt+J"),
                                          (3, 0x58, "Ctrl+Alt+X")):
                if user32.RegisterHotKey(None, identifier, 0x4000 | 0x0001 | 0x0002, vk):
                    registered.append(identifier)
                else:
                    self.on_error("%s занята другой программой. Используйте кнопки в Jarvis." % label)
            while not self.stopped.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == 0x0312 and message.wParam in self.callbacks:
                    self.callbacks[message.wParam]()
        finally:
            for identifier in registered:
                user32.UnregisterHotKey(None, identifier)

    def stop(self):
        self.stopped.set()
        if os.name == "nt" and self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
