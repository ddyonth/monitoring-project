"""
Секреты: переменные окружения MONITORING_API_KEY / MONITORING_CLIENT_UPDATE_KEY
имеют приоритет над server/config.json; без них поведение прежнее
(значение из config.json, а при его отсутствии — константа из кода).
"""

import json

import pytest
import server_app
from fastapi import HTTPException
from server_app import _cfg_get, require_api_key, require_client_key


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Временный config.json вместо server/config.json из репозитория."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "api_key": "KEY_FROM_FILE",
        "client_update_key": "CLIENT_KEY_FROM_FILE",
        "listen_port": 8123,
    }), encoding="utf-8")
    monkeypatch.setattr(server_app, "CONFIG_PATH", str(path))
    return path


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("MONITORING_API_KEY", raising=False)
    monkeypatch.delenv("MONITORING_CLIENT_UPDATE_KEY", raising=False)


class TestEnvOverridesConfig:
    def test_api_key_env_overrides_file(self, config_file, clean_env, monkeypatch):
        monkeypatch.setenv("MONITORING_API_KEY", "KEY_FROM_ENV")
        assert _cfg_get("api_key", server_app.API_KEY_DEFAULT) == "KEY_FROM_ENV"
        require_api_key("KEY_FROM_ENV")  # принимается
        with pytest.raises(HTTPException) as exc:
            require_api_key("KEY_FROM_FILE")  # значение из файла больше не подходит
        assert exc.value.status_code == 401

    def test_client_update_key_env_overrides_file(self, config_file, clean_env, monkeypatch):
        monkeypatch.setenv("MONITORING_CLIENT_UPDATE_KEY", "CLIENT_KEY_FROM_ENV")
        assert _cfg_get("client_update_key", server_app.CLIENT_UPDATE_KEY_DEFAULT) == "CLIENT_KEY_FROM_ENV"
        require_client_key("CLIENT_KEY_FROM_ENV")
        with pytest.raises(HTTPException):
            require_client_key("CLIENT_KEY_FROM_FILE")

    def test_env_does_not_affect_other_keys(self, config_file, clean_env, monkeypatch):
        monkeypatch.setenv("MONITORING_API_KEY", "KEY_FROM_ENV")
        assert _cfg_get("listen_port", 8000) == 8123

    def test_empty_env_is_treated_as_unset(self, config_file, clean_env, monkeypatch):
        monkeypatch.setenv("MONITORING_API_KEY", "   ")
        assert _cfg_get("api_key", server_app.API_KEY_DEFAULT) == "KEY_FROM_FILE"


class TestFallbackWithoutEnv:
    def test_api_key_from_file_when_env_unset(self, config_file, clean_env):
        assert _cfg_get("api_key", server_app.API_KEY_DEFAULT) == "KEY_FROM_FILE"
        require_api_key("KEY_FROM_FILE")
        with pytest.raises(HTTPException) as exc:
            require_api_key("KEY_FROM_ENV")
        assert exc.value.status_code == 401

    def test_client_update_key_from_file_when_env_unset(self, config_file, clean_env):
        assert _cfg_get("client_update_key", server_app.CLIENT_UPDATE_KEY_DEFAULT) == "CLIENT_KEY_FROM_FILE"
        require_client_key("CLIENT_KEY_FROM_FILE")

    def test_defaults_when_no_config_file_and_no_env(self, tmp_path, clean_env, monkeypatch):
        monkeypatch.setattr(server_app, "CONFIG_PATH", str(tmp_path / "missing.json"))
        assert _cfg_get("api_key", server_app.API_KEY_DEFAULT) == server_app.API_KEY_DEFAULT
        assert _cfg_get("client_update_key", server_app.CLIENT_UPDATE_KEY_DEFAULT) == server_app.CLIENT_UPDATE_KEY_DEFAULT
        require_api_key(server_app.API_KEY_DEFAULT)
        require_client_key(server_app.CLIENT_UPDATE_KEY_DEFAULT)

    def test_repo_config_json_still_uses_placeholder(self, clean_env):
        # Файл-шаблон server/config.json остаётся с placeholder, чтобы сервер
        # стартовал локально без настройки.
        assert _cfg_get("api_key", server_app.API_KEY_DEFAULT) == "CHANGE_ME_LOCAL_KEY"
