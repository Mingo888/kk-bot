# -*- coding: utf-8 -*-
import nest_asyncio
import asyncio
import requests
import pytz
import os
import json
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import Conflict, NetworkError

# 雲端環境設定
nest_asyncio.apply()

# --- 設定區 ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8429894936:AAFMVu3NZR4Em6VuWTUe1vdklTrn28mnZPY')
ADMIN_ID = int(os.getenv('ADMIN_ID', '7767209131'))
SHEET_NAME = 'KK報價機器人紀錄'

# 預設加碼數值
CURRENT_SPREAD = 0.4 
# ----------------------------

def get_taipei_now():
    tw_tz = pytz.timezone('Asia/Taipei')
    return datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

# --- Google Sheet 寫入功能 ---
def log_to_google_sheet(user_data):
    try:
        json_creds = os.getenv('GOOGLE_CREDENTIALS')
        if not json_creds: return
        creds_dict = json.loads(json_creds)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1
        row = [get_taipei_now(), user_data['full_name'], str(user_data['id']), f"@{user_data['username']}", "啟動/查詢"]
        sheet.append_row(row)
    except Exception as e: print(f"Sheet Error: {e}")

# --- 價格查詢函數 ---
def get_bitopro_price():
    url = "https://api.bitopro.com/v3/tickers/usdt_twd"
    try:
        data = requests.get(url, timeout=5).json()
        return float(data['data']['lastPrice'])
    except: return None

def get_binance_cny_third_price():
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT", "fiat": "CNY", "merchantCheck": False, "page": 1,
        "payTypes": [], "publisherType": None, "rows": 10, "tradeType": "BUY"
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        data = response.json()
        ads = data.get('data', [])
        valid_ads = [ad for ad in ads if 6.0 <= float(ad['adv']['price']) <= 9.0]
        if len(valid_ads) >= 3:
            target = valid_ads[2]
            return {"price": float(target['adv']['price']), "name": target['advertiser']['nickName']}
        elif valid_ads:
            target = valid_ads[0]
            return {"price": float(target['adv']['price']), "name": target['advertiser']['nickName']}
        return None
    except: return None

# 🔥 功能選單 (這裡改用 tg:// 協議，解決瀏覽器跳轉問題)
def get_function_inline_kb():
    kb = [
        [InlineKeyboardButton("🇨🇳 U兌人民幣", callback_data="switch_cny"),
         InlineKeyboardButton("🇹🇼 U兌台幣", callback_data="switch_u2tw")],
        [InlineKeyboardButton("🚀 台幣兌U", callback_data="switch_tw2u"),
         InlineKeyboardButton("💱 台幣兌人民幣", callback_data="switch_tw2cny")],
        # 👇 這裡改成 tg://resolve，手機點了會直接開機器人，不會開網頁
        [InlineKeyboardButton("⚡️ TRX能量兌換", url="tg://resolve?domain=kk168usdt_bot")]
    ]
    return InlineKeyboardMarkup(kb)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, user):
    msg = f"🔔 **新用戶通知**\n👤 {user.full_name}\n🆔 `{user.id}`\n@{user.username}"
    try: await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode='Markdown')
    except: pass

async def set_spread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_SPREAD
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ 您沒有權限執行此指令。")
        return
    try:
        new_value = float(context.args[0])
        CURRENT_SPREAD = new_value
        await update.message.reply_text(f"✅ **設定成功！**\n目前的加碼值已更新為：`+{CURRENT_SPREAD}`", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text(f"⚠️ **格式錯誤**\n請輸入 `/set 數字`\n例如：`/set 0.5`\n\n目前數值為：`+{CURRENT_SPREAD}`", parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await notify_admin(context, user)
    
    # 寫入 Google Sheet
    user_data = {'full_name': user.full_name, 'id': user.id, 'username': user.username if user.username else '無'}
    asyncio.get_running_loop().run_in_executor(None, log_to_google_sheet, user_data)

    # 🔥 這裡新增了第三排按鈕：TRX能量租賃
    keyboard = [
        ['🇨🇳 U兌人民幣', '💱 台幣兌人民幣'],
        ['🇹🇼 U兌台幣', '🚀 台幣兌U'],
        ['⚡️ TRX能量租賃']
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_text = "✨ **KK 匯率報價助手已就緒**\n━━━━━━━━━━━━━━━━━━\n選擇查詢項目或直接聯絡『可愛的米果』@nk5219 👇"
    await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)

async def send_price_message(update_or_query, mode):
    is_query = hasattr(update_or_query, 'data')
    now = get_taipei_now()
    kb = get_function_inline_kb()
    func = update_or_query.edit_message_text if is_query else update_or_query.message.reply_text

    if mode == "cny":
        data = get_binance_cny_third_price()
        if data:
            msg = f"📋 **報價結果：🇨🇳 USDT 兌 人民幣**\n🕒 查詢時間：`{now}`\n━━━━━━━━━━━━━━━━━━\n\n👉 **即時報價：{data['price']:.2f} CNY**\n👤 參考商家：{data['name']}\n\n⚠️ *來源：幣安 P2P (第3檔)*"
            await func(msg, parse_mode='Markdown', reply_markup=kb)
        else: await func("⚠️ **數據獲取失敗**，請稍後再試。", reply_markup=kb)

    elif mode in ["u2tw", "tw2u"]:
        raw = get_bitopro_price()
        if raw:
            final = (raw + CURRENT_SPREAD) if mode == "tw2u" else raw
            title = "🚀 台幣 兌 USDT" if mode == "tw2u" else "🇹🇼 USDT 兌 台幣"
            msg = f"📋 **報價結果：{title}**\n🕒 查詢時間：`{now}`\n━━━━━━━━━━━━━━━━━━\n\n👉 **即時報價：{final:.2f} TWD**\n\n"
            
            if mode == "tw2u":
                msg += f"⚠️ 本報價參考台灣銀行美元現金銀行賣出價及當下C2C市場波動浮動調整。"
            else:
                msg += f"⚠️ 報價是參考台灣幣托實時報價"
            await func(msg, parse_mode='Markdown', reply_markup=kb)

    elif mode == "tw2cny":
        raw_bito = get_bitopro_price()
        cny_data = get_binance_cny_third_price()
        if raw_bito and cny_data:
            final_rate = (raw_bito + CURRENT_SPREAD) / cny_data['price']
            msg = f"📋 **報價結果：💱 台幣 兌 人民幣**\n🕒 查詢時間：`{now}`\n━━━━━━━━━━━━━━━━━━\n\n👉 **換算匯率：{final_rate:.3f}**\n(每 1 人民幣 約需 {final_rate:.3f} 台幣)\n\n💡 *備註：是以USDT 本位計算之結果*"
            await func(msg, parse_mode='Markdown', reply_markup=kb)
        else: await func("⚠️ **無法計算**\n暫時無法獲取數據，請稍後再試。", reply_markup=kb)

# 🔥 專門處理 TRX 跳轉請求
async def send_trx_link(update):
    # 這邊一樣使用 tg:// 協議，確保直覺跳轉
    kb = [[InlineKeyboardButton("⚡️ 點擊前往 TRX 能量兌換", url="tg://resolve?domain=kk168usdt_bot")]]
    await update.message.reply_text(
        "⚡️ **TRX 能量租賃服務**\n━━━━━━━━━━━━━━━━━━\n請點擊下方按鈕直接前往機器人：",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    # 判斷文字，決定要查價還是給連結
    if '🇨🇳 U兌人民幣' in text: await send_price_message(update, "cny")
    elif '🇹🇼 U兌台幣' in text: await send_price_message(update, "u2tw")
    elif '🚀 台幣兌U' in text: await send_price_message(update, "tw2u")
    elif '💱 台幣兌人民幣' in text: await send_price_message(update, "tw2cny")
    elif 'TRX' in text or '租賃' in text: await send_trx_link(update) # 捕捉新按鈕

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    mode_map = {"switch_cny": "cny", "switch_u2tw": "u2tw", "switch_tw2u": "tw2u", "switch_tw2cny": "tw2cny"}
    if query.data in mode_map: await send_price_message(query, mode_map[query.data])

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", start))
    app.add_handler(CommandHandler("set", set_spread))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    print("🚀 Railway 機器人已啟動 (TRX直連版)...")

    # 防崩潰重連機制
    while True:
        try:
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            while app.updater.running:
                await asyncio.sleep(1)
        except (Conflict, NetworkError) as e:
            print(f"⚠️ 偵測到連線衝突或網路錯誤，5秒後重連...")
            await asyncio.sleep(5)
            if app.updater.running: await app.updater.stop()
            continue
        except Exception as e:
            print(f"❌ 錯誤：{e}")
            await asyncio.sleep(5)

if __name__ == '__main__':
    try: asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt: pass
    except Exception: pass
