"""Exact-process event monitoring for Codex Desktop (Windows/Python 3.11+)."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit
import watch
import job_watch

EXAMPLE = {
    'thread_id': 'REPLACE_WITH_CODEX_THREAD_ID',
    'cwd': r'E:\Projects\example',
    'cdp_url': 'http://127.0.0.1:9335',
    'runtime_dir': 'runtime',
    'poll_seconds': 15,
    'pwsh': 'pwsh',
    'target': {'mode': 'local', 'job_directory': r'E:\Jobs\example-run'},
}


def load_config(path):
    path = Path(path).resolve()
    data = job_watch.strict_json(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or set(data) != set(EXAMPLE):
        raise ValueError('Config fields must be: ' + ', '.join(EXAMPLE))
    for key in ('thread_id', 'cwd', 'cdp_url', 'runtime_dir', 'pwsh'):
        if not isinstance(data[key], str) or not data[key].strip() or any(c in data[key] for c in '\x00\r\n'):
            raise ValueError('Invalid config field: ' + key)
    if not Path(data['cwd']).is_absolute():
        raise ValueError('cwd must be an absolute Windows path')
    url = urlsplit(data['cdp_url'])
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('cdp_url must be a local HTTP endpoint')
    if url.port is None:
        raise ValueError('cdp_url requires an explicit port')
    if type(data['poll_seconds']) is not int or not 1 <= data['poll_seconds'] <= 3600:
        raise ValueError('poll_seconds must be an integer between 1 and 3600')
    runtime = Path(data['runtime_dir'])
    data['runtime_dir'] = str((path.parent / runtime).resolve())
    if Path(data['runtime_dir']) == watch.SOURCE:
        raise ValueError('runtime_dir must be separate from the source directory')
    data['target'] = job_watch.normalize_target(data['target'])
    return data


def configure(config):
    watch.configure(config)
    job_watch.ROOT = watch.ROOT
    job_watch.THREAD = watch.THREAD


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(watch.SOURCE / 'config.json'))
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'doctor', 'probe', 'stop'):
        commands.add_parser(name)
    run = commands.add_parser('run', help='Run in foreground and deliver terminal job events')
    run.add_argument('--enable-send', action='store_true', help='Explicitly authorize delivery to the configured Codex chat')
    args = parser.parse_args(argv)
    if args.command == 'run' and not args.enable_send:
        parser.error('run requires --enable-send; use probe for read-only observation')
    try:
        config_path = Path(args.config).resolve()
        if args.command == 'init':
            with config_path.open('x', encoding='utf-8') as stream:
                json.dump(EXAMPLE, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            emit({'created': str(config_path), 'next': 'Edit thread_id, cwd and target before run'})
            return 0
        config = load_config(config_path)
        configure(config)
        if args.command == 'probe':
            observation = job_watch.observe(config['target'])
            emit(observation)
            return 0 if all(j['valid'] for j in observation['jobs']) else 1
        if args.command == 'doctor':
            report = {'python': sys.version.split()[0], 'platform': sys.platform,
                      'pwsh': shutil.which(config['pwsh']),
                      'ssh': shutil.which('ssh'), 'runtime_dir': config['runtime_dir'],
                      'thread_configured': config['thread_id'] != EXAMPLE['thread_id'],
                      'cwd_exists': Path(config['cwd']).is_dir(), 'watchdog_enabled': False,
                      'sends_messages': False}
            try:
                desktop = watch.client()
                report['desktop'] = desktop.probe()
                target = desktop.evaluate(watch.target_probe_expression())
                if not isinstance(target, dict) or target.get('ok') is not True or target.get('threadId') != config['thread_id']:
                    raise RuntimeError('Target chat/queue inspection failed')
                report['target'] = target
                report['desktop_ok'] = True
            except Exception as exc:
                report['desktop_ok'] = False
                report['desktop_error'] = str(exc)
            emit(report)
            remote = config['target']['mode'] != 'local'
            return 0 if report['desktop_ok'] and report['pwsh'] and report['thread_configured'] and report['cwd_exists'] and (not remote or report['ssh']) else 1
        if args.command == 'stop':
            if watch.ROOT.is_dir():
                (watch.ROOT / 'STOP').write_text('Stop requested\n', encoding='utf-8')
                emit({'stop_requested': True, 'runtime_dir': str(watch.ROOT)})
            else:
                emit({'stop_requested': False, 'reason': 'Runtime directory does not exist'})
            return 0
        if config['thread_id'] == EXAMPLE['thread_id']:
            raise ValueError('Set thread_id before enabling delivery')
        if not Path(config['cwd']).is_dir():
            raise ValueError('Configured cwd does not exist')
        watch.ROOT.mkdir(parents=True, exist_ok=True)
        # The lock is acquired by the watcher before runtime configuration is written.
        return job_watch.main(config['target']) or 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
