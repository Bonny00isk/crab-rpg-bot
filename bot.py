# -*- coding: utf-8 -*-
import asyncio
import hmac
import logging
import os
import secrets

import database
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.types import Update, MenuButtonWebApp, WebAppInfo
from aiohttp import web
from redis.asyncio import Redis

from config import BOT_TOKEN, REDIS_DATA_TTL, REDIS_STATE_TTL, REDIS_URL
from database import init_db
from handlers import start, menu, hunt, mutations, profile, dig, inventory, misc, admin, shop, craft
from handlers.middlewares import EnsureUserMiddleware, SyncBufferMiddleware
from events_scheduler import events_scheduler_loop
from afk_saver import afk_saver_loop

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("crab-rpg-bot")

PROXY_URL = os.getenv("PROXY_URL")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3000"))
WEBHOOK_PATH = "/telegram/webhook"


async def create_fsm_storage():
    if not REDIS_URL:
        logger.info("REDIS_URL is not set; using MemoryStorage and direct PostgreSQL saves.")
        return MemoryStorage()
    try:
        redis = Redis.from_url(REDIS_URL, decode_responses=False)
        await redis.ping()
        logger.info("Redis connected; using RedisStorage.")
        return RedisStorage(
            redis=redis,
            key_builder=DefaultKeyBuilder(with_bot_id=True),
            state_ttl=REDIS_STATE_TTL,
            data_ttl=REDIS_DATA_TTL,
        )
    except Exception:
        logger.exception("Redis unavailable; falling back to MemoryStorage.")
        return MemoryStorage()


async def start_health_server(bot, dp, webhook_url=None):
    app = web.Application()
    from webapp_api import setup_webapp
    setup_webapp(app, bot)

    async def health(_request):
        return web.json_response({"ok": True, "service": "crab-rpg-bot", "mode": "webhook" if webhook_url else "polling"})

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    if webhook_url:
        webhook_secret = os.getenv("WEBHOOK_SECRET") or secrets.token_urlsafe(32)

        async def telegram_webhook(request):
            supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(supplied, webhook_secret):
                raise web.HTTPUnauthorized(text="invalid webhook secret")
            try:
                payload = await request.json()
                update = Update.model_validate(payload, context={"bot": bot})
                await dp.feed_update(bot, update)
            except (ValueError, TypeError):
                logger.exception("Invalid Telegram webhook update")
                raise web.HTTPBadRequest(text="invalid update")
            return web.Response(text="ok")

        app.router.add_post(WEBHOOK_PATH, telegram_webhook)
        await bot.set_webhook(
            url=f"{webhook_url.rstrip('/')}{WEBHOOK_PATH}",
            secret_token=webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        logger.info("Telegram webhook configured: %s%s", webhook_url.rstrip("/"), WEBHOOK_PATH)
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("No WEBHOOK_URL; starting local polling mode.")

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    logger.info("HTTP server listening on %s:%s", HOST, PORT)
    return runner


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN is missing. Set it in the host environment.")
    init_db()
    await database.auto_update_db_nectars()

    storage = await create_fsm_storage()
    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)
    dp = Dispatcher(storage=storage)

    dp.message.middleware(EnsureUserMiddleware())
    dp.callback_query.middleware(EnsureUserMiddleware())
    dp.message.middleware(SyncBufferMiddleware())
    dp.callback_query.middleware(SyncBufferMiddleware())

    for router in (admin.router, start.router, menu.router, hunt.router, mutations.router,
                   profile.router, shop.router, dig.router, inventory.router, craft.router, misc.router):
        dp.include_router(router)

    webhook_url = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    app_url = (os.getenv("WEBAPP_URL") or (webhook_url.rstrip("/") + "/app" if webhook_url else "")).strip()
    if app_url:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🦀 Играть", web_app=WebAppInfo(url=app_url)))
            logger.info("Telegram Mini App menu button configured: %s", app_url)
        except Exception:
            logger.exception("Could not configure Mini App menu button")
    health_runner = None
    tasks = []
    try:
        health_runner = await start_health_server(bot, dp, webhook_url)
        tasks.append(asyncio.create_task(events_scheduler_loop(bot)))
        if hasattr(storage, "redis"):
            tasks.append(asyncio.create_task(afk_saver_loop(storage.redis)))
        if webhook_url:
            await asyncio.Event().wait()
        else:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if health_runner:
            await health_runner.cleanup()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
