# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

try:  # pragma: no cover - import failure depends on runtime
    import keyring
    from keyring.errors import KeyringError
except Exception:  # pragma: no cover - import failure depends on runtime
    keyring = None  # type: ignore[assignment]

    class KeyringError(Exception):  # type: ignore[no-redef]
        pass


DEFAULT_BASE_URL = "https://api.sanka.com"
DEFAULT_PROFILE = "default"
KEYRING_SERVICE = "sanka-cli"


class CredentialStoreError(RuntimeError):
    pass


def _config_directory() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "sanka"
    return Path(user_config_dir("sanka"))


def config_path() -> Path:
    return _config_directory() / "config.toml"


def _default_config() -> dict[str, Any]:
    return {
        "active_profile": DEFAULT_PROFILE,
        "profiles": {
            DEFAULT_PROFILE: {
                "base_url": DEFAULT_BASE_URL,
            }
        },
    }


def _normalize_profile_name(value: str | None) -> str:
    normalized = str(value or "").strip()
    return normalized or DEFAULT_PROFILE


def _quote(value: str) -> str:
    return json.dumps(str(value))


def _keyring_username(profile_name: str, token_kind: str) -> str:
    return f"{_normalize_profile_name(profile_name)}:{token_kind}"


def _get_keyring_password(profile_name: str, token_kind: str) -> str | None:
    if keyring is None:
        return None
    try:
        return keyring.get_password(
            KEYRING_SERVICE,
            _keyring_username(profile_name, token_kind),
        )
    except KeyringError as exc:  # pragma: no cover - OS keychain dependent
        raise CredentialStoreError(
            "Sanka CLI couldn't access the system keychain. "
            "Allow keychain access for the installed Python runtime, or run commands "
            "with SANKA_ACCESS_TOKEN."
        ) from exc


def _set_keyring_password(profile_name: str, token_kind: str, value: str) -> None:
    if keyring is None:
        raise CredentialStoreError(
            "Sanka CLI couldn't access a supported system keychain. "
            "Use SANKA_ACCESS_TOKEN for non-persistent auth."
        )
    try:
        keyring.set_password(
            KEYRING_SERVICE,
            _keyring_username(profile_name, token_kind),
            value,
        )
    except KeyringError as exc:  # pragma: no cover - OS keychain dependent
        raise CredentialStoreError(
            "Sanka CLI couldn't store tokens in the system keychain. "
            "Allow keychain access for the installed Python runtime, or use "
            "SANKA_ACCESS_TOKEN for non-persistent auth."
        ) from exc


def _delete_keyring_password(profile_name: str, token_kind: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(
            KEYRING_SERVICE,
            _keyring_username(profile_name, token_kind),
        )
    except Exception:  # pragma: no cover - deleting non-existent token is fine
        return


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return _default_config()

    import tomllib

    with path.open("rb") as handle:
        loaded = tomllib.load(handle) or {}

    config = _default_config()
    active_profile = _normalize_profile_name(loaded.get("active_profile"))
    profiles = loaded.get("profiles") or {}
    normalized_profiles: dict[str, dict[str, Any]] = {}
    for profile_name, profile_config in profiles.items():
        name = _normalize_profile_name(profile_name)
        normalized_profiles[name] = {
            "base_url": str((profile_config or {}).get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        }
    if normalized_profiles:
        config["profiles"] = normalized_profiles
    config["active_profile"] = active_profile
    if active_profile not in config["profiles"]:
        config["profiles"][active_profile] = {"base_url": DEFAULT_BASE_URL}
    return config


def save_config(config: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    active_profile = _normalize_profile_name(config.get("active_profile"))
    profiles = config.get("profiles") or {}

    lines = [f"active_profile = {_quote(active_profile)}", ""]
    for profile_name in sorted(profiles):
        profile = profiles[profile_name] or {}
        base_url = str(profile.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        lines.append(f"[profiles.{_quote(profile_name)}]")
        lines.append(f"base_url = {_quote(base_url)}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def upsert_profile(profile_name: str, *, base_url: str | None = None) -> dict[str, Any]:
    config = load_config()
    normalized_profile_name = _normalize_profile_name(profile_name)
    existing = config["profiles"].get(normalized_profile_name) or {}
    config["profiles"][normalized_profile_name] = {
        "base_url": str(base_url or existing.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
    }
    save_config(config)
    return config


def set_active_profile(profile_name: str) -> dict[str, Any]:
    config = load_config()
    normalized_profile_name = _normalize_profile_name(profile_name)
    if normalized_profile_name not in config["profiles"]:
        raise ValueError(f"profile not found: {normalized_profile_name}")
    config["active_profile"] = normalized_profile_name
    save_config(config)
    return config


def resolve_profile_name(profile_name: str | None) -> str:
    env_profile = os.environ.get("SANKA_PROFILE")
    if env_profile:
        return _normalize_profile_name(env_profile)
    if profile_name:
        return _normalize_profile_name(profile_name)
    return _normalize_profile_name(load_config().get("active_profile"))


def get_profile(profile_name: str | None = None) -> dict[str, Any]:
    normalized_profile_name = resolve_profile_name(profile_name)
    config = load_config()
    profile = config["profiles"].get(normalized_profile_name)
    if not profile:
        profile = {"base_url": DEFAULT_BASE_URL}
    return {
        "name": normalized_profile_name,
        "base_url": str(profile.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        "is_active": normalized_profile_name
        == _normalize_profile_name(config.get("active_profile")),
    }


def list_profiles() -> list[dict[str, Any]]:
    config = load_config()
    active_profile = _normalize_profile_name(config.get("active_profile"))
    profiles: list[dict[str, Any]] = []
    for profile_name in sorted(config["profiles"]):
        profile = config["profiles"][profile_name] or {}
        try:
            has_access_token = bool(_get_keyring_password(profile_name, "access_token"))
            has_refresh_token = bool(_get_keyring_password(profile_name, "refresh_token"))
        except CredentialStoreError:
            has_access_token = False
            has_refresh_token = False
        profiles.append(
            {
                "name": profile_name,
                "base_url": str(profile.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
                "is_active": profile_name == active_profile,
                "has_access_token": has_access_token,
                "has_refresh_token": has_refresh_token,
            }
        )
    return profiles


def store_tokens(
    profile_name: str,
    *,
    access_token: str,
    refresh_token: str | None = None,
) -> None:
    normalized_profile_name = _normalize_profile_name(profile_name)
    _set_keyring_password(normalized_profile_name, "access_token", access_token)
    if refresh_token:
        _set_keyring_password(normalized_profile_name, "refresh_token", refresh_token)
    else:
        _delete_keyring_password(normalized_profile_name, "refresh_token")


def clear_tokens(profile_name: str) -> None:
    normalized_profile_name = _normalize_profile_name(profile_name)
    _delete_keyring_password(normalized_profile_name, "access_token")
    _delete_keyring_password(normalized_profile_name, "refresh_token")


def get_tokens(profile_name: str) -> dict[str, str | None]:
    normalized_profile_name = _normalize_profile_name(profile_name)
    return {
        "access_token": _get_keyring_password(normalized_profile_name, "access_token"),
        "refresh_token": _get_keyring_password(
            normalized_profile_name,
            "refresh_token",
        ),
    }


def resolve_runtime(
    *,
    profile_name: str | None = None,
    base_url_override: str | None = None,
) -> dict[str, Any]:
    resolved_profile_name = resolve_profile_name(profile_name)
    profile = get_profile(resolved_profile_name)
    env_access_token = os.environ.get("SANKA_ACCESS_TOKEN")
    env_base_url = os.environ.get("SANKA_BASE_URL")
    stored_tokens: dict[str, str | None] = {"access_token": None, "refresh_token": None}
    if not env_access_token:
        stored_tokens = get_tokens(resolved_profile_name)

    return {
        "profile_name": resolved_profile_name,
        "base_url": str(
            base_url_override or env_base_url or profile["base_url"] or DEFAULT_BASE_URL
        ).rstrip("/"),
        "access_token": env_access_token or stored_tokens["access_token"],
        "refresh_token": stored_tokens["refresh_token"],
        "token_source": "env" if env_access_token else "keyring",
    }
