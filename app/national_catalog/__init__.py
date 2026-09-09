"""
Национальный Каталог (НКТ) Честного Знака — модуль управления карточками товаров.
"""
from app.national_catalog.models import ProductCard
from app.national_catalog.router import router as nk_router

__all__ = ["ProductCard", "nk_router"]
