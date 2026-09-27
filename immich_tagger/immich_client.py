"""Immich API adapter with scoped searches and explicit write boundaries."""
import logging
import time
from collections.abc import Iterator

import httpx
from .config import Settings, get_settings
from .models import Asset, Tag


class ImmichAPIError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


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
        self._tag_cache: dict[str, Tag] = {}
        self._cache_time = None
        self._search_api = self.settings.search_api

    def _make_request(self, method, endpoint, params=None, json_data=None):
        if self.dry_run and method not in ("GET", "HEAD") and endpoint != "/api/search/metadata":
            raise ImmichAPIError("Dry run forbids Immich writes")
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self.client.request(method, endpoint, params=params, json=json_data)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if status not in (429, 500, 502, 503, 504) or attempt == self.settings.max_retries:
                    raise ImmichAPIError(f"{method} {endpoint}: HTTP {status}: {error.response.text[:500]}", status) from error
                retry_after = error.response.headers.get("Retry-After", "")
                delay = min(float(retry_after), 60) if retry_after.isdigit() else self.settings.retry_delay * 2 ** attempt
            except httpx.RequestError as error:
                if attempt == self.settings.max_retries:
                    raise ImmichAPIError(f"{method} {endpoint}: {type(error).__name__}") from error
                delay = self.settings.retry_delay * 2 ** attempt
            time.sleep(min(delay, 60))
        raise AssertionError("Unreachable")

    def get_all_tags(self, use_cache=True):
        if use_cache and self._cache_time is not None and time.monotonic() - self._cache_time < self.settings.tag_cache_ttl:
            return list(self._tag_cache.values())
        data = self._make_request("GET", "/api/tags").json()
        tags = [Tag.model_validate(item) for item in data]
        self._tag_cache = {tag.path: tag for tag in tags}
        self._cache_time = time.monotonic()
        return tags

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

    def tag_single_asset(self, asset_id, tag_ids):
        if not tag_ids:
            return
        self._make_request("PUT", "/api/tags/assets", json_data={"assetIds": [asset_id], "tagIds": list(set(tag_ids))})
        # Bulk tagging can silently omit inaccessible IDs. Read back before recording completion.
        actual = self.get_asset(asset_id)
        if actual.tags is None or not set(tag_ids).issubset({tag.id for tag in actual.tags}):
            raise ImmichAPIError(f"Tag assignment incomplete for asset {asset_id}")

    def get_asset(self, asset_id):
        return Asset.model_validate(self._make_request("GET", f"/api/assets/{asset_id}").json())

    def download_asset(self, asset_id, use_thumbnail=True):
        endpoint = f"/api/assets/{asset_id}/" + ("thumbnail" if use_thumbnail else "original")
        return self._make_request("GET", endpoint, params={"size": "preview"} if use_thumbnail else None).content

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
        album_ids = self._album_asset_ids()
        libraries = self.settings.immich_include_library_ids or [None]
        excluded = set(self.settings.immich_exclude_library_ids)
        seen = set()
        for library_id in libraries:
            if library_id in excluded:
                continue
            page, cursor = 1, None
            tokens = set()
            while True:
                structured = self._search_api != "legacy"
                query = self._structured_query(library_id, processed_tag_id, cursor) if structured else {
                    "type": "IMAGE", "size": self.settings.batch_size, "page": page,
                    "order": "asc", "isOffline": False, "withDeleted": False,
                }
                if not structured and library_id:
                    query["libraryId"] = library_id
                try:
                    response = self._make_request("POST", "/api/search/metadata", json_data=query).json()
                except ImmichAPIError as error:
                    # Only a rejected structured query permits fallback; never retry authorization errors unscoped.
                    if self._search_api == "auto" and error.status_code == 400:
                        self.logger.warning("Structured search rejected; using legacy pagination with local scope/marker checks")
                        self._search_api = "legacy"
                        continue
                    raise
                section = response.get("assets")
                if not isinstance(section, dict) or not isinstance(section.get("items"), list):
                    raise ImmichAPIError("Invalid metadata search response")
                # Older versions may ignore the unknown filter rather than reject it.
                if structured and self._search_api == "auto" and "nextCursor" not in section:
                    self.logger.warning("Server returned legacy pagination; using legacy search")
                    self._search_api = "legacy"
                    continue
                if structured:
                    self._search_api = "structured"
                for item in section["items"]:
                    asset = Asset.model_validate(item)
                    if asset.id in seen:
                        continue
                    if asset.type != "IMAGE" or asset.isOffline or asset.isTrashed:
                        continue
                    if library_id and asset.libraryId != library_id:
                        continue
                    if asset.libraryId in excluded or (album_ids is not None and asset.id not in album_ids):
                        continue
                    if asset.tags is None:
                        asset = self.get_asset(asset.id)
                    if asset.tags is None:
                        raise ImmichAPIError(f"Asset {asset.id} response omitted tags")
                    if asset.type != "IMAGE" or asset.isOffline or asset.isTrashed:
                        continue
                    if library_id and asset.libraryId != library_id:
                        continue
                    if asset.libraryId in excluded:
                        continue
                    if processed_tag_id and any(t.id == processed_tag_id for t in asset.tags):
                        continue
                    seen.add(asset.id)
                    yield asset
                token_key = "nextCursor" if structured else "nextPage"
                if token_key not in section:
                    raise ImmichAPIError(f"Metadata response omitted {token_key}")
                token = section[token_key]
                if token is None:
                    break
                if str(token) in tokens or not section["items"]:
                    raise ImmichAPIError("Metadata pagination did not advance")
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
