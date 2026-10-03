"""Телеграм-бот: узнаёт песню из видео, кружка, голосового, аудио или по ссылке.

Что умеет:
  - принимает видео, кружок, голосовое, аудиофайл и распознаёт музыку (Shazam)
  - принимает ссылку на видео (YouTube, TikTok, Instagram и т.д.), качает звук и распознаёт
  - если написать текстом кусок строчки или название, попробует найти трек

Запуск:
    pip install -r requirements.txt
    (нужен установленный ffmpeg)
    export BOT_TOKEN="токен от @BotFather"
    python bot.py
"""
import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

import aiohttp
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
from shazamio import Shazam

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8615217948:AAHcC9lWWfnNNiantBUA-DESKu6AjZHDg6Q")
MAX_DOWNLOAD_MB = 20  # лимит Telegram на скачивание файлов ботом
MAX_LINK_DURATION = 15 * 60  # ссылки длиннее 15 минут не берём
# с какой секунды пробуем резать звук, если с начала песня не нашлась
OFFSETS = (0, 30, 75)
CLIP_SECONDS = 25
MAX_SONG_DURATION = 10 * 60  # песни длиннее 10 минут не качаем
MAX_SEND_MB = 49  # лимит Телеграма на отправку файла ботом

URL_RE = re.compile(r"https?://\S+")

dp = Dispatcher()
shazam = Shazam()


# ---------- работа со звуком ----------

async def cut_audio(src: Path, dst: Path, start: int) -> bool:
    """Вырезает кусок звука через ffmpeg. Возвращает True, если получился файл."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-ss", str(start), "-i", str(src),
        "-vn", "-t", str(CLIP_SECONDS), "-ac", "1", "-ar", "44100", str(dst),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return dst.exists() and dst.stat().st_size > 10_000


async def recognize_file(src: Path) -> dict | None:
    """Пробует распознать трек в нескольких местах файла."""
    for i, start in enumerate(OFFSETS):
        clip = src.with_name(f"clip_{i}.mp3")
        if not await cut_audio(src, clip, start):
            continue  # файл короче, чем смещение
        try:
            result = await shazam.recognize(str(clip))
        except Exception:
            logging.exception("Shazam упал на куске %s", start)
            continue
        track = result.get("track")
        if track:
            return track
    return None


def download_audio(url: str, folder: Path) -> Path:
    """Скачивает звук по ссылке (блокирующая функция, вызывать через to_thread)."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "link.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration < {MAX_LINK_DURATION}"
        ),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = list(folder.glob("link.*"))
    if not files:
        raise RuntimeError("не получилось скачать")
    return files[0]


# ---------- ответы ----------

def track_text(track: dict) -> str:
    title = track.get("title", "Без названия")
    artist = track.get("subtitle", "неизвестный исполнитель")
    text = f"Нашёл: {artist}, «{title}»"
    if track.get("url"):
        text += f"\n{track['url']}"
    return text


def download_song(query: str, folder: Path) -> Path:
    """Ищет песню на YouTube и скачивает как mp3 (блокирующая, вызывать через to_thread)."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "song.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration < {MAX_SONG_DURATION}"
        ),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"ytsearch1:{query}"])
    files = list(folder.glob("song.mp3"))
    if not files:
        raise RuntimeError("песня не скачалась")
    return files[0]


def preview_url(track: dict) -> str | None:
    """Ссылка на 30-секундный отрывок от Shazam (запасной вариант)."""
    for action in (track.get("hub") or {}).get("actions", []):
        uri = action.get("uri")
        if action.get("type") == "uri" and uri and ".m4a" in uri:
            return uri
    return None


async def send_song(message: Message, track: dict) -> None:
    """Отправляет в чат саму песню: сначала целиком, если не вышло, то отрывок."""
    title = track.get("title", "Без названия")
    artist = track.get("subtitle", "")

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)

        # 1) полная песня
        try:
            path = await asyncio.to_thread(
                download_song, f"{artist} - {title}", folder
            )
            if path.stat().st_size <= MAX_SEND_MB * 1024 * 1024:
                await message.answer_audio(
                    FSInputFile(path), title=title, performer=artist
                )
                return
        except Exception:
            logging.exception("Не вышло скачать полную песню")

        # 2) запасной вариант: отрывок от Shazam
        url = preview_url(track)
        if url:
            try:
                path = folder / "preview.m4a"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        path.write_bytes(await resp.read())
                await message.answer_audio(
                    FSInputFile(path), title=title, performer=artist
                )
                await message.answer(
                    "Полную версию скачать не вышло, прислал короткий отрывок."
                )
                return
            except Exception:
                logging.exception("Не вышло отправить отрывок")

    await message.answer("Саму песню скинуть не получилось, но название выше.")


async def send_track(message: Message, track: dict) -> None:
    cover = (track.get("images") or {}).get("coverart")
    sent = False
    if cover:
        try:
            await message.answer_photo(cover, caption=track_text(track))
            sent = True
        except Exception:
            pass
    if not sent:
        await message.answer(track_text(track))
    await send_song(message, track)


async def handle_media(message: Message, file_id: str, size: int | None, name: str):
    if size and size > MAX_DOWNLOAD_MB * 1024 * 1024:
        await message.answer(
            f"Файл больше {MAX_DOWNLOAD_MB} МБ, Телеграм не даёт мне его скачать. "
            "Обрежьте видео до кусочка с песней и пришлите снова."
        )
        return

    wait = await message.answer("Слушаю…")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / name
        await message.bot.download(file_id, destination=src)
        track = await recognize_file(src)

    await wait.delete()
    if track:
        await send_track(message, track)
    else:
        await message.answer(
            "Не смог узнать. Попробуйте кусок, где музыка слышна лучше "
            "и нет сильного шума или голоса поверх."
        )


# ---------- хендлеры ----------

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Кидайте видео, кружок, голосовое или аудио, и я скажу, что за песня играет.\n\n"
        "Ещё можно прислать ссылку на видео (YouTube, TikTok, Reels) "
        "или написать кусок текста песни."
    )


@dp.message(F.video)
async def on_video(message: Message):
    v = message.video
    await handle_media(message, v.file_id, v.file_size, "video.mp4")


@dp.message(F.video_note)
async def on_video_note(message: Message):
    v = message.video_note
    await handle_media(message, v.file_id, v.file_size, "note.mp4")


@dp.message(F.voice)
async def on_voice(message: Message):
    v = message.voice
    await handle_media(message, v.file_id, v.file_size, "voice.ogg")


@dp.message(F.audio)
async def on_audio(message: Message):
    a = message.audio
    await handle_media(message, a.file_id, a.file_size, "audio.mp3")


@dp.message(F.text)
async def on_text(message: Message):
    text = message.text.strip()
    link = URL_RE.search(text)

    if link:
        wait = await message.answer("Качаю звук по ссылке…")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = await asyncio.to_thread(download_audio, link.group(), Path(tmp))
                track = await recognize_file(src)
        except Exception:
            logging.exception("Ошибка по ссылке")
            await wait.edit_text(
                "Со ссылкой не вышло. Возможно, видео закрытое или слишком длинное."
            )
            return
        await wait.delete()
        if track:
            await send_track(message, track)
        else:
            await message.answer("По этой ссылке песню узнать не получилось.")
        return

    # просто текст: ищем трек по названию или строчке
    try:
        found = await shazam.search_track(query=text, limit=3)
        hits = (found.get("tracks") or {}).get("hits") or []
    except Exception:
        logging.exception("Ошибка поиска")
        hits = []

    if not hits:
        await message.answer("Ничего не нашёл. Попробуйте написать по-другому.")
        return

    top = hits[0].get("track")
    if top:
        await send_track(message, top)
    else:
        await message.answer("Ничего не нашёл. Попробуйте написать по-другому.")


async def start_health_server():
    """Мини-сервер для хостингов типа Render: им нужен открытый порт,
    а внешний пинг на /health не даёт бесплатному сервису заснуть."""
    from aiohttp import web

    async def health(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Задайте токен: export BOT_TOKEN=...")
    logging.basicConfig(level=logging.INFO)
    await start_health_server()
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
