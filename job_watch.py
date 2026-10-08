"""Exact-run completion watcher; no watchdog, CPU gate, or model polling."""
import base64
import json
import msvcrt
import os
import re
import subprocess
import time
import uuid
from pathlib import Path, PureWindowsPath
from watch import ROOT, THREAD, SOURCE, SETTINGS, client, guarded_expression, save

FIELDS = ('state', 'child_exit_code', 'wrapper_exit_code', 'scientific_status')
WATCHDOG_SECONDS = 0  # Deliberately disabled; only exact job events trigger delivery.
TERMINAL_STATES = frozenset(('program_completed', 'program_failed', 'wrapper_failed',
    'processes_exited', 'completed', 'failed', 'stopped', 'exited', 'finished',
    'cancelled', 'aborted', 'error', 'required_failure'))
EVENT_REASONS = frozenset(('status', 'exit', 'required_failure'))
REMOTE_FIELDS = frozenset(('mode', 'ssh_host', 'job_directory', 'job_id', 'job_name',
    'probe_file', 'owned_file', 'summary_file', 'launch_file', 'driver_exit_file'))


def strict_json(text):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique_object)


def watchdog(state, observation, now=None):
    """Compatibility no-op: never create, delete, or rearm historical events."""


def acknowledge(state, key, now=None):
    """Compatibility no-op: queue acknowledgement does not arm a timer."""


def signature(job):
    return {key: job.get(key) for key in FIELDS}


def event_kind(job):
    if job.get('valid') is not True:
        return None
    state = (job.get('state') or '').lower()
    scientific = (job.get('scientific_status') or '').upper()
    failed = (job.get('required_failure') is True or state in
              ('program_failed', 'wrapper_failed', 'failed', 'aborted', 'error', 'required_failure')
              or scientific in ('ERROR', 'REQUIRED_FAILURE') or 'FAILED' in scientific.split('_')
              or any(isinstance(job.get(k), int) and not isinstance(job[k], bool) and job[k] != 0
                     for k in ('child_exit_code', 'wrapper_exit_code')))
    if failed:
        return 'required_failure'
    processes = job.get('processes', [])
    if (job.get('processes_known') is True and processes
            and all(p.get('alive') is False for p in processes)):
        return 'exit'
    return 'status' if state in TERMINAL_STATES else None


def detect(previous, job):
    """One completion episode per exact run ID; healthy phase changes stay quiet."""
    current = event_kind(job)
    if current is None or (previous is not None and event_kind(previous) is not None):
        return None
    return current


def normalize_target(data):
    if not isinstance(data, dict):
        raise ValueError('Target must be an object')
    mode = data.get('mode')
    if mode == 'remote-list':
        if set(data) != {'mode', 'targets'} or not isinstance(data['targets'], list) or not data['targets']:
            raise ValueError('remote-list requires only mode and nonempty targets')
        targets = []
        for item in data['targets']:
            item = normalize_target(item)
            if item['mode'] != 'remote':
                raise ValueError('remote-list entries must be exact remote targets')
            targets.append(item)
        ids = [item['job_id'].casefold() for item in targets]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate target job ID')
        return {'mode': mode, 'targets': targets}
    if mode == 'remote':
        if set(data) != REMOTE_FIELDS:
            raise ValueError('Unknown or missing remote target field')
        if not isinstance(data['ssh_host'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', data['ssh_host']):
            raise ValueError('Unsupported SSH target')
        for key in REMOTE_FIELDS - {'mode', 'ssh_host'}:
            if not isinstance(data[key], str) or not data[key] or any(c in data[key] for c in '\x00\r\n'):
                raise ValueError('Invalid target field: ' + key)
        directory = PureWindowsPath(data['job_directory'])
        if not directory.is_absolute():
            raise ValueError('Remote job_directory must be absolute')
        for key in ('probe_file', 'owned_file', 'summary_file', 'launch_file', 'driver_exit_file'):
            relative = PureWindowsPath(data[key])
            if not relative.parts or relative.drive or relative.root or '..' in relative.parts:
                raise ValueError('Run evidence must be relative and inside job_directory')
        return {**data, 'job_directory': str(directory)}
    if mode == 'local':
        if set(data) != {'mode', 'job_directory'} or not isinstance(data['job_directory'], str):
            raise ValueError('Unknown or missing local target field')
        directory = Path(data['job_directory'])
        if not directory.is_absolute():
            raise ValueError('Local job_directory must be absolute')
        return {'mode': 'local', 'job_directory': str(directory.resolve())}
    raise ValueError('Explicit local, remote, or remote-list target required')


def target_config():
    path = ROOT / 'watch-target.json'
    if not path.exists():
        raise ValueError('Explicit watch-target.json required; broad legacy discovery is disabled')
    return normalize_target(strict_json(path.read_text(encoding='utf-8-sig')))


def target_key(target):
    if target['mode'] == 'local':
        return 'local:' + target['job_directory']
    return target['job_id'] if target['mode'] == 'remote' else None


def target_keys(target):
    target = normalize_target(target)
    return tuple(t['job_id'] for t in target['targets']) if target['mode'] == 'remote-list' else (target_key(target),)


def eligible_events(state, target):
    keys = set(target_keys(target))
    return [(key, event) for key, event in state['pending'].items()
            if key in keys and event.get('reason') in EVENT_REASONS
            and isinstance(event.get('job'), dict) and event['job'].get('id') == key
            and event_kind(event['job']) is not None]


def delivery_review(state, target):
    """Read-only classification; never clear/replay/migrate an uncertain delivery."""
    keys = set(target_keys(target))
    current, inactive, unknown = [], [], []
    pointer = state.get('dispatching')
    if pointer:
        matches = [(key, event) for key, event in state['pending'].items()
                   if event.get('event_id') == pointer]
        if len(matches) != 1 or matches[0][1].get('job', {}).get('id') != matches[0][0]:
            unknown.append({'event_id': pointer, 'source': 'dispatching', 'retry_allowed': False})
        else:
            key = matches[0][0]
            record = {'event_id': pointer, 'job_id': key, 'source': 'dispatching',
                      'review_path': str(ROOT / 'job-events' / pointer / 'delivery.json'), 'retry_allowed': False}
            (current if key in keys else inactive).append(record)
    unresolved = state.get('unresolved_delivery_events', {})
    if not isinstance(unresolved, dict):
        unknown.append({'source': 'unresolved_delivery_events', 'retry_allowed': False})
    else:
        for event_id, event in unresolved.items():
            key = event.get('job_id') if isinstance(event, dict) else None
            record = {'event_id': event_id, 'job_id': key, 'source': 'unresolved_delivery_events',
                      'review_path': str(ROOT / 'job-events' / event_id / 'delivery.json'), 'retry_allowed': False}
            (current if key in keys else inactive if isinstance(key, str) and key else unknown).append(record)
    # A lost/cleared global pointer must not permit replay of an on-disk unknown
    # outcome or queued acknowledgement. Read only current pending receipts.
    for key, event in state['pending'].items():
        if key not in keys:
            continue
        event_id = event.get('event_id')
        if not isinstance(event_id, str) or not event_id or any(c in event_id for c in '/\\\x00\r\n') or event_id in ('.', '..'):
            unknown.append({'job_id': key, 'source': 'invalid_pending_event_id', 'retry_allowed': False})
            continue
        path = ROOT / 'job-events' / event_id / 'delivery.json'
        if path.exists():
            try:
                receipt = strict_json(path.read_text(encoding='utf-8'))
                safe = isinstance(receipt, dict) and receipt.get('status') == 'deferred'
            except (ValueError, OSError):
                safe = False
            if not safe:
                current.append({'event_id': event_id, 'job_id': key, 'source': 'delivery_receipt',
                                'review_path': str(path), 'retry_allowed': False})
    return {'blocking': bool(current or unknown), 'current': current, 'inactive': inactive, 'unknown': unknown}


def remote_probe_command(target):
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    parameters = {'JobDirectory': 'job_directory', 'OwnedProcessesFile': 'owned_file',
                  'SummaryFile': 'summary_file', 'LaunchFile': 'launch_file',
                  'DriverExitFile': 'driver_exit_file', 'HostAlias': 'ssh_host',
                  'JobId': 'job_id', 'JobName': 'job_name'}
    probe_path = str(PureWindowsPath(target['job_directory']) / target['probe_file'])
    invocation = ' '.join('-' + name + ' ' + quote(target[key]) for name, key in parameters.items())
    return '& ' + quote(probe_path) + ' ' + invocation


def validate_observation(data, expected_keys=None):
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), list) or not isinstance(data.get('observed_at'), str) or not data['observed_at']:
        raise ValueError('Invalid job observation')
    ids = []
    for job in data['jobs']:
        if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not job['id'] or not isinstance(job.get('valid'), bool):
            raise ValueError('Invalid job identity or validity')
        ids.append(job['id'])
        if not job['valid']:
            continue
        if not isinstance(job.get('name'), str) or not isinstance(job.get('processes_known'), bool) or not isinstance(job.get('processes'), list):
            raise ValueError('Unknown job or process layout')
        identities = []
        for p in job['processes']:
            if not isinstance(p, dict) or not isinstance(p.get('identity'), str) or not p['identity'] or not isinstance(p.get('alive'), bool):
                raise ValueError('Unknown exact process identity or state')
            identity = p['identity'].split(':')
            if len(identity) != 2 or not all(v.isascii() and v.isdigit() and int(v) > 0 for v in identity):
                raise ValueError('Process identity must be positive PID:UTC-start-ticks')
            identities.append(p['identity'])
        if len(identities) != len(set(identities)) or (job['processes_known'] and not identities):
            raise ValueError('Duplicate or empty known process identities')
        for key in ('state', 'scientific_status'):
            if job.get(key) is not None and not isinstance(job[key], str):
                raise ValueError('Unknown status layout')
        for key in ('child_exit_code', 'wrapper_exit_code'):
            if job.get(key) is not None and (not isinstance(job[key], int) or isinstance(job[key], bool)):
                raise ValueError('Unknown exit-code layout')
        if 'required_failure' in job and not isinstance(job['required_failure'], bool):
            raise ValueError('Unknown required_failure layout')
    if len(ids) != len(set(i.casefold() for i in ids)):
        raise ValueError('Duplicate observed job IDs')
    if expected_keys is not None and (len(ids) != len(expected_keys) or set(ids) != set(expected_keys)):
        raise ValueError('Probe returned a different run or incomplete target set')
    return data


def observe(target=None):
    target = target_config() if target is None else normalize_target(target)
    if target['mode'] == 'remote-list':
        # All probes must succeed before any job is ingested or any event delivered.
        parts = [observe(item) for item in target['targets']]
        combined = {'observed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'jobs': [job for part in parts for job in part['jobs']],
                    'target_observations': [{'job_id': target_key(item), 'observed_at': part['observed_at']}
                                            for item, part in zip(target['targets'], parts)]}
        return validate_observation(combined, target_keys(target))
    if target['mode'] == 'local':
        args = [SETTINGS.get('pwsh', 'pwsh'), '-NoProfile', '-NonInteractive',
                '-File', str(SOURCE / 'probe_local_job.ps1'), '-JobDirectory', target['job_directory']]
    else:
        encoded = base64.b64encode(remote_probe_command(target).encode('utf-16-le')).decode('ascii')
        args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5',
                '-o', 'ServerAliveCountMax=2', target['ssh_host'], 'powershell', '-NoProfile',
                '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned', '-EncodedCommand', encoded]
    response = subprocess.run(args, capture_output=True, encoding='utf-8', errors='replace', timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    if response.returncode:
        raise RuntimeError(response.stderr[-1000:])
    return validate_observation(strict_json(response.stdout.lstrip('\ufeff').strip()), target_keys(target))


def ingest(state, observation, target=None):
    validate_observation(observation, target_keys(target) if target is not None else None)
    updates = []
    for job in observation['jobs']:
        if not job['valid']:
            continue
        reason = detect(state['jobs'].get(job['id']), job)
        event = None
        if reason:
            old = state['pending'].get(job['id'])
            if old and (old.get('reason') not in EVENT_REASONS or event_kind(old.get('job', {})) is None):
                raise ValueError('Current terminal event conflicts with preserved legacy pending; review required')
            event = dict(old) if old else {'event_id': str(uuid.uuid4())}
            event.update(job=job, reason=reason, observed_at=observation['observed_at'], event_version=2)
        updates.append((job, event))
    for job, event in updates:
        if event is not None:
            state['pending'][job['id']] = event
        state['jobs'][job['id']] = job


def deliver(state, target=None):
    target = target_config() if target is None else normalize_target(target)
    review = delivery_review(state, target)
    if review['blocking']:
        raise RuntimeError('Current or unidentified delivery outcome needs review; no retry')
    if state.get('dispatching'):
        # Observe inactive historical runs quietly, but never overwrite their pointer.
        # A human-reviewed migration receipt must preserve it before clearing it.
        return False
    eligible = eligible_events(state, target)
    if not eligible:
        return False
    key, event = eligible[0]
    folder = ROOT / 'job-events' / event['event_id']
    delivery_path = folder / 'delivery.json'
    if delivery_path.exists():
        previous_delivery = json.loads(delivery_path.read_text(encoding='utf-8'))
        if previous_delivery.get('status') != 'deferred':
            raise RuntimeError('Existing queued or unknown delivery receipt forbids retry')
    c = client()
    c.probe()
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / 'evidence.json', event)
    prompt = ('计算作业进程已退出' if event['reason'] == 'exit' else
              '计算作业记录明确失败' if event['reason'] == 'required_failure' else '计算作业已到终端状态')
    prompt += '（' + event['job']['name'] + '）。'
    state['dispatching'] = event['event_id']
    save(ROOT / 'job-state.json', state)
    try:
        result = c.evaluate(guarded_expression(prompt, event['event_id']))
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise RuntimeError('Unknown queue result')
        if result.get('deferred'):
            save(delivery_path, {'status': 'deferred', 'at': time.time(), 'target': result})
            state['dispatching'] = None
            save(ROOT / 'job-state.json', state)
            return False
        if not isinstance(result.get('queuedMessageId'), str) or not result['queuedMessageId']:
            raise RuntimeError('Missing queue acknowledgement')
        save(delivery_path, {'status': 'queued', 'at': time.time(), 'message': prompt,
                            'queued_message_id': result['queuedMessageId'], 'thread_id': THREAD})
        del state['pending'][key]
        state['dispatching'] = None
        state['delivered_count'] += 1
        save(ROOT / 'job-state.json', state)
        return True
    except Exception as exc:
        save(delivery_path, {'status': 'outcome_unknown_no_retry', 'error': str(exc)})
        raise


def target_run_fields(target):
    return {'target_mode': target['mode'], 'target_job_ids': list(target_keys(target)),
            'job_directory': target.get('job_directory'), 'target_job_id': target_key(target),
            'ssh_host': target.get('ssh_host')}


def main(initial_target=None):
    if initial_target is None and not (ROOT / 'ENABLED').exists():
        raise SystemExit('Monitor is not enabled')
    with (ROOT / 'watch.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        if initial_target is not None:
            save(ROOT / 'watch-target.json', normalize_target(initial_target))
        if (ROOT / 'STOP').exists():
            (ROOT / 'STOP').unlink()
        path = ROOT / 'job-state.json'
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'jobs': {}, 'pending': {}, 'dispatching': None, 'delivered_count': 0}
        target = target_config()
        save(ROOT / 'run.json', {'pid': os.getpid(), 'started_at': time.time(), 'mode': 'job-events',
                                'thread_id': THREAD, 'poll_seconds': SETTINGS.get('poll_seconds', 15), 'watchdog_seconds': None,
                                'watchdog_disabled': True, **target_run_fields(target)})
        while not (ROOT / 'STOP').exists():
            configured_target = target_config()
            if configured_target != target:
                target = configured_target
                run = json.loads((ROOT / 'run.json').read_text(encoding='utf-8'))
                run.update(target_run_fields(target))
                save(ROOT / 'run.json', run)
            review = delivery_review(state, target)
            if review['blocking']:
                save(ROOT / 'status.json', {'status': 'delivery_needs_review', 'at': time.time(),
                                           'delivery_review': review})
                return 2
            try:
                observation = observe(target)
                ingest(state, observation, target)
                save(path, state)
                save(ROOT / 'job-observation.json', observation)
                sent = deliver(state, target)
                save(ROOT / 'status.json', {'status': 'monitoring_job_events', 'at': time.time(),
                    'job_count': len(state['jobs']), 'pending_count': len(state['pending']),
                    'delivered_count': state['delivered_count'], 'sent_this_check': sent,
                    **target_run_fields(target), 'target_pending_count': len(eligible_events(state, target)),
                    'delivery_review': delivery_review(state, target),
                    'legacy_pointer_migration_required': bool(state.get('dispatching')),
                    'invalid_job_count': sum(not j.get('valid') for j in observation['jobs'])})
            except Exception as exc:
                save(ROOT / 'status.json', {'status': 'observation_or_delivery_error', 'at': time.time(), 'error': str(exc)})
            for _ in range(SETTINGS.get('poll_seconds', 15)):
                if (ROOT / 'STOP').exists():
                    break
                time.sleep(1)
        save(ROOT / 'status.json', {'status': 'stopped', 'at': time.time()})


if __name__ == '__main__':
    main()
