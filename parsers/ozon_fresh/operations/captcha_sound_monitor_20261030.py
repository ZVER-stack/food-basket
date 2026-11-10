"""Sound on a manual-access episode; reads only this parser's local state."""
import argparse
import json
import os
from pathlib import Path
import time

WORK = Path(__file__).resolve().parent
PROJECT = Path(r'C:\Users\maxik\PycharmProjects\PythonProject\fresh_fast')
SNAPSHOT = PROJECT / 'results/full_shared_20261018_111443'
STATE = WORK / 'captcha_sound_state_20261030.json'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    payload = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    # Windows readers may briefly hold the destination without delete sharing.
    # Retry the same state only; never replay an alarm or clear its latch.
    for attempt in range(20):
        try:
            temporary.write_text(payload, encoding='utf-8')
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.1)


def manual_episode(run, backoff, browser):
    manual = any(data.get('requires_manual_access_check') or
                 data.get('status') == 'waiting_manual_access' or
                 data.get('manual_required_since_unix', 0) > 0
                 for data in (run, backoff))
    since = max(float(data.get('manual_required_since_unix', 0) or 0)
                for data in (run, backoff))
    reason = str(backoff.get('stop_reason') or run.get('stop_reason') or '')
    browser_reason = str(browser.get('reason') or '')
    browser_manual = browser.get('blocked') and any(
        word in browser_reason.casefold()
        for word in ('captcha', 'капч', 'challenge', 'manual access', 'authenticated'))
    if browser_manual and not manual:
        since = float(browser.get('blocked_at_unix', 0) or 0)
        reason = browser_reason
    return bool(manual or browser_manual), since, reason


def update_episode(state, manual, since):
    """Latch one episode through timestamp changes and collector exits/restarts."""
    if not manual:
        state['active_episode'] = None
        return False
    if state.get('active_episode') is not None:
        return False
    state['active_episode'] = str(since) if since else 'manual_without_timestamp'
    return True


def sound_alert():
    import winsound
    try:
        winsound.Beep(1000, 350)
        winsound.Beep(750, 350)
    except (OSError, RuntimeError):
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def acquire_lock(path):
    import msvcrt
    stream = path.open('a+b')
    if stream.seek(0, 2) == 0:
        stream.write(b'0')
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        stream.close()
        return None
    return stream


def watch():
    lock = acquire_lock(WORK / 'captcha_sound_monitor_20261030.lock')
    if lock is None:
        print('An existing sound monitor owns the lock; no duplicate started.', flush=True)
        return 0
    state = read_json(STATE) if STATE.exists() else {}
    state.update(pid=os.getpid(), script=str(Path(__file__).resolve()), status='running')
    print('Manual-access sound monitor active; local files only.', flush=True)
    try:
        while True:
            try:
                authorization = read_json(WORK / 'overnight_authorization_20261030.json')
                pointer = read_json(PROJECT / 'results/active_shared_run.json')
                if not authorization.get('active'):
                    state['status'] = 'authorization_inactive'
                    break
                if Path(pointer['out_dir']).resolve() != SNAPSHOT.resolve():
                    raise ValueError('Unexpected snapshot; monitor will not follow another run')
                run = read_json(SNAPSHOT / 'run_status.json')
                backoff = read_json(SNAPSHOT / 'backoff_status.json')
                browser = read_json(PROJECT / 'work/browser_transport_9222.json')
                manual, since, reason = manual_episode(run, backoff, browser)
                alert = update_episode(state, manual, since)
                state.update(status='waiting_manual_access' if manual else 'armed',
                             last_checked_unix=time.time(), manual_since=since if manual else 0)
                state.pop('read_error', None)
                if alert:
                    state.update(last_alert_unix=time.time(), last_alert_episode=state['active_episode'],
                                 last_alert_reason=reason, sound_attempted=True)
                    # Persist the latch first, so a restart cannot repeat this alarm.
                    write_json(STATE, state)
                    try:
                        sound_alert()
                        state['sound_error'] = None
                        print('Sound played for a new manual-access episode.', flush=True)
                    except Exception as error:
                        state['sound_error'] = str(error)
                        print('Sound playback failed: ' + str(error), flush=True)
                if pointer.get('complete'):
                    state['status'] = 'complete'
                    break
                write_json(STATE, state)
            except (OSError, ValueError, KeyError, TypeError) as error:
                state.update(read_error=str(error), last_checked_unix=time.time())
                write_json(STATE, state)
            time.sleep(5)
    except KeyboardInterrupt:
        state['status'] = 'stopped'
    finally:
        state['stopped_unix'] = time.time()
        write_json(STATE, state)
        lock.close()
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-sound', action='store_true')
    args = parser.parse_args()
    if args.test_sound:
        sound_alert()
        print('Test sound completed.', flush=True)
    else:
        raise SystemExit(watch())
