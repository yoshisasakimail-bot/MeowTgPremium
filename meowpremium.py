import os
import logging
import json
import gspread
import datetime # User Registration အတွက် ထည့်သွင်းခြင်း

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, 
    CommandHandler, 
    ContextTypes, 
    MessageHandler, 
    filters, 
    ConversationHandler,
    CallbackQueryHandler
)

# ----------------- A. Configuration & Setup -----------------

# Bot ၏ အခြေအနေများကို စစ်ဆေးရန် Logging စနစ် ဖွင့်ခြင်း
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

# Global Variables (လိုအပ်သော ကိန်းရှင်များ)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789")) 
SHEET_ID = os.environ.get("SHEET_ID", "YOUR_GOOGLE_SHEET_ID_HERE") 

# Global Sheet References (Initialization မှာ တန်ဖိုးဖြည့်ပါမယ်)
GSHEET_CLIENT = None
WS_USER_DATA = None
WS_CONFIG = None
WS_ORDERS = None

# Conversation States
(
    CHOOSING_PAYMENT_METHOD, 
    WAITING_FOR_RECEIPT,
    SELECT_PRODUCT_PRICE, 
    WAITING_FOR_PHONE, 
    WAITING_FOR_USERNAME
) = range(5)


# ----------------- B. Google Sheet Initialization and Utilities -----------------

def initialize_sheets():
    """Google Sheet Client ကို စတင်ခြင်း"""
    global GSHEET_CLIENT, WS_USER_DATA, WS_CONFIG, WS_ORDERS
    
    sa_json_str = os.environ.get("GSPREAD_SA_JSON")
    
    if not sa_json_str or SHEET_ID == "YOUR_GOOGLE_SHEET_ID_HERE":
        logging.error("🚨 GSPREAD_SA_JSON သို့မဟုတ် SHEET_ID Environment Variable မတွေ့ပါရှင်။")
        return False
        
    try:
        sa_credentials = json.loads(sa_json_str)
        GSHEET_CLIENT = gspread.service_account_from_dict(sa_credentials)
        sheet = GSHEET_CLIENT.open_by_key(SHEET_ID)

        WS_USER_DATA = sheet.worksheet("user_data")
        WS_CONFIG = sheet.worksheet("config")
        WS_ORDERS = sheet.worksheet("orders")
        
        logging.info("✅ Google Sheet များ အောင်မြင်စွာ ချိတ်ဆက်ပြီးပါပြီရှင်။")
        return True

    except Exception as e:
        logging.error(f"❌ Google Sheet ချိတ်ဆက်ရာတွင် Error: {e}")
        return False


def get_config_data() -> dict:
    """Reads the entire config sheet and returns a dictionary {key: value}."""
    global WS_CONFIG
    if not WS_CONFIG:
        logging.error("❌ config sheet object is None.")
        return {}
    try:
        records = WS_CONFIG.get_all_records()
        config_dict = {str(item.get('key')).strip(): str(item.get('value')).strip() 
                       for item in records if item.get('key') and item.get('value') is not None}
        return config_dict
    except Exception as e:
        logging.error(f"❌ Error reading config sheet: {e}")
        return {}


def get_product_keyboard(product_type: str) -> InlineKeyboardMarkup:
    """Dynamically generates the product price keyboard based on product type (star/premium)."""
    config = get_config_data()
    keyboard_buttons = []
    
    prefix = f'{product_type}_'
    product_keys = sorted([k for k in config.keys() if k.startswith(prefix)])

    for key in product_keys:
        price = config.get(key)
        
        if price:
            button_name = key.replace(prefix, '').replace('_', ' ').title()
            
            button_text = f"{'⭐' if product_type == 'star' else '💎'} {button_name} ({price} MMK)"
            
            keyboard_buttons.append([InlineKeyboardButton(button_text, callback_data=f'{key}')])

    keyboard_buttons.append([InlineKeyboardButton("⬅️ Back to Service Menu", callback_data='menu_back')])
    
    return InlineKeyboardMarkup(keyboard_buttons)


def register_user_if_not_exists(user_id: int, username: str):
    """Checks if user exists in user_data sheet. If not, adds the user."""
    global WS_USER_DATA
    if not WS_USER_DATA:
        logging.error("❌ user_data sheet object is None for registration.")
        return

    try:
        # User ID ကို ရှာဖွေခြင်း
        cell = WS_USER_DATA.find(str(user_id), in_column=1) 
        
        if cell is None:
            # User မရှိသေးပါက တန်းအသစ်တစ်ခု ထည့်သွင်းခြင်း
            today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_row = [str(user_id), username if username else 'N/A', 0, today]
            WS_USER_DATA.append_row(new_row, value_input_option='USER_ENTERED')
            logging.info(f"✅ New user registered: {user_id}")
            
        else:
            logging.info(f"User already exists: {user_id}")

    except Exception as e:
        logging.error(f"❌ Error during user registration: {e}")


# ----------------- C. Keyboard Definitions -----------------

# Reply Keyboard (persistent bottom menu)
ENGLISH_REPLY_KEYBOARD = [
    [
        KeyboardButton("👤 User Account"),
        KeyboardButton("💰 Payment Method")
    ],
    [
        KeyboardButton("❓ Help Center")
    ]
]
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(ENGLISH_REPLY_KEYBOARD, resize_keyboard=True, one_time_keyboard=False)

# Inline Keyboard (Initial Product Selection)
INITIAL_INLINE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("⭐ Telegram Star", callback_data='product_star'),
        InlineKeyboardButton("💎 Telegram Premium", callback_data='product_premium')
    ]
])


# ----------------- D. Command & Message Handlers -----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /start command and registers user if not exists."""
    user = update.effective_user
    
    # User Registration Logic ကို စတင်ခေါ်ယူခြင်း
    register_user_if_not_exists(user.id, user.full_name) 

    welcome_text = (
        f"Hello, **{user.full_name}**! "
        f"Welcome to our service. Please select from the menu below:"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=MAIN_MENU_KEYBOARD,
        parse_mode='Markdown'
    )
    await update.message.reply_text(
        "Available Services:",
        reply_markup=INITIAL_INLINE_KEYBOARD
    )


async def handle_user_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles User Account button press."""
    # ဤနေရာတွင် Google Sheet မှ User Data (Coin Balance, Order History) များကို ဆွဲယူပြီး ပြသရပါမည်။
    await update.message.reply_text("👤 User Account details will be retrieved from Google Sheet. (To be implemented)")


async def handle_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles Payment Method button press and initial payment options."""
    config = get_config_data() # Sheet မှ Data ယူခြင်း
    # ဤနေရာတွင် Coin ဈေးနှုန်းများကို ပြရပါမည်။
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 Kpay (KBZ Pay)", callback_data='pay_kpay'),
            InlineKeyboardButton("💸 Wave Money", callback_data='pay_wave')
        ]
    ])
    
    # Update object မျိုးစုံကနေ message ကို ကိုင်တွယ်နိုင်အောင် ပြင်ဆင်ခြင်း
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "💰 Select a method for coin purchase:",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            "💰 Select a method for coin purchase:",
            reply_markup=keyboard
        )
    return CHOOSING_PAYMENT_METHOD


async def handle_help_center(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles Help Center button press, retrieves admin contact from config sheet."""
    config = get_config_data()
    # Sheet ကနေ Admin Contact Username ကို ဆွဲယူခြင်း
    admin_username = config.get('admin_contact_username', '@AdminUsername_Error') 
    
    help_text = (
        "❓ **Help Center**\n\n"
        f"For assistance or issues, please contact the administrator:\n"
        f"Admin Contact: **{admin_username}**\n\n"
        "We will respond as quickly as possible."
    )
    
    # Main Menu ကို ပြန်သွားဖို့ Back ခလုတ် ထည့်ခြင်း
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data='menu_back')]])
    
    await update.message.reply_text(
        help_text,
        reply_markup=back_keyboard,
        parse_mode='Markdown'
    )


# ----------------- E. Payment Conversation Handlers -----------------

async def start_payment_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Displays payment details and prompts for receipt."""
    query = update.callback_query
    await query.answer()
    
    payment_method = query.data.split('_')[1]
    config = get_config_data() # Sheet မှ Data ယူခြင်း
    
    # Sheet မှ Admin ၏ ငွေလက်ခံမည့်အချက်အလက်များ ဆွဲယူခြင်း
    admin_name = config.get(f'{payment_method}_name', 'Admin Name (Error)')
    phone_number = config.get(f'{payment_method}_phone', '09XXXXXXXXX (Error)')
    
    # Back button ကို ထည့်သွင်းခြင်း
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Payment Menu", callback_data='payment_back')]])

    transfer_text = (
        f"✅ Please transfer the payment via **{payment_method.upper()}** as follows:\n\n"
        f"Name: **{admin_name}**\n"
        f"Phone Number: **{phone_number}**\n\n"
        f"**Please send the receipt (Screenshot) after the transfer.**"
    )
    
    await query.edit_message_text(transfer_text, reply_markup=back_keyboard, parse_mode='Markdown')
    return WAITING_FOR_RECEIPT


async def receive_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives receipt (photo/text) and forwards to Admin."""
    
    # ဤနေရာတွင် Admin Group သို့ ပြေစာ၊ User Info, Coin ပမာဏ များကို Forward လုပ်ရပါမည်။
    
    await update.message.reply_text(
        "💌 Receipt sent to Admin. Please wait for coin deposit confirmation."
    )
    return ConversationHandler.END


async def back_to_payment_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the 'Back' button press from the transfer details screen."""
    query = update.callback_query
    await query.answer()
    
    # Payment Method Menu ကို ပြန်ပို့ခြင်း
    return await handle_payment_method(query, context)


# ----------------- F. Product Purchase Conversation Handlers -----------------

async def start_product_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles callback from 'Telegram Star' or 'Telegram Premium' button."""
    query = update.callback_query
    await query.answer()
    
    product_type = query.data.split('_')[1] # 'star' or 'premium'
    context.user_data['product_type'] = product_type
    
    keyboard = get_product_keyboard(product_type)
    
    await query.edit_message_text(
        f"Please select the duration/amount for the **Telegram {product_type.upper()}** purchase:",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )
    return SELECT_PRODUCT_PRICE


async def select_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles price button press and prompts for phone number."""
    query = update.callback_query
    await query.answer()
    
    selected_key = query.data # ဥပမာ: 'star_100' သို့မဟုတ် 'premium_1year'
    
    context.user_data['product_key'] = selected_key
    
    await query.edit_message_text(
        f"You selected {selected_key.upper().replace('_', ' ')}.\n"
        f"Please send the **Telegram Phone Number** for the service. (Digits only)"
    )
    return WAITING_FOR_PHONE


async def validate_phone_and_ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validates phone number input and asks for username."""
    user_input = update.message.text
    
    if user_input and user_input.isdigit() and len(user_input) >= 8:
        context.user_data['premium_phone'] = user_input
        await update.message.reply_text(
            f"Thank you. Now, please send the **Telegram Username** associated with the phone number {user_input}."
        )
        return WAITING_FOR_USERNAME
    else:
        await update.message.reply_text(
            "❌ Invalid input. Please send the **Telegram Phone Number** (digits only) that you want to top up."
        )
        return WAITING_FOR_PHONE


async def finalize_product_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles username input, checks coin balance, and places the order."""
    
    user_id = update.effective_user.id
    product_key = context.user_data.get('product_key', 'N/A')
    
    config = get_config_data()
    price_mmk_str = config.get(product_key)
    
    if price_mmk_str is None:
        await update.message.reply_text("❌ Error: Could not retrieve price for the selected product from the sheet.")
        return ConversationHandler.END

    try:
        COIN_PRICE_REQUIRED = int(price_mmk_str) 
    except ValueError:
        await update.message.reply_text("❌ Error: Product price in the sheet is not a valid number.")
        return ConversationHandler.END

    # (၂) User ရဲ့ Coin Balance ကို Google Sheet (user_data) ကနေ စစ်ဆေးခြင်း
    # ဤနေရာတွင် WS_USER_DATA မှ User ID ကို ရှာဖွေပြီး Coin Balance ကို ဆွဲယူရမည်။
    USER_COINS = 500 # ဥပမာသာ (Google Sheet မှ ဆွဲယူရမည်)
    
    context.user_data['premium_username'] = update.message.text

    if USER_COINS >= COIN_PRICE_REQUIRED:
        # (၃) Order ကို Orders Sheet သို့ ရေးသွင်းခြင်းနှင့် Coin နှုတ်ခြင်း
        
        await update.message.reply_text(
            f"✅ Order Successful! {COIN_PRICE_REQUIRED} Coins have been deducted for {product_key.upper().replace('_', ' ')}. "
            f"Please wait a moment while your service is being activated."
        )
        return ConversationHandler.END
    else:
        # (၄) Coin မရှိပါက ငွေဖြည့်ရန် ညွှန်ကြားခြင်း
        await update.message.reply_text(
            f"❌ Insufficient Coin Balance. You need {COIN_PRICE_REQUIRED} Coins but only have {USER_COINS} Coins. "
            f"Please use the **'💰 Payment Method'** button to top up."
        )
        return ConversationHandler.END


async def back_to_service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the 'Back' button press from the product selection menu."""
    query = update.callback_query
    await query.answer()
    
    user = query.effective_user
    
    welcome_text = (
        f"Hello, **{user.full_name}**! "
        f"Welcome to our service. Please select from the menu below:"
    )
    
    # ဤနေရာတွင် callback နှိပ်ပြီးနောက် Message အသစ်ပို့ခြင်း
    await query.message.reply_text(
        welcome_text,
        reply_markup=MAIN_MENU_KEYBOARD,
        parse_mode='Markdown'
    )
    await query.message.reply_text(
        "Available Services:",
        reply_markup=INITIAL_INLINE_KEYBOARD
    )
    return ConversationHandler.END


# ----------------- G. Main Function (Application Integration) -----------------

def main() -> None:
    # Google Sheet ချိတ်ဆက်မှုကို စတင်ခြင်း
    if not initialize_sheets():
        logging.error("❌ Bot ကို Google Sheet မပါဘဲ စတင်၍မရပါရှင်။")
        return

    TOKEN = os.environ.get("BOT_TOKEN")
    PORT = int(os.environ.get("PORT", "8080")) 
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL") 
    
    if not all([TOKEN, RENDER_URL]):
        logging.error("🚨 လိုအပ်သော Environment Variables များ (BOT_TOKEN / RENDER_EXTERNAL_URL) မပြည့်စုံပါရှင်။")
        return

    application = Application.builder().token(TOKEN).build()
    
    # 1. Command Handlers
    application.add_handler(CommandHandler("start", start_command))

    # 2. Payment Conversation Handler
    payment_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("💰 Payment Method"), handle_payment_method)],
        states={
            CHOOSING_PAYMENT_METHOD: [
                CallbackQueryHandler(start_payment_conv, pattern='^pay_'),
                CallbackQueryHandler(back_to_payment_menu, pattern='^payment_back') 
            ],
            WAITING_FOR_RECEIPT: [
                MessageHandler(filters.PHOTO | filters.TEXT, receive_receipt), 
                CallbackQueryHandler(back_to_payment_menu, pattern='^payment_back') 
            ],
        },
        fallbacks=[]
    )
    application.add_handler(payment_conv_handler)
    
    # 3. Product Purchase Conversation Handler (Star and Premium)
    product_purchase_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_product_purchase, pattern='^product_'),
            CallbackQueryHandler(back_to_service_menu, pattern='^menu_back$')
        ],
        states={
            SELECT_PRODUCT_PRICE: [
                CallbackQueryHandler(select_product_price, pattern='^star_|^premium_'),
                CallbackQueryHandler(back_to_service_menu, pattern='^menu_back$') 
            ],
            
            WAITING_FOR_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, validate_phone_and_ask_username)
            ],
            
            WAITING_FOR_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_product_order)
            ]
        },
        fallbacks=[]
    )
    application.add_handler(product_purchase_handler)
    
    # 4. Message Handlers (Reply Keyboard ခလုတ်များ)
    application.add_handler(MessageHandler(filters.Text("👤 User Account"), handle_user_account))
    application.add_handler(MessageHandler(filters.Text("❓ Help Center"), handle_help_center)) # Help Center Handler
    
    # Webhook စနစ်ဖြင့် Bot ကို Run ခြင်း
    print(f"✨ Bot ကို Webhook စနစ်ဖြင့် Port {PORT} မှာ စတင် Run နေပါပြီရှင်...")
    logging.info(f"Setting Webhook URL to: {RENDER_URL}/{TOKEN}")
    
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN, 
        webhook_url=f"{RENDER_URL}/{TOKEN}"
    )

if __name__ == '__main__':
    main()

