import copy
import json

import httpx
import pytest

from immich_tagger.config import Settings
from immich_tagger.immich_client import ImmichClient
from immich_tagger.models import TagPrediction

LIBRARY_A = "11111111-1111-4111-8111-111111111111"
LIBRARY_B = "22222222-2222-4222-8222-222222222222"
ALBUM = "33333333-3333-4333-8333-333333333333"


def asset(number, **values):
    return {"id": str(number), "type": "IMAGE", "libraryId": None, "tags": [], **values}


def tag(name):
    return {"id": "tag-" + name, "name": name.rsplit("/", 1)[-1], "value": name}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in Settings.model_fields:
        monkeypatch.delenv(key.upper(), raising=False)
    return Settings(_env_file=None, immich_base_url="http://immich.test", immich_api_key="test-key",
                    state_dir=tmp_path / "state", model_cache_dir=tmp_path / "models", max_retries=0)


class FakeImmich:
    """HTTP-level fixture that can ignore filters and partially accept writes."""
    def __init__(self, assets=(), *, legacy=False, reject_structured=False):
        self.assets = {item["id"]: copy.deepcopy(item) for item in assets}
        self.tags = {t["value"]: t for item in assets for t in item.get("tags") or []}
        self.legacy = legacy
        self.reject_structured = reject_structured
        self.calls = []
        self.albums = {}
        self.omit_search_tags = False
        self.ignore_filters = False
        self.incomplete_upsert = False
        self.drop_assignment = False
        self.error_status = None

    def factory(self, settings, account, *, dry_run=False):
        return ImmichClient(settings, account, dry_run=dry_run, transport=httpx.MockTransport(self.handle))

    @property
    def writes(self):
        return [call for call in self.calls if call[0] not in ("GET", "HEAD") and call[1] != "/api/search/metadata"]

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if self.error_status:
            return httpx.Response(self.error_status, json={"error": "test error"})
        if path == "/api/tags":
            if request.method == "GET":
                return httpx.Response(200, json=list(self.tags.values()))
            names = body["tags"]
            if self.incomplete_upsert:
                names = names[:-1]
            for name in names:
                self.tags.setdefault(name, tag(name))
            return httpx.Response(200, json=[self.tags[name] for name in names])
        if path == "/api/tags/assets":
            if not self.drop_assignment:
                tags_by_id = {t["id"]: t for t in self.tags.values()}
                for identifier in body["assetIds"]:
                    current = self.assets[identifier]["tags"]
                    for tid in body["tagIds"]:
                        if tid not in {t["id"] for t in current}:
                            current.append(tags_by_id[tid])
            return httpx.Response(200, json={"count": len(body["tagIds"])})
        if path == "/api/search/metadata":
            structured = "filter" in body
            if structured and self.reject_structured:
                return httpx.Response(400, json={"message": "Unknown filter"})
            items = list(self.assets.values())
            if not self.ignore_filters:
                library = body.get("filter", {}).get("libraryId", {}).get("eq", body.get("libraryId"))
                if library:
                    items = [item for item in items if item["libraryId"] == library]
                marker_ids = body.get("filter", {}).get("tagIds", {}).get("none", [])
                items = [item for item in items if not any(t["id"] in marker_ids for t in item.get("tags") or [])]
            size = body["size"]
            offset = int(body.get("cursor", 0)) if structured and not self.legacy else (body.get("page", 1) - 1) * size
            page = copy.deepcopy(items[offset:offset + size])
            if self.omit_search_tags:
                for item in page:
                    item.pop("tags", None)
            more = offset + size < len(items)
            token = "nextCursor" if structured and not self.legacy else "nextPage"
            next_value = str(offset + size) if token == "nextCursor" else str(offset // size + 2)
            return httpx.Response(200, json={"assets": {"items": page, token: next_value if more else None}})
        if path.startswith("/api/albums/"):
            return httpx.Response(200, json=self.albums[path.rsplit("/", 1)[1]])
        if path.endswith("/thumbnail"):
            return httpx.Response(200, content=b"fake-image")
        if path.startswith("/api/assets/"):
            return httpx.Response(200, json=self.assets[path.rsplit("/", 1)[1]])
        raise AssertionError(f"Unexpected request: {request.method} {path}")


class FakeEngine:
    def __init__(self):
        self.calls = 0
        self.prepared = False

    def prepare(self):
        self.prepared = True

    def predict_tags(self, image):
        assert self.prepared
        self.calls += 1
        return [TagPrediction(name="blue_hair", confidence=.8),
                TagPrediction(name="hatsune_miku", confidence=.95, kind="character")]
