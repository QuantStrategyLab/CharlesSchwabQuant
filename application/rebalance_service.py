"""Application orchestration for CharlesSchwabQuant."""

from __future__ import annotations

import time

from quant_platform_kit.common.models import OrderIntent

from strategy.allocation import build_rebalance_plan


def run_strategy_core(
    client,
    now_ny,
    *,
    fetch_default_daily_price_history_candles,
    fetch_account_snapshot,
    fetch_quotes,
    submit_equity_order,
    send_tg_message,
    signal_text,
    translator,
    income_threshold_usd,
    qqqi_income_ratio,
    cash_reserve_ratio,
    rebalance_threshold_ratio,
    limit_buy_premium,
    sell_settle_delay_sec,
    alloc_tier1_breakpoints,
    alloc_tier1_values,
    alloc_tier2_breakpoints,
    alloc_tier2_values,
    risk_leverage_factor,
    risk_agg_cap,
    risk_numerator,
    atr_exit_scale,
    atr_entry_scale,
    exit_line_floor,
    exit_line_cap,
    entry_line_floor,
    entry_line_cap,
):
    del now_ny

    strategy_symbols = ["TQQQ", "BOXX", "SPYI", "QQQI"]
    snapshot = fetch_account_snapshot(client, strategy_symbols=strategy_symbols)
    plan = build_rebalance_plan(
        fetch_default_daily_price_history_candles(client, "QQQ"),
        snapshot,
        signal_text_fn=signal_text,
        translator=translator,
        income_threshold_usd=income_threshold_usd,
        qqqi_income_ratio=qqqi_income_ratio,
        cash_reserve_ratio=cash_reserve_ratio,
        rebalance_threshold_ratio=rebalance_threshold_ratio,
        alloc_tier1_breakpoints=alloc_tier1_breakpoints,
        alloc_tier1_values=alloc_tier1_values,
        alloc_tier2_breakpoints=alloc_tier2_breakpoints,
        alloc_tier2_values=alloc_tier2_values,
        risk_leverage_factor=risk_leverage_factor,
        risk_agg_cap=risk_agg_cap,
        risk_numerator=risk_numerator,
        atr_exit_scale=atr_exit_scale,
        atr_entry_scale=atr_entry_scale,
        exit_line_floor=exit_line_floor,
        exit_line_cap=exit_line_cap,
        entry_line_floor=entry_line_floor,
        entry_line_cap=entry_line_cap,
    )

    quote_snapshots = fetch_quotes(client, strategy_symbols)
    quotes = {
        symbol: {
            "lastPrice": quote_snapshots[symbol].last_price,
            "askPrice": quote_snapshots[symbol].ask_price or quote_snapshots[symbol].last_price,
        }
        for symbol in strategy_symbols
    }
    trade_logs = []

    def execute_fire_forget(symbol, action_type, quantity, price=None):
        if quantity <= 0:
            return False
        try:
            price_text = "{:.2f}".format(price) if price else None
            if action_type == "SELL":
                order_intent = OrderIntent(symbol=symbol, side="sell", quantity=quantity)
            elif action_type == "BUY_LIMIT":
                order_intent = OrderIntent(
                    symbol=symbol,
                    side="buy",
                    quantity=quantity,
                    order_type="limit",
                    limit_price=float(price),
                )
            elif action_type == "BUY_MARKET":
                order_intent = OrderIntent(symbol=symbol, side="buy", quantity=quantity)
            else:
                return False

            report = submit_equity_order(client, plan["account_hash"], order_intent)
            success = report.status == "accepted"
            info = report.broker_order_id if success else report.raw_payload.get("detail", report.status)
            if success:
                if action_type == "SELL":
                    trade_logs.append(
                        f"✅ 📉 {translator('market_sell_cmd')} {symbol}: {quantity}{translator('shares')} (ID: {info})"
                    )
                elif action_type == "BUY_LIMIT":
                    trade_logs.append(
                        f"✅ 💰 {translator('limit_buy_cmd')} {symbol} (${price_text}): {quantity}{translator('shares')} {translator('submitted')} (ID: {info})"
                    )
                elif action_type == "BUY_MARKET":
                    trade_logs.append(
                        f"✅ 📈 {translator('market_buy_cmd')} {symbol}: {quantity}{translator('shares')} (ID: {info})"
                    )
                return True

            if action_type == "SELL":
                message = f"❌ {translator('market_sell')} {symbol}: {quantity}{translator('shares')} {translator('failed')} - {info}"
            elif action_type == "BUY_LIMIT":
                message = f"❌ {translator('limit_buy')} {symbol}: {quantity}{translator('shares')} {translator('failed')} - {info}"
            else:
                message = f"❌ {translator('market_buy')} {symbol}: {quantity}{translator('shares')} {translator('failed')} - {info}"
            trade_logs.append(message)
            send_tg_message(message)
            return False
        except Exception as exc:
            message = f"🚨 {symbol} {translator('buy_label')} {quantity}{translator('shares')} {translator('exception')}: {exc}"
            trade_logs.append(message)
            send_tg_message(message)
            return False

    market_values = plan["market_values"]
    target_values = plan["target_values"]
    threshold = plan["threshold"]
    sell_executed = False
    for symbol in ("TQQQ", "SPYI", "QQQI", "BOXX"):
        current = market_values[symbol]
        target = target_values[symbol]
        if current > (target + threshold):
            quantity = int((current - target) // quotes[symbol]["lastPrice"])
            execute_fire_forget(symbol, "SELL", quantity)
            sell_executed = True

    if sell_executed:
        time.sleep(sell_settle_delay_sec)

    estimated_buying_power = max(0, plan["real_buying_power"] - plan["reserved"])
    for symbol in ("SPYI", "QQQI", "TQQQ"):
        target_val = target_values[symbol]
        if market_values[symbol] < (target_val - threshold):
            amount_to_spend = min(target_val - market_values[symbol], estimated_buying_power)
            if amount_to_spend > 0:
                ask = quotes[symbol]["askPrice"]
                quantity = int(amount_to_spend // ask)
                if quantity > 0:
                    limit_price = round(ask * limit_buy_premium, 2)
                    execute_fire_forget(symbol, "BUY_LIMIT", quantity, limit_price)
                    estimated_buying_power -= quantity * limit_price

    if estimated_buying_power > quotes["BOXX"]["lastPrice"] * 2:
        quantity = int(estimated_buying_power // quotes["BOXX"]["lastPrice"])
        if quantity > 0:
            execute_fire_forget("BOXX", "BUY_MARKET", quantity)

    if trade_logs:
        trade_message = (
            f"{translator('trade_header')}\n"
            f"📊 {translator('signal_label')}: {plan['sig_display']}\n\n"
            f"{plan['dashboard']}\n"
            f"{plan['separator']}\n"
            + "\n".join(trade_logs)
        )
        send_tg_message(trade_message)
    else:
        no_trade_message = (
            f"{translator('heartbeat_header')}\n"
            f"💰 {translator('equity')}: ${plan['total_equity']:,.2f}\n"
            f"{plan['separator']}\n"
            f"TQQQ: ${market_values['TQQQ']:,.2f}  BOXX: ${market_values['BOXX']:,.2f}\n"
            f"QQQI: ${market_values['QQQI']:,.2f}  SPYI: ${market_values['SPYI']:,.2f}\n"
            f"{plan['separator']}\n"
            f"🎯 {translator('signal_label')}: {plan['sig_display']}\n"
            f"QQQ: {plan['qqq_p']:.2f} | MA200: {plan['ma200']:.2f} | Exit: {plan['exit_line']:.2f}\n"
            f"{plan['separator']}\n"
            f"{translator('no_trades')}"
        )
        print(no_trade_message, flush=True)
        send_tg_message(no_trade_message)

