"""Well-known mainnet contracts so filters like Uniswap V4 and USDT match immediately.

User-saved labels override these. Pool contracts Etherscan has individually named
are not in this list; those swaps are still tagged from the router that sent them.
"""

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

# address -> label shown in From / To
KNOWN_LABELS: dict[str, str] = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2: Router 2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3: Router",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3: Router 2",
    "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b": "Uniswap: Universal Router",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap: Universal Router",
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "Uniswap: Universal Router v2",
    "0x4c82d1fbfe28c977cbb58d8c7ff8fcf9f70a2cca": "Uniswap: Universal Router 2.1.1",
    "0x23617e59a5925b2a4bf75d73ff6711cd0b29de85": "Uniswap: Universal Router 2.1.2",
    "0x000000000004444c5dc75cb358380d2e3de08a90": "Uniswap V4: Pool Manager",
    "0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e": "Uniswap V4: Position Manager",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "Tether USD (USDT)",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USD Coin (USDC)",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "Dai Stablecoin (DAI)",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "Wrapped Ether (WETH)",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "Wrapped BTC (WBTC)",
    "0xba12222222228d8ba445958a75a0704d566bf2c8": "Balancer: Vault",
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "Lido: stETH",
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f": "SushiSwap: Router",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch v5: Router",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch v6: Router",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x: Exchange Proxy",
}

# tx.to -> protocol name, used when the router itself is not a token-transfer party.
DIRECT_PROTOCOL: dict[str, str] = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3",
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f": "SushiSwap",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x",
    "0xba12222222228d8ba445958a75a0704d566bf2c8": "Balancer",
}

# Older universal routers have no V4 command. The 0x10 command is V4 only on the routers below.
UNIVERSAL_ROUTERS = {
    "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af",
    "0x4c82d1fbfe28c977cbb58d8c7ff8fcf9f70a2cca",
    "0x23617e59a5925b2a4bf75d73ff6711cd0b29de85",
}
V4_CAPABLE_ROUTERS = {
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af",
    "0x4c82d1fbfe28c977cbb58d8c7ff8fcf9f70a2cca",
    "0x23617e59a5925b2a4bf75d73ff6711cd0b29de85",
}
V4_PARTIES = {
    "0x000000000004444c5dc75cb358380d2e3de08a90",
    "0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e",
}

# Universal Router command ids. High bits are flags (0x40, 0x80) and are masked off.
V3_COMMANDS = {0x00, 0x01}
V2_COMMANDS = {0x08, 0x09}
V4_COMMAND = 0x10
COMMAND_MASK = 0x3F
