import hashlib
import hmac
import time
from urllib.parse import urlencode

from wakefinder.telegram_auth import verify_init_data

BOT_TOKEN = "123456:FAKE-BOT-TOKEN-FOR-TESTS-ONLY"


def _sign(fields: dict, bot_token: str = BOT_TOKEN) -> str:
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": computed_hash})


def test_valid_init_data_verifies():
    fields = {"user": '{"id":12345,"first_name":"Test"}', "auth_date": str(int(time.time()))}
    init_data = _sign(fields)
    result = verify_init_data(init_data, BOT_TOKEN)
    assert result is not None
    assert result["auth_date"] == fields["auth_date"]


def test_tampered_field_rejected():
    fields = {"user": '{"id":12345,"first_name":"Test"}', "auth_date": str(int(time.time()))}
    init_data = _sign(fields)
    tampered = init_data.replace("12345", "99999")
    assert verify_init_data(tampered, BOT_TOKEN) is None


def test_wrong_bot_token_rejected():
    fields = {"user": '{"id":12345,"first_name":"Test"}', "auth_date": str(int(time.time()))}
    init_data = _sign(fields, bot_token="wrong-token")
    assert verify_init_data(init_data, BOT_TOKEN) is None


def test_missing_hash_rejected():
    assert verify_init_data("user=%7B%22id%22%3A1%7D&auth_date=123", BOT_TOKEN) is None


def test_stale_auth_date_rejected():
    fields = {"user": '{"id":12345}', "auth_date": str(int(time.time()) - 999999)}
    init_data = _sign(fields)
    assert verify_init_data(init_data, BOT_TOKEN) is None


def test_empty_init_data_rejected():
    assert verify_init_data("", BOT_TOKEN) is None


def test_empty_bot_token_rejected():
    fields = {"user": '{"id":12345}', "auth_date": str(int(time.time()))}
    init_data = _sign(fields)
    assert verify_init_data(init_data, "") is None


if __name__ == "__main__":
    test_valid_init_data_verifies()
    test_tampered_field_rejected()
    test_wrong_bot_token_rejected()
    test_missing_hash_rejected()
    test_stale_auth_date_rejected()
    test_empty_init_data_rejected()
    test_empty_bot_token_rejected()
    print("ok")
