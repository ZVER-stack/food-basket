"""Read the main collection status locally; no browser connection or site requests."""
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import closing
import ozon_fresh_fast as core


def process_alive(pid):
    if not isinstance(pid,int) or pid<=0:return False
    if os.name!='nt':
        try:os.kill(pid,0);return True
        except ProcessLookupError:return False
        except PermissionError:return None
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:return False if ctypes.get_last_error()==87 else None
    try:
        code=wintypes.DWORD()
        return code.value==259 if kernel.GetExitCodeProcess(handle,ctypes.byref(code)) else None
    finally:kernel.CloseHandle(handle)


def read_status(root):
    root=Path(root);results=root/'results';pointer=results/'active_shared_run.json'
    if not pointer.exists():return {'status':'not_started'}
    active=core.load_json(pointer);folder=Path(active['out_dir']).resolve()
    if not folder.is_relative_to(results.resolve()):raise core.ParseError('Active run is outside results')
    def read(name):
        path=folder/name
        return core.load_json(path) if path.exists() else {}
    status=read('run_status.json');backoff=read('backoff_status.json');progress=read('progress.json')
    report=read('live_report.json')
    zones,skipped=core.load_zones(root/'hexagons.csv')
    configuration_path=root/'standalone_configuration.json'
    configuration=core.load_json(configuration_path) if configuration_path.exists() else {}
    counts={};rows=0;quarantined=0;db_path=folder/'snapshot.sqlite'
    if db_path.exists():
        with closing(sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            counts=dict(db.execute('SELECT status,count(*) FROM zones GROUP BY status'))
            for payload, in db.execute("SELECT payload FROM zones WHERE status IN ('complete','collected_with_gaps')"):
                rows+=len(json.loads(payload))
            quarantined=db.execute('SELECT count(*) FROM quarantined').fetchone()[0]
    state=status.get('status','unknown');alive=process_alive(status.get('pid'))
    if alive is False and state in ('running','waiting_backoff','waiting_manual_access'):
        state='process_not_running'
    return dict(status=state,process_alive=alive,folder=str(folder),requested_zones=len(zones),
        excluded_no_house=len(skipped),zones_complete=counts.get('complete',0),
        zones_with_gaps=counts.get('collected_with_gaps',0),zones_no_delivery=counts.get('no_delivery',0),
        zones_processed=sum(counts.get(k,0) for k in ('complete','collected_with_gaps','no_delivery')),
        exported_zone_rows=rows,incomplete_products=quarantined,
        interval_seconds=backoff.get('request_interval_seconds',report.get('request_interval_seconds')),
        configured_http_error_steps=configuration.get('http_error_steps_seconds'),
        configured_initial_interval=configuration.get('interval_min_seconds'),
        retry_at_unix=backoff.get('retry_at_unix',0),
        manual=bool(backoff.get('manual_required_since_unix')),
        current_zone=progress.get('zone_number'),current_category=progress.get('category'),
        updated_at=progress.get('updated_at_msk'),reason=status.get('stop_reason'))


def show_status(root):
    data=read_status(root)
    labels={'not_started':'Сбор ещё не запускался','running':'Работает',
        'waiting_backoff':'Ожидание после ограничения доступа',
        'waiting_manual_access':'Нужна ручная проверка доступа в Chrome, затем файл06',
        'process_not_running':'Процесс не работает; сохранённый прогресс не удалён',
        'stopped':'Остановлен','deadline_finished':'Истёк бюджет времени; сохранён частичный результат',
        'complete':'ГОТОВО: полный сбор завершён',
        'finished_with_gaps':'Все адреса обработаны, но есть пропуски в товарах'}
    print('\nСостояние: '+labels.get(data['status'],data['status']),flush=True)
    if data['status']=='not_started':return data
    print(f"Обработано адресов: {data['zones_processed']}/{data['requested_zones']}; полностью: {data['zones_complete']}; с пропусками: {data['zones_with_gaps']}; без доставки: {data['zones_no_delivery']}",flush=True)
    print(f"Товаров в завершённых адресах: {data['exported_zone_rows']}; неполных товаров отдельно: {data['incomplete_products']}",flush=True)
    active=data['status'] in ('running','waiting_backoff','waiting_manual_access')
    if data['interval_seconds'] is not None:
        label='Текущий интервал на сборщик' if active else 'Интервал последнего запуска'
        print(f"{label}: {data['interval_seconds']} сек.",flush=True)
    if not active and data.get('configured_http_error_steps'):
        rates=[data['configured_initial_interval']]+data['configured_http_error_steps']
        print('Настройка следующего запуска: '+' → '.join(str(x) for x in rates)+' сек.',flush=True)
    if data['retry_at_unix']>time.time():
        print('Пауза доступа до: '+core.datetime.fromtimestamp(data['retry_at_unix'],core.MSK).strftime('%d.%m.%Y %H:%M:%S МСК'),flush=True)
    if data.get('current_category') and data['status']=='running':
        print(f"Адрес {data['current_zone']}: {data['current_category']}",flush=True)
    print('Результаты: '+data['folder'],flush=True)
    print('Основной CSV: '+str(Path(data['folder'])/'ozon_fresh.csv'),flush=True)
    if data['reason']:print('Причина: '+data['reason'],flush=True)
    return data
