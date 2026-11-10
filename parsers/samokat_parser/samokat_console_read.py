"""Read only the tail of an already-authorized parser console; never send input."""
import ctypes
import re
from ctypes import wintypes


class COORD(ctypes.Structure):
    _fields_ = [('X', wintypes.SHORT), ('Y', wintypes.SHORT)]


class RECT(ctypes.Structure):
    _fields_ = [('Left', wintypes.SHORT), ('Top', wintypes.SHORT),
                ('Right', wintypes.SHORT), ('Bottom', wintypes.SHORT)]


class INFO(ctypes.Structure):
    _fields_ = [('size', COORD), ('cursor', COORD), ('attributes', wintypes.WORD),
                ('window', RECT), ('maximum', COORD)]


def read_tail(console_pid, lines=160):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.AttachConsole.argtypes = [wintypes.DWORD]
    api.AttachConsole.restype = wintypes.BOOL
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(INFO)]
    api.ReadConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                                               COORD, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.FreeConsole()
    if not api.AttachConsole(console_pid):
        raise ctypes.WinError(ctypes.get_last_error())
    handle = None
    try:
        handle = api.CreateFileW('CONOUT$', 0x80000000, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        info = INFO()
        if not api.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        first = max(0, info.cursor.Y - lines + 1)
        result = []
        for y in range(first, info.cursor.Y + 1):
            text = ctypes.create_unicode_buffer(info.size.X + 1)
            count = wintypes.DWORD()
            if not api.ReadConsoleOutputCharacterW(handle, text, info.size.X, COORD(0, y), ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            result.append(text.value.rstrip())
        return {'text': '\n'.join(result), 'first_y': first, 'cursor_y': int(info.cursor.Y)}
    finally:
        if handle is not None and handle != ctypes.c_void_p(-1).value:
            api.CloseHandle(handle)
        api.FreeConsole()


def last_failure(snapshot):
    matches = list(re.finditer(r'(?m)^(?:Blocked|[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):', snapshot['text']))
    if not matches:
        return None
    match = matches[-1]
    tail = snapshot['text'][match.start():]
    marker = 'Проход не завершён.'
    if marker not in tail:
        return None
    # Do not include an older error after a later PS prompt or a later run.
    end = tail.index(marker) + len(marker)
    text = tail[:end]
    row = snapshot['first_y'] + snapshot['text'][:match.start()].count('\n')
    return {'text': text, 'row': row, 'confirmed_403': bool(re.match(r'Blocked:\s*HTTP\s+403\b', text, re.I))}


def open_process(pid):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def exit_code(handle):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    value = wintypes.DWORD()
    if not api.GetExitCodeProcess(handle, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.value


def close_process(handle):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle(handle)
