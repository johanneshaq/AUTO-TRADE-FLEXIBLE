import ccxt
import time
import telebot
import pandas as pd
import threading
import urllib3
import json
import uuid
import os
from flask import Flask, jsonify, render_template, request, Response
from datetime import datetime, timezone
from dotenv import load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================== 🔐 LOAD DATA ENV ==================
load_dotenv("DATA.env")

TOKEN = os.getenv("TOKEN_MACRO")
# Mendukung multi CHAT_ID (pisahkan dengan koma di .env)
raw_chat_ids = os.getenv("CHAT_ID", "")
CHAT_IDS = [cid.strip() for cid in raw_chat_ids.split(",") if cid.strip()]
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "181268")

if not TOKEN or not CHAT_IDS:
    raise ValueError("❌ Kritis: TOKEN_LOW atau CHAT_ID belum diset di DATA.env.")

# ================== ⚙️ CONFIG VIRTUAL ACCOUNT ==================
ACCOUNT_FILE       = "virtual_account_indodax.json"
INITIAL_BALANCE    = 1000.0
LEVERAGE           = 1  # Indodax adalah Spot, kita simulasikan 1x leverage
MARGIN_PER_TRADE   = 0.10 # Gunakan 10% saldo per koin
MAX_OPEN_POSITIONS = 5

app = Flask(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 🔗 INITIALIZATION ==================
bot = telebot.TeleBot(TOKEN)
exchange = ccxt.indodax({'enableRateLimit': True, 'verify': False})
current_usd_rate = 16200 
ALL_IDR_SYMBOLS = []
last_alerts = {}
active_alerts = {}
current_prices = {}

# --- SECURITY ---
def check_auth(username, password):
    return username == "admin" and password == WEB_PASSWORD

def authenticate():
    return Response('Akses ditolak!', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

# ================== 💰 VIRTUAL ACCOUNT ENGINE ==================

def load_account():
    if os.path.exists(ACCOUNT_FILE):
        try:
            with open(ACCOUNT_FILE,'r') as f: return json.load(f)
        except: pass
    return {
        'balance': INITIAL_BALANCE, 
        'initial_balance': INITIAL_BALANCE,
        'positions': {}, 
        'history': [], 
        'total_trades': 0,
        'winning_trades': 0, 
        'total_pnl': 0.0
    }

def save_account(a):
    with open(ACCOUNT_FILE,'w') as f: json.dump(a, f, indent=2)

def calculate_pnl(pos, current_price):
    if pos['direction'] == "ACCUMULATION":
        return (current_price - pos['entry_price']) / pos['entry_price'] * pos['notional']
    else: # DISTRIBUTION / SELL (Short simulation)
        return (pos['entry_price'] - current_price) / pos['entry_price'] * pos['notional']

def open_virtual_trade(symbol, data):
    acc = load_account()
    if len(acc['positions']) >= MAX_OPEN_POSITIONS: return
    
    # Cek apakah koin ini sudah ada posisi aktif
    if any(p['symbol'] == symbol for p in acc['positions'].values()): return

    margin = acc['balance'] * MARGIN_PER_TRADE
    notional = margin * LEVERAGE
    pid = str(uuid.uuid4())[:8].upper()
    
    pos = {
        'id': pid,
        'symbol': symbol,
        'direction': "LONG" if "ACCUMULATION" in data['signal'] else "SHORT",
        'entry_price': data['price_idr'],
        'margin': round(margin, 2),
        'notional': round(notional, 2),
        'tp1': data['tp1_idr'], # Kita asumsikan fungsi analysis return idr
        'rsi': data['rsi'],
        'opened_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    acc['positions'][pid] = pos
    acc['balance'] -= margin
    acc['total_trades'] += 1
    save_account(acc)
    return pos

def update_virtual_positions():
    acc = load_account()
    closed_any = False
    
    for pid, pos in list(acc['positions'].items()):
        cp = current_prices.get(pos['symbol'])
        if not cp: continue
        
        # Logika Exit Sederhana: TP1 tercapai atau RSI berbalik
        is_long = pos['direction'] == "LONG"
        hit_tp = (cp >= pos['tp1']) if is_long else (cp <= pos['tp1'])
        
        if hit_tp:
            pnl = calculate_pnl(pos, cp)
            acc['balance'] += pos['margin'] + pnl
            acc['total_pnl'] += pnl
            if pnl > 0: acc['winning_trades'] += 1
            
            history_entry = {**pos, 'exit_price': cp, 'pnl': round(pnl, 2), 'closed_at': datetime.now().strftime('%H:%M:%S')}
            acc['history'].append(history_entry)
            del acc['positions'][pid]
            closed_any = True
            
            # Notifikasi Close
            broadcast_msg(f"✅ *TRADE CLOSED: {pos['symbol']}*\nPnL: IDR {pnl:,.0f}\nReason: TP Hit")

    if closed_any:
        save_account(acc)

def broadcast_msg(msg, markup=None):
    for cid in CHAT_IDS:
        try:
            bot.send_message(cid, msg, parse_mode='Markdown', reply_markup=markup)
        except: pass

# ================= 🧠 INTELLIGENCE ENGINE (Logika Sinyal Kamu) =================

def get_market_analysis(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=100)
        if not ohlcv or len(ohlcv) < 20: return None        
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        
        # Indikator Dasar
        df['sma_20'] = df['close'].rolling(window=20).mean()
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))
        
        # MPI & Vol Spike
        green_vol = df[df['close'] > df['open']]['vol'].sum()
        red_vol = df[df['close'] < df['open']]['vol'].sum()
        mpi = (green_vol / (green_vol + red_vol)) * 100 if (green_vol + red_vol) > 0 else 50
        
        last = df.iloc[-1]
        df['vol_avg'] = df['vol'].rolling(window=20).mean()
        vol_spike_ratio = last['vol'] / df['vol_avg'].iloc[-1] if df['vol_avg'].iloc[-1] > 0 else 0
        
        signal = "⚖️ NEUTRAL"
        if last['rsi'] < 35: signal = "🚀 STRONG ACCUMULATION"
        elif last['rsi'] > 65: signal = "🔴 DISTRIBUTION / SELL"

        curr_p = last['close']
        current_prices[symbol] = curr_p # Update global price for account engine
        
        # Adaptive TP (Revisi: Simpan dalam IDR juga)
        df['range_pct'] = (df['high'] - df['low']) / df['low']
        avg_range = df['range_pct'].tail(20).mean()
        base_step = max(min(avg_range, 0.08), 0.01)
        power_multiplier = 1.0 + (vol_spike_ratio / 10)

        if "ACCUMULATION" in signal:
            tp1_idr = curr_p * (1 + base_step)
        elif "DISTRIBUTION" in signal:
            tp1_idr = curr_p * (1 - base_step)
        else: tp1_idr = curr_p

        grade = "C (LOW)"
        if "ACCUMULATION" in signal and mpi > 65 and vol_spike_ratio > 1.5: grade = "A+ (PERFECT)"
        elif "DISTRIBUTION" in signal and mpi < 35 and vol_spike_ratio > 1.5: grade = "A+ (PERFECT)"
        elif (mpi > 65 or mpi < 35) and vol_spike_ratio <= 1.5: grade = "B (EARLY)"

        return {
            'price_usd': (curr_p / current_usd_rate) * 0.95,
            'price_idr': curr_p,
            'tp1_idr': tp1_idr,
            'tp1_usd': (tp1_idr / current_usd_rate) * 0.95,
            'rsi': last['rsi'], 'mpi': mpi, 'signal': signal, 'vol_spike': vol_spike_ratio, 'grade': grade
        }
    except: return None

# ================= 🐋 SCANNER ENGINE =================
def scanner_engine():
    while True:
        for symbol in ALL_IDR_SYMBOLS:
            try:
                data = get_market_analysis(symbol)
                if data is None: continue
            
                coin_name = symbol.split('/')[0]
                data['time'] = datetime.now().strftime('%H:%M:%S')
                active_alerts[coin_name] = data 

                # UPDATE POSISI VIRTUAL
                update_virtual_positions()

                # EKSEKUSI OTOMATIS JIKA GRADE A+
                if data['grade'] == "A+ (PERFECT)":
                    if coin_name not in last_alerts or last_alerts[coin_name] != data['signal']:
                        
                        # 1. Buka Posisi di Virtual Account
                        pos = open_virtual_trade(symbol, data)
                        
                        # 2. Kirim Notifikasi Telegram
                        status_trade = "✅ AUTO-TRADE EXECUTED" if pos else "⚠️ SCANNER ALERT (Max Pos Reached)"
                        msg = (
                            f"🌟 **{status_trade}** 🌟\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 Asset: `{coin_name}`\n"
                            f"🏆 Grade: **{data['grade']}**\n"
                            f"📢 Signal: **{data['signal']}**\n"
                            f"💵 Entry: `Rp {data['price_idr']:,}`\n"
                            f"🎯 TP1: `Rp {data['tp1_idr']:,}`\n"
                            f"🐳 Power: `{data['mpi']:.1f}%` | ⚡ Vol: `{data['vol_spike']:.1f}x`"
                        )
                        markup = InlineKeyboardMarkup()
                        markup.add(InlineKeyboardButton("📊 Chart", url=f"https://indodax.com/market/{coin_name}IDR"))
                        broadcast_msg(msg, markup)
                        
                        last_alerts[coin_name] = data['signal']
                time.sleep(1)
            except: continue
        time.sleep(30)

# ================= 🌐 ROUTES =================
@app.route('/')
def index():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password): return authenticate()
    return render_template('index.html') # Pastikan index.html kamu mendukung data dari /api/account

@app.route('/api/close/<pid>', methods=['POST'])
def api_close(pid):
    acc = load_account()
    if pid in acc['positions']:
        pos = acc['positions'].pop(pid)
        cp = current_prices.get(pos['symbol'], pos['entry_price'])
        pnl = calculate_pnl(pos, cp)
        
        acc['balance'] += pos['margin'] + pnl
        acc['total_pnl'] += pnl
        if pnl > 0: acc['winning_trades'] += 1
        
        acc['history'].append({**pos, 'exit_price': cp, 'pnl': round(pnl, 2), 'closed_at': datetime.now().strftime('%H:%M:%S'), 'close_reason': 'Manual Close'})
        save_account(acc)
        return jsonify({"success": True, "pnl": pnl})
    return jsonify({"success": False, "error": "Position not found"}), 404

@app.route('/api/account')
def api_account():
    acc = load_account()
    # Hitung uPnL Real-time
    unrealized = 0
    for p in acc['positions'].values():
        cp = current_prices.get(p['symbol'], p['entry_price'])
        unrealized += calculate_pnl(p, cp)
    
    stats = {
        'balance': round(acc['balance'], 2),
        'equity': round(acc['balance'] + unrealized, 2),
        'unrealized_pnl': round(unrealized, 2),
        'total_trades': acc['total_trades'],
        'win_rate': round((acc['winning_trades']/acc['total_trades']*100),1) if acc['total_trades']>0 else 0,
        'open_positions': len(acc['positions'])
    }
    return jsonify({"stats": stats, "positions": list(acc['positions'].values()), "history": acc['history'][-10:]})

@app.route('/api/account/reset', methods=['POST'])
def api_reset():
    if not _auth(): return jsonify({"error":"Unauthorized"}), 401
    # Fungsi reset_account harus dibuat untuk menghapus file JSON atau mereset dictionary
    acc = {
        'balance': 1000.0, 
        'initial_balance': 1000.0,
        'positions': {}, 
        'history': [], 
        'total_trades': 0,
        'winning_trades': 0, 
        'total_pnl': 0.0
    }
    save_account(acc)
    return jsonify({"success": True, "message": "Account reset to $1000"})

@app.route('/api/intelligence')
def get_intelligence():
    reports = []
    for coin, info in active_alerts.items():
        reports.append({"asset": coin, "signal": info['signal'], "grade": info['grade'], "price": f"{info['price_usd']:.4f}"})
    return jsonify({"reports": reports})

if __name__ == "__main__":
    exchange.load_markets()
    ALL_IDR_SYMBOLS = [s for s in exchange.markets if s.endswith('/IDR')]
    print(f"🚀 Bot Started. Scanning {len(ALL_IDR_SYMBOLS)} Indodax assets.")
    
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8000)))
