import logging
import datetime
import re
import uuid
import csv
import io
import asyncio
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
AWAIT_BROADCAST_TYPE = 35
AWAIT_BROADCAST_TARGET_USER = 36
AWAIT_USER_SEARCH = 37
AWAIT_DATA_EXPORT_TYPE = 38

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
            entry_points=[MessageHandler(filters.Text("👾 Broadcast"), self.start_broadcast_type)],
            states={
                AWAIT_BROADCAST_TYPE: [
                    CallbackQueryHandler(self.handle_broadcast_type, pattern=r"^broadcast_type_")
                ],
                AWAIT_BROADCAST_TARGET_USER: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_broadcast_target_user)
                ],
                AWAIT_BROADCAST_MESSAGE: [
                    MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL, self.receive_broadcast_message)
                ],
                AWAIT_BROADCAST_CONFIRM: [
                    CallbackQueryHandler(self.confirm_broadcast, pattern=r"^broadcast_confirm$"),
                    CallbackQueryHandler(self.cancel_broadcast, pattern=r"^broadcast_cancel$")
                ]
            },
            fallbacks=[
                MessageHandler(filters.Text("🚫 Cancel"), self.cancel_broadcast_action),
                CallbackQueryHandler(self.cancel_broadcast_action_callback, pattern=r"^broadcast_cancel$")
            ],
            allow_reentry=True
        )
        application.add_handler(broadcast_handler)
        
        # Bot Status Handler
        application.add_handler(MessageHandler(filters.Text("⚙️ Bot Status"), self.handle_bot_status))
        application.add_handler(CallbackQueryHandler(self.bot_status_callback, pattern=r"^bot_"))
        
        # Cash Control Conversation Handler
        cash_control_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Text("📝 Cash Control"), self.start_cash_control)],
            states={
                AWAIT_CASH_CONTROL_ID: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.cash_control_get_id)
                ],
                AWAIT_CASH_CONTROL_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.cash_control_apply_amount),
                    CallbackQueryHandler(self.handle_user_add_coins, pattern=r"^user_add_")
                ]
            },
            fallbacks=[MessageHandler(filters.Text("🚫 Cancel"), self.cash_control_cancel)],
            allow_reentry=True
        )
        application.add_handler(cash_control_handler)
        
        # User Search Handler
        user_search_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Text("👤 User Search"), self.start_user_search)],
            states={
                AWAIT_USER_SEARCH: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_user_search)
                ]
            },
            fallbacks=[MessageHandler(filters.Text("🚫 Cancel"), self.cancel_user_search)],
            allow_reentry=True
        )
        application.add_handler(user_search_handler)
        
        # System Health Handler
        application.add_handler(MessageHandler(filters.Text("📈 System Health"), self.handle_system_health))
        application.add_handler(CallbackQueryHandler(self.health_refresh_callback, pattern=r"^health_"))
        
        # Data Export Handler
        data_export_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Text("📤 Data Export"), self.start_data_export)],
            states={
                AWAIT_DATA_EXPORT_TYPE: [
                    CallbackQueryHandler(self.process_data_export, pattern=r"^export_")
                ]
            },
            fallbacks=[MessageHandler(filters.Text("🚫 Cancel"), self.cancel_data_export)],
            allow_reentry=True
        )
        application.add_handler(data_export_handler)
        
        # =============== USER SEARCH ACTIONS HANDLERS ===============
        application.add_handler(CallbackQueryHandler(self.handle_user_add_coins, pattern=r"^user_add_"))
        application.add_handler(CallbackQueryHandler(self.handle_user_ban_unban, pattern=r"^user_ban_"))
        application.add_handler(CallbackQueryHandler(self.handle_user_orders, pattern=r"^user_orders_"))
        application.add_handler(CallbackQueryHandler(self.handle_user_edit, pattern=r"^user_edit_"))
        
        # Edit actions handlers
        application.add_handler(CallbackQueryHandler(self.handle_edit_username, pattern=r"^edit_username_"))
        application.add_handler(CallbackQueryHandler(self.handle_edit_balance, pattern=r"^edit_balance_"))
        application.add_handler(CallbackQueryHandler(self.handle_edit_lastactive, pattern=r"^edit_lastactive_"))
        application.add_handler(CallbackQueryHandler(self.handle_edit_totalpurchase, pattern=r"^edit_totalpurchase_"))
        
        # Admin back button handler
        application.add_handler(CallbackQueryHandler(self.admin_back_callback, pattern=r"^admin_back$"))
    
    # =============== ADMIN BACK CALLBACK ===============
    async def admin_back_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin back button"""
        query = update.callback_query
        await query.answer()
        
        try:
            await query.message.delete()
        except:
            pass
        
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🏠 Returning to admin menu...",
            reply_markup=self.get_admin_keyboard()
        )
    
    # =============== ENHANCED BROADCAST FEATURE ===============
    async def start_broadcast_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized to use Broadcast.")
            return ConversationHandler.END
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Broadcast to All Users", callback_data="broadcast_type_all")],
            [InlineKeyboardButton("👤 Broadcast to Single User", callback_data="broadcast_type_single")],
            [InlineKeyboardButton("🚫 Cancel", callback_data="broadcast_cancel")]
        ])
        
        await update.message.reply_text(
            "📢 **BROADCAST TYPE SELECTION**\n\n"
            "Choose broadcast type:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
        return AWAIT_BROADCAST_TYPE
    
    async def handle_broadcast_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        
        broadcast_type = query.data.replace("broadcast_type_", "")
        context.user_data['broadcast_type'] = broadcast_type
        
        if broadcast_type == "all":
            await query.message.edit_text(
                "📢 **BROADCAST TO ALL USERS**\n\n"
                "Please enter the message you want to broadcast to all users.\n"
                "You can send text, photo, video, or document.\n"
                "Use Markdown for text formatting.\n\n"
                "Type '🚫 Cancel' to cancel.",
                parse_mode="Markdown"
            )
            return AWAIT_BROADCAST_MESSAGE
            
        elif broadcast_type == "single":
            await query.message.edit_text(
                "👤 **BROADCAST TO SINGLE USER**\n\n"
                "Please enter the User ID or Username (@username) of the target user:\n\n"
                "Type '🚫 Cancel' to cancel.",
                parse_mode="Markdown"
            )
            return AWAIT_BROADCAST_TARGET_USER
        
        return ConversationHandler.END
    
    async def handle_broadcast_target_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        target_input = update.message.text.strip()
        
        # Try to find user by ID or username
        user_id = None
        username = None
        
        if target_input.isdigit():
            user_id = int(target_input)
            try:
                cell = self.ws_user_data.find(str(user_id), in_column=1)
                if cell:
                    username_cell = self.ws_user_data.cell(cell.row, 2).value
                    username = username_cell if username_cell else f"ID:{user_id}"
                else:
                    await update.message.reply_text("❌ User not found.")
                    return AWAIT_BROADCAST_TARGET_USER
            except:
                await update.message.reply_text("❌ User not found.")
                return AWAIT_BROADCAST_TARGET_USER
        elif target_input.startswith('@'):
            username = target_input
            try:
                cell = self.ws_user_data.find(username, in_column=2)
                if cell:
                    user_id = int(self.ws_user_data.cell(cell.row, 1).value)
                else:
                    await update.message.reply_text("❌ User not found.")
                    return AWAIT_BROADCAST_TARGET_USER
            except:
                await update.message.reply_text("❌ User not found.")
                return AWAIT_BROADCAST_TARGET_USER
        else:
            await update.message.reply_text("❌ Invalid input. Please enter a valid User ID or @username.")
            return AWAIT_BROADCAST_TARGET_USER
        
        context.user_data['broadcast_target_user_id'] = user_id
        context.user_data['broadcast_target_username'] = username
        
        await update.message.reply_text(
            f"✅ Target user found: {username}\n\n"
            "Now please send the message you want to broadcast to this user.\n"
            "You can send text, photo, video, or document.\n"
            "Use Markdown for text formatting.\n\n"
            "Type '🚫 Cancel' to cancel.",
            parse_mode="Markdown"
        )
        
        return AWAIT_BROADCAST_MESSAGE
    
    async def receive_broadcast_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        broadcast_type = context.user_data.get('broadcast_type', 'all')
        
        if update.message.text:
            context.user_data['broadcast_message_type'] = 'text'
            context.user_data['broadcast_content'] = update.message.text
            preview_text = f"**Text Message Preview:**\n\n{update.message.text}"
            
        elif update.message.photo:
            context.user_data['broadcast_message_type'] = 'photo'
            context.user_data['broadcast_photo'] = update.message.photo[-1].file_id
            context.user_data['broadcast_caption'] = update.message.caption or ""
            preview_text = f"**Photo Message Preview:**\n\n{update.message.caption or '(No caption)'}"
            
        elif update.message.video:
            context.user_data['broadcast_message_type'] = 'video'
            context.user_data['broadcast_video'] = update.message.video.file_id
            context.user_data['broadcast_caption'] = update.message.caption or ""
            preview_text = f"**Video Message Preview:**\n\n{update.message.caption or '(No caption)'}"
            
        elif update.message.document:
            context.user_data['broadcast_message_type'] = 'document'
            context.user_data['broadcast_document'] = update.message.document.file_id
            context.user_data['broadcast_caption'] = update.message.caption or ""
            preview_text = f"**Document Preview:**\n\n{update.message.caption or '(No caption)'}"
        else:
            await update.message.reply_text("❌ Unsupported message type.")
            return AWAIT_BROADCAST_MESSAGE
        
        if broadcast_type == 'all':
            users = self.get_all_users()
            user_count = len(users)
            preview_info = f"**Recipients:** {user_count} users"
        else:
            target_username = context.user_data.get('broadcast_target_username', 'Unknown')
            preview_info = f"**Recipient:** {target_username}"
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Send Broadcast", callback_data="broadcast_confirm"),
                InlineKeyboardButton("🚫 Cancel", callback_data="broadcast_cancel")
            ]
        ])
        
        await update.message.reply_text(
            f"📢 **Broadcast Preview**\n\n"
            f"{preview_text}\n\n"
            f"{preview_info}\n\n"
            f"Are you sure you want to send this broadcast?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
        return AWAIT_BROADCAST_CONFIRM
    
    async def confirm_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        broadcast_type = context.user_data.get('broadcast_type', 'all')
        message_type = context.user_data.get('broadcast_message_type', 'text')
        
        if broadcast_type == 'all':
            users = self.get_all_users()
            total_users = len(users)
            successful = 0
            failed = 0
            
            status_msg = await query.message.reply_text(f"📤 Broadcasting to {total_users} users...\n✅ Successful: 0\n❌ Failed: 0")
            
            for user_data in users:
                try:
                    user_id = int(user_data['user_id'])
                    
                    if message_type == 'text':
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"📢 **ANNOUNCEMENT**\n\n{context.user_data.get('broadcast_content', '')}\n\n— Admin Team",
                            parse_mode="Markdown"
                        )
                    elif message_type == 'photo':
                        await context.bot.send_photo(
                            chat_id=user_id,
                            photo=context.user_data.get('broadcast_photo'),
                            caption=f"📢 **ANNOUNCEMENT**\n\n{context.user_data.get('broadcast_caption', '')}\n\n— Admin Team",
                            parse_mode="Markdown"
                        )
                    elif message_type == 'video':
                        await context.bot.send_video(
                            chat_id=user_id,
                            video=context.user_data.get('broadcast_video'),
                            caption=f"📢 **ANNOUNCEMENT**\n\n{context.user_data.get('broadcast_caption', '')}\n\n— Admin Team",
                            parse_mode="Markdown"
                        )
                    elif message_type == 'document':
                        await context.bot.send_document(
                            chat_id=user_id,
                            document=context.user_data.get('broadcast_document'),
                            caption=f"📢 **ANNOUNCEMENT**\n\n{context.user_data.get('broadcast_caption', '')}\n\n— Admin Team",
                            parse_mode="Markdown"
                        )
                    
                    successful += 1
                    
                    if successful % 10 == 0:
                        await status_msg.edit_text(
                            f"📤 Broadcasting to {total_users} users...\n"
                            f"✅ Successful: {successful}\n"
                            f"❌ Failed: {failed}\n"
                            f"📊 Progress: {((successful + failed) / total_users * 100):.1f}%"
                        )
                        
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    failed += 1
                    logger.error(f"Failed to send broadcast to {user_data['user_id']}: {e}")
            
            await status_msg.edit_text(
                f"✅ **Broadcast Completed!**\n\n"
                f"📊 **Statistics:**\n"
                f"• Total Users: {total_users}\n"
                f"• ✅ Successful: {successful}\n"
                f"• ❌ Failed: {failed}\n"
                f"• 📈 Success Rate: {(successful/total_users*100):.1f}%"
            )
            
            self.log_admin_action(
                admin_id=user.id,
                admin_username=user.username or str(user.id),
                action="BROADCAST_ALL",
                details=f"Type: {message_type} | Sent: {successful}/{total_users}"
            )
            
        else:
            target_user_id = context.user_data.get('broadcast_target_user_id')
            target_username = context.user_data.get('broadcast_target_username', 'Unknown')
            
            try:
                if message_type == 'text':
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=f"📢 **MESSAGE FROM ADMIN**\n\n{context.user_data.get('broadcast_content', '')}\n\n— Admin Team",
                        parse_mode="Markdown"
                    )
                elif message_type == 'photo':
                    await context.bot.send_photo(
                        chat_id=target_user_id,
                        photo=context.user_data.get('broadcast_photo'),
                        caption=f"📢 **MESSAGE FROM ADMIN**\n\n{context.user_data.get('broadcast_caption', '')}\n\n— Admin Team",
                        parse_mode="Markdown"
                    )
                elif message_type == 'video':
                    await context.bot.send_video(
                        chat_id=target_user_id,
                        video=context.user_data.get('broadcast_video'),
                        caption=f"📢 **MESSAGE FROM ADMIN**\n\n{context.user_data.get('broadcast_caption', '')}\n\n— Admin Team",
                        parse_mode="Markdown"
                    )
                elif message_type == 'document':
                    await context.bot.send_document(
                        chat_id=target_user_id,
                        document=context.user_data.get('broadcast_document'),
                        caption=f"📢 **MESSAGE FROM ADMIN**\n\n{context.user_data.get('broadcast_caption', '')}\n\n— Admin Team",
                        parse_mode="Markdown"
                    )
                
                self.log_admin_action(
                    admin_id=user.id,
                    admin_username=user.username or str(user.id),
                    action="BROADCAST_SINGLE",
                    target_user=str(target_user_id),
                    details=f"Type: {message_type} | To: {target_username}"
                )
                
                await query.message.edit_text(
                    f"✅ **Message sent successfully to {target_username}!**"
                )
                
            except Exception as e:
                logger.error(f"Failed to send broadcast to {target_user_id}: {e}")
                await query.message.edit_text(
                    f"❌ **Failed to send message to {target_username}**\n\nError: {str(e)}"
                )
        
        self._clear_broadcast_context(context)
        return ConversationHandler.END
    
    def _clear_broadcast_context(self, context):
        """Clear broadcast context data"""
        keys_to_remove = [
            'broadcast_type', 'broadcast_message_type', 'broadcast_content',
            'broadcast_photo', 'broadcast_video', 'broadcast_document',
            'broadcast_caption', 'broadcast_target_user_id', 'broadcast_target_username'
        ]
        for key in keys_to_remove:
            if key in context.user_data:
                del context.user_data[key]
    
    async def cancel_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        
        await query.message.edit_text("🚫 Broadcast cancelled.")
        self._clear_broadcast_context(context)
        return ConversationHandler.END
    
    async def cancel_broadcast_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(
            "🚫 Broadcast cancelled.",
            reply_markup=self.get_admin_keyboard()
        )
        self._clear_broadcast_context(context)
        return ConversationHandler.END
    
    async def cancel_broadcast_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        
        await query.message.edit_text("🚫 Broadcast cancelled.")
        self._clear_broadcast_context(context)
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
            [InlineKeyboardButton("🔄 Refresh Status", callback_data="bot_refresh")]
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
        elif action == "bot_refresh":
            current_status = self.get_bot_status()
            status_text = "🟢 ACTIVE" if current_status else "🔴 INACTIVE"
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🟢 Activate Bot", callback_data="bot_activate"),
                    InlineKeyboardButton("🔴 Deactivate Bot", callback_data="bot_deactivate")
                ],
                [InlineKeyboardButton("🔄 Refresh Status", callback_data="bot_refresh")]
            ])
            
            await query.message.delete()
            await context.bot.send_message(
                chat_id=user.id,
                text=f"🤖 **BOT STATUS CONTROL**\n\nCurrent Status: {status_text}\n\nChoose an action:",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            return
        
        if action in ["bot_activate", "bot_deactivate"]:
            self.log_admin_action(
                admin_id=user.id,
                admin_username=user.username or str(user.id),
                action=f"BOT_{action_text.upper()}",
                details=f"Bot {action_text}"
            )
        
        current_status = self.get_bot_status()
        status_text = "🟢 ACTIVE" if current_status else "🔴 INACTIVE"
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢 Activate Bot", callback_data="bot_activate"),
                InlineKeyboardButton("🔴 Deactivate Bot", callback_data="bot_deactivate")
            ],
            [InlineKeyboardButton("🔄 Refresh Status", callback_data="bot_refresh")]
        ])
        
        await query.message.delete()
        await context.bot.send_message(
            chat_id=user.id,
            text=f"✅ Bot {action_text}!\n\nCurrent Status: {status_text}\n\nChoose an action:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    
    # =============== CASH CONTROL FEATURE ===============
    async def start_cash_control(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized to use Cash Control.", reply_markup=self.get_admin_keyboard())
            return ConversationHandler.END
        
        await update.message.reply_text(
            "💰 **CASH CONTROL**\n\n"
            "Please enter the **User ID (number)** or **Username (@...)** of the user whose balance you want to modify.\n\n"
            "Type '🚫 Cancel' to cancel.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["🚫 Cancel"]], resize_keyboard=True)
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
                "banned": row_values[6] if len(row_values) > 6 else "FALSE",
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
            await update.message.reply_text("❌ User not found or ID/Username is invalid. Please try again or type '🚫 Cancel'.")
            return AWAIT_CASH_CONTROL_ID
        
        user_data = self.get_user_data_from_sheet(user_id_int)
        current_balance = user_data.get('coin_balance', '0')
        
        context.user_data['target_cash_control_id'] = user_id_int
        context.user_data['target_cash_control_name'] = target_username
        context.user_data['current_coin_balance'] = current_balance
        
        await update.message.reply_text(
            f"✅ **Target User Found**: {target_username} (ID `{user_id_int}`)\n"
            f"💰 **Current Coin Balance**: {current_balance} Coins\n\n"
            "Please enter the Coin amount to add or subtract.\n"
            "Use **+** for adding (e.g., `+5000`)\n"
            "Use **-** for subtracting (e.g., `-100`)\n\n"
            "Type '🚫 Cancel' to cancel.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["🚫 Cancel"]], resize_keyboard=True)
        )
        
        return AWAIT_CASH_CONTROL_AMOUNT
    
    async def cash_control_apply_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        amount_text = update.message.text.strip()
        target_user_id = context.user_data.get('target_cash_control_id')
        target_user_name = context.user_data.get('target_cash_control_name', f"ID:{target_user_id}")
        current_balance = context.user_data.get('current_coin_balance', '0')
        admin_user = update.effective_user
        
        if not target_user_id:
            await update.message.reply_text("❌ Error: Target user ID lost. Please restart Cash Control.", reply_markup=self.get_admin_keyboard())
            return ConversationHandler.END
        
        match = re.match(r"([+\-]?\d+)", amount_text)
        if not match:
            await update.message.reply_text("❌ Invalid format. Please use '+[number]', '-[number]' or just '[number]' (e.g., `+5000`, `-100`, or `10000`).")
            return AWAIT_CASH_CONTROL_AMOUNT
        
        try:
            coin_change = int(match.group(1))
        except ValueError:
            await update.message.reply_text("❌ The number provided is too large or not a valid integer.")
            return AWAIT_CASH_CONTROL_AMOUNT
        
        user_row = self.find_user_row(target_user_id)
        
        if user_row:
            try:
                old_balance = int(current_balance)
            except ValueError:
                old_balance = 0
                
            new_balance = old_balance + coin_change
            
            if new_balance < 0:
                await update.message.reply_text(
                    f"❌ Cannot subtract {abs(coin_change)} coins. User only has {old_balance} coins.\n"
                    f"Maximum subtraction allowed: {old_balance} coins."
                )
                return AWAIT_CASH_CONTROL_AMOUNT
            
            self.ws_user_data.update_cell(user_row, 3, new_balance)
            
            if coin_change > 0:
                action_text = "Added"
                action_emoji = "🟢"
                notification_text = "added to"
            elif coin_change < 0:
                action_text = "Subtracted"
                action_emoji = "🔴"
                notification_text = "subtracted from"
            else:
                action_text = "No Change"
                action_emoji = "⚪"
                notification_text = "unchanged for"
            
            admin_processed_by = f"@{admin_user.username}" if admin_user.username else f"ID:{admin_user.id}"
            
            admin_success_msg = (
                f"✅ **Cash Control Successful!**\n\n"
                f"{action_emoji} **Action:** {action_text} **{abs(coin_change):,.0f} Coins**\n"
                f"**User:** {target_user_name} (ID `{target_user_id}`)\n"
                f"**Old Balance:** {old_balance:,.0f} Coins\n"
                f"**New Balance:** {new_balance:,.0f} Coins\n"
                f"**Processed by:** {admin_processed_by}"
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
            ])
            
            await update.message.reply_text(admin_success_msg, parse_mode="Markdown", reply_markup=keyboard)
            
            self.log_admin_action(
                admin_id=admin_user.id,
                admin_username=admin_user.username or str(admin_user.id),
                action="CASH_CONTROL",
                target_user=str(target_user_id),
                details=f"Change: {coin_change} coins | Old: {old_balance} | New: {new_balance}"
            )
            
            if coin_change != 0:
                user_notification = (
                    f"💰 **Coin Balance Update**\n\n"
                    f"**{abs(coin_change):,.0f} Coins** have been {notification_text} your account by the Admin.\n\n"
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
            await update.message.reply_text("❌ Error: Target user row could not be located.", reply_markup=self.get_admin_keyboard())
        
        if 'target_cash_control_id' in context.user_data:
            del context.user_data['target_cash_control_id']
        if 'target_cash_control_name' in context.user_data:
            del context.user_data['target_cash_control_name']
        if 'current_coin_balance' in context.user_data:
            del context.user_data['current_coin_balance']
            
        return ConversationHandler.END
    
    async def cash_control_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(
            "🚫 Cash Control cancelled.",
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
            "Type '🚫 Cancel' to cancel.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["🚫 Cancel"]], resize_keyboard=True)
        )
        
        return AWAIT_USER_SEARCH
    
    async def process_user_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        search_term = update.message.text.strip()
        
        try:
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
            
            if len(found_users) == 1:
                user = found_users[0]
                user_info = self._format_user_details(user)
                
                # Get current banned status for button text
                is_banned = str(user.get('banned', 'FALSE')).upper() == 'TRUE'
                ban_button_text = "✅ Unban" if is_banned else "❌ Ban"
                
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("💰 Add Coins", callback_data=f"user_add_{user['user_id']}"),
                        InlineKeyboardButton(ban_button_text, callback_data=f"user_ban_{user['user_id']}")
                    ],
                    [
                        InlineKeyboardButton("📋 Orders", callback_data=f"user_orders_{user['user_id']}"),
                        InlineKeyboardButton("📝 Edit", callback_data=f"user_edit_{user['user_id']}")
                    ],
                    [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
                ])
                
                await update.message.reply_text(
                    user_info,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                
            else:
                results_text = f"🔍 Found {len(found_users)} users:\n\n"
                for i, user in enumerate(found_users[:10], 1):
                    banned_status = "❌" if str(user.get('banned', 'FALSE')).upper() == 'TRUE' else "✅"
                    results_text += f"{i}. {banned_status} {user.get('username', 'N/A')} (ID: `{user.get('user_id', 'N/A')}`) - {user.get('coin_balance', '0')} coins\n"
                
                if len(found_users) > 10:
                    results_text += f"\n... and {len(found_users) - 10} more users."
                
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
                ])
                
                await update.message.reply_text(
                    results_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                
        except Exception as e:
            logger.error(f"Error in user search: {e}")
            await update.message.reply_text(
                "❌ Error searching for users.",
                reply_markup=self.get_admin_keyboard()
            )
        
        return ConversationHandler.END
    
    def _format_user_details(self, user: Dict) -> str:
        banned_status = "✅ Active" if str(user.get('banned', 'FALSE')).upper() == 'FALSE' else "❌ Banned"
        
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
            "🚫 User Search cancelled.",
            reply_markup=self.get_admin_keyboard()
        )
        return ConversationHandler.END
    
    # =============== USER SEARCH ACTIONS HANDLERS ===============
    async def handle_user_add_coins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Add Coins button from user search"""
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return
        
        parts = query.data.split("_")
        if len(parts) < 3:
            await query.message.edit_text("❌ Invalid user data.")
            return
        
        try:
            target_user_id = int(parts[2])
        except ValueError:
            await query.message.edit_text("❌ Invalid user ID.")
            return
        
        context.user_data['target_cash_control_id'] = target_user_id
        context.user_data['target_cash_control_name'] = f"ID:{target_user_id}"
        
        user_data = self.get_user_data_from_sheet(target_user_id)
        current_balance = user_data.get('coin_balance', '0')
        context.user_data['current_coin_balance'] = current_balance
        
        await query.message.edit_text(
            f"💰 **ADD COINS TO USER**\n\n"
            f"User ID: `{target_user_id}`\n"
            f"Current Balance: {current_balance} Coins\n\n"
            "Please enter the amount of coins to add (use positive number):\n"
            "Example: `+5000` or just `5000`",
            parse_mode="Markdown"
        )
        
        return AWAIT_CASH_CONTROL_AMOUNT
    
    async def handle_user_ban_unban(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Ban/Unban button from user search - WORKING TOGGLE"""
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return
        
        # Get user ID from callback data
        parts = query.data.split("_")
        if len(parts) < 3:
            await query.message.edit_text("❌ Invalid user data.")
            return
        
        try:
            target_user_id = int(parts[2])
        except ValueError:
            await query.message.edit_text("❌ Invalid user ID.")
            return
        
        # Get current user data
        user_data = self.get_user_data_from_sheet(target_user_id)
        current_status = user_data.get('banned', 'FALSE')
        is_banned = str(current_status).upper() == 'TRUE'
        
        # Toggle ban status
        new_status = not is_banned
        new_status_text = "TRUE" if new_status else "FALSE"
        
        # Find the row
        row = self.find_user_row(target_user_id)
        if not row:
            await query.message.edit_text("❌ User not found in database.")
            return
        
        # Update in sheet - Column 7 is banned status
        try:
            self.ws_user_data.update_cell(row, 7, new_status_text)
        except Exception as e:
            logger.error(f"Error updating banned status: {e}")
            # Try column 8 if column 7 fails
            try:
                self.ws_user_data.update_cell(row, 8, new_status_text)
            except:
                await query.message.edit_text("❌ Error updating user status.")
                return
        
        # Log admin action
        action = "BAN_USER" if new_status else "UNBAN_USER"
        self.log_admin_action(
            admin_id=user.id,
            admin_username=user.username or str(user.id),
            action=action,
            target_user=str(target_user_id),
            details=f"Changed from {'BANNED' if is_banned else 'ACTIVE'} to {'BANNED' if new_status else 'ACTIVE'}"
        )
        
        # Prepare response
        status_emoji = "❌" if new_status else "✅"
        status_text = "BANNED" if new_status else "UNBANNED"
        action_text = "banned" if new_status else "unbanned"
        
        # Update button text
        new_ban_button_text = "✅ Unban" if new_status else "❌ Ban"
        
        # Create updated user info
        updated_user_data = {**user_data, 'banned': new_status_text}
        user_info = self._format_user_details(updated_user_data)
        
        # Create keyboard with updated button
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💰 Add Coins", callback_data=f"user_add_{target_user_id}"),
                InlineKeyboardButton(new_ban_button_text, callback_data=f"user_ban_{target_user_id}")
            ],
            [
                InlineKeyboardButton("📋 Orders", callback_data=f"user_orders_{target_user_id}"),
                InlineKeyboardButton("📝 Edit", callback_data=f"user_edit_{target_user_id}")
            ],
            [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
        ])
        
        # Update the message
        await query.message.edit_text(
            f"{status_emoji} **User {action_text.upper()} successfully!**\n\n"
            f"{user_info}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
        # Send notification to user if unbanned
        if not new_status:  # If user was unbanned
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text="🎉 **Good news! Your account has been unbanned.**\n\n"
                         "You can now access all bot features again.\n"
                         "Welcome back!",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Could not notify user about unban: {e}")
    
    async def handle_user_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Orders button from user search"""
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return
        
        parts = query.data.split("_")
        if len(parts) < 3:
            await query.message.edit_text("❌ Invalid user data.")
            return
        
        try:
            target_user_id = int(parts[2])
        except ValueError:
            await query.message.edit_text("❌ Invalid user ID.")
            return
        
        # Get user orders
        try:
            all_orders = self.ws_orders.get_all_records()
            user_orders = []
            for order in all_orders:
                if str(order.get('user_id', '')) == str(target_user_id):
                    user_orders.append(order)
            
            if not user_orders:
                await query.message.edit_text(
                    f"📊 **Orders History**\n\n"
                    f"User ID: `{target_user_id}`\n"
                    f"No orders found.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
                    ])
                )
                return
            
            # Format orders list
            orders_text = f"📊 **Orders History**\n\n"
            orders_text += f"User ID: `{target_user_id}`\n"
            orders_text += f"Total Orders: {len(user_orders)}\n\n"
            
            for i, order in enumerate(user_orders[:10], 1):
                orders_text += f"**Order {i}:**\n"
                orders_text += f"• ID: `{order.get('order_id', 'N/A')}`\n"
                orders_text += f"• Product: {order.get('product_key', 'N/A')}\n"
                orders_text += f"• Amount: {order.get('price_mmk', '0')} MMK\n"
                orders_text += f"• Status: {order.get('status', 'N/A')}\n"
                orders_text += f"• Date: {order.get('timestamp', 'N/A')}\n"
                orders_text += "---\n"
            
            if len(user_orders) > 10:
                orders_text += f"\n... and {len(user_orders) - 10} more orders."
            
            await query.message.edit_text(
                orders_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
                ])
            )
            
        except Exception as e:
            logger.error(f"Error getting user orders: {e}")
            await query.message.edit_text(
                f"❌ Error retrieving orders: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
                ])
            )
    
    async def handle_user_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Edit button from user search"""
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return
        
        parts = query.data.split("_")
        if len(parts) < 3:
            await query.message.edit_text("❌ Invalid user data.")
            return
        
        try:
            target_user_id = int(parts[2])
        except ValueError:
            await query.message.edit_text("❌ Invalid user ID.")
            return
        
        # Get current user data
        user_data = self.get_user_data_from_sheet(target_user_id)
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ Edit Username", callback_data=f"edit_username_{target_user_id}"),
                InlineKeyboardButton("💰 Edit Balance", callback_data=f"edit_balance_{target_user_id}")
            ],
            [
                InlineKeyboardButton("📅 Edit Last Active", callback_data=f"edit_lastactive_{target_user_id}"),
                InlineKeyboardButton("💵 Edit Total Purchase", callback_data=f"edit_totalpurchase_{target_user_id}")
            ],
            [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
        ])
        
        await query.message.edit_text(
            f"✏️ **EDIT USER DATA**\n\n"
            f"User ID: `{target_user_id}`\n"
            f"Username: {user_data.get('username', 'N/A')}\n"
            f"Coin Balance: {user_data.get('coin_balance', '0')}\n"
            f"Last Active: {user_data.get('last_active', 'N/A')}\n"
            f"Total Purchase: {user_data.get('total_purchase', '0')} MMK\n\n"
            f"Select what you want to edit:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    
    # =============== EDIT USER DATA FUNCTIONS ===============
    async def handle_edit_username(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Edit Username"""
        query = update.callback_query
        await query.answer()
        
        await query.message.edit_text(
            "✏️ **Edit Username**\n\n"
            "This feature is under development.\n"
            "Please use the Google Sheet directly for now.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
            ])
        )
    
    async def handle_edit_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Edit Balance - redirect to cash control"""
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        if not self.is_multi_admin(user.id):
            await query.message.edit_text("You are not authorized.")
            return
        
        parts = query.data.split("_")
        if len(parts) < 3:
            await query.message.edit_text("❌ Invalid user data.")
            return
        
        try:
            target_user_id = int(parts[2])
        except ValueError:
            await query.message.edit_text("❌ Invalid user ID.")
            return
        
        # Redirect to cash control with this user
        context.user_data['target_cash_control_id'] = target_user_id
        context.user_data['target_cash_control_name'] = f"ID:{target_user_id}"
        
        user_data = self.get_user_data_from_sheet(target_user_id)
        current_balance = user_data.get('coin_balance', '0')
        context.user_data['current_coin_balance'] = current_balance
        
        await query.message.edit_text(
            f"💰 **EDIT COIN BALANCE**\n\n"
            f"User ID: `{target_user_id}`\n"
            f"Current Balance: {current_balance} Coins\n\n"
            "Please enter the new amount (use + for add, - for subtract):\n"
            "Examples: `+5000`, `-100`, or `10000` for exact amount",
            parse_mode="Markdown"
        )
        
        return AWAIT_CASH_CONTROL_AMOUNT
    
    async def handle_edit_lastactive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Edit Last Active"""
        query = update.callback_query
        await query.answer()
        
        await query.message.edit_text(
            "✏️ **Edit Last Active**\n\n"
            "This feature is under development.\n"
            "Last active time updates automatically when user uses the bot.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
            ])
        )
    
    async def handle_edit_totalpurchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Edit Total Purchase"""
        query = update.callback_query
        await query.answer()
        
        await query.message.edit_text(
            "✏️ **Edit Total Purchase**\n\n"
            "This feature is under development.\n"
            "Total purchase updates automatically when user makes orders.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
            ])
        )
    
    # =============== SYSTEM HEALTH FEATURE ===============
    async def handle_system_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized.")
            return
        
        try:
            sheets_status = "✅ Connected" if self.ws_user_data else "❌ Disconnected"
            bot_status = "🟢 Active" if self.get_bot_status() else "🔴 Inactive"
            user_count = len(self.get_all_users())
            pending_orders = len(self.get_pending_orders())
            
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
            
            health_score = 100
            issues = []
            
            if not self.ws_user_data:
                health_score -= 30
                issues.append("Google Sheets disconnected")
            
            if pending_orders > 20:
                health_score -= 10
                issues.append("High pending orders")
            
            if isinstance(recent_errors, int) and recent_errors > 10:
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
                [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
            ])
            
            await update.message.reply_text(
                health_text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Error checking system health: {e}")
            await update.message.reply_text("❌ Error checking system health.")
    
    async def health_refresh_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "health_refresh":
            try:
                await query.message.delete()
            except:
                pass
            
            new_update = Update(
                update_id=update.update_id,
                message=query.message
            )
            await self.handle_system_health(new_update, context)
        elif query.data == "admin_back":
            try:
                await query.message.delete()
            except:
                pass
            
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="🏠 Returning to admin menu...",
                reply_markup=self.get_admin_keyboard()
            )
    
    # =============== DATA EXPORT FEATURE ===============
    async def start_data_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not self.is_multi_admin(user.id):
            await update.message.reply_text("You are not authorized to use Data Export.")
            return ConversationHandler.END
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Export Users (CSV)", callback_data="export_users")],
            [InlineKeyboardButton("📦 Export Orders (CSV)", callback_data="export_orders")],
            [InlineKeyboardButton("📝 Export Admin Logs (CSV)", callback_data="export_logs")],
            [InlineKeyboardButton("🚫 Cancel", callback_data="export_cancel")]
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
            await query.message.edit_text("🚫 Data export cancelled.")
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
            
            else:
                await query.message.edit_text("❌ Invalid export type.")
                return ConversationHandler.END
            
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            
            for row in data:
                writer.writerow(row)
            
            await context.bot.send_document(
                chat_id=user.id,
                document=io.BytesIO(output.getvalue().encode()),
                filename=filename,
                caption=f"✅ {export_type.title()} export completed.\n\n📅 Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            self.log_admin_action(
                admin_id=user.id,
                admin_username=user.username or str(user.id),
                action="DATA_EXPORT",
                details=f"Type: {export_type}"
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Back to Admin Menu", callback_data="admin_back")]
            ])
            
            await query.message.edit_text(f"✅ {export_type.title()} exported successfully!", reply_markup=keyboard)
            
        except Exception as e:
            logger.error(f"Error exporting data: {e}")
            await query.message.edit_text(f"❌ Error exporting {export_type}: {str(e)}")
        
        return ConversationHandler.END
    
    async def cancel_data_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(
            "🚫 Data export cancelled.",
            reply_markup=self.get_admin_keyboard()
        )
        return ConversationHandler.END
    
    # =============== HELPER METHODS ===============
    def get_admin_keyboard(self):
        """Get admin reply keyboard"""
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("👤 User Info"), KeyboardButton("💰 Payment Method")],
                [KeyboardButton("❓ Help Center"), KeyboardButton("✨ Premium & Star")],
                [KeyboardButton("👾 Broadcast"), KeyboardButton("⚙️ Bot Status")],
                [KeyboardButton("📝 Cash Control"), KeyboardButton("👤 User Search")],
                [KeyboardButton("📈 System Health"), KeyboardButton("📤 Data Export")]
            ],
            resize_keyboard=True,
            one_time_keyboard=False
            )
