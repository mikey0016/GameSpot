# bot/bot.py

"""Telegram Bot for Game Spot.

/start — kanal obuna tekshiruvi + Web App ochish tugmasi
Foydalanuvchi kanalga a'zo bo'lmaguncha WebApp tugmasi ko'rsatilmaydi.

Kanal tekshiruvi HAQIQIY Telegram API (getChatMember) orqali bajariladi:
- True  → a'zo (member/administrator/creator)
- False → a'zo emas (left/kicked/blocked yoki umuman topilmadi)
- None  → API xatosi (bot kanal admin emas, kanal topilmadi, tarmoq xatosi...)
          Bu holatda foydalanuvchi O'TKAZILMAYDI (fail-closed) va
          aniq xato xabari ko'rsatiladi — hech qachon "obuna bo'ldi" deyilmaydi.
"""

import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:3000")
ADMIN_ID = os.getenv("ADMIN_ID", "0")
# Kanal havolasi (obuna sharti). Masalan: https://t.me/gamespotofficial
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/gamespotofficial")
# Ixtiyoriy: privat kanal uchun raqamli ID (-100...). Bo'sh bo'lsa CHANNEL_URL'dan olinadi.
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()

# URL'dan @username ajratib olamiz (get_chat_member uchun)
_channel_path = CHANNEL_URL.rstrip("/").split("/")[-1]
CHANNEL_USERNAME = _channel_path if _channel_path.startswith("@") else "@" + _channel_path
# getChatMember uchun ishlatiladigan manzil: privat kanal bo'lsa raqamli ID g'alaba qozonadi
CHANNEL_REF = CHANNEL_ID or CHANNEL_USERNAME

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


async def check_telegram_subscription(bot, user_id: int) -> bool | None:
    """Foydalanuvchi kanalga a'zoligini Telegram Bot API orqali tekshirish.

    Qaytaradi:
      True  → a'zo (member/administrator/creator)
      False → a'zo emas (left/kicked yoki user topilmadi)
      None  → API xato — qaror qabul qilib BO'LMAYDI (chaqiruvchi xato xabari ko'rsatadi)
    """
    if not user_id or not CHANNEL_REF:
        return None
    try:
        member = await bot.get_chat_member(CHANNEL_REF, int(user_id))
        return member.status in ALLOWED_STATUSES
    except BadRequest as e:
        msg = str(e).lower()
        # Bu ikkisi = foydalanuvchi kanalda yo'q → a'zo emas (haqiqiy javob, xato emas)
        if "user not found" in msg or "participant" in msg or "member not found" in msg:
            return False
        logger.error(
            "Kanal tekshiruvi BadRequest (%s): %s — botni '%s' kanaliga ADMIN qilib qo'shing",
            CHANNEL_REF, e, CHANNEL_REF
        )
        return None
    except Exception as e:
        logger.error(
            "Kanal tekshiruvi xato (%s): %s — botni '%s' kanaliga ADMIN qilib qo'shing",
            type(e).__name__, e, CHANNEL_REF
        )
        return None


# Eski nom bilan moslik (boshqa joyda ishlatilsa): endi uch holat qaytaradi
async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool | None:
    return await check_telegram_subscription(context.bot, user_id)


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

ERROR_TEXT = (
    "⚠️ Obuna tekshiruvida texnik xatolik yuz berdi.\n"
    "\n"
    "Bot kanalga ADMIN bo'lishi kerak. Birozdan so'ng qayta tekshirib ko'ring:\n"
    "/start buyrug'ini yuboring yoki «✅ Tekshirish» tugmasini bosing."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kanal obunasini tekshirib, mos javob yuborish."""
    user = update.effective_user

    sub = await check_telegram_subscription(context.bot, user.id)
    if sub is None:
        # API xato — foydalanuvchini qulflab qo'ymaymiz lekin O'TKAZMAYMIZ ham
        await update.message.reply_text(ERROR_TEXT)
        return
    if sub is False:
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
    """«✅ Tekshirish» tugmasi — HAQIQIY getChatMember tekshiruvi.

    - Har doim query.answer() chaqiriladi (spinner abadiy aylantirmaydi)
    - Tekshiruv Telegram API orqali; frontend/localStorage ishlatilmaydi
    - Tugmani bir necha marta bosish xavfsiz (edit_message_text xatosi ushlanadi)
    """
    query = update.callback_query
    user = query.from_user

    sub = await check_telegram_subscription(context.bot, user.id)

    if sub is None:
        await query.answer("⚠️ Tekshiruvda xatolik — qayta urinib ko'ring", show_alert=True)
        return

    if sub is False:
        await query.answer("❌ Hali obuna bo'lmadingiz! Kanalga a'zo bo'ling.", show_alert=True)
        try:
            await query.edit_message_text(JOIN_TEXT, reply_markup=_join_keyboard())
        except TelegramError:
            pass  # xabar allaqachon shu holatda (qayta bosilgan) — jim o'tkazamiz
        return

    await query.answer("✅ Obuna tasdiqlandi!")
    try:
        await query.edit_message_text(
            f"✅ Obuna tasdiqlandi, {user.first_name}!\n\n"
            "👇 Endi o'yinni boshlashingiz mumkin:",
            reply_markup=_webapp_keyboard(),
        )
    except TelegramError:
        pass  # allaqachon tasdiqlangan xabar — jim o'tkazamiz


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help info (kanal a'zolari uchun)."""
    user = update.effective_user

    sub = await check_telegram_subscription(context.bot, user.id)
    if sub is None:
        await update.message.reply_text(ERROR_TEXT)
        return
    if sub is False:
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
    logger.info(f"Subscribe channel: {CHANNEL_URL} (check via: {CHANNEL_REF})")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(check_sub, pattern="^check_sub$"))

    logger.info("Bot is running! Polling for updates...")
    print("Bot is running! Polling for updates...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
