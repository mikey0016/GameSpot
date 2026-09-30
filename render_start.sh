#!/bin/bash
# render_start.sh — Render Free (1 web service) da backend + bot ni birga ishga tushirish.
# Render faqat web servicelarga ruxsat beradi (background worker pullik),
# shuning uchun bot polling shu container ichida background da yashaydi.
set -e
PORT="${PORT:-8000}"
python bot/bot.py &
exec uvicorn backend.main:app --host 0.0.0.0 --port "$PORT"
