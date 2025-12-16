import os
import time
import logging
import json
import datetime
import re
import uuid
from typing import Dict, Optional, List
import gspread
from google.auth.transport.requests import Request
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Message
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

# Global Bot Status (Default is ON)
BOT_STATUS_ON = True

# Conversation states
(
    CHOOSING_PAYMENT_METHOD,
    WAITING_FOR_RECEIPT,
    SELECT_PRODUCT_PRICE,
    WAITING_FOR_PHONE,
    WAITING_FOR_USERNAME,
    SELECT_COIN_PACKAGE,
) = range(6)

# NEW: States for Cash Control Conversation (START at 30)
AWAIT_CASH_CONTROL_ID, AWAIT_CASH_CONTROL_AMOUNT = range(30, 32)
# NEW: States for User Search Conversation (START at 32)
AWAIT_USER_SEARCH_ID = 32
# NEW: States for Broadcast Conversation (START at 33)
AWAIT_BROADCAST_CONTENT, CONFIRM_BROADCAST = range(33, 35)


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

# NEW Helper: Getter for sheets (used in admin functions)
def get_user_data_sheet() -> gspread.Worksheet:
    global WS_USER_DATA
    return WS_USER_DATA

def get_config_sheet() -> gspread.Worksheet:
    global WS_CONFIG
    return WS_CONFIG

def get_orders_sheet() -> gspread.Worksheet:
    global WS_ORDERS
    return WS_ORDERS


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
        logger.info("Config cache refreshed.")
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

def is_user_exists(user_id: int) -> bool:
    return find_user_row(user_id) is not None

def get_user_row_by_id(user_id: int) -> Optional[int]:
    """Alias for find_user_row for clarity in admin functions."""
    return find_user_row(user_id)

# NEW Helper: Resolve user ID from ID or Username (improved search logic)
def resolve_user_id(identifier: str) -> Optional[int]:
    """Attempts to resolve a user ID from a string which can be ID (digits), @username, or plain username."""
    ws_user = get_user_data_sheet()
    if not ws_user:
        return None
        
    identifier = identifier.strip()

    # 1. Check if it's a digit (User ID)
    if identifier.isdigit():
        user_id_int = int(identifier)
        if find_user_row(user_id_int):
             return user_id_int
        return None

    # 2. Check for @username or plain username
    username_to_search = identifier
    if not username_to_search.startswith('@'):
        username_to_search = '@' + username_to_search
    
    try:
        # Search by username (column B is usually username)
        cell = ws_user.find(username_to_search, in_column=2)
        if cell:
            # Get ID from the first column (A) of that row
            return int(ws_user.cell(cell.row, 1).value)
    except Exception as e:
        logger.debug(f"Error resolving username {username_to_search}: {e}")
        pass

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
    
# NEW Helper: Get list of all registered user IDs (for broadcast)
def get_all_user_ids() -> List[int]:
    global WS_USER_DATA
    if not WS_USER_DATA:
        return []
    try:
        # Get all values from the first column (User IDs) excluding the header row
        user_ids_list = WS_USER_DATA.col_values(1)[1:] 
        # Filter out empty strings and convert to integer
        return [int(uid) for uid in user_ids_list if uid and uid.isdigit()]
    except Exception as e:
        logger.error(f"Error getting all user IDs: {e}")
        return []


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

# NEW: Admin Only Reply Keyboard (Including all new buttons)
ADMIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("👤 User Info"), KeyboardButton("💰 Payment Method")],
        [KeyboardButton("❓ Help Center"), KeyboardButton("✨ Premium & Star")],
        [KeyboardButton("👾 Broadcast"), KeyboardButton("⚙️ Close to Selling")],
        [KeyboardButton("📝 Cash Control"), KeyboardButton("👤 User Search")],
        [KeyboardButton("🔄 Refresh Config"), KeyboardButton("📊 Statistics")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# NEW: Keyboard for canceling admin conversations
ADMIN_CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("⬅️ Cancel")]],
    resize_keyboard=True,
    one_time_keyboard=True
)

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
# MODIFIED: start_command checks for BOT_STATUS_ON
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user_if_not_exists(user.id, user.full_name)
    
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    is_admin = (user.id == admin_id_check)
    
    if not is_admin and not BOT_STATUS_ON:
        await update.message.reply_text("⛔ **Maintenance Mode:** Bot is currently closed for maintenance. Please check back later.", parse_mode="Markdown")
        return
        
    if is_user_banned(user.id):
        # Keep Burmese ban message as it is likely crucial for the audience
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားထားသည်။ Support ထံ ဆက်သွယ်ပါ။")
        return

    # MODIFIED: Updated welcome message format
    welcome_text = (
        f"Hello, 👑**{user.full_name}**\n\n"
        f"🧸Welcome — Meow Telegram Bot 🫶\n"
        f"To make a purchase with excellent service and advanced functionality, choose from the menu below."
    )
    
    # Use Admin Keyboard if the user is Admin, otherwise use the standard menu
    keyboard_to_use = ADMIN_REPLY_KEYBOARD if is_admin else MAIN_MENU_KEYBOARD

    # Send the main menu reply keyboard
    await update.message.reply_text(welcome_text, reply_markup=keyboard_to_use, parse_mode="Markdown")


# NEW: Function to display the Star/Premium inline buttons, triggered by the new Reply Button
async def show_product_inline_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    if not BOT_STATUS_ON and user.id != admin_id_check:
        await update.message.reply_text("⛔ Bot is in maintenance mode. Cannot process orders now.", parse_mode="Markdown")
        return
    
    if is_user_banned(user.id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return

    text = "✨ Available Services (Star & Premium):\nPlease select an option below:"
    
    # Send the inline keyboard only
    await update.message.reply_text(text, reply_markup=PRODUCT_SELECTION_INLINE_KEYBOARD)


# MODIFIED: Does not show service menu afterwards
async def handle_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return
    data = get_user_data_from_sheet(user.id)
    info_text = (
        f"👤 **User Information**\n\n"
        f"🔸 **Your ID:** `{data.get('user_id')}`\n"
        f"🔸 **Username:** {data.get('username')}\n"
        f"🔸 **Coin Balance:** **{int(data.get('coin_balance', '0')):,}** Coins\n"
        f"🔸 **Registered Since:** {data.get('registration_date')}\n"
        f"🔸 **Banned:** {data.get('banned')}\n"
    )
    # Use menu_back to return to the main menu (no inline service menu here)
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")]])
    await update.message.reply_text(info_text, reply_markup=back_keyboard, parse_mode="Markdown")


# MODIFIED: Does not show service menu afterwards
async def handle_help_center(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    user = update.effective_user
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    if not BOT_STATUS_ON and user.id != admin_id_check:
        await update.message.reply_text("⛔ Bot is in maintenance mode. Cannot process payments now.", parse_mode="Markdown")
        return ConversationHandler.END
        
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
        f"💳 You selected **{coins:,} Coins — {mmk:,} MMK**.\nPlease choose payment method:",
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
        pkg_text = f"\nPackage: {pkg['coins']:,} Coins — {pkg['mmk']:,} MMK\n"
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
                # FIX: Remove any potential non-digit/non-comma characters before split
                clean_amounts_cfg = "".join(c for c in amounts_cfg if c.isdigit() or c == ',')
                choices = [int(x.strip()) for x in clean_amounts_cfg.split(",") if x.strip() and x.strip().isdigit()]
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
            row.append(InlineKeyboardButton(f"✅ Approve {amt:,.0f} MMK", callback_data=f"rpa|{user.id}|{short_ts}|{amt}"))
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
    target_user_name = user_data.get("username", f"ID:{user_id}")
    
    try:
        # Coin Balance ကို fetch လုပ်ရာမှာ clean လုပ်ပြီးသားဖြစ်တဲ့အတွက်၊ ဒီနေရာမှာ မှန်ကန်စွာ ပေါင်းသွားပါပြီ။
        current_coins = int(user_data.get("coin_balance", "0"))
    except ValueError:
        current_coins = 0
    new_balance = current_coins + coins_to_add

    ok = update_user_balance(user_id, new_balance)
    if not ok:
        await query.message.edit_text("Failed to update user balance in sheet.")
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
    
    # Get the username of the admin who processed the order
    processed_by_username = f"@{query.from_user.username}" if query.from_user.username else f"(id:{query.from_user.id})"
    
    # NEW: Beautiful Admin Notification Message (As requested)
    beautiful_message = (
        f"✅ **APPROVED: {approved_amount:,.0f} MMK**\n\n"
        f"💰 **Added {coins_to_add:,.0f} Coins** to user.\n\n"
        f"♦️User: 🧸**{target_user_name}** (id:`{user_id}`)\n"
        f"👾Processed by: **{processed_by_username}**\n"
        f"👾Order ID: `{order['order_id']}`\n"
        f"👾Time: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}`"
    )

    try:
        # User Notification
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🎉Your balance {coins_to_add:,.0f} coin top up Successful. New balance: {new_balance:,.0f} Coins.",
        )
        
        # Admin Notification: Edit the inline keyboard message to show the final result
        await query.message.edit_text(beautiful_message, parse_mode="Markdown")
        
    except Exception as e:
        logger.error("Failed to notify user after approval: %s", e)
        await query.message.edit_text(f"Approved but failed to notify user. {beautiful_message}", parse_mode="Markdown")


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
        await query.message.edit_text("❌ Denied and user notified.")
    except Exception as e:
        logger.error("Failed to notify user after denial: %s", e)
        await query.message.edit_text("Denied but failed to notify user.")


# ----------- Product purchase flow (NEW CANCEL BUTTONS ADDED) -----------
async def start_product_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    if not BOT_STATUS_ON and user.id != admin_id_check:
        await query.message.reply_text("⛔ Bot is in maintenance mode. Cannot process orders now.", parse_mode="Markdown")
        return ConversationHandler.END
        
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
            f"❌ Insufficient coin balance. You need {price_needed_coins:,.0f} Coins but have {user_coins:,.0f} Coins. Use '💰 Payment Method' to top up.",
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
        "notes": f"Order placed and {price_needed_coins:,.0f} Coins deducted.",
    }
    log_order(order)
    
    config = get_config_data()
    # MODIFIED: Get ADMIN_ID from config data
    admin_id_check = get_dynamic_admin_id(config)


    await update.message.reply_text(
        f"✅ Order successful! **{price_needed_coins:,.0f} Coins** have been deducted for {product_key.replace('_',' ').upper()}.\n"
        f"New balance: {new_balance:,.0f} Coins. Please wait while service is processed.",
        reply_markup=MAIN_MENU_KEYBOARD # Show main menu keyboard on success
    )
    try:
        admin_msg = (
            f"🛒 New Order\n"
            f"Order ID: {order['order_id']}\n"
            f"User: @{user.username or user.full_name} (id:{user_id})\n"
            f"Product: {product_key}\n"
            f"Price: {price_mmk_needed:,.0f} MMK ({price_needed_coins:,.0f} Coins deducted)\n"
            f"Phone: {premium_phone}\n"
            f"Username: {premium_username}\n"
        )
        await context.bot.send_message(chat_id=admin_id_check, text=admin_msg)
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


# MODIFIED: Global back to service menu (menu_back) now only returns to the main Reply Keyboard
async def back_to_service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    # Check if the user is the Admin from the config sheet
    is_admin = (user.id == admin_id_check)
    keyboard_to_use = ADMIN_REPLY_KEYBOARD if is_admin else MAIN_MENU_KEYBOARD

    welcome_text = "Welcome back to the main menu. Choose from the options below."

    # Use reply_text which sends a new message with the Reply Keyboard.
    try:
        # Delete the previous inline message if possible
        await query.message.delete()
    except Exception:
        pass # Ignore error if delete fails (e.g., message is too old)

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=welcome_text,
        reply_markup=keyboard_to_use, # Use the determined keyboard
    )
    return ConversationHandler.END # Exit any active conversation state

# --------------- Admin Features (NEWLY IMPLEMENTED OR MODIFIED) ---------------

# Admin authorization check helper
def is_admin(user_id: int) -> bool:
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    return user_id == admin_id_check

# ----------------- ⚙️ Close to Selling (Bot Status Control) -----------------
async def handle_close_to_selling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("You are not authorized.")
        return
        
    global BOT_STATUS_ON
    status_text = "🟢 ACTIVE" if BOT_STATUS_ON else "⛔ MAINTENANCE MODE"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Set to ACTIVE", callback_data="set_status_on")],
        [InlineKeyboardButton("⛔ Set to MAINTENANCE", callback_data="set_status_off")]
    ])
    
    await update.message.reply_text(
        f"⚙️ **Bot Status Control**\n\n"
        f"Current Status: **{status_text}**\n\n"
        "Select the new status for the bot. Users will be blocked from ordering/topping up if set to Maintenance.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def set_bot_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.message.reply_text("You are not authorized.")
        return
        
    global BOT_STATUS_ON
    new_status = query.data.split("_")[-1]

    if new_status == 'on':
        BOT_STATUS_ON = True
        msg = "✅ Bot is now **ACTIVE (ON)**. Users can place orders."
    else:
        BOT_STATUS_ON = False
        msg = "⛔ Bot is now in **MAINTENANCE MODE (OFF)**. Only Admin can use it."
        
    await query.message.edit_text(
        f"⚙️ Status Updated: **{msg}**",
        parse_mode="Markdown"
    )
    await query.message.reply_text("Returning to Admin Menu.", reply_markup=ADMIN_REPLY_KEYBOARD)


# ----------------- 📊 Statistics (Placeholder) -----------------
async def handle_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return
        
    # --- Placeholder Data Generation ---
    try:
        total_users = len(get_all_user_ids())
    except Exception:
        total_users = "N/A"
        
    stats_msg = (
        f"📊 **Bot Statistics Overview**\n\n"
        f"👤 Total Registered Users: **{total_users:,}**\n"
        f"🛒 Total Orders (Placeholder): **{350:,}**\n" # Use placeholder until log_orders sheet processing is added
        f"💵 Total Revenue (Placeholder): **{500_000:,}** MMK\n\n"
        f"*(Note: Order and Revenue stats require further sheet processing logic)*"
    )
    
    await update.message.reply_text(stats_msg, parse_mode="Markdown")


# ----------------- 🔄 Refresh Config -----------------
async def handle_refresh_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return
        
    get_config_data(force_refresh=True)
    await update.message.reply_text("🔄 Config data refreshed from Google Sheet.")


# ----------------- 📝 Cash Control (Modified) -----------------

# Function to start the Cash Control process
async def start_cash_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("You are not authorized to use Cash Control.", reply_markup=ADMIN_REPLY_KEYBOARD)
        return ConversationHandler.END

    # Ask Admin for User ID or Username
    await update.message.reply_text(
        "📝 **CASH CONTROL**\n\n"
        "Please enter the **User ID (number)** or **Username (@...)** of the user whose balance you want to modify.",
        parse_mode="Markdown",
        reply_markup=ADMIN_CANCEL_KEYBOARD # Use generic cancel keyboard
    )
    
    return AWAIT_CASH_CONTROL_ID

# Function to handle cancellation
async def cash_control_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📝 Cash Control cancelled.",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    return ConversationHandler.END

# Function to get User ID/Username and ask for amount (FIXED SEARCH LOGIC + NEW MESSAGE FORMAT)
async def cash_control_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    input_identifier = update.message.text.strip()
    
    # Use the improved resolve_user_id helper
    user_id_int = resolve_user_id(input_identifier)

    if not user_id_int:
        await update.message.reply_text("❌ User not found or ID/Username is invalid. Please try again or type '⬅️ Cancel'.")
        return AWAIT_CASH_CONTROL_ID
         
    # Get user data for balance display
    user_data = get_user_data_from_sheet(user_id_int)
    target_username = user_data.get("username", f"ID:{user_id_int}")
    current_coin_balance = int(user_data.get("coin_balance", "0"))
         
    # Store the target ID and Username in context
    context.user_data['target_cash_control_id'] = user_id_int
    context.user_data['target_cash_control_name'] = target_username
    
    # 2. Ask for Coin amount with NEW FORMAT
    new_message = (
        f"📝 **Target User Found**: {target_username} (ID `{user_id_int}`)\n\n"
        f"💰**Coin Balance**: **{current_coin_balance:,}** Coins\n\n"
        f"♦️Please enter the Coin amount to add or subtract.\n\n"
        f"🌀Use **+** for adding (e.g., `+5000`)\n"
        f"🌀Use **-** for subtracting (e.g., `-100`)\n"
    )
    
    await update.message.reply_text(
        new_message,
        parse_mode="Markdown",
        reply_markup=ADMIN_CANCEL_KEYBOARD
    )
    
    return AWAIT_CASH_CONTROL_AMOUNT

# Function to apply the coin change and finish
async def cash_control_apply_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount_text = update.message.text.strip()
    target_user_id = context.user_data.get('target_cash_control_id')
    target_user_name = context.user_data.get('target_cash_control_name', f"ID:{target_user_id}")
    admin_user = update.effective_user
    
    if not target_user_id:
        await update.message.reply_text("❌ Error: Target user ID lost. Please restart Cash Control.", reply_markup=ADMIN_REPLY_KEYBOARD)
        return ConversationHandler.END

    # 1. Validate the input format (+/- and number)
    match = re.match(r"([+\-]\d+)", amount_text)
    if not match:
        await update.message.reply_text("❌ Invalid format. Please use '+[number]' or '-[number]' (e.g., `+5000` or `-100`).")
        return AWAIT_CASH_CONTROL_AMOUNT

    try:
        coin_change = int(match.group(1))
    except ValueError:
        await update.message.reply_text("❌ The number provided is too large or not a valid integer.")
        return AWAIT_CASH_CONTROL_AMOUNT

    # 2. Update Coin Balance in the sheet
    ws_user = get_user_data_sheet()
    user_row = find_user_row(target_user_id)
    
    if user_row:
        balance_cell_col = 3 # coin_balance is Column C
        # Ensure we read the current balance correctly (clean up the string)
        try:
             old_balance = int(ws_user.cell(user_row, balance_cell_col).value or 0)
        except ValueError:
             old_balance = 0
             
        new_balance = old_balance + coin_change
        
        # Update the sheet
        ws_user.update_cell(user_row, balance_cell_col, new_balance)
        
        # 3. Create success message for Admin
        if coin_change > 0:
            action_text = "Added"
            action_emoji = "🟢"
        elif coin_change < 0:
            action_text = "Subtracted"
            action_emoji = "🔴"
        else:
            action_text = "No Change"
            action_emoji = "⚪"

        admin_processed_by = f"@{admin_user.username}" if admin_user.username else f"ID:{admin_user.id}"
        
        admin_success_msg = (
            f"✅ **Cash Control Successful!**\n\n"
            f"{action_emoji} **Action:** {action_text} **{abs(coin_change):,} Coins**\n"
            f"**User:** {target_user_name} (ID `{target_user_id}`)\n"
            f"**Old Balance:** {old_balance:,} Coins\n"
            f"**New Balance:** {new_balance:,} Coins\n"
            f"**Processed by:** {admin_processed_by}"
        )
        
        await update.message.reply_text(admin_success_msg, parse_mode="Markdown", reply_markup=ADMIN_REPLY_KEYBOARD)

        # 4. Notify User (Only if coins were added, as requested)
        if coin_change > 0:
            user_notification = (
                f"🎉 **Coin Update Notification**\n\n"
                f"**{coin_change:,} Coins** have been manually added to your account by the Admin.\n\n"
                f"Your new balance is **{new_balance:,} Coins**."
            )
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=user_notification,
                    parse_mode="Markdown"
                )
            except Exception as e:
                await update.message.reply_text(f"⚠️ Warning: Could not send notification to user ID {target_user_id}. Error: {e}", reply_markup=ADMIN_REPLY_KEYBOARD)

    else:
        await update.message.reply_text("❌ Error: Target user row could not be located in the sheet during final update.", reply_markup=ADMIN_REPLY_KEYBOARD)

    # Clean up context data
    context.user_data.pop('target_cash_control_id', None)
    context.user_data.pop('target_cash_control_name', None)
        
    return ConversationHandler.END


# ----------------- 👤 User Search (New Conversation Handler) -----------------

async def start_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return ConversationHandler.END
        
    await update.message.reply_text(
        "👤 **USER SEARCH**\n\n"
        "Please enter the **User ID (number)** or **Username (@...)** to search for.",
        parse_mode="Markdown",
        reply_markup=ADMIN_CANCEL_KEYBOARD
    )
    return AWAIT_USER_SEARCH_ID

async def user_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👤 User Search cancelled.",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    return ConversationHandler.END

async def user_search_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    input_identifier = update.message.text.strip()
    user_id_int = resolve_user_id(input_identifier)

    if not user_id_int:
        await update.message.reply_text("❌ User not found or ID/Username is invalid. Please try again or type '⬅️ Cancel'.")
        return AWAIT_USER_SEARCH_ID
        
    user_data = get_user_data_from_sheet(user_id_int)
    
    banned_status = "BANNED" if user_data.get('banned', 'FALSE').upper() == 'TRUE' else "CLEAN"
    
    info_text = (
        f"🔍 **Search Result**\n\n"
        f"👤 **Name/Username:** {user_data.get('username')}\n"
        f"🔸 **User ID:** `{user_data.get('user_id')}`\n"
        f"🔸 **Status:** **{banned_status}**\n"
        f"🔸 **Coin Balance:** **{int(user_data.get('coin_balance', '0')):,}** Coins\n"
        f"🔸 **Total Purchase:** {int(user_data.get('total_purchase', '0')):,} MMK\n"
        f"🔸 **Registered Since:** {user_data.get('registration_date')}\n"
    )
    
    # Inline buttons for action (Ban/Unban)
    is_banned = user_data.get('banned', 'FALSE').upper() == 'TRUE'
    action_button = InlineKeyboardButton(
        "✅ Unban User" if is_banned else "⛔ Ban User",
        callback_data=f"toggleban|{user_id_int}|{'unban' if is_banned else 'ban'}"
    )
    
    keyboard = InlineKeyboardMarkup([[action_button]])
    
    await update.message.reply_text(info_text, parse_mode="Markdown", reply_markup=keyboard)
    
    # Return to the main admin keyboard
    await update.message.reply_text("Action complete. Returning to Admin Menu.", reply_markup=ADMIN_REPLY_KEYBOARD)
    return ConversationHandler.END

async def toggle_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.message.reply_text("You are not authorized.")
        return
        
    data = query.data.split("|")
    try:
        user_id = int(data[1])
        action = data[2] # 'ban' or 'unban'
    except Exception:
        await query.message.edit_text("Invalid Ban/Unban parameter.")
        return
        
    new_status = (action == 'ban')
    
    if set_user_banned_status(user_id, new_status):
        new_text = "⛔ BANNED" if new_status else "✅ UNBANNED"
        await query.message.edit_text(f"User `{user_id}` has been successfully **{new_text}**.", parse_mode="Markdown")
    else:
        await query.message.edit_text(f"❌ Failed to update ban status for user `{user_id}`.")


# ----------------- 👾 Broadcast (New Conversation Handler) -----------------
async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return ConversationHandler.END
        
    await update.message.reply_text(
        "👾 **BROADCAST MESSAGE**\n\n"
        "Please send the **message (text or photo + caption)** you want to broadcast to all users.",
        parse_mode="Markdown",
        reply_markup=ADMIN_CANCEL_KEYBOARD
    )
    return AWAIT_BROADCAST_CONTENT

async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop('broadcast_message', None)
    await update.message.reply_text(
        "👾 Broadcast cancelled.",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    return ConversationHandler.END

async def confirm_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    
    # Store message details in user_data
    context.user_data['broadcast_message'] = {
        'text': message.text_html or message.caption_html,
        'photo_file_id': message.photo[-1].file_id if message.photo else None,
        'has_photo': bool(message.photo),
    }

    # Prepare confirmation message
    confirm_text = "✅ **Broadcast Content Received.**\n\n"
    if message.photo:
        confirm_text += "*(Photo attached)*\n"
    
    confirm_text += f"**Content (HTML):**\n{message.text_html or message.caption_html or 'No Text Provided'}"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 CONFIRM AND SEND BROADCAST", callback_data="broadcast_send")],
        [InlineKeyboardButton("⬅️ Cancel Broadcast", callback_data="broadcast_cancel")]
    ])
    
    # Send confirmation back to Admin
    if message.photo:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=message.photo[-1].file_id,
            caption=confirm_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(confirm_text, parse_mode="HTML", reply_markup=keyboard)
        
    # Remove the temporary keyboard for confirmation step
    await update.message.reply_text("Please confirm the broadcast.", reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel"]], resize_keyboard=True))

    return CONFIRM_BROADCAST

async def execute_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "broadcast_cancel":
        return await broadcast_cancel(update, context)

    if not is_admin(query.from_user.id):
        await query.message.reply_text("You are not authorized.")
        return ConversationHandler.END
        
    message_data = context.user_data.get('broadcast_message')
    if not message_data:
        await query.message.edit_text("❌ Broadcast data lost. Please start again.", reply_markup=ADMIN_REPLY_KEYBOARD)
        return ConversationHandler.END
        
    all_user_ids = get_all_user_ids()
    sent_count = 0
    failed_count = 0
    
    await query.message.edit_text("🚀 Starting broadcast... This may take a moment.", reply_markup=None)

    for user_id in all_user_ids:
        try:
            if message_data['has_photo']:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=message_data['photo_file_id'],
                    caption=message_data['text'],
                    parse_mode="HTML"
                )
            else:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message_data['text'],
                    parse_mode="HTML"
                )
            sent_count += 1
            # Add a small delay to avoid hitting Telegram's flood limits for sending messages
            time.sleep(0.05) 
        except Exception as e:
            # Catch errors like "blocked by user" or "chat not found"
            failed_count += 1
            logger.debug(f"Failed to send broadcast to user {user_id}: {e}")

    final_msg = (
        f"✅ **BROADCAST COMPLETE!**\n\n"
        f"👥 Total Users Attempted: **{len(all_user_ids):,}**\n"
        f"🟢 Successfully Sent: **{sent_count:,}**\n"
        f"🔴 Failed (Blocked/Error): **{failed_count:,}**"
    )
    
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=final_msg,
        parse_mode="Markdown",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    
    # Clean up context data
    context.user_data.pop('broadcast_message', None)
    return ConversationHandler.END


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

    # Admin commands (legacy /ban /unban)
    application.add_handler(CommandHandler("ban", admin_ban_user))
    application.add_handler(CommandHandler("unban", admin_unban_user))
    
    # Admin Inline Callback Handlers
    application.add_handler(CallbackQueryHandler(set_bot_status_callback, pattern=r"^set_status_"))
    application.add_handler(CallbackQueryHandler(toggle_ban_callback, pattern=r"^toggleban\|"))


    # NEW: Admin Reply Keyboard Handlers
    application.add_handler(MessageHandler(filters.Text("⚙️ Close to Selling"), handle_close_to_selling))
    application.add_handler(MessageHandler(filters.Text("📊 Statistics"), handle_statistics))
    application.add_handler(MessageHandler(filters.Text("🔄 Refresh Config"), handle_refresh_config))
    
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

    # NEW: Cash Control Conversation Handler
    cash_control_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📝 Cash Control"), start_cash_control)],
        states={
            AWAIT_CASH_CONTROL_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Text("⬅️ Cancel"), cash_control_get_id)
            ],
            AWAIT_CASH_CONTROL_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Text("⬅️ Cancel"), cash_control_apply_amount)
            ]
        },
        fallbacks=[MessageHandler(filters.Text("⬅️ Cancel"), cash_control_cancel)],
        allow_reentry=True
    )
    application.add_handler(cash_control_handler)
    
    # NEW: User Search Conversation Handler
    user_search_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("👤 User Search"), start_user_search)],
        states={
            AWAIT_USER_SEARCH_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Text("⬅️ Cancel"), user_search_get_id)
            ]
        },
        fallbacks=[MessageHandler(filters.Text("⬅️ Cancel"), user_search_cancel)],
        allow_reentry=True
    )
    application.add_handler(user_search_handler)
    
    # NEW: Broadcast Conversation Handler
    broadcast_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("👾 Broadcast"), start_broadcast)],
        states={
            AWAIT_BROADCAST_CONTENT: [
                MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND & ~filters.Text("⬅️ Cancel"), confirm_broadcast_content)
            ],
            CONFIRM_BROADCAST: [
                CallbackQueryHandler(execute_broadcast, pattern=r"broadcast_send|broadcast_cancel")
            ]
        },
        fallbacks=[MessageHandler(filters.Text("⬅️ Cancel"), broadcast_cancel)],
        allow_reentry=True
    )
    application.add_handler(broadcast_handler)


    # Message handlers for reply keyboard (Main Menu)
    application.add_handler(MessageHandler(filters.Text("👤 User Info"), handle_user_info))
    application.add_handler(MessageHandler(filters.Text("❓ Help Center"), handle_help_center))
    
    # NEW: Handler for the "Premium & Star" Reply Button
    application.add_handler(MessageHandler(filters.Text("✨ Premium & Star"), show_product_inline_menu))
    
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
