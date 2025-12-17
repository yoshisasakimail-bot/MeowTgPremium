import logging
import asyncio
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
    AWAIT_CASH_CONTROL_AMOUNT,
    AWAIT_BROADCAST_MESSAGE,
    CONFIRM_BROADCAST
) = range(30, 34)


# ==============================
# Utilities
# ==============================
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from meowpremium import get_config_data, get_dynamic_admin_id, ADMIN_REPLY_KEYBOARD

        user = update.effective_user
        config = get_config_data()
        admin_id = get_dynamic_admin_id(config)

        if user.id != admin_id:
            if update.message:
                await update.message.reply_text(
                    "⛔ **Access Denied**\nAdmin only command.",
                    parse_mode="Markdown",
                    reply_markup=ADMIN_REPLY_KEYBOARD
                )
            elif update.callback_query:
                await update.callback_query.message.reply_text(
                    "⛔ **Access Denied**\nAdmin only command.",
                    parse_mode="Markdown",
                    reply_markup=ADMIN_REPLY_KEYBOARD
                )
            return ConversationHandler.END

        return await func(update, context)
    return wrapper


# ==============================
# Admin Dashboard - Close to Selling
# ==============================
@admin_only
async def show_admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import is_selling_open, ADMIN_REPLY_KEYBOARD
    
    selling_status = "🟢 OPEN" if is_selling_open() else "🔴 CLOSED"
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🟢 Selling: {selling_status}", callback_data="toggle_selling")
        ]
    ])

    await update.message.reply_text(
        f"⚙️ **ADMIN DASHBOARD**\n\n"
        f"Bot Status: Online\n"
        f"Selling Status: {selling_status}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# ==============================
# Toggle Selling Status
# ==============================
@admin_only
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import set_selling_status, is_selling_open
    
    query = update.callback_query
    await query.answer()
    
    if query.data == "toggle_selling":
        current_status = is_selling_open()
        new_status = not current_status
        
        if set_selling_status(new_status):
            status_text = "🟢 OPEN" if new_status else "🔴 CLOSED"
            action_text = "opened" if new_status else "closed"
            
            # Update the button text
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"🟢 Selling: {status_text}", callback_data="toggle_selling")
                ]
            ])
            
            await query.edit_message_text(
                f"✅ **Selling {action_text} successfully!**\n\n"
                f"Current Status: {status_text}",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            await query.edit_message_text(
                "❌ Failed to update selling status. Please try again.",
                parse_mode="Markdown"
            )
    
    elif query.data == "admin_broadcast":
        from meowpremium import ADMIN_REPLY_KEYBOARD
        await query.message.reply_text(
            "👾 **BROADCAST**\n\n"
            "Click the '👾 Broadcast' button in your keyboard to start broadcasting.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
    
    elif query.data == "admin_stats":
        await handle_statistics(update, context)
    
    elif query.data == "admin_cash":
        await start_cash_control(update, context)
    
    elif query.data == "admin_refresh":
        await handle_refresh_config(update, context)


# ==============================
# Premium Broadcast System - FIXED
# ==============================
@admin_only
async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import WS_USER_DATA, ADMIN_REPLY_KEYBOARD
    
    if not WS_USER_DATA:
        await update.message.reply_text(
            "❌ User database not available. Cannot send broadcast.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
        return ConversationHandler.END
    
    try:
        # Get total user count
        all_users = WS_USER_DATA.get_all_records()
        total_users = len([u for u in all_users if u.get("banned", "FALSE").upper() != "TRUE"])
        
        await update.message.reply_text(
            f"👾 **PREMIUM BROADCAST SYSTEM**\n\n"
            f"📊 Total Active Users: {total_users}\n\n"
            f"📝 Please send the message you want to broadcast.\n"
            f"You can use Markdown formatting.\n\n"
            f"⚠️ **Warning:** This will be sent to ALL users.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel Broadcast"]], resize_keyboard=True)
        )
        return AWAIT_BROADCAST_MESSAGE
    except Exception as e:
        logger.error(f"Error preparing broadcast: {e}")
        await update.message.reply_text(
            "❌ Error preparing broadcast. Please try again.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
        return ConversationHandler.END


@admin_only
async def receive_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import ADMIN_REPLY_KEYBOARD
    
    if update.message.text == "❌ Cancel Broadcast":
        await update.message.reply_text(
            "❌ Broadcast cancelled.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
        return ConversationHandler.END
    
    context.user_data["broadcast_message"] = update.message.text_markdown if update.message.text_markdown else update.message.text
    
    # Show preview and confirmation
    preview_text = context.user_data["broadcast_message"][:500]
    if len(context.user_data["broadcast_message"]) > 500:
        preview_text += "..."
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Send Now", callback_data="confirm_broadcast"),
            InlineKeyboardButton("✏️ Edit Message", callback_data="edit_broadcast")
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")
        ]
    ])
    
    await update.message.reply_text(
        f"📋 **BROADCAST PREVIEW**\n\n"
        f"{preview_text}\n\n"
        f"📊 **Message Length:** {len(context.user_data['broadcast_message'])} characters\n\n"
        f"Are you sure you want to send this to ALL users?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return CONFIRM_BROADCAST


@admin_only
async def confirm_broadcast_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import ADMIN_REPLY_KEYBOARD, send_broadcast_to_all_users
    
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_broadcast":
        await query.edit_message_text(
            "❌ Broadcast cancelled.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    
    elif query.data == "edit_broadcast":
        await query.edit_message_text(
            "✏️ Please send the updated broadcast message:",
            parse_mode="Markdown"
        )
        return AWAIT_BROADCAST_MESSAGE
    
    elif query.data == "confirm_broadcast":
        # Show sending status
        await query.edit_message_text(
            "📤 **SENDING BROADCAST...**\n\n"
            "Please wait while messages are being sent to all users.\n"
            "This may take a few minutes depending on the number of users.",
            parse_mode="Markdown"
        )
        
        # Send broadcast
        successful, failed_users = await send_broadcast_to_all_users(
            context, 
            context.user_data["broadcast_message"]
        )
        
        # Prepare result message
        result_text = (
            f"✅ **BROADCAST COMPLETED**\n\n"
            f"📊 **Results:**\n"
            f"• ✅ Successful: {successful} users\n"
            f"• ❌ Failed: {len(failed_users)} users\n\n"
        )
        
        if failed_users:
            failed_list = ", ".join([str(uid) for uid in failed_users[:10]])
            if len(failed_users) > 10:
                failed_list += f" and {len(failed_users) - 10} more..."
            result_text += f"❌ **Failed Users:** {failed_list}"
        
        await query.edit_message_text(
            result_text,
            parse_mode="Markdown"
        )
        
        # Clear broadcast data
        if "broadcast_message" in context.user_data:
            del context.user_data["broadcast_message"]
        
        return ConversationHandler.END


# ==============================
# User Search - FIXED
# ==============================
@admin_only
async def handle_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import WS_USER_DATA, ADMIN_REPLY_KEYBOARD
    
    # Prompt user for search input
    await update.message.reply_text(
        "👤 **User Search**\n\n"
        "Please send User ID or @username to search for user information.\n\n"
        "Format:\n"
        "• User ID: `123456789`\n"
        "• Username: `@username`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel Search"]], resize_keyboard=True)
    )
    
    context.user_data["awaiting_user_search"] = True
    return "AWAIT_USER_SEARCH_INPUT"


@admin_only
async def handle_user_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import ADMIN_REPLY_KEYBOARD, get_user_data_from_sheet
    
    if update.message.text == "⬅️ Cancel Search":
        await update.message.reply_text(
            "❌ User search cancelled.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
        context.user_data.pop("awaiting_user_search", None)
        return ConversationHandler.END
    
    search_input = update.message.text.strip()
    
    try:
        user_data = None
        
        # Check if input is numeric (User ID)
        if search_input.isdigit():
            user_id = int(search_input)
            user_data = get_user_data_from_sheet(user_id)
        
        # Check if input is username (starts with @)
        elif search_input.startswith("@"):
            username = search_input[1:].lower()
            
            # Search through all users
            all_users = WS_USER_DATA.get_all_records()
            for user in all_users:
                user_username = str(user.get("username", "")).lower().replace("@", "")
                if user_username == username:
                    user_data = user
                    break
        
        if user_data and user_data.get("user_id") != "N/A":
            # Format user information
            user_id = user_data.get("user_id", "N/A")
            username = user_data.get("username", "N/A")
            first_name = user_data.get("first_name", "N/A")
            last_name = user_data.get("last_name", "N/A")
            coin_balance = user_data.get("coin_balance", 0)
            banned = user_data.get("banned", "FALSE").upper() == "TRUE"
            
            status_icon = "🔴" if banned else "🟢"
            status_text = "BANNED" if banned else "ACTIVE"
            
            user_info = (
                f"👤 **User Information**\n\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"👤 **Username:** {username}\n"
                f"📛 **First Name:** {first_name}\n"
                f"📛 **Last Name:** {last_name}\n"
                f"💰 **Coin Balance:** {coin_balance:,} Coins\n"
                f"📊 **Status:** {status_icon} {status_text}\n"
            )
            
            # Add additional info if available
            registration_date = user_data.get("registration_date", "")
            if registration_date:
                user_info += f"📅 **Registered:** {registration_date}\n"
            
            # Add action buttons
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("💰 Adjust Balance", callback_data=f"adjust_balance_{user_id}"),
                    InlineKeyboardButton("🚫 Ban/Unban", callback_data=f"toggle_ban_{user_id}")
                ],
                [
                    InlineKeyboardButton("📨 Message User", callback_data=f"message_user_{user_id}")
                ]
            ])
            
            await update.message.reply_text(
                user_info,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text(
                f"❌ User `{search_input}` not found in the database.",
                parse_mode="Markdown",
                reply_markup=ADMIN_REPLY_KEYBOARD
            )
    
    except Exception as e:
        logger.error(f"Error searching user: {e}")
        await update.message.reply_text(
            f"❌ Error searching user: {str(e)}",
            parse_mode="Markdown",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
    
    context.user_data.pop("awaiting_user_search", None)
    return ConversationHandler.END


# ==============================
# Refresh Config - FIXED
# ==============================
@admin_only
async def handle_refresh_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import get_config_data, ADMIN_REPLY_KEYBOARD
    
    try:
        get_config_data(force_refresh=True)
        
        await update.message.reply_text(
            "🔄 **Configuration Updated**\n\n"
            "Google Sheet data refreshed successfully.",
            parse_mode="Markdown",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
    except Exception as e:
        logger.error(f"Error refreshing config: {e}")
        await update.message.reply_text(
            "❌ Failed to refresh configuration. Please try again.",
            parse_mode="Markdown",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )


# ==============================
# Statistics - FIXED
# ==============================
@admin_only
async def handle_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import WS_USER_DATA, WS_ORDERS, get_config_data, is_selling_open, ADMIN_REPLY_KEYBOARD
    
    try:
        # Check if sheets are available
        if not WS_USER_DATA or not WS_ORDERS:
            await update.message.reply_text(
                "❌ Database not available. Cannot generate statistics.",
                parse_mode="Markdown",
                reply_markup=ADMIN_REPLY_KEYBOARD
            )
            return
        
        # Get user count
        all_users = []
        try:
            all_users = WS_USER_DATA.get_all_records()
        except Exception as e:
            logger.error(f"Error getting user records: {e}")
            all_users = []
            
        total_users = len(all_users)
        
        # Count active and banned users
        active_users = 0
        banned_users = 0
        for user in all_users:
            banned_status = str(user.get("banned", "FALSE")).upper()
            if banned_status == "TRUE":
                banned_users += 1
            else:
                active_users += 1
        
        # Get order statistics
        all_orders = []
        try:
            all_orders = WS_ORDERS.get_all_records()
        except Exception as e:
            logger.error(f"Error getting order records: {e}")
            all_orders = []
            
        total_orders = len(all_orders)
        
        # Count completed orders
        completed_orders = 0
        for order in all_orders:
            status = str(order.get("status", "")).upper()
            if status in ["APPROVED_RECEIPT", "ORDER_PLACED", "COMPLETED"]:
                completed_orders += 1
        
        # Calculate revenue
        total_revenue = 0
        for order in all_orders:
            try:
                price_str = str(order.get("price_mmk", "0")).replace(",", "").strip()
                if price_str and price_str.isdigit():
                    total_revenue += int(price_str)
            except:
                pass
        
        # Get selling status
        selling_status = "🟢 OPEN" if is_selling_open() else "🔴 CLOSED"
        
        # Format revenue with commas
        revenue_formatted = f"{total_revenue:,}"
        
        stats_text = (
            f"📊 **BOT STATISTICS**\n\n"
            f"🤖 **Bot Status:**\n"
            f"• Status: Online\n"
            f"• Selling: {selling_status}\n\n"
            f"👥 **Users:**\n"
            f"• Total Users: {total_users}\n"
            f"• Active Users: {active_users}\n"
            f"• Banned Users: {banned_users}\n\n"
            f"📦 **Orders:**\n"
            f"• Total Orders: {total_orders}\n"
            f"• Completed Orders: {completed_orders}\n\n"
            f"💰 **Revenue:**\n"
            f"• Total Revenue: {revenue_formatted} MMK\n"
        )
        
        if update.callback_query:
            await update.callback_query.message.reply_text(
                stats_text,
                parse_mode="Markdown",
                reply_markup=ADMIN_REPLY_KEYBOARD
            )
        else:
            await update.message.reply_text(
                stats_text,
                parse_mode="Markdown",
                reply_markup=ADMIN_REPLY_KEYBOARD
            )
        
    except Exception as e:
        logger.error(f"Error generating statistics: {e}")
        error_message = str(e)
        if len(error_message) > 100:
            error_message = error_message[:100] + "..."
        
        if update.callback_query:
            await update.callback_query.message.reply_text(
                f"❌ Error generating statistics: {error_message}",
                parse_mode="Markdown",
                reply_markup=ADMIN_REPLY_KEYBOARD
            )
        else:
            await update.message.reply_text(
                f"❌ Error generating statistics: {error_message}",
                parse_mode="Markdown",
                reply_markup=ADMIN_REPLY_KEYBOARD
            )


# ==============================
# Cash Control (Conversation) - FIXED
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


@admin_only
async def cash_control_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import ADMIN_REPLY_KEYBOARD
    
    text = update.message.text.strip()

    if text == "⬅️ Cancel":
        await update.message.reply_text(
            "❌ Cash Control cancelled.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["cash_user"] = text

    await update.message.reply_text(
        "💵 Enter amount to add / deduct:\n"
        "Example: `+5000` or `-2000`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel"]], resize_keyboard=True)
    )
    return AWAIT_CASH_CONTROL_AMOUNT


@admin_only
async def cash_control_apply_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import ADMIN_REPLY_KEYBOARD, get_user_data_from_sheet, update_user_balance

    amount_text = update.message.text.strip()
    target_user = context.user_data.get("cash_user")

    if amount_text == "⬅️ Cancel":
        await update.message.reply_text(
            "❌ Cash Control cancelled.",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Parse amount
    try:
        if amount_text.startswith("+"):
            amount = int(amount_text[1:])
        elif amount_text.startswith("-"):
            amount = -int(amount_text[1:])
        else:
            amount = int(amount_text)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount format. Use `+5000` or `-2000`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel"]], resize_keyboard=True)
        )
        return AWAIT_CASH_CONTROL_AMOUNT

    # Find user
    try:
        if target_user.isdigit():
            user_id = int(target_user)
            user_data = get_user_data_from_sheet(user_id)
            
            if user_data and user_data.get("user_id") != "N/A":
                current_balance = int(user_data.get("coin_balance", 0))
                new_balance = current_balance + amount
                
                if update_user_balance(user_id, new_balance):
                    # Notify user if coins were added
                    if amount > 0:
                        try:
                            from telegram.error import BadRequest
                            user_notification = (
                                f"🎉 **Coin Update Notification**\n\n"
                                f"**{amount:,.0f} Coins** have been manually added to your account by the Admin.\n\n"
                                f"Your new balance is **{new_balance:,.0f} Coins**."
                            )
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=user_notification,
                                parse_mode="Markdown"
                            )
                        except BadRequest:
                            logger.warning(f"Could not notify user {user_id} - user may have blocked the bot")
                        except Exception as e:
                            logger.error(f"Error notifying user: {e}")
                    
                    await update.message.reply_text(
                        f"✅ **Cash Updated Successfully**\n\n"
                        f"👤 User: `{user_data.get('username', 'N/A')}`\n"
                        f"🆔 ID: `{user_id}`\n"
                        f"💰 Change: `{amount:+d}` Coins\n"
                        f"💎 Old Balance: `{current_balance:,}` Coins\n"
                        f"💎 New Balance: `{new_balance:,}` Coins",
                        parse_mode="Markdown",
                        reply_markup=ADMIN_REPLY_KEYBOARD
                    )
                else:
                    await update.message.reply_text(
                        "❌ Failed to update user balance. Please try again.",
                        parse_mode="Markdown",
                        reply_markup=ADMIN_REPLY_KEYBOARD
                    )
            else:
                await update.message.reply_text(
                    f"❌ User ID `{user_id}` not found.",
                    parse_mode="Markdown",
                    reply_markup=ADMIN_REPLY_KEYBOARD
                )
        else:
            # Try to find by username
            await update.message.reply_text(
                "❌ Currently only User ID is supported. Please enter numeric User ID.",
                parse_mode="Markdown",
                reply_markup=ADMIN_REPLY_KEYBOARD
            )
            
    except Exception as e:
        logger.error(f"Error updating cash balance: {e}")
        await update.message.reply_text(
            f"❌ Error: {str(e)[:100]}",
            parse_mode="Markdown",
            reply_markup=ADMIN_REPLY_KEYBOARD
        )

    context.user_data.clear()
    # ==============================
# Cash Control Cancel Function
# ==============================
@admin_only
async def cash_control_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meowpremium import ADMIN_REPLY_KEYBOARD

    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cash Control cancelled.",
        reply_markup=ADMIN_REPLY_KEYBOARD
    )
    return ConversationHandler.END
