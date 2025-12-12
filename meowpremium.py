import os
import time
import logging
import json
import datetime
from typing import Dict, Optional, Tuple

import gspread
from google.auth.transport.requests import Request  # available via google-auth
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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))  # change with real admin
SHEET_ID = os.environ.get("SHEET_ID", "")  # Google Sheet ID
GSPREAD_SA_JSON = os.environ.get("GSPREAD_SA_JSON", "")  # service account JSON string
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")  # webhook base url
PORT = int(os.environ.get("PORT", "8080"))

# Sheets global objects (initialized later)
GSHEET_CLIENT: Optional[gspread.Client] = None
WS_USER_DATA = None
WS_CONFIG = None
WS_ORDERS = None

# Config cache (store dict + timestamp)
CONFIG_CACHE: Dict = {"data": {}, "ts": 0}
CONFIG_TTL_SECONDS = int(os.environ.get("CONFIG_TTL_SECONDS", "25"))  # small cache

# Conversation states
(
    CHOOSING_PAYMENT_METHOD,
    WAITING_FOR_RECEIPT,
    SELECT_PRODUCT_PRICE,
    WAITING_FOR_PHONE,
    WAITING_FOR_USERNAME,
) = range(5)

# ------------ Helper: Retry wrapper for sheet init ----------------
def initialize_sheets(retries: int = 3, backoff: float = 2.0) -> bool:
    """
    Initialize gspread client and worksheets. Retries on failure.
    Expects GSPREAD_SA_JSON & SHEET_ID env vars to be present.
    """
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

            # Worksheets expected to exist:
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
    """Read config sheet into a dict {key: value}."""
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
    """Return config dict; use cache unless forced refresh."""
    global CONFIG_CACHE
    now = time.time()
    if force_refresh or (now - CONFIG_CACHE["ts"] > CONFIG_TTL_SECONDS):
        CONFIG_CACHE["data"] = _read_config_sheet()
        CONFIG_CACHE["ts"] = now
    return CONFIG_CACHE["data"]


# ------------ User data helpers ----------------
def find_user_row(user_id: int) -> Optional[int]:
    """Return row number in WS_USER_DATA where column1 == user_id (as string)."""
    global WS_USER_DATA
    if not WS_USER_DATA:
        return None
    try:
        cell = WS_USER_DATA.find(str(user_id), in_column=1)
        if cell:
            return cell.row
    except Exception as e:
        # find raises if not found sometimes; handle gracefully
        logger.debug("find_user_row exception: %s", e)
    return None


def get_user_data_from_sheet(user_id: int) -> Dict[str, str]:
    """Return a dict for the user from user_data sheet."""
    global WS_USER_DATA
    default = {"user_id": str(user_id), "username": "N/A", "coin_balance": "0", "registration_date": "N/A"}
    if not WS_USER_DATA:
        return default
    try:
        row = find_user_row(user_id)
        if not row:
            return default
        row_values = WS_USER_DATA.row_values(row)
        # Expected columns: user_id, username, coin_balance, registration_date, last_active, total_purchase, banned
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
    """Append a new user row if not exists."""
    global WS_USER_DATA
    if not WS_USER_DATA:
        logger.error("WS_USER_DATA not available.")
        return
    try:
        if find_user_row(user_id) is None:
            now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            # columns: user_id, username, coin_balance, registration_date, last_active, total_purchase, banned
            new_row = [str(user_id), username or "N/A", "0", now, now, "0", "FALSE"]
            WS_USER_DATA.append_row(new_row, value_input_option="USER_ENTERED")
            logger.info("Registered new user %s", user_id)
    except Exception as e:
        logger.error("Error registering user: %s", e)


def update_user_balance(user_id: int, new_balance: int) -> bool:
    """Set user's coin_balance cell to new_balance. Returns True on success."""
    global WS_USER_DATA
    row = find_user_row(user_id)
    if not row:
        logger.error("update_user_balance: user row not found for %s", user_id)
        return False
    try:
        # coin_balance is column 3 (index 2)
        WS_USER_DATA.update_cell(row, 3, str(new_balance))
        # update last_active and (optionally) total_purchase/time
        WS_USER_DATA.update_cell(row, 5, datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))  # last_active column (5)
        return True
    except Exception as e:
        logger.error("Failed to update user balance: %s", e)
        return False


# ------------ Orders logging ----------------
def log_order(order: Dict) -> bool:
    """
    Append an order to the orders sheet.
    Expected fields: user_id, username, product_key, price_mmk, phone, premium_username, status, timestamp, notes
    """
    global WS_ORDERS
    if not WS_ORDERS:
        logger.error("WS_ORDERS not initialized.")
        return False
    try:
        row = [
            order.get("user_id", ""),
            order.get("username", ""),
            order.get("product_key", ""),
            str(order.get("price_mmk", "")),
            order.get("phone", ""),
            order.get("premium_username", ""),
            order.get("status", "PENDING"),
            order.get("timestamp", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            order.get("notes", ""),
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


# Reply keyboard
ENGLISH_REPLY_KEYBOARD = [
    [KeyboardButton("👤 User Info"), KeyboardButton("💰 Payment Method")],
    [KeyboardButton("❓ Help Center")],
]
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(ENGLISH_REPLY_KEYBOARD, resize_keyboard=True, one_time_keyboard=False)

INITIAL_INLINE_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("⭐ Telegram Star", callback_data="product_star")],
        [InlineKeyboardButton("💎 Telegram Premium", callback_data="product_premium")],
    ]
)


# ------------ Handlers ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user_if_not_exists(user.id, user.full_name)
    welcome_text = f"Hello, **{user.full_name}**!\nWelcome — choose from the menu below."
    await update.message.reply_text(welcome_text, reply_markup=MAIN_MENU_KEYBOARD, parse_mode="Markdown")
    await show_service_menu(update, context)


async def show_service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the initial service selection menu."""
    # support both callback_query and message
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text("Available Services:", reply_markup=INITIAL_INLINE_KEYBOARD)
        except Exception:
            await update.callback_query.message.reply_text("Available Services:", reply_markup=INITIAL_INLINE_KEYBOARD)
    else:
        await update.message.reply_text("Available Services:", reply_markup=INITIAL_INLINE_KEYBOARD)


async def handle_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_data_from_sheet(user.id)
    info_text = (
        f"👤 **User Information**\n\n"
        f"🔸 **Your ID:** `{data.get('user_id')}`\n"
        f"🔸 **Username:** {data.get('username')}\n"
        f"🔸 **Coin Balance:** **{data.get('coin_balance')}**\n"
        f"🔸 **Registered Since:** {data.get('registration_date')}\n"
    )
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")]])
    await update.message.reply_text(info_text, reply_markup=back_keyboard, parse_mode="Markdown")


async def handle_help_center(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config_data()
    admin_username = config.get("admin_contact_username", "@AdminUsername_Error")
    help_text = (
        "❓ **Help Center**\n\n"
        f"For assistance, contact the administrator:\nAdmin Contact: **{admin_username}**\n\n"
        "We will respond as soon as possible."
    )
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")]])
    if update.callback_query:
        await update.callback_query.message.reply_text(help_text, reply_markup=back_keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(help_text, reply_markup=back_keyboard, parse_mode="Markdown")


# ----------- Payment Flow -----------

async def handle_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point to payment conversation: show Kpay/Wave options."""
    keyboard = get_payment_keyboard()
    if update.callback_query:
        await update.callback_query.message.reply_text("💰 Select a method for coin purchase:", reply_markup=keyboard)
    else:
        await update.message.reply_text("💰 Select a method for coin purchase:", reply_markup=keyboard)
    return CHOOSING_PAYMENT_METHOD


async def start_payment_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """After user selects pay_kpay or pay_wave: show admin name/phone and ask receipt."""
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
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Payment Menu", callback_data="payment_back")]])
    transfer_text = (
        f"✅ Please transfer via **{payment_method.upper()}** as follows:\n\n"
        f"Name: **{admin_name}**\n"
        f"Phone Number: **{phone_number}**\n\n"
        "Please *send the receipt (screenshot)* here after transfer."
    )
    await query.message.reply_text(transfer_text, reply_markup=back_keyboard, parse_mode="Markdown")
    return WAITING_FOR_RECEIPT


async def back_to_payment_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💰 Select a method for coin purchase:", reply_markup=get_payment_keyboard())
    return CHOOSING_PAYMENT_METHOD


async def receive_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles receipt image or text from user. Forwards to admin with Approve/Deny buttons.
    Admin actions will update sheet and notify user.
    """
    user = update.effective_user
    config = get_config_data()
    admin_contact_id = int(os.environ.get("ADMIN_ID", ADMIN_ID))
    # Build a message to admin with inline approve/deny and encoded meta
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    # Save a receipt metadata in context.user_data for potential later use
    receipt_meta = {
        "from_user_id": user.id,
        "from_username": user.username or user.full_name,
        "timestamp": timestamp,
    }
    context.user_data["last_receipt_meta"] = receipt_meta

    # Forward photo if exist, else forward text
    try:
        if update.message.photo:
            # forward the largest photo to admin
            msg = await update.message.forward(chat_id=admin_contact_id)
            forwarded_note = f"📥 Receipt from @{user.username or user.full_name} (id:{user.id})\nTime: {timestamp}"
            # send admin approve/deny keyboard referencing user id and a small nonce (timestamp)
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Approve",
                            callback_data=f"admin_approve_receipt|{user.id}|{timestamp}",
                        ),
                        InlineKeyboardButton(
                            "❌ Deny",
                            callback_data=f"admin_deny_receipt|{user.id}|{timestamp}",
                        ),
                    ]
                ]
            )
            await context.bot.send_message(chat_id=admin_contact_id, text=forwarded_note, reply_markup=kb)
        else:
            # Text receipt -> forward text to admin
            text = update.message.text or "<no text>"
            forwarded_text = f"📥 Receipt (text) from @{user.username or user.full_name} (id:{user.id})\nTime: {timestamp}\n\n{text}"
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Approve",
                            callback_data=f"admin_approve_receipt|{user.id}|{timestamp}",
                        ),
                        InlineKeyboardButton(
                            "❌ Deny",
                            callback_data=f"admin_deny_receipt|{user.id}|{timestamp}",
                        ),
                    ]
                ]
            )
            await context.bot.send_message(chat_id=admin_contact_id, text=forwarded_text, reply_markup=kb)
    except Exception as e:
        logger.error("Failed to forward receipt to admin: %s", e)
        await update.message.reply_text("❌ Could not forward receipt to admin. Please try again later.")
        return ConversationHandler.END

    await update.message.reply_text("💌 Receipt sent to Admin. You will be notified after approval.")
    return ConversationHandler.END


# Admin callbacks for receipts
async def admin_approve_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin pressed Approve on a forwarded receipt. This should add coins (top-up) to the user."""
    query = update.callback_query
    await query.answer()
    data = query.data  # admin_approve_receipt|<user_id>|<timestamp>
    parts = data.split("|")
    if len(parts) < 3:
        await query.message.reply_text("Invalid admin action.")
        return

    action, user_id_str, ts = parts[0], parts[1], parts[2]
    try:
        user_id = int(user_id_str)
    except ValueError:
        await query.message.reply_text("Invalid user id.")
        return

    # For security: ensure this callback is from admin
    if query.from_user.id != ADMIN_ID:
        await query.message.reply_text("You are not authorized to perform this action.")
        return

    # How many coins to add? Admin can define in config, or we can default: For a receipt approval flow
    # here we will read config values: last_receipt_amount_default (fallback)
    config = get_config_data()
    default_topup_coins = int(config.get("default_topup_coins", "100"))

    # Update user coin balance in sheet
    user_data = get_user_data_from_sheet(user_id)
    try:
        current_coins = int(user_data.get("coin_balance", "0"))
    except ValueError:
        current_coins = 0
    new_balance = current_coins + default_topup_coins

    ok = update_user_balance(user_id, new_balance)
    if not ok:
        await query.message.reply_text("Failed to update user balance in sheet.")
        return

    # Log the topup as an order in WS_ORDERS with status APPROVED_RECEIPT
    order = {
        "user_id": user_id,
        "username": user_data.get("username", ""),
        "product_key": "COIN_TOPUP",
        "price_mmk": default_topup_coins,
        "phone": "",
        "premium_username": "",
        "status": "APPROVED_RECEIPT",
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": f"Receipt approved by admin {query.from_user.id} at {ts}",
    }
    log_order(order)

    # notify user
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Your payment has been approved by admin. {default_topup_coins} Coins added. New balance: {new_balance} Coins.",
        )
        await query.message.reply_text("✅ Approved and user balance updated.")
    except Exception as e:
        logger.error("Failed to notify user after approval: %s", e)
        await query.message.reply_text("Approved but failed to notify user.")


async def admin_deny_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin pressed Deny on a forwarded receipt."""
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

    # Log denial to orders
    order = {
        "user_id": user_id,
        "username": get_user_data_from_sheet(user_id).get("username", ""),
        "product_key": "COIN_TOPUP",
        "price_mmk": 0,
        "phone": "",
        "premium_username": "",
        "status": "DENIED_RECEIPT",
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": f"Receipt denied by admin {query.from_user.id} at {ts}",
    }
    log_order(order)

    # Notify user
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Admin has denied your payment/receipt. Please contact support or retry the payment.",
        )
        await query.message.reply_text("❌ Denied and user notified.")
    except Exception as e:
        logger.error("Failed to notify user after denial: %s", e)
        await query.message.reply_text("Denied but failed to notify user.")


# ----------- Product purchase flow -----------
async def start_product_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # pattern 'product_star' or 'product_premium'
    parts = query.data.split("_")
    if len(parts) < 2:
        await query.message.reply_text("Invalid product selection.")
        return ConversationHandler.END
    product_type = parts[1]
    context.user_data["product_type"] = product_type
    keyboard = get_product_keyboard(product_type)
    try:
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
    selected_key = query.data  # e.g., star_7days or premium_30days etc.
    context.user_data["product_key"] = selected_key
    # Ask for phone number
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
    text = update.message.text.strip()
    # Basic validation: digits and length 8-15
    if text.isdigit() and 8 <= len(text) <= 15:
        context.user_data["premium_phone"] = text
        await update.message.reply_text(
            f"Thank you. Now please send the **Telegram Username** associated with {text} (start with @ or plain username)."
        )
        return WAITING_FOR_USERNAME
    else:
        await update.message.reply_text("❌ Invalid phone. Send digits only (8-15 digits).")
        return WAITING_FOR_PHONE


async def finalize_product_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deduct coins, log order, and notify user. If insufficient coins, prompt to top up."""
    user = update.effective_user
    user_id = user.id
    product_key = context.user_data.get("product_key")
    premium_phone = context.user_data.get("premium_phone", "")
    premium_username = update.message.text.strip()

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
        # Log attempted order as FAILED_INSUFFICIENT_FUNDS
        order = {
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

    # Deduct coins and update sheet
    new_balance = user_coins - price_needed
    ok = update_user_balance(user_id, new_balance)
    if not ok:
        await update.message.reply_text("❌ Failed to deduct coins. Please contact admin.")
        return ConversationHandler.END

    # Log order as SUCCESS
    order = {
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
    # Optionally notify admin about new order
    try:
        admin_msg = (
            f"🛒 New Order\n"
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
    await show_service_menu(update, context)
    return ConversationHandler.END


# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    # notify admin
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚨 Bot Error: {type(context.error).__name__}\n{context.error}",
        )
    except Exception:
        pass


# --------------- Main ---------------
def main():
    ok = initialize_sheets()
    if not ok:
        logger.error("Bot cannot start due to Google Sheets initialization failure.")
        return

    if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
        logger.error("Missing BOT_TOKEN or RENDER_EXTERNAL_URL environment variable.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))

    # Payment Conversation Handler
    payment_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("💰 Payment Method"), handle_payment_method)],
        states={
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

    # Message handlers for reply keyboard
    application.add_handler(MessageHandler(filters.Text("👤 User Info"), handle_user_info))
    application.add_handler(MessageHandler(filters.Text("❓ Help Center"), handle_help_center))
    application.add_handler(MessageHandler(filters.Text("💰 Payment Method"), handle_payment_method))

    # Keyword handler
    keyword_filter = filters.TEXT & (filters.Regex(r"(?i)premium|star|price"))
    application.add_handler(MessageHandler(keyword_filter, show_service_menu))

    # Admin callback handlers for approve/deny
    application.add_handler(CallbackQueryHandler(admin_approve_receipt_callback, pattern=r"^admin_approve_receipt\|"))
    application.add_handler(CallbackQueryHandler(admin_deny_receipt_callback, pattern=r"^admin_deny_receipt\|"))

    # Back/menu callback
    application.add_handler(CallbackQueryHandler(back_to_service_menu, pattern=r"^menu_back$"))

    # Global error handler
    application.add_error_handler(error_handler)

    # Run webhook
    token = BOT_TOKEN
    listen = "0.0.0.0"
    port = PORT
    url_path = token
    webhook_url = f"{RENDER_EXTERNAL_URL}/{token}"
    print(f"Starting webhook on port {port}, URL: {webhook_url}")
    logger.info("Setting webhook URL to: %s", webhook_url)

    application.run_webhook(listen=listen, port=port, url_path=url_path, webhook_url=webhook_url)


if __name__ == "__main__":
    main()
