"""Минимальные ABI-фрагменты Uniswap V3 (NonfungiblePositionManager/
IUniswapV3Pool/SwapRouter02) — только функции, реально используемые
chains/eth/jit_liquidity.py и v3_swap_watcher.py, не полный интерфейс.

ЧЕСТНО: адреса контрактов (config.py) и сигнатуры функций — из
документированного, стабильного публичного интерфейса Uniswap V3
(github.com/Uniswap/v3-periphery, github.com/Uniswap/v3-core) — актуальность
конкретных АДРЕСОВ для mainnet НЕ проверена вживую в этой песочнице (нет
сетевого доступа), та же оговорка, что у Aave/CoW Protocol адресов в этом
проекте. ПРОВЕРЬТЕ САМИ перед использованием.

Минт/decrease/collect идут через NonfungiblePositionManager (периферийный
контракт САМОГО Uniswap), а не прямым вызовом IUniswapV3Pool.mint() — прямой
вызов пула требует, чтобы ВЫЗЫВАЮЩИЙ реализовывал IUniswapV3MintCallback
(колбэк для оплаты позиции мидтранзакционно), что физически невозможно с
голого EOA-аккаунта без деплоя отдельного Solidity-контракта — того, что
этот Python-проект нигде не делает (см. README "Ликвидации на Aave V3" про
тот же класс ограничения). NonfungiblePositionManager сам реализует этот
колбэк и просто списывает токены с вызывающего через обычный ERC20
transferFrom — тот же путь, которым пользуется веб-интерфейс Uniswap."""

NPM_ABI = [
    {
        "name": "mint",
        "type": "function",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "token0", "type": "address"},
                    {"name": "token1", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "tickLower", "type": "int24"},
                    {"name": "tickUpper", "type": "int24"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                ],
            }
        ],
        "outputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
    },
    {
        "name": "decreaseLiquidity",
        "type": "function",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "liquidity", "type": "uint128"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
            }
        ],
        "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}],
        "stateMutability": "payable",
    },
    {
        "name": "collect",
        "type": "function",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amount0Max", "type": "uint128"},
                    {"name": "amount1Max", "type": "uint128"},
                ],
            }
        ],
        "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}],
        "stateMutability": "payable",
    },
    {
        "name": "multicall",
        "type": "function",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [{"name": "results", "type": "bytes[]"}],
        "stateMutability": "payable",
    },
    {
        "name": "IncreaseLiquidity",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "tokenId", "type": "uint256", "indexed": True},
            {"name": "liquidity", "type": "uint128", "indexed": False},
            {"name": "amount0", "type": "uint256", "indexed": False},
            {"name": "amount1", "type": "uint256", "indexed": False},
        ],
    },
    {
        "name": "Collect",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "tokenId", "type": "uint256", "indexed": True},
            {"name": "recipient", "type": "address", "indexed": False},
            {"name": "amount0", "type": "uint256", "indexed": False},
            {"name": "amount1", "type": "uint256", "indexed": False},
        ],
    },
]

POOL_ABI = [
    {
        "name": "slot0",
        "type": "function",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
    },
    {"name": "liquidity", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "uint128"}], "stateMutability": "view"},
    {"name": "fee", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "uint24"}], "stateMutability": "view"},
    {"name": "tickSpacing", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "int24"}], "stateMutability": "view"},
    {"name": "token0", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "address"}], "stateMutability": "view"},
    {"name": "token1", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "address"}], "stateMutability": "view"},
]

# SwapRouter02 — deadline отсутствует в ExactInputSingleParams (в отличие от
# оригинального SwapRouter v1) — только для ДЕКОДИРОВАНИЯ pending-калдаты
# чужих свопов в v3_swap_watcher.py, наши собственные транзакции этот ABI
# не строит.
SWAP_ROUTER_02_ABI = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
    },
]
