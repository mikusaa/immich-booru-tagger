"""Offline restart fixture, runnable both as a subprocess and inside the test image."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from conftest import FakeEngine, FakeImmich, asset
from immich_tagger.config import Settings
from immich_tagger.logging import setup_logging
from immich_tagger.main import parse_arguments, run_service
from immich_tagger.processor import ImmichAutoTagger
from immich_tagger.task_store import TaskStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--block', default='')
    parser.add_argument('--service', action='store_true')
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    remote_path = directory / 'remote.json'
    saved = json.loads(remote_path.read_text()) if remote_path.exists() else None
    settings = Settings(_env_file=None, immich_base_url='http://immich.test', immich_api_key='restart-test-secret',
                        state_dir=directory / 'state', model_cache_dir=directory / 'models', batch_size=2,
                        max_retries=0, log_progress_interval_seconds=1, log_slow_operation_seconds=2)
    setup_logging('INFO', settings.timezone, [settings.immich_api_key])
    blocked = False
    worker = None

    def block(stage):
        nonlocal blocked
        if args.block != stage or blocked:
            return
        blocked = True
        (directory / 'blocked').write_text(stage)
        while not worker.cancelled.is_set():
            time.sleep(.02)

    class Server(FakeImmich):
        def __init__(self):
            super().__init__(saved['assets'].values() if saved else [asset(i) for i in range(4)])
            self.searches = saved['searches'] if saved else 0
            self.inferences = saved['inferences'] if saved else []
            self.assignments = saved['assignments'] if saved else []
            if saved:
                self.tags = saved['tags']

        def save(self):
            temporary = remote_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'assets': self.assets, 'tags': self.tags, 'searches': self.searches,
                                              'inferences': self.inferences, 'assignments': self.assignments}))
            os.replace(temporary, remote_path)

        def handle(self, request):
            if request.url.path == '/api/search/metadata':
                self.searches += 1
                self.save()
                if self.searches == 2:
                    block('scan')
            response = super().handle(request)
            if request.url.path.endswith('/thumbnail'):
                response = httpx.Response(200, content=request.url.path.split('/')[-2].encode())
            if request.method == 'PUT' and request.url.path == '/api/tags/assets':
                body = json.loads(request.content)
                self.assignments.append(body)
                self.save()
                marker = 'tag-' + settings.processed_tag_name in body['tagIds']
                block('marker' if marker else 'tags')
            self.save()
            return response

    server = Server()

    class Engine(FakeEngine):
        def prepare(self):
            block('model')
            super().prepare()

        def predict_tags(self, image):
            server.inferences.append(image.decode())
            server.save()
            if image == b'1':
                block('inference')
            return super().predict_tags(image)

    original_finish = TaskStore.finish_item
    def finish(store, *a, **kw):
        block('checkpoint')
        result = original_finish(store, *a, **kw)
        block('after_checkpoint')
        return result
    TaskStore.finish_item = finish
    worker = ImmichAutoTagger(settings, client_factory=server.factory, engine_factory=lambda _: Engine())
    try:
        if args.service:
            return asyncio.run(run_service(worker, parse_arguments(['--mode', 'continuous', '--limit', '3'])))
        signal.signal(signal.SIGTERM, lambda *_: worker.cancelled.set())
        worker.run(limit=3)
        return 0
    finally:
        worker.close()


if __name__ == '__main__':
    sys.exit(main())
