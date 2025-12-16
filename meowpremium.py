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

# NEW Helper: Check if user is admin
def is_admin(user_id: int) -> bool:
    """Checks if the given user ID matches the current dynamic admin ID."""
    config = get_config_data()
    admin_id = get_dynamic_admin_id(config)
    return user_id == admin_id


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
    is_admin_user = (user.id == admin_id_check)
    
    if not is_admin_user and not BOT_STATUS_ON:
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
    keyboard_to_use = ADMIN_REPLY_KEYBOARD if is_admin_user else MAIN_MENU_KEYBOARD

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
        
        choices = default_choices
        if amounts_cfg:
            try:
                # FIX: Remove any potential non-digit/non-comma characters before split
                clean_amounts_cfg = "".join(c for c in amounts_cfg if c.isdigit() or c == ',')
                configured_choices = [int(x.strip()) for x in clean_amounts_cfg.split(",") if x.strip() and x.strip().isdigit()]
                if configured_choices:
                    choices = configured_choices
            except Exception:
                # Configuration မှားယွင်းပါက Default သို့ ပြန်သွားပါမည်။
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
    data = query.data # rpa|<user_id>|<short_ts>|<amount>
    
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

    # Check if this receipt has already been processed
    if "✅" in (query.message.text or "") or "❌" in (query.message.text or ""):
        await query.edit_message_text(f"❗ This receipt has already been processed by {query.message.text.split('by', 1)[-1].strip() or 'another admin'}.")
        return

    # Process: Find coin rate for coin package
    coin_rate_key = "coin_rate_coin"
    try:
        coin_rate_mmk = float(config.get(coin_rate_key, "1000")) 
    except ValueError:
        coin_rate_mmk = 1000.0

    if coin_rate_mmk <= 0:
         coin_rate_mmk = 1000.0
         
    # Calculate Coins: Use integer division (or int casting)
    coins_to_add = int(approved_amount / coin_rate_mmk)

    # 1. Update User Balance
    user_data = get_user_data_from_sheet(user_id)
    current_balance = int(user_data.get("coin_balance", "0"))
    new_balance = current_balance + coins_to_add
    
    if coins_to_add > 0:
        ok = update_user_balance(user_id, new_balance)
    else:
        ok = True # Nothing to update if coins_to_add is 0

    if ok:
        # 2. Update Admin Message
        admin_username = query.from_user.username or query.from_user.full_name
        
        original_text = query.message.text
        new_text = (
            f"✅ APPROVED ({approved_amount:,} MMK = {coins_to_add:,} Coins) by @{admin_username} (id:{query.from_user.id}) "
            f"on {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"{original_text}"
        )
        
        await query.edit_message_text(new_text)

        # 3. Notify User
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ Your payment of **{approved_amount:,} MMK** has been **APPROVED**.\n"
                     f"You received **{coins_to_add:,} Coins**.\n"
                     f"Your new balance is **{new_balance:,} Coins**.",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to notify user {user_id} of receipt approval: {e}")
            
    else:
        await query.edit_message_text(f"❌ ERROR: Failed to update user balance for ID {user_id}. Please check sheets.")


async def admin_deny_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data # rpd|<user_id>|<short_ts>
    
    parts = data.split("|")
    if len(parts) < 3:
        await query.message.reply_text("Invalid admin action.")
        return
        
    _, user_id_str, short_ts_str = parts[0], parts[1], parts[2]
    
    try:
        user_id = int(user_id_str)
        unix_to_dt = datetime.datetime.fromtimestamp(int(short_ts_str))
        ts_human_readable = unix_to_dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        await query.message.reply_text("Invalid parameters.")
        return

    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    
    if query.from_user.id != admin_id_check:
        await query.message.reply_text("You are not authorized to perform this action.")
        return
        
    # Check if already processed
    if "✅" in (query.message.text or "") or "❌" in (query.message.text or ""):
        await query.edit_message_text(f"❗ This receipt has already been processed by {query.message.text.split('by', 1)[-1].strip() or 'another admin'}.")
        return

    # Update Admin Message
    admin_username = query.from_user.username or query.from_user.full_name
    original_text = query.message.text
    new_text = (
        f"❌ DENIED by @{admin_username} (id:{query.from_user.id}) "
        f"on {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"{original_text}"
    )
    await query.edit_message_text(new_text)

    # Notify User
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Your payment receipt has been **DENIED** by the admin. Please check the receipt and try again, or contact support.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {user_id} of receipt denial: {e}")


# ---------- Product Purchase Flow (Star/Premium -> Phone/Username) ----------
async def start_product_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if is_user_banned(query.from_user.id):
        await query.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return ConversationHandler.END

    product_key = query.data
    config = get_config_data()
    product_type = product_key.split("_")[1] # star or premium
    
    # Get price in coins (this assumes the full key is one of the keys in config e.g., 'star_100_boosts')
    price_mmk_str = config.get(product_key)
    if not price_mmk_str:
        await query.message.reply_text("❌ Product configuration not found.")
        return ConversationHandler.END

    try:
        price_mmk = int(price_mmk_str)
        coin_rate_key = f"coin_rate_{product_type}"
        coin_rate_mmk = float(config.get(coin_rate_key, "1000"))
        if coin_rate_mmk <= 0:
             coin_rate_mmk = 1000.0
             
        price_coin = int(price_mmk / coin_rate_mmk) 
        price_coin = max(1, price_coin) # Ensure at least 1 coin
        
    except (ValueError, TypeError):
        await query.message.reply_text("❌ Invalid price configuration.")
        return ConversationHandler.END
    
    # Check user coin balance
    user_data = get_user_data_from_sheet(query.from_user.id)
    current_balance = int(user_data.get("coin_balance", "0"))

    if current_balance < price_coin:
        await query.message.reply_text(
            f"❌ **Insufficient Coins!**\n"
            f"You need **{price_coin:,} Coins** for this purchase, but you only have **{current_balance:,} Coins**.\n"
            "Please buy more coins using the '💰 Payment Method' button.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
        
    # Store purchase details
    context.user_data["product_details"] = {
        "product_key": product_key,
        "price_mmk": price_mmk,
        "price_coin": price_coin,
        "product_type": product_type
    }
    
    # Prompt for phone number
    await query.message.reply_text(
        f"✅ You selected **{product_key.replace('_', ' ').title()}** for **{price_coin:,} Coins**.\n\n"
        "Please send your **phone number** (e.g., `09XXXXXXXX`) to proceed with the purchase.",
        reply_markup=CANCEL_KEYBOARD,
        parse_mode="Markdown"
    )

    return WAITING_FOR_PHONE


async def receive_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_phone = update.message.text.strip()
    
    if not PHONE_RE.match(user_phone):
        await update.message.reply_text(
            "❌ Invalid phone number format. Please ensure it contains only 8 to 15 digits (e.g., `09XXXXXXXX`)."
        )
        return WAITING_FOR_PHONE # Stay in this state
        
    context.user_data["purchase_phone"] = user_phone
    
    # Prompt for username
    await update.message.reply_text(
        "✅ Phone number received.\n\n"
        "Please send the **Telegram Username** you want to receive the service on (e.g., `@YourUsername` or `YourUsername`).",
        reply_markup=CANCEL_KEYBOARD
    )
    
    return WAITING_FOR_USERNAME


async def receive_premium_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_username = update.message.text.strip()
    premium_username = normalize_username(raw_username)
    
    if not premium_username:
        await update.message.reply_text(
            "❌ Invalid Telegram Username format. Please ensure it is a valid username (5-32 characters, A-Z, 0-9, and underscores)."
        )
        return WAITING_FOR_USERNAME # Stay in this state

    # Confirmation and deduction logic
    user = update.effective_user
    details = context.user_data.get("product_details")
    phone = context.user_data.get("purchase_phone")
    
    if not details:
        await update.message.reply_text("❌ Purchase details lost. Please restart the purchase process.", reply_markup=MAIN_MENU_KEYBOARD)
        return ConversationHandler.END
        
    price_coin = details["price_coin"]
    product_key = details["product_key"]
    
    user_data = get_user_data_from_sheet(user.id)
    current_balance = int(user_data.get("coin_balance", "0"))
    
    if current_balance < price_coin:
         await update.message.reply_text(
            f"❌ **Insufficient Coins!** You need **{price_coin:,} Coins**, but you only have **{current_balance:,} Coins**.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU_KEYBOARD
        )
         return ConversationHandler.END

    new_balance = current_balance - price_coin
    
    # Deduct coins and update order
    ok_balance = update_user_balance(user.id, new_balance)

    if ok_balance:
        # Log the order
        order_details = {
            "user_id": user.id,
            "username": user.username or user.full_name,
            "product_key": product_key,
            "price_mmk": details["price_mmk"],
            "phone": phone,
            "premium_username": premium_username,
            "status": "PENDING (Coin Paid)",
        }
        log_order(order_details)

        # Notify Admin
        config = get_config_data()
        admin_contact_id = get_dynamic_admin_id(config)
        admin_message = (
            "🔔 **NEW ORDER (Coin Paid)**\n\n"
            f"👤 User: @{user.username or user.full_name} (ID: `{user.id}`)\n"
            f"✨ Product: **{product_key.replace('_', ' ').title()}**\n"
            f"🪙 Cost: **{price_coin:,} Coins**\n"
            f"📞 Phone: `{phone}`\n"
            f"🔗 Target Username: **{premium_username}**\n\n"
            "Status: PENDING. Please process manually."
        )
        try:
            await context.bot.send_message(chat_id=admin_contact_id, text=admin_message, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to notify admin of new order: {e}")


        # Success Message to User
        await update.message.reply_text(
            f"✅ **Purchase Successful!**\n\n"
            f"You bought **{product_key.replace('_', ' ').title()}** for **{price_coin:,} Coins**.\n"
            f"Your new balance is **{new_balance:,} Coins**.\n"
            f"The service will be applied to **{premium_username}** soon. Please be patient.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU_KEYBOARD
        )

    else:
        # Failure message
        await update.message.reply_text(
            "❌ **Error:** Failed to deduct coins. Please try again or contact support.",
            reply_markup=MAIN_MENU_KEYBOARD
        )
    
    # Clear user data and end conversation
    context.user_data.pop("product_details", None)
    context.user_data.pop("purchase_phone", None)
    return ConversationHandler.END


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clear stored data
    context.user_data.pop("product_details", None)
    context.user_data.pop("purchase_phone", None)
    context.user_data.pop("selected_coinpkg", None)
    
    # Send cancellation message
    await update.message.reply_text(
        "❌ Order or purchase process cancelled. Returning to main menu.",
        reply_markup=ADMIN_REPLY_KEYBOARD if is_admin(update.effective_user.id) else MAIN_MENU_KEYBOARD
    )
    return ConversationHandler.END


# ----------------- Admin Handlers -----------------

async def handle_refresh_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return

    # Force refresh the config cache
    config = get_config_data(force_refresh=True)
    
    # Check if admin ID was successfully updated from config
    admin_id_check = get_dynamic_admin_id(config)
    
    if update.effective_user.id != admin_id_check:
        # This should theoretically not happen if the current user is an admin, 
        # but protects against stale admin config if the current admin ID changed
        await update.message.reply_text("✅ Config refreshed, but your admin status might have changed. Please check if your keyboard is still Admin Keyboard.")
    else:
        await update.message.reply_text("✅ Configuration refreshed successfully.")


# ----------------- 📝 Cash Control Handlers (FIXED) -----------------
async def start_cash_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 **CASH CONTROL**\n\n"
        "Please send the **User ID or Username** you want to adjust the coin balance for.",
        parse_mode="Markdown",
        reply_markup=ADMIN_CANCEL_KEYBOARD
    )
    return AWAIT_CASH_CONTROL_ID

async def cash_control_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop('target_user_id', None)
    context.user_data.pop('target_username', None)
    await update.message.reply_text(
        "📝 Cash Control cancelled. Returned to Admin Menu.",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    return ConversationHandler.END

async def cash_control_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    identifier = update.message.text
    target_user_id = resolve_user_id(identifier)

    if not target_user_id:
        await update.message.reply_text(
            f"❌ User '{identifier}' not found in the database. Please try another ID or Username, or click Cancel.",
            reply_markup=ADMIN_CANCEL_KEYBOARD
        )
        return AWAIT_CASH_CONTROL_ID

    user_data = get_user_data_from_sheet(target_user_id)
    # Check if user_id is properly retrieved (e.g., handles default return if not found)
    if str(user_data.get('user_id')) != str(target_user_id):
         await update.message.reply_text(
            f"❌ User '{identifier}' not found in the database. Please try another ID or Username, or click Cancel.",
            reply_markup=ADMIN_CANCEL_KEYBOARD
        )
         return AWAIT_CASH_CONTROL_ID
         
    current_balance = int(user_data.get("coin_balance", "0"))
    
    context.user_data['target_user_id'] = target_user_id
    context.user_data['target_username'] = user_data.get("username", "N/A")
    
    await update.message.reply_text(
        f"✅ User found: **{user_data.get('username')}** (ID: `{target_user_id}`).\n"
        f"Current Balance: **{current_balance:,} Coins**.\n\n"
        "Please send the **NEW** coin balance (e.g., `1000` to set to 1,000 coins).",
        parse_mode="Markdown",
        reply_markup=ADMIN_CANCEL_KEYBOARD
    )
    return AWAIT_CASH_CONTROL_AMOUNT

async def cash_control_apply_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount_str = (update.message.text or "").strip()
    target_user_id = context.user_data.get('target_user_id')
    target_username = context.user_data.get('target_username', 'N/A')

    if not target_user_id:
         await update.message.reply_text("❌ Error: Target user ID lost. Please restart Cash Control.", reply_markup=ADMIN_REPLY_KEYBOARD)
         return ConversationHandler.END

    try:
        new_balance = int(amount_str)
        if new_balance < 0:
            raise ValueError("Amount cannot be negative.")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Please send a non-negative integer for the new coin balance.",
            reply_markup=ADMIN_CANCEL_KEYBOARD
        )
        return AWAIT_CASH_CONTROL_AMOUNT

    # Apply the update
    ok = update_user_balance(target_user_id, new_balance)

    if ok:
        await update.message.reply_text(
            f"✅ Success! **{target_username}**'s (ID: `{target_user_id}`) coin balance updated to **{new_balance:,} Coins**.",
            parse_mode="Markdown",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
        # Notify user
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"⚙️ Your coin balance has been manually adjusted by the Admin. New balance: **{new_balance:,} Coins**.",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to notify user {target_user_id} of cash control change: {e}")
            
    else:
        await update.message.reply_text(
            "❌ Failed to update the coin balance in the sheet. Please check permissions/connection.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )

    context.user_data.pop('target_user_id', None)
    context.user_data.pop('target_username', None)
    return ConversationHandler.END
    
# ----------------- 👤 User Search Handlers (FIXED) -----------------
async def start_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return ConversationHandler.END

    await update.message.reply_text(
        "👤 **USER SEARCH**\n\n"
        "Please send the **User ID or Username** you want to search for.",
        parse_mode="Markdown",
        reply_markup=ADMIN_CANCEL_KEYBOARD
    )
    return AWAIT_USER_SEARCH_ID

async def user_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👤 User Search cancelled. Returned to Admin Menu.",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    return ConversationHandler.END

async def user_search_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    identifier = update.message.text
    target_user_id = resolve_user_id(identifier)

    if not target_user_id:
        await update.message.reply_text(
            f"❌ User '{identifier}' not found in the database. Please try another ID or Username, or click Cancel.",
            reply_markup=ADMIN_CANCEL_KEYBOARD
        )
        return AWAIT_USER_SEARCH_ID

    data = get_user_data_from_sheet(target_user_id)
    
    # Check if the user is actually found and not just returning the default
    if str(data.get('user_id')) != str(target_user_id):
         await update.message.reply_text(
            f"❌ User '{identifier}' not found in the database. Please try another ID or Username, or click Cancel.",
            reply_markup=ADMIN_CANCEL_KEYBOARD
        )
         return AWAIT_USER_SEARCH_ID

    info_text = (
        f"✅ **Search Result: User Found**\n\n"
        f"🔸 **User ID:** `{data.get('user_id')}`\n"
        f"🔸 **Username:** {data.get('username')}\n"
        f"🔸 **Coin Balance:** **{int(data.get('coin_balance', '0')):,}** Coins\n"
        f"🔸 **Total Purchase:** {data.get('total_purchase', '0')} MMK\n"
        f"🔸 **Registered Since:** {data.get('registration_date')}\n"
        f"🔸 **Last Active:** {data.get('last_active')}\n"
        f"🔸 **Banned Status:** **{data.get('banned')}**\n"
    )
    
    is_banned_user = str(data.get('banned')).upper() == "TRUE"
    # Use ban/unban callbacks: "ban_<user_id>" or "unban_<user_id>"
    ban_unban_button = [InlineKeyboardButton("✅ Unban User", callback_data=f"unban_{target_user_id}")] if is_banned_user else [InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_{target_user_id}")]
    
    keyboard = InlineKeyboardMarkup([
        ban_unban_button,
        [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="menu_back")]
    ])
    
    await update.message.reply_text(
        info_text,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    return ConversationHandler.END

# ----------------- 🚫 Ban/Unban Handlers (for Inline Callback) -----------------
async def toggle_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Check Admin Authorization
    if not is_admin(query.from_user.id):
        await query.edit_message_text("You are not authorized to perform this action.")
        return

    data = query.data
    try:
        action, user_id_str = data.split("_", 1)
        target_user_id = int(user_id_str)
    except ValueError:
        await query.edit_message_text("Invalid ban/unban command format.")
        return

    is_banning = (action == "ban")
    
    # Prevent Admin from banning themselves
    if target_user_id == query.from_user.id:
        await query.edit_message_text("You cannot ban yourself.")
        return

    # Use existing helper to update the sheet
    ok = set_user_banned_status(target_user_id, is_banning)
    
    if ok:
        status_text = "BANNED" if is_banning else "UNBANNED"
        target_user_data = get_user_data_from_sheet(target_user_id)
        target_username = target_user_data.get('username', f'ID: {target_user_id}')
        
        await query.edit_message_text(f"✅ User **{target_username}** (ID: `{target_user_id}`) has been **{status_text}**.", parse_mode="Markdown")
        
        # Notify the affected user
        try:
            message_text = "❌ Your account has been permanently **banned** by the admin." if is_banning else "✅ Your account has been **unbanned** by the admin. You can now use the bot."
            await context.bot.send_message(chat_id=target_user_id, text=message_text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to notify user {target_user_id} of ban/unban change: {e}")
    else:
        await query.edit_message_text(f"❌ Failed to update ban status for User ID `{target_user_id}` in the sheet.")
            

# ----------------- ⚙️ Close to Selling / Maintenance Mode Handlers -----------------

async def handle_close_to_selling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return

    config = get_config_data()
    
    # Check current status
    current_status = config.get("bot_status", "ON")
    current_status_text = "✅ ON (Accepting Orders)" if current_status == "ON" else "⛔ OFF (Maintenance Mode)"
    
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                "⛔ Set Maintenance Mode (Close)", 
                callback_data="set_status_OFF"
            )] if current_status == "ON" else [InlineKeyboardButton(
                "✅ Set Online (Open)", 
                callback_data="set_status_ON"
            )],
            [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="menu_back")]
        ]
    )

    await update.message.reply_text(
        f"⚙️ **Close to Selling / Bot Status**\n\n"
        f"Current Status: **{current_status_text}**\n\n"
        "Use the buttons below to toggle the bot's maintenance mode.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def handle_toggle_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("You are not authorized to perform this action.")
        return

    new_status = "ON" if query.data == "set_status_ON" else "OFF"
    
    # Logic to update config sheet (This helper is assumed to exist or needs implementation)
    # The actual implementation depends on how you update a single key/value in WS_CONFIG.
    # Placeholder for the actual update logic:
    try:
        ws_config = get_config_sheet()
        # Find the row for 'bot_status' (assuming key is in column A)
        cell = ws_config.find("bot_status", in_column=1)
        if cell:
            ws_config.update_cell(cell.row, 2, new_status) # Update value in column B
            
            # Update global flag and cache
            global BOT_STATUS_ON 
            BOT_STATUS_ON = (new_status == "ON")
            get_config_data(force_refresh=True) # Force refresh cache
            
            status_text = "✅ Online (Accepting Orders)" if new_status == "ON" else "⛔ Maintenance Mode (Closed)"
            
            new_keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(
                        "⛔ Set Maintenance Mode (Close)", 
                        callback_data="set_status_OFF"
                    )] if new_status == "ON" else [InlineKeyboardButton(
                        "✅ Set Online (Open)", 
                        callback_data="set_status_ON"
                    )],
                    [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="menu_back")]
                ]
            )
            
            await query.edit_message_text(
                f"⚙️ Bot Status Updated!\n\nNew Status: **{status_text}**",
                reply_markup=new_keyboard,
                parse_mode="Markdown"
            )
            
        else:
            await query.edit_message_text("❌ Error: 'bot_status' key not found in config sheet.")

    except Exception as e:
        logger.error(f"Failed to update bot status: {e}")
        await query.edit_message_text("❌ Failed to update bot status in the sheet. Please check sheet connection/permissions.")


# ----------------- 📊 Statistics (Placeholder) -----------------
# This function is used in the keyboard but the implementation is omitted for brevity.
# If you need this function implemented, please provide the required logic.
async def handle_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return
        
    await update.message.reply_text("📊 **Statistics**\n\nStatistics reporting is not yet implemented. Please check manually on Google Sheets.", reply_markup=ADMIN_REPLY_KEYBOARD, parse_mode="Markdown")

# ----------------- 👾 Broadcast Handlers (Placeholder) -----------------
# These functions are needed for the ConversationHandler to work.
async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return ConversationHandler.END

    await update.message.reply_text(
        "👾 **BROADCAST MESSAGE**\n\n"
        "Please send the message content you wish to broadcast to all users.",
        parse_mode="Markdown",
        reply_markup=ADMIN_CANCEL_KEYBOARD
    )
    return AWAIT_BROADCAST_CONTENT

async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop('broadcast_message', None)
    await update.message.reply_text(
        "👾 Broadcast cancelled. Returned to Admin Menu.",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    return ConversationHandler.END

async def receive_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Check if a message or a photo with caption was sent
    if update.message.text:
        content = update.message.text
        content_type = "text"
    elif update.message.photo and update.message.caption:
        content = update.message.caption
        context.user_data['broadcast_photo_id'] = update.message.photo[-1].file_id
        content_type = "photo_caption"
    else:
        await update.message.reply_text("❌ Invalid content. Please send a message or a photo with a caption.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return AWAIT_BROADCAST_CONTENT
        
    context.user_data['broadcast_message'] = content
    context.user_data['broadcast_type'] = content_type

    await update.message.reply_text(
        f"✅ **Confirmation**\n\n"
        f"The following message will be broadcast to **{len(get_all_user_ids())} users**.\n\n"
        f"**Content Preview:**\n{content}\n\n"
        "Are you sure you want to proceed?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Broadcast", callback_data="confirm_broadcast")],
            [InlineKeyboardButton("⬅️ Cancel", callback_data="cancel_broadcast")]
        ])
    )
    return CONFIRM_BROADCAST

async def execute_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_broadcast":
        context.user_data.pop('broadcast_message', None)
        context.user_data.pop('broadcast_photo_id', None)
        await query.edit_message_text(
            "👾 Broadcast cancelled. Returned to Admin Menu.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
        return ConversationHandler.END

    # Get broadcast data
    content = context.user_data.get('broadcast_message')
    content_type = context.user_data.get('broadcast_type')
    photo_id = context.user_data.get('broadcast_photo_id')

    if not content:
        await query.edit_message_text("❌ Error: Broadcast message not found. Restart broadcast.")
        return ConversationHandler.END

    user_ids = get_all_user_ids()
    sent_count = 0
    failed_count = 0
    
    # Inform admin that broadcast started
    await query.edit_message_text(f"🚀 Starting broadcast to {len(user_ids)} users. Please wait...", parse_mode="Markdown")

    for user_id in user_ids:
        try:
            if content_type == "text":
                await context.bot.send_message(chat_id=user_id, text=content, parse_mode="Markdown")
            elif content_type == "photo_caption" and photo_id:
                await context.bot.send_photo(chat_id=user_id, photo=photo_id, caption=content, parse_mode="Markdown")
            sent_count += 1
        except Exception:
            failed_count += 1
            # Note: Telegram will often raise exceptions for users who blocked the bot

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=f"✅ **Broadcast Finished!**\n\n"
             f"Total users: {len(user_ids)}\n"
             f"Successfully sent: {sent_count}\n"
             f"Failed (Blocked/Inactive): {failed_count}",
        parse_mode="Markdown",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )

    context.user_data.pop('broadcast_message', None)
    context.user_data.pop('broadcast_photo_id', None)
    return ConversationHandler.END

# ----------------- Universal Callbacks -----------------
# Back to main menu or service selection
async def back_to_service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    config = get_config_data()
    admin_id_check = get_dynamic_admin_id(config)
    is_admin_user = (user.id == admin_id_check)
    
    # Use Admin Keyboard if the user is Admin, otherwise use the standard menu
    keyboard_to_use = ADMIN_REPLY_KEYBOARD if is_admin_user else MAIN_MENU_KEYBOARD
    
    # Edit the existing message to show the main menu options
    # NOTE: The text of the menu should be generic as we are using it for multiple returns
    try:
        await query.message.edit_text(
            "⬅️ Back to Main Menu. Please select an option below:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ Services (Star & Premium)", callback_data="open_product_menu")]])
        )
    except Exception:
        # If edit fails (e.g., trying to edit a message that has already been edited or is too old)
        await query.message.reply_text(
            "⬅️ Back to Main Menu. Please select an option below:",
            reply_markup=keyboard_to_use # This should be the Reply Keyboard, not inline
        )
        
# Special handler for the product menu callback
async def open_product_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    text = "✨ Available Services (Star & Premium):\nPlease select an option below:"
    
    await query.message.edit_text(text, reply_markup=PRODUCT_SELECTION_INLINE_KEYBOARD)


# Global error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    # The message is too long, send it to the admin if possible
    try:
        admin_id = get_dynamic_admin_id(get_config_data())
        tb_list = context.error.__traceback__
        # Limit traceback size to avoid Telegram message limits
        error_message = f"An error occurred: {context.error}\n"
        for i, tb in enumerate(tb_list):
            if i > 5: # Limit to 5 frames
                error_message += f"... and {len(tb_list) - i} more frames\n"
                break
            error_message += f"File: {tb.tb_frame.f_code.co_filename}, Line: {tb.tb_lineno}\n"
            
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"🚨 Bot Error!\n\n{error_message}",
        )
        
    except Exception as e:
        logger.error(f"Failed to send error message to admin: {e}")


# ----------------- Main Function -----------------
def main() -> None:
    # 1. Initialize Sheets and check connection
    if not initialize_sheets():
        logger.error("Fatal: Could not start bot due to sheet initialization failure.")
        return

    # 2. Build Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # 3. Read initial configuration
    get_config_data(force_refresh=True)

    # 4. Handlers: Conversations
    
    # Payment / Coin Purchase Conversation (MODIFIED to include Coin Package Selection)
    coin_purchase_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text("💰 Payment Method"), handle_payment_method),
            CallbackQueryHandler(handle_payment_method, pattern=r"^payment_back$") # Allow restart from inline keyboard
        ],
        states={
            SELECT_COIN_PACKAGE: [
                CallbackQueryHandler(handle_coin_package_select, pattern=r"^buycoin_\d+_\d+$"),
                MessageHandler(filters.Text("❌ Cancel Order"), cancel_order),
            ],
            CHOOSING_PAYMENT_METHOD: [
                CallbackQueryHandler(start_payment_conv, pattern=r"^pay_"),
                CallbackQueryHandler(back_to_payment_menu, pattern=r"^payment_back$"),
                MessageHandler(filters.Text("❌ Cancel Order"), cancel_order),
            ],
            WAITING_FOR_RECEIPT: [
                # Text, Photos with caption, or Photos alone are considered receipts
                MessageHandler((filters.PHOTO | filters.TEXT) & (~filters.COMMAND), receive_receipt),
                MessageHandler(filters.Text("❌ Cancel Order"), cancel_order),
            ],
        },
        fallbacks=[MessageHandler(filters.Text("❌ Cancel Order"), cancel_order)],
        # Use per_user=True (default), per_chat=True (default)
    )

    # Product Purchase Conversation (Star/Premium)
    product_purchase_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_product_purchase, pattern=r"^(star|premium)_\d+"),
        ],
        states={
            WAITING_FOR_PHONE: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), receive_phone_number),
                MessageHandler(filters.Text("❌ Cancel Order"), cancel_order),
            ],
            WAITING_FOR_USERNAME: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), receive_premium_username),
                MessageHandler(filters.Text("❌ Cancel Order"), cancel_order),
            ],
        },
        fallbacks=[MessageHandler(filters.Text("❌ Cancel Order"), cancel_order)],
    )
    
    # Admin: Cash Control Conversation (FIXED)
    cash_control_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📝 Cash Control"), start_cash_control)],
        states={
            AWAIT_CASH_CONTROL_ID: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), cash_control_get_id),
                MessageHandler(filters.Text("⬅️ Cancel"), cash_control_cancel),
            ],
            AWAIT_CASH_CONTROL_AMOUNT: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), cash_control_apply_amount),
                MessageHandler(filters.Text("⬅️ Cancel"), cash_control_cancel),
            ],
        },
        fallbacks=[
            MessageHandler(filters.Text("⬅️ Cancel"), cash_control_cancel),
        ],
    )
    
    # Admin: User Search Conversation (FIXED)
    user_search_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("👤 User Search"), start_user_search)],
        states={
            AWAIT_USER_SEARCH_ID: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), user_search_get_id),
                MessageHandler(filters.Text("⬅️ Cancel"), user_search_cancel),
            ],
        },
        fallbacks=[
            MessageHandler(filters.Text("⬅️ Cancel"), user_search_cancel),
        ],
    )

    # Admin: Broadcast Conversation (NEW)
    broadcast_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("👾 Broadcast"), start_broadcast)],
        states={
            AWAIT_BROADCAST_CONTENT: [
                MessageHandler(filters.TEXT | filters.PHOTO & (~filters.COMMAND), receive_broadcast_content),
                MessageHandler(filters.Text("⬅️ Cancel"), broadcast_cancel),
            ],
            CONFIRM_BROADCAST: [
                CallbackQueryHandler(execute_broadcast, pattern=r"^(confirm|cancel)_broadcast$"),
                MessageHandler(filters.Text("⬅️ Cancel"), broadcast_cancel),
            ],
        },
        fallbacks=[
            MessageHandler(filters.Text("⬅️ Cancel"), broadcast_cancel),
        ],
    )

    # Add Conversation Handlers
    application.add_handler(coin_purchase_conv_handler)
    application.add_handler(product_purchase_conv_handler)
    application.add_handler(cash_control_conv_handler) # FIXED
    application.add_handler(user_search_conv_handler)   # FIXED
    application.add_handler(broadcast_conv_handler)     # NEW

    # 5. Handlers: Commands and Reply Buttons
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.Text("👤 User Info"), handle_user_info))
    application.add_handler(MessageHandler(filters.Text("❓ Help Center"), handle_help_center))
    
    # NEW: Handler for the "Premium & Star" Reply Button
    application.add_handler(MessageHandler(filters.Text("✨ Premium & Star"), show_product_inline_menu))
    
    # Admin Menu Handlers (Reply Buttons)
    application.add_handler(MessageHandler(filters.Text("🔄 Refresh Config"), handle_refresh_config))
    application.add_handler(MessageHandler(filters.Text("⚙️ Close to Selling"), handle_close_to_selling))
    application.add_handler(MessageHandler(filters.Text("📊 Statistics"), handle_statistics))
    
    # 6. Handlers: Inline callbacks

    # Inline callbacks: products
    application.add_handler(CallbackQueryHandler(start_product_purchase, pattern=r"^product_"))
    
    # Admin callback handlers for approve/deny (Updated patterns)
    application.add_handler(CallbackQueryHandler(admin_approve_receipt_callback, pattern=r"^rpa\|"))
    application.add_handler(CallbackQueryHandler(admin_deny_receipt_callback, pattern=r"^rpd\|"))
    
    # Admin callback handlers for Ban/Unban (NEWLY ADDED)
    application.add_handler(CallbackQueryHandler(toggle_ban_callback, pattern=r"^(ban|unban)_\d+$")) # FIXED
    
    # Admin callback handlers for Bot Status toggle
    application.add_handler(CallbackQueryHandler(handle_toggle_status_callback, pattern=r"^set_status_"))

    # Back/menu callback (This is crucial for returning to the main Reply Keyboard)
    application.add_handler(CallbackQueryHandler(back_to_service_menu, pattern=r"^menu_back$"))
    application.add_handler(CallbackQueryHandler(open_product_menu_callback, pattern=r"^open_product_menu$")) # For button from back_to_service_menu

    # Global error handler
    application.add_error_handler(error_handler)

    # 7. Run the bot
    token = BOT_TOKEN
    if RENDER_EXTERNAL_URL:
        listen = "0.0.0.0"
        port = PORT
        url_path = token
        webhook_url = f"{RENDER_EXTERNAL_URL}/{token}"
        print(f"Starting webhook on port {port}, URL: {webhook_url}")
        logger.info("Setting webhook URL to: %s", webhook_url)
        # Use logger.info for status updates, print is for immediate console feedback
        application.run_webhook(listen=listen, port=port, url_path=url_path, webhook_url=webhook_url)
    else:
        logger.info("Starting polling mode.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
