import os
import time
import logging
import json
import datetime
import re
import uuid
from typing import Dict, Optional
import gspread
from google.auth.transport.requests import Request
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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
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
) = range(6)

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
        data = {
            "user_id": row_values[0] if len(row_values) > 0 else str(user_id),
            "username": row_values[1] if len(row_values) > 1 else "N/A",
            "coin_balance": row_values[2] if len(row_values) > 2 else "0",
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


def get_product_keyboard(product_type: str) -> InlineKeyboardMarkup:
    config = get_config_data()
    keyboard_buttons = []
    prefix = f"{product_type}_"
    product_keys = sorted([k for k in config.keys() if k.startswith(prefix)])
    for key in product_keys:
        price = config.get(key)
        if price:
            button_name = key.replace(prefix, "").replace("_", " ").title()
            button_text = f"{'⭐' if product_type == 'star' else '💎'} {button_name} ({price} MMK)"
            keyboard_buttons.append([InlineKeyboardButton(button_text, callback_data=f"{key}")])

    keyboard_buttons.append([InlineKeyboardButton("⬅️ Back to Service Menu", callback_data="menu_back")])
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


# MODIFIED: Reply keyboard now only has User Info, Payment Method, Help Center
ENGLISH_REPLY_KEYBOARD = [
    [KeyboardButton("👤 User Info"), KeyboardButton("💰 Payment Method")],
    [KeyboardButton("❓ Help Center")]
]
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(ENGLISH_REPLY_KEYBOARD, resize_keyboard=True, one_time_keyboard=False)

# MODIFIED: Initial Inline Keyboard now only has product selection buttons
INITIAL_INLINE_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("⭐ Telegram Star", callback_data="product_star")],
        [InlineKeyboardButton("💎 Telegram Premium", callback_data="product_premium")],
    ]
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
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user_if_not_exists(user.id, user.full_name)
    if is_user_banned(user.id):
        # Keep Burmese ban message as it is likely crucial for the audience
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားထားသည်။ Support ထံ ဆက်သွယ်ပါ။")
        return
    welcome_text = f"Hello, **{user.full_name}**!\nWelcome — choose from the menu below."
    # Send the main menu reply keyboard
    await update.message.reply_text(welcome_text, reply_markup=MAIN_MENU_KEYBOARD, parse_mode="Markdown")
    # Then show the service menu in a separate message below it
    await show_service_menu(update, context, welcome_msg=False) # Don't repeat welcome text


# MODIFIED: show_service_menu now only shows products
async def show_service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, welcome_msg: bool = True):
    caller_id = None
    if update.callback_query:
        caller_id = update.callback_query.from_user.id
    elif update.message:
        caller_id = update.message.from_user.id
    if caller_id and is_user_banned(caller_id):
        if update.callback_query:
            await update.callback_query.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        else:
            await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return

    text = "Available Services (Star & Premium):"
    
    # If called from a callback query, attempt to edit the message to show the menu
    if update.callback_query:
        try:
            # Edit the message the callback came from
            await update.callback_query.message.edit_text(text, reply_markup=INITIAL_INLINE_KEYBOARD)
        except Exception:
            # If editing fails (e.g., message too old), send a new message
            await update.callback_query.message.reply_text(text, reply_markup=INITIAL_INLINE_KEYBOARD)
    # If called from a command/message, send a new message
    elif update.message:
        await update.message.reply_text(text, reply_markup=INITIAL_INLINE_KEYBOARD)


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
        f"🔸 **Coin Balance:** **{data.get('coin_balance')}**\n"
        f"🔸 **Registered Since:** {data.get('registration_date')}\n"
        f"🔸 **Banned:** {data.get('banned')}\n"
    )
    # Use menu_back to return to the state where the service menu is shown
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
    # Use menu_back to return to the state where the service menu is shown
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")]])
    if update.callback_query:
        # A help_center callback is no longer an entry point, but keep the handler in case
        # a message is edited to contain it in the future, or to support the old flow.
        await update.callback_query.message.reply_text(help_text, reply_markup=back_keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(help_text, reply_markup=back_keyboard, parse_mode="Markdown")


# ----------- Payment Flow (coin package -> payment method -> receipt) -----------
# MODIFIED: This is the entry point for the conversation from the reply keyboard.
# It should not show the service menu.
async def handle_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return ConversationHandler.END
    # Show coin package keyboard first
    if update.callback_query:
        # This branch is likely dead if the reply keyboard is used as the entry point
        await update.callback_query.message.reply_text("💰 Select Coin Package:", reply_markup=get_coin_package_keyboard())
    else:
        # This is the primary entry point from the reply button
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
    admin_contact_id = int(os.environ.get("ADMIN_ID", ADMIN_ID))
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    receipt_meta = {
        "from_user_id": user.id,
        "from_username": user.username or user.full_name,
        "timestamp": timestamp,
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
        if amounts_cfg:
            try:
                choices = [int(x.strip()) for x in amounts_cfg.split(",") if x.strip().isdigit()]
            except Exception:
                choices = []
        else:
            choices = [2000, 4000, 10000, 20000]

        if detected_amount and detected_amount not in choices:
            choices = [detected_amount] + choices

        kb_rows = []
        row = []
        for i, amt in enumerate(choices):
            row.append(InlineKeyboardButton(f"✅ Approve {amt} MMK", callback_data=f"admin_approve_receipt|{user.id}|{timestamp}|{amt}"))
            if len(row) == 2:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        kb_rows.append([InlineKeyboardButton("❌ Deny", callback_data=f"admin_deny_receipt|{user.id}|{timestamp}")])

        await context.bot.send_message(
            chat_id=admin_contact_id,
            text=f"📥 Receipt from @{user.username or user.full_name} (id:{user.id}) Time: {timestamp}",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
    except Exception as e:
        logger.error("Failed to forward receipt to admin: %s", e)
        await update.message.reply_text("❌ Could not forward receipt to admin. Please try again later.")
        return ConversationHandler.END

    await update.message.reply_text("💌 Receipt sent to Admin. You will be notified after approval.")
    return ConversationHandler.END


# Admin callbacks for receipts
async def admin_approve_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # admin_approve_receipt|<user_id>|<timestamp>|<amount>
    parts = data.split("|")
    if len(parts) < 4:
        await query.message.reply_text("Invalid admin action.")
        return

    _, user_id_str, ts, amount_str = parts[0], parts[1], parts[2], parts[3]
    try:
        user_id = int(user_id_str)
        approved_amount = int(amount_str)
    except ValueError:
        await query.message.reply_text("Invalid parameters.")
        return

    if query.from_user.id != ADMIN_ID:
        await query.message.reply_text("You are not authorized to perform this action.")
        return

    config = get_config_data()
    # ratio: mmk -> coins (user requested: 1 MMK = 0.5 coin)
    try:
        ratio = float(config.get("mmk_to_coins_ratio", "0.5"))
    except Exception:
        ratio = 0.5
    coins_to_add = int(approved_amount * ratio)

    user_data = get_user_data_from_sheet(user_id)
    try:
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
        "notes": f"Receipt approved by admin {query.from_user.id} at {ts}",
        "processed_by": str(query.from_user.id),
    }
    log_order(order)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Your payment of {approved_amount} MMK has been approved by admin. {coins_to_add} Coins added. New balance: {new_balance} Coins.",
        )
        await query.message.reply_text("✅ Approved and user balance updated.")
    except Exception as e:
        logger.error("Failed to notify user after approval: %s", e)
        await query.message.reply_text("Approved but failed to notify user.")


async def admin_deny_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("|")
    if len(parts) < 3:
        await query.message.reply_text("Invalid admin action.")
        return
    _, user_id_str, ts = parts
    try:
        user_id = int(user_id_str)
    except ValueError:
        await query.message.reply_text("Invalid user id.")
        return

    if query.from_user.id != ADMIN_ID:
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
        "notes": f"Receipt denied by admin {query.from_user.id} at {ts}",
        "processed_by": str(query.from_user.id),
    }
    log_order(order)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Admin has denied your payment/receipt. Please contact support or retry the payment.",
        )
        await query.message.reply_text("❌ Denied and user notified.")
    except Exception as e:
        logger.error("Failed to notify user after denial: %s", e)
        await query.message.reply_text("Denied but failed to notify user.")


# ----------- Product purchase flow (unchanged) -----------
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
    return WAITING_FOR_PHONE


async def validate_phone_and_ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if PHONE_RE.match(text):
        context.user_data["premium_phone"] = text
        await update.message.reply_text(
            f"Thank you. Now please send the **Telegram Username** associated with {text} (start with @ or plain username)."
        )
        return WAITING_FOR_USERNAME
    else:
        await update.message.reply_text("❌ Invalid phone. Send digits only (8-15 digits).")
        return WAITING_FOR_PHONE


async def finalize_product_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if is_user_banned(user_id):
        await update.message.reply_text("❌ သင့်အကောင့်အား ပိတ်ထားပါသည်။")
        return ConversationHandler.END

    product_key = context.user_data.get("product_key")
    premium_phone = context.user_data.get("premium_phone", "")
    raw_username = (update.message.text or "").strip()
    premium_username = normalize_username(raw_username)

    if not product_key:
        await update.message.reply_text("❌ No product selected. Please start again.")
        return ConversationHandler.END

    config = get_config_data()
    price_mmk_str = config.get(product_key)
    if price_mmk_str is None:
        await update.message.reply_text("❌ Price for this product not found in config.")
        return ConversationHandler.END

    try:
        price_needed = int(price_mmk_str)
    except ValueError:
        await update.message.reply_text("❌ Product price in config is invalid.")
        return ConversationHandler.END

    user_data = get_user_data_from_sheet(user_id)
    try:
        user_coins = int(user_data.get("coin_balance", "0"))
    except ValueError:
        user_coins = 0

    if user_coins < price_needed:
        await update.message.reply_text(
            f"❌ Insufficient coin balance. You need {price_needed} but have {user_coins}. Use '💰 Payment Method' to top up."
        )
        order = {
            "order_id": str(uuid.uuid4()),
            "user_id": user_id,
            "username": user_data.get("username", ""),
            "product_key": product_key,
            "price_mmk": price_needed,
            "phone": premium_phone,
            "premium_username": premium_username,
            "status": "FAILED_INSUFFICIENT_FUNDS",
            "notes": "User attempted purchase without sufficient coins.",
        }
        log_order(order)
        return ConversationHandler.END

    new_balance = user_coins - price_needed
    ok = update_user_balance(user_id, new_balance)
    if not ok:
        await update.message.reply_text("❌ Failed to deduct coins. Please contact admin.")
        return ConversationHandler.END

    order = {
        "order_id": str(uuid.uuid4()),
        "user_id": user_id,
        "username": user_data.get("username", ""),
        "product_key": product_key,
        "price_mmk": price_needed,
        "phone": premium_phone,
        "premium_username": premium_username,
        "status": "ORDER_PLACED",
        "notes": "Order placed and coins deducted.",
    }
    log_order(order)

    await update.message.reply_text(
        f"✅ Order successful! {price_needed} Coins have been deducted for {product_key.replace('_',' ').upper()}.\n"
        f"New balance: {new_balance} Coins. Please wait while service is processed."
    )
    try:
        admin_msg = (
            f"🛒 New Order\n"
            f"Order ID: {order['order_id']}\n"
            f"User: @{user.username or user.full_name} (id:{user_id})\n"
            f"Product: {product_key}\n"
            f"Price: {price_needed}\n"
            f"Phone: {premium_phone}\n"
            f"Username: {premium_username}\n"
        )
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg)
    except Exception as e:
        logger.error("Failed to notify admin about order: %s", e)

    return ConversationHandler.END


# Global back to service menu (menu_back)
async def back_to_service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Edit the message the callback came from (e.g., User Info, Help Center, or product selection)
    await show_service_menu(update, context) 
    return ConversationHandler.END


# Admin commands (ban/unban)
async def admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
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
    user = update.effective_user
    if user.id != ADMIN_ID:
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


# Error handler (sanitized)
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err_type = type(context.error).__name__ if context.error else "UnknownError"
    err_msg = str(context.error)[:1000] if context.error else "No details"
    logger.error("Exception while handling an update: %s: %s", err_type, err_msg)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
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
        fallbacks=[MessageHandler(filters.Text("💰 Payment Method"), handle_payment_method)],
        allow_reentry=True,
    )
    application.add_handler(payment_conv_handler)

    # Product Conversation Handler
    product_purchase_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_product_purchase, pattern=r"^product_")],
        states={
            SELECT_PRODUCT_PRICE: [
                CallbackQueryHandler(select_product_price, pattern=r"^(star_|premium_).*"),
                CallbackQueryHandler(back_to_service_menu, pattern=r"^menu_back$"),
            ],
            WAITING_FOR_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, validate_phone_and_ask_username)],
            WAITING_FOR_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_product_order)],
        },
        fallbacks=[CallbackQueryHandler(back_to_service_menu, pattern=r"^menu_back$")],
        allow_reentry=True,
    )
    application.add_handler(product_purchase_handler)

    # Message handlers for reply keyboard (which now ensures the service menu is hidden)
    application.add_handler(MessageHandler(filters.Text("👤 User Info"), handle_user_info))
    application.add_handler(MessageHandler(filters.Text("❓ Help Center"), handle_help_center))
    
    # Inline callbacks: products
    application.add_handler(CallbackQueryHandler(start_product_purchase, pattern=r"^product_"))
    
    # Admin callback handlers for approve/deny
    application.add_handler(CallbackQueryHandler(admin_approve_receipt_callback, pattern=r"^admin_approve_receipt\|"))
    application.add_handler(CallbackQueryHandler(admin_deny_receipt_callback, pattern=r"^admin_deny_receipt\|"))

    # Back/menu callback (This is crucial for returning to the Service Menu)
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
