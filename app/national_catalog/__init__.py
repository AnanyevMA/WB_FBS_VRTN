"""
Национальный Каталог (НКТ) Честного Знака — модуль управления карточками товаров.
"""
from app.national_catalog.models import ProductCard

__all__ = ["ProductCard", "nk_router"]


def __getattr__(name: str):
    # app.models imports this package to register ProductCard. Loading the router eagerly
    # would pull FastAPI and all request schemas into the Celery worker and the bot.
    if name == "nk_router":
        from app.national_catalog.router import router
        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
