#!/usr/bin/env python3
"""Offline Docker stop/kill recovery acceptance; only disposable labeled test containers."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def check(image, scenario):
    with tempfile.TemporaryDirectory(prefix='tagger-restart-') as directory:
        root = Path(directory)
        name = 'tagger-restart-' + uuid.uuid4().hex[:10]
        user = f'{os.getuid()}:{os.getgid()}'
        stage = {'stop': 'inference', 'kill': 'marker', 'scan': 'scan'}[scenario]
        try:
            docker('run', '-d', '--name', name, '--network', 'none', '--user', user,
                   '--label', 'immich-tagger.restart-test=true', '-v', f'{root}:/fixture', image,
                   'python', 'tests/restart_harness.py', '/fixture', '--service', '--block', stage)
            deadline = time.monotonic() + 30
            while not (root / 'blocked').exists() and time.monotonic() < deadline:
                status = json.loads(docker('inspect', '--format', '{{json .State}}', name))
                if not status['Running']:
                    raise RuntimeError(docker('logs', name))
                time.sleep(.2)
            assert (root / 'blocked').exists(), docker('logs', name)
            metrics = json.loads(docker('exec', name, 'python', '-c',
                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())"))
            assert metrics['running']
            assert metrics['progress']['operation'] is not None
            if scenario == 'stop':
                docker('stop', '--time', '10', name)
                assert docker('inspect', '--format', '{{.State.ExitCode}}', name) == '130'
            else:
                docker('kill', '--signal', 'KILL', name)
            before = json.loads((root / 'remote.json').read_text())
            docker('run', '--rm', '--network', 'none', '--user', user, '-v', f'{root}:/fixture', image,
                             'python', 'tests/restart_harness.py', '/fixture', '--service')
            after = json.loads((root / 'remote.json').read_text())
            snapshot = json.loads(docker('run', '--rm', '--network', 'none', '--user', user, '-v', f'{root}:/fixture', image,
                'python', '-c', "import json; from immich_tagger.task_store import TaskStore; "
                               "print(json.dumps(TaskStore.snapshot('/fixture/state')))"))
            assert snapshot['progress']['completed'] == snapshot['progress']['total'] == 3
            assert snapshot['progress']['task_status'] == 'completed'
            assert not after['assets']['3']['tags']
            if scenario != 'scan':
                assert after['searches'] == before['searches']
                assert after['inferences'].count('0') == 1
            else:
                assert after['searches'] > before['searches']
            print(f'{scenario}: PASS — completed 3/3, prior queue recovered')
        finally:
            # Only the randomly named container created by this invocation is removed.
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='immich-booru-tagger:progress-test')
    args = parser.parse_args()
    for scenario in ('stop', 'kill', 'scan'):
        check(args.image, scenario)


if __name__ == '__main__':
    main()
