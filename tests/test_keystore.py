import os
import tempfile

import pytest

from wakefinder.common.keystore import decrypt_from_file, encrypt_to_file


def test_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "key.enc")
        encrypt_to_file("0xdeadbeef", "correct horse battery staple", path)
        assert decrypt_from_file(path, "correct horse battery staple") == "0xdeadbeef"


def test_wrong_passphrase_rejected():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "key.enc")
        encrypt_to_file("0xdeadbeef", "right passphrase", path)
        with pytest.raises(ValueError, match="не удалось расшифровать"):
            decrypt_from_file(path, "wrong passphrase")


def test_file_permissions_restricted():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "key.enc")
        encrypt_to_file("0xdeadbeef", "pw", path)
        assert oct(os.stat(path).st_mode)[-3:] == "600"


if __name__ == "__main__":
    test_round_trip()
    test_wrong_passphrase_rejected()
    test_file_permissions_restricted()
    print("ok")
