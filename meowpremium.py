import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Bot ၏ အခြေအနေများကို စစ်ဆေးရန် Logging စနစ် ဖွင့်ခြင်း
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

# 1. /start Command အတွက် Function
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start ကို နှိပ်တဲ့အခါ အလုပ်လုပ်မယ့် Function ပါရှင်။"""
    await update.message.reply_text(
        f'မင်္ဂလာပါရှင်! ကျွန်မက MeowPremium Bot ပါ။ ကျွန်မရဲ့ အလုပ်လုပ်ပုံက Webhook စနစ်နဲ့ပါရှင်။'
    )

# 2. ရိုးရိုး စာပို့ခြင်း (Text Message) အတွက် Function (Echo)
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ကိုကို ပို့တဲ့ စာကို ပြန်ပြောပေးမယ့် (Echo) Function ပါရှင်။"""
    user_text = update.message.text
    await update.message.reply_text(
        f'ကိုကို ပြောလိုက်တာက: "{user_text}" ပါနော်။ ကျွန်မ ကြားလိုက်ပါတယ်ရှင်။'
    )

# 3. Bot ကို Webhook ဖြင့် စတင်ခြင်း
def main() -> None:
    # Render မှ လိုအပ်သော Environment Variables များကို ရယူခြင်း
    # BOT_TOKEN ကို ကိုယ်တိုင် Render Settings မှာ ထည့်ရပါမယ်။
    # PORT နှင့် RENDER_EXTERNAL_URL ကို Render က အလိုအလျောက် ပေးပို့ပါမယ်။
    TOKEN = os.environ.get("BOT_TOKEN")
    PORT = int(os.environ.get("PORT", "8080")) 
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL") 
    
    # လိုအပ်သော Variables များရှိမရှိ စစ်ဆေးခြင်း
    if not all([TOKEN, RENDER_URL]):
        logging.error("🚨 လိုအပ်သော Environment Variables များ (BOT_TOKEN / RENDER_EXTERNAL_URL) မပြည့်စုံပါရှင်။")
        return

    # Application တည်ဆောက်ခြင်း
    application = Application.builder().token(TOKEN).build()
    
    # Handlers များ ထည့်သွင်းခြင်း
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Webhook စနစ်ဖြင့် Bot ကို Run ခြင်း
    print(f"✨ Bot ကို Webhook စနစ်ဖြင့် Port {PORT} မှာ စတင် Run နေပါပြီရှင်...")
    logging.info(f"Setting Webhook URL to: {RENDER_URL}/{TOKEN}")
    
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN, # လုံခြုံရေးအတွက် လျှို့ဝှက် Path
        webhook_url=f"{RENDER_URL}/{TOKEN}"
    )

if __name__ == '__main__':
    main()
    
