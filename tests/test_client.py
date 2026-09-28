import httpx
import pytest

from immich_tagger.immich_client import ImmichAPIError, ImmichClient
from conftest import ALBUM, LIBRARY_A, LIBRARY_B, FakeImmich, asset, tag


@pytest.mark.parametrize("legacy,reject", [(False, False), (True, False), (True, True)])
def test_pagination_beyond_default_batch_and_manual_tags(settings, legacy, reject):
    marker = tag(settings.processed_tag_name)
    server = FakeImmich([asset(i) for i in range(503)], legacy=legacy, reject_structured=reject)
    server.assets["0"]["tags"] = [marker]
    server.assets["1"]["tags"] = [tag("manual")]
    server.omit_search_tags = True
    with server.factory(settings, settings.get_library_config()[0]) as client:
        found = list(client.iter_assets(processed_tag_id=marker["id"]))
    assert len(found) == 502
    assert found[0].id == "1"
    assert len({item.id for item in found}) == 502
    queries = [c[2] for c in server.calls if c[0] == "POST"]
    assert all(q["size"] == 250 for q in queries)
    assert all("tagIds" not in q for q in queries)
    assert all(q["orderBy"]["field"] == "fileCreatedAt" for q in queries if "orderBy" in q)


@pytest.mark.parametrize("legacy", [False, True])
def test_library_album_intersection_and_exclusions_even_if_server_ignores_scope(settings, legacy):
    settings.immich_include_library_ids = [LIBRARY_A, LIBRARY_B]
    settings.immich_include_album_ids = [ALBUM]
    settings.immich_exclude_library_ids = [LIBRARY_B]
    server = FakeImmich([asset(0, libraryId=LIBRARY_A), asset(1, libraryId=LIBRARY_B),
                        asset(2), asset(3, libraryId=LIBRARY_A),
                        asset(4, libraryId=LIBRARY_A, type="VIDEO")], legacy=legacy)
    server.ignore_filters = True
    server.albums[ALBUM] = {"assets": [{"id": str(i)} for i in [0, 1, 2, 4]]}
    with server.factory(settings, settings.get_library_config()[0]) as client:
        assert [a.id for a in client.iter_assets()] == ["0"]


def test_ignored_library_filter_does_not_dedupe_away_later_library(settings):
    settings.immich_include_library_ids = [LIBRARY_A, LIBRARY_B]
    server = FakeImmich([asset(0, libraryId=LIBRARY_A), asset(1, libraryId=LIBRARY_B)])
    server.ignore_filters = True
    with server.factory(settings, settings.get_library_config()[0]) as client:
        assert [a.id for a in client.iter_assets()] == ["0", "1"]


def test_upload_assets_with_null_library_are_scoped_by_album(settings):
    settings.immich_include_album_ids = [ALBUM]
    server = FakeImmich([asset(0), asset(1)])
    server.albums[ALBUM] = {"assets": [{"id": "1"}]}
    with server.factory(settings, settings.get_library_config()[0]) as client:
        assert [a.id for a in client.iter_assets()] == ["1"]


def test_missing_album_membership_fails_closed(settings):
    settings.immich_include_album_ids = [ALBUM]
    server = FakeImmich([asset(0)])
    server.albums[ALBUM] = {}
    with server.factory(settings, settings.get_library_config()[0]) as client:
        with pytest.raises(ImmichAPIError, match="unscoped"):
            list(client.iter_assets())
    assert not any(c[0] == "POST" for c in server.calls)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_auth_and_server_errors_never_fallback(settings, status):
    server = FakeImmich()
    server.error_status = status
    with server.factory(settings, settings.get_library_config()[0]) as client:
        with pytest.raises(ImmichAPIError):
            list(client.iter_assets())
    assert len(server.calls) == 1


@pytest.mark.parametrize("section", [{"items": [asset(0)], "nextCursor": "same"}, {"items": []}])
def test_broken_pagination_fails(settings, section):
    settings.search_api = "structured"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"assets": section}))
    with ImmichClient(settings, transport=transport) as client:
        with pytest.raises(ImmichAPIError, match="pagination|nextCursor"):
            list(client.iter_assets())


def test_tags_cache_by_full_path_and_incomplete_upsert(settings):
    server = FakeImmich()
    server.tags = {name: tag(name) for name in ["a/leaf", "b/leaf"]}
    with server.factory(settings, settings.get_library_config()[0]) as client:
        tags = client.get_or_create_tags_bulk(["a/leaf", "b/leaf", "a/leaf"])
        assert tags["a/leaf"].id != tags["b/leaf"].id
        assert not server.writes
        server.incomplete_upsert = True
        with pytest.raises(ImmichAPIError, match="incomplete"):
            client.get_or_create_tags_bulk(["new"])


def test_write_guard_and_readback(settings):
    server = FakeImmich([asset(0)])
    with server.factory(settings, settings.get_library_config()[0], dry_run=True) as client:
        client.test_connection()
        with pytest.raises(ImmichAPIError, match="Dry run"):
            client.get_or_create_tag("new")
        assert not server.writes
    with server.factory(settings, settings.get_library_config()[0]) as client:
        new = client.get_or_create_tag("new")
        server.drop_assignment = True
        with pytest.raises(ImmichAPIError, match="incomplete"):
            client.tag_single_asset("0", [new.id])


def test_cleanup_queue_status_and_pause_validation(settings):
    server = FakeImmich()
    with server.factory(settings, {"name": "admin", "api_key": "admin-key"}) as client:
        assert client.get_cleanup_queue("sidecar")["isPaused"] is False
        assert client.set_cleanup_queue_paused("sidecar", True)["isPaused"] is True
        with pytest.raises(ValueError):
            client.get_cleanup_queue("library")
        with pytest.raises(ValueError):
            client.set_cleanup_queue_paused("sidecar", 1)


def test_transport_retry(settings, monkeypatch):
    settings.max_retries = 2
    attempts = []
    sleeps = []
    monkeypatch.setattr("immich_tagger.immich_client.time.sleep", sleeps.append)
    def handle(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[])
    with ImmichClient(settings, transport=httpx.MockTransport(handle)) as client:
        assert client.test_connection()
    assert sleeps == [2, 2]
