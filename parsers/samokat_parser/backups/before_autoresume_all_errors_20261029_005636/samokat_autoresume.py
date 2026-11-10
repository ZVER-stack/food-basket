"""Continue an existing Samokat collection; retry only confirmed HTTP 403 exits."""
from __future__ import annotations

import argparse
import csv
import ctypes
import json
import logging
import msvcrt
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3), name='Europe/Moscow')
PROJECT = Path(__file__).resolve().parent
RESULTS = PROJECT / 'results'
STATE = RESULTS / 'samokat_autoresume.state.json'
LOG = RESULTS / 'samokat_autoresume.log'
STOP = RESULTS / 'samokat_autoresume.stop'
LOCK = RESULTS / 'samokat_autoresume.lock'
OUTPUT = RESULTS / 'samokat_2026-10-21_arkhipov_recovered.csv'
COMMAND = [str(PROJECT / '.venv/Scripts/python.exe'), '-m', 'samokat', 'run',
           '--output', r'.\results\samokat_2026-10-21_arkhipov_recovered.csv',
           '--interval', '10', '--startup-timeout', '600', '--capture-dir', r'.\results\raw_arkhipov']
UNFINISHED = 'Проход не завершён.'


def classify(exit_code, output):
    lower = output.lower()
    if exit_code == 0:
        return 'success'
    if exit_code == 130 or exit_code == -1073741510:
        return 'interrupted'
    if any(word in lower for word in ('капча', 'captcha', 'подтвердите, что вы', 'verify you are human')):
        return 'attention'
    if exit_code == 2 and re.search(r'\bHTTP\s+403\b', output, re.I) and UNFINISHED in output:
        return 'retry_403'
    return 'attention'


def retry_delay(consecutive_failures, progress):
    failures = 1 if progress else consecutive_failures + 1
    return failures, min(900 * 2 ** min(failures - 1, 3), 7200)


def is_parser_command(command):
    return bool(re.search(r'(?:^|\s)-m\s+samokat\s+run(?:\s|$)', command or '', re.I))


def external_can_retry(previous_failure, failure, codes):
    return bool(codes and 2 in codes and 259 not in codes and failure and failure['confirmed_403']
                and failure != previous_failure and classify(2, failure['text']) == 'retry_403')


def other_parsers():
    script = "Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run([r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
                             '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, encoding='utf-8', errors='replace', timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Не удалось проверить отсутствие другого парсера')
    text = result.stdout.strip()
    processes = json.loads(text) if text else []
    if isinstance(processes, dict):
        processes = [processes]
    return [int(p['ProcessId']) for p in processes if is_parser_command(p.get('CommandLine'))]


def progress_snapshot():
    with sqlite3.connect(OUTPUT.with_suffix('.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        finished = db.execute('SELECT COUNT(*) FROM zones').fetchone()[0]
        partial = db.execute('SELECT COALESCE(SUM(count),0) FROM partial_zones').fetchone()[0]
    captures = sum(1 for _ in (RESULTS / 'raw_arkhipov').glob('*.json'))
    return finished, partial, captures


def write_state(status, **extra):
    value = {'status': status, 'pid': os.getpid(), 'updated_at': datetime.now(MOSCOW).isoformat(),
             'command': COMMAND, 'output': str(OUTPUT), **extra}
    temp = STATE.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, STATE)


def note(message):
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(f'\n{datetime.now(MOSCOW).isoformat()} SUPERVISOR {message}\n')


def wait_until(deadline, **extra):
    while time.time() < deadline:
        if STOP.exists():
            return False
        write_state('waiting_after_403', next_retry_at=datetime.fromtimestamp(deadline, MOSCOW).isoformat(), **extra)
        time.sleep(min(15, max(0, deadline - time.time())))
    return not STOP.exists()


def run_parser():
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
    with LOG.open('ab', buffering=0) as output:
        offset = output.tell()
        child = subprocess.Popen(COMMAND, cwd=PROJECT, stdout=output, stderr=subprocess.STDOUT,
                                 env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        note(f'Запущен парсер PID={child.pid}')
        while child.poll() is None:
            write_state('running_stop_requested' if STOP.exists() else 'running', child_pid=child.pid)
            time.sleep(10)
        code = child.wait()
    with LOG.open('rb') as output:
        output.seek(max(offset, LOG.stat().st_size - 131072))
        transcript = output.read().decode('utf-8', errors='replace')
    return code, transcript


def supervise(console_pid=None):
    from samokat_console_read import read_tail, last_failure, open_process, exit_code, close_process
    RESULTS.mkdir(parents=True, exist_ok=True)
    lock = LOCK.open('a+b')
    if lock.tell() == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise SystemExit('Автопродолжение уже работает')
    awake = ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    failures = 0
    saw_external = False
    external_handles = []
    external_baseline = None
    external_snapshot = None
    try:
        with (PROJECT / 'data/hexagons.csv').open(encoding='utf-8-sig', newline='') as source:
            planned = sum(1 for _ in csv.DictReader(source))
        note('Контроллер запущен; повторяются только остановки HTTP 403, интервал каталога 10 секунд')
        # Не повторять недавно завершившийся проход немедленно.
        parser_log = OUTPUT.with_suffix('.log')
        if parser_log.exists() and not other_parsers():
            if not wait_until(parser_log.stat().st_mtime + 900, reason='initial_cooldown'):
                write_state('stopped')
                return
        while not STOP.exists():
            snapshot = progress_snapshot()
            if snapshot[0] >= planned:
                write_state('complete', completed_zones=snapshot[0], planned_zones=planned)
                note('Все зоны обработаны; автопродолжение завершено')
                return
            existing = other_parsers()
            if existing:
                if not saw_external:
                    if not console_pid:
                        raise RuntimeError('Для наблюдения уже идущего парсера не задана его консоль')
                    external_baseline = last_failure(read_tail(console_pid))
                    external_handles = [open_process(pid) for pid in existing]
                    external_snapshot = snapshot
                    note(f'Наблюдается существующий парсер PID={existing}; консоль только читается')
                saw_external = True
                write_state('waiting_for_existing_parser', existing_pids=existing)
                time.sleep(15)
                continue
            if saw_external:
                codes = [exit_code(handle) for handle in external_handles]
                failure = last_failure(read_tail(console_pid))
                for handle in external_handles:
                    close_process(handle)
                external_handles = []
                if not external_can_retry(external_baseline, failure, codes):
                    write_state('attention', reason='Исходный парсер завершился без подтверждённого нового HTTP 403; повтор остановлен', exit_codes=codes)
                    return
                progress = any(a > b for a, b in zip(snapshot, external_snapshot))
                failures, delay = retry_delay(failures, progress)
                note(f'Исходный парсер завершился с подтверждённым HTTP 403; пауза {delay // 60} мин.')
                saw_external = False
                if not wait_until(time.time() + delay, consecutive_403=failures, source='original_console'):
                    break
                continue
            code, transcript = run_parser()
            if STOP.exists():
                write_state('stopped', last_exit_code=code)
                return
            outcome = classify(code, transcript)
            after = progress_snapshot()
            note(f'Парсер завершился: code={code}, outcome={outcome}, обработано зон={after[0]}')
            if outcome == 'success':
                if after[0] >= planned:
                    write_state('complete', completed_zones=after[0], planned_zones=planned)
                else:
                    write_state('attention', reason='Процесс завершился без ошибки, но обработаны не все зоны')
                return
            if outcome != 'retry_403':
                write_state(outcome, last_exit_code=code, reason='Не HTTP 403; автоматический повтор остановлен')
                return
            progress = after[0] > snapshot[0] or after[1] > snapshot[1] or after[2] > snapshot[2]
            failures, delay = retry_delay(failures, progress)
            note(f'Подтверждён HTTP 403. Пауза {delay // 60} мин.; прогресс={progress}')
            if not wait_until(time.time() + delay, consecutive_403=failures, last_exit_code=code):
                break
        write_state('stopped')
    except Exception as error:
        note(f'Контроллер остановлен: {type(error).__name__}: {error}')
        write_state('attention', reason=f'{type(error).__name__}: {error}')
    finally:
        for handle in external_handles:
            close_process(handle)
        if awake:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true', help='Отключить новые запуски; текущий проход продолжится до завершения')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--console-pid', type=int, help='PID исходной консоли, вывод которой разрешено читать')
    args = parser.parse_args()
    if args.stop:
        STOP.write_text(datetime.now(MOSCOW).isoformat(), encoding='utf-8')
        print('Автоповторы отключены; текущий проход не прерывается.')
    elif args.status:
        print(STATE.read_text(encoding='utf-8') if STATE.exists() else 'Контроллер ещё не записал состояние')
    else:
        supervise(args.console_pid)


if __name__ == '__main__':
    main()
