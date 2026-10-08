import json
from pathlib import Path
import subprocess
import sys
import unittest
import shutil
import watch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from desktop_cdp_transport import queue_lookup_code

NODE = shutil.which('node')

@unittest.skipUnless(NODE, 'Node.js is required for isolated JavaScript guard tests')
class GuardTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.enterContext(patch.dict(watch.SETTINGS, {'cwd': str(Path.cwd())}))
    def check(self, status, queued=0, error=False):
        # Execute the actual generated pre-enqueue guard with an isolated Desktop stub.
        expression = watch.guarded_expression('test only', 'test-event')
        stub = '''
        function queuePreview(item){return item;}
        function findDesktopManager(){return {requestClient:{sendRequest:async()=>{
          if (ERROR) throw Error('unavailable');
          return {thread:{status:STATUS}};
        }}};}
        async function desktopQueueAccess(){return {
          read:async()=>Array(QUEUED).fill({id:'existing'}),
          enqueue:async()=>{globalThis.calls++;return {inserted:true,id:'accepted',items:[]};}
        };}
        '''.replace('ERROR', json.dumps(error)).replace('STATUS', json.dumps(status)).replace('QUEUED', str(queued))
        expression = expression.replace(queue_lookup_code(), stub)
        code = 'globalThis.calls=0;' + expression + '.then(r=>console.log(JSON.stringify({result:r,calls})));'
        result = subprocess.run([str(NODE), '-e', code], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    def test_idle_sends_once(self):
        self.assertEqual(self.check({'type':'idle'})['calls'], 1)
    def test_active_never_enqueues(self):
        self.assertEqual(self.check({'type':'active'})['calls'], 0)
    def test_unknown_never_enqueues(self):
        for status in (None, {}, {'type':'notLoaded'}, {'type':'systemError'}):
            self.assertEqual(self.check(status)['calls'], 0)
    def test_queue_blocks_even_when_idle(self):
        self.assertEqual(self.check({'type':'idle'}, queued=1)['calls'], 0)
    def test_read_error_waits(self):
        result = self.check({'type':'idle'}, error=True)
        self.assertEqual(result['calls'], 0)
        self.assertTrue(result['result']['deferred'])

if __name__ == '__main__':
    unittest.main()
