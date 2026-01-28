# Ryuryu's FOREX MT5 EURUSD Bot
# * Longs & Shorts * (Production Mode #6973)
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
import datetime
import ta



# Main settings
magic = 12345678

# Symbol settings
symbol = 'EURUSD'
sl_multiplier = 13
take_profit_short = 21
sl_short = take_profit_short * sl_multiplier

# Long position settings
take_profit_long = 21
sl_long = take_profit_long * sl_multiplier

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

# HTF Regime Detection Settings (H4-led, Daily-anchored)
# State machine: TREND_UP -> WARNING -> TRANSITION -> TREND_DOWN (and reverse)
regime_state = "TREND_UP"  # Current regime state
warning_count = 0  # Warnings before transition
poc_threshold_pct = 0.15  # POC migration threshold (15% of H4 range)
flip_cooldown = 0  # Cooldown counter (in H4 candles)
flip_cooldown_max = 2  # Wait 2 H4 candles after flip before allowing new flip
last_h4_time = 0  # Track last processed H4 candle

# Volume Profile approximation settings
vp_lookback_h4 = 6  # H4 candles for H4 VP calculation
vp_bins = 50  # Price bins for volume profile

# Time filter settings (avoid illiquid periods)
avoid_asia_start = 22  # UTC hour
avoid_asia_end = 2  # UTC hour
avoid_ny_close_start = 20  # UTC hour
avoid_ny_close_end = 21  # UTC hour

# Track hedge state
hedge_active = False
hedge_identifier = 0
short_entry_price = 0
short_volume_at_hedge = 0

# Track long positions
long_pos_price = 0
long_identifier = 0
long_volume = 0

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


def calculate_volume_profile(bars_df):
    """
    Calculate approximate POC and Value Area from price data.
    Uses price frequency as proxy for volume profile.
    Returns: (POC, VAH, VAL)
    """
    if bars_df is None or len(bars_df) == 0:
        return None, None, None

    # Get price range
    price_high = bars_df['high'].max()
    price_low = bars_df['low'].min()
    price_range = price_high - price_low

    if price_range == 0:
        return None, None, None

    # Create price bins
    bin_size = price_range / vp_bins
    bins = [0] * vp_bins

    # Count price touches in each bin (using close prices as proxy)
    for _, row in bars_df.iterrows():
        # Count all prices within candle range
        candle_low = row['low']
        candle_high = row['high']

        for i in range(vp_bins):
            bin_low = price_low + (i * bin_size)
            bin_high = bin_low + bin_size

            # Check if candle overlaps this bin
            if candle_low <= bin_high and candle_high >= bin_low:
                bins[i] += 1

    # Find POC (bin with most activity)
    poc_bin = bins.index(max(bins))
    poc = price_low + (poc_bin * bin_size) + (bin_size / 2)

    # Calculate Value Area (70% of activity)
    total_activity = sum(bins)
    target_activity = total_activity * 0.70

    # Expand from POC until we capture 70%
    va_low_bin = poc_bin
    va_high_bin = poc_bin
    current_activity = bins[poc_bin]

    while current_activity < target_activity:
        expand_low = va_low_bin > 0
        expand_high = va_high_bin < vp_bins - 1

        if expand_low and expand_high:
            # Expand in direction with more activity
            if bins[va_low_bin - 1] >= bins[va_high_bin + 1]:
                va_low_bin -= 1
                current_activity += bins[va_low_bin]
            else:
                va_high_bin += 1
                current_activity += bins[va_high_bin]
        elif expand_low:
            va_low_bin -= 1
            current_activity += bins[va_low_bin]
        elif expand_high:
            va_high_bin += 1
            current_activity += bins[va_high_bin]
        else:
            break

    val = price_low + (va_low_bin * bin_size)
    vah = price_low + ((va_high_bin + 1) * bin_size)

    return poc, vah, val


def get_htf_data():
    """
    Fetch Daily and H4 data for regime detection.
    Returns: (daily_df, h4_df) or None if data unavailable.
    Also updates global poc_daily, vah_daily, val_daily, poc_h4, poc_h4_prev.
    """
    global poc_daily, vah_daily, val_daily, poc_h4, poc_h4_prev, h4_df, daily_df

    # Fetch Daily data (last 5 days for context)
    daily_bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 5)
    if daily_bars is None or len(daily_bars) < 2:
        print("Daily data unavailable")
        return None

    daily_df = pd.DataFrame(daily_bars)

    # Fetch H4 data (last 24 candles = 4 days)
    h4_bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 24)
    if h4_bars is None or len(h4_bars) < 6:
        print("H4 data unavailable")
        return None

    h4_df = pd.DataFrame(h4_bars)
    h4_df['time'] = pd.to_datetime(h4_df['time'], unit='s')

    # Calculate Daily volume profile (yesterday's data for today's levels)
    daily_vp_data = daily_df.iloc[-2:-1]  # Yesterday
    poc_daily, vah_daily, val_daily = calculate_volume_profile(daily_vp_data)

    # Calculate H4 POC (current and previous for migration check)
    h4_current = h4_df.iloc[-vp_lookback_h4:]
    h4_prev = h4_df.iloc[-vp_lookback_h4*2:-vp_lookback_h4]

    poc_h4, _, _ = calculate_volume_profile(h4_current)
    poc_h4_prev, _, _ = calculate_volume_profile(h4_prev)

    return daily_df, h4_df


def is_illiquid_time():
    """Check if current time is in illiquid period (Asia session, NY close)"""
    now = datetime.datetime.utcnow()
    hour = now.hour

    # Asia illiquid (22:00 - 02:00 UTC)
    if hour >= avoid_asia_start or hour < avoid_asia_end:
        return True

    # NY close (20:00 - 21:00 UTC)
    if avoid_ny_close_start <= hour < avoid_ny_close_end:
        return True

    return False


def check_daily_permits_flip():
    """
    Daily answers: Is the market allowed to flip direction?
    Returns True if flip is permitted.

    Daily allows flip if ANY are true:
    - POC flat or lower vs yesterday
    - Daily close inside prior value area
    - Failure at VAH occurred

    Uses cached daily_df from get_htf_data() to avoid duplicate API calls.
    """
    global poc_daily, vah_daily, val_daily, daily_df

    if poc_daily is None or vah_daily is None:
        return False

    if daily_df is None or len(daily_df) < 3:
        return False

    # Calculate POC for day before yesterday (using cached data)
    prev_daily = daily_df.iloc[-3:-2]
    poc_prev, vah_prev, val_prev = calculate_volume_profile(prev_daily)

    if poc_prev is None:
        return False

    yesterday_close = daily_df.iloc[-2]['close']

    # Condition 1: POC flat or lower vs yesterday
    poc_migration = poc_daily <= poc_prev

    # Condition 2: Daily close inside prior value area
    close_in_value = val_prev <= yesterday_close <= vah_prev if val_prev and vah_prev else False

    return poc_migration or close_in_value


def update_regime_state():
    """
    H4-led regime state machine.
    TREND_UP -> WARNING -> TRANSITION -> TREND_DOWN (and reverse)
    Only processes on H4 close.
    """
    global regime_state, warning_count, flip_cooldown, last_h4_time
    global poc_daily, vah_daily, val_daily, poc_h4, poc_h4_prev, h4_df

    # Fetch HTF data
    result = get_htf_data()
    if result is None:
        return regime_state

    daily_df, h4_df = result

    # Check if new H4 candle closed
    current_h4_time = h4_df.iloc[-1]['time']
    if current_h4_time == last_h4_time:
        return regime_state  # No new H4 close

    last_h4_time = current_h4_time

    # Skip illiquid times
    if is_illiquid_time():
        print("Illiquid time - skipping regime check")
        return regime_state

    # Handle cooldown
    if flip_cooldown > 0:
        flip_cooldown -= 1
        print(f"Flip cooldown: {flip_cooldown} H4 candles remaining")
        return regime_state

    # Check if Daily permits flip
    daily_permits = check_daily_permits_flip()

    # Get latest H4 candle data
    h4_latest = h4_df.iloc[-1]
    h4_high = h4_latest['high']
    h4_low = h4_latest['low']
    h4_close = h4_latest['close']

    # Calculate H4 range for POC migration threshold
    h4_range = h4_df.iloc[-6:]['high'].max() - h4_df.iloc[-6:]['low'].min()
    poc_migration_threshold = h4_range * poc_threshold_pct

    print(f"Regime: {regime_state} | Warnings: {warning_count} | Daily permits: {daily_permits}")
    print(f"H4 close: {h4_close:.5f} | POC_D1: {poc_daily:.5f if poc_daily else 0} | VAH_D1: {vah_daily:.5f if vah_daily else 0}")

    # ===== BEARISH FLIP LOGIC (TREND_UP -> TREND_DOWN) =====
    if regime_state in ["TREND_UP", "WARNING"]:

        # STEP A: Failure at VAH (early warning)
        if vah_daily and h4_high > vah_daily and h4_close < vah_daily:
            warning_count += 1
            print(f"VAH FAILURE - Warning count: {warning_count}")

        # STEP B: POC stops migrating (structural)
        if poc_h4 and poc_h4_prev:
            poc_migration = abs(poc_h4 - poc_h4_prev)
            if poc_migration < poc_migration_threshold:
                warning_count += 1
                print(f"POC STALLED - Warning count: {warning_count}")

        # Move to WARNING after 2 warnings
        if warning_count >= 2 and regime_state == "TREND_UP":
            regime_state = "WARNING"
            print("STATE: TREND_UP -> WARNING")

        # STEP C: Close through POC (transition trigger)
        if regime_state == "WARNING" and poc_daily and h4_close < poc_daily:
            regime_state = "TRANSITION"
            print("STATE: WARNING -> TRANSITION")

    # STEP D: Fail to reclaim POC (confirmation)
    if regime_state == "TRANSITION":
        if poc_daily and h4_high >= poc_daily and h4_close < poc_daily:
            regime_state = "TREND_DOWN"
            warning_count = 0
            flip_cooldown = flip_cooldown_max
            print("STATE: TRANSITION -> TREND_DOWN (CONFIRMED)")

    # ===== BULLISH FLIP LOGIC (TREND_DOWN -> TREND_UP) =====
    if regime_state in ["TREND_DOWN", "WARNING_BULL"]:

        # STEP A: Failure at VAL (early warning)
        if val_daily and h4_low < val_daily and h4_close > val_daily:
            warning_count += 1
            print(f"VAL FAILURE - Warning count: {warning_count}")

        # STEP B: POC stops migrating
        if poc_h4 and poc_h4_prev:
            poc_migration = abs(poc_h4 - poc_h4_prev)
            if poc_migration < poc_migration_threshold:
                warning_count += 1
                print(f"POC STALLED (BULL) - Warning count: {warning_count}")

        # Move to WARNING_BULL after 2 warnings
        if warning_count >= 2 and regime_state == "TREND_DOWN":
            regime_state = "WARNING_BULL"
            print("STATE: TREND_DOWN -> WARNING_BULL")

        # STEP C: Close through POC (transition trigger)
        if regime_state == "WARNING_BULL" and poc_daily and h4_close > poc_daily:
            regime_state = "TRANSITION_BULL"
            print("STATE: WARNING_BULL -> TRANSITION_BULL")

    # STEP D: Fail to break back below POC (confirmation)
    if regime_state == "TRANSITION_BULL":
        if poc_daily and h4_low <= poc_daily and h4_close > poc_daily:
            regime_state = "TREND_UP"
            warning_count = 0
            flip_cooldown = flip_cooldown_max
            print("STATE: TRANSITION_BULL -> TREND_UP (CONFIRMED)")

    return regime_state


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
    """Get position data for short, long, and hedge positions"""
    global pos_price, identifier, volume, hedge_active, hedge_identifier
    global short_positions, hedge_positions, long_positions
    global long_pos_price, long_identifier, long_volume

    positions = mt5.positions_get(symbol=symbol)
    short_positions = []
    hedge_positions = []
    long_positions = []

    if positions is None or len(positions) == 0:
        pos_price = 0
        identifier = 0
        volume = 0
        long_pos_price = 0
        long_identifier = 0
        long_volume = 0
        hedge_active = False
        hedge_identifier = 0
        return

    for position in positions:
        post_dict = position._asdict()
        pos_type = post_dict['type']  # 0 = BUY, 1 = SELL
        comment = post_dict.get('comment', '')

        if pos_type == 1:  # SELL (short)
            short_positions.append(post_dict)
            pos_price = post_dict['price_open']
            identifier = post_dict['identifier']
            volume = post_dict['volume']
            print(f"SHORT: {pos_price}, ID: {identifier}, Vol: {volume}")
        elif pos_type == 0:  # BUY
            # Distinguish between long positions and hedge positions by comment
            if 'HEDGE' in comment:
                hedge_positions.append(post_dict)
                hedge_active = True
                hedge_identifier = post_dict['identifier']
                print(f"HEDGE: {post_dict['price_open']}, ID: {hedge_identifier}, Vol: {post_dict['volume']}")
            else:
                long_positions.append(post_dict)
                long_pos_price = post_dict['price_open']
                long_identifier = post_dict['identifier']
                long_volume = post_dict['volume']
                print(f"LONG: {long_pos_price}, ID: {long_identifier}, Vol: {long_volume}")

    if len(short_positions) == 0:
        pos_price = 0
        identifier = 0
        volume = 0

    if len(long_positions) == 0:
        long_pos_price = 0
        long_identifier = 0
        long_volume = 0


def get_total_short_volume():
    """Calculate total volume of all short positions"""
    total = 0
    for pos in short_positions:
        total += pos['volume']
    return total


def get_total_long_volume():
    """Calculate total volume of all long positions"""
    total = 0
    for pos in long_positions:
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


def get_long_pnl():
    """Calculate current P&L of long positions"""
    total_pnl = 0
    for pos in long_positions:
        entry = pos['price_open']
        current = bid  # Use bid for closing longs
        pnl_points = (current - entry) / point
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
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    ask = tick.ask
    bid = tick.bid
    return True

point = mt5.symbol_info(symbol).point
deviation = 20


def build_sell_order(lot_size, comment):
    """Build sell order dict with fresh price"""
    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": mt5.ORDER_TYPE_SELL,
        "price": bid,
        "sl": bid + sl_short * point,
        "tp": bid - take_profit_short * point,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }


def build_buy_order(lot_size, comment):
    """Build buy order dict with fresh price"""
    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": mt5.ORDER_TYPE_BUY,
        "price": ask,
        "sl": ask - sl_long * point,
        "tp": ask + take_profit_long * point,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }


def build_avg_sell_order(lot_size, entry_price, comment):
    """Build averaging sell order with SL/TP based on entry price"""
    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": mt5.ORDER_TYPE_SELL,
        "price": bid,
        "sl": entry_price + sl_short * point,
        "tp": entry_price - take_profit_short * point,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }


def build_avg_buy_order(lot_size, entry_price, comment):
    """Build averaging buy order with SL/TP based on entry price"""
    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": mt5.ORDER_TYPE_BUY,
        "price": ask,
        "sl": entry_price - sl_long * point,
        "tp": entry_price + take_profit_long * point,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }


def build_sltp_sell(pos_id, vol, entry_price):
    """Build SL/TP modification for sell position"""
    return {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "volume": float(vol),
        "type": mt5.ORDER_TYPE_SELL,
        "position": pos_id,
        "sl": entry_price + sl_short * point,
        "tp": entry_price - take_profit_short * point,
        "magic": magic,
        "comment": "Update SL/TP for Sell",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }


def build_sltp_buy(pos_id, vol, entry_price):
    """Build SL/TP modification for buy position"""
    return {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "volume": float(vol),
        "type": mt5.ORDER_TYPE_BUY,
        "position": pos_id,
        "sl": entry_price - sl_long * point,
        "tp": entry_price + take_profit_long * point,
        "magic": magic,
        "comment": "Update SL/TP for Buy",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }


short_positions = []
hedge_positions = []
long_positions = []

# Initialize HTF variables
poc_daily = None
vah_daily = None
val_daily = None
poc_h4 = None
poc_h4_prev = None
h4_df = None
daily_df = None

while True:

    get_sma()
    if not get_ask_bid():
        print("Failed to get tick data, retrying...")
        time.sleep(0.5)
        continue

    get_position_data()

    # Update HTF regime state (H4-led, Daily-anchored)
    update_regime_state()

    # Detect market compression for dynamic lot sizing
    detect_compression()
    current_lot = get_dynamic_lot()
    current_add_lot = get_dynamic_add_lot()

    # MA conditions for entries
    good_short_ma_order = bid > sma6H
    good_long_ma_order = ask < sma6L

    # ==========================================
    # HEDGE LOGIC - Check for recovery first
    # ==========================================
    if hedge_active:
        if check_hedge_recovery():
            print("All positions closed at breakeven")
            time.sleep(0.1)
            continue

    # ==========================================
    # HEDGE TRIGGER - At 3/4 of Stop Loss (for shorts)
    # ==========================================
    if pos_price > 0 and not hedge_active:
        drawdown_points = (ask - pos_price) / point
        hedge_trigger_points = sl_short * hedge_trigger_pct

        if drawdown_points >= hedge_trigger_points:
            print(f"SHORT DRAWDOWN: {drawdown_points:.1f} pts (Trigger: {hedge_trigger_points:.1f})")
            print(f"RSI: {current_rsi:.1f}")

            if is_trending_up():
                print("TRENDING DETECTED - Opening hedge")
                open_hedge()
            else:
                print("RANGING - No hedge needed, expecting reversal")

    # ==========================================
    # REGIME-BASED TRADING LOGIC
    # ==========================================
    print(f"REGIME: {regime_state} | Short: {pos_price:.5f} | Long: {long_pos_price:.5f}")

    # ----- TREND_DOWN: Only SHORT entries -----
    if regime_state == "TREND_DOWN":
        # First Short Entry
        if pos_price == 0 and good_short_ma_order and not hedge_active:
            trade_counter += 1
            get_ask_bid()  # Refresh price before order
            order = build_sell_order(current_lot, f"RYU-SHORT-{trade_counter}")
            result = mt5.order_send(order)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"NEW SHORT #{trade_counter} - Regime: TREND_DOWN")
            else:
                print(f"SHORT FAILED: {result.retcode} - {result.comment}")
                trade_counter -= 1

        # Additional Short Entry (averaging)
        elif pos_price > 0 and good_short_ma_order and sma6L > pos_price and not hedge_active:
            get_ask_bid()  # Refresh price before order
            order = build_avg_sell_order(current_add_lot, pos_price, f"RYU-SAVG-{trade_counter}")
            result = mt5.order_send(order)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"AVERAGING SHORT #{trade_counter}")
                time.sleep(0.01)
                sltp_order = build_sltp_sell(identifier, volume, pos_price)
                sltp_result = mt5.order_send(sltp_order)
                if sltp_result.retcode != mt5.TRADE_RETCODE_DONE:
                    print(f"SLTP UPDATE FAILED: {sltp_result.retcode}")
            else:
                print(f"AVG SHORT FAILED: {result.retcode}")

    # ----- TREND_UP: Only LONG entries -----
    elif regime_state == "TREND_UP":
        # First Long Entry
        if long_pos_price == 0 and good_long_ma_order and not hedge_active:
            trade_counter += 1
            get_ask_bid()  # Refresh price before order
            order = build_buy_order(current_lot, f"RYU-LONG-{trade_counter}")
            result = mt5.order_send(order)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"NEW LONG #{trade_counter} - Regime: TREND_UP")
            else:
                print(f"LONG FAILED: {result.retcode} - {result.comment}")
                trade_counter -= 1

        # Additional Long Entry (averaging)
        elif long_pos_price > 0 and good_long_ma_order and sma6H < long_pos_price and not hedge_active:
            get_ask_bid()  # Refresh price before order
            order = build_avg_buy_order(current_add_lot, long_pos_price, f"RYU-LAVG-{trade_counter}")
            result = mt5.order_send(order)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"AVERAGING LONG #{trade_counter}")
                time.sleep(0.01)
                sltp_order = build_sltp_buy(long_identifier, long_volume, long_pos_price)
                sltp_result = mt5.order_send(sltp_order)
                if sltp_result.retcode != mt5.TRADE_RETCODE_DONE:
                    print(f"SLTP UPDATE FAILED: {sltp_result.retcode}")
            else:
                print(f"AVG LONG FAILED: {result.retcode}")

    # ----- WARNING / TRANSITION: No new entries, manage existing -----
    elif regime_state in ["WARNING", "WARNING_BULL", "TRANSITION", "TRANSITION_BULL"]:
        print(f"TRANSITION STATE - No new entries, managing existing positions")

    # No position and not ready
    if pos_price == 0 and long_pos_price == 0 and regime_state in ["WARNING", "TRANSITION", "WARNING_BULL", "TRANSITION_BULL"]:
        print(f'{symbol} Waiting for regime confirmation...')

    time.sleep(0.1)
