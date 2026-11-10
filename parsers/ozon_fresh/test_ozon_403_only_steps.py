import json,tempfile,threading,unittest
from pathlib import Path
from ozon_backoff import BackoffGate
class Http403StepsTests(unittest.TestCase):
 def test_generic_penalty_rate_is_not_lowered_by_restart_or_next403(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
   clock=[1000.0]
   def gate():return BackoffGate(temp,threading.Event(),5,clock=lambda:clock[0],max_interval=10,http_error_steps=(6,7,8,9,10))
   g=gate();g.penalize('HTTP0; manual access check required',manual=True)
   self.assertEqual(g.interval,10)
   g=gate();self.assertEqual(g.interval,10);self.assertTrue(g.manual_since)
   clock[0]+=200;g.manual_since=0;g.penalize('HTTP403')
   self.assertEqual(g.interval,10)
 def test_increment_cap_restart_burst_429_and_manual_retry_after(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
   clock=[1000.0]
   def gate():return BackoffGate(temp,threading.Event(),5,clock=lambda:clock[0],max_interval=10,on_429_interval=5,http_error_steps=(6,7,8,9,10),http_error_statuses=(403,))
   g=gate()
   for expected in [6,7,8,9,10,10]:
    g.penalize('HTTP403');self.assertEqual(g.interval,expected)
    g.penalize('HTTP403');self.assertEqual(g.interval,expected)
    g=gate();self.assertEqual(g.interval,expected)
    clock[0]+=20
   before=g.http_error_level;g.penalize('HTTP429');self.assertEqual(g.http_error_level,before);self.assertEqual(g.interval,10)
   clock[0]+=20;g.penalize('Challenge URL in HTTP403',manual=True,retry_after=120)
   self.assertEqual(g.interval,10);self.assertEqual(g.until,clock[0]+120);self.assertTrue(g.manual_since)
   g=gate();self.assertTrue(g.manual_since);self.assertEqual(g.until,clock[0]+120)
 def test_mixed403429_use_one_shared_staircase_and_persist_cap(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
   clock=[1000.0]
   def gate():return BackoffGate(temp,threading.Event(),5,clock=lambda:clock[0],max_interval=10,http_error_steps=(6,7,8,9,10))
   g=gate()
   for reason,expected in [('HTTP403',6),('HTTP429',7),('HTTP403',8),('HTTP429',9),('HTTP429',10),('HTTP403',10)]:
    g.penalize(reason);self.assertEqual(g.interval,expected)
    g.penalize('HTTP429');self.assertEqual(g.interval,expected)
    g=gate();self.assertEqual(g.interval,expected)
    clock[0]+=20

if __name__=='__main__':unittest.main()
