# Polymarket Documentation & Support Skill

This skill provides a comprehensive reference for interacting with Polymarket's on-chain and off-chain infrastructure.

## 1. Endpoints

| Environment | Base URL | Description |
| :--- | :--- | :--- |
| **CLOB API** | `https://clob.polymarket.com` | Orderbook, order placement, and real-time prices. |
| **Gamma API** | `https://gamma-api.polymarket.com` | Market metadata, questions, and settlement info. |
| **Relayer V2** | `https://relayer-v2.polymarket.com` | Gasless transaction submission for Proxy/Safe wallets. |
| **Data API** | `https://data-api.polymarket.com` | Wallet-level stats, P&L, and historical trade data. |

## 2. Smart Contract Registry (Polygon)

| Contract | Address |
| :--- | :--- |
| **USDC.e (Collateral)** | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| **ConditionalTokens (CTF)**| `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| **CTF Exchange** | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| **NegRisk Exchange** | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |

## 3. Core Mechanics

### A. Redemption Logic
To convert winning tokens into USDC, you must call `redeemPositions` on the `ConditionalTokens` contract.
*   **Collateral**: `USDC_E_ADDRESS`
*   **ParentCollectionId**: `0x00...00` (Bytes32 Zero)
*   **ConditionId**: Derived from market metadata.
*   **IndexSets**: `[1, 2]` (For Binary Markets).

### B. Proxy Wallet (Safe) Execution
Most users utilize a Gnosis Safe proxy. For a bot to execute redemptions:
1. Construct the `redeemPositions` data.
2. Call `execTransaction` on the Proxy address.
3. Sign with the **Owner's Private Key**.

## 4. py-clob-client Usage

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs

# Initialize Client
client = ClobClient(
    host="https://clob.polymarket.com", 
    key=PRIVATE_KEY, 
    chain_id=137,
    signature_type=1, # 1 for POLY_PROXY (Safe)
    funder=PROXY_WALLET_ADDRESS
)
```

## 5. Helpful Snippets

### Fetching a Market by Slug
`GET /markets?slug=btc-updown-5m-123456`

### Redeeming (Web3.py Concept)
```python
contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
tx = contract.functions.redeemPositions(USDC_E, ZERO_BYTES, condition_id, [1, 2]).build_transaction({
    'from': PROXY_WALLET_ADDRESS,
    'gas': 200000,
    # ... execTransaction wrap if Proxy
})
```
