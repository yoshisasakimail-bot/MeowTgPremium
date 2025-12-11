import os
import logging
import json # JSON Key ကို ကိုင်တွယ်ရန်
import gspread # Google Sheet အတွက်

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler

# ... (logging.basicConfig အောက်မှာ ထည့်ပါ)

# Global Variables (လိုအပ်သော ကိန်းရှင်များ)
ADMIN_ID = 123456789 # 👈 Admin ရဲ့ Telegram User ID ကို ဒီမှာ ထည့်ပါနော်
SHEET_ID = "YOUR_GOOGLE_SHEET_ID_HERE" # 👈 ကိုကို့ရဲ့ Google Sheet URL က ID ကို ထည့်ပါ

# Global Sheet References (Initialization မှာ တန်ဖိုးဖြည့်ပါမယ်)
GSHEET_CLIENT = None
WS_USER_DATA = None
WS_CONFIG = None
WS_ORDERS = None

