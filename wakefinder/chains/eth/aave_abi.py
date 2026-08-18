"""Минимальные ABI-фрагменты Aave V3 (IPool/IPoolDataProvider/IPriceOracle) —
только функции, реально используемые chains/eth/liquidate.py,
liquidation_watcher.py и liquidation_scanner.py, не полный интерфейс
протокола. getUserAccountData/Borrow/getUserReserveData — для АКТИВНОГО
сканирования должников (liquidation_scanner.py), в отличие от liquidationCall,
на которую реагирует только реактивный watcher.

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
    {
        "name": "getUserAccountData",
        "type": "function",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
    },
    {
        "name": "Borrow",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "reserve", "type": "address", "indexed": True},
            {"name": "user", "type": "address", "indexed": False},
            {"name": "onBehalfOf", "type": "address", "indexed": False},
            {"name": "amount", "type": "uint256", "indexed": False},
            {"name": "interestRateMode", "type": "uint8", "indexed": False},
            {"name": "borrowRate", "type": "uint256", "indexed": False},
            {"name": "referralCode", "type": "uint16", "indexed": True},
        ],
    },
    {
        "name": "LiquidationCall",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "collateralAsset", "type": "address", "indexed": True},
            {"name": "debtAsset", "type": "address", "indexed": True},
            {"name": "user", "type": "address", "indexed": True},
            {"name": "debtToCover", "type": "uint256", "indexed": False},
            {"name": "liquidatedCollateralAmount", "type": "uint256", "indexed": False},
            {"name": "liquidator", "type": "address", "indexed": False},
            {"name": "receiveAToken", "type": "bool", "indexed": False},
        ],
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
    {
        "name": "getUserReserveData",
        "type": "function",
        "inputs": [{"name": "asset", "type": "address"}, {"name": "user", "type": "address"}],
        "outputs": [
            {"name": "currentATokenBalance", "type": "uint256"},
            {"name": "currentStableDebt", "type": "uint256"},
            {"name": "currentVariableDebt", "type": "uint256"},
            {"name": "principalStableDebt", "type": "uint256"},
            {"name": "scaledVariableDebt", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "stableRateLastUpdated", "type": "uint40"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
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
