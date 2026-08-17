import json

from wakefinder.common.exposure import total_token_exposure_eth, total_token_exposure_solana

TOKEN = "0xTOKEN"


class _Settings:
    def __init__(self, **paths):
        for k, v in paths.items():
            setattr(self, k, v)


def _write_positions(path, positions: dict) -> None:
    with open(path, "w") as f:
        json.dump(positions, f)


def test_eth_exposure_missing_files_returns_zero(tmp_path):
    settings = _Settings(
        copytrade_positions_file=str(tmp_path / "nope1.json"),
        snipe_positions_file=str(tmp_path / "nope2.json"),
    )
    assert total_token_exposure_eth(TOKEN, settings) == 0


def test_eth_exposure_sums_across_copytrade_and_snipe(tmp_path):
    copytrade_path = tmp_path / "positions.json"
    snipe_path = tmp_path / "positions_snipe.json"
    _write_positions(copytrade_path, {TOKEN: {"token": TOKEN, "entry_amount_in": 100}})
    _write_positions(snipe_path, {TOKEN: {"token": TOKEN, "entry_amount_in_wei": 50}})
    settings = _Settings(copytrade_positions_file=str(copytrade_path), snipe_positions_file=str(snipe_path))
    assert total_token_exposure_eth(TOKEN, settings) == 150


def test_eth_exposure_ignores_other_tokens(tmp_path):
    copytrade_path = tmp_path / "positions.json"
    snipe_path = tmp_path / "positions_snipe.json"
    _write_positions(copytrade_path, {"0xOTHER": {"token": "0xOTHER", "entry_amount_in": 100}})
    _write_positions(snipe_path, {})
    settings = _Settings(copytrade_positions_file=str(copytrade_path), snipe_positions_file=str(snipe_path))
    assert total_token_exposure_eth(TOKEN, settings) == 0


def test_eth_exposure_case_insensitive(tmp_path):
    copytrade_path = tmp_path / "positions.json"
    snipe_path = tmp_path / "positions_snipe.json"
    _write_positions(copytrade_path, {TOKEN: {"token": TOKEN.upper(), "entry_amount_in": 100}})
    _write_positions(snipe_path, {})
    settings = _Settings(copytrade_positions_file=str(copytrade_path), snipe_positions_file=str(snipe_path))
    assert total_token_exposure_eth(TOKEN.lower(), settings) == 100


def test_solana_exposure_sums_across_copytrade_and_snipe_by_mint(tmp_path):
    copytrade_path = tmp_path / "positions_solana.json"
    snipe_path = tmp_path / "positions_snipe_solana.json"
    _write_positions(copytrade_path, {TOKEN: {"token": TOKEN, "entry_amount_in": 100}})
    _write_positions(snipe_path, {TOKEN: {"mint": TOKEN, "entry_amount_in": 75}})
    settings = _Settings(solana_copytrade_positions_file=str(copytrade_path), solana_snipe_positions_file=str(snipe_path))
    assert total_token_exposure_solana(TOKEN, settings) == 175


def test_corrupt_json_file_treated_as_empty(tmp_path):
    path = tmp_path / "positions.json"
    path.write_text("not valid json")
    settings = _Settings(copytrade_positions_file=str(path), snipe_positions_file=str(tmp_path / "nope.json"))
    assert total_token_exposure_eth(TOKEN, settings) == 0


if __name__ == "__main__":
    print("тесты используют tmp_path — запускайте через pytest")
