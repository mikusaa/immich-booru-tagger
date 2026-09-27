import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from immich_tagger.config import Settings
from immich_tagger.translation_catalog import TranslationCatalog
from conftest import LIBRARY_A, LIBRARY_B


@pytest.mark.parametrize("raw", [f"{LIBRARY_A},{LIBRARY_B},{LIBRARY_A}", json.dumps([LIBRARY_A, LIBRARY_B])])
def test_uuid_list_configuration(settings, monkeypatch, raw):
    monkeypatch.setenv("IMMICH_INCLUDE_LIBRARY_IDS", raw)
    parsed = Settings(_env_file=None, immich_base_url="http://test", immich_api_key="key")
    assert parsed.immich_include_library_ids == [LIBRARY_A, LIBRARY_B]


@pytest.mark.parametrize("values", [{"immich_api_key": "one"}, {"immich_api_keys": "one,two"},
                                    {"immich_api_keys": '["one", "two"]'},
                                    {"immich_libraries": '{"Alice":"one", "Bob":"two"}'}])
def test_authentication_forms(settings, values):
    parsed = Settings(_env_file=None, immich_base_url="http://test", **values)
    assert parsed.get_library_config()[0]["api_key"] == "one"
    assert "'one'" not in repr(parsed)


@pytest.mark.parametrize("values", [{}, {"immich_api_key": "one", "immich_api_keys": ["two"]},
                                    {"immich_api_key": "one", "immich_include_library_ids": ["folder"]},
                                    {"immich_api_key": "one", "batch_size": 1001}])
def test_invalid_configuration_rejected(settings, values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, immich_base_url="http://test", **values)


@pytest.mark.parametrize("english,chinese", [("true", "true"), ("false", "true"),
                                            ("true", "false"), ("false", "false")])
def test_tag_language_environment_configuration(settings, monkeypatch, english, chinese):
    monkeypatch.setenv("ENGLISH_TAGS_ENABLED", english)
    monkeypatch.setenv("TRANSLATIONS_ENABLED", chinese)
    if english == chinese == "false":
        with pytest.raises(ValidationError, match="ENGLISH_TAGS_ENABLED or TRANSLATIONS_ENABLED"):
            Settings(_env_file=None, immich_base_url="http://test", immich_api_key="key")
    else:
        parsed = Settings(_env_file=None, immich_base_url="http://test", immich_api_key="key")
        assert parsed.english_tags_enabled is (english == "true")
        assert parsed.translations_enabled is (chinese == "true")


def test_help_without_credentials_or_machine_learning_dependencies(settings, tmp_path):
    root = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-m", "immich_tagger.main", "--help"],
                            cwd=tmp_path, env={**os.environ, "PYTHONPATH": root}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "backfill-zh" in result.stdout
    result = subprocess.run([sys.executable, "-c", "import sys; import immich_tagger.main; "
                             "assert 'torch' not in sys.modules; assert 'wdtagger' not in sys.modules"],
                            cwd=tmp_path, env={**os.environ, "PYTHONPATH": root}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_bundled_catalog_and_overrides(settings, tmp_path):
    catalog = TranslationCatalog(settings.translation_file)
    assert len(catalog.tags) > 10000
    assert catalog.translate("hatsune_miku").startswith("角色/")
    assert catalog.translate("general").startswith("评级/")
    assert catalog.translate("unknown") is None
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"blue_hair": {"zh": "蓝发/蓝色头发", "kind": "general"}}))
    custom = TranslationCatalog(settings.translation_file, overrides)
    assert custom.translate("blue_hair") == "属性/蓝发／蓝色头发"
    assert custom.revision != catalog.revision
    assert custom.translate("hatsune_miku") == catalog.translate("hatsune_miku")


def test_invalid_translation_rejected_before_writes(settings, tmp_path):
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"bad": {"zh": "text\ntext", "kind": "general"}}))
    with pytest.raises(ValueError, match="control characters"):
        TranslationCatalog(settings.translation_file, overrides)


def test_configuration_errors_do_not_echo_api_keys(settings):
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, immich_base_url="http://test", immich_api_key="private-key-one",
                 immich_api_keys=["private-key-two"])
    assert "private-key" not in str(error.value)
