"""Allocation and plan helpers for CharlesSchwabPlatform."""

from __future__ import annotations

import numpy as np
import pandas as pd


def get_hybrid_allocation(
    total_equity_usd,
    qqq_p,
    stop_line,
    *,
    alloc_tier1_breakpoints,
    alloc_tier1_values,
    alloc_tier2_breakpoints,
    alloc_tier2_values,
    risk_leverage_factor,
    risk_agg_cap,
    risk_numerator,
):
    if total_equity_usd <= alloc_tier2_breakpoints[0]:
        target_agg = float(
            np.interp(total_equity_usd, alloc_tier1_breakpoints, alloc_tier1_values)
        )
    elif total_equity_usd <= alloc_tier2_breakpoints[1]:
        target_agg = float(
            np.interp(total_equity_usd, alloc_tier2_breakpoints, alloc_tier2_values)
        )
    else:
        if qqq_p <= stop_line:
            target_agg = 0.0
        else:
            risk = max(0.01, (qqq_p - stop_line) / qqq_p * risk_leverage_factor)
            target_agg = min(risk_agg_cap, risk_numerator / risk)
    target_yield = max(0.0, 1.0 - target_agg)
    return target_agg, target_yield


def get_income_ratio(total_equity_usd: float, *, income_threshold_usd: float) -> float:
    if total_equity_usd < income_threshold_usd:
        return 0.0
    if total_equity_usd <= 2 * income_threshold_usd:
        return float(
            np.interp(
                total_equity_usd,
                [income_threshold_usd, 2 * income_threshold_usd],
                [0.0, 0.40],
            )
        )
    return 0.60


def build_rebalance_plan(
    qqq_history,
    snapshot,
    *,
    signal_text_fn,
    translator,
    income_threshold_usd,
    qqqi_income_ratio,
    cash_reserve_ratio,
    rebalance_threshold_ratio,
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
    df_qqq = pd.DataFrame(qqq_history)
    qqq_p = df_qqq["close"].iloc[-1]
    ma200 = df_qqq["close"].rolling(200).mean().iloc[-1]

    true_range = pd.concat(
        [
            df_qqq["high"] - df_qqq["low"],
            abs(df_qqq["high"] - df_qqq["close"].shift(1)),
            abs(df_qqq["low"] - df_qqq["close"].shift(1)),
        ],
        axis=1,
    ).max(axis=1)
    atr_pct = true_range.rolling(14).mean().iloc[-1] / qqq_p
    exit_line = ma200 * max(
        exit_line_floor,
        min(exit_line_cap, 1.0 - (atr_pct * atr_exit_scale)),
    )
    entry_line = ma200 * max(
        entry_line_floor,
        min(entry_line_cap, 1.0 + (atr_pct * atr_entry_scale)),
    )

    strategy_symbols = ["TQQQ", "BOXX", "SPYI", "QQQI"]
    market_values = {symbol: 0.0 for symbol in strategy_symbols}
    quantities = {symbol: 0 for symbol in strategy_symbols}
    for position in snapshot.positions:
        if position.symbol in market_values:
            market_values[position.symbol] = float(position.market_value)
            quantities[position.symbol] = int(position.quantity)

    total_equity = snapshot.total_equity
    real_buying_power = float(snapshot.buying_power or 0.0)

    income_ratio = get_income_ratio(
        total_equity,
        income_threshold_usd=income_threshold_usd,
    )
    target_income_val = total_equity * income_ratio
    target_spyi_val = target_income_val * (1.0 - qqqi_income_ratio)
    target_qqqi_val = target_income_val * qqqi_income_ratio

    strategy_equity = max(0.0, total_equity - target_income_val)
    reserved = strategy_equity * cash_reserve_ratio
    agg_ratio, _ = get_hybrid_allocation(
        strategy_equity,
        qqq_p,
        exit_line,
        alloc_tier1_breakpoints=alloc_tier1_breakpoints,
        alloc_tier1_values=alloc_tier1_values,
        alloc_tier2_breakpoints=alloc_tier2_breakpoints,
        alloc_tier2_values=alloc_tier2_values,
        risk_leverage_factor=risk_leverage_factor,
        risk_agg_cap=risk_agg_cap,
        risk_numerator=risk_numerator,
    )

    target_tqqq_ratio, icon, _reason = 0.0, "idle", "no signal"
    if quantities["TQQQ"] > 0:
        if qqq_p < exit_line:
            target_tqqq_ratio, icon = 0.0, "exit"
        elif qqq_p < ma200:
            target_tqqq_ratio, icon = agg_ratio * 0.33, "reduce"
        else:
            target_tqqq_ratio, icon = agg_ratio, "hold"
    elif qqq_p > entry_line:
        target_tqqq_ratio, icon = agg_ratio, "entry"

    target_tqqq_val = strategy_equity * target_tqqq_ratio
    target_boxx_val = max(0.0, (strategy_equity - reserved) - target_tqqq_val)
    threshold = total_equity * rebalance_threshold_ratio

    sig_display = signal_text_fn(icon)
    separator = translator("separator")
    dashboard = (
        f"{translator('dashboard_label')} | {translator('equity')}: ${total_equity:,.2f}\n"
        f"TQQQ: ${market_values['TQQQ']:,.2f} | SPYI: ${market_values['SPYI']:,.2f} | "
        f"QQQI: ${market_values['QQQI']:,.2f} | BOXX: ${market_values['BOXX']:,.2f}\n"
        f"{translator('buying_power')}: ${real_buying_power:,.2f} | {translator('signal_label')}: {sig_display}\n"
        f"QQQ: {qqq_p:.2f} | MA200: {ma200:.2f} | Exit: {exit_line:.2f}"
    )

    return {
        "strategy_symbols": strategy_symbols,
        "account_hash": snapshot.metadata["account_hash"],
        "market_values": market_values,
        "quantities": quantities,
        "total_equity": total_equity,
        "real_buying_power": real_buying_power,
        "reserved": reserved,
        "threshold": threshold,
        "target_values": {
            "TQQQ": target_tqqq_val,
            "BOXX": target_boxx_val,
            "SPYI": target_spyi_val,
            "QQQI": target_qqqi_val,
        },
        "sig_display": sig_display,
        "dashboard": dashboard,
        "qqq_p": qqq_p,
        "ma200": ma200,
        "exit_line": exit_line,
        "separator": separator,
    }

