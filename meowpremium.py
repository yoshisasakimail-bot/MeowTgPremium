import os
import logging
import json
import gspread

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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789")) # 👈 Admin ID ကို Render မှာ ထည့်ပါ
SHEET_ID = os.environ.get("SHEET_ID", "YOUR_GOOGLE_SHEET_ID_HERE") # 👈 Sheet ID ကို Render မှာ ထည့်ပါ

# Global Sheet References (Initialization မှာ တန်ဖိုးဖြည့်ပါမယ်)
GSHEET_CLIENT = None
WS_USER_DATA = None
WS_CONFIG = None
WS_ORDERS = None

# Conversation States (Payment Flow အတွက်)
CHOOSING_PAYMENT_METHOD, WAITING_FOR_RECEIPT = range(2)


# ----------------- B. Google Sheet Initialization -----------------

def initialize_sheets():
    """Google Sheet Client ကို စတင်ခြင်း"""
    global GSHEET_CLIENT, WS_USER_DATA, WS_CONFIG, WS_ORDERS
    
    # Render Environment မှ JSON Key ကို ရယူခြင်း
    sa_json_str = os.environ.get("GSPREAD_SA_JSON")
    
    if not sa_json_str or SHEET_ID == "YOUR_GOOGLE_SHEET_ID_HERE":
        logging.error("🚨 GSPREAD_SA_JSON သို့မဟုတ် SHEET_ID Environment Variable မတွေ့ပါရှင်။")
        return False
        
    try:
        sa_credentials = json.loads(sa_json_str)
        GSHEET_CLIENT = gspread.service_account_from_dict(sa_credentials)
        sheet = GSHEET_CLIENT.open_by_key(SHEET_ID)

        # Sheet များ ဖွင့်ခြင်း
        WS_USER_DATA = sheet.worksheet("user_data")
        WS_CONFIG = sheet.worksheet("config")
        WS_ORDERS = sheet.worksheet("orders")
        
        logging.info("✅ Google Sheet များ အောင်မြင်စွာ ချိတ်ဆက်ပြီးပါပြီရှင်။")
        return True

    except Exception as e:
        logging.error(f"❌ Google Sheet ချိတ်ဆက်ရာတွင် Error: {e}")
        return False

# ----------------- C. Keyboard Definitions -----------------

# Reply Keyboard (စာရိုက်တဲ့နားမှာ ပေါ်နေမယ့် ခလုတ်များ)
REPLY_KEYBOARD = [
    [
        KeyboardButton("👤 User Account"),
        KeyboardButton("💰 Payment Method")
    ],
    [
        KeyboardButton("❓ Help Center")
    ]
]
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(REPLY_KEYBOARD, resize_keyboard=True, one_time_keyboard=False)

# Inline Keyboard (ပထမဆုံး ဝန်ဆောင်မှု ရွေးရန်)
INITIAL_INLINE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("⭐ Telegram Star", callback_data='product_star'),
        InlineKeyboardButton("💎 Telegram Premium", callback_data='product_premium')
    ]
])


# ----------------- D. Command & Message Handlers -----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start ကို နှိပ်တဲ့အခါ အလုပ်လုပ်မယ့် Function ပါရှင်။"""
    user = update.effective_user
    
    # User ရဲ့ နာမည်ကို Unicode ဖြင့် တွဲဖက် ပြသခြင်း
    welcome_text = (
        f"𐙚 𝒥𝒾𝒥𝒾 ᥫ᭡ **{user.full_name}**၊ "
        f"ကျွန်မရဲ့ ဝန်ဆောင်မှုများကို ရွေးချယ်နိုင်ပါတယ်:"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=MAIN_MENU_KEYBOARD, # Reply Keyboard ကို ပြသခြင်း
        parse_mode='Markdown'
    )
    # Inline Keyboard ကို သီးသန့် ပို့ခြင်း
    await update.message.reply_text(
        "ရောင်းချပေးနိုင်တဲ့ ဝန်ဆောင်မှုတွေ:",
        reply_markup=INITIAL_INLINE_KEYBOARD
    )


async def handle_user_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User Account ကို နှိပ်တဲ့အခါ အလုပ်လုပ်မယ့် Function ပါရှင်။"""
    # ဤနေရာတွင် Google Sheet မှ User Data (Coin Balance, Order History) များကို ရယူပြီး ပြသရပါမည်။
    await update.message.reply_text("👤 User Account အချက်အလက်များကို Google Sheet မှ ဆွဲယူပြသပါမည်။ (ဆက်လက်ရေးသားရမည့်အပိုင်း)")


async def handle_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Payment Method ကို နှိပ်တဲ့အခါ အလုပ်လုပ်မယ့် Function ပါရှင်။"""
    # ဤနေရာတွင် Coin ဈေးနှုန်းများကို Google Sheet မှ ဆွဲယူပြီး Payment ခလုတ်များ ပြရပါမည်။
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 Kpay (KBZ Pay)", callback_data='pay_kpay'),
            InlineKeyboardButton("💸 Wave Money", callback_data='pay_wave')
        ]
    ])
    await update.message.reply_text(
        "💰 Coin ဈေးနှုန်းများကို ပြသပြီး၊ ငွေလွှဲဖို့အတွက် ပုံစံရွေးချယ်ပါရှင်။",
        reply_markup=keyboard
    )
    return CHOOSING_PAYMENT_METHOD # Conversation Handler ကို စတင်ခြင်း

# ----------------- E. Payment Conversation Handlers -----------------

async def start_payment_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Kpay/Wave ခလုတ် နှိပ်ပြီးနောက် အလုပ်လုပ်မယ့် Function ပါရှင်။"""
    query = update.callback_query
    await query.answer()
    
    payment_method = query.data.split('_')[1]
    
    # ဤနေရာတွင် Google Sheet (config) မှ Phone Number နှင့် Name များကို ဆွဲယူရပါမည်။
    # ဥပမာ- config sheet ကနေ Kpay phone, Wave phone ယူရပါမယ်။
    
    await query.edit_message_text(
        f"✅ {payment_method.upper()} မှတစ်ဆင့် ငွေပေးချေရန်အတွက် အောက်ပါအတိုင်း လွှဲပြောင်းပေးပါ:\n\n"
        f"ဖုန်းနံပါတ်: 09XXXXXXXXX (Sheet မှယူ)\n"
        f"အမည်: Admin Name (Sheet မှယူ)\n\n"
        f"ငွေလွှဲပြီးပါက **ပြေစာ (Screenshot)** ကို ပေးပို့ပေးပါရှင်။"
    )
    return WAITING_FOR_RECEIPT

async def receive_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User က ပေးပို့လိုက်တဲ့ ပြေစာကို လက်ခံခြင်းနှင့် Admin သို့ Forward လုပ်ခြင်း။"""
    # ဤနေရာတွင် Admin Group သို့ ပြေစာ၊ User Info, Coin ပမာဏ များကို Forward လုပ်ရပါမည်။
    
    await update.message.reply_text(
        "💌 ငွေလွှဲပြေစာကို Admin သို့ ပေးပို့လိုက်ပါပြီရှင်။ Coin ထည့်သွင်းပေးရန် စောင့်ဆိုင်းနေပါသည်။"
    )
    # Admin ထံမှ 'Done' သို့မဟုတ် 'Failed' Reply ရသည်အထိ ဒီအဆင့်မှာပဲ ရပ်နေပါမယ်။
    return ConversationHandler.END # စမ်းသပ်ရန်အတွက် Conversation ကို ချက်ချင်း အဆုံးသတ်ထားသည်


# ----------------- F. Main Function (Application Integration) -----------------

def main() -> None:
    # Google Sheet ချိတ်ဆက်မှုကို စတင်ခြင်း
    if not initialize_sheets():
        logging.error("❌ Bot ကို Google Sheet မပါဘဲ စတင်၍မရပါရှင်။")
        return

    # Render မှ လိုအပ်သော Environment Variables များကို ရယူခြင်း
    TOKEN = os.environ.get("BOT_TOKEN")
    PORT = int(os.environ.get("PORT", "8080")) 
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL") 
    
    if not all([TOKEN, RENDER_URL]):
        logging.error("🚨 လိုအပ်သော Environment Variables များ (BOT_TOKEN / RENDER_EXTERNAL_URL) မပြည့်စုံပါရှင်။")
        return

    # Application တည်ဆောက်ခြင်း
    application = Application.builder().token(TOKEN).build()
    
    # 1. Command Handlers
    application.add_handler(CommandHandler("start", start_command))

    # 2. Conversation Handler (Payment Flow)
    payment_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("💰 Payment Method"), handle_payment_method)],
        states={
            CHOOSING_PAYMENT_METHOD: [CallbackQueryHandler(start_payment_conv, pattern='^pay_')],
            WAITING_FOR_RECEIPT: [MessageHandler(filters.PHOTO | filters.TEXT, receive_receipt)], # ဓာတ်ပုံ သို့ စာကို လက်ခံခြင်း
        },
        fallbacks=[]
    )
    application.add_handler(payment_conv_handler)
    
    # 3. Message Handlers (Reply Keyboard ခလုတ်များ)
    application.add_handler(MessageHandler(filters.Text("👤 User Account"), handle_user_account))
    # filters.Text("❓ Help Center") ကတော့ ရိုးရိုး စာပြန်ပို့တဲ့ Function သုံးလို့ရပါတယ်။

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

