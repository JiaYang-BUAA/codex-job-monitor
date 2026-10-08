import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor
import job_watch
import watch


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'config.json'
        self.config = copy.deepcopy(monitor.EXAMPLE)
        self.config.update(thread_id='test-thread', cwd=str(self.root))
        self.config['target']['job_directory'] = str(self.root / 'job')
        self.write_config()
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        old = dict(watch.SETTINGS)
        old_root, old_thread = watch.ROOT, watch.THREAD
        def restore():
            watch.SETTINGS.clear(); watch.SETTINGS.update(old)
            watch.ROOT, watch.THREAD = old_root, old_thread
            job_watch.ROOT, job_watch.THREAD = old_root, old_thread
        self.addCleanup(restore)

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')

    def cli(self, *args):
        return monitor.main(['--config', str(self.config_path), *args])

    def test_runtime_relative_to_config_not_working_directory(self):
        config = monitor.load_config(self.config_path)
        self.assertEqual(config['runtime_dir'], str(self.root / 'runtime'))

    def test_init_refuses_overwrite(self):
        before = self.config_path.read_bytes()
        self.assertEqual(self.cli('init'), 1)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_run_requires_explicit_send_switch(self):
        with patch.object(job_watch, 'main') as run, self.assertRaises(SystemExit):
            self.cli('run')
        run.assert_not_called()

    def test_probe_never_opens_transport_or_writes_runtime(self):
        with patch.object(job_watch, 'observe', return_value={'observed_at':'now', 'jobs':[]}), patch.object(watch,'client') as client:
            self.assertEqual(self.cli('probe'), 0)
        client.assert_not_called()
        self.assertFalse((self.root/'runtime').exists())

    def test_doctor_probes_without_enqueue_or_state_write(self):
        with patch.object(watch,'client') as client, patch.object(monitor.shutil,'which', return_value='binary'):
            client.return_value.probe.return_value = {'ok':True}
            client.return_value.evaluate.return_value = {'ok':True, 'threadId':'test-thread', 'status':'idle', 'queuedCount':0}
            self.assertEqual(self.cli('doctor'),0)
            expression = client.return_value.evaluate.call_args.args[0]
            self.assertIn("'thread/read'", expression)
            self.assertNotIn('await access.enqueue(', expression)
        self.assertFalse((self.root/'runtime').exists())

    def test_doctor_rejects_unavailable_target(self):
        with patch.object(watch,'client') as client:
            client.return_value.probe.return_value = {'ok':True}
            client.return_value.evaluate.side_effect = RuntimeError('Unknown thread')
            self.assertEqual(self.cli('doctor'),1)
        self.assertFalse((self.root/'runtime').exists())

    def test_review_exit_code_reaches_cli(self):
        with patch.object(job_watch,'main', return_value=2):
            self.assertEqual(self.cli('run','--enable-send'),2)

    def test_run_passes_validated_target(self):
        with patch.object(job_watch,'main', return_value=0) as run:
            self.assertEqual(self.cli('run','--enable-send'),0)
            run.assert_called_once_with(job_watch.normalize_target(self.config['target']))

    def test_stop_creates_flag_only_in_runtime(self):
        (self.root/'runtime').mkdir()
        self.assertEqual(self.cli('stop'),0)
        self.assertTrue((self.root/'runtime/STOP').exists())

    def test_external_cdp_and_invalid_poll_rejected(self):
        for field,value in [('cdp_url','http://192.0.2.1:9222'),('poll_seconds', True),('poll_seconds',0),('runtime_dir', str(watch.SOURCE))]:
            with self.subTest(field=field,value=value):
                config=copy.deepcopy(self.config); config[field]=value
                self.config_path.write_text(json.dumps(config),encoding='utf-8')
                with self.assertRaises(ValueError): monitor.load_config(self.config_path)

    def test_valid_ssh_aliases_and_unsafe_aliases(self):
        from test_events import A
        for alias in ['compute-server','node.example.org','cluster_2']:
            self.assertEqual(job_watch.normalize_target(dict(A,ssh_host=alias))['ssh_host'],alias)
        for alias in ['-oProxyCommand=bad', 'node;command', 'node\nnext', 'user@node', '']:
            with self.assertRaises(ValueError): job_watch.normalize_target(dict(A,ssh_host=alias))

    def test_guard_uses_configured_target_and_checks_queue_before_enqueue(self):
        monitor.configure(monitor.load_config(self.config_path))
        expression=watch.guarded_expression('job done','event-id')
        self.assertIn('test-thread',expression)
        self.assertIn('target_queue_not_empty',expression)
        self.assertIn('target_not_idle',expression)
        self.assertLess(expression.index('target_not_idle'),expression.index('const result = await access.enqueue(payload.message)'))

if __name__ == '__main__':
    unittest.main()
