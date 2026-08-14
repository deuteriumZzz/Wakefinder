# mevbot

Standalone MEV searcher bot (mempool sniffing → AMM simulation → Flashbots/Jito bundle
submission). Independent from BitbotBY — this is on-chain (ETH/Solana), BitbotBY is CEX/ccxt.

Build order: Ethereum full cycle first, then Solana over the same `mevbot/common/interfaces.py`
contracts (`MempoolWatcher`, `Simulator`, `BundleSender`).

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env  # fill in your own RPC/keys — never commit .env
```

## Safety

- Start on testnet (Sepolia) — no real capital until the full cycle is validated there.
- `MAX_GAS_GWEI` / `MAX_CAPITAL_PER_BUNDLE_ETH` in `.env` cap per-bundle risk.
- Default strategy is backrun/arbitrage, not sandwich (sandwich directly harms the
  tracked trader — that's an explicit opt-in, not the default).
