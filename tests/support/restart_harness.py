"""Offline restart fixture, runnable both as a subprocess and inside the test image."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time

import httpx
from tests.support.fakes import FakeEngine, FakeImmich, asset, tag
from app.english_cleanup import EnglishTagCleaner
from app.cleanup_queues import CleanupQueues
from app.config import Settings
from app.logging import setup_logging
from app.main import parse_arguments, run_service
from app.processor import ImmichAutoTagger
from app.task_store import TaskStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--block', default='')
    parser.add_argument('--service', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--maintenance', action='store_true')
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    remote_path = directory / 'remote.json'
    saved = json.loads(remote_path.read_text()) if remote_path.exists() else None
    settings = Settings(_env_file=None, immich_base_url='http://immich.test', immich_api_key='restart-test-secret',
                        state_dir=directory / 'state', model_cache_dir=directory / 'models', batch_size=2,
                        max_retries=0, log_progress_interval_seconds=1, log_slow_operation_seconds=2,
                        cleanup_admin_api_key='restart-admin-secret' if args.maintenance else '')
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
            initial_tags = list(map(tag, ['blue_hair', '属性/蓝发', 'auto:processed', 'manual'])) if args.cleanup else []
            super().__init__(saved['assets'].values() if saved else [asset(i, tags=initial_tags) for i in range(4)])
            self.searches = saved['searches'] if saved else 0
            self.inferences = saved['inferences'] if saved else []
            self.assignments = saved['assignments'] if saved else []
            self.removals = saved.get('removals', []) if saved else []
            if saved:
                self.tags = saved['tags']
                self.queues = saved['queues']
            elif args.maintenance:
                self.queues['sidecar']['isPaused'] = True

        def save(self):
            temporary = remote_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'assets': self.assets, 'tags': self.tags, 'searches': self.searches,
                                              'inferences': self.inferences, 'assignments': self.assignments,
                                              'removals': self.removals, 'queues': self.queues}))
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
            if request.method == 'DELETE':
                self.removals.append({'path': request.url.path, 'body': json.loads(request.content)})
                self.save()
                block('cleanup-deleted')
            if request.method == 'PUT' and request.url.path.startswith('/api/queues/'):
                self.save()
                name = request.url.path.rsplit('/', 1)[-1]
                paused = json.loads(request.content)['isPaused']
                block('queue-paused-' + name if paused else 'queue-resumed-' + name)
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
    if args.maintenance:
        CleanupQueues.poll_interval = 0
        original_save = CleanupQueues._save
        def save_intent(queues, state):
            original_save(queues, state)
            if state['phase'] == 'pausing':
                block('queue-intent')
        CleanupQueues._save = save_intent
    if args.cleanup:
        import app.english_cleanup as cleanup
        original_journal = cleanup.record_cleanup
        def journal(*a, **kw):
            original_journal(*a, **kw)
            if kw['status'] == 'prepared':
                block('cleanup-prepared')
        cleanup.record_cleanup = journal
        worker = EnglishTagCleaner(settings, cleanup_scope='catalog', dry_run=False,
                                   confirm_cleanup_english=True, maintenance=args.maintenance, client_factory=server.factory,
                                   engine_factory=lambda _: Engine())
        # This fake server has no asynchronous metadata jobs; exercise persistence without real delays.
        worker._wait_for_readback = lambda attempt: None
        worker._wait_between_removals = lambda: None
    else:
        worker = ImmichAutoTagger(settings, client_factory=server.factory, engine_factory=lambda _: Engine())
    try:
        if args.service:
            flags = (['--mode', 'cleanup-english', '--cleanup-scope', 'catalog', '--confirm-cleanup-english']
                     if args.cleanup else ['--mode', 'continuous'])
            if args.maintenance:
                flags.append('--cleanup-maintenance')
            return asyncio.run(run_service(worker, parse_arguments([*flags, '--limit', '3'])))
        signal.signal(signal.SIGTERM, lambda *_: worker.cancelled.set())
        worker.run(limit=3)
        return 0
    finally:
        worker.close()


if __name__ == '__main__':
    sys.exit(main())
