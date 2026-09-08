import os
import time
import json
import asyncio
import websockets
import requests
from ai_brain import GeminiTradingBrain

# --- KONFIGURASI KREDENSIAL & BOT ---
DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089" # App ID publik Deriv untuk testing/live
SYMBOL = "frxXAUUSD"
TIMEFRAME = "M5"

# Ambil token/chat ID dari environment variables (GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

# Inisialisasi Otak AI Gemini & 45 Modul Sistem
ai_brain = GeminiTradingBrain()
system_modules_health = {mod: True for mod in ai_brain.modules_45}

# Variabel Kontrol Anti-Spam Cooldown (Detik)
LAST_SIGNAL_TIME = 0
COOLDOWN_SECONDS = 60 

def send_telegram_message(message: str):
    """Mengirimkan laporan atau sinyal ke Telegram secara aman."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "WARNING", "module": "TELEGRAM", "message": "Telegram token not configured. Skipping alert."}))
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "INFO", "module": "AGI_PROFIT_CORE", "message": "Profit signal successfully dispatched to Telegram."}))
        else:
            print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "ERROR", "module": "TELEGRAM", "message": f"Failed to send alert: {response.text}"}))
    except Exception as e:
        print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "ERROR", "module": "TELEGRAM", "message": str(e)}))

async def run_agi_autonomous_loop():
    global LAST_SIGNAL_TIME
    
    print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "INFO", "module": "AGI_PROFIT_CORE", "message": "Initializing AGI Profit Maximization Matrix..."}))
    print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "INFO", "module": "AGI_PROFIT_CORE", "message": f"Connecting to Deriv WebSocket stream for {SYMBOL}"}))

    try:
        async with websockets.connect(DERIV_WS_URL) as websocket:
            # Kirim permintaan berlangganan tick / harga real-time
            subscribe_request = json.dumps({"ticks": SYMBOL})
            await websocket.send(subscribe_request)

            while True:
                response = await websocket.recv()
                data = json.loads(response)

                # Proses data tick masuk dari WebSocket
                if "tick" in data:
                    tick_data = data["tick"]
                    current_price = tick_data["quote"]
                    
                    # Simulasi status indikator teknikal (MA, MACD, RSI)
                    current_time = time.time()
                    ma_status = "Bullish Crossover Active"
                    macd_status = "Histogram Positif (Strong Momentum)"
                    rsi_value = 58.4

                    # Pengecekan Anti-Spam Guard / Cooldown
                    if current_time - LAST_SIGNAL_TIME < COOLDOWN_SECONDS:
                        print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "WARNING", "module": "AGI_PROFIT_CORE", "message": "Anti-Spam Guard: Signal throttled. Cooldown active."}))
                        await asyncio.sleep(2)
                        continue

                    # Perbarui waktu sinyal terakhir
                    LAST_SIGNAL_TIME = current_time

                    # Siapkan paket data pasar untuk dievaluasi Otak AI Gemini & 45 Modul
                    market_snapshot = {
                        "symbol": SYMBOL,
                        "price": current_price,
                        "timeframe": TIMEFRAME,
                        "technical_summary": f"MA: {ma_status} | MACD: {macd_status} | RSI: {rsi_value}"
                    }

                    print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "INFO", "module": "AGI_PROFIT_CORE", "message": "Evaluating market conditions via Gemini Brain & 45 Modules..."}))
                    
                    # Memanggil Otak AI Gemini
                    ai_decision_report = ai_brain.analyze_market_with_modules(market_snapshot, system_modules_health)

                    # Format pesan pengiriman ke Telegram
                    telegram_payload = (
                        f"⚡ **SINGULARITY AGI PROFIT BOT** ⚡\n\n"
                        f"📊 **Instrument:** {SYMBOL} ({TIMEFRAME})\n"
                        f"💵 **Current Price:** `{current_price}`\n\n"
                        f"🧠 **Gemini AI & 45 Modules Evaluation:**\n{ai_decision_report}"
                    )

                    # Kirim hasil eksekusi ke Telegram
                    send_telegram_message(telegram_payload)
                    
                    # Berhenti sejenak setelah mengirim satu siklus sinyal valid
                    await asyncio.sleep(COOLDOWN_SECONDS)

    except Exception as e:
        print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "ERROR", "module": "AGI_PROFIT_CORE", "message": f"WebSocket Connection Error: {str(e)}"}))

if __name__ == "__main__":
    try:
        asyncio.run(run_agi_autonomous_loop())
    except KeyboardInterrupt:
        print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "INFO", "module": "AGI_PROFIT_CORE", "message": "AGI Autonomous Loop safely terminated by user."}))
