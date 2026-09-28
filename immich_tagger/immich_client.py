"""Immich API adapter with scoped searches and explicit write boundaries."""
import logging
import time
from collections.abc import Iterator
from contextlib import nullcontext
from functools import wraps

import httpx
from .config import Settings, get_settings
from .models import Asset, Tag


def observed(operation):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            with self._operation(operation):
                return method(self, *args, **kwargs)
        return wrapped
    return decorate


class ProcessingCancelled(Exception):
    """Cooperative cancellation while discovering candidates."""


class ImmichAPIError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class TagRemovalIncompleteError(ImmichAPIError):
    """The DELETE completed, but its association is still present on readback."""


class ImmichClient:
    def __init__(self, settings: Settings | None = None, account=None, *, dry_run=False, transport=None):
        self.settings = settings or get_settings()
        self.account = account or self.settings.get_library_config()[0]
        self.current_library_name = self.account["name"]
        self.base_url = self.settings.immich_base_url
        self.dry_run = dry_run
        self.logger = logging.getLogger("immich_client")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": self.account["api_key"]},
            timeout=self.settings.request_timeout,
            transport=transport,
        )
        self.progress = None
        self.cancelled = None
        self.album_asset_ids = None
        self._tag_cache: dict[str, Tag] = {}
        self._cache_time = None
        self._search_api = self.settings.search_api

    def _operation(self, name):
        return self.progress.operation(name) if self.progress else nullcontext()

    def _check_cancelled(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise ProcessingCancelled()

    def _skip(self, reason):
        if self.progress:
            self.progress.skip(reason)

    def _make_request(self, method, endpoint, params=None, json_data=None):
        if self.dry_run and method not in ("GET", "HEAD") and endpoint != "/api/search/metadata":
            raise ImmichAPIError("Dry run forbids Immich writes")
        for attempt in range(self.settings.max_retries + 1):
            status = None
            try:
                response = self.client.request(method, endpoint, params=params, json=json_data)
                response.raise_for_status()
                if self.progress:
                    self.progress.update(advance=True)
                return response
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if status not in (429, 500, 502, 503, 504) or attempt == self.settings.max_retries:
                    raise ImmichAPIError(f"{method} {endpoint}: HTTP {status}", status) from None
                retry_after = error.response.headers.get("Retry-After", "")
                delay = min(float(retry_after), 60) if retry_after.isdigit() else self.settings.retry_delay * 2 ** attempt
            except httpx.RequestError as error:
                if attempt == self.settings.max_retries:
                    raise ImmichAPIError(f"{method} {endpoint}: {type(error).__name__}") from error
                delay = self.settings.retry_delay * 2 ** attempt
            reason = f"HTTP {status}" if status is not None else "网络请求异常"
            message = f"[重试] {method} {endpoint}：{reason}｜第 {attempt + 1} 次重试，最多 {self.settings.max_retries} 次｜{min(delay, 60):g} 秒后重试"
            if self.progress:
                self.progress.log(message, logging.WARNING)
            else:
                self.logger.warning(message)
            time.sleep(min(delay, 60))
        raise AssertionError("Unreachable")

    @observed("read_tags")
    def get_all_tags(self, use_cache=True):
        if use_cache and self._cache_time is not None and time.monotonic() - self._cache_time < self.settings.tag_cache_ttl:
            return list(self._tag_cache.values())
        data = self._make_request("GET", "/api/tags").json()
        tags = [Tag.model_validate(item) for item in data]
        self._tag_cache = {tag.path: tag for tag in tags}
        self._cache_time = time.monotonic()
        return tags

    @observed("create_tags")
    def get_or_create_tags_bulk(self, names):
        names = list(dict.fromkeys(names))
        if any(not n or any(c in n for c in "\r\n\t") or any(not p for p in n.split("/")) for n in names):
            raise ValueError("Tags must have nonempty path segments without control characters")
        self.get_all_tags()
        missing = [name for name in names if name not in self._tag_cache]
        for offset in range(0, len(missing), 100):
            requested = missing[offset:offset + 100]
            response = self._make_request("PUT", "/api/tags", json_data={"tags": requested})
            tags = [Tag.model_validate(item) for item in response.json()]
            self._tag_cache.update({tag.path: tag for tag in tags})
            if any(name not in self._tag_cache for name in requested):
                raise ImmichAPIError("Tag upsert returned an incomplete result")
        return {name: self._tag_cache[name] for name in names}

    def get_or_create_tag(self, name):
        return self.get_or_create_tags_bulk([name])[name]

    def tag_single_asset(self, asset_id, tag_ids, *, marker=False):
        if not tag_ids:
            return
        with self._operation("marker" if marker else "assign_tags"):
            self._make_request("PUT", "/api/tags/assets", json_data={"assetIds": [asset_id], "tagIds": list(set(tag_ids))})
            actual = self.get_asset(asset_id, operation="readback")
            if actual.tags is None or not set(tag_ids).issubset({tag.id for tag in actual.tags}):
                raise ImmichAPIError(f"标签关联回读不完整：{asset_id} (Tag assignment incomplete)")

    def get_asset(self, asset_id, *, operation="read_asset"):
        with self._operation(operation):
            return Asset.model_validate(self._make_request("GET", f"/api/assets/{asset_id}").json())

    def untag_single_asset(self, asset_id, tag_id):
        with self._operation("remove_tags"):
            # Immich exposes removal per tag, not DELETE /tags/assets.
            data = self._make_request("DELETE", f"/api/tags/{tag_id}/assets", json_data={"ids": [asset_id]}).json()
            if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
                    or data[0].get("id") != asset_id or data[0].get("success") is not True):
                raise ImmichAPIError(f"标签解除接口未确认成功：{asset_id}")
            actual = self.get_asset(asset_id, operation="readback")
            if actual.tags is None or any(tag.id == tag_id for tag in actual.tags):
                raise TagRemovalIncompleteError(f"标签解除回读不完整：{asset_id} (Tag removal incomplete)")

    @staticmethod
    def _validate_queue(name, data):
        if name not in ("sidecar", "metadataExtraction"):
            raise ValueError("Unsupported maintenance queue")
        counts = data.get("statistics") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get("name") != name
                or type(data.get("isPaused")) is not bool or not isinstance(counts, dict)
                or any(type(counts.get(k)) is not int or counts[k] < 0
                       for k in ("active", "waiting", "delayed", "paused", "failed"))):
            raise ImmichAPIError(f"队列状态无效：{name}；需要兼容的 Immich 队列 API")
        return data

    @observed("read_queues")
    def get_cleanup_queue(self, name):
        if name not in ("sidecar", "metadataExtraction"):
            raise ValueError("Unsupported maintenance queue")
        return self._validate_queue(name, self._make_request("GET", f"/api/queues/{name}").json())

    @observed("update_queues")
    def set_cleanup_queue_paused(self, name, paused):
        if name not in ("sidecar", "metadataExtraction") or type(paused) is not bool:
            raise ValueError("Invalid maintenance queue update")
        data = self._validate_queue(name, self._make_request(
            "PUT", f"/api/queues/{name}", json_data={"isPaused": paused}).json())
        if data["isPaused"] != paused:
            raise ImmichAPIError(f"队列未达到请求状态：{name}")
        return data

    @observed("download")
    def download_asset(self, asset_id, use_thumbnail=True):
        endpoint = f"/api/assets/{asset_id}/" + ("thumbnail" if use_thumbnail else "original")
        return self._make_request("GET", endpoint, params={"size": "preview"} if use_thumbnail else None).content

    @observed("read_album")
    def _album_asset_ids(self):
        if not self.settings.immich_include_album_ids:
            return None
        ids = set()
        for album_id in self.settings.immich_include_album_ids:
            data = self._make_request("GET", f"/api/albums/{album_id}").json()
            if not isinstance(data.get("assets"), list):
                raise ImmichAPIError("Album response does not include assets; refusing an unscoped search")
            ids.update(asset["id"] for asset in data["assets"])
        return ids

    def _structured_query(self, library_id, marker_id, cursor):
        filters = {"type": {"eq": "IMAGE"}, "isOffline": {"eq": False}, "trashedAt": {"eq": None}}
        if library_id:
            filters["libraryId"] = {"eq": library_id}
        if marker_id:
            filters["tagIds"] = {"none": [marker_id]}
        query = {"filter": filters, "size": self.settings.batch_size,
                 "orderBy": {"field": "fileCreatedAt", "direction": "asc"}}
        if cursor:
            query["cursor"] = cursor
        return query

    def iter_assets(self, *, processed_tag_id=None) -> Iterator[Asset]:
        for page in self.iter_asset_pages(processed_tag_id=processed_tag_id):
            yield from page

    def iter_asset_pages(self, *, processed_tag_id=None):
        self._check_cancelled()
        album_ids = self.album_asset_ids = self._album_asset_ids()
        libraries = self.settings.immich_include_library_ids or [None]
        excluded = set(self.settings.immich_exclude_library_ids)
        seen = set()
        for library_id in libraries:
            if library_id in excluded:
                continue
            page, cursor = 1, None
            tokens = set()
            while True:
                self._check_cancelled()
                structured = self._search_api != "legacy"
                query = self._structured_query(library_id, processed_tag_id, cursor) if structured else {
                    "type": "IMAGE", "size": self.settings.batch_size, "page": page,
                    "order": "asc", "isOffline": False, "withDeleted": False,
                }
                if not structured and library_id:
                    query["libraryId"] = library_id
                try:
                    with self._operation("search"):
                        response = self._make_request("POST", "/api/search/metadata", json_data=query).json()
                except ImmichAPIError as error:
                    if self._search_api == "auto" and error.status_code == 400:
                        self.logger.warning("[扫描] 结构化搜索被拒绝，改用旧版分页并保持范围与完成标记校验")
                        self._search_api = "legacy"
                        continue
                    raise
                section = response.get("assets")
                if not isinstance(section, dict) or not isinstance(section.get("items"), list):
                    raise ImmichAPIError("图片搜索返回格式无效")
                if structured and self._search_api == "auto" and "nextCursor" not in section:
                    self.logger.warning("[扫描] 服务返回旧版分页格式，改用旧版搜索")
                    self._search_api = "legacy"
                    continue
                if structured:
                    self._search_api = "structured"
                if self.progress:
                    self.progress.increment("scan_pages")
                    self.progress.increment("scan_records", len(section["items"]))
                candidates = []
                for item in section["items"]:
                    self._check_cancelled()
                    asset = Asset.model_validate(item)
                    if asset.id in seen:
                        self._skip("重复记录")
                        continue
                    if asset.type != "IMAGE" or asset.isOffline or asset.isTrashed:
                        self._skip("非图片或不可用")
                        continue
                    if library_id and asset.libraryId != library_id:
                        self._skip("图库范围外")
                        continue
                    if asset.libraryId in excluded or (album_ids is not None and asset.id not in album_ids):
                        self._skip("排除范围或相册外")
                        continue
                    if asset.tags is None:
                        if self.progress:
                            self.progress.increment("detail_requests")
                        try:
                            asset = self.get_asset(asset.id, operation="read_details")
                        except ImmichAPIError as error:
                            if error.status_code != 404:
                                raise
                            self._skip("已删除")
                            continue
                    if asset.tags is None:
                        raise ImmichAPIError(f"资产 {asset.id} 的响应缺少标签")
                    if asset.type != "IMAGE" or asset.isOffline or asset.isTrashed:
                        self._skip("非图片或不可用")
                        continue
                    if (library_id and asset.libraryId != library_id) or asset.libraryId in excluded:
                        self._skip("图库范围外")
                        continue
                    if processed_tag_id and any(t.id == processed_tag_id for t in asset.tags):
                        self._skip("已有完成标记")
                        continue
                    seen.add(asset.id)
                    candidates.append(asset)
                token_key = "nextCursor" if structured else "nextPage"
                if token_key not in section:
                    raise ImmichAPIError(f"图片搜索响应缺少 {token_key}")
                token = section[token_key]
                if token is not None and (str(token) in tokens or not section["items"]):
                    raise ImmichAPIError("图片搜索分页未推进 (pagination did not advance)")
                yield candidates
                if token is None:
                    break
                tokens.add(str(token))
                if structured:
                    cursor = token
                else:
                    page = int(token)

    def test_connection(self):
        self.get_all_tags(use_cache=False)
        return True

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
