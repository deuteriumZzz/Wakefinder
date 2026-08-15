"""Шифрованное хранилище приватных ключей — альтернатива plaintext-ключам в
.env. Ключ шифрования выводится из пассфразы через PBKDF2HMAC (соль хранится
рядом с шифротекстом, сама пассфраза — никогда), сам секрет шифруется Fernet
(AES-128-CBC + HMAC, аутентифицированное шифрование из cryptography).

Не заменяет полноценный HSM/KMS (AWS KMS, Vault, ...) — это требует выбора
конкретного облачного вендора/инфраструктуры, которую бот не может выбрать
за пользователя. Это конкретный, самодостаточный шаг между "голый plaintext
в .env" и "полноценная корпоративная инфраструктура секретов": пассфраза
всё ещё должна откуда-то браться при старте процесса (env var
WALLET_KEY_PASSPHRASE) — файл на диске защищает от кражи самого файла
(бэкап, чужой доступ к диску), не от компрометации живого процесса.
"""

import argparse
import base64
import getpass
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 600_000  # текущая рекомендация OWASP для PBKDF2-HMAC-SHA256


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def encrypt_to_file(plaintext: str, passphrase: str, path: str) -> None:
    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(plaintext.encode())
    with open(path, "w") as f:
        json.dump({"salt": base64.b64encode(salt).decode(), "token": token.decode()}, f)
    os.chmod(path, 0o600)


def decrypt_from_file(path: str, passphrase: str) -> str:
    with open(path) as f:
        payload = json.load(f)
    salt = base64.b64decode(payload["salt"])
    try:
        return Fernet(_derive_key(passphrase, salt)).decrypt(payload["token"].encode()).decode()
    except InvalidToken as exc:
        raise ValueError(f"не удалось расшифровать {path} — неверная пассфраза или повреждённый файл") from exc


def _main() -> None:
    parser = argparse.ArgumentParser(description="Зашифровать приватный ключ в файл для WAKEFINDER (*_KEY_FILE + WALLET_KEY_PASSPHRASE)")
    parser.add_argument("output_path")
    args = parser.parse_args()

    secret = getpass.getpass("Приватный ключ (не отобразится, не попадёт в историю shell): ")
    passphrase = getpass.getpass("Пассфраза для шифрования: ")
    if passphrase != getpass.getpass("Повторите пассфразу: "):
        raise SystemExit("пассфразы не совпадают")

    encrypt_to_file(secret, passphrase, args.output_path)
    print(f"Зашифрованный ключ записан в {args.output_path}. Не забудьте WALLET_KEY_PASSPHRASE в окружении процесса.")


if __name__ == "__main__":
    _main()
