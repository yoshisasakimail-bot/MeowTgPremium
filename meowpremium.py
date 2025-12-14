import os
import time
import logging
import json
import datetime
import re
import uuid
import asyncio # NEW: Added for Broadcast delay
from typing import Dict, Optional
import gspread
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# ----------------- Logging -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- ENV / Globals -----------------
# MODIFIED: Define ADMIN_ID_DEFAULT for explicit fallback
ADMIN_ID_DEFAULT = 123456789
ADMIN_ID = int(os.environ.get("ADMIN_ID", ADMIN_ID_DEFAULT)) # Keep ADMIN_ID as the initial default/fallback from ENV
SHEET_ID = os.environ.get("SHEET_ID", "")
GSPREAD_SA_JSON = os.environ.get("GSPREAD_SA_JSON", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", "8080"))

# Sheets global objects (initialized later)
GSHEET_CLIENT: Optional[gspread.Client] = None
WS_USER_DATA = None
WS_CONFIG = None
WS_ORDERS = None

# Config cache
CONFIG_CACHE: Dict = {"data": {}, "ts": 0}
CONFIG_TTL_SECONDS = int(os.environ.get("CONFIG_TTL_SECONDS", "25"))

# Conversation states
(
    CHOOSING_PAYMENT_METHOD,
    WAITING_FOR_RECEIPT,
    SELECT_PRODUCT_PRICE,
    WAITING_FOR_PHONE,
    WAITING_FOR_USERNAME,
    SELECT_COIN_PACKAGE,
    # NEW: Admin Broadcast States
    BROADCAST_WAITING_FOR_CONTENT, 
    BROADCAST_CONFIRMATION,
) = range(8) # MODIFIED: Changed range from 6 to 8


# ------------ Helper: Retry wrapper for sheet init ----------------
def initialize_sheets(retries: int = 3, backoff: float = 2.0) -> bool:
    global GSHEET_CLIENT, WS_USER_DATA, WS_CONFIG, WS_ORDERS

    if not GSPREAD_SA_JSON:
        logger.error("GSPREAD_SA_JSON environment variable not set.")
        return False
    if not SHEET_ID:
        logger.error("SHEET_ID environment variable not set.")
        return False

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            sa_credentials = json.loads(GSPREAD_SA_JSON)
            GSHEET_CLIENT = gspread.service_account_from_dict(sa_credentials)
            sheet = GSHEET_CLIENT.open_by_key(SHEET_ID)

            WS_USER_DATA = sheet.worksheet("user_data")
            WS_CONFIG = sheet.worksheet("config")
            WS_ORDERS = sheet.worksheet("orders")

            logger.info("✅ Google Sheets initialized successfully.")
            return True
        except Exception as e:
            last_exc = e
            logger.warning(
                f"Attempt {attempt}/{retries} - failed to initialize Google Sheets: {e}"
            )
            time.sleep(backoff * attempt)

    logger.error("❌ Could not initialize Google Sheets after retries: %s", last_exc)
    return False


# ------------ Config reading & caching ----------------
def _read_config_sheet() -> Dict[str, str]:
    global WS_CONFIG
    out = {}
    if not WS_CONFIG:
        logger.warning("WS_CONFIG is not initialized.")
        return out
    try:
        records = WS_CONFIG.get_all_records()
        for item in records:
            k = item.get("key")
            v = item.get("value")
            if k is not None and v is not None:
                out[str(k).strip()] = str(v).strip()
    except Exception as e:
        logger.error("Error reading config sheet: %s", e)
    return out


def get_config_data(force_refresh: bool = False) -> Dict[str, str]:
    global CONFIG_CACHE
    now = time.time()
    if force_refresh or (now - CONFIG_CACHE["ts"] > CONFIG_TTL_SECONDS):
        CONFIG_CACHE["data"] = _read_config_sheet()
        CONFIG_CACHE["ts"] = now
    return CONFIG_CACHE["data"]


# NEW Helper: Get Admin ID from config sheet, falling back to global default
def get_dynamic_admin_id(config: Dict) -> int:
    """Retrieves ADMIN_ID from config sheet, falls back to global ADMIN_ID."""
    try:
        # Try to get from config sheet, fallback to global ADMIN_ID (which is 123456789 or from Render ENV)
        return int(config.get("admin_contact_id", ADMIN_ID))
    except (ValueError, TypeError):
        # If the value in the sheet is not a valid integer, use the global default
        logger.warning("admin_contact_id in sheet is invalid or missing. Using fallback: %s", ADMIN_ID)
        return ADMIN_ID


# NEW: Helper to check the current bot status from config sheet
def is_bot_closed():
    config = get_config_data()
    # Default to 'active' if key not found
    return config.get('bot_status', 'active').lower() == 'closed'

# NEW: Helper to update the bot status in the config sheet
def set_bot_status(status: str) -> bool:
    global WS_CONFIG, CONFIG_CACHE
    try:
        if WS_CONFIG is None:
            logger.error("WS_CONFIG is not initialized in set_bot_status.")
            return False
        
        # We need to find the 'bot_status' row and update its value
        records = WS_CONFIG.get_all_records()
        
        # Look for existing 'bot_status' key
        for i, record in enumerate(records):
            if record.get("key") == "bot_status":
                # Row index in gspread is 1-based, plus header row (i+2)
                WS_CONFIG.update_cell(i + 2, 2, status.lower()) 
                # After updating, clear the cache
                CONFIG_CACHE = {"data": {}, "ts": 0}
                return True
        
        # If 'bot_status' key doesn't exist, append a new row
        WS_CONFIG.append_row(["bot_status", status.lower()])
        # After appending, clear the cache
        CONFIG_CACHE = {"data": {}, "ts": 0}
        return True

    except Exception as e:
        logger.error("Failed to update bot status in config sheet: %s", e)
        return False


# ------------ User data helpers ----------------
def find_user_row(user_id: int) -> Optional[int]:
    global WS_USER_DATA
    if not WS_USER_DATA:
        return None
    try:
        cell = WS_USER_DATA.find(str(user_id), in_column=1)
        if cell:
            return cell.row
    except Exception as e:
        logger.debug("find_user_row exception: %s", e)
    return None


def get_user_data_from_sheet(user_id: int) -> Dict[str, str]:
    global WS_USER_DATA
    default = {"user_id": str(user_id), "username": "N/A", "coin_balance": "0", "registration_date": "N/A", "banned": "FALSE"}
    if not WS_USER_DATA:
        return default
    try:
        row = find_user_row(user_id)
        if not row:
            return default
        row_values = WS_USER_DATA.row_values(row)

        # FIX: Ensure coin_balance is clean (strip whitespace) before returning
        coin_balance_raw = row_values[2] if len(row_values) > 2 else "0"
        clean_coin_balance = coin_balance_raw.strip()
        
        data = {
            "user_id": row_values[0] if len(row_values) > 0 else str(user_id),
            "username": row_values[1] if len(row_values) > 1 else "N/A",
            "coin_balance": clean_coin_balance, # Use the cleaned value
            "registration_date": row_values[3] if len(row_values) > 3 else "N/A",
            "last_active": row_values[4] if len(row_values) > 4 else "",
            "total_purchase": row_values[5] if len(row_values) > 5 else "0",
            "banned": row_values[6] if len(row_values) > 6 else "FALSE",
        }
        return data
    except Exception as e:
        logger.error("Error get_user_data_from_sheet: %s", e)
        return default


def register_user_if_not_exists(user_id: int, username: str) -> None:
    global WS_USER_DATA
    if not WS_USER_DATA:
        logger.error("WS_USER_DATA not available.")
        return
    try:
        if find_user_row(user_id) is None:
            now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            new_row = [str(user_id), username or "N/A", "0", now, now, "0", "FALSE"]
            WS_USER_DATA.append_row(new_row, value_input_option="USER_ENTERED")
            logger.info("Registered new user %s", user_id)
    except Exception as e:
        logger.error("Error registering user: %s", e)


def update_user_balance(user_id: int, new_balance: int) -> bool:
    global WS_USER_DATA
    row = find_user_row(user_id)
    if not row:
        logger.error("update_user_balance: user row not found for %s", user_id)
        return False
    try:
        WS_USER_DATA.update_cell(row, 3, str(new_balance))
        WS_USER_DATA.update_cell(row, 5, datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        return True
    except Exception as e:
        logger.error("Failed to update user balance: %s", e)
        return False


def set_user_banned_status(user_id: int, banned: bool) -> bool:
    global WS_USER_DATA
    row = find_user_row(user_id)
    if not row:
        logger.error("set_user_banned_status: user row not found for %s", user_id)
        return False
    try:
        WS_USER_DATA.update_cell(row, 7, "TRUE" if banned else "FALSE")
        return True
    except Exception as e:
        logger.error("Failed to update banned status: %s", e)
        return False


def is_user_banned(user_id: int) -> bool:
    data = get_user_data_from_sheet(user_id)
    return str(data.get("banned", "FALSE")).upper() == "TRUE"


# ------------ Orders logging ----------------
def log_order(order: Dict) -> bool:
    global WS_ORDERS
    if not WS_ORDERS:
        logger.error("WS_ORDERS not initialized.")
        return False
    try:
        order_id = order.get("order_id") or str(uuid.uuid4())
        row = [
            order_id,
            order.get("user_id", ""),
            order.get("username", ""),
            order.get("product_key", ""),
            str(order.get("price_mmk", "")),
            order.get("phone", ""),
            order.get("premium_username", ""),
            order.get("status", "PENDING"),
            order.get("timestamp", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            order.get("notes", ""),
            order.get("processed_by", ""),
        ]
        WS_ORDERS.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        logger.error("log_order error: %s", e)
        return False


# ------------ Keyboards ----------------
def get_payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💸 Kpay (KBZ Pay)", callback_data="pay_kpay"),
                InlineKeyboardButton("💸 Wave Money", callback_data="pay_wave"),
            ]
        ]
    )


# MODIFIED: Show price in Coin instead of MMK
def get_product_keyboard(product_type: str) -> InlineKeyboardMarkup:
    config = get_config_data()
    keyboard_buttons = []
    prefix = f"{product_type}_"
    product_keys = sorted([k for k in config.keys() if k.startswith(prefix)])
    
    # NEW: Determine icon and get Coin Rate from config
    icon = '⭐' if product_type == 'star' else '❄️' # Using ❄️ as requested
    coin_rate_key = f"coin_rate_{product_type}"
    
    # Use 1000 if not found in config to avoid division by zero.
    try:
        # Get the coin rate (MMK price to 1 Coin)
        coin_rate_mmk = float(config.get(coin_rate_key, "1000")) 
    except ValueError:
        coin_rate_mmk = 1000.0

    if coin_rate_mmk <= 0:
         coin_rate_mmk = 1000.0
    
    for key in product_keys:
        price_mmk_str = config.get(key)
        if price_mmk_str:
            try:
                price_mmk = int(price_mmk_str)
            except ValueError:
                continue # Skip if MMK price is invalid
            
            # Calculate Coin Price: MMK Price / Coin Rate (MMK per 1 Coin)
            # Use ceiling to round up to the nearest integer Coin
            price_coin = int(price_mmk / coin_rate_mmk) 
            
            # Ensure price is at least 1 Coin
            price_coin = max(1, price_coin) 

            button_name = key.replace(prefix, "").replace("_", " ").title()
            
            # MODIFIED: Display Coin Price
            button_text = f"{icon} {button_name} ({price_coin} Coins)" 
            keyboard_buttons.append([InlineKeyboardButton(button_text, callback_data=f"{key}")])

    # Go back to the menu where the 'Premium & Star' button is visible
    keyboard_buttons.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")]) 
    return InlineKeyboardMarkup(keyboard_buttons)


# Coin package keyboard (reads config coinpkg_ keys, else uses defaults)
def get_coin_package_keyboard() -> InlineKeyboardMarkup:
    config = get_config_data()
    buttons = []
    # collect coinpkg_ keys
    coin_items = []
    for k, v in config.items():
        if k.startswith("coinpkg_"):
            try:
                coin_count = int(k.replace("coinpkg_", ""))
                price_mmk = int(v)
                coin_items.append((coin_count, price_mmk))
            except Exception:
                continue
    if not coin_items:
        # defaults:
        coin_items = [
            (1000, 2000),
            (2000, 4000),
            (5000, 10000),
            (10000, 20000),
        ]
    # sort by coin_count asc
    coin_items.sort(key=lambda x: x[0])
    for coins, mmk in coin_items:
        txt = f"🟡 {coins} Coins — {mmk} MMK"
        buttons.append([InlineKeyboardButton(txt, callback_data=f"buycoin_{coins}_{mmk}")])
    buttons.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


# MODIFIED: Reply keyboard now includes the new "Premium & Star" and "Help Center" button
ENGLISH_REPLY_KEYBOARD = [
    [KeyboardButton("👤 User Info"), KeyboardButton("💰 Payment Method")],
    [KeyboardButton("❓ Help Center"), KeyboardButton("✨ Premium & Star")] # Added new button
]
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(ENGLISH_REPLY_KEYBOARD, resize_keyboard=True, one_time_keyboard=False)

# NEW: Admin Only Reply Keyboard
ADMIN_REPLY_KEYBOARD = [
    [KeyboardButton("👤 User Info"), KeyboardButton("💰 Payment Method")],
    [KeyboardButton("❓ Help Center"), KeyboardButton("✨ Premium & Star")],
    [KeyboardButton("👾 Broadcast"), KeyboardButton("⚙️ Close to Selling")] # Added Broadcast and Close to Selling
]
ADMIN_MENU_KEYBOARD = ReplyKeyboardMarkup(ADMIN_REPLY_KEYBOARD, resize_keyboard=True, one_time_keyboard=False)


# New inline keyboard for the service selection (only Star and Premium)
PRODUCT_SELECTION_INLINE_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("⭐ Telegram Star", callback_data="product_star")],
        [InlineKeyboardButton("❄️ Telegram Premium", callback_data="product_premium")], # MODIFIED: Changed to ❄️
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")] # Added back button
    ]
)

# NEW: Reply keyboard for cancelling product purchase flow
CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Cancel Order")]],
    resize_keyboard=True,
    one_time_keyboard=True # Use one_time_keyboard for temporary keyboards
)

# NEW: Reply keyboard for cancelling Admin actions
ADMIN_CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Cancel Admin Action")]],
    resize_keyboard=True,
    one_time_keyboard=True
)


# ------------ Validation helpers ----------------
PHONE_RE = re.compile(r"^\d{8,15}$")
USERNAME_RE = re.compile(r"^@?([a-zA-Z0-9_]{5,32})$")

def normalize_username(raw: str) -> str:
    m = USERNAME_RE.match(raw)
    if not m:
        return ""
    return "@" + m.group(1)


def parse_amount_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    cleaned = text.replace(",", "").replace(".", "")
    m = re.search(r"(\d{3,9})", cleaned)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


# ------------ Handlers ----------------
# MODIFIED: start_command uses the new welcome message format and checks for admin and status
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: Global Bot Status Check
    if is_bot_closed():
        await update.message.reply_text("⛔ **Maintenance Mode**\n\n"
                                        "ဝန်ဆောင်မှုအား မွမ်းမံခြင်းပြုလုပ်နေသည်အတွက် အချိန်အနည်းငယ်စောင့်ဆိုင်းပြီးမှ ပြန်လာပါခင်ဗျာ။",
                                        parse_mode="Markdown")
        return

    user = update.effective_user
    register_user_if_not_exists(user.id, user.full_name)
    if is_user_banned(user.id):
        # Keep Burmese ban message as it is likely crucial for the audience
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားထားသည်။ Support ထံ ဆက်သွယ်ပါ။")
        return

    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    # NEW: Admin Check
    if user.id == admin_id_check:
        reply_markup = ADMIN_MENU_KEYBOARD # Use Admin Keyboard
        welcome_text = (
            f"Hello, 👑**{user.full_name}**\n\n"
            f"**ADMIN MODE ACTIVATED.**\n"
            f"Use the menu below to manage or use commands like /ban, /unban."
        )
    else:
        reply_markup = MAIN_MENU_KEYBOARD # Use User Keyboard
        welcome_text = (
            f"Hello, 👑**{user.full_name}**\n\n"
            f"🧸Welcome — Meow Telegram Bot 🫶\n"
            f"To make a purchase with excellent service and advanced functionality, choose from the menu below."
        )

    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")


# NEW: Function to display the Star/Premium inline buttons, triggered by the new Reply Button
async def show_product_inline_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: Global Bot Status Check
    if is_bot_closed():
        await update.message.reply_text("⛔ **Maintenance Mode**\n\n"
                                        "ဝန်ဆောင်မှုအား မွမ်းမံခြင်းပြုလုပ်နေသည်အတွက် အချိန်အနည်းငယ်စောင့်ဆိုင်းပြီးမှ ပြန်လာပါခင်ဗျာ။",
                                        parse_mode="Markdown")
        return ConversationHandler.END

    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return

    text = "✨ Available Services (Star & Premium):\nPlease select an option below:"
    
    # Send the inline keyboard only
    await update.message.reply_text(text, reply_markup=PRODUCT_SELECTION_INLINE_KEYBOARD)


# MODIFIED: Does not show service menu afterwards
async def handle_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: Global Bot Status Check
    if is_bot_closed():
        await update.message.reply_text("⛔ **Maintenance Mode**\n\n"
                                        "ဝန်ဆောင်မှုအား မွမ်းမံခြင်းပြုလုပ်နေသည်အတွက် အချိန်အနည်းငယ်စောင့်ဆိုင်းပြီးမှ ပြန်လာပါခင်ဗျာ။",
                                        parse_mode="Markdown")
        return
        
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return
    data = get_user_data_from_sheet(user.id)
    info_text = (
        f"👤 **User Information**\n\n"
        f"🔸 **Your ID:** `{data.get('user_id')}`\n"
        f"🔸 **Username:** {data.get('username')}\n"
        f"🔸 **Coin Balance:** **{data.get('coin_balance')}**\n"
        f"🔸 **Registered Since:** {data.get('registration_date')}\n"
        f"🔸 **Banned:** {data.get('banned')}\n"
    )
    # Use menu_back to return to the main menu (no inline service menu here)
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")]])
    await update.message.reply_text(info_text, reply_markup=back_keyboard, parse_mode="Markdown")


# MODIFIED: Does not show service menu afterwards
async def handle_help_center(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: Global Bot Status Check
    if is_bot_closed():
        await update.message.reply_text("⛔ **Maintenance Mode**\n\n"
                                        "ဝန်ဆောင်မှုအား မွမ်းမံခြင်းပြုလုပ်နေသည်အတွက် အချိန်အနည်းငယ်စောင့်ဆိုင်းပြီးမှ ပြန်လာပါခင်ဗျာ။",
                                        parse_mode="Markdown")
        return

    config = get_config_data()
    admin_username = config.get("admin_contact_username", "@Admin")
    help_text = (
        "❓ **Help Center**\n\n"
        f"For assistance, contact the administrator:\nAdmin Contact: **{admin_username}**\n\n"
        "We will respond as soon as possible."
    )
    # Use menu_back to return to the main menu (no inline service menu here)
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")]])
    if update.callback_query:
        # If triggered from a previous inline keyboard (which shouldn't happen now), reply.
        await update.callback_query.message.reply_text(help_text, reply_markup=back_keyboard, parse_mode="Markdown")
    else:
        # Primary entry point from the reply button
        await update.message.reply_text(help_text, reply_markup=back_keyboard, parse_mode="Markdown")


# ----------- Payment Flow (coin package -> payment method -> receipt) -----------
# MODIFIED: Entry point for conversation from the reply keyboard.
async def handle_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: Global Bot Status Check
    if is_bot_closed():
        await update.message.reply_text("⛔ **Maintenance Mode**\n\n"
                                        "ဝန်ဆောင်မှုအား မွမ်းမံခြင်းပြုလုပ်နေသည်အတွက် အချိန်အနည်းငယ်စောင့်ဆိုင်းပြီးမှ ပြန်လာပါခင်ဗျာ။",
                                        parse_mode="Markdown")
        return ConversationHandler.END

    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return ConversationHandler.END
    # Show coin package keyboard first
    if update.callback_query:
        await update.callback_query.message.reply_text("💰 Select Coin Package:", reply_markup=get_coin_package_keyboard())
    else:
        # Primary entry point from the reply button
        await update.message.reply_text("💰 Select Coin Package:", reply_markup=get_coin_package_keyboard())
    return SELECT_COIN_PACKAGE


async def handle_coin_package_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback data format: buycoin_<coins>_<mmk>
    parts = query.data.split("_")
    try:
        coins = int(parts[1])
        mmk = int(parts[2])
    except Exception:
        await query.message.reply_text("Invalid package selected.")
        return ConversationHandler.END
    # store package selection
    context.user_data["selected_coinpkg"] = {"coins": coins, "mmk": mmk}
    # Then show payment method buttons
    await query.message.edit_text(
        f"💳 You selected **{coins} Coins — {mmk} MMK**.\nPlease choose payment method:",
        reply_markup=get_payment_keyboard(),
        parse_mode="Markdown",
    )
    return CHOOSING_PAYMENT_METHOD


async def start_payment_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cd = query.data  # e.g., pay_kpay
    parts = cd.split("_")
    if len(parts) < 2:
        await query.message.reply_text("Invalid payment method selected.")
        return ConversationHandler.END
    payment_method = parts[1]
    config = get_config_data()
    admin_name = config.get(f"{payment_method}_name", "Admin Name")
    phone_number = config.get(f"{payment_method}_phone", "09XXXXXXXXX")
    # Show selected package summary if exists
    pkg = context.user_data.get("selected_coinpkg")
    pkg_text = ""
    if pkg:
        pkg_text = f"\nPackage: {pkg['coins']} Coins — {pkg['mmk']} MMK\n"
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Payment Menu", callback_data="payment_back")]])
    transfer_text = (
        f"✅ Please transfer via **{payment_method.upper()}** as follows:{pkg_text}\n"
        f"Name: **{admin_name}**\n"
        f"Phone Number: **{phone_number}**\n\n"
        "Please *send the receipt (screenshot or text)* here after transfer. If amount is visible, bot will try to detect it automatically."
    )
    await query.message.reply_text(transfer_text, reply_markup=back_keyboard, parse_mode="Markdown")
    return WAITING_FOR_RECEIPT


async def back_to_payment_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Go back to coin package selection, not just payment methods
    await query.message.edit_text("💰 Select Coin Package:", reply_markup=get_coin_package_keyboard())
    return SELECT_COIN_PACKAGE


async def receive_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return ConversationHandler.END

    config = get_config_data()
    # BUG FIX: Get Admin ID from config data, falling back to global ADMIN_ID
    admin_contact_id = get_dynamic_admin_id(config)
    
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    # FIX: Use a short Unix timestamp for callback data to avoid Button_data_invalid error
    short_ts = int(time.time())
    
    receipt_meta = {
        "from_user_id": user.id,
        "from_username": user.username or user.full_name,
        "timestamp": timestamp,
        "short_ts": short_ts, # Store short timestamp
        "package": context.user_data.get("selected_coinpkg"),
    }
    context.user_data["last_receipt_meta"] = receipt_meta

    detected_amount = None
    try:
        if update.message.photo:
            # forward photo
            await update.message.forward(chat_id=admin_contact_id)
            detected_amount = parse_amount_from_text(update.message.caption or "")
        else:
            text = update.message.text or ""
            detected_amount = parse_amount_from_text(text)
            forwarded_text = f"📥 Receipt (text) from @{user.username or user.full_name} (id:{user.id})\nTime: {timestamp}\n\n{text}"
            await context.bot.send_message(chat_id=admin_contact_id, text=forwarded_text)

        # Build approve buttons with amounts from config or defaults
        amounts_cfg = config.get("receipt_approve_amounts", "")
        # MODIFIED: Use the new requested default amounts (Request 1)
        default_choices = [19000, 20000, 50000, 100000]

        if amounts_cfg:
            try:
                # ဤနေရာတွင် စာလုံးမှားယွင်းပါက ValueError တက်ပါသည်။
                choices = [int(x.strip()) for x in amounts_cfg.split(",") if x.strip().isdigit()]
            except Exception:
                # Configuration မှားယွင်းပါက Default သို့ ပြန်သွားပါမည်။
                choices = default_choices

        else:
            # MODIFIED: Use the requested amounts as the final fallback default
            choices = default_choices

        # If a detected amount exists and is not one of the choices, prepend it
        if detected_amount and detected_amount not in choices:
            choices = [detected_amount] + choices

        # Ensure unique and sorted (optional: but useful for consistent UI)
        choices = sorted(list(set(choices)), reverse=True)

        kb_rows = []
        row = []
        for i, amt in enumerate(choices):
            # FIX: Use short prefix 'rpa' (Receipt Process Approve)
            row.append(InlineKeyboardButton(f"✅ Approve {amt} MMK", callback_data=f"rpa|{user.id}|{short_ts}|{amt}"))
            if len(row) == 2: # Keep two buttons per row as requested
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        # FIX: Use short prefix 'rpd' (Receipt Process Deny)
        kb_rows.append([InlineKeyboardButton("❌ Deny", callback_data=f"rpd|{user.id}|{short_ts}")])

        await context.bot.send_message(
            chat_id=admin_contact_id,
            text=f"📥 Receipt from @{user.username or user.full_name} (id:{user.id}) Time: {timestamp}",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
    except Exception as e:
        # Error တက်ပါက Bot မှ Admin သို့ Approval Button များပို့ရန် မအောင်မြင်ပါ။
        logger.error("Failed to send receipt buttons to admin: %s", e)
        # The receipt forward succeeded but the buttons failed, so we give a specific error.
        await update.message.reply_text("❌ Could not forward receipt to admin. Please try again later. Please check your ADMIN_ID and Bot permissions.")
        return ConversationHandler.END

    await update.message.reply_text("💌 Receipt sent to Admin. You will be notified after approval.")
    return ConversationHandler.END


# Admin callbacks for receipts (updated to handle short_ts and new messages)
async def admin_approve_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # rpa|<user_id>|<short_ts>|<amount>
    parts = data.split("|")
    if len(parts) < 4:
        await query.message.reply_text("Invalid admin action.")
        return

    # short_ts_str now contains the Unix timestamp (string format)
    _, user_id_str, short_ts_str, amount_str = parts[0], parts[1], parts[2], parts[3]
    try:
        user_id = int(user_id_str)
        approved_amount = int(amount_str)
        # Convert short_ts back to human-readable format for logging
        unix_to_dt = datetime.datetime.fromtimestamp(int(short_ts_str))
        ts_human_readable = unix_to_dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        await query.message.reply_text("Invalid parameters.")
        return

    config = get_config_data()
    # MODIFIED: Get ADMIN_ID from config data for authorization check
    admin_id_check = get_dynamic_admin_id(config)
    
    if query.from_user.id != admin_id_check:
        await query.message.reply_text("You are not authorized to perform this action.")
        return

    
    # ratio: mmk -> coins (user requested: 1 MMK = 0.5 coin)
    try:
        ratio = float(config.get("mmk_to_coins_ratio", "0.5"))
    except Exception:
        ratio = 0.5
    coins_to_add = int(approved_amount * ratio)

    user_data = get_user_data_from_sheet(user_id)
    try:
        # Coin Balance ကို fetch လုပ်ရာမှာ clean လုပ်ပြီးသားဖြစ်တဲ့အတွက်၊ ဒီနေရာမှာ မှန်ကန်စွာ ပေါင်းသွားပါပြီ။
        current_coins = int(user_data.get("coin_balance", "0"))
    except ValueError:
        current_coins = 0
    new_balance = current_coins + coins_to_add

    ok = update_user_balance(user_id, new_balance)
    if not ok:
        await query.message.reply_text("Failed to update user balance in sheet.")
        return

    order = {
        "order_id": str(uuid.uuid4()),
        "user_id": user_id,
        "username": user_data.get("username", ""),
        "product_key": "COIN_TOPUP",
        "price_mmk": approved_amount,
        "phone": "",
        "premium_username": "",
        "status": "APPROVED_RECEIPT",
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": f"Receipt approved by admin {query.from_user.id} at {ts_human_readable}", # Use human-readable time
        "processed_by": str(query.from_user.id),
    }
    log_order(order)
    
    # Get the user's username for the admin message
    display_username = user_data.get("username", str(user_id))
    # Prepend '@' if it's a valid username (not 'N/A' or just the user ID string)
    if display_username != "N/A" and not str(user_id) in display_username:
        display_username = f"@{display_username}"
    # If still 'N/A', just use the user ID as a fallback
    if display_username == "N/A":
         display_username = f"id:{user_id}" 

    try:
        # User Notification (Request 3)
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🎉Your balance {coins_to_add} coin top up Successful. New balance: {new_balance} Coins.",
        )
        
        # Admin Notification: Edit the original message to show 'Processed' status (NEW)
        # --------------------------------------------------------------------------------
        admin_username = query.from_user.username or "Admin"
        
        # Build the processed message
        processed_msg = (
            f"✅ **APPROVED: {approved_amount} MMK**\n"
            f"💰 Added **{coins_to_add} Coins** to user.\n"
            f"User: {display_username} (id:`{user_id}`)\n"
            f"Processed by: @{admin_username} (id:`{query.from_user.id}`)\n"
            f"Time: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        
        # Edit the previous message
        await query.message.edit_text(
            text=processed_msg,
            parse_mode="Markdown"
        )
        # --------------------------------------------------------------------------------
        
    except Exception as e:
        logger.error("Failed to notify user or edit admin message after approval: %s", e)
        await query.message.reply_text(f"Approved but failed to notify user or edit message. Error: {e}")


async def admin_deny_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data # rpd|<user_id>|<short_ts>
    parts = data.split("|")
    if len(parts) < 3:
        await query.message.reply_text("Invalid admin action.")
        return
    
    # short_ts_str now contains the Unix timestamp (string format)
    _, user_id_str, short_ts_str = parts
    try:
        user_id = int(user_id_str)
        # Convert short_ts back to human-readable format for logging
        unix_to_dt = datetime.datetime.fromtimestamp(int(short_ts_str))
        ts_human_readable = unix_to_dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        await query.message.reply_text("Invalid user id or timestamp.")
        return

    config = get_config_data()
    # MODIFIED: Get ADMIN_ID from config data for authorization check
    admin_id_check = get_dynamic_admin_id(config)

    if query.from_user.id != admin_id_check:
        await query.message.reply_text("You are not authorized to perform this action.")
        return

    order = {
        "order_id": str(uuid.uuid4()),
        "user_id": user_id,
        "username": get_user_data_from_sheet(user_id).get("username", ""),
        "product_key": "COIN_TOPUP",
        "price_mmk": 0,
        "phone": "",
        "premium_username": "",
        "status": "DENIED_RECEIPT",
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": f"Receipt denied by admin {query.from_user.id} at {ts_human_readable}", # Use human-readable time
        "processed_by": str(query.from_user.id),
    }
    log_order(order)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Admin has denied your payment/receipt. Please contact support or retry the payment.",
        )
        
        # Admin Notification: Edit the original message to show 'Denied' status (NEW)
        admin_username = query.from_user.username or "Admin"
        processed_msg = (
            f"❌ **DENIED**\n"
            f"User: {get_user_data_from_sheet(user_id).get('username', f'id:{user_id}')}\n"
            f"Processed by: @{admin_username} (id:`{query.from_user.id}`)\n"
            f"Time: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )

        await query.message.edit_text(
            text=processed_msg,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error("Failed to notify user or edit admin message after denial: %s", e)
        await query.message.reply_text("Denied but failed to notify user or edit message.")


# ----------- Product purchase flow (NEW CANCEL BUTTONS ADDED) -----------
async def start_product_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    if len(parts) < 2:
        await query.message.reply_text("Invalid product selection.")
        return ConversationHandler.END
    product_type = parts[1]
    context.user_data["product_type"] = product_type
    keyboard = get_product_keyboard(product_type)
    try:
        # Edit the message with the service menu to show product selection
        await query.message.edit_text(
            f"Please select the duration/amount for the **Telegram {product_type.upper()}** purchase:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception:
        await query.message.reply_text(
            f"Please select the duration/amount for the **Telegram {product_type.upper()}** purchase:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    return SELECT_PRODUCT_PRICE


async def select_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    selected_key = query.data
    context.user_data["product_key"] = selected_key
    
    # NEW: Send the initial message without the keyboard, then send the keyboard as a new message
    try:
        await query.message.edit_text(
            f"You selected *{selected_key.replace('_',' ').upper()}*.\n"
            "Please send the **Telegram Phone Number** for the service (digits only).",
            parse_mode="Markdown",
        )
    except Exception:
        await query.message.reply_text(
            f"You selected *{selected_key.replace('_',' ').upper()}*.\n"
            "Please send the **Telegram Phone Number** for the service (digits only).",
            parse_mode="Markdown",
        )
        
    # NEW: Send the cancel keyboard to the user
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text="If you want to stop the order, click '❌ Cancel Order'.",
        reply_markup=CANCEL_KEYBOARD
    )
    return WAITING_FOR_PHONE


async def validate_phone_and_ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if PHONE_RE.match(text):
        context.user_data["premium_phone"] = text
        await update.message.reply_text(
            f"Thank you. Now please send the **Telegram Username** associated with {text} (start with @ or plain username)."
        )
        # NEW: Send the cancel keyboard again
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="If you want to stop the order, click '❌ Cancel Order'.",
            reply_markup=CANCEL_KEYBOARD
        )
        return WAITING_FOR_USERNAME
    else:
        await update.message.reply_text("❌ Invalid phone. Send digits only (8-15 digits).")
        # Keep the cancel keyboard visible
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="If you want to stop the order, click '❌ Cancel Order'.",
            reply_markup=CANCEL_KEYBOARD
        )
        return WAITING_FOR_PHONE


async def finalize_product_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    # NEW: Global Bot Status Check
    if is_bot_closed():
        await update.message.reply_text("⛔ **Maintenance Mode**\n\n"
                                        "ဝန်ဆောင်မှုအား မွမ်းမံခြင်းပြုလုပ်နေသည်အတွက် အချိန်အနည်းငယ်စောင့်ဆိုင်းပြီးမှ ပြန်လာပါခင်ဗျာ။",
                                        reply_markup=MAIN_MENU_KEYBOARD,
                                        parse_mode="Markdown")
        return ConversationHandler.END


    if is_user_banned(user_id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။", reply_markup=MAIN_MENU_KEYBOARD)
        return ConversationHandler.END

    product_key = context.user_data.get("product_key")
    premium_phone = context.user_data.get("premium_phone", "")
    raw_username = (update.message.text or "").strip()
    premium_username = normalize_username(raw_username)

    if not premium_username:
        await update.message.reply_text("❌ Invalid username format. Please try again.")
        # Keep the cancel keyboard visible
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="If you want to stop the order, click '❌ Cancel Order'.",
            reply_markup=CANCEL_KEYBOARD
        )
        return WAITING_FOR_USERNAME

    if not product_key:
        await update.message.reply_text("❌ No product selected. Please start again.", reply_markup=MAIN_MENU_KEYBOARD)
        return ConversationHandler.END

    config = get_config_data()
    price_mmk_str = config.get(product_key)
    if price_mmk_str is None:
        await update.message.reply_text("❌ Price for this product not found in config.", reply_markup=MAIN_MENU_KEYBOARD)
        return ConversationHandler.END

    try:
        price_mmk_needed = int(price_mmk_str)
    except ValueError:
        await update.message.reply_text("❌ Product MMK price in config is invalid.", reply_markup=MAIN_MENU_KEYBOARD)
        return ConversationHandler.END

    # --- Calculate Coin Price needed ---
    product_type = product_key.split('_')[0]
    coin_rate_key = f"coin_rate_{product_type}"
    try:
        coin_rate_mmk = float(config.get(coin_rate_key, "1000")) 
    except ValueError:
        coin_rate_mmk = 1000.0

    if coin_rate_mmk <= 0:
         coin_rate_mmk = 1000.0
         
    price_needed_coins = int(price_mmk_needed / coin_rate_mmk) 
    price_needed_coins = max(1, price_needed_coins) # Ensure at least 1 coin

    # --- Check Balance ---
    user_data = get_user_data_from_sheet(user_id)
    try:
        user_coins = int(user_data.get("coin_balance", "0"))
    except ValueError:
        user_coins = 0

    if user_coins < price_needed_coins:
        await update.message.reply_text(
            f"❌ Insufficient coin balance. You need {price_needed_coins} Coins but have {user_coins} Coins. Use '💰 Payment Method' to top up.",
            reply_markup=MAIN_MENU_KEYBOARD
        )
        order = {
            "order_id": str(uuid.uuid4()),
            "user_id": user_id,
            "username": user_data.get("username", ""),
            "product_key": product_key,
            "price_mmk": price_mmk_needed,
            "phone": premium_phone,
            "premium_username": premium_username,
            "status": "FAILED_INSUFFICIENT_FUNDS",
            "notes": "User attempted purchase without sufficient coins.",
        }
        log_order(order)
        return ConversationHandler.END

    # --- Deduct Coin ---
    new_balance = user_coins - price_needed_coins
    ok = update_user_balance(user_id, new_balance)
    if not ok:
        await update.message.reply_text("❌ Failed to deduct coins. Please contact admin.", reply_markup=MAIN_MENU_KEYBOARD)
        return ConversationHandler.END

    # --- Log Order ---
    order = {
        "order_id": str(uuid.uuid4()),
        "user_id": user_id,
        "username": user_data.get("username", ""),
        "product_key": product_key,
        "price_mmk": price_mmk_needed, # Log the MMK price for consistency
        "phone": premium_phone,
        "premium_username": premium_username,
        "status": "ORDER_PLACED",
        "notes": f"Order placed and {price_needed_coins} Coins deducted.",
    }
    log_order(order)
    
    config = get_config_data()
    # MODIFIED: Get ADMIN_ID from config data
    admin_id_check = get_dynamic_admin_id(config)


    await update.message.reply_text(
        f"✅ Order successful! **{price_needed_coins} Coins** have been deducted for {product_key.replace('_',' ').upper()}.\n"
        f"New balance: {new_balance} Coins. Please wait while service is processed.",
        reply_markup=MAIN_MENU_KEYBOARD # Show main menu keyboard on success
    )
    try:
        # MODIFIED: Updated Admin notification message formatting (Request 6)
        admin_msg = (
            f"🛒 New Order\n\n"
            
            f"📝 Order ID: `{order['order_id']}`\n"
            f"♦️ User: @{user.username or user.full_name} (id:`{user_id}`)\n"
            f"________________________________\n\n"
            
            f"💾 Product: `{product_key}`\n"
            f"💰 Price: {price_mmk_needed} MMK ({price_needed_coins} Coins deducted)\n\n"
            
            f"☎️ Phone: `{premium_phone}`\n"
            f"🎭 Username: {premium_username}\n"
            f"________________________________"
        )
        
        await context.bot.send_message(chat_id=admin_id_check, text=admin_msg, parse_mode="Markdown")
    except Exception as e:
        logger.error("Failed to notify admin about order: %s", e)

    return ConversationHandler.END

# NEW: Handler to cancel the product purchase conversation
async def cancel_product_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Order cancelled. You have returned to the main menu.",
        reply_markup=MAIN_MENU_KEYBOARD
    )
    return ConversationHandler.END


# MODIFIED: Global back to service menu (menu_back) now only returns to the appropriate Reply Keyboard
async def back_to_service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    if user_id == admin_id_check:
        reply_markup = ADMIN_MENU_KEYBOARD # Admin: Show Admin Keyboard
        welcome_text = "Returned to Admin Menu."
    else:
        reply_markup = MAIN_MENU_KEYBOARD # User: Show Main Menu Keyboard
        welcome_text = "Welcome back to the main menu. Choose from the options below."


    try:
        # Delete the previous inline message if possible
        await query.message.delete()
    except Exception:
        pass # Ignore error if delete fails (e.g., message is too old)

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=welcome_text,
        reply_markup=reply_markup, # Use conditional markup
    )
    return ConversationHandler.END # Exit any active conversation state


# NEW: Handler for '⚙️ Close to Selling' button
async def start_close_to_selling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    if user.id != admin_id_check:
        await update.message.reply_text("You are not authorized to use this feature.")
        return

    current_status = config.get('bot_status', 'active').upper()
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Active (Open Bot)", callback_data="set_status_active")],
        [InlineKeyboardButton("⛔ Close (Maintenance)", callback_data="set_status_closed")]
    ])
    
    await update.message.reply_text(
        f"⚙️ **Bot Status Management**\n\n"
        f"Current Status: **{current_status}**\n\n"
        f"Choose the new status:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# NEW: Callback handler for setting the status
async def set_new_bot_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_") # e.g., set_status_active
    new_status = data[-1]
    
    is_success = set_bot_status(new_status)
    
    if is_success:
        status_text = "🟢 ACTIVE (Bot is Open)" if new_status == 'active' else "⛔ CLOSED (Maintenance Mode)"
        
        await query.message.edit_text(
            f"✅ **Status Updated!**\n\n"
            f"New Bot Status is: **{status_text}**.\n"
            f"Users will now see the appropriate message.",
            parse_mode="Markdown"
        )
    else:
        await query.message.edit_text("❌ Failed to update the bot status in the Google Sheet.")


# NEW: Handler for '👾 Broadcast' button
async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)

    if user.id != admin_id_check:
        await update.message.reply_text("You are not authorized to use this feature.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 **Broadcast Mode**\n\n"
        "Please send the **Message and Photo** you want to broadcast to all users in *one single message*.\n"
        "The photo must be attached to the message with a caption (text).\n\n"
        "Click '❌ Cancel Admin Action' to stop.",
        reply_markup=ADMIN_CANCEL_KEYBOARD,
        parse_mode="Markdown"
    )
    return BROADCAST_WAITING_FOR_CONTENT


# NEW: Handler to receive content and start confirmation
async def receive_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Must contain both photo and caption/text
    if not (update.message.photo and update.message.caption):
        await update.message.reply_text(
            "❌ Invalid input. Please send the message and photo in *one message* (photo with caption/text)."
        )
        return BROADCAST_WAITING_FOR_CONTENT # Wait again

    # Store content
    context.user_data["broadcast_photo_id"] = update.message.photo[-1].file_id # Largest photo size
    context.user_data["broadcast_caption"] = update.message.caption
    
    # Get total user count (excluding header row)
    total_users_count = len(WS_USER_DATA.get_all_records()) if WS_USER_DATA else 0
    if total_users_count > 0:
        # Subtract the header row.
        total_users_count -= 1 
    
    # Send confirmation message with the content
    confirm_text = (
        f"✅ **Confirmation**\n\n"
        f"You are about to send this message to **ALL {total_users_count} users** (approximate, excluding header row and banned/admin).\n"
        f"Do you want to proceed?"
    )
    confirmation_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Send Broadcast NOW", callback_data="broadcast_confirm_yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="broadcast_confirm_no")]
    ])
    
    # Send the photo and caption back to admin for confirmation
    await context.bot.send_photo(
        chat_id=user.id,
        photo=context.user_data["broadcast_photo_id"],
        caption=confirm_text + "\n\n---\n" + context.user_data["broadcast_caption"],
        reply_markup=confirmation_kb,
        parse_mode="Markdown"
    )

    return BROADCAST_CONFIRMATION


# NEW: Handler for final confirmation and execution
async def finalize_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "broadcast_confirm_no":
        await query.message.edit_text("❌ Broadcast cancelled.")
        # Send Admin Menu Keyboard explicitly since this is a CallbackQueryHandler
        await context.bot.send_message(query.from_user.id, "Returned to Admin Menu.", reply_markup=ADMIN_MENU_KEYBOARD)
        return ConversationHandler.END

    # Start sending
    await query.message.edit_text("⏳ **Broadcast started...** This may take a few minutes.", parse_mode="Markdown")
    
    photo_id = context.user_data.get("broadcast_photo_id")
    caption = context.user_data.get("broadcast_caption")
    
    # Fetch Admin ID once
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    user_rows = WS_USER_DATA.get_all_records()
    total_users = 0
    success_count = 0
    fail_count = 0
    
    for row in user_rows:
        try:
            target_id = int(row.get("user_id"))
            if target_id == admin_id_check:
                continue # Skip admin
            if row.get("banned", "FALSE").upper() == "TRUE":
                continue # Skip banned users
            
            total_users += 1
            # Send photo/caption
            await context.bot.send_photo(
                chat_id=target_id,
                photo=photo_id,
                caption=caption,
                parse_mode="Markdown" # Ensure markdown is supported in broadcast
            )
            success_count += 1
            # Add a small delay to respect Telegram's rate limits
            await asyncio.sleep(0.05) 
        except Exception as e:
            # Catch exceptions like 'Bot blocked by user' or 'Chat not found'
            if "blocked by the user" in str(e) or "chat not found" in str(e):
                 logger.info("User %s blocked the bot. Skipping.", row.get("user_id"))
            else:
                 logger.error("Failed to send broadcast to user %s: %s", row.get("user_id"), e)
                 fail_count += 1

    final_msg = (
        f"✅ **Broadcast Complete!**\n"
        f"Total Users Targeted: {total_users}\n"
        f"Successful Sends: {success_count}\n"
        f"Failed/Blocked Sends: {fail_count}"
    )
    
    await query.message.reply_text(final_msg, reply_markup=ADMIN_MENU_KEYBOARD, parse_mode="Markdown")
    return ConversationHandler.END


# NEW: Cancel handler for Admin actions
async def cancel_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Admin action cancelled. You have returned to the Admin Menu.",
        reply_markup=ADMIN_MENU_KEYBOARD
    )
    return ConversationHandler.END


# Admin commands (ban/unban) - Updated to use config ID
async def admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    user = update.effective_user
    if user.id != admin_id_check:
        await update.message.reply_text("You are not authorized.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        target = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user id.")
        return
    ok = set_user_banned_status(target, True)
    if ok:
        await update.message.reply_text(f"User {target} banned.")
    else:
        await update.message.reply_text("Failed to ban user.")


async def admin_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    user = update.effective_user
    if user.id != admin_id_check:
        await update.message.reply_text("You are not authorized.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        target = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user id.")
        return
    ok = set_user_banned_status(target, False)
    if ok:
        await update.message.reply_text(f"User {target} unbanned.")
    else:
        await update.message.reply_text("Failed to unban user.")


# Error handler (sanitized) - Updated to use config ID
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err_type = type(context.error).__name__ if context.error else "UnknownError"
    err_msg = str(context.error)[:1000] if context.error else "No details"
    logger.error("Exception while handling an update: %s: %s", err_type, err_msg)
    
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)

    try:
        await context.bot.send_message(
            chat_id=admin_id_check,
            text=f"🚨 Bot Error: {err_type}\n{err_msg}",
        )
    except Exception:
        pass


# --------------- Main ---------------
def main():
    ok = initialize_sheets()
    if not ok:
        logger.error("Bot cannot start due to Google Sheets initialization failure.")
        return

    if not BOT_TOKEN:
        logger.error("Missing BOT_TOKEN environment variable.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cancel", cancel_product_order)) # NEW: Handle /cancel command

    # Admin commands
    application.add_handler(CommandHandler("ban", admin_ban_user))
    application.add_handler(CommandHandler("unban", admin_unban_user))

    # Payment Conversation Handler (entry: Payment Method button)
    payment_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("💰 Payment Method"), handle_payment_method)],
        states={
            SELECT_COIN_PACKAGE: [
                CallbackQueryHandler(handle_coin_package_select, pattern=r"^buycoin_")
            ],
            CHOOSING_PAYMENT_METHOD: [
                CallbackQueryHandler(start_payment_conv, pattern=r"^pay_"),
                CallbackQueryHandler(back_to_payment_menu, pattern=r"^payment_back$"),
            ],
            WAITING_FOR_RECEIPT: [
                MessageHandler(filters.PHOTO | filters.TEXT, receive_receipt),
                CallbackQueryHandler(back_to_payment_menu, pattern=r"^payment_back$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(back_to_service_menu, pattern=r"^menu_back$")], # Updated fallback to main menu
        allow_reentry=True,
    )
    application.add_handler(payment_conv_handler)

    # Product Conversation Handler (entry: Inline buttons)
    product_purchase_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_product_purchase, pattern=r"^product_")],
        states={
            SELECT_PRODUCT_PRICE: [
                CallbackQueryHandler(select_product_price, pattern=r"^(star_|premium_).*"),
                CallbackQueryHandler(back_to_service_menu, pattern=r"^menu_back$"),
            ],
            WAITING_FOR_PHONE: [
                MessageHandler(filters.Text("❌ Cancel Order"), cancel_product_order), # NEW: Cancel button handler
                MessageHandler(filters.TEXT & ~filters.COMMAND, validate_phone_and_ask_username)
            ],
            WAITING_FOR_USERNAME: [
                MessageHandler(filters.Text("❌ Cancel Order"), cancel_product_order), # NEW: Cancel button handler
                MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_product_order)
            ],
        },
        # NEW: Added MessageHandler for "❌ Cancel Order" to catch button press in all states
        fallbacks=[
            CallbackQueryHandler(back_to_service_menu, pattern=r"^menu_back$"),
            MessageHandler(filters.Text("❌ Cancel Order"), cancel_product_order) 
        ],
        allow_reentry=True,
    )
    application.add_handler(product_purchase_handler)

    # Admin Broadcast Conversation Handler (NEW)
    broadcast_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("👾 Broadcast"), start_broadcast)],
        states={
            BROADCAST_WAITING_FOR_CONTENT: [
                MessageHandler(filters.Text("❌ Cancel Admin Action"), cancel_admin_action),
                MessageHandler(filters.PHOTO & filters.Caption, receive_broadcast_content), # Photo + Caption Required
            ],
            BROADCAST_CONFIRMATION: [
                CallbackQueryHandler(finalize_broadcast, pattern=r"^broadcast_confirm_")
            ],
        },
        fallbacks=[
            MessageHandler(filters.Text("❌ Cancel Admin Action"), cancel_admin_action),
            # MessageHandler(filters.Regex(r".*"), cancel_admin_action) # Catch any other text input as implicit cancel
        ],
        allow_reentry=True,
    )
    application.add_handler(broadcast_conv_handler)


    # Message handlers for reply keyboard
    application.add_handler(MessageHandler(filters.Text("👤 User Info"), handle_user_info))
    application.add_handler(MessageHandler(filters.Text("❓ Help Center"), handle_help_center))
    
    # NEW: Handler for the "Premium & Star" Reply Button
    application.add_handler(MessageHandler(filters.Text("✨ Premium & Star"), show_product_inline_menu))
    
    # NEW: Handler for the "Close to Selling" Admin Button
    application.add_handler(MessageHandler(filters.Text("⚙️ Close to Selling"), start_close_to_selling))
    application.add_handler(CallbackQueryHandler(set_new_bot_status_callback, pattern=r"^set_status_"))
    
    # Inline callbacks: products
    application.add_handler(CallbackQueryHandler(start_product_purchase, pattern=r"^product_"))
    
    # Admin callback handlers for approve/deny (Updated patterns)
    application.add_handler(CallbackQueryHandler(admin_approve_receipt_callback, pattern=r"^rpa\|"))
    application.add_handler(CallbackQueryHandler(admin_deny_receipt_callback, pattern=r"^rpd\|"))

    # Back/menu callback (This is crucial for returning to the main Reply Keyboard)
    application.add_handler(CallbackQueryHandler(back_to_service_menu, pattern=r"^menu_back$"))

    # Global error handler
    application.add_error_handler(error_handler)

    # Run webhook if RENDER_EXTERNAL_URL provided; otherwise fallback to polling
    token = BOT_TOKEN
    if RENDER_EXTERNAL_URL:
        listen = "0.0.0.0"
        port = PORT
        url_path = token
        webhook_url = f"{RENDER_EXTERNAL_URL}/{token}"
        print(f"Starting webhook on port {port}, URL: {webhook_url}")
        logger.info("Setting webhook URL to: %s", webhook_url)
        application.run_webhook(listen=listen, port=port, url_path=url_path, webhook_url=webhook_url)
    else:
        logger.info("RENDER_EXTERNAL_URL not set — using long polling (development mode).")
        application.run_polling()


if __name__ == "__main__":
    main()
