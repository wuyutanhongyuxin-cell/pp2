#!/usr/bin/env python3
"""
Jess-Para Sniper Bot (V27 Python API Version)
基于原 Console 脚本改写，使用 Paradex API 进行后端下单
核心特性：使用 interactive token 获得 0 手续费
"""

import os
import sys
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from decimal import Decimal, ROUND_DOWN

from dotenv import load_dotenv

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('JESS-SNIPER')

# =============================================================================
# 配置类
# =============================================================================

@dataclass
class TradingConfig:
    """交易配置 - 对应原脚本的 CFG 对象"""

    # 开仓限制：点差必须很小才开 (≤ 0.004%)
    spread_threshold_percent: float = 0.004

    # 订单簿厚度限制：买一卖一 Size >= 600 USD
    min_order_book_size_usd: float = 600

    # 平仓参数
    close_spread_target: float = 0.005    # 目标平仓点差 (≤ 0.005% 秒平)
    close_timeout_ms: int = 3000          # 超过 3 秒强制平

    # 周期参数
    cycle_every_ms: int = 10000
    wait_spread_ms: int = 9000
    wait_confirm_ms: int = 12000
    wait_market_ms: int = 15000
    wait_modal_ms: int = 15000
    wait_after_close_ms: int = 6000

    # 仓位参数
    open_size_percent: int = 90           # 按余额百分比开仓 (当 fixed_size 为空时使用)
    close_size_percent: int = 100
    price_offset: float = 0
    fixed_size: str = ""                  # 固定交易大小, 如 "0.0009" (优先级高于 open_size_percent)

    # 限速
    limits_per_second: int = 3
    limits_per_minute: int = 30
    limits_per_hour: int = 300
    limits_per_day: int = 1000

    # 市场
    market: str = "BTC-USD-PERP"

    # 运行状态
    enabled: bool = False


@dataclass
class Stats:
    """统计数据"""
    is_running: bool = False
    runs: int = 0
    total_wear: float = 0
    total_volume: float = 0
    last_wear: float = 0
    last_delta: float = 0
    start_balance: Optional[float] = None
    end_balance: Optional[float] = None
    last_start_at: Optional[int] = None
    last_stop_at: Optional[int] = None
    last_stop_reason: Optional[str] = None


@dataclass
class RateLimitState:
    """限速状态"""
    day: str = ""
    trades: List[int] = field(default_factory=list)


@dataclass
class AccountInfo:
    """账号信息"""
    l2_private_key: str
    l2_address: str
    name: str = ""  # 账号名称/标识


class AccountManager:
    """
    多账号管理器
    当一个账号达到日限制时自动切换到下一个账号
    """

    def __init__(self, accounts: List[AccountInfo], environment: str = "prod"):
        if not accounts:
            raise ValueError("至少需要配置一个账号")

        self.accounts = accounts
        self.environment = environment
        self.current_index = 0
        self.clients: Dict[int, 'ParadexInteractiveClient'] = {}
        self.rate_states: Dict[int, RateLimitState] = {}
        self.daily_limits = 1000  # 每个账号每天最大交易次数

        # 初始化每个账号的限速状态
        for i in range(len(accounts)):
            self.rate_states[i] = RateLimitState()

        log.info(f"账号管理器初始化: 共 {len(accounts)} 个账号")

    def get_current_client(self) -> Optional['ParadexInteractiveClient']:
        """获取当前活跃的客户端"""
        if self.current_index >= len(self.accounts):
            return None

        # 懒加载客户端
        if self.current_index not in self.clients:
            account = self.accounts[self.current_index]
            try:
                client = ParadexInteractiveClient(
                    l2_private_key=account.l2_private_key,
                    l2_address=account.l2_address,
                    environment=self.environment
                )
                self.clients[self.current_index] = client
                log.info(f"已加载账号 #{self.current_index + 1}: {account.name or account.l2_address[:10]}...")
            except Exception as e:
                log.error(f"加载账号 #{self.current_index + 1} 失败: {e}")
                return None

        return self.clients[self.current_index]

    def get_current_rate_state(self) -> RateLimitState:
        """获取当前账号的限速状态"""
        return self.rate_states[self.current_index]

    def get_current_account_name(self) -> str:
        """获取当前账号名称"""
        if self.current_index >= len(self.accounts):
            return "无可用账号"
        account = self.accounts[self.current_index]
        return account.name or f"账号#{self.current_index + 1}"

    def is_current_account_limited(self) -> bool:
        """检查当前账号是否达到日限制"""
        state = self.get_current_rate_state()
        today = datetime.now().strftime("%Y-%m-%d")

        # 如果是新的一天，重置计数
        if state.day != today:
            state.day = today
            state.trades = []
            return False

        return len(state.trades) >= self.daily_limits

    def switch_to_next_account(self) -> bool:
        """
        切换到下一个可用账号 (未达到 day 限制)
        返回: True 如果成功切换, False 如果所有账号都已达到限制
        """
        original_index = self.current_index
        today = datetime.now().strftime("%Y-%m-%d")

        # 尝试找到下一个未达到日限制的账号
        for _ in range(len(self.accounts)):
            self.current_index = (self.current_index + 1) % len(self.accounts)

            # 检查是否回到了起始账号
            if self.current_index == original_index:
                # 检查所有账号是否都达到限制
                all_limited = all(
                    self.rate_states[i].day == today and
                    len(self.rate_states[i].trades) >= self.daily_limits
                    for i in range(len(self.accounts))
                )
                if all_limited:
                    log.warning("所有账号都已达到今日交易限制!")
                    return False

            # 检查新账号是否可用
            if not self.is_current_account_limited():
                log.info(f"切换到 {self.get_current_account_name()}")
                return True

        return False

    def _count_hour_trades(self, account_index: int) -> int:
        """统计某账号过去1小时的交易数"""
        state = self.rate_states[account_index]
        cutoff = int(time.time() * 1000) - 3600000  # 1小时前
        return len([t for t in state.trades if t > cutoff])

    def is_account_hour_limited(self, account_index: int) -> bool:
        """检查某账号是否达到小时限制 (300单)"""
        hour_trades = self._count_hour_trades(account_index)
        return hour_trades >= 300  # limits_per_hour

    def switch_to_next_available_account(self) -> str:
        """
        切换到下一个小时未满的账号
        返回:
          - "switched": 成功切换到 hour 未满的账号
          - "all_hour_limited": 所有账号 hour 都满了，但有 day 未满的，需要等待
          - "all_day_limited": 所有账号 day 都满了
        """
        today = datetime.now().strftime("%Y-%m-%d")

        # 尝试找到下一个 hour 未满的账号
        for _ in range(len(self.accounts)):
            self.current_index = (self.current_index + 1) % len(self.accounts)

            # 跳过 day 已满的账号
            state = self.rate_states[self.current_index]
            if state.day == today and len(state.trades) >= self.daily_limits:
                continue

            # 检查 hour 是否未满
            if not self.is_account_hour_limited(self.current_index):
                log.info(f"切换到 {self.get_current_account_name()} (hour: {self._count_hour_trades(self.current_index)}/300)")
                return "switched"

        # 所有账号的 hour 都满了，检查是否有 day 未满的
        has_day_available = False
        for i in range(len(self.accounts)):
            state = self.rate_states[i]
            day_trades = len(state.trades) if state.day == today else 0
            if day_trades < self.daily_limits:
                has_day_available = True
                break

        if has_day_available:
            # 所有账号 hour 都满了，需要等待
            log.info("所有账号小时限制已满，等待重置...")
            return "all_hour_limited"

        # 所有账号 day 都满了
        log.warning("所有账号今日交易额度已用完!")
        return "all_day_limited"

    def record_trade(self):
        """记录一次交易"""
        state = self.get_current_rate_state()
        state.trades.append(int(time.time() * 1000))

    def get_all_stats(self) -> Dict:
        """获取所有账号的统计信息"""
        today = datetime.now().strftime("%Y-%m-%d")
        stats = {
            "current_account": self.get_current_account_name(),
            "current_index": self.current_index + 1,
            "total_accounts": len(self.accounts),
            "accounts": []
        }

        for i, account in enumerate(self.accounts):
            state = self.rate_states[i]
            trades_today = len(state.trades) if state.day == today else 0
            stats["accounts"].append({
                "name": account.name or f"账号#{i + 1}",
                "address": account.l2_address[:10] + "...",
                "trades_today": trades_today,
                "remaining": max(0, self.daily_limits - trades_today),
                "is_limited": trades_today >= self.daily_limits
            })

        return stats

    def all_accounts_exhausted(self) -> bool:
        """检查是否所有账号都已用完今日额度"""
        today = datetime.now().strftime("%Y-%m-%d")
        return all(
            self.rate_states[i].day == today and
            len(self.rate_states[i].trades) >= self.daily_limits
            for i in range(len(self.accounts))
        )

    def save_state(self, filepath: str = "account_states.json"):
        """保存所有账号的状态"""
        data = {
            "current_index": self.current_index,
            "rate_states": {
                str(i): {"day": state.day, "trades": state.trades[-1000:]}
                for i, state in self.rate_states.items()
            }
        }
        try:
            with open(filepath, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            log.error(f"保存账号状态失败: {e}")

    def load_state(self, filepath: str = "account_states.json"):
        """加载账号状态"""
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    self.current_index = data.get("current_index", 0)
                    for i_str, state_data in data.get("rate_states", {}).items():
                        i = int(i_str)
                        if i < len(self.accounts):
                            self.rate_states[i] = RateLimitState(
                                day=state_data.get("day", ""),
                                trades=state_data.get("trades", [])
                            )
                log.info(f"已加载账号状态，当前账号: {self.get_current_account_name()}")
        except Exception as e:
            log.warning(f"加载账号状态失败: {e}")


# =============================================================================
# Paradex API 客户端 (带 Interactive Token)
# =============================================================================

class ParadexInteractiveClient:
    """
    Paradex API 客户端
    关键：认证时使用 ?token_usage=interactive 获取 0 手续费 token
    """

    def __init__(self, l2_private_key: str, l2_address: str, environment: str = "prod"):
        self.l2_private_key = l2_private_key
        self.l2_address = l2_address
        self.environment = environment

        self.base_url = f"https://api.{'prod' if environment == 'prod' else 'testnet'}.paradex.trade/v1"
        self.jwt_token: Optional[str] = None
        self.jwt_expires_at: int = 0

        # 市场信息缓存
        self.market_info: Dict[str, Any] = {}

        # 导入 paradex-py
        try:
            from paradex_py import ParadexSubkey
            from paradex_py.environment import PROD, TESTNET

            env = PROD if environment == "prod" else TESTNET
            self.paradex = ParadexSubkey(
                env=env,
                l2_private_key=l2_private_key,
                l2_address=l2_address,
            )
            log.info(f"Paradex SDK 初始化成功 (环境: {environment})")
        except ImportError:
            log.error("请先安装 paradex-py: pip install paradex-py")
            raise

    async def authenticate_interactive(self) -> bool:
        """
        使用 interactive 模式认证
        关键：POST /v1/auth?token_usage=interactive
        """
        try:
            import aiohttp

            # 生成认证签名
            timestamp = int(time.time())
            expiry = timestamp + 24 * 60 * 60  # 24小时有效

            # 使用 paradex SDK 生成签名
            auth_headers = self.paradex.account.auth_headers()

            # 发送认证请求，关键是 URL 参数 token_usage=interactive
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/auth?token_usage=interactive"

                headers = {
                    "Content-Type": "application/json",
                    **auth_headers
                }

                async with session.post(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.jwt_token = data.get("jwt_token")

                        # 解析 token 获取过期时间
                        import base64
                        payload = self.jwt_token.split('.')[1]
                        # 添加 padding
                        payload += '=' * (4 - len(payload) % 4)
                        decoded = json.loads(base64.b64decode(payload))

                        self.jwt_expires_at = decoded.get("exp", 0)
                        token_usage = decoded.get("token_usage", "unknown")

                        log.info(f"认证成功! token_usage={token_usage} (应该是 interactive)")

                        if token_usage != "interactive":
                            log.warning("警告: token_usage 不是 interactive，手续费可能不是 0!")

                        return True
                    else:
                        error = await resp.text()
                        log.error(f"认证失败: {resp.status} - {error}")
                        return False

        except Exception as e:
            log.error(f"认证异常: {e}")
            return False

    async def ensure_authenticated(self) -> bool:
        """确保已认证且 token 未过期"""
        now = int(time.time())

        # token 还有至少 60 秒有效期
        if self.jwt_token and self.jwt_expires_at > now + 60:
            return True

        log.info("Token 已过期或不存在，重新认证...")
        return await self.authenticate_interactive()

    def _get_auth_headers(self) -> Dict[str, str]:
        """获取带认证的请求头"""
        return {
            "Authorization": f"Bearer {self.jwt_token}",
            "Content-Type": "application/json"
        }

    async def get_balance(self) -> Optional[float]:
        """获取 USDC 余额"""
        try:
            if not await self.ensure_authenticated():
                return None

            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/balance"
                async with session.get(url, headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("results", []):
                            if item.get("token") == "USDC":
                                return float(item.get("size", 0))
            return 0
        except Exception as e:
            log.error(f"获取余额失败: {e}")
            return None

    async def get_positions(self, market: str = None) -> List[Dict]:
        """获取持仓"""
        try:
            if not await self.ensure_authenticated():
                return []

            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/positions"
                async with session.get(url, headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        positions = data.get("results", [])

                        if market:
                            positions = [p for p in positions if p.get("market") == market]

                        # 过滤掉已关闭的仓位
                        return [p for p in positions if p.get("status") != "CLOSED" and float(p.get("size", 0)) > 0]
            return []
        except Exception as e:
            log.error(f"获取持仓失败: {e}")
            return []

    async def get_market_info(self, market: str) -> Optional[Dict]:
        """获取市场信息（tick size, min notional 等）"""
        if market in self.market_info:
            return self.market_info[market]

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/markets"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for m in data.get("results", []):
                            self.market_info[m.get("symbol")] = m
                        return self.market_info.get(market)
            return None
        except Exception as e:
            log.error(f"获取市场信息失败: {e}")
            return None

    async def get_bbo(self, market: str) -> Optional[Dict]:
        """
        获取最优买卖价 (Best Bid/Offer)
        返回: {"bid": price, "ask": price, "bid_size": size, "ask_size": size}
        """
        try:
            if not await self.ensure_authenticated():
                return None

            import aiohttp
            async with aiohttp.ClientSession() as session:
                # 使用 orderbook API
                url = f"{self.base_url}/orderbook/{market}?depth=1"

                try:
                    async with session.get(url, headers=self._get_auth_headers()) as resp:
                        if resp.status == 200:
                            data = await resp.json()

                            # 解析 bids 和 asks 数组: [[price, size], ...]
                            bids = data.get("bids", [])
                            asks = data.get("asks", [])

                            # 优先使用 best_bid_api/best_ask_api (格式: [price, size])
                            best_bid = data.get("best_bid_api") or (bids[0] if bids else None)
                            best_ask = data.get("best_ask_api") or (asks[0] if asks else None)

                            if best_bid and best_ask:
                                return {
                                    "bid": float(best_bid[0]),
                                    "ask": float(best_ask[0]),
                                    "bid_size": float(best_bid[1]),
                                    "ask_size": float(best_ask[1]),
                                }
                except Exception as e:
                    log.debug(f"orderbook API 调用失败: {e}")

            return None
        except Exception as e:
            log.error(f"获取 BBO 失败: {e}")
            return None

    async def get_spread_percent(self, market: str) -> Optional[float]:
        """计算点差百分比"""
        bbo = await self.get_bbo(market)
        if not bbo or not bbo["bid"] or not bbo["ask"]:
            return None

        mid = (bbo["bid"] + bbo["ask"]) / 2
        spread = bbo["ask"] - bbo["bid"]
        return (spread / mid) * 100 if mid > 0 else None

    async def place_limit_order(
        self,
        market: str,
        side: str,  # "BUY" or "SELL"
        size: str,
        price: str,
        instruction: str = "GTC",
        reduce_only: bool = False
    ) -> Optional[Dict]:
        """
        下限价单
        使用 paradex SDK 进行签名，但通过 HTTP 发送以使用 interactive token
        """
        try:
            if not await self.ensure_authenticated():
                return None

            # 使用 SDK 创建并签名订单
            from paradex_py.common.order import Order, OrderSide, OrderType
            from decimal import Decimal

            order_side = OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell

            order = Order(
                market=market,
                order_type=OrderType.Limit,
                order_side=order_side,
                size=Decimal(size),
                limit_price=Decimal(price),
                client_id=f"sniper_{int(time.time()*1000)}",
                instruction=instruction,
                reduce_only=reduce_only,
                signature_timestamp=int(time.time() * 1000),
            )

            # 使用 SDK 签名订单并将签名赋值给订单
            order.signature = self.paradex.account.sign_order(order)

            # 通过 HTTP 发送，使用我们的 interactive JWT token
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/orders"
                payload = order.dump_to_dict()

                async with session.post(url, headers=self._get_auth_headers(), json=payload) as resp:
                    if resp.status == 201:
                        result = await resp.json()
                        log.info(f"下单成功: {side} {size} @ {price}, order_id={result.get('id')}")

                        # 检查是否为 interactive 模式
                        flags = result.get("flags", [])
                        if "INTERACTIVE" in flags:
                            log.info("确认: 订单使用 INTERACTIVE 模式 (0 手续费)")
                        else:
                            log.warning(f"警告: 订单 flags={flags}, 可能不是 interactive 模式")

                        return result
                    else:
                        error = await resp.text()
                        log.error(f"下单失败: {resp.status} - {error}")
                        return None

        except Exception as e:
            log.error(f"下单失败: {e}")
            return None

    async def place_market_order(
        self,
        market: str,
        side: str,
        size: str,
        reduce_only: bool = False
    ) -> Optional[Dict]:
        """下市价单（用于平仓）"""
        try:
            if not await self.ensure_authenticated():
                return None

            from paradex_py.common.order import Order, OrderSide, OrderType
            from decimal import Decimal

            order_side = OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell

            order = Order(
                market=market,
                order_type=OrderType.Market,
                order_side=order_side,
                size=Decimal(size),
                client_id=f"sniper_mkt_{int(time.time()*1000)}",
                reduce_only=reduce_only,
                signature_timestamp=int(time.time() * 1000),
            )

            # 使用 SDK 签名订单并将签名赋值给订单
            order.signature = self.paradex.account.sign_order(order)

            # 通过 HTTP 发送，使用我们的 interactive JWT token
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/orders"
                payload = order.dump_to_dict()

                async with session.post(url, headers=self._get_auth_headers(), json=payload) as resp:
                    if resp.status == 201:
                        result = await resp.json()
                        log.info(f"市价单成功: {side} {size}, order_id={result.get('id')}")
                        return result
                    else:
                        error = await resp.text()
                        log.error(f"市价单失败: {resp.status} - {error}")
                        return None

        except Exception as e:
            log.error(f"市价单失败: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        try:
            if not await self.ensure_authenticated():
                return False

            self.paradex.api_client.cancel_order(order_id)
            log.info(f"订单已取消: {order_id}")
            return True

        except Exception as e:
            log.error(f"取消订单失败: {e}")
            return False

    async def cancel_all_orders(self, market: str = None) -> int:
        """取消所有挂单"""
        try:
            if not await self.ensure_authenticated():
                return 0

            import aiohttp
            async with aiohttp.ClientSession() as session:
                # 获取所有挂单
                url = f"{self.base_url}/orders"
                params = {"status": "OPEN"}
                if market:
                    params["market"] = market

                async with session.get(url, headers=self._get_auth_headers(), params=params) as resp:
                    if resp.status != 200:
                        return 0
                    data = await resp.json()
                    orders = data.get("results", [])

                if not orders:
                    log.info("没有挂单需要取消")
                    return 0

                # 取消所有订单
                cancelled = 0
                for order in orders:
                    order_id = order.get("id")
                    if order_id:
                        cancel_url = f"{self.base_url}/orders/{order_id}"
                        async with session.delete(cancel_url, headers=self._get_auth_headers()) as cancel_resp:
                            if cancel_resp.status in [200, 204]:
                                cancelled += 1

                log.info(f"已取消 {cancelled}/{len(orders)} 个挂单")
                return cancelled

        except Exception as e:
            log.error(f"取消所有订单失败: {e}")
            return 0

    async def close_all_positions(self, market: str = None) -> int:
        """平掉所有仓位"""
        try:
            if not await self.ensure_authenticated():
                return 0

            positions = await self.get_positions()
            if not positions:
                log.info("没有仓位需要平仓")
                return 0

            closed = 0
            for pos in positions:
                pos_market = pos.get("market")
                if market and pos_market != market:
                    continue

                size = pos.get("size", "0")
                side = pos.get("side", "")

                if float(size) == 0:
                    continue

                # 平仓方向与持仓相反
                close_side = "SELL" if side == "LONG" else "BUY"

                result = await self.place_market_order(
                    market=pos_market,
                    side=close_side,
                    size=size,
                    reduce_only=True
                )

                if result:
                    closed += 1
                    log.info(f"已平仓 {pos_market}: {close_side} {size}")

            log.info(f"已平仓 {closed} 个仓位")
            return closed

        except Exception as e:
            log.error(f"平仓所有仓位失败: {e}")
            return 0


# =============================================================================
# 交易机器人主逻辑
# =============================================================================

class SniperBot:
    """狙击机器人 - 对应原脚本的主循环逻辑"""

    def __init__(
        self,
        client: ParadexInteractiveClient,
        config: TradingConfig,
        account_manager: Optional[AccountManager] = None
    ):
        self.client = client
        self.config = config
        self.stats = Stats()
        self.rate_state = RateLimitState()
        self.account_manager = account_manager

        # 加载持久化数据
        self._load_state()

        # 注意: account_manager 的状态应该在 main() 中提前加载
        # 这样确保 client 与 current_index 同步
        # 这里只同步 rate_state
        if self.account_manager:
            self.rate_state = self.account_manager.get_current_rate_state()

    def _load_state(self):
        """加载持久化状态"""
        try:
            if os.path.exists("sniper_state.json"):
                with open("sniper_state.json", "r") as f:
                    data = json.load(f)
                    self.stats = Stats(**data.get("stats", {}))
                    self.rate_state = RateLimitState(**data.get("rate_state", {}))
        except Exception as e:
            log.warning(f"加载状态失败: {e}")

    def _save_state(self):
        """保存持久化状态"""
        try:
            data = {
                "stats": {
                    "is_running": self.stats.is_running,
                    "runs": self.stats.runs,
                    "total_wear": self.stats.total_wear,
                    "total_volume": self.stats.total_volume,
                },
                "rate_state": {
                    "day": self.rate_state.day,
                    "trades": self.rate_state.trades[-1000:],  # 只保留最近1000条
                }
            }
            with open("sniper_state.json", "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning(f"保存状态失败: {e}")

    def _day_key(self) -> str:
        """获取当天日期键"""
        return datetime.now().strftime("%Y-%m-%d")

    def _prune_trades(self):
        """清理过期的交易记录"""
        cutoff = int(time.time() * 1000) - 86400000  # 24小时前
        self.rate_state.trades = [t for t in self.rate_state.trades if t > cutoff]

    def _count_trades_in_window(self, window_ms: int) -> int:
        """统计时间窗口内的交易数"""
        cutoff = int(time.time() * 1000) - window_ms
        return len([t for t in self.rate_state.trades if t > cutoff])

    def _can_trade(self) -> tuple[bool, Optional[str], Dict]:
        """检查是否可以交易（限速检查）"""
        # 如果使用多账号管理器
        if self.account_manager:
            return self._can_trade_multi_account()

        # 单账号模式（原逻辑）
        # 检查日期是否变化
        if self.rate_state.day != self._day_key():
            self.rate_state.day = self._day_key()
            self.rate_state.trades = []

        self._prune_trades()

        usage = {
            "sec": self._count_trades_in_window(1000),
            "min": self._count_trades_in_window(60000),
            "hour": self._count_trades_in_window(3600000),
            "day": len(self.rate_state.trades),
        }

        if usage["day"] >= self.config.limits_per_day:
            return False, "day", usage
        if usage["hour"] >= self.config.limits_per_hour:
            return False, "hour", usage
        if usage["min"] >= self.config.limits_per_minute:
            return False, "min", usage
        if usage["sec"] >= self.config.limits_per_second:
            return False, "sec", usage

        return True, None, usage

    def _can_trade_multi_account(self) -> tuple[bool, Optional[str], Dict]:
        """多账号模式的限速检查"""
        # 使用当前账号的限速状态
        self.rate_state = self.account_manager.get_current_rate_state()

        # 检查日期是否变化
        if self.rate_state.day != self._day_key():
            self.rate_state.day = self._day_key()
            self.rate_state.trades = []

        self._prune_trades()

        usage = {
            "sec": self._count_trades_in_window(1000),
            "min": self._count_trades_in_window(60000),
            "hour": self._count_trades_in_window(3600000),
            "day": len(self.rate_state.trades),
            "account": self.account_manager.get_current_account_name(),
        }

        # 检查是否达到日限制 (1000单)，如果所有账号都满了就停止
        if usage["day"] >= self.config.limits_per_day:
            log.info(f"{usage['account']} 达到日限制 ({usage['day']}/{self.config.limits_per_day})")

            # 检查是否所有账号的 day 限制都满了
            if self.account_manager.all_accounts_exhausted():
                return False, "all_accounts_exhausted", usage

            # 标记需要切换账号 (hour 限制触发切换，day 限制不切换只是等待)
            return False, "day_limit_wait", usage

        # 检查是否达到小时限制 (300单)，触发账号轮换
        if usage["hour"] >= self.config.limits_per_hour:
            log.info(f"{usage['account']} 达到小时限制 ({usage['hour']}/{self.config.limits_per_hour})，需要切换账号...")
            return False, "hour_switch", usage
        if usage["min"] >= self.config.limits_per_minute:
            return False, "min", usage
        if usage["sec"] >= self.config.limits_per_second:
            return False, "sec", usage

        return True, None, usage

    def _record_trade(self):
        """记录一次交易"""
        self.rate_state.trades.append(int(time.time() * 1000))

        # 如果使用多账号管理器，也记录到管理器中并保存
        if self.account_manager:
            self.account_manager.save_state()
        else:
            self._save_state()

    async def _open_position(self) -> tuple[bool, str]:
        """
        开仓逻辑
        使用 Last Price 下限价单
        """
        try:
            market = self.config.market

            # 获取市场信息
            market_info = await self.client.get_market_info(market)
            if not market_info:
                return False, "无法获取市场信息"

            tick_size = Decimal(market_info.get("price_tick_size", "0.1"))
            size_increment = Decimal(market_info.get("order_size_increment", "0.0001"))
            min_notional = float(market_info.get("min_notional", 10))

            # 获取 BBO
            bbo = await self.client.get_bbo(market)
            if not bbo:
                return False, "无法获取 BBO"

            # 获取余额
            balance = await self.client.get_balance()
            if not balance or balance < min_notional:
                return False, f"余额不足: {balance}"

            mid_price = (bbo["bid"] + bbo["ask"]) / 2

            # 计算开仓大小
            if self.config.fixed_size:
                # 使用固定大小
                size = Decimal(self.config.fixed_size)
            else:
                # 按余额百分比计算
                trade_value = balance * (self.config.open_size_percent / 100)
                size = Decimal(str(trade_value / mid_price))

            # 对齐到 size_increment
            size = (size / size_increment).quantize(Decimal('1'), rounding=ROUND_DOWN) * size_increment

            if float(size) * mid_price < min_notional:
                return False, f"订单金额低于最小值 {min_notional}"

            # 使用 mid price 作为限价（对齐到 tick_size）
            price = Decimal(str(mid_price))
            price = (price / tick_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * tick_size

            # 下单
            result = await self.client.place_limit_order(
                market=market,
                side="BUY",
                size=str(size),
                price=str(price),
                instruction="GTC"
            )

            if result:
                return True, f"开仓成功: {size} @ {price}"
            else:
                return False, "下单失败"

        except Exception as e:
            return False, f"开仓异常: {e}"

    async def _close_position(self) -> tuple[bool, str]:
        """
        平仓逻辑
        智能择时：点差 <= 目标点差时平仓，或超时强制平仓
        """
        try:
            market = self.config.market
            start_time = time.time() * 1000

            while True:
                # 检查是否超时
                elapsed = time.time() * 1000 - start_time

                # 获取当前点差
                spread = await self.client.get_spread_percent(market)

                # 满足平仓条件：点差足够小 或 超时
                can_close = (
                    (spread is not None and spread <= self.config.close_spread_target) or
                    (elapsed > self.config.close_timeout_ms)
                )

                if not can_close:
                    await asyncio.sleep(0.2)
                    continue

                # 获取当前持仓
                positions = await self.client.get_positions(market)
                if not positions:
                    return True, "无持仓需要平仓"

                pos = positions[0]
                size = pos.get("size", "0")
                side = pos.get("side", "LONG")

                if float(size) <= 0:
                    return True, "持仓已关闭"

                # 平仓方向与持仓相反
                close_side = "SELL" if side == "LONG" else "BUY"

                # 市价平仓
                result = await self.client.place_market_order(
                    market=market,
                    side=close_side,
                    size=size,
                    reduce_only=True
                )

                if result:
                    reason = "点差满足" if spread and spread <= self.config.close_spread_target else "超时强平"
                    return True, f"平仓成功 ({reason}): {size}"
                else:
                    return False, "平仓下单失败"

        except Exception as e:
            return False, f"平仓异常: {e}"

    async def run_cycle(self) -> tuple[bool, str]:
        """运行一个交易周期"""
        market = self.config.market

        # 1. 检查限速
        can_trade, reason, usage = self._can_trade()
        if not can_trade:
            return False, f"限速中: {reason} ({usage})"

        # 2. 获取订单簿 (同时用于点差和厚度检查)
        bbo = await self.client.get_bbo(market)
        if not bbo:
            return False, "无法获取订单簿"

        # 计算点差
        mid = (bbo["bid"] + bbo["ask"]) / 2
        spread = ((bbo["ask"] - bbo["bid"]) / mid) * 100 if mid > 0 else None
        if spread is None:
            return False, "无法计算点差"

        # 使用 <= 判断，并添加小数精度容差
        if spread > self.config.spread_threshold_percent + 0.00001:
            return False, f"点差过大: {spread:.4f}% > {self.config.spread_threshold_percent}%"

        # 3. 检查订单簿厚度
        bid_usd = bbo["bid_size"] * bbo["bid"]
        ask_usd = bbo["ask_size"] * bbo["ask"]

        if bid_usd < self.config.min_order_book_size_usd or ask_usd < self.config.min_order_book_size_usd:
            return False, f"订单簿不足: 买一=${bid_usd:.2f} 卖一=${ask_usd:.2f} (size: {bbo['bid_size']:.6f}/{bbo['ask_size']:.6f})"

        log.info(f"条件满足! 点差={spread:.4f}%, 开始开仓...")

        # 4. 开仓
        success, msg = await self._open_position()
        if not success:
            return False, f"开仓失败: {msg}"

        self._record_trade()
        log.info(msg)

        await asyncio.sleep(0.5)

        # 5. 平仓
        log.info("准备平仓...")
        success, msg = await self._close_position()
        if success:
            self._record_trade()

        log.info(msg)

        # 更新统计
        self.stats.runs += 1
        balance = await self.client.get_balance()
        if balance:
            self.stats.total_volume += balance * 0.9
        self._save_state()

        return True, "周期完成"

    async def run(self):
        """主运行循环"""
        log.info("=" * 50)
        log.info("Jess-Para Sniper Bot (V27 Python API - 多账号版)")
        log.info(f"市场: {self.config.market}")
        log.info(f"点差阈值: {self.config.spread_threshold_percent}%")
        log.info(f"订单簿最小厚度: ${self.config.min_order_book_size_usd}")

        # 显示多账号信息
        if self.account_manager:
            stats = self.account_manager.get_all_stats()
            log.info(f"多账号模式: 共 {stats['total_accounts']} 个账号")
            for acc in stats["accounts"]:
                status = "🔴 已满" if acc["is_limited"] else "🟢 可用"
                log.info(f"  {acc['name']}: 今日 {acc['trades_today']}/{self.config.limits_per_day} {status}")
            log.info(f"当前账号: {stats['current_account']}")
        else:
            log.info("单账号模式")

        log.info("=" * 50)

        # 初始认证
        if not await self.client.authenticate_interactive():
            log.error("初始认证失败!")
            return

        log.info("认证成功，开始监控...")
        self.config.enabled = True
        cycle_count = 0
        last_status_time = time.time()
        last_stats_time = time.time()

        while True:
            try:
                if not self.config.enabled:
                    log.info("机器人已暂停")
                    await asyncio.sleep(1)
                    continue

                success, msg = await self.run_cycle()
                cycle_count += 1

                # 检查是否所有账号都用完
                if "all_accounts_exhausted" in msg:
                    log.warning("=" * 50)
                    log.warning("所有账号今日交易额度已用完!")
                    log.warning("明天将自动重置，或手动添加新账号")
                    log.warning("=" * 50)
                    # 等待到明天凌晨
                    await self._wait_until_tomorrow()
                    continue

                # 检查是否需要因 hour 限制切换账号
                if "hour_switch" in msg and self.account_manager:
                    switch_result = await self._switch_account_with_cleanup()

                    if switch_result == "wait_hour":
                        # 所有账号 hour 都满了，等待一段时间
                        log.info("所有账号小时限制已满，等待 10 分钟后重试...")
                        await asyncio.sleep(600)  # 等待 10 分钟

                    elif switch_result == "wait_day":
                        # 所有账号 day 都满了
                        await self._wait_until_tomorrow()

                    continue

                # 检查是否达到 day 限制但还有其他账号可用
                if "day_limit_wait" in msg and self.account_manager:
                    # 当前账号 day 满了，尝试切换到 hour 未满的账号
                    switch_result = await self._switch_account_with_cleanup()

                    if switch_result == "wait_hour":
                        log.info("所有账号小时限制已满，等待 10 分钟后重试...")
                        await asyncio.sleep(600)

                    elif switch_result == "wait_day":
                        await self._wait_until_tomorrow()

                    continue

                if success:
                    log.info(f"交易完成: {msg}")
                    await asyncio.sleep(self.config.cycle_every_ms / 1000)
                else:
                    # 每 10 秒输出一次状态日志
                    if time.time() - last_status_time >= 10:
                        account_info = ""
                        if self.account_manager:
                            account_info = f"[{self.account_manager.get_current_account_name()}] "
                        log.info(f"[监控中] {account_info}周期#{cycle_count} | {msg}")
                        last_status_time = time.time()

                    # 每 5 分钟输出一次多账号统计
                    if self.account_manager and time.time() - last_stats_time >= 300:
                        self._log_account_stats()
                        last_stats_time = time.time()

                    await asyncio.sleep(0.2)

            except KeyboardInterrupt:
                log.info("收到中断信号 (Ctrl+C)，正在执行退出清理...")
                await self._cleanup_on_exit()
                break
            except Exception as e:
                log.error(f"循环异常: {e}")
                await asyncio.sleep(1)

        log.info("机器人已停止")
        if self.account_manager:
            self.account_manager.save_state()
        else:
            self._save_state()

    def _log_account_stats(self):
        """输出账号统计信息"""
        if not self.account_manager:
            return
        stats = self.account_manager.get_all_stats()
        log.info("--- 账号统计 ---")
        total_trades = 0
        for acc in stats["accounts"]:
            total_trades += acc["trades_today"]
            status = "满" if acc["is_limited"] else "可用"
            log.info(f"  {acc['name']}: {acc['trades_today']}/{self.config.limits_per_day} [{status}]")
        log.info(f"  总计: {total_trades} 笔交易")

    async def _cleanup_on_exit(self):
        """
        退出时清理: 取消所有挂单并平掉所有仓位
        """
        log.info("=" * 50)
        log.info("开始退出清理流程...")

        if self.account_manager:
            # 多账号模式: 清理所有已初始化的账号
            for idx, client in self.account_manager.clients.items():
                account_name = self.account_manager.accounts[idx].name or f"账号#{idx + 1}"
                log.info(f"[{account_name}] 检查并清理...")

                try:
                    # 确保认证有效
                    if not await client.ensure_authenticated():
                        log.warning(f"[{account_name}] 认证失败，跳过清理")
                        continue

                    # 取消所有挂单
                    log.info(f"[{account_name}] 取消所有挂单...")
                    cancelled = await client.cancel_all_orders(self.config.market)

                    # 平掉所有仓位
                    log.info(f"[{account_name}] 平掉所有仓位...")
                    closed = await client.close_all_positions(self.config.market)

                    log.info(f"[{account_name}] 清理完成: 取消 {cancelled} 个挂单, 平仓 {closed} 个仓位")

                except Exception as e:
                    log.error(f"[{account_name}] 清理异常: {e}")
        else:
            # 单账号模式
            try:
                log.info("取消所有挂单...")
                cancelled = await self.client.cancel_all_orders(self.config.market)

                log.info("平掉所有仓位...")
                closed = await self.client.close_all_positions(self.config.market)

                log.info(f"清理完成: 取消 {cancelled} 个挂单, 平仓 {closed} 个仓位")

            except Exception as e:
                log.error(f"清理异常: {e}")

        log.info("退出清理完成")
        log.info("=" * 50)

    async def _wait_until_tomorrow(self):
        """等待到明天凌晨"""
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (tomorrow - now).total_seconds()
        log.info(f"等待 {wait_seconds/3600:.1f} 小时后重新开始...")
        await asyncio.sleep(wait_seconds + 60)  # 多等 1 分钟确保日期变化

    async def _switch_account_with_cleanup(self) -> str:
        """
        切换账号前执行清仓操作
        返回:
          - "switched": 成功切换
          - "wait_hour": 需要等待小时重置
          - "wait_day": 需要等待到明天
        """
        if not self.account_manager:
            return "switched"

        current_account = self.account_manager.get_current_account_name()
        log.info("=" * 50)
        log.info(f"[{current_account}] 开始切换账号流程...")

        # 1. 取消所有挂单
        log.info(f"[{current_account}] 取消所有挂单...")
        await self.client.cancel_all_orders(self.config.market)

        # 2. 平掉所有仓位
        log.info(f"[{current_account}] 平掉所有仓位...")
        await self.client.close_all_positions(self.config.market)

        # 3. 保存当前账号状态
        self.account_manager.save_state()

        # 4. 切换到下一个可用账号
        result = self.account_manager.switch_to_next_available_account()

        if result == "switched":
            new_client = self.account_manager.get_current_client()
            if new_client:
                self.client = new_client
                self.rate_state = self.account_manager.get_current_rate_state()

                new_account = self.account_manager.get_current_account_name()
                log.info(f"已切换到 {new_account}")

                # 重新认证新账号
                if await self.client.authenticate_interactive():
                    log.info(f"[{new_account}] 认证成功")
                else:
                    log.error(f"[{new_account}] 认证失败!")

            log.info("=" * 50)
            return "switched"

        elif result == "all_hour_limited":
            log.info("=" * 50)
            return "wait_hour"

        else:  # all_day_limited
            log.info("=" * 50)
            return "wait_day"


# =============================================================================
# 主入口
# =============================================================================

def parse_accounts(accounts_str: str) -> List[AccountInfo]:
    """
    解析多账号配置字符串
    格式: 私钥1,地址1;私钥2,地址2;私钥3,地址3
    """
    accounts = []
    if not accounts_str:
        return accounts

    pairs = accounts_str.strip().split(";")
    for i, pair in enumerate(pairs):
        pair = pair.strip()
        if not pair:
            continue

        parts = pair.split(",")
        if len(parts) != 2:
            log.warning(f"跳过无效的账号配置 #{i+1}: {pair[:20]}...")
            continue

        private_key = parts[0].strip()
        address = parts[1].strip()

        if not private_key.startswith("0x") or not address.startswith("0x"):
            log.warning(f"跳过无效的账号配置 #{i+1}: 私钥或地址格式错误")
            continue

        accounts.append(AccountInfo(
            l2_private_key=private_key,
            l2_address=address,
            name=f"账号#{i+1}"
        ))

    return accounts


async def main():
    # 加载环境变量
    load_dotenv()

    environment = os.getenv("PARADEX_ENVIRONMENT", "prod")
    market = os.getenv("MARKET", "BTC-USD-PERP")

    # 尝试加载多账号配置
    accounts_str = os.getenv("PARADEX_ACCOUNTS", "")
    accounts = parse_accounts(accounts_str)

    account_manager = None
    client = None

    if accounts:
        # 多账号模式
        log.info(f"检测到多账号配置: {len(accounts)} 个账号")
        account_manager = AccountManager(accounts, environment)

        # 重要: 先加载状态，恢复 current_index，然后再获取客户端
        # 这样确保重启后使用正确的账号
        account_manager.load_state()

        # 检查当前账号是否已达到 hour 限制，如果是则尝试切换
        if account_manager.is_account_hour_limited(account_manager.current_index):
            log.info(f"当前账号 #{account_manager.current_index + 1} 已达小时限制，尝试切换...")
            result = account_manager.switch_to_next_available_account()
            if result == "all_day_limited":
                log.error("所有账号今日额度已用完!")
                sys.exit(1)

        client = account_manager.get_current_client()

        if not client:
            log.error("无法初始化任何账号!")
            sys.exit(1)
    else:
        # 单账号模式（向后兼容）
        l2_private_key = os.getenv("PARADEX_L2_PRIVATE_KEY")
        l2_address = os.getenv("PARADEX_L2_ADDRESS")

        if not l2_private_key or not l2_address:
            log.error("请在 .env 文件中配置账号信息:")
            log.error("  方式1 (多账号): PARADEX_ACCOUNTS=私钥1,地址1;私钥2,地址2")
            log.error("  方式2 (单账号): PARADEX_L2_PRIVATE_KEY 和 PARADEX_L2_ADDRESS")
            log.error("参考 .env.example 文件")
            sys.exit(1)

        log.info("使用单账号模式")
        client = ParadexInteractiveClient(
            l2_private_key=l2_private_key,
            l2_address=l2_address,
            environment=environment
        )

    # 创建配置
    config = TradingConfig(market=market)

    # 读取交易大小配置
    fixed_size = os.getenv("FIXED_SIZE", "").strip()
    if fixed_size:
        config.fixed_size = fixed_size
        log.info(f"使用固定交易大小: {fixed_size} BTC")
    else:
        open_size_percent = os.getenv("OPEN_SIZE_PERCENT", "")
        if open_size_percent:
            config.open_size_percent = int(open_size_percent)
        log.info(f"使用余额百分比: {config.open_size_percent}%")

    # 创建并运行机器人
    bot = SniperBot(client, config, account_manager)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
