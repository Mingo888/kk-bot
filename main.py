# -*- coding: utf-8 -*-
import nest_asyncio
import asyncio
import requests
import pytz
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import Conflict, NetworkError

# 雲端環境設定
nest_asyncio.apply()

# --- 設定區 ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8429894936:AAHSOBz1-uBD5bzrjSP1xe3Amaky1q_juB8")
ADMIN_IDS = [7767209131, 7627006763] # 預設管理員 ID 列表
SHEET_NAME = "KK報價機器人紀錄"
CURRENT_SPREAD = 0.4

# ----------------------------

def get_taipei_now():
    tw_tz = pytz.timezone("Asia/Taipei")
    return datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

# --- Google Sheet 寫入 ---
def log_to_google_sheet(user_data):
    try:
        json_creds = os.getenv("GOOGLE_CREDENTIALS")
        if not json_creds: return
        creds_dict = json.loads(json_creds)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1
        row = [get_taipei_now(), user_data["full_name"], str(user_data["id"]), f"@{user_data['username']}", "啟動/查詢"]
        sheet.append_row(row)
    except Exception as e: print(f"Sheet Error: {e}")

# --- 價格查詢區 ---
def get_bitopro_price():
    url = "https://api.bitopro.com/v3/tickers/usdt_twd"
    try:
        data = requests.get(url, timeout=5).json()
        return float(data["data"]["lastPrice"])
    except: return None

def get_binance_p2p_price(fiat_code, price_range=None, trade_type="BUY", rows=10):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT", "fiat": fiat_code, "merchantCheck": False, "page": 1,
        "payTypes": [], "publisherType": None, "rows": rows, "tradeType": trade_type
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        data = response.json()
        ads = data.get("data", [])
        
        valid_ads = []
        for ad in ads:
            price = float(ad["adv"]["price"])
            if price_range:
                min_price, max_price = price_range
                if (min_price is None or price >= min_price) and \
                   (max_price is None or price <= max_price):
                    valid_ads.append(ad)
            else:
                valid_ads.append(ad)

        if len(valid_ads) >= 3: 
            return {"price": float(valid_ads[2]["adv"]["price"]), "name": valid_ads[2]["advertiser"]["nickName"]}
        elif valid_ads: 
            return {"price": float(valid_ads[0]["adv"]["price"]), "name": valid_ads[0]["advertiser"]["nickName"]}
        return None
    except Exception as e:
        print(f"P2P Price Error for {fiat_code}: {e}")
        return None

def get_binance_cny_third_price():
    return get_binance_p2p_price("CNY", price_range=(6.0, 9.0))

def get_bithumb_krw_price():
    url = "https://api.bithumb.com/public/ticker/USDT_KRW"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        if data["status"] == "0000": return {"price": float(data["data"]["closing_price"]), "name": "Bithumb 交易所"}
        return None
    except: return None

def get_binance_krw_price():
    return get_binance_p2p_price("KRW", price_range=(1000, None))

def get_remitano_myr_price():
    url = "https://api.remitano.com/api/v1/rates/ads"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        price = float(data.get("usdtmyr", {}).get("exchangeRate", 0))
        if price > 0:
            return {"price": price, "source": "Remitano 當地 P2P 行情"}
        return None
    except Exception as e:
        print(f"Remitano Price Error: {e}")
        return None

def get_coinbase_myr_price():
    url = "https://api.coinbase.com/v2/prices/USDT-MYR/spot"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        price = float(data.get("data", {}).get("amount", 0))
        if price > 0:
            return {"price": price, "source": "Coinbase 國際指數價"}
        return None
    except Exception as e:
        print(f"Coinbase Price Error: {e}")
        return None

# 🔥 核心修正：台銀中價計算
def get_taiwan_bank_cny():
    url = "https://rate.bot.com.tw/xrt/flcsv/0/day"
    try:
        response = requests.get(url, timeout=5)
        response.encoding = "utf-8"
        lines = response.text.splitlines()
        for line in lines:
            if line.startswith("CNY"):
                cols = line.split(",")
                cash_buy = float(cols[2])   # 台銀現金買入
                cash_sell = float(cols[12]) # 台銀現金賣出 (精準鎖定這格)
                mid_price = (cash_buy + cash_sell) / 2 
                return {"buy": cash_buy, "sell": cash_sell, "mid": mid_price}
        return None
    except: return None

# 🔥 功能選單 
def get_function_inline_kb():
    kb = [
        [InlineKeyboardButton("🇨🇳 U兌人民幣", callback_data="switch_cny"),
         InlineKeyboardButton("🚀 韓幣兌U", callback_data="switch_krw2u"),
         InlineKeyboardButton("🇲🇾 馬幣兌U", callback_data="switch_myr")],
        [InlineKeyboardButton("🇹🇼 U兌台幣", callback_data="switch_u2tw"),
         InlineKeyboardButton("🚀 台幣兌U", callback_data="switch_tw2u")],
        [InlineKeyboardButton("💱 台幣兌人民幣", callback_data="switch_tw2cny"),
         InlineKeyboardButton("⚡️ TRX能量兌換", url="tg://resolve?domain=KKfreetron_Bot")]
    ]
    return InlineKeyboardMarkup(kb)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, user):
    msg = f"🔔 **新用戶通知**\n👤 {user.full_name}\n🆔 `{user.id}`\n@{user.username}"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
        except: pass

async def set_spread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_SPREAD
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        CURRENT_SPREAD = float(context.args[0])
        await update.message.reply_text(f"✅ **設定成功！**\n目前的加碼值已更新為：`+{CURRENT_SPREAD}`", parse_mode="Markdown")
    except:
        await update.message.reply_text(f"⚠️ **格式錯誤**\n目前數值為：`+{CURRENT_SPREAD}`", parse_mode="Markdown")

# 🔥 老闆專屬指令：/tc (格式完全客製化) 🔥
async def tc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return

    await update.message.reply_text("⏳ 正在為您結算分析，請稍候...")

    raw_bito = get_bitopro_price()
    cny_data = get_binance_cny_third_price()
    bot_data = get_taiwan_bank_cny()

    if raw_bito and cny_data and bot_data:
        bot_best_rate = (raw_bito + CURRENT_SPREAD) / cny_data["price"]
        mid_price = bot_data["mid"]
        now = get_taipei_now()

        try:
            if context.args:
                client_price = float(context.args[0])
                is_custom = True
            else:
                client_price = bot_best_rate
                is_custom = False
        except ValueError:
            await update.message.reply_text("⚠️ 格式錯誤，請輸入數字，例如：`/tc 4.6`")
            return

        # 1. 銀行對標計算
        diff_bank = mid_price - bot_best_rate
        pct_bank = (diff_bank / bot_best_rate) * 100
        bank_word = "溢價" if diff_bank > 0 else "折讓"
        bank_sign = "+" if diff_bank > 0 else ""

        msg = f"🕵️‍♂️ **老闆專屬：報價結算分析**\n🕒 `{now}`\n━━━━━━━━━━━━━━━━━━\n\n"
        msg += f"🤖 **最佳狀態成本價**：`{bot_best_rate:.4f}`\n"
        msg += f"🏦 **台銀現金中間價**：`{mid_price:.4f}`\n\n"

        if is_custom:
            msg += f"🤝 **您兌給客戶的價**：`{client_price:.4f}` (手動輸入)\n\n"
        else:
            msg += f"🤝 **您兌給客戶的價**：`{client_price:.4f}` (預設最佳)\n\n"

        msg += f"📊 **結算分析**：\n"

        if is_custom:
            # 情況 A: 輸入了 /tc 4.6
            diff_client = client_price - bot_best_rate
            pct_client = (diff_client / bot_best_rate) * 100
            client_word = "溢價" if diff_client > 0 else "折讓"
            client_sign = "+" if diff_client > 0 else ""

            msg += f"① 台銀中價的話，成本折讓為： {mid_price:.4f}-{bot_best_rate:.4f} = {diff_bank:.4f}\n"
            msg += f"{bank_word}{bank_sign}{pct_bank:.3f}%\n"
            msg += f"② 客戶價對標最佳成本：{diff_client:.4f}\n"
            msg += f"{client_word}{client_sign}{pct_client:.3f}%\n"
        else:
            # 情況 B: 只輸入 /tc
            msg += f"台銀中價的話，成本折讓為： {mid_price:.4f}-{bot_best_rate:.4f} = {diff_bank:.4f}\n"
            msg += f"{bank_word}{bank_sign}{pct_bank:.3f}%\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ **數據抓取失敗**，請稍後再試。")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await notify_admin(context, user)
    
    user_data = {"full_name": user.full_name, "id": user.id, "username": user.username if user.username else "無"}
    asyncio.get_running_loop().run_in_executor(None, log_to_google_sheet, user_data)

    keyboard = [["🇨🇳 U兌人民幣", "🚀 韓幣兌U"], ["🇲🇾 馬幣兌U", "🇹🇼 U兌台幣", "🚀 台幣兌U"], ["💱 台幣兌人民幣", "⚡️ TRX能量租賃"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_text = "✨ **KK 匯率報價助手已就緒**\n━━━━━━━━━━━━━━━━━━\n選擇查詢項目或直接聯絡『白資承兌商』@nk5219 👇"
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)

async def send_price_message(update_or_query, mode):
    is_query = hasattr(update_or_query, "data")
    now = get_taipei_now()
    kb = get_function_inline_kb()
    func = update_or_query.edit_message_text if is_query else update_or_query.message.reply_text

    if mode == "cny":
        data = get_binance_cny_third_price()
        if data:
            msg = f"📋 **報價結果：🇨🇳 USDT 兌 人民幣**\n🕒 查詢時間：`{now}`\n━━━━━━━━━━━━━━━━━━\n\n👉 **即時報價：{data['price']:.2f} CNY**\n👤 參考商家：{data['name']}\n\n⚠️ *來源：幣安 P2P (第3檔)*"
            await func(msg, parse_mode="Markdown", reply_markup=kb)
        else: await func("⚠️ **數據獲取失敗**，請稍後再試。", reply_markup=kb)
    
    elif mode == "krw2u":
        data = get_bithumb_krw_price()
        source_name = "Bithumb 交易所"
        if not data:
            data = get_binance_krw_price()
            if data: source_name = f"幣安 P2P"
        if data:
            price = data["price"]
            msg = f"📋 **報價結果：🚀 韓幣 兌 USDT**\n🕒 查詢時間：`{now}`\n━━━━━━━━━━━━━━━━━━\n\n🏦 **即時報價：{int(price)} KRW**\n🤝 **若需韓幣現金面交服務**\n💵 **+1%：為 {int(round(price * 1.01))} KRW**\n\n⚠️ *來源：{source_name}*"
            if "幣安" in source_name: msg += f"\n👤 參考商家：{data['name']}"
            await func(msg, parse_mode="Markdown", reply_markup=kb)
        else: await func("⚠️ **數據獲取失敗**", reply_markup=kb)

    elif mode in ["u2tw", "tw2u"]:
        raw = get_bitopro_price()
        if raw:
            final = (raw + CURRENT_SPREAD) if mode == "tw2u" else raw
            title = "🚀 台幣 兌 USDT" if mode == "tw2u" else "🇹🇼 USDT 兌 台幣"
            msg = f"📋 **報價結果：{title}**\n🕒 查詢時間：`{now}`\n━━━━━━━━━━━━━━━━━━\n\n👉 **即時報價：{final:.2f} TWD**\n\n"
            if mode == "tw2u": msg += f"⚠️ 本報價參考台灣銀行美元現金銀行賣出價及當下C2C市場波動浮動調整。"
            else: msg += f"⚠️ 報價是參考台灣幣托實時報價"
            await func(msg, parse_mode="Markdown", reply_markup=kb)

    elif mode == "myr":
        remitano_data = get_remitano_myr_price()
        coinbase_data = get_coinbase_myr_price()
        
        price = None
        source_name = ""
        markup_info = ""

        if remitano_data:
            price = remitano_data["price"]
            source_name = remitano_data["source"]
            markup_info = "🤝 **面交價 (Remitano 實價)：無需加價**\n"
        elif coinbase_data:
            price = coinbase_data["price"]
            source_name = coinbase_data["source"]
            markup_price = price * 1.015
            markup_info = f"🤝 **若需馬幣現金面交服務**\n💵 **+1.5%：為 {markup_price:.2f} MYR**\n"
        
        if price:
            msg = f"📋 **報價結果：🇲🇾 馬幣 兌 USDT**\n🕒 查詢時間：`{now}`\n━━━━━━━━━━━━━━━━━━\n\n👉 **即時報價：{price:.2f} MYR**\n{markup_info}\n⚠️ *來源：{source_name}*"
            await func(msg, parse_mode="Markdown", reply_markup=kb)
        else: await func("⚠️ **數據獲取失敗，請稍後再試**", reply_markup=kb)

    elif mode == "tw2cny":
        raw_bito = get_bitopro_price()
        cny_data = get_binance_cny_third_price()
        if raw_bito and cny_data:
            final_rate = (raw_bito + CURRENT_SPREAD) / cny_data["price"]
            msg = f"📋 **報價結果：💱 台幣 兌 人民幣**\n🕒 查詢時間：`{now}`\n━━━━━━━━━━━━━━━━━━\n\n👉 **換算匯率：{final_rate:.3f}**\n(每 1 人民幣 約需 {final_rate:.3f} 台幣)\n\n💡 *備註：是以USDT 本位計算之結果*"
            await func(msg, parse_mode="Markdown", reply_markup=kb)
        else: await func("⚠️ **無法計算**", reply_markup=kb)

async def send_trx_link(update):
    kb = [[InlineKeyboardButton("⚡️ 點擊前往 TRX 能量兌換", url="tg://resolve?domain=KKfreetron_Bot")]]
    await update.message.reply_text("⚡️ **TRX 能量租賃服務**\n━━━━━━━━━━━━━━━━━━\n請點擊下方按鈕直接前往機器人：", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "🇨🇳 U兌人民幣" in text: await send_price_message(update, "cny")
    elif "🚀 韓幣兌U" in text: await send_price_message(update, "krw2u")
    elif "🇹🇼 U兌台幣" in text: await send_price_message(update, "u2tw")
    elif "🚀 台幣兌U" in text: await send_price_message(update, "tw2u")
    elif "💱 台幣兌人民幣" in text: await send_price_message(update, "tw2cny")
    elif "🇲🇾 馬幣兌U" in text: await send_price_message(update, "myr")
    elif "TRX" in text or "租賃" in text: await send_trx_link(update)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode_map = {"switch_cny": "cny", "switch_krw2u": "krw2u", "switch_u2tw": "u2tw", "switch_tw2u": "tw2u", "switch_tw2cny": "tw2cny", "switch_myr": "myr"}
    if query.data in mode_map: await send_price_message(query, mode_map[query.data])

async def main():
    print("🚀 Railway 機器人初始化中 (V22 台銀欄位修正 + 100%客製化)...")
    while True:
        try:
            app = Application.builder().token(TELEGRAM_TOKEN).build()
            app.add_handler(CommandHandler("start", start))
            app.add_handler(CommandHandler("price", start))
            app.add_handler(CommandHandler("set", set_spread))
            app.add_handler(CommandHandler("tc", tc_command))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_handler(CallbackQueryHandler(callback_handler))

            await app.initialize(); await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            
            while True:
                await asyncio.sleep(2)
                if not app.updater.running: break
        except Conflict:
            try:
                if "app" in locals() and app.updater.running:
                    await app.updater.stop(); await app.stop(); await app.shutdown()
            except: pass
            await asyncio.sleep(5); continue 
        except Exception: await asyncio.sleep(5); continue

if __name__ == "__main__":
    try: asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt: pass
    except Exception: pass
