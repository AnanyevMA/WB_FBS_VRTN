from datetime import datetime
from typing import List, Optional, Any
from pydantic import BaseModel, ConfigDict, Field


class ProductAttributeItem(BaseModel):
    attr_id: int
    attr_name: Optional[str] = None
    attr_value: str
    attr_value_type: Optional[str] = None
    attr_value_id: Optional[int] = None
    delete: Optional[int] = None


class ProductImageItem(BaseModel):
    photo_type: str = "default"
    photo_url: str
    photo_date: Optional[str] = None


class ProductCardCreateRequest(BaseModel):
    gtin: Optional[str] = Field(None, description="Код товара (14 цифр) или пусто при технической карточке")
    name: str = Field(..., min_length=2, max_length=255, description="Полное наименование товара")
    brand: Optional[str] = Field(None, description="Торговая марка / товарный знак")
    tnved: Optional[str] = Field(None, description="Код ТН ВЭД (4 или 10 знаков)")
    category_id: Optional[int] = Field(None, description="ID категории в Национальном каталоге")
    category_name: Optional[str] = Field(None, description="Название категории")
    is_tech_gtin: bool = Field(False, description="Признак создания технической карточки (диапазон 029)")
    is_set: bool = Field(False, description="Признак набора / комплекта")
    moderation: bool = Field(True, description="Сразу отправить карточку на модерацию в ЧЗ")
    attributes: List[ProductAttributeItem] = Field(default_factory=list)
    images: List[ProductImageItem] = Field(default_factory=list)


class ProductCardUpdateRequest(BaseModel):
    name: Optional[str] = None
    brand: Optional[str] = None
    tnved: Optional[str] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    moderation: bool = True
    attributes: Optional[List[ProductAttributeItem]] = None
    images: Optional[List[ProductImageItem]] = None


class ProductCardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    seller_id: str
    good_id: Optional[int] = None
    gtin: Optional[str] = None
    name: str
    brand: Optional[str] = None
    tnved: Optional[str] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    status: str
    good_mark_flag: bool
    good_turn_flag: bool
    is_tech_gtin: bool
    is_set: bool
    feed_id: Optional[int] = None
    feed_status: Optional[str] = None
    attributes: List[Any] = Field(default_factory=list)
    images: List[Any] = Field(default_factory=list)
    error_details: Optional[Any] = None
    created_at: datetime
    updated_at: datetime


class PrepareSignResponse(BaseModel):
    good_id: int
    gtin: Optional[str] = None
    raw_xml: str
    base64_xml: str


class SubmitSignRequest(BaseModel):
    signature: str = Field(..., description="Открепленная CMS/PKCS#7 подпись в Base64")


class SyncNKResponse(BaseModel):
    success: bool = True
    total_remote: int
    synced_count: int
    created_count: int
    updated_count: int
    message: str
