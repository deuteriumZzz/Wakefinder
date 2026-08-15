"""Регрессионные тесты на утечку секретов и права доступа к файлам с ними —
см. README "Безопасность". Не заменяют аудит, но ловят самый частый класс
регрессии: кто-то завёл новое поле-секрет как str вместо SecretStr, или
куда-то в лог/сообщение исключения попал сырой .get_secret_value()."""

import logging
import os

from pydantic import SecretStr

from wakefinder.common.config import Settings, _warn_if_permissive

KEY_A = "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004"
KEY_B = "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99"

# Поля Settings, содержащие настоящие секреты (не адреса контрактов, не URL
# без ключа в пути и т.п.) — держим список явным, а не угадываем по имени,
# чтобы тест сам не стал источником ложной уверенности.
SECRET_FIELDS = {
    "eth_rpc_ws_url", "eth_rpc_http_url", "eth_private_key", "flashbots_signer_key",
    "solana_rpc_ws_url", "solana_rpc_http_url", "solana_private_key",
    "wallet_key_passphrase", "telegram_bot_token",
}


def test_all_known_secret_fields_are_secretstr():
    import typing

    for name in SECRET_FIELDS:
        annotation = Settings.model_fields[name].annotation
        is_secret = annotation is SecretStr or SecretStr in typing.get_args(annotation)
        assert is_secret, f"{name} должен быть типа SecretStr (или SecretStr | None)"


def test_settings_repr_does_not_leak_secret_values():
    secret_eth_ws = "wss://supersecret-provider.example/v3/abc123uniquekey"
    secret_telegram = "123456:AAsupersecrettelegramtoken"

    s = Settings(
        eth_rpc_ws_url=secret_eth_ws,
        eth_rpc_http_url="https://example/http",
        eth_private_key=KEY_A,
        flashbots_signer_key=KEY_B,
        telegram_bot_token=secret_telegram,
        _env_file=None,
    )

    dump = repr(s) + str(s) + str(vars(s))

    assert secret_eth_ws not in dump
    assert KEY_A not in dump
    assert KEY_B not in dump
    assert secret_telegram not in dump


def test_warn_if_permissive_logs_warning_for_group_readable_file(tmp_path, caplog):
    path = tmp_path / "secret.env"
    path.write_text("SECRET=1")
    os.chmod(path, 0o644)  # читаемо всеми — должно предупредить

    with caplog.at_level(logging.WARNING, logger="wakefinder.config"):
        _warn_if_permissive(str(path), "test-file")

    assert any("test-file" in record.message for record in caplog.records)


def test_warn_if_permissive_silent_for_owner_only_file(tmp_path, caplog):
    path = tmp_path / "secret.env"
    path.write_text("SECRET=1")
    os.chmod(path, 0o600)

    with caplog.at_level(logging.WARNING, logger="wakefinder.config"):
        _warn_if_permissive(str(path), "test-file")

    assert caplog.records == []


def test_warn_if_permissive_silent_for_missing_file(caplog):
    with caplog.at_level(logging.WARNING, logger="wakefinder.config"):
        _warn_if_permissive("/nonexistent/path/does/not/exist", "test-file")

    assert caplog.records == []


def test_warn_if_permissive_silent_for_none_path(caplog):
    with caplog.at_level(logging.WARNING, logger="wakefinder.config"):
        _warn_if_permissive(None, "test-file")

    assert caplog.records == []


if __name__ == "__main__":
    print("run via pytest (uses tmp_path/caplog fixtures)")
