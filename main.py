import os
import traceback
import requests
from flask import Flask
import google.auth

from application.rebalance_service import run_strategy_core as run_rebalance_cycle
from entrypoints.cloud_run import is_market_open_today
from quant_platform_kit.schwab import (
    fetch_account_snapshot,
    fetch_default_daily_price_history_candles,
    fetch_quotes,
    get_client_from_secret,
    submit_equity_order,
)
from strategy.allocation import (
    get_hybrid_allocation as strategy_get_hybrid_allocation,
    get_income_ratio as strategy_get_income_ratio,
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def get_project_id():
    try:
        _, project_id = google.auth.default()
        return project_id if project_id else os.getenv("GOOGLE_CLOUD_PROJECT")
    except Exception:
        return os.getenv("GOOGLE_CLOUD_PROJECT")

PROJECT_ID = get_project_id()
APP_KEY = os.getenv("SCHWAB_API_KEY")
APP_SECRET = os.getenv("SCHWAB_APP_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("GLOBAL_TELEGRAM_CHAT_ID")
SECRET_ID = "SCHWAB_TOKENS"
TOKEN_PATH = '/tmp/token.json'

CASH_RESERVE_RATIO = 0.05
INCOME_THRESHOLD_USD = float(os.getenv("INCOME_THRESHOLD_USD", "100000"))
QQQI_INCOME_RATIO = float(os.getenv("QQQI_INCOME_RATIO", "0.5"))

# Rebalance: minimum deviation (fraction of equity) to trigger trades
REBALANCE_THRESHOLD_RATIO = 0.01

# Order pricing: limit buy premium above ask price
LIMIT_BUY_PREMIUM = 1.005

# Sell-to-buy delay: seconds to wait after sells before buying
SELL_SETTLE_DELAY_SEC = 3

# Allocation breakpoints by account size tier
ALLOC_TIER1_BREAKPOINTS = [0, 15000, 30000, 70000]
ALLOC_TIER1_VALUES = [1.0, 0.95, 0.85, 0.70]
ALLOC_TIER2_BREAKPOINTS = [70000, 140000]
ALLOC_TIER2_VALUES = [0.70, 0.50]

# Risk parameters for large accounts (>140k)
RISK_LEVERAGE_FACTOR = 3.0
RISK_NUMERATOR = 0.30
RISK_AGG_CAP = 0.50

# ATR band scaling for entry/exit lines
ATR_EXIT_SCALE = 2.0
ATR_ENTRY_SCALE = 2.5
EXIT_LINE_FLOOR = 0.92
EXIT_LINE_CAP = 0.98
ENTRY_LINE_FLOOR = 1.02
ENTRY_LINE_CAP = 1.08

# ---------------------------------------------------------------------------
# Language / i18n
# ---------------------------------------------------------------------------
NOTIFY_LANG = os.getenv("NOTIFY_LANG", "en")

SIGNAL_ICONS = {
    "hold":   "💎",
    "entry":  "🚀",
    "reduce": "⚠️",
    "exit":   "🔴",
    "idle":   "💤",
}

I18N = {
    "zh": {
        "trade_header":      "🔔 【交易执行报告】",
        "heartbeat_header":  "💓 【心跳检测】",
        "error_header":      "🚨 【策略异常】",
        "signal_label":      "信号",
        "dashboard_label":   "📊 资产看板",
        "equity":            "净值",
        "buying_power":      "购买力",
        "no_trades":         "✅ 无需调仓",
        "separator":         "━━━━━━━━━━━━━━━━━━",
        "signal_hold":       "趋势持有",
        "signal_entry":      "入场信号",
        "signal_reduce":     "减仓信号",
        "signal_exit":       "离场信号",
        "signal_idle":       "等待信号",
        "limit_buy":         "限价买入",
        "market_buy":        "市价买入",
        "market_sell":       "市价卖出",
        "shares":            "股",
        "submitted":         "已下发",
        "failed":            "失败",
        "exception":         "异常",
        "buy_label":         "买入",
        "limit_buy_cmd":     "限价买入指令",
        "market_buy_cmd":    "市价买入指令",
        "market_sell_cmd":   "市价卖出指令",
    },
    "en": {
        "trade_header":      "🔔 【Trade Execution Report】",
        "heartbeat_header":  "💓 【Heartbeat】",
        "error_header":      "🚨 【Strategy Error】",
        "signal_label":      "Signal",
        "dashboard_label":   "📊 Dashboard",
        "equity":            "Equity",
        "buying_power":      "Buying Power",
        "no_trades":         "✅ No rebalance needed",
        "separator":         "━━━━━━━━━━━━━━━━━━",
        "signal_hold":       "Trend Hold",
        "signal_entry":      "Entry Signal",
        "signal_reduce":     "Reduce Signal",
        "signal_exit":       "Exit Signal",
        "signal_idle":       "Idle",
        "limit_buy":         "Limit Buy",
        "market_buy":        "Market Buy",
        "market_sell":       "Market Sell",
        "shares":            " shares",
        "submitted":         "submitted",
        "failed":            "failed",
        "exception":         "error",
        "buy_label":         "Buy",
        "limit_buy_cmd":     "Limit Buy",
        "market_buy_cmd":    "Market Buy",
        "market_sell_cmd":   "Market Sell",
    },
}


def t(key, **kwargs):
    """Return translated string for the current LANG setting."""
    lang = NOTIFY_LANG if NOTIFY_LANG in I18N else "en"
    template = I18N[lang].get(key, key)
    return template.format(**kwargs) if kwargs else template


def signal_text(icon_key):
    """Return the emoji + translated signal name, e.g. '💎 趋势持有'."""
    emoji = SIGNAL_ICONS.get(icon_key, "❓")
    name = t(f"signal_{icon_key}")
    return f"{emoji} {name}"


def validate_config():
    """Fail loudly at startup if required config is missing or invalid."""
    missing = [v for v in ("SCHWAB_API_KEY", "SCHWAB_APP_SECRET") if not os.getenv(v)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")
    if not (0.0 <= QQQI_INCOME_RATIO <= 1.0):
        raise ValueError(f"QQQI_INCOME_RATIO must be in [0,1], got {QQQI_INCOME_RATIO}")


validate_config()


def send_tg_message(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=15)
    except Exception as e:
        print(f"Telegram send failed: {e}", flush=True)


def get_hybrid_allocation(total_equity_usd, qqq_p, stop_line):
    return strategy_get_hybrid_allocation(
        total_equity_usd,
        qqq_p,
        stop_line,
        alloc_tier1_breakpoints=ALLOC_TIER1_BREAKPOINTS,
        alloc_tier1_values=ALLOC_TIER1_VALUES,
        alloc_tier2_breakpoints=ALLOC_TIER2_BREAKPOINTS,
        alloc_tier2_values=ALLOC_TIER2_VALUES,
        risk_leverage_factor=RISK_LEVERAGE_FACTOR,
        risk_agg_cap=RISK_AGG_CAP,
        risk_numerator=RISK_NUMERATOR,
    )


def get_income_ratio(total_equity_usd: float) -> float:
    return strategy_get_income_ratio(
        total_equity_usd,
        income_threshold_usd=INCOME_THRESHOLD_USD,
    )


# ---------------------------------------------------------------------------
# Strategy execution
# ---------------------------------------------------------------------------
def run_strategy_core(c, now_ny):
    return run_rebalance_cycle(
        c,
        now_ny,
        fetch_default_daily_price_history_candles=fetch_default_daily_price_history_candles,
        fetch_account_snapshot=fetch_account_snapshot,
        fetch_quotes=fetch_quotes,
        submit_equity_order=submit_equity_order,
        send_tg_message=send_tg_message,
        signal_text=signal_text,
        translator=t,
        income_threshold_usd=INCOME_THRESHOLD_USD,
        qqqi_income_ratio=QQQI_INCOME_RATIO,
        cash_reserve_ratio=CASH_RESERVE_RATIO,
        rebalance_threshold_ratio=REBALANCE_THRESHOLD_RATIO,
        limit_buy_premium=LIMIT_BUY_PREMIUM,
        sell_settle_delay_sec=SELL_SETTLE_DELAY_SEC,
        alloc_tier1_breakpoints=ALLOC_TIER1_BREAKPOINTS,
        alloc_tier1_values=ALLOC_TIER1_VALUES,
        alloc_tier2_breakpoints=ALLOC_TIER2_BREAKPOINTS,
        alloc_tier2_values=ALLOC_TIER2_VALUES,
        risk_leverage_factor=RISK_LEVERAGE_FACTOR,
        risk_agg_cap=RISK_AGG_CAP,
        risk_numerator=RISK_NUMERATOR,
        atr_exit_scale=ATR_EXIT_SCALE,
        atr_entry_scale=ATR_ENTRY_SCALE,
        exit_line_floor=EXIT_LINE_FLOOR,
        exit_line_cap=EXIT_LINE_CAP,
        entry_line_floor=ENTRY_LINE_FLOOR,
        entry_line_cap=ENTRY_LINE_CAP,
    )


@app.route("/", methods=["POST", "GET"])
def handle_schwab():
    try:
        c = get_client_from_secret(PROJECT_ID, SECRET_ID, APP_KEY, APP_SECRET, token_path=TOKEN_PATH)
        if not is_market_open_today():
            return "Market Closed", 200
        run_strategy_core(c, None)
        return "OK", 200
    except Exception:
        send_tg_message(f"{t('error_header')}\n{traceback.format_exc()}")
        return "Error", 500


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
