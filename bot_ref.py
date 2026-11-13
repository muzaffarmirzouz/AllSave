# -*- coding: utf-8 -*-
"""
bot.py moduliga XAVFSIZ murojaat qilish uchun umumiy yordamchi.

MUAMMO: bot `python bot.py` bilan ishga tushadi, ya'ni bot.py xotirada
`__main__` nomi ostida bo'ladi. Agar qo'shimcha modul (caption_mode.py
va h.k.) ichida `from bot import ...` yozilsa, Python bot.py'ni IKKINCHI
marta ("bot" nomi bilan) yuklab, butun faylni qaytadan bajaradi — natijada
ikkita alohida nusxa paydo bo'ladi: cookie yo'llari, Instagram so'rov
cheklovchisi (throttle), kutish ro'yxatlari va h.k. ikkiga bo'linib ketadi
(masalan /setcookies_ig bilan yangilangan cookie titr bo'limiga yetib
bormaydi, Instagram so'rovlari orasidagi tanaffus umumiy bo'lmaydi).

YECHIM: avval `__main__` ni tekshiramiz — bu aynan ishlab turgan bot.py.
Faqat u topilmasa (masalan test muhitida) oddiy import qilamiz.
Circular import bo'lmasligi uchun bu funksiya faqat handler ICHIDA
(bot to'liq ishga tushgandan keyin) chaqiriladi.
"""

import importlib
import sys


def bot_mod():
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "get_user_lang") and hasattr(main, "BOT_USERNAME_TAG"):
        return main
    return importlib.import_module("bot")
