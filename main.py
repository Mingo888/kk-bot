import asyncio
import html
import json
import os
import random
import shlex
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

import gspread
import nest_asyncio
import pytz
import requests
from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# 雲端環境設定
nest_asyncio.apply()

# --- 設定區 ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_ID = 7767209131
ADMIN_IDS = {7767209131, 7627006763}
SHEET_NAME = "KK報價機器人紀錄"
CURRENT_SPREAD = 0.4

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADS_DB_PATH = os.getenv("ADS_DB_PATH", os.path.join(BASE_DIR, "ads.db"))
OKX_P2P_BOOKS_URL = "https://www.okx.com/v3/c2c/tradingOrders/books"

# ----------------------------


def get_taipei_now():
    tw_tz = pytz.timezone("Asia/Taipei")
    return datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")


# --- SQLite 廣告資料庫 ---
def get_ads_connection():
    connection = sqlite3.connect(ADS_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_ads_db():
    """建立廣告資料表；刪除採軟刪除，保留下架稽核資料。"""
    db_dir = os.path.dirname(ADS_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with get_ads_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS banners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                link TEXT NOT NULL,
                placement TEXT NOT NULL DEFAULT 'all',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                deleted_by INTEGER,
                deleted_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_banners_active_placement
            ON banners (is_active, placement)
            """
        )
        connection.commit()


def normalize_banner_placement(value):
    """將管理員輸入的版位名稱轉成可供程式比對的分類。"""
    if not value:
        return "all"

    compact = value.strip().casefold().replace(" ", "")
    aliases = {
        "all": "all",
        "全部": "all",
        "所有": "all",
        "cashflow": "cashflow",
        "客服": "cashflow",
        "金流客服": "cashflow",
        "💬金流客服": "cashflow",
        "u2tw": "u2tw",
        "u兌台幣": "u2tw",
        "🇹🇼u兌台幣": "u2tw",
        "tw2u": "tw2u",
        "台幣兌u": "tw2u",
        "🚀台幣兌u": "tw2u",
    }
    return aliases.get(compact)


def placement_label(placement):
    return {
        "all": "全部",
        "cashflow": "💬 金流客服",
        "u2tw": "🇹🇼 U兌台幣",
        "tw2u": "🚀 台幣兌U",
    }.get(placement, placement)


def is_valid_banner_link(link):
    parsed = urlparse(link)
    return parsed.scheme in {"http", "https", "tg"} and bool(parsed.netloc)


def parse_addbanner_command(command_text):
    """解析 /addbanner <跳轉連結> [版位分類]。"""
    try:
        args = shlex.split(command_text.strip())
    except ValueError:
        return None, "⚠️ 指令格式無法解析，請確認連結沒有未配對的引號。"

    if not args or not args[0].lower().split("@", 1)[0] == "/addbanner":
        return None, None
    if len(args) not in (2, 3):
        return None, (
            "⚠️ 格式錯誤。請將圖片與以下 Caption 一起傳送：\n"
            "/addbanner <跳轉連結> [版位分類]\n\n"
            "版位分類可用：all、cashflow、u2tw、tw2u。"
        )

    link = args[1].strip()
    if not is_valid_banner_link(link):
        return None, "⚠️ 跳轉連結必須是有效的 http://、https:// 或 tg:// 連結。"

    placement = normalize_banner_placement(args[2] if len(args) == 3 else "all")
    if placement is None:
        return None, "⚠️ 不支援的版位分類，請使用 all、cashflow、u2tw 或 tw2u。"
    return {"link": link, "placement": placement}, None


def get_active_banners(placement):
    with get_ads_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, file_id, link, placement
            FROM banners
            WHERE is_active = 1 AND placement IN ('all', ?)
            ORDER BY id ASC
            """,
            (placement,),
        ).fetchall()
    return rows


def get_chat_id(update_or_query):
    if getattr(update_or_query, "effective_chat", None):
        return update_or_query.effective_chat.id
    message = getattr(update_or_query, "message", None)
    return getattr(message, "chat_id", None)


async def send_random_banner(update_or_query, context, placement):
    """隨機發送指定版位或 all 版位中的一則上架廣告。"""
    banners = get_active_banners(placement)
    chat_id = get_chat_id(update_or_query)
    if not banners or chat_id is None:
        return

    banner = random.choice(banners)
    keyboard = [[InlineKeyboardButton("立即查看", url=banner["link"])]]
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=banner["file_id"],
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as error:
        # file_id 失效或 Telegram 拒絕連結時，不影響原本的報價回覆。
        print(f"Banner Send Error (id={banner['id']}): {error}")


# --- Google Sheet 寫入 ---
def log_to_google_sheet(user_data):
    try:
        json_creds = os.getenv("GOOGLE_CREDENTIALS")
        if not json_creds:
            return
        creds_dict = json.loads(json_creds)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1
        row = [
            get_taipei_now(),
            user_data["full_name"],
            str(user_data["id"]),
            f"@{user_data['username']}",
            "啟動/查詢",
        ]
        sheet.append_row(row)
    except Exception as error:
        print(f"Sheet Error: {error}")


# --- 價格查詢區 ---
def get_bitopro_price():
    url = "https://api.bitopro.com/v3/tickers/usdt_twd"
    try:
        data = requests.get(url, timeout=5).json()
        return float(data["data"]["lastPrice"])
    except Exception:
        return None


def get_binance_p2p_price(fiat_code, price_range=None, trade_type="BUY", rows=10):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT",
        "fiat": fiat_code,
        "merchantCheck": False,
        "page": 1,
        "payTypes": [],
        "publisherType": None,
        "rows": rows,
        "tradeType": trade_type,
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        ads = data.get("data", [])

        valid_ads = []
        for ad in ads:
            price = float(ad["adv"]["price"])
            if price_range:
                min_price, max_price = price_range
                if (min_price is None or price >= min_price) and (
                    max_price is None or price <= max_price
                ):
                    valid_ads.append(ad)
            else:
                valid_ads.append(ad)

        if len(valid_ads) >= 3:
            selected = valid_ads[2]
        elif valid_ads:
            selected = valid_ads[-1]
        else:
            return None
        return {
            "price": float(selected["adv"]["price"]),
            "name": selected["advertiser"]["nickName"],
        }
    except Exception as error:
        print(f"P2P Price Error for {fiat_code}: {error}")
        return None


def get_okx_cny_third_price():
    """取得 OKX C2C USDT/CNY 賣出掛單的第 3 檔，少於 3 檔時取最後一筆。"""
    params = {
        "quoteCurrency": "CNY",
        "baseCurrency": "USDT",
        "paymentMethod": "all",
        "side": "sell",
        "userType": "all",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    try:
        response = requests.get(
            OKX_P2P_BOOKS_URL,
            params=params,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, "0", None):
            raise RuntimeError(f"OKX API code={payload.get('code')}")

        ads = payload.get("data", {}).get("sell", [])
        valid_ads = []
        for ad in ads:
            try:
                price = float(ad["price"])
            except (KeyError, TypeError, ValueError):
                continue
            valid_ads.append(
                {
                    "price": price,
                    "name": ad.get("nickName") or "OKX 商家",
                }
            )

        if not valid_ads:
            return None

        # OKX API 回傳的 sell 陣列就是目前市場排序；需求固定使用 index[2]，不足時取最後一筆。
        selected = valid_ads[min(2, len(valid_ads) - 1)]
        return selected
    except Exception as error:
        print(f"OKX CNY P2P Price Error: {error}")
        return None


def get_bithumb_krw_price():
    url = "https://api.bithumb.com/public/ticker/USDT_KRW"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data["status"] == "0000":
            return {
                "price": float(data["data"]["closing_price"]),
                "name": "Bithumb 交易所",
            }
        return None
    except Exception:
        return None


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
    except Exception as error:
        print(f"Remitano Price Error: {error}")
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
    except Exception as error:
        print(f"Coinbase Price Error: {error}")
        return None


# --- 台銀中價計算 ---
def get_taiwan_bank_cny():
    url = "https://rate.bot.com.tw/xrt/flcsv/0/day"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        response.encoding = "utf-8"
        lines = response.text.splitlines()
        for line in lines:
            if line.startswith("CNY"):
                cols = line.split(",")
                cash_buy = float(cols[2])
                cash_sell = float(cols[12])
                mid_price = (cash_buy + cash_sell) / 2
                return {"buy": cash_buy, "sell": cash_sell, "mid": mid_price}
        return None
    except Exception:
        return None


# --- 功能選單 ---
def get_function_inline_kb():
    keyboard = [
        [
            InlineKeyboardButton("🇨🇳 U兌人民幣", callback_data="switch_cny"),
            InlineKeyboardButton("💱 台幣兌人民幣", callback_data="switch_tw2cny"),
            InlineKeyboardButton("🚀 韓幣兌U", callback_data="switch_krw2u"),
        ],
        [
            InlineKeyboardButton("🇹🇼 U兌台幣", callback_data="switch_u2tw"),
            InlineKeyboardButton("🚀 台幣兌U", callback_data="switch_tw2u"),
            InlineKeyboardButton("🇲🇾 馬幣兌U", callback_data="switch_myr"),
        ],
        [
            InlineKeyboardButton("💬 金流客服", callback_data="switch_cashflow"),
            InlineKeyboardButton(
                "⚡ TRX能量租賃", url="tg://resolve?domain=KKfreetron_Bot"
            ),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, user):
    msg = f"🔔 **新用戶通知**\n👤 {user.full_name}\n🆔 `{user.id}`\n@{user.username}"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=msg,
                parse_mode="Markdown",
            )
        except Exception as error:
            print(f"Admin Notify Error ({admin_id}): {error}")


def is_admin(update: Update):
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)


async def require_admin(update: Update):
    if is_admin(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("⛔ 此管理指令僅限管理員使用。")
    return False


async def require_banner_admin(update: Update):
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        return True
    if update.effective_message:
        await update.effective_message.reply_text("⛔ 廣告管理指令僅限指定管理員使用。")
    return False


async def add_banner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_banner_admin(update):
        return

    message = update.effective_message
    if not message or not message.photo:
        await message.reply_text(
            "請將一張圖片與 Caption 同時傳送，格式：\n"
            "/addbanner <跳轉連結> [版位分類]\n\n"
            "版位分類可用：all、cashflow、u2tw、tw2u。"
        )
        return

    parsed, error_message = parse_addbanner_command(message.caption or "")
    if error_message:
        await message.reply_text(error_message)
        return
    if not parsed:
        return

    file_id = message.photo[-1].file_id
    with get_ads_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO banners (file_id, link, placement, is_active, created_by, created_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (
                file_id,
                parsed["link"],
                parsed["placement"],
                update.effective_user.id,
                get_taipei_now(),
            ),
        )
        banner_id = cursor.lastrowid
        connection.commit()

    await message.reply_text(
        f"✅ 廣告已上架\n廣告 ID：{banner_id}\n"
        f"版位：{placement_label(parsed['placement'])}\n"
        f"連結：{parsed['link']}"
    )


async def list_banners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_banner_admin(update):
        return

    with get_ads_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, placement, link, created_at
            FROM banners
            WHERE is_active = 1
            ORDER BY id ASC
            """
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text("目前沒有上架中的廣告。")
        return

    lines = ["📋 目前上架中的廣告："]
    for row in rows:
        lines.append(
            f"ID：{row['id']}｜版位：{placement_label(row['placement'])}\n"
            f"連結：{row['link']}"
        )
    await update.effective_message.reply_text("\n\n".join(lines))


async def delete_banner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_banner_admin(update):
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("格式：/delbanner <廣告ID>")
        return

    banner_id = int(context.args[0])
    with get_ads_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE banners
            SET is_active = 0, deleted_by = ?, deleted_at = ?
            WHERE id = ? AND is_active = 1
            """,
            (update.effective_user.id, get_taipei_now(), banner_id),
        )
        connection.commit()

    if cursor.rowcount == 0:
        await update.effective_message.reply_text(
            f"⚠️ 找不到上架中的廣告 ID：{banner_id}。"
        )
        return
    await update.effective_message.reply_text(
        f"✅ 廣告 ID {banner_id} 已下架，資料仍保留於資料庫作為稽核紀錄。"
    )


async def set_spread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_SPREAD
    if not is_admin(update):
        return
    try:
        CURRENT_SPREAD = float(context.args[0])
        await update.message.reply_text(
            f"✅ **設定成功！**\n目前的加碼值已更新為：`+{CURRENT_SPREAD}`",
            parse_mode="Markdown",
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            f"⚠️ **格式錯誤**\n目前數值為：`+{CURRENT_SPREAD}`",
            parse_mode="Markdown",
        )


async def tc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    await update.message.reply_text("⏳ 正在為您結算分析，請稍候...")

    raw_bito = get_bitopro_price()
    cny_data = get_okx_cny_third_price()
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

        msg += "📊 **結算分析**：\n"

        if is_custom:
            diff_client = client_price - bot_best_rate
            pct_client = (diff_client / bot_best_rate) * 100
            client_word = "溢價" if diff_client > 0 else "折讓"
            client_sign = "+" if diff_client > 0 else ""

            msg += f"① 台銀中價的話，成本折讓為： {mid_price:.4f}-{bot_best_rate:.4f} = {diff_bank:.4f}\n"
            msg += f"{bank_word}{bank_sign}{pct_bank:.3f}%\n"
            msg += f"② 客戶價對標最佳成本：{diff_client:.4f}\n"
            msg += f"{client_word}{client_sign}{pct_client:.3f}%\n"
        else:
            msg += f"台銀中價的話，成本折讓為： {mid_price:.4f}-{bot_best_rate:.4f} = {diff_bank:.4f}\n"
            msg += f"{bank_word}{bank_sign}{pct_bank:.3f}%\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ **數據抓取失敗**，請稍後再試。")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await notify_admin(context, user)

    user_data = {
        "full_name": user.full_name,
        "id": user.id,
        "username": user.username if user.username else "無",
    }
    asyncio.get_running_loop().run_in_executor(
        None, log_to_google_sheet, user_data
    )

    keyboard = [
        ["🇨🇳 U兌人民幣", "💱 台幣兌人民幣", "🚀 韓幣兌U"],
        ["🇹🇼 U兌台幣", "🚀 台幣兌U", "🇲🇾 馬幣兌U"],
        ["💬 金流客服", "⚡ TRX能量租賃"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    welcome_text = (
        "✨ **KK 匯率報價助手已就緒**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "選擇查詢項目或直接聯絡『白資承兌商』@nk5219 👇"
    )
    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def send_price_message(update_or_query, context, mode):
    is_query = hasattr(update_or_query, "data")
    now = get_taipei_now()
    keyboard = get_function_inline_kb()
    func = (
        update_or_query.edit_message_text
        if is_query
        else update_or_query.message.reply_text
    )

    if mode == "cny":
        data = get_okx_cny_third_price()
        if data:
            msg = (
                "📋 **報價結果：🇨🇳 USDT 兌 人民幣**\n"
                f"🕒 查詢時間：`{now}`\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"👉 **即時報價：{data['price']:.2f} CNY**\n"
                f"👤 參考商家：{data['name']}\n\n"
                "⚠️ 來源：歐易全部 (第3檔)"
            )
            await func(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await func(
                "⚠️ **數據獲取失敗，請稍後再試。**",
                reply_markup=keyboard,
            )

    elif mode == "krw2u":
        data = get_bithumb_krw_price()
        source_name = "Bithumb 交易所"
        if not data:
            data = get_binance_krw_price()
            if data:
                source_name = "幣安 P2P"
        if data:
            price = data["price"]
            msg = (
                "📋 **報價結果：🚀 韓幣 兌 USDT**\n"
                f"🕒 查詢時間：`{now}`\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🏦 **即時報價：{int(price)} KRW**\n"
                "🤝 **若需韓幣現金面交服務**\n"
                f"💵 **+1%：為 {int(round(price * 1.01))} KRW**\n\n"
                f"⚠️ *來源：{source_name}*"
            )
            if "幣安" in source_name:
                msg += f"\n👤 參考商家：{data['name']}"
            await func(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await func("⚠️ **數據獲取失敗**", reply_markup=keyboard)

    elif mode in {"u2tw", "tw2u"}:
        raw = get_bitopro_price()
        if raw:
            final = (raw + CURRENT_SPREAD) if mode == "tw2u" else raw
            title = "🚀 台幣 兌 USDT" if mode == "tw2u" else "🇹🇼 USDT 兌 台幣"
            msg = (
                f"📋 **報價結果：{title}**\n"
                f"🕒 查詢時間：`{now}`\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"👉 **即時報價：{final:.2f} TWD**\n\n"
            )
            if mode == "tw2u":
                msg += "⚠️ 本報價參考台灣銀行美元現金銀行賣出價及當下C2C市場波動浮動調整。"
            else:
                msg += "⚠️ 報價是參考台灣幣托實時報價"
            await func(msg, parse_mode="Markdown", reply_markup=keyboard)
            await send_random_banner(update_or_query, context, mode)
        else:
            await func("⚠️ **數據獲取失敗，請稍後再試。**", reply_markup=keyboard)

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
            markup_info = (
                "🤝 **若需馬幣現金面交服務**\n"
                f"💵 **+1.5%：為 {markup_price:.2f} MYR**\n"
            )

        if price:
            msg = (
                "📋 **報價結果：🇲🇾 馬幣 兌 USDT**\n"
                f"🕒 查詢時間：`{now}`\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"👉 **即時報價：{price:.2f} MYR**\n"
                f"{markup_info}\n"
                f"⚠️ *來源：{source_name}*"
            )
            await func(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await func(
                "⚠️ **數據獲取失敗，請稍後再試**",
                reply_markup=keyboard,
            )

    elif mode == "tw2cny":
        raw_bito = get_bitopro_price()
        cny_data = get_okx_cny_third_price()
        if raw_bito and cny_data:
            final_rate = (raw_bito + CURRENT_SPREAD) / cny_data["price"]
            msg = (
                "📋 **報價結果：💱 台幣 兌 人民幣**\n"
                f"🕒 查詢時間：`{now}`\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"👉 **換算匯率：{final_rate:.3f}**\n"
                f"(每 1 人民幣 約需 {final_rate:.3f} 台幣)\n\n"
                "💡 *備註：是以USDT 本位計算之結果*"
            )
            await func(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await func("⚠️ **無法計算**", reply_markup=keyboard)


async def send_cashflow_service(update_or_query, context):
    text = (
        "💬 **金流客服**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "如需金流承兌、報價或面交服務，請聯絡：@nk5219"
    )
    if hasattr(update_or_query, "data"):
        await update_or_query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_function_inline_kb(),
        )
    else:
        await update_or_query.message.reply_text(text, parse_mode="Markdown")
    await send_random_banner(update_or_query, context, "cashflow")


async def send_trx_link(update):
    keyboard = [
        [
            InlineKeyboardButton(
                "⚡ 點擊前往 TRX 能量租賃",
                url="tg://resolve?domain=KKfreetron_Bot",
            )
        ]
    ]
    await update.message.reply_text(
        "⚡ **TRX 能量租賃服務**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "請點擊下方按鈕直接前往機器人：",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 只有帶有 /addbanner Caption 的圖片才進入廣告上架流程；其他圖片不回覆。
    caption = update.effective_message.caption or ""
    if caption.strip().lower().split("@", 1)[0].startswith("/addbanner"):
        await add_banner(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "🇨🇳 U兌人民幣":
        await send_price_message(update, context, "cny")
    elif text == "🚀 韓幣兌U":
        await send_price_message(update, context, "krw2u")
    elif text == "🇹🇼 U兌台幣":
        await send_price_message(update, context, "u2tw")
    elif text == "🚀 台幣兌U":
        await send_price_message(update, context, "tw2u")
    elif text == "💱 台幣兌人民幣":
        await send_price_message(update, context, "tw2cny")
    elif text == "🇲🇾 馬幣兌U":
        await send_price_message(update, context, "myr")
    elif text == "💬 金流客服":
        await send_cashflow_service(update, context)
    elif text == "⚡ TRX能量租賃":
        await send_trx_link(update)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode_map = {
        "switch_cny": "cny",
        "switch_krw2u": "krw2u",
        "switch_u2tw": "u2tw",
        "switch_tw2u": "tw2u",
        "switch_tw2cny": "tw2cny",
        "switch_myr": "myr",
    }
    if query.data in mode_map:
        await send_price_message(query, context, mode_map[query.data])
    elif query.data == "switch_cashflow":
        await send_cashflow_service(query, context)


async def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN 環境變數未設定")

    init_ads_db()
    print("🚀 Railway 機器人初始化中 (OKX C2C 第3檔 + SQLite 廣告系統)...")
    while True:
        app = None
        try:
            app = Application.builder().token(TELEGRAM_TOKEN).build()
            app.add_handler(CommandHandler("start", start))
            app.add_handler(CommandHandler("price", start))
            app.add_handler(CommandHandler("set", set_spread))
            app.add_handler(CommandHandler("tc", tc_command))
            app.add_handler(CommandHandler("addbanner", add_banner))
            app.add_handler(CommandHandler("banners", list_banners))
            app.add_handler(CommandHandler("delbanner", delete_banner))
            app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_handler(CallbackQueryHandler(callback_handler))

            await app.initialize()
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

            while True:
                await asyncio.sleep(2)
                if not app.updater.running:
                    break
        except Conflict:
            try:
                if app and app.updater.running:
                    await app.updater.stop()
                if app:
                    await app.stop()
                    await app.shutdown()
            except Exception:
                pass
            await asyncio.sleep(5)
        except Exception as error:
            print(f"Bot Error: {error}")
            try:
                if app and app.updater.running:
                    await app.updater.stop()
                if app:
                    await app.stop()
                    await app.shutdown()
            except Exception:
                pass
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"Startup Error: {error}")
