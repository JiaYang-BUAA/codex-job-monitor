"""Read-only probe regression checks using disposable local run evidence."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / 'probe_remote_target.ps1'
PWSH = shutil.which('pwsh') or r'C:\Program Files\PowerShell\7\pwsh.exe'


class RemoteProbeTests(unittest.TestCase):
    def run_probe(self, preparation=''):
        with tempfile.TemporaryDirectory(prefix='remote-probe-test-', dir=ROOT) as folder:
            # The pwsh process stays alive while it probes its own identity.
            script = r'''
$ErrorActionPreference='Stop'
$testRoot=__DIRECTORY__
$self=Get-Process -Id $PID
$identity=@{pid=$PID;start_ticks=$self.StartTime.ToUniversalTime().Ticks}
$launch=@{pid=$identity.pid;start_ticks=$identity.start_ticks;started_utc='2026-10-01T17:10:09Z'}
$owned=@{guard_pid=$PID;processes=@($identity)}
$summary=@{status='WALL_LIMIT_NOT_ACCEPTED';actual_iteration=2808;accepted=$false;wall_deadline_hit=$true}
$driverExit=$null
__PREPARATION__
$launch | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $testRoot 'launch.json') -Encoding UTF8
if($null -ne $owned){$owned | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $testRoot 'owned-processes.json') -Encoding UTF8}
if($null -ne $summary){$summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $testRoot 'summary.json') -Encoding UTF8}
if($null -ne $driverExit){$driverExit | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $testRoot 'driver-exit.json') -Encoding UTF8}
if($malformedOwned){Set-Content -LiteralPath (Join-Path $testRoot 'owned-processes.json') -Value '{broken' -Encoding UTF8}
if($denyIdentity){function Get-Process {throw [UnauthorizedAccessException]::new('Test process access denied')}}
& __PROBE__ -JobDirectory $testRoot -SummaryFile 'summary.json' -JobId 'remote:test:run' -JobName 'single-test-run'
'''
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            script = (script.replace('__DIRECTORY__', quote(folder))
                      .replace('__PROBE__', quote(PROBE))
                      .replace('__PREPARATION__', preparation))
            result = subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-Command', script],
                                    capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            observation = json.loads(result.stdout.lstrip('\ufeff'))
            self.assertEqual(len(observation['jobs']), 1)
            self.assertTrue(observation['observed_at'])
            return observation['jobs'][0]

    def test_live_identity_and_schema(self):
        job = self.run_probe()
        for key in ('id', 'name', 'valid', 'state', 'started_utc', 'finished_utc',
                    'child_exit_code', 'wrapper_exit_code', 'scientific_status',
                    'processes_known', 'processes'):
            self.assertIn(key, job)
        self.assertTrue(job['valid'])
        self.assertEqual(job['state'], 'running')
        self.assertTrue(job['processes_known'])
        self.assertEqual(len(job['processes']), 1)
        self.assertTrue(job['processes'][0]['alive'])

    def test_reused_pid_does_not_keep_old_identity_alive(self):
        job = self.run_probe('$identity.start_ticks += 1; $launch.start_ticks += 1')
        self.assertTrue(job['valid'])
        self.assertTrue(job['processes_known'])
        self.assertFalse(job['processes'][0]['alive'])
        self.assertEqual(job['state'], 'processes_exited')

    def test_missing_pid_is_verified_exit_but_not_scientific_acceptance(self):
        job = self.run_probe("$identity.pid=2147483647; $launch.pid=2147483647; $owned.guard_pid=2147483647; "
                             "$driverExit=@{pid=2147483647;exit_code=0;exited_utc='2026-10-01T19:10:48Z'}")
        self.assertTrue(job['valid'])
        self.assertEqual(job['state'], 'processes_exited')
        self.assertEqual(job['child_exit_code'], 0)
        self.assertIsNone(job['wrapper_exit_code'])
        self.assertEqual(job['scientific_status'], 'WALL_LIMIT_NOT_ACCEPTED')
        self.assertFalse(job['scientific_accepted'])
        self.assertEqual(job['actual_iteration'], 2808)

    def test_missing_owned_record_cannot_certify_exit(self):
        job = self.run_probe('$identity.pid=2147483647; $launch.pid=2147483647; $owned=$null; $summary=$null')
        self.assertTrue(job['valid'])
        self.assertFalse(job['processes_known'])
        self.assertEqual(job['state'], 'running')

    def test_invalid_json_cannot_certify_exit(self):
        job = self.run_probe('$malformedOwned=$true')
        self.assertFalse(job['valid'])
        self.assertFalse(job['processes_known'])
        self.assertIsNone(job['state'])

    def test_process_access_denied_cannot_certify_exit(self):
        job = self.run_probe('$denyIdentity=$true')
        self.assertFalse(job['valid'])
        self.assertFalse(job['processes_known'])
        self.assertIn('access denied', job['error'])

    def test_missing_guard_identity_cannot_certify_exit(self):
        job = self.run_probe('$owned.guard_pid=2147483647')
        self.assertFalse(job['valid'])
        self.assertFalse(job['processes_known'])

    def test_exit_record_for_other_run_is_invalid(self):
        job = self.run_probe("$driverExit=@{pid=2147483647;exit_code=0;exited_utc='2026-10-01T19:10:48Z'}")
        self.assertFalse(job['valid'])
        self.assertIn('does not match', job['error'])


if __name__ == '__main__':
    unittest.main()
