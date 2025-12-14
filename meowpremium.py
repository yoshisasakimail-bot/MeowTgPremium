import os
import time
import logging
import json
import datetime
import re
import uuid
from typing import Dict, Optional
# gspread, google.auth module များကို ဖယ်ရှားသည်။
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
# ADMIN_ID_DEFAULT ကို Define လုပ်ထားသည်။
ADMIN_ID_DEFAULT = 123456789
# ADMIN_ID ကို ENV မှ တိုက်ရိုက်ယူသည်။ SHEET_ID/GSPREAD_SA_JSON တို့ကို ဖယ်ရှားသည်။
ADMIN_ID = int(os.environ.get("ADMIN_ID", ADMIN_ID_DEFAULT))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", "8080"))

# Sheets global objects များကို ဖယ်ရှားသည်။ (Sheet မသုံးတော့သောကြောင့်)
# GSHEET_CLIENT: Optional[gspread.Client] = None
# WS_USER_DATA = None
# WS_CONFIG = None
# WS_ORDERS = None

# Config cache နှင့်ပတ်သက်သော code များကို ဖယ်ရှားသည်။
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
# initialize_sheets function ကို ဖယ်ရှားသည်။

# ------------ Config reading & caching: Mock functions ----------------
# Google Sheet မသုံးတော့သော်လည်း၊ config, user data, order log function များသည် ရှိနေရမည်။
# ထို့ကြောင့် config/user data များကို hardcode/mock ပြုလုပ်ထားပါသည်။
# * သတိပြုရန်: ဤဖိုင်သည် Google Sheet ကို အသုံးပြုခြင်း မရှိတော့သောကြောင့် 
# * user data (ဥပမာ: coin balance) နှင့် order history များသည် သိမ်းဆည်းနိုင်မည် မဟုတ်ပါ။ 
# * အကယ်၍ Database မရှိပါက Bot ကို restart လုပ်တိုင်း data များ ပျောက်ဆုံးသွားပါမည်။

def _read_config_sheet() -> Dict[str, str]:
    # Config sheet ကို hardcode လုပ်သည် (သို့မဟုတ်) Sheet မသုံးလိုပါက ဤနေရာတွင် Hardcode data ထည့်သွင်းရမည်။
    # Sheet ကို အသုံးပြုခြင်း မရှိတော့ပါက ဒီ Mock Data ကို အသုံးျပုရပါမည်။
    # Admin username ကို ADMIN_ID ဖြင့် ပြောင်းလဲသည်။
    return {
        "admin_contact_username": f"@{ADMIN_ID}", # Admin ID ကို hardcode ထည့်သည်။
        "star_100": "20000",
        "star_50": "10000",
        "premium_1month": "15000",
        "premium_3month": "40000",
        "coinpkg_1000": "2000",
        "coinpkg_2000": "4000",
        "kpay_name": "Kpay User",
        "kpay_phone": "09987654321",
        "wave_name": "Wave User",
        "wave_phone": "09123456789",
        "mmk_to_coins_ratio": "0.5",
        "receipt_approve_amounts": "2000, 4000, 10000, 20000",
    }


def get_config_data(force_refresh: bool = False) -> Dict[str, str]:
    global CONFIG_CACHE
    now = time.time()
    # Sheet ကိုဖတ်ရန် မလိုတော့ဘဲ Mock data ကိုသာ ပြန်ပေးသည်။
    if force_refresh or (now - CONFIG_CACHE["ts"] > CONFIG_TTL_SECONDS):
        CONFIG_CACHE["data"] = _read_config_sheet()
        CONFIG_CACHE["ts"] = now
    return CONFIG_CACHE["data"]


# NEW Helper: Get Admin ID from config sheet, falling back to global default
def get_dynamic_admin_id(config: Dict) -> int:
    """Retrieves ADMIN_ID from global variable as sheet is no longer used."""
    # Sheet မှ မယူဘဲ global ADMIN_ID ကို တိုက်ရိုက်ပြန်ပေးသည်။
    return ADMIN_ID


# ------------ User data helpers: Mocking Sheet Interaction ----------------
# Sheet မသုံးတော့သောကြောင့် ဤ functions များသည် In-memory data ကို အသုံးပြုရမည် သို့မဟုတ် 
# မည်သည့်အရာကိုမှ သိမ်းဆည်းနိုင်မည်မဟုတ်ကြောင်း သတိပြုရန်။
# လက်ရှိတွင် ဤ helper များကို Mock ပြုလုပ်ထားပါသည်။

# In-memory storage for user data (Volatile - will reset on restart)
MOCK_USER_DATA = {}

def find_user_row(user_id: int) -> Optional[int]:
    # Mocking: Check if user exists in in-memory dict
    return user_id if user_id in MOCK_USER_DATA else None


def get_user_data_from_sheet(user_id: int) -> Dict[str, str]:
    default = {"user_id": str(user_id), "username": "N/A", "coin_balance": "0", "registration_date": "N/A", "banned": "FALSE"}
    # Mocking: return data from in-memory dict or default
    data = MOCK_USER_DATA.get(user_id, default)
    # Ensure keys are present even if from mock data
    return {
        "user_id": str(data.get("user_id", str(user_id))),
        "username": data.get("username", "N/A"),
        "coin_balance": str(data.get("coin_balance", "0")),
        "registration_date": data.get("registration_date", "N/A"),
        "last_active": data.get("last_active", ""),
        "total_purchase": str(data.get("total_purchase", "0")),
        "banned": data.get("banned", "FALSE"),
    }


def register_user_if_not_exists(user_id: int, username: str) -> None:
    if user_id not in MOCK_USER_DATA:
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        MOCK_USER_DATA[user_id] = {
            "user_id": str(user_id), 
            "username": username or "N/A", 
            "coin_balance": "0", 
            "registration_date": now, 
            "last_active": now,
            "total_purchase": "0",
            "banned": "FALSE"
        }
        logger.info("Registered new mock user %s", user_id)
    else:
        # Update last active time for existing user
        MOCK_USER_DATA[user_id]["last_active"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        # Update username if it changed
        MOCK_USER_DATA[user_id]["username"] = username or "N/A"


def update_user_balance(user_id: int, new_balance: int) -> bool:
    if user_id in MOCK_USER_DATA:
        MOCK_USER_DATA[user_id]["coin_balance"] = str(new_balance)
        MOCK_USER_DATA[user_id]["last_active"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("Mock balance update for %s: %s", user_id, new_balance)
        return True
    logger.error("update_user_balance: mock user row not found for %s", user_id)
    return False


def set_user_banned_status(user_id: int, banned: bool) -> bool:
    if user_id in MOCK_USER_DATA:
        MOCK_USER_DATA[user_id]["banned"] = "TRUE" if banned else "FALSE"
        logger.info("Mock banned status update for %s: %s", user_id, MOCK_USER_DATA[user_id]["banned"])
        return True
    logger.error("set_user_banned_status: mock user row not found for %s", user_id)
    return False


def is_user_banned(user_id: int) -> bool:
    data = get_user_data_from_sheet(user_id)
    return str(data.get("banned", "FALSE")).upper() == "TRUE"


# ------------ Orders logging: Mocking Sheet Interaction ----------------
MOCK_ORDERS = [] # In-memory order log

def log_order(order: Dict) -> bool:
    try:
        order_id = order.get("order_id") or str(uuid.uuid4())
        # Append only essential details for mock logging
        order_entry = {
            "order_id": order_id,
            "user_id": order.get("user_id", ""),
            "product_key": order.get("product_key", ""),
            "status": order.get("status", "PENDING"),
            "timestamp": order.get("timestamp", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        }
        MOCK_ORDERS.append(order_entry)
        logger.info("Logged mock order: %s", order_id)
        return True
    except Exception as e:
        logger.error("log_order mock error: %s", e)
        return False


# ------------ Keyboards (No change needed) ----------------
# ... (Keyboards section is unchanged as it uses get_config_data which is now mocked)
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


# New inline keyboard for the service selection (only Star and Premium)
PRODUCT_SELECTION_INLINE_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("⭐ Telegram Star", callback_data="product_star")],
        [InlineKeyboardButton("💎 Telegram Premium", callback_data="product_premium")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_back")] # Added back button
    ]
)

# NEW: Reply keyboard for cancelling product purchase flow
CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Cancel Order")]],
    resize_keyboard=True,
    one_time_keyboard=True # Use one_time_keyboard for temporary keyboards
)


# ------------ Validation helpers (No change needed) ----------------
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
# MODIFIED: start_command only sends the reply keyboard (no inline menu)
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


# NEW: Function to display the Star/Premium inline buttons, triggered by the new Reply Button
async def show_product_inline_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
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
        f"🔸 **Coin Balance:** **{data.get('coin_balance')}**\n"
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
    # MODIFIED: Get Admin ID from global ADMIN_ID
    admin_contact_id = ADMIN_ID
    
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
                # ဤနေရာတွင် စာလုံးမှားယွင်းပါက ValueError တက်ပါသည်။
                choices = [int(x.strip()) for x in amounts_cfg.split(",") if x.strip().isdigit()]
            except Exception:
                # Configuration မှားယွင်းပါက Default သို့ ပြန်သွားပါမည်။
                choices = [2000, 4000, 10000, 20000]

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
        # Error တက်ပါက Bot မှ Admin သို့ Approval Button များပို့ရန် မအောင်မြင်ပါ။
        logger.error("Failed to send receipt buttons to admin: %s", e)
        await update.message.reply_text("❌ Could not forward receipt to admin. Please try again later. Please check your ADMIN_ID and Bot permissions.")
        return ConversationHandler.END

    await update.message.reply_text("💌 Receipt sent to Admin. You will be notified after approval.")
    return ConversationHandler.END


# Admin callbacks for receipts (unchanged)
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

    # MODIFIED: Get ADMIN_ID from global variable for authorization check
    admin_id_check = ADMIN_ID
    
    if query.from_user.id != admin_id_check:
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

    # MODIFIED: Get ADMIN_ID from global variable for authorization check
    admin_id_check = ADMIN_ID

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
        price_needed = int(price_mmk_str)
    except ValueError:
        await update.message.reply_text("❌ Product price in config is invalid.", reply_markup=MAIN_MENU_KEYBOARD)
        return ConversationHandler.END

    user_data = get_user_data_from_sheet(user_id)
    try:
        user_coins = int(user_data.get("coin_balance", "0"))
    except ValueError:
        user_coins = 0

    if user_coins < price_needed:
        await update.message.reply_text(
            f"❌ Insufficient coin balance. You need {price_needed} but have {user_coins}. Use '💰 Payment Method' to top up.",
            reply_markup=MAIN_MENU_KEYBOARD
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
        await update.message.reply_text("❌ Failed to deduct coins. Please contact admin.", reply_markup=MAIN_MENU_KEYBOARD)
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
    
    # MODIFIED: Get ADMIN_ID from global variable
    admin_id_check = ADMIN_ID


    await update.message.reply_text(
        f"✅ Order successful! {price_needed} Coins have been deducted for {product_key.replace('_',' ').upper()}.\n"
        f"New balance: {new_balance} Coins. Please wait while service is processed.",
        reply_markup=MAIN_MENU_KEYBOARD # Show main menu keyboard on success
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
    
    welcome_text = "Welcome back to the main menu. Choose from the options below."

    # Use reply_text which sends a new message with the Reply Keyboard.
    # The new Reply Keyboard contains "Premium & Star" and "Help Center".
    try:
        # Delete the previous inline message if possible
        await query.message.delete()
    except Exception:
        pass # Ignore error if delete fails (e.g., message is too old)

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=welcome_text,
        reply_markup=MAIN_MENU_KEYBOARD,
    )
    return ConversationHandler.END # Exit any active conversation state


# Admin commands (ban/unban) - Updated to use config ID
async def admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # MODIFIED: Check against global ADMIN_ID
    admin_id_check = ADMIN_ID
    
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
    # MODIFIED: Check against global ADMIN_ID
    admin_id_check = ADMIN_ID
    
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
    
    # MODIFIED: Send error to global ADMIN_ID
    admin_id_check = ADMIN_ID

    try:
        await context.bot.send_message(
            chat_id=admin_id_check,
            text=f"🚨 Bot Error: {err_type}\n{err_msg}",
        )
    except Exception:
        pass


# --------------- Main ---------------
def main():
    # MODIFIED: initialize_sheets() ကို ဖယ်ရှားသည်။
    # ok = initialize_sheets()
    # if not ok:
    #     logger.error("Bot cannot start due to Google Sheets initialization failure.")
    #     return

    if not BOT_TOKEN:
        logger.error("Missing BOT_TOKEN environment variable.")
        return
    
    # **သတိပြုရန်**: Sheet ကို မသုံးတော့သောကြောင့် user data (coin balance, registration) နှင့် order log များသည်
    # Bot restart လုပ်တိုင်း ပျောက်ဆုံးသွားပါမည်။ ၎င်းကို Mocking Functions များဖြင့် အစားထိုးထားပါသည်။

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

    # Message handlers for reply keyboard
    application.add_handler(MessageHandler(filters.Text("👤 User Info"), handle_user_info))
    application.add_handler(MessageHandler(filters.Text("❓ Help Center"), handle_help_center))
    
    # NEW: Handler for the "Premium & Star" Reply Button
    application.add_handler(MessageHandler(filters.Text("✨ Premium & Star"), show_product_inline_menu))
    
    # Inline callbacks: products
    application.add_handler(CallbackQueryHandler(start_product_purchase, pattern=r"^product_"))
    
    # Admin callback handlers for approve/deny
    application.add_handler(CallbackQueryHandler(admin_approve_receipt_callback, pattern=r"^admin_approve_receipt\|"))
    application.add_handler(CallbackQueryHandler(admin_deny_receipt_callback, pattern=r"^admin_deny_receipt\|"))

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
