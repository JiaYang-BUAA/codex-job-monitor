"""Offline actual-function tests. No real ROOT, SSH, Desktop client, or enqueue."""
import argparse
import ast
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

REAL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REAL_ROOT))
import job_watch as w


def target(name):
    return dict(mode='remote', ssh_host='compute-server',
        job_directory=r'E:\Jobs\compat-test-only',
        job_id='remote:compute-server:E:\\Jobs\\compat-test-only\\output\\' + name,
        job_name=name, probe_file=r'scripts\exact-run-probe.ps1',
        owned_file='owned-' + name + '.json', summary_file='output\\' + name + '\\summary.json',
        launch_file='launch-' + name + '.json', driver_exit_file='exit-' + name + '.json')


A, B, OLD = target('G1'), target('G2'), target('old-inactive')
MULTI = dict(mode='remote-list', targets=[A, B])
EID = '11111111-1111-4111-8111-111111111111'
OID = '22222222-2222-4222-8222-222222222222'


def job(t=A, alive=True, state='running', **kw):
    data = dict(id=t['job_id'], name=t['job_name'], valid=True, state=state,
        started_utc='1970-01-01T00:00:00Z', processes_known=True,
        processes=[dict(identity='123:639267192537532135', alive=alive)],
        child_exit_code=None, wrapper_exit_code=None, scientific_status=None)
    data.update(kw)
    return data


def observation(*jobs):
    return dict(observed_at='2026-10-04T15:00:00Z', jobs=list(jobs))


def response(*jobs):
    return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(observation(*jobs)))


def pending(j, reason='exit', event_id=EID):
    return dict(event_id=event_id, job=copy.deepcopy(j), reason=reason, observed_at='old')


def state():
    return dict(jobs={}, pending={}, dispatching=None, delivered_count=0,
        watchdog_due={'historical': 7200}, closed_events={'keep': {'retry_allowed': False}})


class CompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='offline-', dir=Path(__file__).resolve().parent)
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.client = self.enterContext(patch.object(w, 'client',
            side_effect=AssertionError('A real Desktop client is forbidden in offline tests')))
        self.ssh = self.enterContext(patch.object(w.subprocess, 'run',
            side_effect=AssertionError('A real subprocess/SSH is forbidden in offline tests')))
        self.enterContext(patch.object(w, 'ROOT', self.root))
        self.enterContext(patch.object(w, 'guarded_expression', return_value='OFFLINE_GUARD_ONLY'))
        self.enterContext(patch.object(w.msvcrt, 'locking', return_value=None))

    def fake_client(self, result=None, exc=None, results=None):
        c = Mock()
        if exc is not None:
            c.evaluate.side_effect = exc
        elif results is not None:
            c.evaluate.side_effect = results
        else:
            c.evaluate.return_value = result or {'ok': True, 'queuedMessageId': 'offline-ack'}
        self.client.side_effect = None
        self.client.return_value = c
        return c

    def run_main_once(self, s, target=MULTI):
        (self.root / 'ENABLED').write_text('offline test only')
        (self.root / 'watch-target.json').write_text(json.dumps(target), encoding='utf-8')
        (self.root / 'job-state.json').write_text(json.dumps(s), encoding='utf-8')
        saved = []
        real_save = w.save
        def capture(path, obj):
            saved.append((path.name, copy.deepcopy(obj)))
            real_save(path, obj)
        def stop_once(_):
            (self.root / 'STOP').write_text('offline test only')
        with patch.object(w, 'save', side_effect=capture), patch.object(w.time, 'sleep', side_effect=stop_once):
            w.main()
        return json.loads((self.root / 'job-state.json').read_text()), saved

    def test_multi_target_config_preserves_exact_run_fields(self):
        (self.root / 'watch-target.json').write_text(json.dumps(MULTI), encoding='utf-8')
        actual = w.target_config()
        self.assertEqual(actual, MULTI)
        self.assertEqual(w.target_keys(actual), (A['job_id'], B['job_id']))
        self.assertEqual(actual['targets'][1]['owned_file'], B['owned_file'])

    def test_unknown_missing_config_keys_and_modes_fail_closed(self):
        variants = [dict(MULTI, unexpected=1), dict(mode='remote-list', targets=[]),
            dict(mode='remote-list', targets=[dict(A, cpu_gate=10)]),
            dict(mode='remote-list', targets=[dict(mode='local', job_directory=str(self.root))]),
            dict(mode='ssh'), dict(A, mode='unknown'), {k:v for k,v in A.items() if k != 'owned_file'},
            dict(A, ssh_host='-invalid-option'), dict(A, summary_file='..\\wrong.json')]
        for item in variants:
            with self.subTest(item=item), self.assertRaises(ValueError):
                w.normalize_target(item)
        self.client.assert_not_called()
        self.ssh.assert_not_called()

    def test_duplicate_target_ids_including_case_variants_fail(self):
        for second in (A, dict(A, job_id=A['job_id'].upper())):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, 'Duplicate'):
                w.normalize_target(dict(mode='remote-list', targets=[A, second]))

    def test_duplicate_json_keys_are_not_silently_overwritten(self):
        for text in ('{"mode":"local","mode":"remote"}',
                     '{"targets":[{"job_id":"a","job_id":"b"}]}'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
                w.strict_json(text)

    def test_missing_target_cannot_restore_broad_legacy_discovery(self):
        with self.assertRaisesRegex(ValueError, 'Explicit'):
            w.target_config()
        self.ssh.assert_not_called()

    def test_multi_target_eligibility_excludes_inactive_and_legacy_phase_events(self):
        s = state()
        s['pending'] = {A['job_id']: pending(job(A, False)),
            B['job_id']: pending(job(B, True, 'initializing'), 'status', 'b'),
            OLD['job_id']: pending(job(OLD, False), event_id=OID),
            'local:old': pending(dict(job(OLD, False), id='local:old'))}
        before = copy.deepcopy(s)
        self.assertEqual([k for k,e in w.eligible_events(s, MULTI)], [A['job_id']])
        s['pending'][B['job_id']]['reason'] = 'watchdog'
        s['pending'][B['job_id']]['job'] = job(B, False)
        self.assertEqual([k for k,e in w.eligible_events(s, MULTI)], [A['job_id']])
        self.assertEqual(before['pending'][OLD['job_id']], s['pending'][OLD['job_id']])

    def test_old_pending_never_connects_to_notifier_or_mutates_history(self):
        s = state()
        s['pending'][OLD['job_id']] = pending(job(OLD, False), event_id=OID)
        before = copy.deepcopy(s)
        self.assertFalse(w.deliver(s, MULTI))
        self.assertEqual(s, before)
        self.client.assert_not_called()

    def test_two_remote_probes_use_individual_exact_run_invocations(self):
        self.ssh.side_effect = [response(job(A)), response(job(B))]
        observed = w.observe(MULTI)
        self.assertEqual([j['id'] for j in observed['jobs']], [A['job_id'], B['job_id']])
        self.assertEqual(self.ssh.call_count, 2)
        for call, t in zip(self.ssh.call_args_list, [A, B]):
            args = call.args[0]
            self.assertEqual(args[0], 'ssh')
            script = base64.b64decode(args[-1]).decode('utf-16-le')
            self.assertEqual(script, w.remote_probe_command(t))
            self.assertIn(t['owned_file'], script)
            self.assertNotIn('probe_jobs.ps1', script)
        self.client.assert_not_called()

    def test_unknown_partial_or_malformed_stdout_cannot_be_observed(self):
        payloads = ['not-json', '{"jobs":', json.dumps({'jobs': [job()]}),
            json.dumps(observation(dict(id=A['job_id'], valid=True))),
            'warning\n' + json.dumps(observation(job())),
            json.dumps(observation(dict(job(), processes=[{'identity':'unknown','alive':False}]))),
            json.dumps(observation(dict(job(), processes_known=True, processes=[]))),
            '{"observed_at":"now","jobs":[],"jobs":[' + json.dumps(job()) + ']}']
        for text in payloads:
            with self.subTest(text=text):
                self.ssh.side_effect = None
                self.ssh.return_value = SimpleNamespace(returncode=0, stdout=text, stderr='')
                with self.assertRaises((ValueError, TypeError)):
                    w.observe(A)
        self.client.assert_not_called()

    def test_unknown_wrong_duplicate_and_extra_job_ids_fail_closed(self):
        for jobs in ([job(B)], [], [job(A), job(A)], [job(A), job(B)]):
            with self.subTest(jobs=jobs):
                self.ssh.side_effect = None
                self.ssh.return_value = response(*jobs)
                with self.assertRaises(ValueError):
                    w.observe(A)
        self.client.assert_not_called()

    def test_partial_second_target_stops_ingestion_and_delivery_in_actual_main(self):
        original = state()
        self.ssh.side_effect = [response(job(A, False)),
            SimpleNamespace(returncode=0, stdout='{"jobs":', stderr='')]
        final, saved = self.run_main_once(original)
        self.assertEqual(final, original)
        self.assertTrue(any(n == 'status.json' and s.get('status') == 'observation_or_delivery_error' for n,s in saved))
        self.client.assert_not_called()

    def test_duplicate_observed_ids_reject_atomically_before_ingestion(self):
        s = state()
        before = copy.deepcopy(s)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            w.ingest(s, observation(job(A, False), job(A, False)), MULTI)
        self.assertEqual(s, before)

    def test_unknown_process_fields_and_exit_layouts_fail_closed(self):
        for extra in (dict(processes=[{'identity':'12:34','alive':'false'}]),
                      dict(processes=[{'identity':'12:34','alive':False}]*2),
                      dict(processes=[{'identity':'12:0','alive':False}]),
                      dict(state={'phase':'done'}), dict(child_exit_code=False),
                      dict(required_failure='true')):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                w.validate_observation(observation(job(**extra)), (A['job_id'],))

    def test_healthy_init_running_phase_and_progress_changes_are_silent(self):
        prior = None
        for state_name in ('pending', 'starting', 'initializing', 'loading_mesh', 'running', 'solver_phase', 'running'):
            now = job(state=state_name, scientific_status='PHASE_' + state_name.upper())
            self.assertIsNone(w.detect(prior, now))
            prior = now
        self.assertIsNone(w.detect(prior, dict(prior, elapsed_seconds=100000, cpu_pct=0.1)))

    def test_actual_main_two_healthy_jobs_over_two_hours_never_wake(self):
        s = state()
        for t in [A, B]:
            s['watchdog_due'][t['job_id']] = 0
            s['pending'][t['job_id']] = pending(job(t), 'watchdog', EID if t is A else OID)
        before = copy.deepcopy(s)
        self.ssh.side_effect = [response(job(A, state='running')),
                               response(job(B, state='initializing'))]
        with patch.object(w.time, 'time', return_value=100000), patch.object(w, 'watchdog',
                side_effect=AssertionError('Main must never call revoked watchdog')):
            final, saved = self.run_main_once(s)
        self.assertEqual(final['pending'], before['pending'])
        self.assertEqual(final['watchdog_due'], before['watchdog_due'])
        self.assertEqual(final['closed_events'], before['closed_events'])
        self.assertEqual(final['delivered_count'], 0)
        run = next(obj for name,obj in saved if name == 'run.json')
        self.assertTrue(run['watchdog_disabled'])
        self.assertIsNone(run['watchdog_seconds'])
        self.client.assert_not_called()

    def test_legacy_watchdog_and_ack_helpers_are_history_preserving_noops(self):
        s = state()
        s['pending'][A['job_id']] = pending(job(), 'watchdog')
        before = copy.deepcopy(s)
        w.watchdog(s, observation(job()), now=100000)
        w.acknowledge(s, A['job_id'], now=100000)
        self.assertEqual(s, before)

    def test_terminal_event_and_queue_ack_occur_exactly_once(self):
        s = state()
        w.ingest(s, observation(job(A)), A)
        w.ingest(s, observation(job(A, False)), A)
        eid = s['pending'][A['job_id']]['event_id']
        c = self.fake_client()
        self.assertTrue(w.deliver(s, A))
        for state_name in ('processes_exited', 'program_completed', 'failed'):
            w.ingest(s, observation(job(A, False, state_name)), A)
            self.assertFalse(w.deliver(s, A))
        self.assertEqual(s['delivered_count'], 1)
        self.assertEqual(c.evaluate.call_count, 1)
        self.assertEqual(s['watchdog_due'], {'historical': 7200})
        receipt = json.loads((self.root/'job-events'/eid/'delivery.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['status'], 'queued')
        self.assertEqual(receipt['thread_id'], w.THREAD)

    def test_first_authoritative_terminal_wrapper_state_is_an_event(self):
        self.assertEqual(w.detect(None, job(state='program_completed')), 'status')
        self.assertIsNone(w.detect(job(state='program_completed'), job(state='finished')))

    def test_required_failure_while_process_is_alive_is_an_event_once(self):
        for extra in (dict(state='program_failed'), dict(child_exit_code=1),
                      dict(required_failure=True), dict(scientific_status='QUALITY_SCREEN_FAILED')):
            with self.subTest(extra=extra):
                failed = job(**extra)
                self.assertEqual(w.detect(job(), failed), 'required_failure')
                self.assertIsNone(w.detect(failed, failed))

    def test_invalid_or_unknown_process_is_not_a_verified_exit(self):
        self.assertIsNone(w.detect(job(), job(alive=False, valid=False)))
        self.assertIsNone(w.detect(job(), job(alive=False, processes_known=False)))

    def test_current_unknown_pointer_locks_main_before_observation(self):
        s = state()
        s['pending'][A['job_id']] = pending(job(A, False))
        s['dispatching'] = EID
        final, saved = self.run_main_once(s)
        self.assertEqual(final, s)
        self.assertTrue(any(n == 'status.json' and d.get('status') == 'delivery_needs_review' for n,d in saved))
        self.ssh.assert_not_called()
        self.client.assert_not_called()

    def test_unidentified_dispatch_pointer_is_fail_closed(self):
        s = state()
        s['dispatching'] = EID
        self.assertTrue(w.delivery_review(s, MULTI)['blocking'])
        with self.assertRaisesRegex(RuntimeError, 'needs review'):
            w.deliver(s, MULTI)
        self.client.assert_not_called()

    def test_current_migrated_unknown_record_still_locks(self):
        s = state()
        s['unresolved_delivery_events'] = {EID: dict(job_id=A['job_id'], retry_allowed=False)}
        final, _ = self.run_main_once(s)
        self.assertEqual(final, s)
        self.ssh.assert_not_called()

    def test_inactive_dispatch_pointer_does_not_stop_observation_or_get_cleared(self):
        s = state()
        s['pending'][OLD['job_id']] = pending(job(OLD, False), event_id=OID)
        s['dispatching'] = OID
        original = copy.deepcopy(s)
        self.ssh.side_effect = [response(job(A, False)), response(job(B))]
        final, saved = self.run_main_once(s)
        self.assertEqual(final['dispatching'], OID)
        self.assertEqual(final['pending'][OLD['job_id']], original['pending'][OLD['job_id']])
        self.assertIn(A['job_id'], final['pending'])
        report = next(d for n,d in saved if n == 'status.json' and d.get('status') == 'monitoring_job_events')
        self.assertTrue(report['legacy_pointer_migration_required'])
        self.assertEqual(report['delivery_review']['inactive'][0]['event_id'], OID)
        self.assertEqual(self.ssh.call_count, 2)
        self.client.assert_not_called()

    def test_explicitly_migrated_inactive_unknown_record_allows_current_delivery(self):
        s = state()
        old = pending(job(OLD, False), event_id=OID)
        record = dict(job_id=OLD['job_id'], original_event=copy.deepcopy(old),
                      migration_receipt='EXTERNAL_REVIEW_REQUIRED', retry_allowed=False)
        s['pending'][OLD['job_id']] = old
        s['unresolved_delivery_events'] = {OID: copy.deepcopy(record)}
        s['pending'][A['job_id']] = pending(job(A, False))
        c = self.fake_client()
        self.assertTrue(w.deliver(s, MULTI))
        self.assertEqual(s['unresolved_delivery_events'][OID], record)
        self.assertEqual(s['pending'][OLD['job_id']], old)
        self.assertEqual(c.evaluate.call_count, 1)

    def test_ambiguous_queue_outcome_preserves_pointer_and_never_retries(self):
        s = state()
        s['pending'][A['job_id']] = pending(job(A, False))
        c = self.fake_client(exc=RuntimeError('offline transport lost after enqueue attempt'))
        with self.assertRaises(RuntimeError):
            w.deliver(s, A)
        self.assertEqual(s['dispatching'], EID)
        self.assertIn(A['job_id'], s['pending'])
        with self.assertRaisesRegex(RuntimeError, 'needs review'):
            w.deliver(s, A)
        self.assertEqual(c.evaluate.call_count, 1)
        receipt = json.loads((self.root/'job-events'/EID/'delivery.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['status'], 'outcome_unknown_no_retry')

    def test_missing_or_unknown_queue_ack_is_never_retried(self):
        for result in ({'ok':True}, {'queuedMessageId':'id'}, {'ok':True,'queuedMessageId':False}, None):
            with self.subTest(result=result):
                s = state()
                eid = str(__import__('uuid').uuid4())
                s['pending'][A['job_id']] = pending(job(A, False), event_id=eid)
                c = self.fake_client()
                c.evaluate.return_value = result
                with self.assertRaises(RuntimeError):
                    w.deliver(s, A)
                with self.assertRaises(RuntimeError):
                    w.deliver(s, A)
                self.assertEqual(c.evaluate.call_count, 1)
                self.assertEqual(s['dispatching'], eid)

    def test_explicit_idle_guard_deferral_can_retry_then_ack_once(self):
        s = state()
        s['pending'][A['job_id']] = pending(job(A, False))
        c = self.fake_client(results=[dict(ok=True, deferred=True, reason='target_not_idle'),
                                     dict(ok=True, queuedMessageId='offline-accepted')])
        self.assertFalse(w.deliver(s, A))
        self.assertIsNone(s['dispatching'])
        self.assertIn(A['job_id'], s['pending'])
        self.assertTrue(w.deliver(s, A))
        self.assertEqual(c.evaluate.call_count, 2)
        self.assertEqual(s['delivered_count'], 1)

    def test_orphaned_unknown_queued_and_corrupt_receipts_lock_current_main(self):
        s = state()
        s['pending'][A['job_id']] = pending(job(A, False))
        folder = self.root/'job-events'/EID
        folder.mkdir(parents=True)
        for text in (json.dumps({'status':'outcome_unknown_no_retry'}),
                     json.dumps({'status':'queued','queued_message_id':'old-ack'}), 'broken-json'):
            with self.subTest(text=text):
                (folder/'delivery.json').write_text(text)
                final, saved = self.run_main_once(s)
                self.assertEqual(final, s)
                self.assertTrue(any(n == 'status.json' and d.get('status') == 'delivery_needs_review' for n,d in saved))
                self.ssh.assert_not_called()
                self.client.assert_not_called()

    def test_legacy_current_pending_conflict_is_preserved_not_overwritten(self):
        s = state()
        s['pending'][A['job_id']] = pending(job(A), 'watchdog')
        before = copy.deepcopy(s)
        with self.assertRaisesRegex(ValueError, 'preserved legacy pending'):
            w.ingest(s, observation(job(A, False), job(B, False)), MULTI)
        self.assertEqual(s, before)

    def test_ast_has_no_main_watchdog_cpu_hub_or_model_poll_calls(self):
        source = (REAL_ROOT/'job_watch.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        main = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        main_calls = {n.func.id for n in ast.walk(main) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
        self.assertNotIn('watchdog', main_calls)
        self.assertNotIn('acknowledge', main_calls)
        self.assertNotIn('urlopen', source)
        self.assertNotIn('check_idle', source)
        self.assertNotIn('cpu_main_legacy', source)
        self.assertNotIn('probe_jobs.ps1', source)
        self.assertIsInstance(w.THREAD, str)


if __name__ == '__main__':
    unittest.main()
