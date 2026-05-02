import ccxt
import time
import telebot
import pandas as pd
import threading
import urllib3
import math
import json
from flask import Flask, jsonify, render_template, request, Response
from datetime import datetime
import requests
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# ================== 🔐 LOAD DATA ENV ==================
load_dotenv("DATA.env")

TOKEN = os.getenv("TOKEN_MACRO")
CHAT_ID = os.getenv("CHAT_ID")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "181268")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

if not TOKEN or not CHAT_ID:
    raise ValueError("❌ Kritis: TOKEN / CHAT_ID belum diset di DATA.env.")

app = Flask(__name__)

# ================== 💰 VIRTUAL ACCOUNT ENGINE ==================
class VirtualAccount:
    """
    Virtual trading account with money management.
    
    Money Management Rules:
    - Default margin: $1000
    - Per trade: 10% of total margin
    - BUT if total margin > $1000 → cap per trade at $100
    - Partial TP: TP1=30%, TP2=40%, TP3=30% of remaining
    - SL to BEP after TP1 hit
    - Trailing stop on anomaly detection against trade direction
    """
    
    DEFAULT_MARGIN = 1000.0  # USD

    def __init__(self):
        self.total_margin = self.DEFAULT_MARGIN
        self.available_margin = self.total_margin
        self.active_trades = {}   # {trade_id: trade_dict}
        self.trade_history = []
        self.trade_counter = 0
        self.lock = threading.Lock()

    def set_margin(self, new_margin: float):
        with self.lock:
            diff = new_margin - self.total_margin
            self.total_margin = new_margin
            self.available_margin = max(0, self.available_margin + diff)

    def get_trade_size(self) -> float:
        """10% of total margin, capped at $100 if total > $1000."""
        if self.total_margin > 1000:
            return 100.0
        return round(self.total_margin * 0.10, 2)

    def open_trade(self, coin: str, direction: str, entry_price: float,
                   sl: float, tp1: float, tp2: float, tp3: float) -> dict | None:
        with self.lock:
            trade_size = self.get_trade_size()
            if self.available_margin < trade_size:
                return None  # Insufficient margin

            # Prevent duplicate open trade for same coin+direction
            for t in self.active_trades.values():
                if t['coin'] == coin and t['direction'] == direction and t['status'] == 'open':
                    return None

            self.trade_counter += 1
            trade_id = f"T{self.trade_counter:04d}"
            trade = {
                'id': trade_id,
                'coin': coin,
                'direction': direction,        # 'LONG' or 'SHORT'
                'entry': entry_price,
                'size_usd': trade_size,
                'qty': trade_size / entry_price,  # units of coin
                'sl': sl,
                'original_sl': sl,
                'tp1': tp1,
                'tp2': tp2,
                'tp3': tp3,
                'status': 'open',
                'tp1_hit': False,
                'tp2_hit': False,
                'tp3_hit': False,
                'bep_active': False,           # SL moved to BEP
                'trailing_active': False,
                'trailing_peak': entry_price,  # highest/lowest price in trade direction
                'realized_pnl': 0.0,
                'remaining_qty_pct': 1.0,      # 100% of position remaining
                'open_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'close_time': None,
            }
            self.available_margin -= trade_size
            self.active_trades[trade_id] = trade
            return trade

    def close_trade_partial(self, trade_id: str, close_pct: float, close_price: float, reason: str) -> float:
        """Close a percentage of position. Returns realized PnL."""
        with self.lock:
            trade = self.active_trades.get(trade_id)
            if not trade or trade['status'] != 'open':
                return 0.0

            qty_to_close = trade['qty'] * trade['remaining_qty_pct'] * close_pct
            entry = trade['entry']
            if trade['direction'] == 'LONG':
                pnl = (close_price - entry) * qty_to_close
            else:
                pnl = (entry - close_price) * qty_to_close

            trade['realized_pnl'] += pnl
            trade['remaining_qty_pct'] *= (1.0 - close_pct)
            self.available_margin += (qty_to_close * entry)  # return margin portion
            self.total_margin += pnl

            if trade['remaining_qty_pct'] < 0.01:
                trade['status'] = 'closed'
                trade['close_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.trade_history.append(trade)
                del self.active_trades[trade_id]

            return pnl

    def close_trade_full(self, trade_id: str, close_price: float, reason: str) -> float:
        """Force close entire position."""
        with self.lock:
            trade = self.active_trades.get(trade_id)
            if not trade or trade['status'] != 'open':
                return 0.0

            qty = trade['qty'] * trade['remaining_qty_pct']
            entry = trade['entry']
            if trade['direction'] == 'LONG':
                pnl = (close_price - entry) * qty
            else:
                pnl = (entry - close_price) * qty

            trade['realized_pnl'] += pnl
            trade['remaining_qty_pct'] = 0
            trade['status'] = 'closed'
            trade['close_reason'] = reason
            trade['close_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            returned_margin = qty * entry
            self.available_margin += returned_margin
            self.total_margin += pnl
            self.trade_history.append(trade.copy())
            del self.active_trades[trade_id]
            return pnl

    def get_summary(self) -> dict:
        total_pnl = sum(t['realized_pnl'] for t in self.trade_history)
        active_count = len(self.active_trades)
        win_trades = [t for t in self.trade_history if t['realized_pnl'] > 0]
        win_rate = (len(win_trades) / len(self.trade_history) * 100) if self.trade_history else 0
        return {
            'total_margin': round(self.total_margin, 2),
            'available_margin': round(self.available_margin, 2),
            'active_trades': active_count,
            'total_closed': len(self.trade_history),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(win_rate, 1),
            'per_trade_size': self.get_trade_size(),
        }


# ================== GLOBAL INIT ==================
G, Y, R, C, W = '\033[92m', '\033[93m', '\033[91m', '\033[96m', '\033[0m'
last_alerts, active_alerts = {}, {}
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

bot = telebot.TeleBot(TOKEN)
exchange = ccxt.indodax({'enableRateLimit': True, 'verify': False})
current_usd_rate = 16200
ALL_IDR_SYMBOLS = []
va = VirtualAccount()   # 🏦 The virtual account instance

# ── Startup progress (dipakai oleh /api/status dan fetch_all_markets) ──
startup_state = {
    'phase': 'INIT',
    'progress': 0,
    'total': 0,
    'scanned': 0,
    'watchlist': 0,
    'ready': False,
}
# ── Margin config dari web ──
margin_config = {'pct': 10}

# ================== 🤖 AI CHAT CONFIG ==================
ANTHROPIC_API_URL  = "https://api.anthropic.com/v1/messages"
AI_MODEL           = "claude-sonnet-4-20250514"
AI_MAX_HISTORY     = 10   # pesan per user yg disimpan
conversation_history: dict = {}   # { chat_id: [ {role, content}, ... ] }


# ================== 🔐 WEB AUTH ==================
def check_auth(username, password):
    return username == "admin" and password == WEB_PASSWORD

def authenticate():
    return Response(
        'Akses ditolak!', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})


# ================== 🧠 MARKET ANALYSIS ENGINE ==================
def fetch_all_markets():
    global ALL_IDR_SYMBOLS
    try:
        startup_state['phase'] = 'LOADING_OHLCV'
        markets = exchange.load_markets()
        ALL_IDR_SYMBOLS = [s for s in markets if s.endswith('/IDR')]
        startup_state['total']     = len(ALL_IDR_SYMBOLS)
        startup_state['watchlist'] = len(ALL_IDR_SYMBOLS)
        startup_state['phase']     = 'SCANNING'
        print(f"✅ Intelligence Engine Ready: {len(ALL_IDR_SYMBOLS)} Assets Scanned.")
    except Exception as e:
        print(f"❌ Error fetch markets: {e}")


def get_market_analysis(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=100)
        if not ohlcv or len(ohlcv) < 20:
            return None
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])

        # RSI
        df['sma_20'] = df['close'].rolling(window=20).mean()
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))

        # MACD
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']

        # Bollinger Bands
        df['bb_mid'] = df['close'].rolling(20).mean()
        df['bb_std'] = df['close'].rolling(20).std()
        df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
        df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']

        # ATR (for SL calculation)
        df['tr'] = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low'] - df['close'].shift()).abs()
        ], axis=1).max(axis=1)
        df['atr'] = df['tr'].rolling(14).mean()

        # MPI & Vol
        green_vol = df[df['close'] > df['open']]['vol'].sum()
        red_vol = df[df['close'] < df['open']]['vol'].sum()
        mpi = (green_vol / (green_vol + red_vol)) * 100 if (green_vol + red_vol) > 0 else 50

        last = df.iloc[-1]
        prev = df.iloc[-2]
        df['vol_avg'] = df['vol'].rolling(window=20).mean()
        vol_spike_ratio = last['vol'] / df['vol_avg'].iloc[-1] if df['vol_avg'].iloc[-1] > 0 else 0

        # Anomaly: sudden spike AGAINST trade direction (for trailing stop trigger)
        # Defined as: 3 consecutive candles reversing + vol spike > 2x
        last3 = df.tail(3)
        bearish_reversal = all(last3['close'].diff().dropna() < 0) and vol_spike_ratio > 2.0
        bullish_reversal = all(last3['close'].diff().dropna() > 0) and vol_spike_ratio > 2.0
        anomaly_bearish = bearish_reversal   # bad for LONG
        anomaly_bullish = bullish_reversal   # bad for SHORT

        # Signal
        signal = "⚖️ NEUTRAL"
        direction = "NONE"
        if last['rsi'] < 35 and last['macd_hist'] > prev['macd_hist']:
            signal = "🚀 STRONG ACCUMULATION"
            direction = "LONG"
        elif last['rsi'] > 65 and last['macd_hist'] < prev['macd_hist']:
            signal = "🔴 DISTRIBUTION / SELL"
            direction = "SHORT"

        curr_p = last['close']
        atr = last['atr']

        # ATR-based SL & TP
        atr_multiplier_sl = 1.5
        atr_multiplier_tp = [1.0, 2.0, 3.5]  # TP1, TP2, TP3

        if direction == "LONG":
            sl = curr_p - atr * atr_multiplier_sl
            tp1_raw = curr_p + atr * atr_multiplier_tp[0]
            tp2_raw = curr_p + atr * atr_multiplier_tp[1]
            tp3_raw = curr_p + atr * atr_multiplier_tp[2]
        elif direction == "SHORT":
            sl = curr_p + atr * atr_multiplier_sl
            tp1_raw = curr_p - atr * atr_multiplier_tp[0]
            tp2_raw = curr_p - atr * atr_multiplier_tp[1]
            tp3_raw = curr_p - atr * atr_multiplier_tp[2]
        else:
            sl = tp1_raw = tp2_raw = tp3_raw = curr_p

        # Grade
        grade = "C (LOW)"
        if "ACCUMULATION" in signal and mpi > 65 and vol_spike_ratio > 1.5:
            grade = "A+ (PERFECT)"
        elif "DISTRIBUTION" in signal and mpi < 35 and vol_spike_ratio > 1.5:
            grade = "A+ (PERFECT)"
        elif (mpi > 65 or mpi < 35) and vol_spike_ratio <= 1.5:
            grade = "B (EARLY)"

        def to_usd(idr): return (idr / current_usd_rate) * 0.95

        return {
            'price_usd': to_usd(curr_p),
            'price_idr': curr_p,
            'sl_usd': to_usd(sl),
            'sl_idr': sl,
            'tp1_usd': to_usd(tp1_raw), 'tp1_idr': tp1_raw,
            'tp2_usd': to_usd(tp2_raw), 'tp2_idr': tp2_raw,
            'tp3_usd': to_usd(tp3_raw), 'tp3_idr': tp3_raw,
            'atr': atr,
            'rsi': last['rsi'], 'mpi': mpi,
            'signal': signal, 'direction': direction,
            'vol_spike': vol_spike_ratio, 'grade': grade,
            'anomaly_bearish': anomaly_bearish,
            'anomaly_bullish': anomaly_bullish,
            'bb_upper': last['bb_upper'], 'bb_lower': last['bb_lower'],
        }
    except Exception as e:
        print(f"⚠️ Error analysis {symbol}: {e}")
        return None


# ================== 🤖 AUTO TRADE EXECUTOR ==================
# Partial TP ratios (% of remaining position to close at each TP)
PARTIAL_TP1_PCT = 0.30   # Close 30% at TP1
PARTIAL_TP2_PCT = 0.40   # Close 40% at TP2 (of remaining 70%)
PARTIAL_TP3_PCT = 1.00   # Close 100% of remainder at TP3

# Trailing stop: lock in profit if price reverses X% from peak
TRAILING_STOP_OFFSET_PCT = 0.005   # 0.5% from trailing peak (tighten after TP1)


def execute_auto_trade(coin: str, data: dict):
    """Open a virtual trade when A+ signal fires."""
    direction = data['direction']
    if direction == "NONE":
        return

    trade = va.open_trade(
        coin=coin,
        direction=direction,
        entry_price=data['price_usd'],
        sl=data['sl_usd'],
        tp1=data['tp1_usd'],
        tp2=data['tp2_usd'],
        tp3=data['tp3_usd'],
    )
    if not trade:
        return  # Margin insufficient or duplicate

    size = va.get_trade_size()
    summary = va.get_summary()

    msg = (
        f"🤖 **AUTO TRADE EXECUTED** 🤖\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID: `{trade['id']}`\n"
        f"🪙 Asset: `{coin}` | Dir: `{direction}`\n"
        f"💵 Entry: `${data['price_usd']:.8f}`\n"
        f"📦 Size: `${size:.2f}` USD\n"
        f"─────────────────────\n"
        f"🛡️ SL: `${data['sl_usd']:.8f}`\n"
        f"🎯 TP1 (30%): `${data['tp1_usd']:.8f}`\n"
        f"🚀 TP2 (40%): `${data['tp2_usd']:.8f}`\n"
        f"🌌 TP3 (30%): `${data['tp3_usd']:.8f}`\n"
        f"─────────────────────\n"
        f"💰 Balance: `${summary['total_margin']:.2f}` | "
        f"Free: `${summary['available_margin']:.2f}`\n"
        f"📊 Active Trades: `{summary['active_trades']}`\n"
        f"📈 Win Rate: `{summary['win_rate']}%`"
    )
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📊 Chart", url=f"https://indodax.com/market/{coin}IDR"),
        InlineKeyboardButton("❌ Close Trade", callback_data=f"close_{trade['id']}")
    )
    bot.send_message(CHAT_ID, msg, parse_mode='Markdown', reply_markup=markup)


def monitor_active_trades():
    """
    Continuously monitors all active virtual trades.
    Handles: SL, TP1/TP2/TP3 partial close, BEP after TP1, trailing stop on anomaly.
    """
    while True:
        try:
            trades_snapshot = list(va.active_trades.values())
            for trade in trades_snapshot:
                trade_id = trade['id']
                coin = trade['coin']
                symbol = f"{coin}/IDR"

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    curr_price_idr = ticker['last']
                    curr_price = (curr_price_idr / current_usd_rate) * 0.95
                except Exception:
                    continue

                direction = trade['direction']
                entry = trade['entry']

                # ── Update trailing peak ──
                if direction == "LONG":
                    if curr_price > trade.get('trailing_peak', entry):
                        trade['trailing_peak'] = curr_price
                else:
                    if curr_price < trade.get('trailing_peak', entry):
                        trade['trailing_peak'] = curr_price

                # ── Check Anomaly for Trailing Stop Trigger ──
                # Only activate after TP1 is hit (we're in profit territory)
                anomaly_triggered = False
                if trade['tp1_hit']:
                    try:
                        analysis = get_market_analysis(symbol)
                        if analysis:
                            if direction == "LONG" and analysis['anomaly_bearish']:
                                anomaly_triggered = True
                            elif direction == "SHORT" and analysis['anomaly_bullish']:
                                anomaly_triggered = True
                    except Exception:
                        pass

                # ── Trailing Stop on Anomaly ──
                if anomaly_triggered and not trade.get('trailing_active'):
                    trade['trailing_active'] = True
                    # Set trailing SL to current price minus offset (LONG) or plus offset (SHORT)
                    if direction == "LONG":
                        new_sl = curr_price * (1 - TRAILING_STOP_OFFSET_PCT)
                        trade['sl'] = max(trade['sl'], new_sl)   # only move up
                    else:
                        new_sl = curr_price * (1 + TRAILING_STOP_OFFSET_PCT)
                        trade['sl'] = min(trade['sl'], new_sl)   # only move down

                    msg = (
                        f"⚡ **TRAILING STOP ACTIVATED**\n"
                        f"🆔 `{trade_id}` | {coin} {direction}\n"
                        f"⚠️ Anomaly detected against trade!\n"
                        f"🔒 New SL: `${trade['sl']:.8f}`\n"
                        f"📍 Current: `${curr_price:.8f}`"
                    )
                    bot.send_message(CHAT_ID, msg, parse_mode='Markdown')

                # Update trailing SL dynamically (tighten as price moves favorably)
                if trade.get('trailing_active'):
                    if direction == "LONG":
                        new_sl = trade['trailing_peak'] * (1 - TRAILING_STOP_OFFSET_PCT)
                        if new_sl > trade['sl']:
                            trade['sl'] = new_sl
                    else:
                        new_sl = trade['trailing_peak'] * (1 + TRAILING_STOP_OFFSET_PCT)
                        if new_sl < trade['sl']:
                            trade['sl'] = new_sl

                # ── SL Hit ──
                sl_hit = (direction == "LONG" and curr_price <= trade['sl']) or \
                         (direction == "SHORT" and curr_price >= trade['sl'])

                if sl_hit:
                    pnl = va.close_trade_full(trade_id, curr_price, "SL")
                    reason_label = "🛑 STOP LOSS" if not trade['tp1_hit'] else "🔒 TRAILING/BEP STOP"
                    _send_close_notification(trade, curr_price, pnl, reason_label)
                    continue

                # ── TP1 Hit ──
                if not trade['tp1_hit']:
                    tp1_hit = (direction == "LONG" and curr_price >= trade['tp1']) or \
                               (direction == "SHORT" and curr_price <= trade['tp1'])
                    if tp1_hit:
                        pnl = va.close_trade_partial(trade_id, PARTIAL_TP1_PCT, curr_price, "TP1")
                        trade['tp1_hit'] = True
                        # Move SL to BEP (entry price)
                        trade['sl'] = entry
                        trade['bep_active'] = True
                        msg = (
                            f"🎯 **TP1 HIT — 30% CLOSED**\n"
                            f"🆔 `{trade_id}` | {coin}\n"
                            f"💵 Close: `${curr_price:.8f}`\n"
                            f"💰 PnL: `${pnl:.4f}`\n"
                            f"🔒 SL moved to BEP: `${entry:.8f}`\n"
                            f"📦 Remaining: 70% of position"
                        )
                        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
                        continue

                # ── TP2 Hit ──
                if trade['tp1_hit'] and not trade['tp2_hit']:
                    tp2_hit = (direction == "LONG" and curr_price >= trade['tp2']) or \
                               (direction == "SHORT" and curr_price <= trade['tp2'])
                    if tp2_hit:
                        pnl = va.close_trade_partial(trade_id, PARTIAL_TP2_PCT, curr_price, "TP2")
                        trade['tp2_hit'] = True
                        msg = (
                            f"🚀 **TP2 HIT — 40% CLOSED**\n"
                            f"🆔 `{trade_id}` | {coin}\n"
                            f"💵 Close: `${curr_price:.8f}`\n"
                            f"💰 PnL: `${pnl:.4f}`\n"
                            f"📦 Remaining: 30% — letting it run to TP3\n"
                            f"⚡ Trailing stop now active"
                        )
                        # Auto-activate trailing after TP2
                        trade['trailing_active'] = True
                        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
                        continue

                # ── TP3 Hit ──
                if trade['tp2_hit'] and not trade['tp3_hit']:
                    tp3_hit = (direction == "LONG" and curr_price >= trade['tp3']) or \
                               (direction == "SHORT" and curr_price <= trade['tp3'])
                    if tp3_hit:
                        pnl = va.close_trade_full(trade_id, curr_price, "TP3")
                        trade['tp3_hit'] = True
                        msg = (
                            f"🌌 **TP3 HIT — 100% POSITION CLOSED**\n"
                            f"🆔 `{trade_id}` | {coin}\n"
                            f"💵 Close: `${curr_price:.8f}`\n"
                            f"💰 PnL this leg: `${pnl:.4f}`\n"
                            f"🏆 FULL TARGET ACHIEVED!"
                        )
                        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')

        except Exception as e:
            print(f"⚠️ Monitor error: {e}")

        time.sleep(15)  # Check every 15 seconds


def _send_close_notification(trade: dict, close_price: float, pnl: float, reason: str):
    emoji = "✅" if pnl >= 0 else "❌"
    msg = (
        f"{emoji} **TRADE CLOSED — {reason}**\n"
        f"🆔 `{trade['id']}` | {trade['coin']} {trade['direction']}\n"
        f"📍 Entry: `${trade['entry']:.8f}`\n"
        f"📍 Close: `${close_price:.8f}`\n"
        f"💰 PnL: `${pnl:.4f}`\n"
        f"─────────────────\n"
        f"💼 Balance: `${va.total_margin:.2f}`"
    )
    bot.send_message(CHAT_ID, msg, parse_mode='Markdown')


# ================== 🐋 SCANNER ENGINE ==================
def whale_and_anomaly_detector():
    while True:
        scanned_count = 0
        for symbol in ALL_IDR_SYMBOLS:
            try:
                data = get_market_analysis(symbol)
                scanned_count += 1
                startup_state['progress'] = scanned_count
                startup_state['scanned']  = scanned_count
                if scanned_count >= 5:
                    startup_state['ready'] = True
                    startup_state['phase'] = 'READY'

                if data is None:
                    continue

                coin_name = symbol.split('/')[0]
                time_now = datetime.now().strftime('%H:%M:%S')
                data['time'] = time_now
                active_alerts[coin_name] = data

                if data['grade'] == "A+ (PERFECT)":
                    if coin_name not in last_alerts or last_alerts[coin_name] != data['signal']:
                        # Send alert
                        msg = (
                            f"🌟 **INTELLIGENCE ALERT** 🌟\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 Asset: `{coin_name}`\n"
                            f"🏆 Grade: **{data['grade']}** 🔥\n"
                            f"📢 Signal: **{data['signal']}**\n"
                            f"💵 Entry: `${data['price_usd']:.8f}`\n"
                            f"🛡️ SL: `${data['sl_usd']:.8f}`\n"
                            f"🎯 TP1: `${data['tp1_usd']:.8f}` (ATR-based)\n"
                            f"🚀 TP2: `${data['tp2_usd']:.8f}`\n"
                            f"🌌 TP3: `${data['tp3_usd']:.8f}`\n"
                            f"🐳 Power: `{data['mpi']:.1f}%` | ⚡ Vol: `{data['vol_spike']:.1f}x`"
                        )
                        markup = InlineKeyboardMarkup()
                        markup.add(
                            InlineKeyboardButton("📊 Chart", url=f"https://indodax.com/market/{coin_name}IDR"),
                        )
                        bot.send_message(CHAT_ID, msg, parse_mode='Markdown', reply_markup=markup)
                        last_alerts[coin_name] = data['signal']

                        # ✅ AUTO EXECUTE TRADE
                        execute_auto_trade(coin_name, data)

                time.sleep(1)
            except Exception:
                continue
        time.sleep(30)


# ================== 🤖 AI CHAT ENGINE ==================

def build_ai_system_prompt() -> str:
    """Bangun system prompt real-time dari data bot saat ini."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    s   = va.get_summary()

    # ── Account ──
    account_ctx = (
        f"=== VIRTUAL ACCOUNT ===\n"
        f"Total Margin : ${s['total_margin']:.2f}\n"
        f"Available    : ${s['available_margin']:.2f}\n"
        f"Per Trade    : ${s['per_trade_size']:.2f}\n"
        f"Active Trades: {s['active_trades']}\n"
        f"Total Closed : {s['total_closed']}\n"
        f"Win Rate     : {s['win_rate']}%\n"
        f"Total PnL    : ${s['total_pnl']:.4f}\n"
    )

    # ── Running Trades ──
    if va.active_trades:
        trades_ctx = "=== RUNNING TRADES ===\n"
        for t in va.active_trades.values():
            curr = active_alerts.get(t['coin'], {}).get('price_usd', t['entry'])
            qty  = t['qty'] * t['remaining_qty_pct']
            upnl = (curr - t['entry']) * qty if t['direction'] == 'LONG' else (t['entry'] - curr) * qty
            trades_ctx += (
                f"[{t['id']}] {t['coin']} {t['direction']} | "
                f"Entry:${t['entry']:.6f} Now:${curr:.6f} uPnL:${upnl:.4f} | "
                f"SL:${t['sl']:.6f} TP1:${t['tp1']:.6f} | "
                f"TP1hit:{t['tp1_hit']} BEP:{t['bep_active']} "
                f"Trailing:{t.get('trailing_active',False)} | "
                f"Sisa:{t['remaining_qty_pct']*100:.0f}%\n"
            )
    else:
        trades_ctx = "=== RUNNING TRADES ===\nTidak ada trade aktif.\n"

    # ── History ──
    if va.trade_history:
        hist_ctx = "=== HISTORY TRADE (5 TERAKHIR) ===\n"
        for t in list(reversed(va.trade_history))[:5]:
            emoji = "✅" if t['realized_pnl'] >= 0 else "❌"
            hist_ctx += (
                f"{emoji} [{t['id']}] {t['coin']} {t['direction']} | "
                f"PnL:${t['realized_pnl']:.4f} | "
                f"Close:{t.get('close_reason','N/A')} | {t.get('close_time','N/A')}\n"
            )
    else:
        hist_ctx = "=== HISTORY TRADE ===\nBelum ada trade selesai.\n"

    # ── Market ──
    if active_alerts:
        a_plus = sum(1 for d in active_alerts.values() if 'A+' in d.get('grade',''))
        longs  = sum(1 for d in active_alerts.values() if d.get('direction') == 'LONG')
        shorts = sum(1 for d in active_alerts.values() if d.get('direction') == 'SHORT')
        top10  = sorted(active_alerts.items(),
                        key=lambda x: abs(x[1].get('mpi',50)-50) + x[1].get('vol_spike',0),
                        reverse=True)[:10]
        market_ctx = (
            f"=== MARKET (TOP 10 SINYAL) ===\n"
            f"Dipantau:{len(active_alerts)} | A+:{a_plus} | LONG:{longs} | SHORT:{shorts}\n"
        )
        for coin, d in top10:
            market_ctx += (
                f"{coin}: {d.get('signal','?')} | Grade:{d.get('grade','?')} | "
                f"${d.get('price_usd',0):.6f} | RSI:{d.get('rsi',0):.1f} | "
                f"MPI:{d.get('mpi',0):.1f}% | Vol:{d.get('vol_spike',0):.1f}x\n"
            )
    else:
        market_ctx = "=== MARKET ===\nMesin warming up, belum ada data.\n"

    status_ctx = (
        f"=== STATUS MESIN ===\n"
        f"Waktu:{now} | Aset dipantau:{len(active_alerts)} | "
        f"Exchange:Indodax | USD Rate:Rp{current_usd_rate:,.0f}\n"
    )

    return (
        "Kamu adalah asisten trading AI terintegrasi langsung dengan bot trading crypto.\n"
        "Kamu punya akses data real-time berikut:\n\n"
        f"{account_ctx}\n{trades_ctx}\n{hist_ctx}\n{market_ctx}\n{status_ctx}\n"
        "ATURAN JAWAB:\n"
        "- Singkat, padat, pakai angka real dari data di atas\n"
        "- Jangan mengarang data yang tidak ada di konteks\n"
        "- Kalau koin tidak ada di data, bilang tidak ada sinyal aktif\n"
        "- Jawab Bahasa Indonesia, boleh pakai emoji\n"
        "- Untuk analisa teknikal gunakan RSI/MPI/Vol Spike yang tersedia\n"
    )


def ai_ask(chat_id: int, user_message: str) -> str:
    """Kirim pesan ke Anthropic API dengan conversation history."""
    if not ANTHROPIC_API_KEY:
        return "❌ ANTHROPIC_API_KEY belum diset di DATA.env"

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({"role": "user", "content": user_message})

    # Trim history
    if len(conversation_history[chat_id]) > AI_MAX_HISTORY * 2:
        conversation_history[chat_id] = conversation_history[chat_id][-(AI_MAX_HISTORY * 2):]

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key":          ANTHROPIC_API_KEY,
                "anthropic-version":  "2023-06-01",
                "content-type":       "application/json",
            },
            json={
                "model":      AI_MODEL,
                "max_tokens": 1024,
                "system":     build_ai_system_prompt(),
                "messages":   conversation_history[chat_id],
            },
            timeout=30
        )
        resp.raise_for_status()
        reply = resp.json()['content'][0]['text']
        conversation_history[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except requests.exceptions.Timeout:
        return "⏱️ AI timeout, coba lagi."
    except requests.exceptions.HTTPError as e:
        return f"❌ API error: {e.response.status_code}"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def _send_ai_reply(m, text: str):
    """Helper: kirim typing action lalu jawaban AI."""
    bot.send_chat_action(m.chat.id, 'typing')
    reply = ai_ask(m.chat.id, text)
    try:
        bot.reply_to(m, reply, parse_mode='Markdown')
    except Exception:
        bot.reply_to(m, reply)


# ================== 💬 BOT COMMANDS ==================
@bot.message_handler(commands=['cek'])
def cmd_cek(m):
    try:
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "Gunakan: `/cek btc`")
            return
        coin = parts[1].upper().replace("IDR", "")
        analysis = get_market_analysis(f"{coin}/IDR")
        if analysis:
            res = (
                f"🧠 **ANALYSIS: {coin}**\n"
                f"🏆 Grade: **{analysis['grade']}**\n"
                f"📢 Signal: **{analysis['signal']}**\n"
                f"💵 Price: `${analysis['price_usd']:.8f}`\n"
                f"🛡️ SL: `${analysis['sl_usd']:.8f}`\n"
                f"🎯 TP1: `${analysis['tp1_usd']:.8f}`\n"
                f"📊 RSI: `{analysis['rsi']:.2f}` | ATR: `{analysis['atr']:.2f}`\n"
                f"🐳 Power: `{analysis['mpi']:.1f}%`"
            )
            bot.send_message(m.chat.id, res, parse_mode='Markdown')
        else:
            bot.reply_to(m, f"❌ Data `{coin}` tidak ditemukan.")
    except Exception as e:
        bot.reply_to(m, f"⚠️ Error: {str(e)}")


@bot.message_handler(commands=['akun'])
def cmd_akun(m):
    """Show virtual account summary."""
    s = va.get_summary()
    msg = (
        f"💼 **VIRTUAL ACCOUNT**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Total Margin: `${s['total_margin']}`\n"
        f"🟢 Available: `${s['available_margin']}`\n"
        f"📦 Per Trade Size: `${s['per_trade_size']}`\n"
        f"─────────────────────\n"
        f"🔄 Active Trades: `{s['active_trades']}`\n"
        f"✅ Total Closed: `{s['total_closed']}`\n"
        f"📈 Win Rate: `{s['win_rate']}%`\n"
        f"💹 Total PnL: `${s['total_pnl']}`"
    )
    bot.send_message(m.chat.id, msg, parse_mode='Markdown')


@bot.message_handler(commands=['setmargin'])
def cmd_set_margin(m):
    """Set custom margin. Usage: /setmargin 5000"""
    try:
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "Gunakan: `/setmargin 5000`")
            return
        new_margin = float(parts[1])
        if new_margin < 10:
            bot.reply_to(m, "❌ Minimum margin $10.")
            return
        old_margin = va.total_margin
        va.set_margin(new_margin)
        per_trade = va.get_trade_size()
        msg = (
            f"✅ **Margin Updated**\n"
            f"Old: `${old_margin:.2f}` → New: `${new_margin:.2f}`\n"
            f"📦 Per Trade: `${per_trade:.2f}` "
            f"({'$100 cap' if new_margin > 1000 else '10% of margin'})"
        )
        bot.send_message(m.chat.id, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"⚠️ Error: {str(e)}")


@bot.message_handler(commands=['trades'])
def cmd_trades(m):
    """List active trades."""
    trades = list(va.active_trades.values())
    if not trades:
        bot.reply_to(m, "📭 Tidak ada trade aktif.")
        return
    msg = "📋 **ACTIVE TRADES**\n━━━━━━━━━━━━━━━━━━━━\n"
    for t in trades:
        pct_remaining = t['remaining_qty_pct'] * 100
        msg += (
            f"🆔 `{t['id']}` | {t['coin']} `{t['direction']}`\n"
            f"   Entry: `${t['entry']:.8f}` | SL: `${t['sl']:.8f}`\n"
            f"   TP1✓: {t['tp1_hit']} | TP2✓: {t['tp2_hit']} | BEP: {t['bep_active']}\n"
            f"   Remaining: `{pct_remaining:.0f}%` of position\n"
            f"─────────────────────\n"
        )
    bot.send_message(m.chat.id, msg, parse_mode='Markdown')


@bot.message_handler(commands=['closetrade'])
def cmd_close_trade(m):
    """Manually close a trade. Usage: /closetrade T0001"""
    try:
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "Gunakan: `/closetrade T0001`")
            return
        trade_id = parts[1].upper()
        trade = va.active_trades.get(trade_id)
        if not trade:
            bot.reply_to(m, f"❌ Trade `{trade_id}` tidak ditemukan.")
            return
        coin = trade['coin']
        ticker = exchange.fetch_ticker(f"{coin}/IDR")
        curr_price = (ticker['last'] / current_usd_rate) * 0.95
        pnl = va.close_trade_full(trade_id, curr_price, "MANUAL")
        bot.reply_to(m, f"✅ Trade `{trade_id}` closed.\n💰 PnL: `${pnl:.4f}`", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"⚠️ Error: {str(e)}")


@bot.message_handler(commands=['history'])
def cmd_history(m):
    """Show last 5 closed trades."""
    history = va.trade_history[-5:]
    if not history:
        bot.reply_to(m, "📭 Belum ada trade selesai.")
        return
    msg = "📜 **LAST 5 TRADES**\n━━━━━━━━━━━━━━━━━━━━\n"
    for t in reversed(history):
        emoji = "✅" if t['realized_pnl'] >= 0 else "❌"
        msg += (
            f"{emoji} `{t['id']}` | {t['coin']} `{t['direction']}`\n"
            f"   Entry: `${t['entry']:.8f}`\n"
            f"   PnL: `${t['realized_pnl']:.4f}`\n"
            f"─────────────────────\n"
        )
    bot.send_message(m.chat.id, msg, parse_mode='Markdown')


# ================== 🤖 AI INTERACTIVE COMMANDS ==================

@bot.message_handler(commands=['ai'])
def cmd_ai(m):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m,
            "💬 *AI Trading Assistant*\n\n"
            "Kirim pertanyaan bebas:\n"
            "`/ai cek btc`\n"
            "`/ai status mesin`\n"
            "`/ai portofolio gw gimana?`\n"
            "`/ai analisa pasar sekarang`\n\n"
            "Atau pakai shortcut:\n"
            "`/portofolio` `/running` `/mesin` `/pasar`\n"
            "`/cekAI [koin]` `/reset_chat`",
            parse_mode='Markdown'
        )
        return
    _send_ai_reply(m, parts[1])


@bot.message_handler(commands=['portofolio'])
def cmd_portofolio(m):
    _send_ai_reply(m, "Tampilkan ringkasan lengkap portofolio virtual account saya: equity, margin tersedia, unrealized PnL, dan semua posisi aktif.")


@bot.message_handler(commands=['running'])
def cmd_running(m):
    _send_ai_reply(m, "Tampilkan semua running trade aktif: entry, harga sekarang, unrealized PnL, status SL/TP/BEP/trailing, dan sisa posisi.")


@bot.message_handler(commands=['mesin'])
def cmd_mesin(m):
    _send_ai_reply(m, "Cek status mesin scanner: berapa aset dipantau, berapa sinyal A+, perbandingan LONG vs SHORT, dan kondisi umum pasar.")


@bot.message_handler(commands=['pasar'])
def cmd_pasar(m):
    _send_ai_reply(m, "Analisa kondisi pasar secara keseluruhan dari data sinyal yang ada. Sentiment bullish atau bearish? Koin mana yang paling menarik?")


@bot.message_handler(commands=['cekAI'])
def cmd_cek_ai(m):
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Gunakan: `/cekAI btc`", parse_mode='Markdown')
        return
    coin = parts[1].upper().replace("IDR", "").replace("USDT", "")
    _send_ai_reply(m, f"Analisa lengkap untuk koin {coin}: sinyal aktif, RSI, MPI, volume spike, grade, level SL/TP, dan apakah layak entry sekarang.")


@bot.message_handler(commands=['reset_chat'])
def cmd_reset_chat(m):
    conversation_history.pop(m.chat.id, None)
    bot.reply_to(m, "🗑️ History percakapan AI dihapus. Mulai sesi baru.")


@bot.message_handler(commands=['help'])
def cmd_help(m):
    msg = (
        "📋 *SEMUA COMMAND*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Trading & Akun:*\n"
        "`/cek [koin]` — Analisa teknikal\n"
        "`/akun` — Summary virtual account\n"
        "`/setmargin [angka]` — Set custom margin\n"
        "`/trades` — Trade aktif\n"
        "`/closetrade [ID]` — Tutup trade manual\n"
        "`/history` — 5 trade terakhir\n\n"
        "*AI Assistant:*\n"
        "`/ai [pertanyaan]` — Chat bebas dengan AI\n"
        "`/cekAI [koin]` — Analisa AI untuk koin\n"
        "`/portofolio` — Ringkasan akun & posisi\n"
        "`/running` — Status semua trade aktif\n"
        "`/mesin` — Status scanner & pasar\n"
        "`/pasar` — Analisa sentimen pasar\n"
        "`/reset_chat` — Hapus history AI\n\n"
        "💡 Bisa juga chat bebas tanpa command!"
    )
    bot.send_message(m.chat.id, msg, parse_mode='Markdown')


@bot.message_handler(func=lambda msg: msg.text and not msg.text.startswith('/'))
def handle_free_chat(m):
    """Auto-reply pesan bebas yang mengandung kata kunci trading."""
    keywords = [
        'cek','btc','eth','sol','bnb','xrp','doge','ada',
        'trade','profit','rugi','pnl','portofolio','saldo',
        'beli','jual','long','short','entry','exit',
        'sinyal','signal','analisa','analisis',
        'rsi','mpi','volume','pasar','market',
        'running','history','mesin','scanner',
        'sl','tp','stop','trailing',
        'gimana','bagaimana','kapan','kenapa','berapa',
    ]
    text_lower = m.text.lower()
    has_keyword = any(k in text_lower for k in keywords)
    has_history = m.chat.id in conversation_history and len(conversation_history[m.chat.id]) > 0

    if has_keyword or has_history:
        _send_ai_reply(m, m.text)


@bot.callback_query_handler(func=lambda call: call.data.startswith("close_"))
def handle_close_callback(call):
    trade_id = call.data.replace("close_", "")
    trade = va.active_trades.get(trade_id)
    if not trade:
        bot.answer_callback_query(call.id, "Trade sudah tertutup.")
        return
    coin = trade['coin']
    try:
        ticker = exchange.fetch_ticker(f"{coin}/IDR")
        curr_price = (ticker['last'] / current_usd_rate) * 0.95
        pnl = va.close_trade_full(trade_id, curr_price, "MANUAL_BUTTON")
        bot.answer_callback_query(call.id, f"✅ Closed! PnL: ${pnl:.4f}")
        bot.send_message(call.message.chat.id, f"✅ Trade `{trade_id}` closed.\n💰 PnL: `${pnl:.4f}`", parse_mode='Markdown')
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}")


# ================== 🌐 WEB API ==================

@app.route('/')
def index():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    return render_template('index.html')


# ── /api/status — untuk progress bar startup HTML ──
@app.route('/api/status')
def api_status():
    return jsonify(startup_state)


# ── /api/market — data sinyal untuk feed cards HTML ──
@app.route('/api/market')
def api_market():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401

    data = []
    longs = shorts = a_plus = 0

    for coin, info in active_alerts.items():
        direction = info.get('direction', 'NONE')
        grade_raw = info.get('grade', 'C (LOW)')

        # Normalize grade label untuk HTML: "A+ (PERFECT)" → "A+", "B (EARLY)" → "B", else "C"
        if 'A+' in grade_raw or 'PERFECT' in grade_raw:
            grade = 'A+'
            a_plus += 1
        elif 'B' in grade_raw or 'EARLY' in grade_raw:
            grade = 'B'
        else:
            grade = 'C'

        if direction == 'LONG':   longs += 1
        elif direction == 'SHORT': shorts += 1

        # Hitung aggregate score dari RSI & MPI (0–100)
        rsi = info.get('rsi', 50)
        mpi = info.get('mpi', 50)
        vol = info.get('vol_spike', 1)
        if direction == 'LONG':
            score = max(0, min(100, (100 - rsi) * 0.4 + mpi * 0.4 + min(vol * 5, 20)))
        elif direction == 'SHORT':
            score = max(0, min(100, rsi * 0.4 + (100 - mpi) * 0.4 + min(vol * 5, 20)))
        else:
            score = 50

        # Build reasons pills
        reasons = []
        if rsi < 35: reasons.append('RSI OVERSOLD')
        if rsi > 65: reasons.append('RSI OVERBOUGHT')
        if mpi > 65: reasons.append('BULL POWER HIGH')
        if mpi < 35: reasons.append('BEAR POWER HIGH')
        if vol > 2:  reasons.append(f'VOL {vol:.1f}x SPIKE')
        if info.get('anomaly_bearish'): reasons.append('BEARISH ANOMALY')
        if info.get('anomaly_bullish'): reasons.append('BULLISH ANOMALY')

        tf_data = {
            'score': score, 'direction': direction,
            'rsi': rsi, 'mpi': mpi,
            'vol_spike': vol,
            'vwap': info.get('price_usd', 0),   # approx: no real VWAP in this bot
            'cvd': (mpi - 50) * vol * 10,         # synthetic CVD
            'stoch_k': max(0, min(100, rsi + (mpi - 50) * 0.3)),  # synthetic stoch
            'tp1': f"{info.get('tp1_usd', 0):.8f}",
            'tp2': f"{info.get('tp2_usd', 0):.8f}",
            'tp3': f"{info.get('tp3_usd', 0):.8f}",
            'sl':  f"{info.get('sl_usd', 0):.8f}",
            'reasons': reasons,
        }

        data.append({
            'symbol': f"{coin}/IDR",
            'price':  f"{info.get('price_usd', 0):.8f}",
            'grade':  grade,
            'agg_score':     score,
            'agg_direction': direction,
            'funding_rate':  0.0,     # Indodax tidak ada funding rate
            'open_interest': 0,
            'ls_ratio':      1.0,
            'orderbook':     {'ob_imbalance': mpi / 100},
            'timeframes': {
                '1h': tf_data,
                'AGG': tf_data,
            },
        })

    return jsonify({
        'data':          data,
        'timestamp':     datetime.utcnow().strftime('%H:%M:%S'),
        'total_scanned': len(active_alerts),
        'longs':         longs,
        'shorts':        shorts,
        'a_plus':        a_plus,
    })


# ── /api/prices — realtime price update per card ──
@app.route('/api/prices')
def api_prices():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401

    prices = {}
    for coin, info in active_alerts.items():
        prices[f"{coin}/IDR"]  = info.get('price_usd', 0)
        prices[coin]           = info.get('price_usd', 0)
    return jsonify({'prices': prices})


# ── /api/account — format lengkap yang diharapkan HTML renderAccount() ──
@app.route('/api/account')
def get_account():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401

    s = va.get_summary()

    # Hitung unrealized PnL dari semua trade aktif
    upnl = 0.0
    positions = []
    for t in va.active_trades.values():
        coin = t['coin']
        info = active_alerts.get(coin, {})
        curr_price = info.get('price_usd', t['entry'])
        qty = t['qty'] * t['remaining_qty_pct']
        if t['direction'] == 'LONG':
            trade_upnl = (curr_price - t['entry']) * qty
        else:
            trade_upnl = (t['entry'] - curr_price) * qty
        upnl += trade_upnl

        pnl_pct = ((curr_price - t['entry']) / t['entry'] * 100) if t['entry'] > 0 else 0
        if t['direction'] == 'SHORT': pnl_pct *= -1

        positions.append({
            'id':              t['id'],
            'symbol':          f"{coin}/USDT",
            'direction':       t['direction'],
            'leverage':        1,
            'entry_price':     f"{t['entry']:.8f}",
            'current_price':   f"{curr_price:.8f}",
            'margin':          t['size_usd'],
            'tp1':             f"{t['tp1']:.8f}",
            'tp2':             f"{t['tp2']:.8f}",
            'sl':              f"{t['sl']:.8f}",
            'tp1_hit':         t['tp1_hit'],
            'tp2_hit':         t['tp2_hit'],
            'unrealized_pnl':  round(trade_upnl, 6),
            'pnl_pct':         round(pnl_pct, 2),
            'bep':             t['bep_active'],
            'trailing':        t.get('trailing_active', False),
            'remaining_pct':   f"{t['remaining_qty_pct']*100:.0f}%",
            'open_time':       t['open_time'],
        })

    history = []
    for t in va.trade_history[-20:]:
        history.append({
            'id':           t['id'],
            'symbol':       f"{t['coin']}/USDT",
            'direction':    t['direction'],
            'entry_price':  f"{t['entry']:.8f}",
            'realized_pnl': round(t['realized_pnl'], 6),
            'close_reason': t.get('close_reason', 'CLOSED'),
            'close_time':   t.get('close_time', ''),
        })

    equity = s['total_margin'] + upnl
    total_pnl = sum(t['realized_pnl'] for t in va.trade_history)
    used_margin = s['total_margin'] - s['available_margin']
    return_pct = ((equity - VirtualAccount.DEFAULT_MARGIN) / VirtualAccount.DEFAULT_MARGIN * 100)

    return jsonify({
        'stats': {
            'equity':           round(equity, 4),
            'balance':          s['total_margin'],
            'unrealized_pnl':   round(upnl, 6),
            'total_pnl':        round(total_pnl, 4),
            'total_return_pct': round(return_pct, 2),
            'win_rate':         s['win_rate'],
            'total_trades':     s['total_closed'],
            'used_margin':      round(used_margin, 2),
            'open_positions':   s['active_trades'],
            'per_trade_size':   s['per_trade_size'],
        },
        'positions': positions,
        'history':   history,
    })


# ── /api/account/set_balance — set balance dari HTML input ──
@app.route('/api/account/set_balance', methods=['POST'])
def api_set_balance():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    new_balance = float(data.get('balance', 0))
    if new_balance < 10:
        return jsonify({"success": False, "error": "Minimum $10"}), 400
    va.set_margin(new_balance)
    return jsonify({"success": True, "balance": va.total_margin})


# ── /api/account/reset — reset akun ke $1000 ──
@app.route('/api/account/reset', methods=['POST'])
def api_reset_account():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    if data.get('password') != "130806":
        return jsonify({"success": False, "error": "Wrong password"}), 403

    with va.lock:
        va.total_margin    = VirtualAccount.DEFAULT_MARGIN
        va.available_margin = VirtualAccount.DEFAULT_MARGIN
        va.active_trades   = {}
        va.trade_history   = []
        va.trade_counter   = 0
    return jsonify({"success": True, "balance": va.total_margin})


# ── /api/close/<pos_id> — close posisi dari tombol CLOSE di HTML ──
@app.route('/api/close/<pos_id>', methods=['POST'])
def api_close_position(pos_id):
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401

    trade = va.active_trades.get(pos_id)
    if not trade:
        return jsonify({"success": False, "error": "Trade not found"}), 404

    coin = trade['coin']
    info = active_alerts.get(coin, {})
    curr_price = info.get('price_usd', trade['entry'])
    pnl = va.close_trade_full(pos_id, curr_price, "WEB_CLOSE")
    return jsonify({"success": True, "pnl": round(pnl, 6)})


# ── /api/config/margin — set margin % dari HTML ──
@app.route('/api/config/margin', methods=['POST'])
def api_config_margin():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    pct = float(data.get('margin', 10))
    if not (1 <= pct <= 100):
        return jsonify({"success": False, "error": "Margin 1–100%"}), 400
    margin_config['pct'] = pct
    return jsonify({"success": True, "margin_pct": pct})


# ── /api/intelligence — legacy endpoint (tetap ada) ──
@app.route('/api/intelligence')
def get_intelligence():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401
    reports = []
    for coin, info in sorted(active_alerts.items(), key=lambda x: x[1].get('time', ''), reverse=True):
        reports.append({
            "asset": coin, "signal": info.get('signal'), "grade": info.get('grade'),
            "direction": info.get('direction'),
            "time": info.get('time'),
            "price": f"{info.get('price_usd', 0):.8f}",
            "sl": f"{info.get('sl_usd', 0):.8f}",
            "tp1": f"{info.get('tp1_usd', 0):.8f}",
            "tp2": f"{info.get('tp2_usd', 0):.8f}",
            "tp3": f"{info.get('tp3_usd', 0):.8f}",
            "rsi": f"{info.get('rsi', 0):.2f}",
            "mpi": f"{info.get('mpi', 0):.1f}",
            "vol": f"{info.get('vol_spike', 0):.1f}",
            "atr": f"{info.get('atr', 0):.4f}",
        })
    return jsonify({"reports": reports})


# ================== 🚀 MAIN ==================
if __name__ == "__main__":
    fetch_all_markets()
    port = int(os.environ.get("PORT", 8000))
    threading.Thread(target=whale_and_anomaly_detector, daemon=True).start()
    threading.Thread(target=monitor_active_trades, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    print(f"🚀 Bot running on port {port}")
    print(f"💼 Virtual Account: ${va.total_margin} | Per Trade: ${va.get_trade_size()}")
    app.run(host='0.0.0.0', port=port)
