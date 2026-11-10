import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ozon_fresh_fast as core
from ozon_watch import Monitor,read_watch_state,message


class WatchTests(unittest.TestCase):
    def test_each_manual_episode_alerts_once_even_across_status_refreshes(self):
        watch=Monitor();state=dict(folder='run',status='waiting_manual_access',manual_since=100)
        self.assertEqual(watch.observe(state),(True,True))
        self.assertEqual(watch.observe(state),(False,False))
        watch.observe(dict(state,status='running'))
        self.assertEqual(watch.observe(state),(True,False))
        self.assertEqual(watch.observe(dict(state,manual_since=200)),(True,True))
    def test_backoff_is_not_called_captcha_and_completion_with_gaps_is_distinct(self):
        watch=Monitor();state=dict(folder='run',status='waiting_backoff',retry_at=100)
        self.assertEqual(watch.observe(state),(True,False))
        self.assertNotIn('НУЖНА РУЧНАЯ',message(state))
        self.assertIn('пропуски',message(dict(state,status='finished_with_gaps')))
        self.assertNotIn('полный сбор',message(dict(state,status='finished_with_gaps')))
    def test_terminal_notification_and_source_folder_are_deduplicated(self):
        watch=Monitor();state=dict(folder='A',status='complete')
        self.assertEqual(watch.observe(state),(True,True))
        self.assertEqual(watch.observe(state),(False,False))
        self.assertEqual(watch.observe(dict(state,folder='B')),(True,True))
    def test_connection_error_is_not_reported_as_proven_captcha(self):
        state=dict(status='waiting_manual_access',reason='Похоже, нет соединения')
        self.assertTrue(message(state).startswith('НУЖНА РУЧНАЯ ПРОВЕРКА ДОСТУПА.'))
        self.assertTrue(message(dict(state,reason='CAPTCHA in HTTP403')).startswith('КАПЧА.'))
    def test_reader_does_not_report_dead_process_as_pending_captcha(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root=Path(tmp);folder=root/'results/full';folder.mkdir(parents=True)
            core.write_json(root/'results/active_shared_run.json',{'out_dir':str(folder.resolve())})
            core.write_json(folder/'run_status.json',{'status':'waiting_manual_access','pid':1})
            core.write_json(folder/'backoff_status.json',{'manual_required_since_unix':100})
            with patch('ozon_watch.process_alive',return_value=False):
                self.assertEqual(read_watch_state(root)['status'],'process_not_running')
    def test_source_pointer_outside_results_is_rejected_without_reading_it(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root=Path(tmp)
            core.write_json(root/'results/active_shared_run.json',{'out_dir':str(root.resolve())})
            with self.assertRaises(core.ParseError):read_watch_state(root)


if __name__=='__main__':unittest.main()
