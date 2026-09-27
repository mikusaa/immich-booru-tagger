"""Shared processing pipeline for single, continuous, scheduled and translation runs."""
import json
import logging
import tempfile
import threading
import time
from contextlib import ExitStack

from .config import get_settings
from .failure_tracker import FailureTracker
from .immich_client import ImmichClient
from .models import Asset, AssetProcessingResult, RunResult
from .state import account_scope, record_assignment, writer_lock
from .translation_catalog import TranslationCatalog


class ProcessorError(RuntimeError):
    pass


class ImmichAutoTagger:
    def __init__(self, settings=None, *, dry_run=False, client_factory=ImmichClient, engine_factory=None):
        self.settings = settings or get_settings()
        self.dry_run = dry_run
        self.logger = logging.getLogger("processor")
        self.clients = [client_factory(self.settings, account, dry_run=dry_run)
                        for account in self.settings.get_library_config()]
        self.engine_factory = engine_factory
        self._engine = None
        self._catalog = None
        self._metrics_lock = threading.Lock()
        self.running = False
        self.last_error = None
        self.last_result = RunResult()
        self.cancelled = threading.Event()

    @property
    def engine(self):
        if self._engine is None:
            from .tagging_engine import create_tagging_engine
            self._engine = (self.engine_factory or create_tagging_engine)(self.settings)
        return self._engine

    @property
    def catalog(self):
        if self._catalog is None and self.settings.translations_enabled:
            self._catalog = TranslationCatalog(self.settings.translation_file, self.settings.translation_overrides)
        return self._catalog

    def failure_tracker(self, client, backfill=False):
        scope = account_scope(self.settings, client.account)
        return FailureTracker(scope + ("-zh" if backfill else ""), settings=self.settings)

    def _paths(self, client, asset, backfill):
        if backfill:
            names = []
            for tag in asset.tags or []:
                name = tag.path
                prefix, separator, leaf = name.partition("/")
                if (name == self.settings.processed_tag_name or prefix == "zh"
                        or prefix in self.catalog.categories.values()):
                    continue
                if separator and prefix in ("general", "character", "rating"):
                    name = leaf
                translated = self.catalog.translate(name)
                if translated:
                    names.append(translated)
            return list(dict.fromkeys(names))
        predictions = self.engine.predict_tags(client.download_asset(asset.id))
        names = [prediction.name for prediction in predictions]
        if self.catalog:
            for prediction in predictions:
                translated = self.catalog.translate(prediction.name)
                if translated:
                    names.append(translated)
        return list(dict.fromkeys(names))

    def process_asset(self, client, asset, *, backfill=False):
        started = time.monotonic()
        result = AssetProcessingResult(asset_id=asset.id)
        try:
            if asset.tags is None:
                asset = client.get_asset(asset.id)
            if not backfill and any(t.path == self.settings.processed_tag_name for t in asset.tags or []):
                result.success, result.status = True, "skipped"
                return result
            paths = self._paths(client, asset, backfill)
            current = client.get_asset(asset.id) if not self.dry_run else asset
            if current.type != "IMAGE" or current.isOffline or current.isTrashed:
                result.success, result.status = True, "skipped"
                return result
            included = self.settings.immich_include_library_ids
            if (included and current.libraryId not in included) or current.libraryId in self.settings.immich_exclude_library_ids:
                raise ProcessorError("Asset moved outside the configured scope")
            if current.tags is None:
                raise ProcessorError("Asset response omitted tags")
            existing = {tag.path for tag in current.tags}
            missing = [path for path in paths if path not in existing]
            if self.dry_run:
                result.success, result.status = True, "planned"
                result.tags_assigned = missing
                self.logger.info("Preview %s: %s", asset.id, json.dumps(missing, ensure_ascii=False))
                return result
            if missing:
                mapping = client.get_or_create_tags_bulk(missing)
                if set(mapping) != set(missing):
                    raise ProcessorError("Not all requested tags were resolved")
                client.tag_single_asset(asset.id, [mapping[name].id for name in missing])
                record_assignment(self.settings.state_dir, account_scope(self.settings, client.account), asset.id, missing)
            if not backfill and self.settings.processed_tag_name not in existing:
                marker = client.get_or_create_tag(self.settings.processed_tag_name)
                client.tag_single_asset(asset.id, [marker.id])
            result.success = True
            result.status = "skipped" if backfill and not missing else "processed"
            result.tags_assigned = missing
        except Exception as error:
            result.error = str(error)
            self.logger.error("Asset %s failed: %s", asset.id, error)
        finally:
            result.processing_time = time.monotonic() - started
        return result

    def run(self, *, backfill=False, limit=None, single=False, max_cycles=None):
        if backfill and not self.settings.translations_enabled:
            raise ProcessorError("backfill-zh requires TRANSLATIONS_ENABLED=true")
        limits = [n for n in (limit, self.settings.batch_size if single else None,
                             max_cycles * self.settings.batch_size if max_cycles else None) if n is not None]
        maximum = min(limits) if limits else None
        if not self.settings.immich_include_library_ids and not self.settings.immich_include_album_ids:
            self.logger.warning("No include scope configured: all visible image assets may be processed")
        result = RunResult()
        self.running, self.last_error = True, None
        try:
            # Validate the catalog before any writes, including the first marker.
            self.catalog
            if self._catalog:
                self._catalog.missing.clear()
            with writer_lock(self.settings.state_dir, self.dry_run), ExitStack() as stack:
                snapshots = []
                selected = 0
                # Snapshot before tagging: legacy numbered pages must not shrink under our writes.
                for client in self.clients:
                    marker = next((t for t in client.get_all_tags(use_cache=False)
                                   if t.path == self.settings.processed_tag_name), None)
                    tracker = self.failure_tracker(client, backfill)
                    snapshot = stack.enter_context(tempfile.SpooledTemporaryFile(mode="w+t", max_size=1024 * 1024, encoding="utf-8"))
                    for asset in client.iter_assets(processed_tag_id=None if backfill or marker is None else marker.id):
                        if self.cancelled.is_set():
                            break
                        if tracker.is_permanently_failed(asset.id):
                            result.skipped += 1
                            continue
                        if maximum is not None and selected >= maximum:
                            break
                        snapshot.write(asset.model_dump_json() + "\n")
                        selected += 1
                    snapshot.seek(0)
                    snapshots.append((client, tracker, snapshot))
                    if maximum is not None and selected >= maximum:
                        break
                # A broken model/cache is a run failure, not thousands of bad images.
                if selected and not backfill and not self.cancelled.is_set():
                    self.engine.prepare()
                for client, tracker, snapshot in snapshots:
                    for line in snapshot:
                        if self.cancelled.is_set():
                            break
                        asset = Asset.model_validate_json(line)
                        item = self.process_asset(client, asset, backfill=backfill)
                        result.attempted += 1
                        setattr(result, item.status, getattr(result, item.status) + 1)
                        if not self.dry_run:
                            if not item.success:
                                tracker.record_failure(asset.id)
                            elif asset.id in tracker.failures:
                                tracker.reset_failures([asset.id])
                        with self._metrics_lock:
                            self.last_result = result.model_copy()
                if result.failed:
                    self.last_error = f"{result.failed} assets failed"
                if self._catalog and self._catalog.missing:
                    self.logger.info("Missing translations (%s): %s", len(self._catalog.missing),
                                     self._catalog.missing.most_common(20))
                self.logger.info("Run complete: %s", result.model_dump())
                return result
        except Exception as error:
            self.last_error = str(error)
            raise
        finally:
            with self._metrics_lock:
                self.last_result = result.model_copy()
            self.running = False

    def get_metrics(self):
        with self._metrics_lock:
            return {"running": self.running, "last_error": self.last_error,
                    "last_run": self.last_result.model_dump(),
                    "translation_revision": self._catalog.revision if self._catalog else None}

    def test_connection(self):
        return all(client.test_connection() for client in self.clients)

    def close(self):
        for client in self.clients:
            client.close()
