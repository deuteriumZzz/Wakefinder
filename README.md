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

## Operational requirements (Ethereum path)

- **RPC provider must support `eth_getRawTransactionByHash`.** Not universal —
  the bot fails loudly (logs and skips) if yours doesn't. Verify before relying on it.
- **Approve every router you might trade through** — the target router
  (`ETH_ROUTER_ADDRESS`) *and* every reference router passed into
  `reference_pools` — from `ETH_PRIVATE_KEY`'s wallet, for every token
  involved. One-time setup, not part of the hot path.
- `reference_pools` (passed to `run()`, not in `.env`) must map each watched
  pool to a genuinely different DEX's pool for the same pair — a backrun
  against a single pool alone isn't an arbitrage.
- `MAX_CAPITAL_PER_BUNDLE_ETH` assumes `token_in` is 18-decimal (WETH). Don't
  point this at a pair where `token_in` has different decimals until that's fixed.

## Safety

- Start on testnet (Sepolia) — no real capital until the full cycle is validated there.
- `MAX_GAS_GWEI` / `MAX_CAPITAL_PER_BUNDLE_ETH` in `.env` cap per-bundle risk;
  `ETH_ROUTER_ADDRESS` is checked against an allowlist in `config.py` — an
  unlisted router (typo or tampered `.env`) refuses to start.
- The bot refuses to start if `ETH_PRIVATE_KEY` and `FLASHBOTS_SIGNER_KEY`
  resolve to the same wallet — the signer key must never hold funds.
- Default strategy is backrun/arbitrage, not sandwich (sandwich directly harms the
  tracked trader — that's an explicit opt-in, not the default).
- **Kill switch**: `touch .kill` (path configurable via `KILL_SWITCH_FILE`) to
  stop the bot before its next action, no redeploy needed. `rm .kill` to resume.
- Balance is checked before every bundle — both native ETH (covers gas for both
  legs at the fee it's about to bid) and `token_in` — the bot skips the
  opportunity rather than sign a transaction it can't afford.
- Bundle inclusion tip scales with `PROFIT_SHARE_BPS` (default 90%) of the
  opportunity's own captured profit, not a flat rate — bids close to what the
  opportunity is actually worth, which is what it takes to win the block
  builder's inclusion auction against other searchers.
