import logging
import datetime
import re
import uuid
import csv
import io
from typing import Dict, List, Optional, Tuple
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

logger = logging.getLogger(__name__)

# Conversation states
AWAIT_CASH_CONTROL_ID, AWAIT_CASH_CONTROL_AMOUNT = range(30, 32)
AWAIT_BROADCAST_CONFIRM, AWAIT_BROADCAST_MESSAGE = range(32, 34)
AWAIT_USER_SEARCH = 34
AWAIT_ORDER_STATUS_UPDATE = 35
AWAIT_CONFIG_EDIT = 36
AWAIT_DATA_EXPORT_TYPE = 37

class AdminCommands:
    def __init__(self, ws_user_data, ws_config, ws_orders, ws_admin_logs, 
                 get_config_data, get_dynamic_admin_id, is_multi_admin,
                 log_admin_action, get_all_users, get_pending_orders,
                 update_order_status, update_config_value, set_bot_status,
                 get_bot_status):
        self.ws_user_data = ws_user_data
        self.ws_config = ws_config
        self.ws_orders = ws_orders
        self.ws_admin_logs = ws_admin_logs
        self.get_config_data = get_config_data
        self.get_dynamic_admin_id = get_dynamic_admin_id
        self.is_multi_admin = is_multi_admin
        self.log_admin_action = log_admin_action
        self.get_all_users = get_all_users
        self.get_pending_orders = get_pending_orders
        self.update_order_status = update_order_status
        self.update_config_value = update_config_value
        self.set_bot_status = set_bot_status
        self.get_bot_status = get_bot_status
    
    def register_handlers(self, application):
        """Register all admin command handlers"""
        
        # Broadcast Conversation Handler
        broadcast_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Text("👾 Broadcast"), self.start_broadcast)],
            states={
                AWAIT_BROADCAST_MESSAGE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Text("⬅️ Cancel"), self.receive_broadcast_message)
                ],
                AWAIT_BROADCAST_CONFIRM: [
                    CallbackQueryHandler(self.confirm_broadcast, pattern=r"^broadcast_confirm$"),
                    CallbackQueryHandler(self.cancel_broadcast, pattern=r"^broadcast_cancel$")
                ]
            },
            fallbacks=[MessageHandler(filters.Text("⬅️ Cancel"), self.cancel_broadcast_action)],
            allow_reentry=True
        )
        application.add_handler(broadcast_handler)
        
        # Bot Status Handler
        application.add_handler(MessageHandler(filters.Text("⚙️ Bot Status"), self.handle_bot_status))
        
        # Cash Control Conversation Handler
        cash_control_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Text("📝 Cash Control"), self.start_cash_control)],
            states={
                AWAIT_CASH_CONTROL_ID: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Text("⬅️ Cancel"), self.cash_control_get_id)
                ],
                AWAIT_CASH_CONTROL_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Text("⬅️ Cancel"), self.cash_control_apply_amount)
                ]
            },
            fallbacks=[MessageHandler(filters.Text("⬅️ Cancel"), self.cash_control_cancel)],
            allow_reentry=True
        )
        application.add_handler(cash_control_handler)
        
        # User Search Handler
        user_search_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Text("👤 User Search"), self.start_user_search)],
            states={
                AWAIT_USER_SEARCH: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Text("⬅️ Cancel"), self.process_user_search)
                ]
            },
            fallbacks=[MessageHandler(filters.Text("⬅️ Cancel"), self.cancel_user_search)],
            allow_reentry=True
        )
        application.add_handler(user_search_handler)
        
        # Order Management Handler
        application.add_handler(MessageHandler(filters.Text("📦 Order Management"), self.handle_order_management))
        
        # Statistics Handler
        application.add_handler(MessageHandler(filters.Text("📊 Statistics"), self.handle_statistics))
        
        # Configuration Handler
        application.add_handler(MessageHandler(filters.Text("⚙️ Configuration"), self.handle_configuration))
        
        # System Health Handler
        application.add_handler(MessageHandler(filters.Text("📈 System Health"), self.handle_system_health))
        
        # Data Export Handler
        data_export_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Text("📤 Data Export"), self.start_data_export)],
            states={
                AWAIT_DATA_EXPORT_TYPE: [
                    CallbackQueryHandler(self.process_data_export, pattern=r"^export_")
                ]
            },
            fallbacks=[MessageHandler(filters.Text("⬅️ Cancel"), self.cancel_data_export)],
            allow_reentry=True
        )
        application.add_handler(data_export_handler)
        
        # Notifications Handler
        application.add_handler(MessageHandler(filters.Text("🔔 Notifications"), self.handle_notifications))
        
        # Order status update callbacks
        application.add_handler(CallbackQueryHandler(self.update_order_status_callback, pattern=r"^order_update_"))
        
        # Config edit callbacks
        application.add_handler(CallbackQueryHandler(self.edit_config_callback, pattern=r"^config_edit_"))
    
    # =============== BROADCAST FEATURE ===============
    async def start_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized to use Broadcast.")
            return ConversationHandler.END
        
        await update.message.reply_text(
            "📢 **BROADCAST MESSAGE**\n\n"
            "Please enter the message you want to broadcast to all users.\n"
            "You can use Markdown formatting.\n\n"
            "Type '⬅️ Cancel' to cancel.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel"]], resize_keyboard=True)
        )
        
        return AWAIT_BROADCAST_MESSAGE
    
    async def receive_broadcast_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        message_text = update.message.text
        context.user_data['broadcast_message'] = message_text
        
        # Get user count
        users = self.get_all_users()
        user_count = len(users)
        
        # Create confirmation keyboard
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Broadcast", callback_data="broadcast_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel")]
        ])
        
        await update.message.reply_text(
            f"📢 **Broadcast Preview**\n\n"
            f"**Message:**\n{message_text}\n\n"
            f"**Recipients:** {user_count} users\n\n"
            f"Are you sure you want to send this broadcast?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
        return AWAIT_BROADCAST_CONFIRM
    
    async def confirm_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        message_text = context.user_data.get('broadcast_message', '')
        
        if not message_text:
            await query.message.edit_text("❌ No message found to broadcast.")
            return ConversationHandler.END
        
        # Get all users
        users = self.get_all_users()
        total_users = len(users)
        successful = 0
        failed = 0
        
        # Send initial status
        status_msg = await query.message.reply_text(f"📤 Broadcasting to {total_users} users...\n✅ Successful: 0\n❌ Failed: 0")
        
        # Send to each user
        for user_data in users:
            try:
                user_id = int(user_data['user_id'])
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 **ANNOUNCEMENT**\n\n{message_text}\n\n— Admin Team",
                    parse_mode="Markdown"
                )
                successful += 1
                
                # Update status every 10 sends
                if successful % 10 == 0:
                    await status_msg.edit_text(
                        f"📤 Broadcasting to {total_users} users...\n"
                        f"✅ Successful: {successful}\n"
                        f"❌ Failed: {failed}\n"
                        f"📊 Progress: {((successful + failed) / total_users * 100):.1f}%"
                    )
                    
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.1)
                
            except Exception as e:
                failed += 1
                logger.error(f"Failed to send broadcast to {user_data['user_id']}: {e}")
        
        # Final status
        await status_msg.edit_text(
            f"✅ **Broadcast Completed!**\n\n"
            f"📊 **Statistics:**\n"
            f"• Total Users: {total_users}\n"
            f"• ✅ Successful: {successful}\n"
            f"• ❌ Failed: {failed}\n"
            f"• 📈 Success Rate: {(successful/total_users*100):.1f}%"
        )
        
        # Log admin action
        self.log_admin_action(
            admin_id=user.id,
            admin_username=user.username or str(user.id),
            action="BROADCAST",
            details=f"Message: {message_text[:100]}... | Sent: {successful}/{total_users}"
        )
        
        # Clear context
        if 'broadcast_message' in context.user_data:
            del context.user_data['broadcast_message']
        
        return ConversationHandler.END
    
    async def cancel_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        
        await query.message.edit_text("❌ Broadcast cancelled.")
        
        if 'broadcast_message' in context.user_data:
            del context.user_data['broadcast_message']
        
        return ConversationHandler.END
    
    async def cancel_broadcast_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(
            "❌ Broadcast cancelled.",
            reply_markup=self.get_admin_keyboard()
        )
        
        if 'broadcast_message' in context.user_data:
            del context.user_data['broadcast_message']
        
        return ConversationHandler.END
    
    # =============== BOT STATUS FEATURE ===============
    async def handle_bot_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized.")
            return
        
        current_status = self.get_bot_status()
        status_text = "🟢 ACTIVE" if current_status else "🔴 INACTIVE"
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢 Activate Bot", callback_data="bot_activate"),
                InlineKeyboardButton("🔴 Deactivate Bot", callback_data="bot_deactivate")
            ],
            [InlineKeyboardButton("🔄 Check Status", callback_data="bot_check")]
        ])
        
        await update.message.reply_text(
            f"🤖 **BOT STATUS CONTROL**\n\n"
            f"Current Status: {status_text}\n\n"
            f"Choose an action:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    
    async def bot_status_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return
        
        action = query.data
        
        if action == "bot_activate":
            self.set_bot_status(True)
            status = "🟢 ACTIVATED"
            action_text = "activated"
        elif action == "bot_deactivate":
            self.set_bot_status(False)
            status = "🔴 DEACTIVATED"
            action_text = "deactivated"
        else:  # bot_check
            current_status = self.get_bot_status()
            status = "🟢 ACTIVE" if current_status else "🔴 INACTIVE"
            await query.message.edit_text(f"✅ Bot Status: {status}")
            return
        
        # Log admin action
        self.log_admin_action(
            admin_id=user.id,
            admin_username=user.username or str(user.id),
            action=f"BOT_{action_text.upper()}",
            details=f"Bot {action_text}"
        )
        
        await query.message.edit_text(f"✅ Bot {action_text}!\n\nCurrent Status: {status}")
    
    # =============== CASH CONTROL FEATURE ===============
    async def start_cash_control(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized to use Cash Control.", reply_markup=self.get_admin_keyboard())
            return ConversationHandler.END
        
        await update.message.reply_text(
            "📝 **CASH CONTROL**\n\n"
            "Please enter the **User ID (number)** or **Username (@...)** of the user whose balance you want to modify.\n\n"
            "Type '⬅️ Cancel' to cancel.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel"]], resize_keyboard=True)
        )
        
        return AWAIT_CASH_CONTROL_ID
    
    def find_user_row(self, user_id: int) -> Optional[int]:
        try:
            cell = self.ws_user_data.find(str(user_id), in_column=1)
            if cell:
                return cell.row
        except Exception as e:
            logger.debug("find_user_row exception: %s", e)
        return None
    
    def get_user_data_from_sheet(self, user_id: int) -> Dict[str, str]:
        row = self.find_user_row(user_id)
        if not row:
            return {"user_id": str(user_id), "username": "N/A", "coin_balance": "0"}
        
        try:
            row_values = self.ws_user_data.row_values(row)
            return {
                "user_id": row_values[0] if len(row_values) > 0 else str(user_id),
                "username": row_values[1] if len(row_values) > 1 else "N/A",
                "coin_balance": row_values[2].strip() if len(row_values) > 2 else "0",
            }
        except Exception as e:
            logger.error("Error get_user_data_from_sheet: %s", e)
            return {"user_id": str(user_id), "username": "N/A", "coin_balance": "0"}
    
    async def cash_control_get_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        input_identifier = update.message.text.strip()
        user_id_int = None
        target_username = None
        
        if input_identifier.isdigit():
            user_id_int = int(input_identifier)
            if self.find_user_row(user_id_int):
                user_data = self.get_user_data_from_sheet(user_id_int)
                target_username = user_data.get("username", f"ID:{user_id_int}")
        
        elif input_identifier.startswith('@'):
            target_username = input_identifier
            try:
                cell = self.ws_user_data.find(target_username, in_column=2)
                if cell:
                    user_id_int = int(self.ws_user_data.cell(cell.row, 1).value)
            except Exception:
                pass
        
        else:
            target_username = "@" + input_identifier
            try:
                cell = self.ws_user_data.find(target_username, in_column=2)
                if cell:
                    user_id_int = int(self.ws_user_data.cell(cell.row, 1).value)
            except Exception:
                pass
        
        if not user_id_int or not self.find_user_row(user_id_int):
            await update.message.reply_text("❌ User not found or ID/Username is invalid. Please try again or type '⬅️ Cancel'.")
            return AWAIT_CASH_CONTROL_ID
        
        context.user_data['target_cash_control_id'] = user_id_int
        context.user_data['target_cash_control_name'] = target_username
        
        await update.message.reply_text(
            f"📝 **Target User Found**: {target_username} (ID `{user_id_int}`)\n\n"
            "Please enter the Coin amount to add or subtract.\n"
            "Use **+** for adding (e.g., `+5000`)\n"
            "Use **-** for subtracting (e.g., `-100`)\n\n"
            "Type '⬅️ Cancel' to cancel.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel"]], resize_keyboard=True)
        )
        
        return AWAIT_CASH_CONTROL_AMOUNT
    
    async def cash_control_apply_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        amount_text = update.message.text.strip()
        target_user_id = context.user_data.get('target_cash_control_id')
        target_user_name = context.user_data.get('target_cash_control_name', f"ID:{target_user_id}")
        admin_user = update.effective_user
        
        if not target_user_id:
            await update.message.reply_text("❌ Error: Target user ID lost. Please restart Cash Control.", reply_markup=self.get_admin_keyboard())
            return ConversationHandler.END
        
        match = re.match(r"([+\-]\d+)", amount_text)
        if not match:
            await update.message.reply_text("❌ Invalid format. Please use '+[number]' or '-[number]' (e.g., `+5000` or `-100`).")
            return AWAIT_CASH_CONTROL_AMOUNT
        
        try:
            coin_change = int(match.group(1))
        except ValueError:
            await update.message.reply_text("❌ The number provided is too large or not a valid integer.")
            return AWAIT_CASH_CONTROL_AMOUNT
        
        user_row = self.find_user_row(target_user_id)
        
        if user_row:
            try:
                old_balance = int(self.ws_user_data.cell(user_row, 3).value or 0)
            except ValueError:
                old_balance = 0
                
            new_balance = old_balance + coin_change
            
            self.ws_user_data.update_cell(user_row, 3, new_balance)
            
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
                f"{action_emoji} **Action:** {action_text} **{abs(coin_change):,.0f} Coins**\n"
                f"**User:** {target_user_name} (ID `{target_user_id}`)\n"
                f"**Old Balance:** {old_balance:,.0f} Coins\n"
                f"**New Balance:** {new_balance:,.0f} Coins\n"
                f"**Processed by:** {admin_processed_by}"
            )
            
            await update.message.reply_text(admin_success_msg, parse_mode="Markdown", reply_markup=self.get_admin_keyboard())
            
            # Log admin action
            self.log_admin_action(
                admin_id=admin_user.id,
                admin_username=admin_user.username or str(admin_user.id),
                action="CASH_CONTROL",
                target_user=str(target_user_id),
                details=f"Change: {coin_change} coins | Old: {old_balance} | New: {new_balance}"
            )
            
            # Notify User (Only if coins were added)
            if coin_change > 0:
                user_notification = (
                    f"🎉 **Coin Update Notification**\n\n"
                    f"**{coin_change:,.0f} Coins** have been manually added to your account by the Admin.\n\n"
                    f"Your new balance is **{new_balance:,.0f} Coins**."
                )
                try:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=user_notification,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Warning: Could not send notification to user ID {target_user_id}. Error: {e}", reply_markup=self.get_admin_keyboard())
        
        else:
            await update.message.reply_text("❌ Error: Target user row could not be located in the sheet during final update.", reply_markup=self.get_admin_keyboard())
        
        # Clean up context data
        if 'target_cash_control_id' in context.user_data:
            del context.user_data['target_cash_control_id']
        if 'target_cash_control_name' in context.user_data:
            del context.user_data['target_cash_control_name']
            
        return ConversationHandler.END
    
    async def cash_control_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(
            "📝 Cash Control cancelled.",
            reply_markup=self.get_admin_keyboard()
        )
        return ConversationHandler.END
    
    # =============== USER SEARCH FEATURE ===============
    async def start_user_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized to use User Search.")
            return ConversationHandler.END
        
        await update.message.reply_text(
            "🔍 **USER SEARCH**\n\n"
            "Enter User ID, Username, or Phone Number to search:\n\n"
            "Type '⬅️ Cancel' to cancel.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel"]], resize_keyboard=True)
        )
        
        return AWAIT_USER_SEARCH
    
    async def process_user_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        search_term = update.message.text.strip()
        
        try:
            # Search in user_data sheet
            users_data = self.ws_user_data.get_all_records()
            found_users = []
            
            for user in users_data:
                user_id_str = str(user.get('user_id', ''))
                username = user.get('username', '')
                phone = user.get('phone', '')
                
                if (search_term in user_id_str or 
                    search_term.lower() in username.lower() or 
                    search_term in phone):
                    found_users.append(user)
            
            if not found_users:
                await update.message.reply_text(
                    "❌ No users found matching your search.",
                    reply_markup=self.get_admin_keyboard()
                )
                return ConversationHandler.END
            
            # Display results
            if len(found_users) == 1:
                user = found_users[0]
                user_info = self._format_user_details(user)
                
                # Add action buttons
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("💰 Add Coins", callback_data=f"user_add_{user['user_id']}"),
                        InlineKeyboardButton("🔨 Ban/Unban", callback_data=f"user_ban_{user['user_id']}")
                    ],
                    [
                        InlineKeyboardButton("📋 Orders", callback_data=f"user_orders_{user['user_id']}"),
                        InlineKeyboardButton("📝 Edit", callback_data=f"user_edit_{user['user_id']}")
                    ]
                ])
                
                await update.message.reply_text(
                    user_info,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                
            else:
                results_text = f"🔍 Found {len(found_users)} users:\n\n"
                for i, user in enumerate(found_users[:10], 1):
                    results_text += f"{i}. {user.get('username', 'N/A')} (ID: `{user.get('user_id', 'N/A')}`) - {user.get('coin_balance', '0')} coins\n"
                
                if len(found_users) > 10:
                    results_text += f"\n... and {len(found_users) - 10} more users."
                
                await update.message.reply_text(
                    results_text,
                    parse_mode="Markdown",
                    reply_markup=self.get_admin_keyboard()
                )
                
        except Exception as e:
            logger.error(f"Error in user search: {e}")
            await update.message.reply_text(
                "❌ Error searching for users.",
                reply_markup=self.get_admin_keyboard()
            )
        
        return ConversationHandler.END
    
    def _format_user_details(self, user: Dict) -> str:
        banned_status = "✅ Active" if user.get('banned', 'FALSE').upper() == 'FALSE' else "❌ Banned"
        
        user_info = (
            f"👤 **User Details**\n\n"
            f"🆔 **ID:** `{user.get('user_id', 'N/A')}`\n"
            f"👤 **Username:** {user.get('username', 'N/A')}\n"
            f"💰 **Coin Balance:** {user.get('coin_balance', '0')}\n"
            f"📅 **Registered:** {user.get('registration_date', 'N/A')}\n"
            f"🕒 **Last Active:** {user.get('last_active', 'N/A')}\n"
            f"💵 **Total Purchase:** {user.get('total_purchase', '0')} MMK\n"
            f"🚫 **Status:** {banned_status}\n"
        )
        
        return user_info
    
    async def cancel_user_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(
            "🔍 User Search cancelled.",
            reply_markup=self.get_admin_keyboard()
        )
        return ConversationHandler.END
    
    # =============== ORDER MANAGEMENT FEATURE ===============
    async def handle_order_management(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized.")
            return
        
        # Get pending orders
        pending_orders = self.get_pending_orders()
        
        if not pending_orders:
            await update.message.reply_text(
                "📦 **ORDER MANAGEMENT**\n\n"
                "✅ No pending orders at the moment.",
                parse_mode="Markdown"
            )
            return
        
        orders_text = f"📦 **Pending Orders:** {len(pending_orders)}\n\n"
        
        for i, order in enumerate(pending_orders[:5], 1):
            orders_text += (
                f"{i}. **Order ID:** `{order.get('order_id', 'N/A')}`\n"
                f"   👤 User: {order.get('username', 'N/A')} (ID: `{order.get('user_id', 'N/A')}`)\n"
                f"   📦 Product: {order.get('product_key', 'N/A')}\n"
                f"   💰 Amount: {order.get('price_mmk', '0')} MMK\n"
                f"   📱 Phone: {order.get('phone', 'N/A')}\n"
                f"   📅 Date: {order.get('timestamp', 'N/A')}\n\n"
            )
        
        if len(pending_orders) > 5:
            orders_text += f"... and {len(pending_orders) - 5} more orders.\n\n"
        
        # Create action keyboard
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="orders_refresh"),
                InlineKeyboardButton("📋 View All", callback_data="orders_view_all")
            ],
            [
                InlineKeyboardButton("✅ Process All", callback_data="orders_process_all"),
                InlineKeyboardButton("📊 Statistics", callback_data="orders_stats")
            ]
        ])
        
        await update.message.reply_text(
            orders_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    
    async def update_order_status_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return
        
        data = query.data
        parts = data.split('_')
        
        if len(parts) < 4:
            await query.message.reply_text("Invalid action.")
            return
        
        action = parts[2]
        order_id = parts[3]
        
        if action == "complete":
            new_status = "COMPLETED"
        elif action == "cancel":
            new_status = "CANCELLED"
        elif action == "process":
            new_status = "PROCESSING"
        else:
            await query.message.reply_text("Invalid action.")
            return
        
        success = self.update_order_status(
            order_id=order_id,
            status=new_status,
            processed_by=str(user.id),
            notes=f"Updated by admin {user.id}"
        )
        
        if success:
            # Log admin action
            self.log_admin_action(
                admin_id=user.id,
                admin_username=user.username or str(user.id),
                action="UPDATE_ORDER",
                details=f"Order {order_id} → {new_status}"
            )
            
            await query.message.edit_text(f"✅ Order `{order_id}` updated to **{new_status}**.")
        else:
            await query.message.edit_text(f"❌ Failed to update order `{order_id}`.")
    
    # =============== STATISTICS FEATURE ===============
    async def handle_statistics(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized.")
            return
        
        try:
            # Get all users
            users = self.get_all_users()
            total_users = len(users)
            
            # Get active users (last 30 days)
            active_users = 0
            thirty_days_ago = datetime.datetime.now() - datetime.timedelta(days=30)
            
            for user_data in users:
                last_active_str = user_data.get('last_active', '')
                if last_active_str:
                    try:
                        last_active = datetime.datetime.strptime(last_active_str, "%Y-%m-%d %H:%M:%S")
                        if last_active > thirty_days_ago:
                            active_users += 1
                    except:
                        pass
            
            # Get banned users
            banned_users = sum(1 for u in users if u.get('banned', 'FALSE').upper() == 'TRUE')
            
            # Get orders statistics
            orders_data = self.ws_orders.get_all_records()
            total_orders = len(orders_data)
            
            # Calculate revenue
            total_revenue = sum(int(order.get('price_mmk', 0)) for order in orders_data)
            
            # Get today's stats
            today = datetime.datetime.now().date()
            today_orders = 0
            today_revenue = 0
            
            for order in orders_data:
                order_date_str = order.get('timestamp', '')
                if order_date_str:
                    try:
                        order_date = datetime.datetime.strptime(order_date_str, "%Y-%m-%d %H:%M:%S").date()
                        if order_date == today:
                            today_orders += 1
                            today_revenue += int(order.get('price_mmk', 0))
                    except:
                        pass
            
            # Get product stats
            product_stats = {}
            for order in orders_data:
                product = order.get('product_key', '')
                if product:
                    product_stats[product] = product_stats.get(product, 0) + 1
            
            top_products = sorted(product_stats.items(), key=lambda x: x[1], reverse=True)[:5]
            
            # Format statistics
            stats_text = (
                f"📊 **BOT STATISTICS**\n\n"
                f"👥 **Users:**\n"
                f"• Total Users: {total_users}\n"
                f"• Active (30 days): {active_users}\n"
                f"• Banned Users: {banned_users}\n"
                f"• Active Rate: {(active_users/total_users*100 if total_users > 0 else 0):.1f}%\n\n"
                
                f"💰 **Financial:**\n"
                f"• Total Revenue: {total_revenue:,} MMK\n"
                f"• Total Orders: {total_orders}\n"
                f"• Today's Revenue: {today_revenue:,} MMK\n"
                f"• Today's Orders: {today_orders}\n\n"
                
                f"🏆 **Top Products:**\n"
            )
            
            for product, count in top_products:
                stats_text += f"• {product}: {count} orders\n"
            
            if not top_products:
                stats_text += "• No product data available\n"
            
            # Add refresh button
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="stats_refresh")],
                [InlineKeyboardButton("📈 Detailed Report", callback_data="stats_detailed")]
            ])
            
            await update.message.reply_text(
                stats_text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Error generating statistics: {e}")
            await update.message.reply_text("❌ Error generating statistics.")
    
    # =============== CONFIGURATION FEATURE ===============
    async def handle_configuration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized.")
            return
        
        config = self.get_config_data(force_refresh=True)
        
        # Create categorized config display
        categories = {
            "💰 Payment": [k for k in config.keys() if any(x in k for x in ['kpay', 'wave', 'cbpay'])],
            "⭐ Products": [k for k in config.keys() if any(x in k for x in ['star_', 'premium_', 'coin_rate'])],
            "📦 Packages": [k for k in config.keys() if 'coinpkg_' in k],
            "⚙️ System": [k for k in config.keys() if any(x in k for x in ['admin', 'ratio', 'receipt', 'bot_status'])],
            "🔧 Other": [k for k in config.keys() if k not in sum(categories.values(), [])]
        }
        
        config_text = "⚙️ **BOT CONFIGURATION**\n\n"
        
        for category, keys in categories.items():
            if keys:
                config_text += f"**{category}:**\n"
                for key in sorted(keys)[:3]:  # Show only first 3 per category
                    value = config.get(key, '')
                    config_text += f"• `{key}`: `{value[:30]}{'...' if len(value) > 30 else ''}`\n"
                if len(keys) > 3:
                    config_text += f"• ... and {len(keys) - 3} more\n"
                config_text += "\n"
        
        config_text += f"**Total Config Items:** {len(config)}\n"
        
        # Create action keyboard
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔍 Search Config", callback_data="config_search"),
                InlineKeyboardButton("✏️ Edit Config", callback_data="config_edit")
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="config_refresh"),
                InlineKeyboardButton("📥 Backup", callback_data="config_backup")
            ]
        ])
        
        await update.message.reply_text(
            config_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    
    async def edit_config_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return
        
        data = query.data
        parts = data.split('_')
        
        if len(parts) < 3:
            await query.message.reply_text("Invalid action.")
            return
        
        action = parts[1]
        
        if action == "search":
            await query.message.reply_text(
                "Enter config key to search:",
                reply_markup=ReplyKeyboardMarkup([["⬅️ Cancel"]], resize_keyboard=True)
            )
            return AWAIT_CONFIG_EDIT
        
        elif action == "edit":
            config = self.get_config_data()
            
            # Create keyboard with config keys
            keyboard_buttons = []
            row = []
            keys = sorted(config.keys())
            
            for i, key in enumerate(keys):
                row.append(InlineKeyboardButton(key[:15], callback_data=f"config_select_{key}"))
                if len(row) == 2:
                    keyboard_buttons.append(row)
                    row = []
            
            if row:
                keyboard_buttons.append(row)
            
            keyboard_buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="config_back")])
            
            await query.message.edit_text(
                "Select config key to edit:",
                reply_markup=InlineKeyboardMarkup(keyboard_buttons)
            )
    
    # =============== SYSTEM HEALTH FEATURE ===============
    async def handle_system_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized.")
            return
        
        try:
            # Check Google Sheets connection
            sheets_status = "✅ Connected" if self.ws_user_data else "❌ Disconnected"
            
            # Check bot status
            bot_status = "🟢 Active" if self.get_bot_status() else "🔴 Inactive"
            
            # Get user count
            users = self.get_all_users()
            user_count = len(users)
            
            # Get pending orders
            pending_orders = len(self.get_pending_orders())
            
            # Get recent errors from admin logs
            recent_errors = 0
            try:
                logs = self.ws_admin_logs.get_all_records()
                twenty_four_hours_ago = datetime.datetime.now() - datetime.timedelta(hours=24)
                
                for log in logs:
                    timestamp_str = log.get('timestamp', '')
                    if timestamp_str:
                        try:
                            log_time = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                            if log_time > twenty_four_hours_ago and "ERROR" in log.get('action', ''):
                                recent_errors += 1
                        except:
                            pass
            except:
                recent_errors = "N/A"
            
            # Format health report
            health_text = (
                f"📈 **SYSTEM HEALTH REPORT**\n\n"
                f"🤖 **Bot Status:** {bot_status}\n"
                f"📊 **Google Sheets:** {sheets_status}\n\n"
                
                f"📊 **Statistics:**\n"
                f"• Total Users: {user_count}\n"
                f"• Pending Orders: {pending_orders}\n"
                f"• Recent Errors (24h): {recent_errors}\n\n"
                
                f"🔄 **Last Refresh:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            
            # Add health indicators
            health_score = 100
            issues = []
            
            if not self.ws_user_data:
                health_score -= 30
                issues.append("Google Sheets disconnected")
            
            if pending_orders > 20:
                health_score -= 10
                issues.append("High pending orders")
            
            if recent_errors > 10:
                health_score -= 20
                issues.append("Multiple recent errors")
            
            if health_score > 80:
                health_emoji = "🟢"
                health_status = "Excellent"
            elif health_score > 60:
                health_emoji = "🟡"
                health_status = "Good"
            elif health_score > 40:
                health_emoji = "🟠"
                health_status = "Fair"
            else:
                health_emoji = "🔴"
                health_status = "Poor"
            
            health_text += f"\n{health_emoji} **Health Score:** {health_score}/100 ({health_status})\n"
            
            if issues:
                health_text += "\n⚠️ **Issues:**\n"
                for issue in issues:
                    health_text += f"• {issue}\n"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="health_refresh")],
                [InlineKeyboardButton("📋 Detailed Logs", callback_data="health_logs")]
            ])
            
            await update.message.reply_text(
                health_text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Error checking system health: {e}")
            await update.message.reply_text("❌ Error checking system health.")
    
    # =============== DATA EXPORT FEATURE ===============
    async def start_data_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized to use Data Export.")
            return ConversationHandler.END
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Export Users (CSV)", callback_data="export_users")],
            [InlineKeyboardButton("📦 Export Orders (CSV)", callback_data="export_orders")],
            [InlineKeyboardButton("📊 Export Statistics (CSV)", callback_data="export_stats")],
            [InlineKeyboardButton("📝 Export Admin Logs (CSV)", callback_data="export_logs")],
            [InlineKeyboardButton("⬅️ Cancel", callback_data="export_cancel")]
        ])
        
        await update.message.reply_text(
            "📤 **DATA EXPORT**\n\n"
            "Select data to export:",
            reply_markup=keyboard
        )
        
        return AWAIT_DATA_EXPORT_TYPE
    
    async def process_data_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return ConversationHandler.END
        
        export_type = query.data.replace("export_", "")
        
        if export_type == "cancel":
            await query.message.edit_text("❌ Data export cancelled.")
            return ConversationHandler.END
        
        try:
            if export_type == "users":
                data = self.ws_user_data.get_all_records()
                filename = f"users_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                fieldnames = ['user_id', 'username', 'coin_balance', 'registration_date', 'last_active', 'total_purchase', 'banned']
                
            elif export_type == "orders":
                data = self.ws_orders.get_all_records()
                filename = f"orders_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                fieldnames = ['order_id', 'user_id', 'username', 'product_key', 'price_mmk', 'phone', 'premium_username', 'status', 'timestamp', 'notes', 'processed_by']
                
            elif export_type == "logs":
                data = self.ws_admin_logs.get_all_records()
                filename = f"logs_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                fieldnames = ['timestamp', 'admin_id', 'admin_username', 'action', 'target_user', 'details', 'ip_address', 'user_agent']
                
            elif export_type == "stats":
                # Create statistics data
                users = self.get_all_users()
                orders = self.ws_orders.get_all_records()
                
                stats_data = [{
                    'metric': 'Total Users',
                    'value': len(users)
                }, {
                    'metric': 'Total Orders',
                    'value': len(orders)
                }, {
                    'metric': 'Total Revenue',
                    'value': sum(int(o.get('price_mmk', 0)) for o in orders)
                }, {
                    'metric': 'Active Users (30 days)',
                    'value': sum(1 for u in users if self._is_recent(u.get('last_active', ''), 30))
                }]
                
                data = stats_data
                filename = f"stats_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                fieldnames = ['metric', 'value']
            
            else:
                await query.message.edit_text("❌ Invalid export type.")
                return ConversationHandler.END
            
            # Create CSV in memory
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
            
            # Send file
            await context.bot.send_document(
                chat_id=user.id,
                document=io.BytesIO(output.getvalue().encode()),
                filename=filename,
                caption=f"✅ {export_type.title()} export completed.\n\n📅 Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            # Log admin action
            self.log_admin_action(
                admin_id=user.id,
                admin_username=user.username or str(user.id),
                action="DATA_EXPORT",
                details=f"Type: {export_type}"
            )
            
            await query.message.edit_text(f"✅ {export_type.title()} exported successfully!")
            
        except Exception as e:
            logger.error(f"Error exporting data: {e}")
            await query.message.edit_text(f"❌ Error exporting {export_type}: {str(e)}")
        
        return ConversationHandler.END
    
    def _is_recent(self, date_str: str, days: int) -> bool:
        """Check if a date string is within the last N days"""
        if not date_str:
            return False
        
        try:
            date = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            days_ago = datetime.datetime.now() - datetime.timedelta(days=days)
            return date > days_ago
        except:
            return False
    
    async def cancel_data_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(
            "❌ Data export cancelled.",
            reply_markup=self.get_admin_keyboard()
        )
        return ConversationHandler.END
    
    # =============== NOTIFICATIONS FEATURE ===============
    async def handle_notifications(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized.")
            return
        
        # Get recent notifications/events
        try:
            logs = self.ws_admin_logs.get_all_records()
            recent_logs = []
            
            twenty_four_hours_ago = datetime.datetime.now() - datetime.timedelta(hours=24)
            
            for log in logs[-10:]:  # Last 10 logs
                timestamp_str = log.get('timestamp', '')
                if timestamp_str:
                    try:
                        log_time = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                        if log_time > twenty_four_hours_ago:
                            recent_logs.append(log)
                    except:
                        pass
            
            notifications_text = "🔔 **RECENT NOTIFICATIONS**\n\n"
            
            if recent_logs:
                for log in recent_logs[:5]:
                    action = log.get('action', '')
                    admin = log.get('admin_username', log.get('admin_id', ''))
                    timestamp = log.get('timestamp', '')
                    
                    # Format timestamp
                    try:
                        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                        time_str = dt.strftime("%H:%M")
                    except:
                        time_str = timestamp
                    
                    notifications_text += f"• **{action}** by {admin} at {time_str}\n"
                
                if len(recent_logs) > 5:
                    notifications_text += f"\n... and {len(recent_logs) - 5} more events.\n"
            else:
                notifications_text += "✅ No recent notifications.\n"
            
            # Add notification settings
            notifications_text += "\n⚙️ **Notification Settings:**\n"
            notifications_text += "• ✅ Order notifications\n"
            notifications_text += "• ✅ Error notifications\n"
            notifications_text += "• ✅ Admin action notifications\n"
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔕 Mute All", callback_data="notify_mute"),
                    InlineKeyboardButton("🔔 Unmute All", callback_data="notify_unmute")
                ],
                [
                    InlineKeyboardButton("⚙️ Settings", callback_data="notify_settings"),
                    InlineKeyboardButton("🔄 Refresh", callback_data="notify_refresh")
                ]
            ])
            
            await update.message.reply_text(
                notifications_text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Error showing notifications: {e}")
            await update.message.reply_text("❌ Error loading notifications.")
    
    # =============== HELPER METHODS ===============
    def get_admin_keyboard(self):
        """Get admin reply keyboard"""
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("👤 User Info"), KeyboardButton("💰 Payment Method")],
                [KeyboardButton("❓ Help Center"), KeyboardButton("✨ Premium & Star")],
                [KeyboardButton("👾 Broadcast"), KeyboardButton("⚙️ Bot Status")],
                [KeyboardButton("📝 Cash Control"), KeyboardButton("👤 User Search")],
                [KeyboardButton("📦 Order Management"), KeyboardButton("📊 Statistics")],
                [KeyboardButton("⚙️ Configuration"), KeyboardButton("📈 System Health")],
                [KeyboardButton("📤 Data Export"), KeyboardButton("🔔 Notifications")]
            ],
            resize_keyboard=True,
            one_time_keyboard=False
        )
