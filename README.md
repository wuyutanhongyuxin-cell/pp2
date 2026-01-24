# Jess-Para Sniper Bot (V27 Python API Version)

基于原 Console 脚本改写的 Python 后端交易机器人。

## 核心特性

- **Interactive Token**: 使用 `?token_usage=interactive` 获取 **0 手续费** Token
- **智能开仓**: 点差 ≤ 0.004% 且订单簿厚度 ≥ $600 时开仓
- **智能平仓**: 点差 ≤ 0.005% 时平仓，超时 3 秒强制市价平仓
- **限速保护**: 支持每秒/分钟/小时/天的交易次数限制
- **状态持久化**: 交易统计和限速状态自动保存

## 安装

### 1. 创建虚拟环境 (推荐)

```bash
cd paradex_sniper
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env` 并填入你的凭证:

```bash
cp .env.example .env
```

编辑 `.env`:

```env
PARADEX_L2_PRIVATE_KEY=0x你的L2私钥
PARADEX_L2_ADDRESS=0x你的L2地址
PARADEX_ENVIRONMENT=prod
MARKET=BTC-USD-PERP
```

### 如何获取 L2 私钥

1. 打开 Paradex 网页并登录
2. 点击右上角头像
3. 选择 "Wallet"
4. 点击 "复制 Paradex 私钥" (Copy Paradex Private Key)

## 运行

```bash
python sniper_bot.py
```

## 配置参数

在 `sniper_bot.py` 中的 `TradingConfig` 类可以调整:

```python
@dataclass
class TradingConfig:
    # 开仓条件
    spread_threshold_percent: float = 0.004   # 点差阈值 (%)
    min_order_book_size_usd: float = 600      # 订单簿最小厚度 ($)

    # 平仓条件
    close_spread_target: float = 0.005        # 平仓点差目标 (%)
    close_timeout_ms: int = 3000              # 超时强制平仓 (ms)

    # 仓位管理
    open_size_percent: int = 90               # 开仓使用余额百分比

    # 限速
    limits_per_second: int = 3
    limits_per_minute: int = 30
    limits_per_hour: int = 300
    limits_per_day: int = 1000
```

## 费率对比

| Token 类型 | Maker Fee | Taker Fee |
|-----------|-----------|-----------|
| Interactive (网页) | 0% | 0% |
| API (程序化) | 0.003% | 0.02% |

使用 `?token_usage=interactive` 参数认证后，API 下单也能享受 0 手续费！

## 文件说明

```
paradex_sniper/
├── sniper_bot.py       # 主程序
├── requirements.txt    # Python 依赖
├── .env.example        # 环境变量示例
├── .env                # 你的实际配置 (不要提交到 git)
├── sniper_state.json   # 运行状态持久化 (自动生成)
└── README.md           # 本文档
```

## 注意事项

1. **测试先行**: 建议先用小额资金测试
2. **网络要求**: 需要稳定的网络连接
3. **风险提示**: 自动交易存在风险，请谨慎使用
4. **私钥安全**: 不要将 `.env` 文件提交到代码仓库

## 日志示例

```
12:34:56 [INFO] Jess-Para Sniper Bot (V27 Python API)
12:34:56 [INFO] 市场: BTC-USD-PERP
12:34:56 [INFO] 点差阈值: 0.004%
12:34:57 [INFO] 认证成功! token_usage=interactive
12:35:01 [INFO] 条件满足! 点差=0.003%, 开始开仓...
12:35:02 [INFO] 下单成功: BUY 0.0001 @ 95315.9
12:35:02 [INFO] 确认: 订单使用 INTERACTIVE 模式 (0 手续费)
12:35:03 [INFO] 平仓成功 (点差满足): 0.0001
```
