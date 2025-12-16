import logging
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler
)

logger = logging.getLogger(__name__)

# ==============================
# Conversation States
# ==============================
(
    AWAIT_CASH_CONTROL_ID,
    AWAIT_CASH_CONTROL_AMOUNT
) = range(30, 32)


# ==============================
# Utilities
# ==============================
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from main_bot import get_config_data, get_dynamic_admin_id, ADMIN_REPLY_KEYBOARD

        user = update.effective_user
        config = get_config_data()
        admin_id = get_dynamic_admin_id(config)

        if user.id != admin_id:
            await update.message.reply_text(
                "⛔ **Access Denied**\nAdmin only command.",
                parse_mode="Markdown",
                reply_markup=ADMIN_REPLY_KEYBOARD
            )
            return ConversationHandler.END

        return await func(update, context)
    return wrapper


# ==============================
# Admin Dashboard
# ==============================
@admin_only
async def show_admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👾 Broadcast", callback_data="admin_broadcast"),
            InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")
        ],
        [
            InlineKeyboardButton("💰 Cash Control", callback_data="admin_cash"),
            InlineKeyboardButton("🔄 Refresh Config", callback_data="admin_refresh")
        ]
    ])

    await update.message.reply_text(
        "⚙️ **ADMIN DASHBOARD**\n\n"
        "🟢 Bot Status : Online\n"
        "🟢 Selling    : Open\n\n"
        "Select an action below 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# ==============================
# Broadcast
# ==============================
@admin_only
async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👾 **Broadcast Mode**\n\n"
        "Send the message you want to broadcast to all users.",
        parse_mode="Markdown"
    )


# ==============================
# User Search
# ==============================
@admin_only
async def handle_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👤 **User Search**\n\n"
        "Send User ID or @username.",
        parse_mode="Markdown"
    )


# ==============================
# Refresh Config
# ==============================
@admin_only
async def handle_refresh_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from main_bot import get_config_data
    get_config_data(force_refresh=True)

    await update.message.reply_text(
        "🔄 **Configuration Updated**\n\n"
        "Google Sheet data refreshed successfully.",
        parse_mode="Markdown"
    )


# ==============================
# Statistics
# ==============================
@admin_only
async def handle_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Example values (later DB connect)
    total_users = 1250
    total_orders = 342
    total_revenue = "2,450,000 MMK"

    await update.message.reply_text(
        "📊 **Bot Statistics**\n\n"
        f"👥 Users      : {total_users}\n"
        f"📦 Orders    : {total_orders}\n"
        f"💰 Revenue   : {total_revenue}",
        parse_mode="Markdown"
    )


# ==============================
# Cash Control (Conversation)
# ==============================
@admin_only
async def start_cash_control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 **CASH CONTROL**\n\n"
        "Enter User ID or @username:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [["⬅️ Cancel"]],
            resize_keyboard=True
        )
    )
    return AWAIT_CASH_CONTROL_ID


async def cash_control_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "⬅️ Cancel":
        return await cash_control_cancel(update, context)

    context.user_data["cash_user"] = text

    await update.message.reply_text(
        "💵 Enter amount to add / deduct:\n"
        "Example: `+5000` or `-2000`",
        parse_mode="Markdown"
    )
    return AWAIT_CASH_CONTROL_AMOUNT


async def cash_control_apply_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from main_bot import ADMIN_REPLY_KEYBOARD

    amount_text = update.message.text.strip()
    target_user = context.user_data.get("cash_user")

    try:
        amount = int(amount_text)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Try again.")
        return AWAIT_CASH_CONTROL_AMOUNT

    # 👉 Database update logic here

    await update.message.reply_text(
        "✅ **Cash Updated Successfully**\n\n"
        f"User : `{target_user}`\n"
        f"Amount : `{amount}`",
        parse_mode="Markdown",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cash_control_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import ADMIN_REPLY_KEYBOARD

    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cash Control cancelled.",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    return ConversationHandler.END
