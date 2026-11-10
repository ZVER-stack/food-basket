"""Local console notifications for an already-running collector; no site requests."""
from pathlib import Path
import sys
import time
import ozon_fresh_fast as core
from ozon_status import process_alive

TERMINAL={'complete','finished_with_gaps','stopped','deadline_finished','process_not_running'}


def read_watch_state(root):
    results=Path(root)/'results';pointer=results/'active_shared_run.json'
    if not pointer.exists():return dict(status='not_started',folder='')
    folder=Path(core.load_json(pointer)['out_dir']).resolve()
    if not folder.is_relative_to(results.resolve()):raise core.ParseError('Active run outside results')
    def read(name):
        p=folder/name
        return core.load_json(p) if p.exists() else {}
    state=read('run_status.json');backoff=read('backoff_status.json');progress=read('progress.json')
    status=state.get('status','unknown');since=backoff.get('manual_required_since_unix',0)
    if status in ('running','waiting_backoff','waiting_manual_access'):
        if process_alive(state.get('pid')) is False:status='process_not_running'
        elif since and read('manual_access_restored.json').get('confirmed_at_unix',0)<=since:
            status='waiting_manual_access'
    return dict(status=status,folder=str(folder),manual_since=since,
        retry_at=backoff.get('retry_at_unix',0),zone=progress.get('zone_number'),
        products=progress.get('unique_products_current_zone'),
        updated_at=progress.get('updated_at_msk'),reason=state.get('stop_reason'))


class Monitor:
    def __init__(self):self.previous=None;self.alerted=set()
    def observe(self,data):
        status=data['status'];key=(data['folder'],status,
            data.get('manual_since',0) if status=='waiting_manual_access' else 0)
        changed=key!=self.previous;self.previous=key
        alert=status in TERMINAL|{'waiting_manual_access'} and key not in self.alerted
        if alert:self.alerted.add(key)
        return changed,alert


def message(data):
    status=data['status']
    if status=='waiting_manual_access':
        reason=(data.get('reason') or '').casefold()
        prefix='КАПЧА. ' if ('captcha' in reason or reason.startswith('captcha')) else 'НУЖНА РУЧНАЯ ПРОВЕРКА ДОСТУПА. '
        return (prefix+'Запросы приостановлены. '
            'В прежнем Chrome откройте товар. Если показана капча, пройдите её. Когда товар и цены видны, '
            'затем в другом свободном PowerShell из папки парсера выполните .\\06_resume_after_captcha.cmd')
    if status=='waiting_backoff':
        until=core.datetime.fromtimestamp(data['retry_at'],core.MSK).strftime('%H:%M:%S МСК')
        return 'Ожидание доступа до '+until+'. Если капча не запрошена, действий не требуется.'
    return {'not_started':'Жду запуска основного сбора файлом08.',
        'running':'Сбор работает.',
        'complete':'ГОТОВО: полный сбор завершён. Итог: .\\12_status.cmd',
        'finished_with_gaps':'Все адреса обработаны, есть пропуски. Итог: .\\12_status.cmd',
        'stopped':'Сбор остановлен. Причина: '+str(data.get('reason') or 'см. файл12'),
        'deadline_finished':'Истёк бюджет времени. Результат частичный: .\\12_status.cmd',
        'process_not_running':'Процесс сборщика не работает. Проверьте основное окно и .\\12_status.cmd'
    }.get(status,'Состояние: '+status)


def sound_alert():
    try:
        import winsound
        winsound.Beep(1000,350)
        winsound.Beep(750,350)
    except (ImportError,OSError,RuntimeError):
        print('\a',end='',flush=True)


def watch(root):
    monitor=Monitor();last_progress=0;last_error=None
    print('Монитор капчи и завершения: проверка локальных файлов каждые5секунд.\n'
        'Оставьте это окно открытым. Ctrl+C закрывает только монитор. Сборщик продолжит работать.',flush=True)
    try:
        while True:
            try:
                data=read_watch_state(root);last_error=None
                changed,alert=monitor.observe(data)
                if changed:print('\n'+core.now()+' | '+message(data),flush=True)
                if alert:sound_alert()
                if data['status'] in TERMINAL:
                    print('Папка результата: '+data['folder'],flush=True)
                    return 0
                if data['status']=='running' and time.monotonic()-last_progress>=30:
                    print(f"Адрес {data['zone']}/197; товаров в текущем каталоге: {data['products']}; прогресс обновлён: {data['updated_at']}",flush=True)
                    last_progress=time.monotonic()
            except (OSError,ValueError,core.ParseError) as exc:
                if str(exc)!=last_error:print('Не удалось прочитать состояние: '+str(exc),flush=True)
                last_error=str(exc)
            time.sleep(5)
    except KeyboardInterrupt:
        print('\nМонитор закрыт; основной сбор не остановлен.',flush=True)
        return 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(watch(Path(__file__).resolve().parent))
