import asyncio

from solders.keypair import Keypair

from wakefinder.chains.solana.sender import JitoBundleSender
from wakefinder.common.interfaces import Bundle


def test_dry_run_skips_real_send():
    sender = JitoBundleSender("https://example/block-engine", Keypair(), dry_run=True)
    calls = {"send_bundle": 0}
    sender.jito.send_bundle = lambda params: calls.__setitem__("send_bundle", calls["send_bundle"] + 1)
    result = asyncio.run(sender.send(Bundle(raw_txs=["dGVzdA=="], target_block=0)))
    assert result is True
    assert calls["send_bundle"] == 0  # реальная отправка НЕ происходит


def test_live_send_calls_jito():
    sender = JitoBundleSender("https://example/block-engine", Keypair(), dry_run=False)
    sender.jito.send_bundle = lambda params: {"success": True, "data": {"result": "sig"}}
    result = asyncio.run(sender.send(Bundle(raw_txs=["dGVzdA=="], target_block=0)))
    assert result is True


if __name__ == "__main__":
    test_dry_run_skips_real_send()
    test_live_send_calls_jito()
    print("ok")
