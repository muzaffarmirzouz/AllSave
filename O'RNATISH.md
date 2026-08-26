# Video yuklab oluvchi bot — o'rnatish

Bu botni oldingi nazorat botidan **butunlay ALOHIDA** Railway loyihasiga
joylashtiring — ular bir-biriga aralashmasligi kerak.

## 1-QADAM: Yangi bot yarating
1. Telegram'da @BotFather ga o'ting, `/newbot` yozing
2. Ism va username bering (masalan: `qisqa_video_bot`)
3. Olgan tokenni saqlab qo'ying

## 2-QADAM: GitHub'ga yuklang
1. github.com'da yangi repository yarating (masalan `video-yuklovchi-bot`)
2. Shu papkadagi 3 ta faylni yuklang: `bot.py`, `requirements.txt`, `nixpacks.toml`

## 3-QADAM: Railway'da YANGI loyiha yarating
1. Railway bosh sahifasida **"+ New Project"** ni bosing (eski loyihaga qo'shmang!)
2. **"Deploy from GitHub repo"** → yangi repongizni tanlang
3. **"Variables"** bo'limiga o'ting, qo'shing:
   - `BOT_TOKEN` = 1-qadamda olgan token
   - `OWNER_CHAT_IDS` = (ixtiyoriy) faqat o'zingiz ishlatmoqchi bo'lsangiz, @userinfobot'dan olgan ID'ingizni kiriting. Bo'sh qoldirsangiz, bot hammaga ochiq bo'ladi.
4. Deploy tugashini kuting, "Logs" da "Video bot ishga tushmoqda..." yozuvini ko'ring

## 4-QADAM: Sinab ko'ring
1. Yangi botga `/start` yozing
2. Instagram Reels, TikTok yoki YouTube Shorts havolasini yuboring
3. Bir necha soniyadan keyin video kelishi kerak

## Narx haqida
Bu bot alohida Railway loyihasi bo'lgani uchun, agar ikkalasi ham bitta
Railway hisobingizda bo'lsa, ularning xarajati **qo'shiladi** (ikkalasi
uchun umumiy $5 kredit). Agar ko'p video yuklab olsangiz, oyiga qo'shimcha
to'lov chiqishi mumkin — Railway "Usage" bo'limidan kuzatib turing.

## Muhim eslatma
- Faqat 50 MB'gacha bo'lgan videolarni yuborishi mumkin (Telegram cheklovi)
- Shaxsiy foydalanish uchun ishlating — boshqalarning kontentini
  ruxsatsiz ommaviy tarqatishdan saqlaning
