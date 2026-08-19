from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime

class OrderBase(BaseModel):
    wb_order_id: Optional[str] = None
    seller_id: Any
    status: str
    wb_status: Optional[str] = None
    supplier_status: Optional[str] = None
    kiz_status: str
    price_kopecks: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
class OrderResponse(OrderBase):
    id: int
    kiz_code: Optional[str] = None
    kiz_attached_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)

class OrderListItem(OrderBase):
    id: int
    model_config = ConfigDict(from_attributes=True)

class KIZAttachRequest(BaseModel):
    kiz_code: str

class KIZValidationResponse(BaseModel):
    valid: bool
    details: Dict[str, Any]
