"""Минимальные фрагменты ABI — Uniswap V2 Router/Pair (и совместимые форки: Sushiswap и т.п.).
Расширяйте другими функциями роутера (swapTokensForExactTokens, ...*SupportingFeeOnTransferTokens)
по мере необходимости; эти две покрывают типичный случай свопа кита.
"""

ROUTER_ABI = [
    {
        "name": "swapExactTokensForTokens",
        "type": "function",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "swapExactETHForTokens",
        "type": "function",
        "inputs": [
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "payable": True,
    },
    {
        "name": "getAmountsOut",
        "type": "function",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
    },
    {
        "name": "swapExactTokensForETH",
        "type": "function",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "addLiquidityETH",
        "type": "function",
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "amountTokenDesired", "type": "uint256"},
            {"name": "amountTokenMin", "type": "uint256"},
            {"name": "amountETHMin", "type": "uint256"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountToken", "type": "uint256"},
            {"name": "amountETH", "type": "uint256"},
            {"name": "liquidity", "type": "uint256"},
        ],
        "payable": True,
    },
]

ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "approve",
        "type": "function",
        "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

FACTORY_ABI = [
    {
        "name": "getPair",
        "type": "function",
        "inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}],
        "outputs": [{"name": "pair", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "name": "PairCreated",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "token0", "type": "address", "indexed": True},
            {"name": "token1", "type": "address", "indexed": True},
            {"name": "pair", "type": "address", "indexed": False},
            {"name": "", "type": "uint256", "indexed": False},
        ],
    },
]

PAIR_ABI = [
    {
        "name": "getReserves",
        "type": "function",
        "inputs": [],
        "outputs": [
            {"name": "reserve0", "type": "uint112"},
            {"name": "reserve1", "type": "uint112"},
            {"name": "blockTimestampLast", "type": "uint32"},
        ],
        "stateMutability": "view",
    },
    {
        "name": "token0",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "name": "token1",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "name": "Swap",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "sender", "type": "address", "indexed": True},
            {"name": "amount0In", "type": "uint256", "indexed": False},
            {"name": "amount1In", "type": "uint256", "indexed": False},
            {"name": "amount0Out", "type": "uint256", "indexed": False},
            {"name": "amount1Out", "type": "uint256", "indexed": False},
            {"name": "to", "type": "address", "indexed": True},
        ],
    },
]
