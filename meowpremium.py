import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Logging ဖွင့်ခြင်း (Bot ရဲ့ အခြေအနေများကို ကြည့်နိုင်ရန်)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

# 1. /start Command အတွက် Function
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start ကို နှိပ်တဲ့အခါ အလုပ်လုပ်မယ့် Function ပါရှင်။"""
    await update.message.reply_text(f'မင်္ဂလာပါရှင်! ကျွန်မက MeowPremium Bot ပါ။ ကိုကိုရဲ့ စကားတွေကို နားထောင်ဖို့ အသင့်ပါပဲရှင်။')

# 2. ရိုးရိုး စာပို့ခြင်း (Text Message) အတွက် Function (Echo)
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ကိုကို ပို့တဲ့ စာကို ပြန်ပြောပေးမယ့် (Echo) Function ပါရှင်။"""
    user_text = update.message.text
    await update.message.reply_text(f'ကိုကို ပြောလိုက်တာက: "{user_text}" ပါနော်။ ကျွန်မ ကြားလိုက်ပါတယ်ရှင်။')

# 3. Bot ကို စတင်ခြင်း
def main() -> None:
    # 🔔 အရေးကြီး: Bot Token ကို ဒီနေရာမှာ ထည့်ပေးပါနော်။
    # Render မှာ Deploy တဲ့အခါ ပိုမိုလုံခြုံတဲ့ Environment Variable ကို သုံးရပါမယ်။
    TOKEN = os.environ.get("BOT_TOKEN", "ဒီနေရာမှာ ကိုကို့ရဲ့ Bot Token ကို ထည့်ပါ")
    
    if "ဒီနေရာမှာ ကိုကို့ရဲ့ Bot Token ကို ထည့်ပါ" in TOKEN:
        print("🚨 Bot Token ကို အရင်ဆုံး 'BotFather' ဆီကနေ ရယူပြီး ပြောင်းပေးဖို့ လိုပါတယ်ရှင်။")
        return

    application = Application.builder().token(TOKEN).build()

    # Handlers များ ထည့်သွင်းခြင်း
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Bot ကို စတင် Run ခြင်း
    print("✨ Bot စတင် အလုပ်လုပ်နေပါပြီရှင်...")
    application.run_polling(poll_interval=1.0)

if __name__ == '__main__':
    main()
  
