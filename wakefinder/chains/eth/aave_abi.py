"""Минимальные ABI-фрагменты Aave V3 (IPool/IPoolDataProvider/IPriceOracle) —
только функции, реально используемые chains/eth/liquidate.py и
liquidation_watcher.py, не полный интерфейс протокола.

ЧЕСТНО: адреса контрактов (config.py: aave_pool_address и т.д.) и сигнатуры
функций взяты из документированного, стабильного публичного интерфейса
Aave V3 (github.com/aave/aave-v3-core) — актуальность конкретных АДРЕСОВ
для mainnet НЕ проверена вживую в этой песочнице (нет сетевого доступа к
Ethereum RPC, та же честная оговорка, что и у builder RPC URL в README).
ПРОВЕРЬТЕ САМИ перед использованием — официальный реестр:
https://github.com/bgd-labs/aave-address-book"""

AAVE_POOL_ABI = [
    {
        "name": "liquidationCall",
        "type": "function",
        "inputs": [
            {"name": "collateralAsset", "type": "address"},
            {"name": "debtAsset", "type": "address"},
            {"name": "user", "type": "address"},
            {"name": "debtToCover", "type": "uint256"},
            {"name": "receiveAToken", "type": "bool"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]

AAVE_POOL_DATA_PROVIDER_ABI = [
    {
        "name": "getReserveConfigurationData",
        "type": "function",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "decimals", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "liquidationThreshold", "type": "uint256"},
            {"name": "liquidationBonus", "type": "uint256"},
            {"name": "reserveFactor", "type": "uint256"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
            {"name": "borrowingEnabled", "type": "bool"},
            {"name": "stableBorrowRateEnabled", "type": "bool"},
            {"name": "isActive", "type": "bool"},
            {"name": "isFrozen", "type": "bool"},
        ],
        "stateMutability": "view",
    },
]

AAVE_PRICE_ORACLE_ABI = [
    {
        "name": "getAssetPrice",
        "type": "function",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]
