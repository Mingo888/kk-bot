# -*- coding: utf-8 -*-
import nest_asyncio
import asyncio
import requests
import pytz
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

# 雲端環境設定
nest_asyncio.apply()

# --- 設定區 ---
TELEGRAM_TOKEN = '8429894936:AAFMVu3NZR4Em6VuWTUe1vdklTrn28mnZPY'
ADMIN_ID = 7767209131
# ----------------------------

def get_taipei_now():
    tw_tz = pytz.timezone('Asia/Taipei')
    return datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

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

def get_function_inline_kb():
    kb = [
        [InlineKeyboardButton("???? U兌人民幣", callback_data="switch_cny"),
         InlineKeyboardButton("???? U兌台幣", callback_data="switch_u2tw")],
        [InlineKeyboardButton("?? 台幣兌U", callback_data="switch_tw2u"),
         InlineKeyboardButton("?? 台幣兌人民幣", callback_data="switch_tw2cny")]
    ]
    return InlineKeyboardMarkup(kb)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, user):
    msg = f"?? **新用戶通知**\n?? {user.full_name}\n?? `{user.id}`\n@{user.username}"
    try: await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode='Markdown')
    except: pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await notify_admin(context, user)
    keyboard = [['???? U兌人民幣', '?? 台幣兌人民幣'], ['???? U兌台幣', '?? 台幣兌U']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    welcome_text = "? **KK 匯率報價助手已就緒**\n??????????????????\n選擇查詢項目或直接聯絡『可愛的米果』@nk5219 ??"
    await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)

async def send_price_message(update_or_query, mode):
    is_query = hasattr(update_or_query, 'data')
    now = get_taipei_now()
    kb = get_function_inline_kb()
    func = update_or_query.edit_message_text if is_query else update_or_query.message.reply_text

    if mode == "cny":
        data = get_binance_cny_third_price()
        if data:
            msg = f"?? **報價結果：???? USDT 兌 人民幣**\n?? 查詢時間：`{now}`\n??????????????????\n\n?? **即時報價：{data['price']:.2f} CNY**\n?? 參考商家：{data['name']}\n\n?? *來源：幣安 P2P (第3檔)*"
            await func(msg, parse_mode='Markdown', reply_markup=kb)
        else: await func("?? **數據獲取失敗**，請稍後再試。", reply_markup=kb)

    elif mode in ["u2tw", "tw2u"]:
        raw = get_bitopro_price()
        if raw:
            final = raw + 0.4 if mode == "tw2u" else raw
            title = "?? 台幣 兌 USDT" if mode == "tw2u" else "???? USDT 兌 台幣"
            msg = f"?? **報價結果：{title}**\n?? 查詢時間：`{now}`\n??????????????????\n\n?? **即時報價：{final:.2f} TWD**\n\n?? *報價僅供參考。*"
            await func(msg, parse_mode='Markdown', reply_markup=kb)

    elif mode == "tw2cny":
        raw_bito = get_bitopro_price()
        cny_data = get_binance_cny_third_price()
        if raw_bito and cny_data:
            final_rate = (raw_bito + 0.4) / cny_data['price']
            msg = f"?? **報價結果：?? 台幣 兌 人民幣**\n?? 查詢時間：`{now}`\n??????????????????\n\n?? **換算匯率：{final_rate:.3f}**\n(每 1 人民幣 約需 {final_rate:.3f} 台幣)\n\n?? *備註：是以USDT 本位計算之結果*"
            await func(msg, parse_mode='Markdown', reply_markup=kb)
        else: await func("?? **無法計算**\n暫時無法獲取數據，請稍後再試。", reply_markup=kb)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if '???? U兌人民幣' in text: await send_price_message(update, "cny")
    elif '???? U兌台幣' in text: await send_price_message(update, "u2tw")
    elif '?? 台幣兌U' in text: await send_price_message(update, "tw2u")
    elif '?? 台幣兌人民幣' in text: await send_price_message(update, "tw2cny")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    mode_map = {"switch_cny": "cny", "switch_u2tw": "u2tw", "switch_tw2u": "tw2u", "switch_tw2cny": "tw2cny"}
    if query.data in mode_map: await send_price_message(query, mode_map[query.data])

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    print("?? Railway 機器人已啟動 (編碼修正版)...")
    await app.initialize(); await app.start(); await app.updater.start_polling()
    while True: await asyncio.sleep(1)

if __name__ == '__main__':
    try: asyncio.get_event_loop().run_until_complete(main())
    except: pass