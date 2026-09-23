# bot/bot.py

"""Telegram Bot for Game Spot.

/start — kanal obuna tekshiruvi + Web App ochish tugmasi
Foydalanuvchi kanalga a'zo bo'lmaguncha WebApp tugmasi ko'rsatilmaydi.
"""

import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:3000")
ADMIN_ID = os.getenv("ADMIN_ID", "0")
# Kanal havolasi (obuna sharti). Masalan: https://t.me/gamespotofficial
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/gamespotofficial")

# URL'dan @username ajratib olamiz (get_chat_member uchun)
_channel_path = CHANNEL_URL.rstrip("/").split("/")[-1]
CHANNEL_USERNAME = _channel_path if _channel_path.startswith("@") else "@" + _channel_path

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ALLOWED_STATUSES = {"member", "administrator", "creator"}


def _webapp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(text="🃏 Web Ilovani Ochish", web_app=WebAppInfo(url=WEBAPP_URL)),
    ]])


def _join_keyboard() -> InlineKeyboardMarkup:
    """2 ta inline tugma: kanal + tekshirish."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="📢 Kanalga Qo'shilish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")],
    ])


async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Foydalanuvchi kanalga a'zo (yoki admin) bo'lsa True."""
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ALLOWED_STATUSES
    except Exception as e:
        # Bot kanalda admin bo'lmasa yoki kanal topilmasa — log qilib, ruxsat beramiz
        # (hamma foydalanuvchi qulflanib qolmasligi uchun fail-open)
        logger.error(
            "Kanal tekshiruvi xato (%s): %s — botni '%s' kanaliga ADMIN qilib qo'shing",
            type(e).__name__, e, CHANNEL_USERNAME
        )
        return True


def _welcome_text(name: str) -> str:
    return (
        f"🎮 Game Spot ga xush kelibsiz, {name}!\n"
        "\n"
        "🃏 Game Spot — bu Telegram ichida do'stlaringiz bilan UNO o'ynash uchun yaratilgan multiplayer platforma.\n"
        "\n"
        "✨ Imkoniyatlar:\n"
        "• Xona yarating va do'stlaringizni chaqiring\n"
        "• Real-time multiplayer UNO o'yini\n"
        "• Chiroyli dark gaming dizayn\n"
        "• Statistika va reytinglar\n"
        "\n"
        "📋 Qoidalar:\n"
        "• Rangi yoki raqami mos kartani tashlang\n"
        "• Maxsus kartalar: +2, Skip, Reverse, Wild\n"
        "• 1 ta karta qolsa — UNO bosing!\n"
        "• Birinchi kartalarini tugatan — g'olib!\n"
        "\n"
        "👇 Quyidagi tugmani bosib o'yinni boshlang:"
    )


JOIN_TEXT = (
    "🔒 Botdan foydalanish uchun avval kanalimizga a'zo bo'ling!\n"
    "\n"
    "1️⃣ «Kanalga Qo'shilish» tugmasini bosing\n"
    "2️⃣ Kanalga a'zo bo'ling\n"
    "3️⃣ «✅ Tekshirish» tugmasini bosing"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kanal obunasini tekshirib, mos javob yuborish."""
    user = update.effective_user

    if not await is_subscribed(context, user.id):
        await update.message.reply_text(
            JOIN_TEXT,
            reply_markup=_join_keyboard(),
        )
        return

    await update.message.reply_text(
        _welcome_text(user.first_name),
        reply_markup=_webapp_keyboard(),
    )


async def check_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Tekshirish» tugmasi — obunani qayta tekshirish."""
    query = update.callback_query
    user = query.from_user

    if not await is_subscribed(context, user.id):
        await query.answer("❌ Hali obuna bo'lmadingiz! Kanalga a'zo bo'ling.", show_alert=True)
        return

    await query.answer("✅ Obuna tasdiqlandi!")
    await query.edit_message_text(
        f"✅ Obuna tasdiqlandi, {user.first_name}!\n\n"
        "👇 Endi o'yinni boshlashingiz mumkin:",
        reply_markup=_webapp_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help info (kanal a'zolari uchun)."""
    user = update.effective_user

    if not await is_subscribed(context, user.id):
        await update.message.reply_text(
            JOIN_TEXT,
            reply_markup=_join_keyboard(),
        )
        return

    help_text = (
        "🎮 Game Spot — UNO Multiplayer\n"
        "\n"
        "Buyruqlar:\n"
        "/start — O'yinni boshlash\n"
        "/help — Yordam\n"
        "\n"
        "📞 Muammo bo'lsa admin bilan bog'laning."
    )
    await update.message.reply_text(help_text)


def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        print("ERROR: BOT_TOKEN environment variable not set!")
        return

    logger.info(f"Starting bot with token: {BOT_TOKEN[:10]}...")
    logger.info(f"WebApp URL: {WEBAPP_URL}")
    logger.info(f"Subscribe channel: {CHANNEL_URL} ({CHANNEL_USERNAME})")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(check_sub, pattern="^check_sub$"))

    logger.info("Bot is running! Polling for updates...")
    print("Bot is running! Polling for updates...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
