# Para Sniper Bot (V27 Python API - 多账号版)

Paradex 交易所自动狙击交易机器人，支持多账号轮换和零手续费交易。

## 核心特性

- **零手续费**: 使用 `?token_usage=interactive` 获取 Interactive Token，享受 0% 手续费
- **智能开仓**: 点差 ≤ 0.004% 且订单簿厚度 ≥ $600 时触发
- **智能平仓**: 点差满足目标时平仓，超时 3 秒强制市价平仓
- **多账号轮换**: 支持配置多个账号，一个达到限制自动切换下一个
- **限速保护**: 秒/分/时/天 四层交易频率限制
- **状态持久化**: 交易统计和账号状态自动保存，重启后恢复

## 费率对比

| Token 类型 | Maker Fee | Taker Fee |
|-----------|-----------|-----------|
| Interactive (本机器人) | **0%** | **0%** |
| API (普通程序化) | 0.003% | 0.02% |

## 快速开始

### 1. 环境准备

```bash
# 克隆仓库
git clone https://github.com/wuyutanhongyuxin-cell/pp2.git
cd pp2

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Linux/Mac:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置账号

复制配置文件模板：

```bash
cp .env.example .env
```

编辑 `.env` 文件，配置你的账号信息：

#### 单账号模式

```env
PARADEX_L2_PRIVATE_KEY=0x你的L2私钥
PARADEX_L2_ADDRESS=0x你的L2地址
PARADEX_ENVIRONMENT=prod
MARKET=BTC-USD-PERP
```

#### 多账号模式（推荐）

```env
# 格式: 私钥1,地址1;私钥2,地址2;私钥3,地址3
PARADEX_ACCOUNTS=0x私钥1,0x地址1;0x私钥2,0x地址2;0x私钥3,0x地址3
PARADEX_ENVIRONMENT=prod
MARKET=BTC-USD-PERP
```

### 3. 获取 Paradex 凭证

1. 打开 [Paradex](https://app.paradex.trade/)
2. 连接钱包并登录
3. 点击右上角头像 → **Wallet**
4. 复制 **Paradex Private Key** (L2 私钥)
5. 复制 **Paradex Address** (L2 地址)

### 4. 运行机器人

```bash
python sniper_bot.py
```

## 配置参数说明

### 交易参数 (TradingConfig)

| 参数 | 默认值 | 说明 |
|-----|-------|------|
| `spread_threshold_percent` | 0.004 | 开仓点差阈值 (%) |
| `min_order_book_size_usd` | 600 | 订单簿最小厚度 ($) |
| `close_spread_target` | 0.005 | 平仓点差目标 (%) |
| `close_timeout_ms` | 3000 | 超时强制平仓时间 (ms) |
| `open_size_percent` | 90 | 开仓使用余额百分比 |

### 限速参数

| 参数 | 默认值 | 说明 |
|-----|-------|------|
| `limits_per_second` | 3 | 每秒最大交易数 |
| `limits_per_minute` | 30 | 每分钟最大交易数 |
| `limits_per_hour` | 300 | 每小时最大交易数 |
| `limits_per_day` | 1000 | 每天最大交易数 |

## 多账号轮换机制

### 工作原理

1. 机器人启动时加载所有配置的账号
2. 从第一个账号开始交易
3. 当账号达到日限制 (1000 笔) 时，自动切换到下一个账号
4. 所有账号都达到限制时，等待到第二天凌晨自动重启
5. 每个账号的交易记录独立保存，重启后恢复

### 状态文件

- `sniper_state.json`: 单账号模式的状态
- `account_states.json`: 多账号模式的状态

### 日志示例

```
==================================================
Jess-Para Sniper Bot (V27 Python API - 多账号版)
市场: BTC-USD-PERP
点差阈值: 0.004%
订单簿最小厚度: $600
多账号模式: 共 3 个账号
  账号#1: 今日 850/1000 🟢 可用
  账号#2: 今日 1000/1000 🔴 已满
  账号#3: 今日 200/1000 🟢 可用
当前账号: 账号#1
==================================================
19:30:15 [INFO] 认证成功! token_usage=interactive
19:30:15 [INFO] 认证成功，开始监控...
19:30:20 [INFO] 条件满足! 点差=0.0020%, 开始开仓...
19:30:21 [INFO] 下单成功: BUY 0.0001 @ 89500.5
19:30:21 [INFO] 确认: 订单使用 INTERACTIVE 模式 (0 手续费)
```

## 文件结构

```
pp2/
├── sniper_bot.py        # 主程序
├── requirements.txt     # Python 依赖
├── .env.example         # 环境变量示例
├── .env                 # 你的实际配置 (不要提交到 git)
├── .gitignore           # Git 忽略规则
├── sniper_state.json    # 单账号状态 (自动生成)
├── account_states.json  # 多账号状态 (自动生成)
└── README.md            # 本文档
```

## 注意事项

1. **私钥安全**: 绝对不要将 `.env` 文件提交到 Git
2. **资金风险**: 自动交易存在风险，请使用小额资金测试
3. **网络要求**: 需要稳定的网络连接
4. **API 限制**: 遵守 Paradex API 的使用规则

## 常见问题

### Q: 为什么订单没有 INTERACTIVE 标志？

确保使用的是通过 `?token_usage=interactive` 获取的 JWT Token。本机器人已自动处理。

### Q: 为什么一直显示"点差过大"？

市场波动时点差会增大。机器人会持续监控，等待点差满足条件时自动开仓。

### Q: 如何添加更多账号？

在 `.env` 文件的 `PARADEX_ACCOUNTS` 中用分号分隔添加更多账号：

```env
PARADEX_ACCOUNTS=私钥1,地址1;私钥2,地址2;私钥3,地址3;私钥4,地址4
```

### Q: 账号切换后需要重新认证吗？

是的，机器人会自动为新账号获取 Interactive Token。

## 技术栈

- Python 3.10+
- paradex-py (官方 SDK)
- aiohttp (异步 HTTP)
- python-dotenv (环境变量)

## 许可证

MIT License

## 免责声明

本软件仅供学习和研究使用。使用本软件进行交易的风险由用户自行承担。作者不对任何因使用本软件造成的损失负责。
