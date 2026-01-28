# Ryuryu's FOREX MT5 EURUSD Bot
# * Only Shorts * (Production Mode #6973)
# -------------------------------------
# (c) 2023 Ryan Hayabusa
# GitGub: https://github.com/ryu878
# Web: https://aadresearch.xyz
# Discord: https://discord.gg/zSw58e9Uvf
# Telegram: https://t.me/aadresearch
# -------------------------------------
# Modified: Added hedge failsafe for trending markets

import MetaTrader5 as mt5
import pandas as pd
import time
import ta



# Main settings
magic = 12345678
account_id = 1234567890

# Symbol settings
symbol = 'EURUSD'
sl_multiplier = 13

lot = 0.1
add_lot = 0.03
min_deleverage = 15
deleverage_steps = 7
take_profit_short = 21
sl_short = take_profit_short * sl_multiplier

# Hedge settings
hedge_trigger_pct = 0.75  # Trigger hedge at 75% of SL (3/4)
hedge_multiplier = 2.0  # Hedge size = 2x position
rsi_trending_threshold = 70  # RSI above this = trending up
candle_trend_count = 3  # Number of bullish candles to confirm trend
candle_body_pct = 0.6  # Candle body must be 60% of total range

# Compression detection settings (dynamic lot sizing) - uses H1 timeframe
compression_lookback = 10  # H1 candles to calculate baseline average body
compression_check = 3  # Recent H1 candles to check for compression
compression_threshold = 0.5  # Bodies must be 50% smaller than baseline
min_lot = 0.05  # Minimum lot when NOT in compression
compression_lot = 0.1  # Increased lot when compression detected
compression_add_lot = 0.05  # Averaging lot during compression
normal_add_lot = 0.01  # Averaging lot when not compressed

# Track hedge state
hedge_active = False
hedge_identifier = 0
short_entry_price = 0
short_volume_at_hedge = 0

# Track compression state
is_compressed = False

# Trade counter for identification
trade_counter = 0


# Init
if not mt5.initialize():
    print('initialize() failed, error code =', mt5.last_error())
    quit()

# Timeframe settings
timeframe = mt5.TIMEFRAME_M1

selected = mt5.symbol_select(symbol)
if not selected:
    print('symbol_select({}) failed, error code = {}'.format(symbol, mt5.last_error()))
    quit()

# Get bars and calculate SMA + RSI
def get_sma():
    bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, 240)
    if bars is None:
        print('copy_rates_from_pos() failed, error code =', mt5.last_error())
        quit()

    df = pd.DataFrame(bars)
    df.set_index(pd.to_datetime(df['time'], unit='s'), inplace=True)
    df.drop(columns=['time'], inplace=True)
    df['sma_6H'] = ta.trend.sma_indicator(df['high'], window=6)
    df['sma_6L'] = ta.trend.sma_indicator(df['low'], window=6)
    df['sma_33'] = ta.trend.sma_indicator(df['close'], window=33)
    df['sma_60'] = ta.trend.sma_indicator(df['close'], window=60)
    df['sma_120'] = ta.trend.sma_indicator(df['close'], window=120)
    df['sma_240'] = ta.trend.sma_indicator(df['close'], window=240)

    # RSI for trend detection
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)

    global sma6H, sma6L, sma33, sma60, sma120, sma240, current_rsi, recent_candles
    sma6H = df['sma_6H'].iloc[-1]
    sma6L = df['sma_6L'].iloc[-1]
    sma33 = df['sma_33'].iloc[-1]
    sma60 = df['sma_60'].iloc[-1]
    sma120 = df['sma_120'].iloc[-1]
    sma240 = df['sma_240'].iloc[-1]
    current_rsi = df['rsi'].iloc[-1]

    # Store recent candles for trend analysis (M1)
    recent_candles = df[['open', 'high', 'low', 'close']].tail(candle_trend_count)


def is_trending_up():
    """Check if market is trending up using RSI and candle analysis"""
    global current_rsi, recent_candles

    # Check RSI
    rsi_trending = current_rsi > rsi_trending_threshold

    # Check candles - count strong bullish candles
    bullish_count = 0
    for _, candle in recent_candles.iterrows():
        candle_range = candle['high'] - candle['low']
        if candle_range == 0:
            continue
        body = candle['close'] - candle['open']
        body_ratio = abs(body) / candle_range

        # Bullish candle with strong body
        if body > 0 and body_ratio >= candle_body_pct:
            bullish_count += 1

    candles_trending = bullish_count >= candle_trend_count

    # Both conditions must be true for trending
    return rsi_trending and candles_trending


def detect_compression():
    """
    Detect market compression using H1 candle body analysis.
    Compression = last 3 H1 candles have bodies 50%+ smaller than baseline average.
    Returns True if compression detected (good for mean reversion).
    """
    global is_compressed

    # Fetch H1 data for compression analysis
    h1_bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, compression_lookback + compression_check)
    if h1_bars is None or len(h1_bars) < compression_lookback + compression_check:
        print("H1 data unavailable for compression check")
        return False

    h1_df = pd.DataFrame(h1_bars)

    # Calculate body sizes for all H1 candles
    bodies = []
    for _, candle in h1_df.iterrows():
        body = abs(candle['close'] - candle['open'])
        bodies.append(body)

    # Baseline: average body of first N candles (prior trend)
    baseline_bodies = bodies[:compression_lookback]
    baseline_avg = sum(baseline_bodies) / len(baseline_bodies) if baseline_bodies else 0

    if baseline_avg == 0:
        return False

    # Recent: last 3 H1 candles
    recent_bodies = bodies[-compression_check:]
    recent_avg = sum(recent_bodies) / len(recent_bodies) if recent_bodies else 0

    # Check if recent bodies are significantly smaller (compression)
    compression_ratio = recent_avg / baseline_avg

    # Compression = recent bodies are 50% or less of baseline
    is_compressed = compression_ratio <= compression_threshold

    if is_compressed:
        print(f"H1 COMPRESSION DETECTED: Recent avg body {compression_ratio:.1%} of baseline")
    else:
        print(f"H1 NO COMPRESSION: Body ratio {compression_ratio:.1%}")

    return is_compressed


def get_dynamic_lot():
    """Return lot size based on compression state"""
    if is_compressed:
        return compression_lot
    else:
        return min_lot


def get_dynamic_add_lot():
    """Return averaging lot size based on compression state"""
    if is_compressed:
        return compression_add_lot
    else:
        return normal_add_lot


def get_position_data():
    """Get position data for both short and hedge positions"""
    global pos_price, identifier, volume, hedge_active, hedge_identifier
    global short_positions, hedge_positions

    positions = mt5.positions_get(symbol=symbol)
    short_positions = []
    hedge_positions = []

    if positions is None or len(positions) == 0:
        pos_price = 0
        identifier = 0
        volume = 0
        hedge_active = False
        hedge_identifier = 0
        return

    for position in positions:
        post_dict = position._asdict()
        pos_type = post_dict['type']  # 0 = BUY, 1 = SELL

        if pos_type == 1:  # SELL (short)
            short_positions.append(post_dict)
            pos_price = post_dict['price_open']
            identifier = post_dict['identifier']
            volume = post_dict['volume']
            print(f"SHORT: {pos_price}, ID: {identifier}, Vol: {volume}")
        elif pos_type == 0:  # BUY (hedge)
            hedge_positions.append(post_dict)
            hedge_active = True
            hedge_identifier = post_dict['identifier']
            print(f"HEDGE: {post_dict['price_open']}, ID: {hedge_identifier}, Vol: {post_dict['volume']}")

    if len(short_positions) == 0:
        pos_price = 0
        identifier = 0
        volume = 0


def get_total_short_volume():
    """Calculate total volume of all short positions"""
    total = 0
    for pos in short_positions:
        total += pos['volume']
    return total


def get_short_pnl():
    """Calculate current P&L of short positions"""
    total_pnl = 0
    for pos in short_positions:
        entry = pos['price_open']
        current = bid  # Use bid for closing shorts
        pnl_points = (entry - current) / point
        total_pnl += pnl_points * pos['volume']
    return total_pnl


def get_hedge_pnl():
    """Calculate current P&L of hedge positions"""
    total_pnl = 0
    for pos in hedge_positions:
        entry = pos['price_open']
        current = ask  # Use ask for closing longs
        pnl_points = (current - entry) / point
        total_pnl += pnl_points * pos['volume']
    return total_pnl


def open_hedge():
    """Open a hedge (BUY) position at 2x the short volume"""
    global hedge_active, hedge_identifier, short_entry_price, short_volume_at_hedge

    total_short_vol = get_total_short_volume()
    hedge_volume = round(total_short_vol * hedge_multiplier, 2)

    # Store short info at time of hedge
    short_entry_price = pos_price
    short_volume_at_hedge = total_short_vol

    hedge_order = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": hedge_volume,
        "type": mt5.ORDER_TYPE_BUY,
        "price": ask,
        "deviation": deviation,
        "magic": magic,
        "comment": f"RYU-HEDGE-{trade_counter}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(hedge_order)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        hedge_active = True
        hedge_identifier = result.order
        print(f"HEDGE OPENED: {hedge_volume} lots @ {ask}")
    else:
        print(f"HEDGE FAILED: {result.retcode}")

    return result


def close_all_positions():
    """Close all positions (shorts and hedge)"""
    global hedge_active, hedge_identifier

    # Close all short positions
    for pos in short_positions:
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos['volume'],
            "type": mt5.ORDER_TYPE_BUY,  # Buy to close short
            "position": pos['identifier'],
            "price": ask,
            "deviation": deviation,
            "magic": magic,
            "comment": f"RYU-CLOSE-S-{pos['identifier']}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(close_request)
        print(f"CLOSED SHORT {pos['identifier']}: {result.retcode}")

    # Close all hedge positions
    for pos in hedge_positions:
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos['volume'],
            "type": mt5.ORDER_TYPE_SELL,  # Sell to close long
            "position": pos['identifier'],
            "price": bid,
            "deviation": deviation,
            "magic": magic,
            "comment": f"RYU-CLOSE-H-{pos['identifier']}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(close_request)
        print(f"CLOSED HEDGE {pos['identifier']}: {result.retcode}")

    hedge_active = False
    hedge_identifier = 0


def check_hedge_recovery():
    """Check if hedge has recovered the short's loss - close all at breakeven"""
    if not hedge_active:
        return False

    short_pnl = get_short_pnl()
    hedge_pnl = get_hedge_pnl()
    net_pnl = short_pnl + hedge_pnl

    print(f"Short P&L: {short_pnl:.1f}, Hedge P&L: {hedge_pnl:.1f}, Net: {net_pnl:.1f}")

    # Close all when hedge profit >= short loss (net >= 0)
    if net_pnl >= 0:
        print("HEDGE RECOVERY COMPLETE - Closing all at breakeven")
        close_all_positions()
        return True

    return False


# Define prices
def get_ask_bid():
    global ask, bid
    ask = mt5.symbol_info_tick(symbol).ask
    bid = mt5.symbol_info_tick(symbol).bid

point = mt5.symbol_info(symbol).point
deviation = 20


short_positions = []
hedge_positions = []

while True:

    identifier = 0
    volume = 0
    pos_price = 0

    get_sma()
    get_ask_bid()
    get_position_data()

    # Detect market compression for dynamic lot sizing
    detect_compression()
    current_lot = get_dynamic_lot()
    current_add_lot = get_dynamic_add_lot()

    # Define Sell Order (with dynamic lot size)

    sell_order = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": current_lot,
        "type": mt5.ORDER_TYPE_SELL,
        "price": ask,
        "sl": ask + sl_short * point,
        "tp": ask - take_profit_short * point,
        "deviation": deviation,
        "magic": magic,
        "comment": f"RYU-INIT-{trade_counter}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
        }

    additional_sell_order = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": current_add_lot,
        "type": mt5.ORDER_TYPE_SELL,
        "price": ask,
        "sl": pos_price + sl_short * point,
        "tp": pos_price - take_profit_short * point,
        "deviation": deviation,
        "magic": magic,
        "comment": f"RYU-AVG-{trade_counter}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
        }

    sltp_request_sell_pos = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "volume": float(volume),
        "type": mt5.ORDER_TYPE_SELL,
        "position": identifier,
        "sl": pos_price + sl_short * point,
        "tp": pos_price - take_profit_short * point,
        "magic": magic,
        "comment": "Change stop loss for Sell position",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    sltp_request_buy_pos = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "volume": float(volume),
        "type": mt5.ORDER_TYPE_BUY,
        "position": identifier,
        "sl": pos_price - sl_short * point,
        "tp": pos_price + take_profit_short * point,
        "magic": magic,
        "comment": "Change stop loss for Buy position",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    # Check if MA order os OK
    good_long_ma_order = ask > sma6H

    # ==========================================
    # HEDGE LOGIC - Check for recovery first
    # ==========================================
    if hedge_active:
        # Check if hedge has recovered - close all at breakeven
        if check_hedge_recovery():
            print("All positions closed at breakeven")
            time.sleep(0.1)
            continue  # Skip rest of loop, start fresh

    # ==========================================
    # HEDGE TRIGGER - At 3/4 of Stop Loss
    # ==========================================
    if pos_price > 0 and not hedge_active:
        # Calculate drawdown in points
        drawdown_points = (ask - pos_price) / point

        # Calculate hedge trigger level (75% of SL)
        hedge_trigger_points = sl_short * hedge_trigger_pct

        if drawdown_points >= hedge_trigger_points:
            print(f"DRAWDOWN: {drawdown_points:.1f} pts (Trigger: {hedge_trigger_points:.1f})")
            print(f"RSI: {current_rsi:.1f}")

            # Check if market is trending (failsafe)
            if is_trending_up():
                print("TRENDING DETECTED - Opening hedge")
                open_hedge()
            else:
                print("RANGING - No hedge needed, expecting reversal")

    # ==========================================
    # NORMAL TRADING LOGIC
    # ==========================================

    # First Entry (only if no hedge active)
    if pos_price == 0 and good_long_ma_order and not hedge_active:
        trade_counter += 1
        sell_order["comment"] = f"RYU-INIT-{trade_counter}"
        sell = mt5.order_send(sell_order)
        print(f"NEW TRADE CYCLE #{trade_counter}")
    elif pos_price == 0:
        print(f' {symbol} Not Ready')

    # Additional Entry (only if no hedge active)
    if pos_price > 0 and good_long_ma_order and sma6L > pos_price and not hedge_active:
        additional_sell_order["comment"] = f"RYU-AVG-{trade_counter}"
        sell = mt5.order_send(additional_sell_order)
        print(f"AVERAGING DOWN - Trade #{trade_counter}")
        time.sleep(0.01)
        check_sl = mt5.order_send(sltp_request_sell_pos)

    time.sleep(0.1)
