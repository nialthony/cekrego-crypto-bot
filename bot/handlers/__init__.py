# bot/handlers/__init__.py
from aiogram import Router

from .admin_handlers import router as admin_router
from .utility_handlers import router as utility_router
from .market_handlers import router as market_router
from .dex_handlers import router as dex_router
from .news_handlers import router as news_router
from .ai_handlers import router as ai_router

router = Router()

# Registrasi router secara terstruktur:
router.include_router(admin_router)
router.include_router(utility_router)
router.include_router(market_router)
router.include_router(dex_router)
router.include_router(news_router)
router.include_router(ai_router)
