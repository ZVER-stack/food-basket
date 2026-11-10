"""One-off repair of the proven legacy counter bug, not a general access reset."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import ozon_fresh_fast as core
from ozon_run_lock import profile_lock,browser_lock_path

KNOWN_EPISODE=1792437252.0906527


def correction(folder,state,workers):
    last=state.get('last_failure_unix',0)
    if abs(last-KNOWN_EPISODE)>0.00001:return None
    if state.get('consecutive_failures')!=7:return None
    if state.get('manual_required_since_unix') or state.get('requires_manual_access_check'):
        raise core.ParseError('Ручная проверка доступа не допускает исправление таймера.')
    if state.get('stop_reason')!='HTTP 403; increasing shared cooldown':return None
    if abs(state.get('retry_at_unix',0)-last-1800)>0.01:return None
    matching=[]
    for worker in workers:
        if not worker.get('blocked'):continue
        reason=worker.get('reason','').casefold()
        if any(x in reason for x in ('captcha','капч','challenge','authenticated')):
            raise core.ParseError('Сохранена капча; таймер не изменён.')
        if worker.get('last_status')!=403 or abs(worker.get('blocked_at_unix',0)-last)>1:
            raise core.ParseError('Есть другое ограничение доступа; таймер не изменён.')
        matching.append(worker)
    if not matching:raise core.ParseError('Нет подтверждения конкретной ошибки403.')
    begin=core.datetime.fromtimestamp(last-300,core.MSK).strftime('%Y-%m-%d %H:%M:%S')
    end=core.datetime.fromtimestamp(last,core.MSK).strftime('%Y-%m-%d %H:%M:%S')
    with closing(sqlite3.connect((Path(folder)/'snapshot.sqlite').resolve().as_uri()+'?mode=ro',uri=True)) as db:
        count,first,latest=db.execute('SELECT count(*),min(captured_at),max(captured_at) FROM pages WHERE captured_at>=? AND captured_at<?',(begin,end)).fetchone()
    if count<20 or not first or not latest:return None
    span=(core.datetime.strptime(latest,'%Y-%m-%d %H:%M:%S')-core.datetime.strptime(first,'%Y-%m-%d %H:%M:%S')).total_seconds()
    if span<60:return None
    server_wait=max(float(w.get('retry_after_seconds',0) or 0) for w in matching)
    revised_until=last+max(120,server_wait)
    if revised_until>=state['retry_at_unix']:return None
    after=dict(state,consecutive_failures=1,retry_at_unix=revised_until,
        total_protection_failures=max(7,state.get('total_protection_failures',7)))
    return dict(before=state,after=after,successful_saved_pages=count,healthy_span_seconds=span,
        retry_after_seconds=server_wait,reason='Known legacy counter counted historic failures after a proven healthy period')


def repair(root):
    results=Path(root)/'results';pointer=results/'active_shared_run.json'
    if not pointer.exists():return False
    folder=Path(core.load_json(pointer)['out_dir']).resolve()
    if not folder.is_relative_to(results.resolve()):raise core.ParseError('Active folder outside results')
    path=folder/'backoff_status.json';audit=folder/'legacy_counter_correction.json'
    if not path.exists() or audit.exists():return False
    state=core.load_json(path)
    if abs(state.get('last_failure_unix',0)-KNOWN_EPISODE)>0.00001:return False
    # A running collector must release the profile itself before any repair.
    with profile_lock(browser_lock_path('http://127.0.0.1:9222')):
        state=core.load_json(path)
        workers=[core.load_json(p) for p in (folder/'price_transports').glob('price_worker_*.json')]
        plan=correction(folder,state,workers)
        if not plan:return False
        plan['corrected_at']=core.now()
        core.write_json(audit,plan)
        # Transport blocked flags and manual signals are deliberately untouched.
        core.write_json(path,plan['after'])
        print('Исправлен ошибочный таймер этого сбоя: обычная пауза120секунд от первого403, с учётом Retry-After. История сохранена.',flush=True)
        return True


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:repair(Path(__file__).resolve().parent)
    except (core.ParseError,OSError,ValueError) as exc:
        print('Сбор не запущен: '+str(exc),flush=True)
        raise SystemExit(2)
