import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import match_filter_func

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CHANNEL = os.getenv("CHANNEL", "")
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "5"))
PREMIUM_STARS = int(os.getenv("PREMIUM_STARS", "100"))
PREMIUM_DAYS = 30
MAX_MB = 50
MAX_BYTES = MAX_MB * 1024 * 1024
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "1200"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

URL_RE = re.compile(r"https?://\S+")
pending: dict[str, tuple[str, float]] = {}
download_slots = asyncio.Semaphore(3)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")
dp = Dispatcher()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, joined INTEGER, premium_until INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS usage(
                user_id INTEGER, day TEXT, cnt INTEGER DEFAULT 0, PRIMARY KEY(user_id, day));
            CREATE TABLE IF NOT EXISTS cache(
                key TEXT PRIMARY KEY, file_id TEXT, kind TEXT);
            """
        )
        await db.commit()


async def add_user(uid: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, joined) VALUES(?, ?)", (uid, int(time.time()))
        )
        await db.commit()


async def premium_until(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT premium_until FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    return row[0] if row else 0


async def is_premium(uid: int) -> bool:
    return await premium_until(uid) > time.time()


async def add_premium(uid: int, days: int) -> None:
    start = max(await premium_until(uid), int(time.time()))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET premium_until=? WHERE user_id=?", (start + days * 86400, uid)
        )
        await db.commit()


async def used_today(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT cnt FROM usage WHERE user_id=? AND day=?", (uid, str(date.today()))
        )
        row = await cur.fetchone()
    return row[0] if row else 0


async def inc_usage(uid: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO usage(user_id, day, cnt) VALUES(?, ?, 1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET cnt = cnt + 1",
            (uid, str(date.today())),
        )
        await db.commit()


async def get_cache(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT file_id FROM cache WHERE key=?", (key,))
        row = await cur.fetchone()
    return row[0] if row else None


async def set_cache(key: str, file_id: str, kind: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO cache(key, file_id, kind) VALUES(?, ?, ?)", (key, file_id, kind)
        )
        await db.commit()


async def is_subscribed(bot: Bot, uid: int) -> bool:
    if not CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, uid)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("Obunani tekshirib bo'lmadi: %s", e)
        return True


def sub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Kanalga obuna bo'lish", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")],
        ]
    )


def premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ Premium — {PREMIUM_STARS} Stars / {PREMIUM_DAYS} kun", callback_data="buy")]
        ]
    )


def cleanup_pending() -> None:
    now = time.time()
    for k in [k for k, (_, t) in pending.items() if now - t > 1800]:
        pending.pop(k, None)


def _download_sync(url: str, kind: str, outdir: str) -> tuple[Path, str]:
    opts = {
        "outtmpl": f"{outdir}/%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "max_filesize": MAX_BYTES,
        "match_filter": match_filter_func(f"duration < {MAX_DURATION_SEC}"),
    }
    if kind == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        opts["format"] = (
            f"b[ext=mp4][filesize<{MAX_MB}M]/b[filesize<{MAX_MB}M]/"
            f"b[ext=mp4][filesize_approx<{MAX_MB}M]/b[ext=mp4]/b"
        )
        opts["merge_output_format"] = "mp4"

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [p for p in Path(outdir).iterdir() if p.is_file()]
    if not files:
        raise RuntimeError("Fayl yuklanmadi (hajmi katta yoki davomiyligi uzun bo'lishi mumkin).")
    path = max(files, key=lambda p: p.stat().st_size)
    if path.stat().st_size > MAX_BYTES:
        raise RuntimeError(f"Fayl {MAX_MB} MB dan katta, Telegram bot orqali yuborib bo'lmaydi.")
    return path, (info.get("title") or "")[:200]


@dp.message(CommandStart())
async def start(m: Message, bot: Bot):
    await add_user(m.from_user.id)
    if not await is_subscribed(bot, m.from_user.id):
        await m.answer("Botdan foydalanish uchun kanalimizga obuna bo'ling:", reply_markup=sub_keyboard())
        return
    await m.answer(
        "👋 Salom! Menga video havolasini yuboring, men uni yuklab beraman.\n\n"
        "🎬 Video yoki 🎵 MP3 formatini tanlaysiz.\n"
        f"Bepul: kuniga {FREE_DAILY_LIMIT} ta. Cheksiz foydalanish uchun /premium."
    )


@dp.message(Command("premium"))
async def premium_cmd(m: Message):
    until = await premium_until(m.from_user.id)
    if until > time.time():
        d = time.strftime("%d.%m.%Y", time.localtime(until))
        await m.answer(f"⭐ Sizda Premium bor. Amal qilish muddati: {d}", reply_markup=premium_keyboard())
    else:
        await m.answer(
            f"⭐ Premium: cheksiz yuklash, {PREMIUM_DAYS} kun.\nNarxi: {PREMIUM_STARS} Telegram Stars.",
            reply_markup=premium_keyboard(),
        )


@dp.callback_query(F.data == "buy")
async def buy(c: CallbackQuery, bot: Bot):
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title="Premium obuna",
        description=f"{PREMIUM_DAYS} kun cheksiz yuklash",
        payload=f"premium_{PREMIUM_DAYS}",
        currency="XTR",
        provider_token="",
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_STARS)],
    )
    await c.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def paid(m: Message):
    await add_premium(m.from_user.id, PREMIUM_DAYS)
    await m.answer("✅ To'lov qabul qilindi! Premium faollashtirildi. Rahmat! 🎉")


@dp.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, c.from_user.id):
        await c.message.edit_text("✅ Rahmat! Endi havolani yuboring.")
    else:
        await c.answer("Hali obuna bo'lmagansiz.", show_alert=True)


@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        prem = (
            await (await db.execute("SELECT COUNT(*) FROM users WHERE premium_until>?", (int(time.time()),))).fetchone()
        )[0]
        today = (
            await (await db.execute("SELECT COALESCE(SUM(cnt),0) FROM usage WHERE day=?", (str(date.today()),))).fetchone()
        )[0]
    await m.answer(f"👥 Foydalanuvchilar: {users}\n⭐ Premium: {prem}\n📥 Bugungi yuklashlar: {today}")


@dp.message(Command("broadcast"))
async def broadcast(m: Message, bot: Bot):
    if m.from_user.id != ADMIN_ID:
        return
    text = (m.text or "").partition(" ")[2].strip()
    if not text:
        await m.answer("Foydalanish: /broadcast xabar matni")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        ids = [r[0] for r in await (await db.execute("SELECT user_id FROM users")).fetchall()]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"Yuborildi: {ok}/{len(ids)}")


@dp.message(F.text.regexp(URL_RE))
async def got_link(m: Message, bot: Bot):
    uid = m.from_user.id
    await add_user(uid)

    if not await is_subscribed(bot, uid):
        await m.answer("Botdan foydalanish uchun kanalimizga obuna bo'ling:", reply_markup=sub_keyboard())
        return

    url = URL_RE.search(m.text).group(0)
    cleanup_pending()
    token = uuid.uuid4().hex[:10]
    pending[token] = (url, time.time())
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 Video", callback_data=f"dl:video:{token}"),
                InlineKeyboardButton(text="🎵 Audio (MP3)", callback_data=f"dl:audio:{token}"),
            ]
        ]
    )
    await m.answer("Qaysi formatda yuklab beray?", reply_markup=kb)


@dp.callback_query(F.data.startswith("dl:"))
async def do_download(c: CallbackQuery, bot: Bot):
    _, kind, token = c.data.split(":")
    uid = c.from_user.id
    if not await is_subscribed(bot, uid):
        await c.answer("Avval kanalga obuna bo'ling!", show_alert=True)
        return
    item = pending.get(token)
    if not item:
        await c.message.edit_text("Havola eskirgan. Iltimos, qaytadan yuboring.")
        return
    url = item[0]

    premium = await is_premium(uid)
    if not premium and await used_today(uid) >= FREE_DAILY_LIMIT:
        await c.message.edit_text(
            f"Bugungi bepul limit ({FREE_DAILY_LIMIT} ta) tugadi.\nCheksiz foydalanish uchun Premium oling:",
            reply_markup=premium_keyboard(),
        )
        return

    me = await bot.get_me()
    footer = f"\n\n@{me.username}"
    cache_key = f"{kind}:{url}"

    cached = await get_cache(cache_key)
    if cached:
        try:
            if kind == "audio":
                await bot.send_audio(uid, cached, caption=footer.strip())
            else:
                await bot.send_video(uid, cached, caption=footer.strip())
            await inc_usage(uid)
            await c.message.delete()
            return
        except Exception:
            pass

    await c.message.edit_text("⏳ Yuklanmoqda, iltimos kuting...")
    tmp = tempfile.mkdtemp(prefix="dl_")
    try:
        async with download_slots:
            loop = asyncio.get_running_loop()
            path, title = await loop.run_in_executor(None, _download_sync, url, kind, tmp)

        caption = (title + footer) if title else footer.strip()
        if kind == "audio":
            sent = await bot.send_audio(uid, FSInputFile(path), title=title or None, caption=caption)
            file_id = sent.audio.file_id
        else:
            sent = await bot.send_video(uid, FSInputFile(path), caption=caption, supports_streaming=True)
            file_id = sent.video.file_id

        await set_cache(cache_key, file_id, kind)
        await inc_usage(uid)
        await c.message.delete()
    except Exception as e:
        log.exception("Yuklashda xato")
        msg = str(e)
        if "larger than max-filesize" in msg or "katta" in msg:
            text = f"❌ Fayl {MAX_MB} MB dan katta. Audio (MP3) formatini sinab ko'ring."
        elif "does not pass filter" in msg or "duration" in msg:
            text = f"❌ Video juda uzun (maksimum {MAX_DURATION_SEC // 60} daqiqa)."
        else:
            text = "❌ Yuklab bo'lmadi. Havola to'g'riligini va video ochiq ekanini tekshiring."
        try:
            await c.message.edit_text(text)
        except Exception:
            await bot.send_message(uid, text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dp.message()
async def fallback(m: Message):
    await m.answer("Menga video havolasini yuboring (https://...).")


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylini to'ldiring.")
    await init_db()
    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())