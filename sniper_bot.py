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
    open_size_percent: int = 90
    close_size_percent: int = 100
    price_offset: float = 0

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


# =============================================================================
# 交易机器人主逻辑
# =============================================================================

class SniperBot:
    """狙击机器人 - 对应原脚本的主循环逻辑"""

    def __init__(self, client: ParadexInteractiveClient, config: TradingConfig):
        self.client = client
        self.config = config
        self.stats = Stats()
        self.rate_state = RateLimitState()

        # 加载持久化数据
        self._load_state()

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

    def _record_trade(self):
        """记录一次交易"""
        self.rate_state.trades.append(int(time.time() * 1000))
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

            # 计算开仓大小（使用余额的 90%）
            mid_price = (bbo["bid"] + bbo["ask"]) / 2
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
        log.info("Jess-Para Sniper Bot (V27 Python API)")
        log.info(f"市场: {self.config.market}")
        log.info(f"点差阈值: {self.config.spread_threshold_percent}%")
        log.info(f"订单簿最小厚度: ${self.config.min_order_book_size_usd}")
        log.info("=" * 50)

        # 初始认证
        if not await self.client.authenticate_interactive():
            log.error("初始认证失败!")
            return

        log.info("认证成功，开始监控...")
        self.config.enabled = True
        cycle_count = 0
        last_status_time = time.time()

        while True:
            try:
                if not self.config.enabled:
                    log.info("机器人已暂停")
                    await asyncio.sleep(1)
                    continue

                success, msg = await self.run_cycle()
                cycle_count += 1

                if success:
                    log.info(f"交易完成: {msg}")
                    await asyncio.sleep(self.config.cycle_every_ms / 1000)
                else:
                    # 每 10 秒输出一次状态日志
                    if time.time() - last_status_time >= 10:
                        log.info(f"[监控中] 周期#{cycle_count} | {msg}")
                        last_status_time = time.time()
                    await asyncio.sleep(0.2)

            except KeyboardInterrupt:
                log.info("收到中断信号，正在退出...")
                break
            except Exception as e:
                log.error(f"循环异常: {e}")
                await asyncio.sleep(1)

        log.info("机器人已停止")
        self._save_state()


# =============================================================================
# 主入口
# =============================================================================

async def main():
    # 加载环境变量
    load_dotenv()

    l2_private_key = os.getenv("PARADEX_L2_PRIVATE_KEY")
    l2_address = os.getenv("PARADEX_L2_ADDRESS")
    environment = os.getenv("PARADEX_ENVIRONMENT", "prod")
    market = os.getenv("MARKET", "BTC-USD-PERP")

    if not l2_private_key or not l2_address:
        log.error("请在 .env 文件中配置 PARADEX_L2_PRIVATE_KEY 和 PARADEX_L2_ADDRESS")
        log.error("参考 .env.example 文件")
        sys.exit(1)

    # 创建客户端
    client = ParadexInteractiveClient(
        l2_private_key=l2_private_key,
        l2_address=l2_address,
        environment=environment
    )

    # 创建配置
    config = TradingConfig(market=market)

    # 创建并运行机器人
    bot = SniperBot(client, config)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
