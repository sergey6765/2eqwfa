#!/usr/bin/env python3
"""
Double-swap trading bot with profit checks.

The bot performs the chain USDT -> BASE -> USDC -> USDT. It only
executes a trade when the estimated net profit after fees exceeds the
configured MIN_PROFIT threshold. This helps avoid entering trades that
are likely to end with a negative result.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List

import pandas as pd
from pybit.unified_trading import HTTP

# Configuration
API_KEY    = os.getenv("BYBIT_KEY",  "0lc1U4DrMBOYf")
API_SECRET = os.getenv("BYBIT_SECRET","3dXsvD03dFJV9fMlWVkcMBzH0")
TESTNET    = False

START_USDT = Decimal("15")
MIN_PROFIT = Decimal("0.1")
TAKER_FEE  = Decimal("0.002")
ACCOUNT    = "UNIFIED"
SLEEP      = 0.3

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)


def get_free_balance(coin: str, account: str = ACCOUNT) -> Decimal:
    """Return immediately available balance for a coin."""
    r = session.get_wallet_balance(accountType=account, coin=coin)
    info = r["result"]["list"][0]["coin"][0]
    wallet = Decimal(info["walletBalance"])
    locked = Decimal(info.get("locked", "0"))
    return wallet - locked


def get_step(symbol: str) -> Decimal:
    """Minimum quantity increment for a symbol."""
    bp = session.get_instruments_info(
        category="spot", symbol=symbol
    )["result"]["list"][0]["lotSizeFilter"]["basePrecision"]
    return Decimal(bp) if "." in bp else Decimal(1) / (10 ** int(bp))


def adjust(qty: Decimal, step: Decimal) -> str:
    """Round quantity down to a multiple of step."""
    return str(qty.quantize(step, rounding=ROUND_DOWN))


def estimate_net(base: str, amount: Decimal = START_USDT) -> Decimal:
    """Estimate net profit for a potential trade chain."""
    tickers = session.get_tickers(category="spot")["result"]["list"]
    price: Dict[str, Decimal] = {
        t["symbol"]: Decimal(t["lastPrice"])
        for t in tickers if t.get("lastPrice")
    }

    try:
        buy_price = price[f"{base}USDT"]
        sell_price = price[f"{base}USDC"]
        usdc_usdt = price["USDCUSDT"]
    except KeyError:
        return Decimal(0)

    qty_base = amount / buy_price
    usdc_after = qty_base * sell_price
    usdt_final = usdc_after * usdc_usdt
    gross = usdt_final - amount
    fees = (amount + usdc_after + usdt_final) * TAKER_FEE
    return gross - fees


def find_opps(start: Decimal = START_USDT,
              min_profit: Decimal = MIN_PROFIT) -> pd.DataFrame | None:
    """Search for profitable base coins."""
    tickers = session.get_tickers(category="spot")["result"]["list"]
    price: Dict[str, Decimal] = {
        t["symbol"]: Decimal(t["lastPrice"])
        for t in tickers if t.get("lastPrice")
    }

    if "USDCUSDT" not in price:
        print("Нет прибыльных комбинаций.")
        return None

    rows: List[Dict[str, Decimal | str]] = []
    usdcr = price["USDCUSDT"]
    for pair in price:
        if not pair.endswith("USDT"):
            continue
        base = pair[:-4]
        pair_usdc = base + "USDC"
        if pair_usdc not in price:
            continue

        buy = price[pair]
        sell = price[pair_usdc]
        gross = start / buy * sell * usdcr - start
        fees = start * TAKER_FEE + (start + gross) * TAKER_FEE * 2
        net = gross - fees

        if net >= min_profit:
            rows.append(
                {
                    "base": base,
                    "net": net.quantize(Decimal("0.000001")),
                    "net_%": (net / start * 100).quantize(Decimal("0.001")),
                }
            )

    if not rows:
        print("Нет прибыльных комбинаций.")
        return None

    return pd.DataFrame(rows).sort_values("net", ascending=False).reset_index(drop=True)


def double_swap(base: str) -> Decimal:
    """Execute the swap chain for `base` and return resulting USDT profit."""
    base_usdt = base + "USDT"
    base_usdc = base + "USDC"

    expected = estimate_net(base)
    if expected < MIN_PROFIT:
        raise RuntimeError("Ожидаемая прибыль ниже минимума")

    bal_before = get_free_balance("USDT")

    session.place_order(
        category="spot",
        symbol=base_usdt,
        side="Buy",
        orderType="Market",
        qty=str(START_USDT),
        marketUnit="quoteCoin",
        timeInForce="IOC",
    )
    time.sleep(SLEEP)
    base_qty = get_free_balance(base)
    if base_qty == 0:
        raise RuntimeError("BASE не зачислен")

    step_base = get_step(base_usdc)
    qty_base = adjust(base_qty, step_base)
    if Decimal(qty_base) == 0:
        raise RuntimeError("BASE меньше шага — нечего продавать")

    session.place_order(
        category="spot",
        symbol=base_usdc,
        side="Sell",
        orderType="Market",
        qty=qty_base,
        timeInForce="IOC",
    )
    time.sleep(SLEEP)

    usdc_qty = get_free_balance("USDC")
    step_usdc = get_step("USDCUSDT")
    qty_usdc = adjust(usdc_qty, step_usdc)
    if Decimal(qty_usdc) == 0:
        raise RuntimeError("USDC меньше шага — нечего продавать")

    session.place_order(
        category="spot",
        symbol="USDCUSDT",
        side="Sell",
        orderType="Market",
        qty=qty_usdc,
        timeInForce="IOC",
    )
    time.sleep(SLEEP)

    bal_after = get_free_balance("USDT")
    return bal_after - bal_before


if __name__ == "__main__":
    opps = find_opps()
    if opps is None:
        raise SystemExit

    best = opps.iloc[0]
    base = best["base"]
    target = best["net"]
    print(f"Пробуем {base}, целевая прибыль ≈ {target:.4f} USDT")

    try:
        profit = double_swap(base)
        if profit < 0:
            print(f"‼ Прибыль отрицательная: {profit:.6f} USDT")
        else:
            print(
                f"✔ Итоговая прибыль: {profit:.6f} USDT "
                f"({profit / START_USDT * 100:.3f} %)"
            )
    except Exception as e:
        print("‼ Ошибка:", e)
