import sys
import asyncio
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from telegram_gtdAppbot import send_today_summary

asyncio.run(send_today_summary())