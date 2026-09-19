# bot/bot.py

"""Telegram Bot for Game Spot.
/start — bot haqida ma'lumot va Web App ochish tugmasi
"""

import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:3000")
ADMIN_ID = os.getenv("ADMIN_ID", "0")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with Web App button."""
    user = update.effective_user

    welcome_text = (
        f"🎮 Game Spot ga xush kelibsiz, {user.first_name}!\n"
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

    button = InlineKeyboardButton(
        text="🃏 Web Ilovani Ochish",
        web_app=WebAppInfo(url=WEBAPP_URL),
    )
    reply_markup = InlineKeyboardMarkup([[button]])

    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help info."""
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

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    logger.info("Bot is running! Polling for updates...")
    print("Bot is running! Polling for updates...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
