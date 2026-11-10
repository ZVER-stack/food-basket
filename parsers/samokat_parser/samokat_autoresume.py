"""Resume Samokat after failed passes with bounded retries and durable checkpoints."""
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
    if any(word in lower for word in ('permissionerror', 'отказано в доступе', 'database is locked',
            'database disk image is malformed', 'disk is full', 'no space left',
            'disk i/o error', 'database or disk is full', 'readonly database')):
        return 'attention'
    if exit_code == 2 and re.search(r'\bHTTP\s+403\b', output, re.I) and UNFINISHED in output:
        return 'retry_403'
    if exit_code == 2 and re.search(r'\bHTTP\s+429\b', output, re.I) and UNFINISHED in output:
        return 'retry_rate_limit'
    if exit_code == 2 and UNFINISHED in output:
        return 'retry_error'
    return 'attention'


def retry_delay(consecutive_failures, progress, *, immediate=False, outcome='retry_error'):
    failures = 1 if progress else consecutive_failures + 1
    if immediate and outcome != 'retry_rate_limit':
        return failures, 0
    return failures, min(900 * 2 ** min(failures - 1, 3), 7200)


def is_parser_command(command):
    return bool(re.search(r'(?:^|\s)-m\s+samokat\s+run(?:\s|$)', command or '', re.I))


def external_can_retry(previous_failure, failure, codes):
    return bool(codes and 2 in codes and all(code in (0, 2) for code in codes) and failure
                and failure != previous_failure and classify(2, failure['text']) in ('retry_403', 'retry_error', 'retry_rate_limit'))


def durable_progress(before, after):
    # New raw responses alone can be repeated failures, not advancement through the zones.
    return after[0] > before[0] or (after[0] == before[0] and after[1] > before[1])


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
    # Windows readers/sync software can briefly prevent replacing an open file.
    # Keep the old complete state until the atomic replacement succeeds.
    for attempt in range(11):
        try:
            os.replace(temp, STATE)
            break
        except OSError as error:
            if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 10:
                raise
            time.sleep(0.2)


def note(message):
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(f'\n{datetime.now(MOSCOW).isoformat()} SUPERVISOR {message}\n')


def wait_until(deadline, **extra):
    while time.time() < deadline:
        if STOP.exists():
            return False
        write_state('waiting_after_error', next_retry_at=datetime.fromtimestamp(deadline, MOSCOW).isoformat(), **extra)
        time.sleep(min(15, max(0, deadline - time.time())))
    return not STOP.exists()


def run_parser(poll_interval=10):
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
    with LOG.open('ab', buffering=0) as output:
        offset = output.tell()
        child = subprocess.Popen(COMMAND, cwd=PROJECT, stdout=output, stderr=subprocess.STDOUT,
                                 env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        note(f'Запущен парсер PID={child.pid}')
        while child.poll() is None:
            write_state('running_stop_requested' if STOP.exists() else 'running', child_pid=child.pid)
            time.sleep(poll_interval)
        code = child.wait()
    with LOG.open('rb') as output:
        output.seek(max(offset, LOG.stat().st_size - 131072))
        transcript = output.read().decode('utf-8', errors='replace')
    return code, transcript


def supervise(console_pid=None, immediate_retry=False):
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
        note('Контроллер запущен; повторы после ошибок прохода, максимум 3 сбоя подряд без прогресса; интервал каталога 10 секунд')
        if immediate_retry:
            note('Включён --immediate-retry: без паузы после ошибок прохода, включая HTTP 403; HTTP 429 сохраняет задержку')
        # Не повторять недавно завершившийся проход немедленно.
        parser_log = OUTPUT.with_suffix('.log')
        if not immediate_retry and parser_log.exists() and not other_parsers():
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
                time.sleep(1 if immediate_retry else 15)
                continue
            if saw_external:
                codes = [exit_code(handle) for handle in external_handles]
                failure = last_failure(read_tail(console_pid))
                for handle in external_handles:
                    close_process(handle)
                external_handles = []
                if not external_can_retry(external_baseline, failure, codes):
                    write_state('attention', reason='Нет нового подтверждённого сбоя прохода либо ошибка требует вмешательства; повтор остановлен', exit_codes=codes)
                    return
                outcome = classify(2, failure['text'])
                progress = durable_progress(external_snapshot, snapshot)
                failures, delay = retry_delay(failures, progress, immediate=immediate_retry, outcome=outcome)
                note(f'Исходный парсер завершился: {outcome}; пауза {delay // 60} мин.; прогресс={progress}')
                saw_external = False
                if failures >= 4:
                    write_state('attention', reason='Три повторных сбоя без прогресса; требуется проверка', consecutive_failures=failures)
                    return
                if not wait_until(time.time() + delay, consecutive_failures=failures, source='original_console', last_outcome=outcome):
                    break
                continue
            code, transcript = run_parser(poll_interval=1 if immediate_retry else 10)
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
            if outcome not in ('retry_403', 'retry_error', 'retry_rate_limit'):
                write_state(outcome, last_exit_code=code, reason='Ошибка требует вмешательства либо проход прерван пользователем; автоматический повтор остановлен')
                return
            progress = durable_progress(snapshot, after)
            failures, delay = retry_delay(failures, progress, immediate=immediate_retry, outcome=outcome)
            if failures >= 4:
                write_state('attention', reason='Три повторных сбоя без прогресса; требуется проверка', consecutive_failures=failures, last_exit_code=code)
                return
            note(f'Ошибка прохода: {outcome}. Пауза {delay // 60} мин.; прогресс={progress}')
            if not wait_until(time.time() + delay, consecutive_failures=failures, last_exit_code=code, last_outcome=outcome):
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
    parser.add_argument('--immediate-retry', action='store_true', help='Повтор без паузы; HTTP 429 сохраняет задержку; максимум 3 неудачных повтора без прогресса')
    args = parser.parse_args()
    if args.stop:
        STOP.write_text(datetime.now(MOSCOW).isoformat(), encoding='utf-8')
        print('Автоповторы отключены; текущий проход не прерывается.')
    elif args.status:
        print(STATE.read_text(encoding='utf-8') if STATE.exists() else 'Контроллер ещё не записал состояние')
    else:
        supervise(args.console_pid, immediate_retry=args.immediate_retry)


if __name__ == '__main__':
    main()
