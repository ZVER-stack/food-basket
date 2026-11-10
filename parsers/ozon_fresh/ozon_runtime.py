"""Keep Windows awake only while an authorized collection process is running."""
from contextlib import contextmanager
from functools import wraps
import os

@contextmanager
def system_awake():
    setter=None
    if os.name=='nt':
        import ctypes
        setter=ctypes.windll.kernel32.SetThreadExecutionState
        setter.argtypes=[ctypes.c_uint];setter.restype=ctypes.c_uint
        if not setter(0x80000001):setter=None
    try:
        yield setter is not None
    finally:
        if setter:setter(0x80000000)

def keep_system_awake(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        with system_awake():return function(*args,**kwargs)
    return wrapped
