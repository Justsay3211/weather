"""
CLOB Client — V2 with neg_risk support for Weather Bot.

Weather markets are NEGATIVE RISK (multi-outcome, only one wins).
All orders MUST use neg_risk=True and PartialCreateOrderOptions.

Based on polymarket-bot-v2/data/clob_client.py (proven working).
"""

import math
import time
import base64
import requests
from typing import Dict, List, Optional, Any

from config import Config
from logger import log


def _fix_base64_padding():
    """Monkey-patch py-clob-client-v2 HMAC to handle missing base64 padding."""
    try:
        import py_clob_client_v2.signing.hmac as hmac_module
        _original_b64decode = base64.b64decode

        def _safe_b64decode(s, *args, **kwargs):
            if isinstance(s, str):
                s += '=' * (-len(s) % 4)
            elif isinstance(s, bytes):
                s += b'=' * (-len(s) % 4)
            return _original_b64decode(s, *args, **kwargs)

        if hasattr(hmac_module, 'base64'):
            hmac_module.base64.b64decode = _safe_b64decode
    except Exception:
        pass

_fix_base64_padding()


class ClobClient:
    """Polymarket CLOB V2 client — weather bot (neg_risk markets)."""

    def __init__(self):
        self.base_url = Config.get_clob_url()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': f'WeatherSniper/{Config.VERSION}',
            'Accept': 'application/json',
        })
        self._py_clob_client = None
        self._wallet_address = ''
        self._builder_code = '0x' + '0' * 64

    def init_py_clob_client(self, private_key: str, funder: str = None,
                            signature_type: int = 3) -> Any:
        """Initialize CLOB V2 client with auth (same flow as polymarket-bot-v2)."""
        pk = private_key.strip()
        if not pk.startswith('0x'):
            pk = '0x' + pk

        try:
            from eth_account import Account
            self._wallet_address = Account.from_key(pk).address
        except Exception:
            pass

        from py_clob_client_v2 import ClobClient as PyClobV2
        from py_clob_client_v2.clob_types import ApiCreds

        # Builder config
        builder_config = None
        if Config.POLY_BUILDER_CODE:
            self._builder_code = Config.POLY_BUILDER_CODE.strip()
            try:
                from py_clob_client_v2.clob_types import BuilderConfig
                builder_config = BuilderConfig(
                    builder_address=Config.POLY_PROXY_WALLET.strip() if Config.POLY_PROXY_WALLET else self._wallet_address,
                    builder_code=self._builder_code,
                )
                log.info(f"Builder code: {self._builder_code[:12]}...")
            except Exception as e:
                log.debug(f"Builder config skip: {e}")

        client = PyClobV2(
            host=self.base_url,
            chain_id=Config.POLY_CHAIN_ID,
            key=pk,
            signature_type=signature_type,
            funder=funder,
            builder_config=builder_config,
        )

        # Auth flow (4 methods, same as bot-v2)
        api_key_obj = None

        # 1. Manual creds from .env
        if Config.POLY_API_KEY and Config.POLY_API_SECRET and Config.POLY_PASSPHRASE:
            api_secret = Config.POLY_API_SECRET.strip()
            api_secret += '=' * (-len(api_secret) % 4)
            api_key_obj = ApiCreds(
                api_key=Config.POLY_API_KEY.strip(),
                api_secret=api_secret,
                api_passphrase=Config.POLY_PASSPHRASE.strip(),
            )
            log.info("Using manual API creds from .env")
        else:
            # 2. Derive existing
            try:
                api_key_obj = client.derive_api_key()
                log.info("Derived API key successfully")
            except Exception:
                # 3. Create or derive
                try:
                    api_key_obj = client.create_or_derive_api_key()
                    log.info("Created/derived API key")
                except Exception as e:
                    raise RuntimeError(f"CLOB auth failed: {e}")

        client.set_api_creds(api_key_obj)
        self._py_clob_client = client

        # Sync balance
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=signature_type,
            )
            client.update_balance_allowance(params)
            bal = client.get_balance_allowance(params)
            if bal:
                raw = float(bal.get('balance', 0))
                log.info(f"💰 CLOB balance: ${raw/1_000_000:.2f} pUSD")
        except Exception as e:
            log.debug(f"Balance sync: {e}")

        log.info(f"✅ CLOB V2 READY — wallet: {self._wallet_address[:8]}...{self._wallet_address[-4:]}")
        return client

    def init(self, private_key: str, funder: str = None, signature_type: int = 3):
        """Alias for init_py_clob_client — used by executor."""
        return self.init_py_clob_client(private_key, funder, signature_type)

    def get_available_balance(self) -> Optional[float]:
        """Get available balance — update allowance FIRST (prevents stale balance)."""
        if not self._py_clob_client:
            return None
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=Config.POLY_SIGNATURE_TYPE,
            )
            # Force allowance sync (fixes "not enough balance" cascade from wle.txt)
            self._py_clob_client.update_balance_allowance(params)
            bal = self._py_clob_client.get_balance_allowance(params)
            if bal:
                raw = float(bal.get('balance', 0)) / 1_000_000
                return raw
        except Exception as e:
            log.debug(f"Balance fetch: {e}")
        return None

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        """Check status of a single order by ID."""
        if not self._py_clob_client:
            return None
        try:
            return self._py_clob_client.get_order(order_id)
        except Exception:
            return None

    def get_balance(self) -> Optional[float]:
        """Get available pUSD balance from CLOB (6 decimal)."""
        if not self._py_clob_client:
            return None
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=Config.POLY_SIGNATURE_TYPE,
            )
            bal = self._py_clob_client.get_balance_allowance(params)
            if bal:
                return float(bal.get('balance', 0)) / 1_000_000
        except Exception as e:
            log.debug(f"Balance check: {e}")
        return None

    def get_open_orders(self) -> List[Dict]:
        """Get all open/pending orders."""
        if not self._py_clob_client:
            return []
        try:
            orders = self._py_clob_client.get_orders()
            return [o for o in (orders or []) if o.get('status', '').upper() in ('LIVE', 'OPEN')]
        except Exception as e:
            log.debug(f"Open orders: {e}")
            return []

    def place_limit_order(self, token_id: str, side: str, price: float,
                          size_pusd: float, expiration: str = "GTC",
                          neg_risk: bool = True) -> Optional[Dict]:
        """
        Place a V2 GTC/FOK order with neg_risk support.
        
        Weather markets = neg_risk=True (multi-outcome, one winner).
        Uses: create_order(args, options) → post_order(signed, type)
        """
        if not self._py_clob_client:
            log.error("CLOB not initialized")
            return None

        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY, SELL

            price_r = round(min(0.99, max(0.01, price)), 2)
            side_const = BUY if side.upper() == 'BUY' else SELL

            # Calculate shares. Both paths enforce ≥ $1 notional;
            # GTC additionally needs ≥ 5 shares (Polymarket minimum).
            shares = size_pusd / price_r
            min_dollar_shares = math.ceil(1.0 / price_r)  # ≥ $1 worth
            if expiration.upper() == 'GTC':
                shares = max(5, min_dollar_shares, math.floor(shares))
            else:
                shares = max(min_dollar_shares, math.floor(shares))

            log.info(f"📤 Order: {side} {shares:.0f}sh @ ${price_r:.2f} = ${shares*price_r:.2f} (neg_risk={neg_risk})")

            # V2 order creation (with neg_risk and builder_code)
            order_args = OrderArgs(
                token_id=token_id,
                price=price_r,
                size=shares,
                side=side_const,
                builder_code=self._builder_code,
            )

            options = PartialCreateOrderOptions(
                tick_size="0.01",
                neg_risk=neg_risk,
            )

            # Create signed order
            signed_order = self._py_clob_client.create_order(order_args, options)

            # Post order
            type_map = {'GTC': OrderType.GTC, 'FOK': OrderType.FOK, 'GTD': OrderType.GTC}
            order_type = type_map.get(expiration.upper(), OrderType.GTC)
            result = self._py_clob_client.post_order(signed_order, order_type)

            order_id = result.get('orderID', result.get('id', 'unknown'))
            status = result.get('status', 'UNKNOWN')
            log.info(f"✅ Order confirmed: ID={order_id[:16]} status={status}")
            return result

        except Exception as e:
            log.error(f"❌ Order failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if not self._py_clob_client:
            return False
        try:
            self._py_clob_client.cancel(order_id)
            log.info(f"🗑️ Cancelled order: {order_id[:12]}...")
            return True
        except Exception as e:
            log.error(f"Cancel failed: {e}")
            return False

    def cancel_all_orders(self) -> int:
        """Cancel all open orders."""
        if not self._py_clob_client:
            return 0
        try:
            result = self._py_clob_client.cancel_all()
            log.info(f"🗑️ Cancelled all orders")
            return 1
        except Exception as e:
            log.error(f"Cancel all failed: {e}")
            return 0

    def get_price(self, token_id: str) -> Optional[float]:
        """Get current price."""
        try:
            resp = self.session.get(f"{self.base_url}/price",
                                    params={'token_id': token_id, 'side': 'BUY'}, timeout=5)
            if resp.status_code == 200:
                return float(resp.json().get('price', 0))
        except Exception:
            pass
        return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price."""
        try:
            resp = self.session.get(f"{self.base_url}/midpoint",
                                    params={'token_id': token_id}, timeout=5)
            if resp.status_code == 200:
                return float(resp.json().get('mid', 0))
        except Exception:
            pass
        return None

    def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """Get orderbook."""
        try:
            resp = self.session.get(f"{self.base_url}/book",
                                    params={'token_id': token_id}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                bids = sorted([(float(b['price']), float(b['size'])) for b in data.get('bids', [])],
                              key=lambda x: x[0], reverse=True)
                asks = sorted([(float(a['price']), float(a['size'])) for a in data.get('asks', [])],
                              key=lambda x: x[0])
                return {
                    'bids': bids, 'asks': asks,
                    'best_bid': bids[0][0] if bids else 0.0,
                    'best_ask': asks[0][0] if asks else 1.0,
                    'spread': (asks[0][0] - bids[0][0]) if bids and asks else 0,
                }
        except Exception:
            pass
        return None

    def get_pusd_balance_onchain(self, wallet_address: str) -> Optional[float]:
        """Read pUSD balance from Polygon RPC (fallback)."""
        if not wallet_address:
            return None
        try:
            from web3 import Web3
            rpcs = ['https://polygon-bor-rpc.publicnode.com', 'https://rpc.ankr.com/polygon']
            if Config.POLYGON_RPC_URL:
                rpcs.insert(0, Config.POLYGON_RPC_URL)

            erc20_abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
                          "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
                          "type": "function"}]

            for rpc in rpcs:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 3}))
                    if not w3.is_connected():
                        continue
                    contract = w3.eth.contract(
                        address=Web3.to_checksum_address(Config.PUSD_CONTRACT), abi=erc20_abi)
                    raw = contract.functions.balanceOf(
                        Web3.to_checksum_address(wallet_address)).call()
                    return raw / 1e6
                except Exception:
                    continue
        except ImportError:
            pass
        return None
