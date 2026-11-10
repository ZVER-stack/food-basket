"""Prevent two address-changing collectors from sharing a Chrome connection."""
from contextlib import contextmanager
from functools import wraps
import hashlib
import msvcrt
from pathlib import Path
from urllib.parse import urlsplit
import ozon_fresh_fast as core

@contextmanager
def profile_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    try:
        if path.stat().st_size==0:
            handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            raise core.ParseError('В этом профиле Chrome уже идёт сбор. Два процесса меняли бы адрес друг другу.') from None
        yield
    finally:
        # Windows releases the byte-range lock when this handle closes.
        handle.close()

def browser_lock_path(cdp,worker_id=None):
    url=urlsplit(cdp)
    host='127.0.0.1' if url.hostname in ('localhost','127.0.0.1') else url.hostname
    key=hashlib.sha256(f'{host}:{url.port}'.encode('utf-8')).hexdigest()[:16]
    suffix='' if worker_id is None else f'_isolated_{int(worker_id)}'
    return Path(__file__).resolve().parent/'work'/f'browser_{key}{suffix}.lock'

def exclusive_browser(function):
    @wraps(function)
    def wrapped(args):
        path=browser_lock_path(args.cdp,getattr(args,'worker_id',0) if getattr(args,'isolated_context',False) else None)
        with profile_lock(path):
            return function(args)
    return wrapped
