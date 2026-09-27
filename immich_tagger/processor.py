"""Shared, restartable processing pipeline with one durable writer."""
import json
import logging
import sqlite3
import threading
import time

from .config import get_settings
from .failure_tracker import FailureTracker
from .immich_client import ImmichAPIError, ImmichClient, ProcessingCancelled
from .logging import safe_error
from .models import Asset, AssetProcessingResult, RunResult
from .progress import ProgressState
from .state import account_scope, record_assignment, writer_lock
from .task_store import TaskStore, fingerprint
from .translation_catalog import TranslationCatalog


class ProcessorError(RuntimeError):
    pass


class StateWriteError(ProcessorError):
    pass


class ImmichAutoTagger:
    def __init__(self, settings=None, *, dry_run=False, client_factory=ImmichClient, engine_factory=None):
        self.settings = settings or get_settings()
        self.dry_run = dry_run
        self.logger = logging.getLogger("processor")
        self.progress = ProgressState(self.settings)
        self.cancelled = threading.Event()
        self.clients = [client_factory(self.settings, account, dry_run=dry_run)
                        for account in self.settings.get_library_config()]
        for client in self.clients:
            client.progress = self.progress
            client.cancelled = self.cancelled
        self.secrets = [client.account["api_key"] for client in self.clients]
        self.engine_factory = engine_factory
        self._engine = None
        self._catalog = None
        self._saved_revision = None
        self._metrics_lock = threading.Lock()
        self.running = False
        self.last_error = None
        self.last_result = RunResult()
        # Read-only startup snapshot, including in health-only mode; never create/migrate state here.
        if not dry_run:
            saved = TaskStore.snapshot(self.settings.state_dir)
            if saved["progress"]:
                progress = saved["progress"]
                self.last_result = RunResult.model_validate(saved["last_run"])
                self.last_error = saved["last_error"]
                self._saved_revision = saved["translation_revision"]
                self.progress.update(run_id=progress["run_id"], task_status=progress["task_status"],
                                     total=progress["total"], completed=progress["completed"],
                                     last_progress_at=progress["last_progress_at"], **progress["scan"])
                self.progress.update(processed=self.last_result.processed, failed=self.last_result.failed,
                                     skipped=self.last_result.skipped - progress["scan_skipped"])

    @property
    def engine(self):
        if self._engine is None:
            from .tagging_engine import create_tagging_engine
            self._engine = (self.engine_factory or create_tagging_engine)(self.settings)
            self._engine.progress = self.progress
        return self._engine

    @property
    def catalog(self):
        if self._catalog is None and self.settings.translations_enabled:
            with self.progress.operation("catalog"):
                self._catalog = TranslationCatalog(self.settings.translation_file, self.settings.translation_overrides)
        return self._catalog

    def failure_tracker(self, client, backfill=False):
        scope = account_scope(self.settings, client.account)
        return FailureTracker(scope + ("-zh" if backfill else ""), settings=self.settings)

    def _signature(self, *, backfill=False, limit=None, single=False, max_cycles=None):
        limits = [n for n in (limit, self.settings.batch_size if single else None,
                             max_cycles * self.settings.batch_size if max_cycles else None) if n is not None]
        maximum = min(limits) if limits else None
        s = self.settings
        # Reload local vocabulary on each run/check so edited overrides invalidate the queue.
        self._catalog = None
        revision = self.catalog.revision if self.catalog else None
        # Explicit allowlist: never serialize credentials or the complete Settings object.
        return fingerprint({
            "pipeline_version": 1, "accounts": [account_scope(s, c.account) for c in self.clients],
            "libraries": sorted(s.immich_include_library_ids), "albums": sorted(s.immich_include_album_ids),
            "excluded": sorted(s.immich_exclude_library_ids), "backfill": backfill, "maximum": maximum,
            "model": [s.tagging_model, s.model_repo, str(s.deepdanbooru_project_dir)],
            "thresholds": [s.effective_general_threshold, s.character_threshold],
            "marker": s.processed_tag_name, "translations": s.translations_enabled,
            "english_tags": s.english_tags_enabled,
            "revision": revision, "failure_timeout": s.failure_timeout,
        }), maximum, revision

    def has_pending_run(self, **kwargs):
        if self.dry_run or not (self.settings.state_dir / "progress.sqlite3").exists():
            return False
        signature, _, _ = self._signature(**kwargs)
        with TaskStore(self.settings.state_dir, readonly=True) as store:
            return store.find_pending("backfill" if kwargs.get("backfill") else "inference", signature) is not None

    def _paths(self, client, asset, backfill):
        if backfill:
            with self.progress.operation("translate"):
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
        image_data = client.download_asset(asset.id)
        with self.progress.operation("inference"):
            predictions = self.engine.predict_tags(image_data)
        names = [prediction.name for prediction in predictions] if self.settings.english_tags_enabled else []
        if self.catalog:
            with self.progress.operation("translate"):
                for prediction in predictions:
                    translated = self.catalog.translate(prediction.name)
                    if translated:
                        names.append(translated)
        return list(dict.fromkeys(names))

    def _in_scope(self, client, asset):
        included = self.settings.immich_include_library_ids
        return (asset.type == "IMAGE" and not asset.isOffline and not asset.isTrashed
                and (not included or asset.libraryId in included)
                and asset.libraryId not in self.settings.immich_exclude_library_ids
                and (client.album_asset_ids is None or asset.id in client.album_asset_ids))

    def process_asset(self, client, asset, *, backfill=False):
        started = time.monotonic()
        result = AssetProcessingResult(asset_id=asset.id)
        try:
            # Queue entries are IDs, not an authoritative snapshot of current tags/scope.
            asset = client.get_asset(asset.id)
            if not self._in_scope(client, asset):
                result.success, result.status, result.reason = True, "skipped", "图片不可用或已移出范围"
                return result
            if asset.tags is None:
                raise ProcessorError("资产响应缺少标签")
            if not backfill and any(t.path == self.settings.processed_tag_name for t in asset.tags):
                result.success, result.status, result.reason = True, "skipped", "已有完成标记"
                return result
            paths = self._paths(client, asset, backfill)
            current = client.get_asset(asset.id) if not self.dry_run else asset
            if not self._in_scope(client, current):
                result.success, result.status, result.reason = True, "skipped", "写入前发现图片不可用或已移出范围"
                return result
            if current.tags is None:
                raise ProcessorError("资产响应缺少标签")
            existing = {tag.path for tag in current.tags}
            missing = [path for path in paths if path not in existing]
            if self.dry_run:
                result.success, result.status = True, "planned"
                result.tags_assigned = missing
                self.progress.log(f"仅预览｜图片 ID：{asset.id}｜计划添加：{json.dumps(missing, ensure_ascii=False)}")
                return result
            if missing:
                mapping = client.get_or_create_tags_bulk(missing)
                if set(mapping) != set(missing):
                    raise ProcessorError("部分标签未能创建或查到")
                client.tag_single_asset(asset.id, [mapping[name].id for name in missing])
                try:
                    record_assignment(self.settings.state_dir, account_scope(self.settings, client.account), asset.id, missing)
                except OSError as error:
                    raise StateWriteError(f"写入标签记录失败：{type(error).__name__}") from error
            if not backfill and self.settings.processed_tag_name not in existing:
                marker = client.get_or_create_tag(self.settings.processed_tag_name)
                client.tag_single_asset(asset.id, [marker.id], marker=True)
            result.success = True
            result.status = "skipped" if backfill and not missing else "processed"
            result.tags_assigned = missing
        except (StateWriteError, sqlite3.Error):
            # Local durability failures must stop the run, not poison asset failure counts.
            raise
        except Exception as error:
            if isinstance(error, ImmichAPIError) and error.status_code in (401, 403):
                raise
            if isinstance(error, ImmichAPIError) and error.status_code == 404:
                result.success, result.status, result.reason = True, "skipped", "图片已删除"
            else:
                result.error = safe_error(error, self.secrets)
                self.progress.log(f"图片处理失败｜图片 ID：{asset.id}｜原因：{result.error}", logging.ERROR)
        finally:
            result.processing_time = time.monotonic() - started
        return result

    def _publish_result(self, result, *, session_advance=False, scan_skipped=0):
        with self._metrics_lock:
            self.last_result = result.model_copy()
            self.progress.update(advance=session_advance, completed=result.attempted, processed=result.processed,
                                 failed=result.failed, skipped=result.skipped - scan_skipped)
            if session_advance:
                self.progress.increment("session_completed")

    def _scan(self, store, run, maximum, backfill):
        run_id = run["id"]
        generation = store.start_scan(run_id)
        self.progress.update(scan_pages=0, scan_records=0, detail_requests=0, candidates=0, scan_skips={})
        self.progress.phase("scanning", "开始收集候选图片，收集完成后开始处理", status="scanning")
        selected, scan_skipped = set(), 0
        for client in self.clients:
            if self.cancelled.is_set():
                raise ProcessingCancelled()
            self.progress.update(account=client.current_library_name)
            marker = next((t for t in client.get_all_tags(use_cache=False)
                           if t.path == self.settings.processed_tag_name), None)
            scope = account_scope(self.settings, client.account)
            failure_scope = scope + ("-zh" if backfill else "")
            failures = (self.failure_tracker(client, backfill).failures if self.dry_run else store.failures(failure_scope))
            for page in client.iter_asset_pages(processed_tag_id=None if backfill or marker is None else marker.id):
                if self.cancelled.is_set():
                    raise ProcessingCancelled()
                candidates = []
                for asset in page:
                    key = (scope, asset.id)
                    if key in selected:
                        continue
                    if failures.get(asset.id, {}).get("permanently_failed"):
                        scan_skipped += 1
                        self.progress.skip("失败次数已达上限")
                        continue
                    if maximum is not None and len(selected) >= maximum:
                        break
                    candidates.append((scope, asset.id, len(selected)))
                    selected.add(key)
                self.progress.update(advance=True, candidates=len(selected))
                snap = self.progress.snapshot()
                scan = {key: snap[key] for key in ("scan_pages", "scan_records", "detail_requests", "candidates", "scan_skips")}
                with self.progress.operation("checkpoint"):
                    store.save_page(run_id, generation, candidates, scan, scan_skipped)
                self._publish_result(RunResult(skipped=scan_skipped), scan_skipped=scan_skipped)
                if maximum is not None and len(selected) >= maximum:
                    break
            if maximum is not None and len(selected) >= maximum:
                break
        if self.cancelled.is_set():
            raise ProcessingCancelled()
        run = store.finish_scan(run_id, generation)
        self.progress.update(total=run["total"], task_status="ready")
        self.progress.log(f"扫描完成｜本轮待处理 {run['total']} 张")
        return run

    def _execute(self, store, signature, maximum, revision, backfill):
        run, resumed = store.begin("backfill" if backfill else "inference", signature, revision)
        run_id = run["id"]
        result = RunResult.model_validate_json(run["result"])
        self.progress.update(run_id=run_id, resumed=resumed, total=run["total"],
                             task_status=run["status"], last_progress_at=run["last_progress_at"],
                             **json.loads(run["scan"]))
        self._publish_result(result, scan_skipped=run["scan_skipped"])
        try:
            if resumed:
                self.progress.phase("recovering", f"发现未完成任务｜已完成 {result.attempted} 张")
                if run["scan_complete"]:
                    self.progress.log(f"候选扫描已完成，将继续剩余 {run['total'] - result.attempted} 张")
                else:
                    self.progress.log("上次扫描未完成，重新核对候选范围并按图片 ID 去重")
            if not run["scan_complete"]:
                run = self._scan(store, run, maximum, backfill)
                result = RunResult.model_validate_json(run["result"])
            elif self.settings.immich_include_album_ids:
                # Refresh album membership on every resume, without a full metadata search.
                for client in self.clients:
                    self.progress.update(account=client.current_library_name)
                    client.album_asset_ids = client._album_asset_ids()
            if self.cancelled.is_set():
                raise ProcessingCancelled()
            if not run["total"]:
                self.progress.log("没有需要处理的图片")
            if run["total"] > result.attempted and not backfill:
                model_name = self.settings.model_repo if self.settings.tagging_model == "wd14" else "DeepDanbooru"
                self.progress.phase("preparing_model", f"正在准备模型 {model_name}｜首次运行可能需要下载权重")
                with self.progress.operation("load_model"):
                    self.engine.prepare()
                self.progress.update(advance=True)
                self.progress.log("模型已就绪")
            if self.cancelled.is_set():
                raise ProcessingCancelled()
            self.progress.phase("processing", f"开始处理｜总计 {run['total']} 张｜已完成 {result.attempted} 张", status="processing")
            clients = {account_scope(self.settings, c.account): c for c in self.clients}
            for queued in store.pending_items(run_id):
                if self.cancelled.is_set():
                    raise ProcessingCancelled()
                client = clients[queued["account"]]
                self.progress.update(account=client.current_library_name, current_asset_id=queued["asset_id"])
                store.mark_processing(run_id, queued["account"], queued["asset_id"])
                item = self.process_asset(client, Asset(id=queued["asset_id"], type="IMAGE"), backfill=backfill)
                with self.progress.operation("checkpoint"):
                    result = store.finish_item(run_id, queued["account"], item,
                                               queued["account"] + ("-zh" if backfill else ""), self.settings.failure_timeout)
                self._publish_result(result, session_advance=True, scan_skipped=run["scan_skipped"])
                status_label = {"processed": "成功", "skipped": "跳过", "planned": "预览", "failed": "失败"}[item.status]
                self.progress.log(f"图片 ID：{item.asset_id}｜结果：{item.reason or status_label}｜耗时 {item.processing_time:.2f} 秒"
                                  f"｜新增标签：{json.dumps(item.tags_assigned, ensure_ascii=False)}", logging.DEBUG)
            error = f"{result.failed} 张图片处理失败" if result.failed else None
            store.complete(run_id, error)
            with self._metrics_lock:
                self.last_error = error
            self.progress.update(current_asset_id=None)
            self.progress.phase("completed", f"本轮结束｜完成 {result.attempted} 张｜成功 {result.processed}｜失败 {result.failed}"
                                f"｜跳过 {result.skipped}｜本次耗时 {self.progress.snapshot()['session_elapsed_seconds']:.0f} 秒", status="completed")
            if self._catalog and self._catalog.missing:
                self.progress.log(f"缺少中文译名：{self._catalog.missing.most_common(20)}", logging.DEBUG)
            return result
        except ProcessingCancelled:
            store.pause(run_id)
            self.progress.phase("paused", "已停止领取新图片，任务进度已保存，下次执行继续", status="paused")
            return self.last_result.model_copy()
        except Exception as error:
            # If a DB write itself failed, pause may fail too; leave its original queue intact.
            try:
                store.pause(run_id, safe_error(error, self.secrets))
            except sqlite3.Error:
                pass
            raise

    def run(self, *, backfill=False, limit=None, single=False, max_cycles=None):
        if backfill and not self.settings.translations_enabled:
            raise ProcessorError("中文补全需要启用 TRANSLATIONS_ENABLED")
        if self.cancelled.is_set():
            return self.last_result.model_copy()
        self.progress.reset()
        with self._metrics_lock:
            self.running, self.last_error, self.last_result = True, None, RunResult()
        try:
            with self.progress.reporting():
                self.progress.phase("recovering", "开始任务｜" + ("补充中文标签" if backfill else "自动打标签")
                                    + ("｜仅预览" if self.dry_run else "｜正式写入"))
                if not self.settings.immich_include_library_ids and not self.settings.immich_include_album_ids:
                    self.progress.log("未限制图库或相册，将处理账号可见的图片；排除规则仍然生效", logging.WARNING)
                else:
                    self.progress.log(f"处理范围｜包含图库 {len(self.settings.immich_include_library_ids)} 个"
                                      f"｜包含相册 {len(self.settings.immich_include_album_ids)} 个"
                                      f"｜排除图库 {len(self.settings.immich_exclude_library_ids)} 个")
                with writer_lock(self.settings.state_dir, self.dry_run):
                    signature, maximum, revision = self._signature(backfill=backfill, limit=limit, single=single, max_cycles=max_cycles)
                    self._saved_revision = revision
                    if self._catalog:
                        self._catalog.missing.clear()
                    with TaskStore(self.settings.state_dir, memory=self.dry_run) as store:
                        return self._execute(store, signature, maximum, revision, backfill)
        except Exception as error:
            with self._metrics_lock:
                self.last_error = safe_error(error, self.secrets)
            self.progress.phase("error", f"任务已中止｜原因：{self.last_error}", status="paused")
            raise
        finally:
            with self._metrics_lock:
                self.progress.finish()
                self.running = False

    def get_metrics(self):
        with self._metrics_lock:
            return {"running": self.running, "last_error": self.last_error,
                    "last_run": self.last_result.model_dump(),
                    "translation_revision": self._catalog.revision if self._catalog else self._saved_revision,
                    "progress": self.progress.snapshot()}

    def test_connection(self):
        return all(client.test_connection() for client in self.clients)

    def close(self):
        for client in self.clients:
            client.close()
