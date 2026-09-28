import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ("ENGLISH_TAGS_ENABLED", "TRANSLATIONS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    for key in Settings.model_fields:
        monkeypatch.delenv(key.upper(), raising=False)
    return Settings(_env_file=None, immich_base_url="http://immich.test", immich_api_key="test-key",
                    state_dir=tmp_path / "state", model_cache_dir=tmp_path / "models", max_retries=0)
