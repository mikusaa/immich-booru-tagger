"""Explicit, restartable removal of English associations already covered by Chinese."""
import logging
import sqlite3
import time
import uuid

from .cleanup_queues import CleanupQueues, QueueMaintenanceError
from .immich_client import ImmichAPIError, ImmichClient, ProcessingCancelled, TagRemovalIncompleteError
from .logging import safe_error
from .models import AssetProcessingResult
from .processor import ImmichAutoTagger, ProcessorError, StateWriteError
from .state import account_scope, record_cleanup, recorded_assignments
from .task_store import fingerprint


class EnglishTagCleaner(ImmichAutoTagger):
    def __init__(self, settings=None, *, cleanup_scope="recorded", dry_run=True,
                 confirm_cleanup_english=False, maintenance=False, **kwargs):
        if cleanup_scope not in ("recorded", "catalog"):
            raise ValueError("cleanup_scope must be recorded or catalog")
        if not dry_run and not confirm_cleanup_english:
            raise ValueError("实际清理需要 --confirm-cleanup-english")
        self.cleanup_scope = cleanup_scope
        self.maintenance = bool(maintenance and not dry_run)
        self._recorded = {}
        self._maintenance_client = None
        self._queues = None
        client_factory = kwargs.get("client_factory", ImmichClient)
        super().__init__(settings, dry_run=dry_run, **kwargs)
        if self.maintenance:
            key = self.settings.cleanup_admin_api_key.strip()
            if not key:
                super().close()
                raise ValueError("清理维护模式需要 CLEANUP_ADMIN_API_KEY")
            self._maintenance_client = client_factory(
                self.settings, {"name": "cleanup-admin", "api_key": key}, dry_run=False)
            self._maintenance_client.progress = self.progress
            self._queues = CleanupQueues(self._maintenance_client, self.settings, self.progress, self.cancelled)

    def _task_kind(self, backfill):
        return "cleanup-english"

    def _task_label(self, backfill):
        return f"清理英文标签｜依据：{self.cleanup_scope}"

    def _failure_scope(self, scope, backfill):
        return f"{scope}-cleanup-{self.cleanup_scope}"

    def _before_run(self):
        if self._queues:
            self._queues.recover()
            self._queues.preflight()
        else:
            super()._before_run()

    def _signature(self, **kwargs):
        kwargs["backfill"] = True
        signature, maximum, revision = super()._signature(**kwargs)
        with self.progress.operation("read_assignments"):
            self._recorded = (recorded_assignments(self.settings.state_dir)
                              if self.cleanup_scope == "recorded" else {})
        # Changes in ownership evidence must not silently expand an existing queue.
        evidence = {scope: {asset: sorted(tags) for asset, tags in assets.items()}
                    for scope, assets in self._recorded.items()}
        return fingerprint({"base": signature, "scope": self.cleanup_scope, "recorded": evidence}), maximum, revision

    def _pairs(self, client, asset):
        if asset.tags is None:
            raise ProcessorError("资产响应缺少标签")
        paths = {tag.path for tag in asset.tags}
        recorded = self._recorded.get(account_scope(self.settings, client.account), {}).get(asset.id, set())
        pairs = []
        for tag in asset.tags:
            if tag.path in (self.settings.processed_tag_name, "auto:processed"):
                continue
            if self.cleanup_scope == "recorded" and tag.path not in recorded:
                continue
            translated = self.catalog.translate_path(tag.path)
            if translated and translated != tag.path and translated in paths:
                pairs.append((tag, translated))
        return pairs

    def _recorded_pages(self, client):
        # Journal mode avoids searching the entire library; still fetch live scope/tags.
        client.album_asset_ids = client._album_asset_ids()
        identifiers = self._recorded.get(account_scope(self.settings, client.account), {})
        page = []
        count = 0
        for asset_id in identifiers:
            if self.cancelled.is_set():
                raise ProcessingCancelled()
            self.progress.increment("scan_records")
            count += 1
            try:
                asset = client.get_asset(asset_id)
            except ImmichAPIError as error:
                if error.status_code != 404:
                    raise
                self.progress.skip("已删除")
            else:
                if self._in_scope(client, asset):
                    page.append(asset)
                else:
                    self.progress.skip("图片不可用或已移出范围")
            if count % self.settings.batch_size == 0:
                self.progress.increment("scan_pages")
                yield page
                page = []
        if count % self.settings.batch_size:
            self.progress.increment("scan_pages")
            yield page

    def _catalog_pages(self, client):
        tags = client.get_all_tags(use_cache=False)
        paths = {tag.path for tag in tags}
        tag_ids = []
        for tag in tags:
            if tag.path in (self.settings.processed_tag_name, "auto:processed"):
                continue
            translated = self.catalog.translate_path(tag.path)
            if translated and translated != tag.path and translated in paths:
                tag_ids.append(tag.id)
        self.progress.log(f"按现有标签收集清理候选｜可匹配英文标签 {len(tag_ids)} 个"
                          "｜仅查询关联图片并读取现有标签，不补标签或运行识别")
        yield from client.iter_asset_pages(tag_ids=tag_ids)

    def _asset_pages(self, client, backfill):
        pages = self._recorded_pages(client) if self.cleanup_scope == "recorded" else self._catalog_pages(client)
        for page in pages:
            candidates = []
            for asset in page:
                if self._pairs(client, asset):
                    candidates.append(asset)
                else:
                    self.progress.skip("无可清理的英文标签")
            yield candidates

    def _journal(self, client, asset_id, **values):
        try:
            record_cleanup(self.settings.state_dir, account_scope(self.settings, client.account), asset_id,
                           run_id=self.progress.snapshot()["run_id"], **values)
        except OSError as error:
            raise StateWriteError(f"写入清理记录失败：{type(error).__name__}") from error

    def _wait_between_removals(self):
        # Each unlink schedules an XMP write in Immich; avoid a rapid burst for one asset.
        if self.maintenance:
            return
        time.sleep(min(max(5, self.settings.retry_delay), 60))

    def _wait_for_readback(self, attempt):
        # Immich may rebuild associations asynchronously while writing XMP metadata.
        # Check the complete asset after allowing those jobs to settle, not just each DELETE.
        if self.maintenance:
            return
        time.sleep(min(max(5, self.settings.retry_delay * 2 ** attempt), 60))

    def _remove_pairs(self, client, asset_id, pairs):
        if not pairs:
            return []
        planned = {tag.id: (tag, translated) for tag, translated in pairs}
        for attempt in range(self.settings.max_retries + 1):
            for index, (tag, translated) in enumerate(planned.values(), 1):
                if self.maintenance and self.cancelled.is_set():
                    raise ProcessingCancelled()
                self.progress.update(cleanup_tag=tag.path, cleanup_index=index, cleanup_total=len(planned),
                                     cleanup_attempt=attempt + 1)
                current = client.get_asset(asset_id)
                if not self._in_scope(client, current):
                    break
                if (tag.id, translated) not in {(t.id, zh) for t, zh in self._pairs(client, current)}:
                    continue
                values = {"operation_id": uuid.uuid4().hex, "tag": tag, "translated": translated}
                self._journal(client, asset_id, status="prepared", **values)
                try:
                    client.untag_single_asset(asset_id, tag.id)
                except TagRemovalIncompleteError:
                    # A delayed whole-asset read below decides what still needs removal.
                    pass
                else:
                    self._journal(client, asset_id, status="confirmed", **values)
                with self.progress.operation("settle_tags"):
                    self._wait_between_removals()
            # Require two spaced clean reads: an immediate or single delayed read can
            # precede a queued metadata job that restores an older tag list.
            for _ in range(2):
                with self.progress.operation("settle_tags"):
                    self._wait_for_readback(attempt)
                current = client.get_asset(asset_id, operation="readback")
                if current.tags is None:
                    raise ProcessorError("资产响应缺少标签")
                present = {tag.id for tag in current.tags}
                remaining = ({tag.id for tag, _ in self._pairs(client, current)} & planned.keys()
                             if self._in_scope(client, current) else set())
                if remaining:
                    break
            if not remaining:
                return [tag.path for tag, _ in planned.values() if tag.id not in present]
            if attempt < self.settings.max_retries:
                self.progress.log(f"图片 ID：{asset_id}｜整张回读仍有 {len(remaining)} 个待清理英文关联"
                                  f"｜将重新核对中文与范围后重试 {attempt + 1}/{self.settings.max_retries}", logging.WARNING)
        raise TagRemovalIncompleteError(f"整张图片清理回读不完整：{asset_id}；可能有异步元数据回写，请稍后重试")

    def process_asset(self, client, asset, *, backfill=False):
        started = time.monotonic()
        self.progress.update(cleanup_started=started)
        result = AssetProcessingResult(asset_id=asset.id)
        try:
            try:
                current = client.get_asset(asset.id)
            except ImmichAPIError as error:
                if error.status_code != 404:
                    raise
                result.success, result.status, result.reason = True, "skipped", "图片已删除"
                return result
            if not self._in_scope(client, current):
                result.success, result.status, result.reason = True, "skipped", "图片不可用或已移出范围"
                return result
            pairs = self._pairs(client, current)
            if self.dry_run:
                result.success, result.status = True, "planned"
                result.tags_removed = [tag.path for tag, _ in pairs]
                for tag, translated in pairs:
                    self.progress.log(f"仅预览｜图片 ID：{asset.id}｜计划移除：{tag.path}｜已有中文：{translated}")
                return result
            result.tags_removed = (self._remove_with_queues(client, asset.id, pairs) if self.maintenance
                                   else self._remove_pairs(client, asset.id, pairs))
            result.success = True
            result.status = "processed" if result.tags_removed else "skipped"
        except (StateWriteError, sqlite3.Error, QueueMaintenanceError, ProcessingCancelled):
            raise
        except Exception as error:
            if isinstance(error, ImmichAPIError) and error.status_code in (401, 403):
                raise
            result.error = safe_error(error, self.secrets)
            self.progress.log(f"英文清理失败｜图片 ID：{asset.id}｜原因：{result.error}", logging.ERROR)
        finally:
            result.processing_time = time.monotonic() - started
            self.progress.update(cleanup_started=None, cleanup_tag=None, cleanup_total=None,
                                 cleanup_index=None, cleanup_attempt=None)
        return result

    def _remove_with_queues(self, client, asset_id, pairs):
        if not pairs:
            return []
        with self._queues.paused():
            # Jobs active at the start may have changed the asset. Capture a fresh
            # plan and protected tags only after they have completely stopped.
            current = client.get_asset(asset_id)
            if not self._in_scope(client, current):
                return []
            pairs = self._pairs(client, current)
            planned = {tag.id: tag.path for tag, _ in pairs}
            protected = {tag.id for tag in current.tags} - planned.keys()
            removed = self._remove_pairs(client, asset_id, pairs)
        # The shared pipeline commits this asset only after this final read.
        current = client.get_asset(asset_id, operation="readback")
        if current.tags is None:
            raise ProcessorError("资产响应缺少标签")
        present = {tag.id for tag in current.tags}
        if not protected.issubset(present):
            raise QueueMaintenanceError(f"后台复核发现保留标签缺失：{asset_id}，停止本轮清理")
        if any(tid in present for tid, path in planned.items() if path in removed):
            raise TagRemovalIncompleteError(f"后台复核发现英文标签重新出现：{asset_id}")
        return removed

    def run(self, *, limit=None, max_cycles=None):
        return super().run(backfill=True, limit=limit, max_cycles=max_cycles)

    def close(self):
        if self._maintenance_client:
            self._maintenance_client.close()
        super().close()
