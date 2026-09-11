from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional, List
from datetime import datetime

# Common valid IANA timezone examples (non-exhaustive hint for docs)
_TZ_EXAMPLES = "Europe/Moscow, Asia/Yekaterinburg, Asia/Novosibirsk, Europe/Kaliningrad, Asia/Vladivostok"


class DigestSettings(BaseModel):
    """Настройки утреннего дайджеста для продавца."""
    enabled: bool = True
    hour: int = Field(8, ge=0, le=23, description="Час отправки (0–23)")
    minute: int = Field(0, ge=0, le=59, description="Минута отправки (0–59)")
    timezone: str = Field(
        "Europe/Moscow",
        description=f"IANA-часовой пояс. Примеры: {_TZ_EXAMPLES}",
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(v)
        except Exception:
            raise ValueError(
                f"Неверный часовой пояс: '{v}'. "
                f"Используйте IANA-формат, например: {_TZ_EXAMPLES}"
            )
        return v


class SellerBase(BaseModel):
    name: str
    wb_supplier_id: Optional[str] = None
    cz_inn: Optional[str] = None
    cz_oms_id: Optional[str] = None
    cryptopro_cert_thumbprint: Optional[str] = None
    cz_cert_path: Optional[str] = None
    mod_fias: Optional[str] = None
    mod_kpp: Optional[str] = None
    telegram_chat_ids: Optional[List[str]] = None
    notification_mode: Optional[str] = "instant"
    notification_schedule: Optional[List[str]] = Field(default_factory=lambda: ["10:00", "14:00", "18:00"])
    timezone: Optional[str] = "Europe/Moscow"

    @field_validator("notification_mode", mode="before")
    @classmethod
    def default_notification_mode(cls, v):
        return v or "instant"

    @field_validator("timezone", mode="before")
    @classmethod
    def default_tz(cls, v):
        return v or "Europe/Moscow"

    @field_validator("notification_schedule", mode="before")
    @classmethod
    def default_schedule(cls, v):
        if not v:
            return ["10:00", "14:00", "18:00"]
        return v

    @field_validator("notification_mode")
    @classmethod
    def validate_notification_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ("instant", "scheduled"):
            raise ValueError(f"Недопустимый режим уведомлений: '{v}'. Допустимо: 'instant', 'scheduled'")
        return v

    @field_validator("notification_schedule")
    @classmethod
    def validate_schedule(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        import re
        cleaned = []
        for item in v:
            s = str(item).strip()
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", s):
                raise ValueError(f"Неверный формат времени '{item}'. Ожидается ЧЧ:ММ (00:00–23:59)")
            parts = s.split(":")
            cleaned.append(f"{int(parts[0]):02d}:{int(parts[1]):02d}")
        return sorted(list(set(cleaned)))

    @field_validator("timezone")
    @classmethod
    def validate_tz(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(v)
        except Exception:
            raise ValueError(
                f"Неверный часовой пояс: '{v}'. "
                f"Используйте IANA-формат, например: {_TZ_EXAMPLES}"
            )
        return v


class SellerCreate(SellerBase):
    wb_api_token: str
    cz_token: Optional[str] = None
    cz_oms_id: Optional[str] = None
    cryptopro_cert_thumbprint: Optional[str] = None
    cz_cert_path: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    polling_interval_minutes: Optional[int] = Field(
        None, ge=1, le=60,
        description="Интервал опроса WB API в минутах (1–60). По умолчанию: 1 мин.",
    )
    digest: Optional[DigestSettings] = Field(
        None,
        description="Настройки утреннего дайджеста",
    )
    archive_reminder_enabled: Optional[bool] = True
    archive_reminder_days: Optional[int] = Field(2, ge=1, le=30)
    archive_reminder_hour: Optional[int] = Field(14, ge=0, le=23)
    archive_reminder_minute: Optional[int] = Field(0, ge=0, le=59)
    # Auto KIZ queue settings
    auto_kiz_queue_enabled: Optional[bool] = True
    auto_kiz_queue_hour: Optional[int] = Field(17, ge=0, le=23, description="Час формирования очереди КИЗ (0–23)")
    auto_kiz_queue_minute: Optional[int] = Field(0, ge=0, le=59, description="Минута формирования очереди КИЗ (0–59)")
    auto_kiz_auto_sign_server: Optional[bool] = False
    auto_kiz_manager_chat_id: Optional[str] = None


class SellerUpdate(BaseModel):
    name: Optional[str] = None
    wb_api_token: Optional[str] = None
    wb_supplier_id: Optional[str] = None
    cz_inn: Optional[str] = None
    cz_token: Optional[str] = None
    cz_oms_id: Optional[str] = None
    cryptopro_cert_thumbprint: Optional[str] = None
    cz_cert_path: Optional[str] = None
    mod_fias: Optional[str] = None
    mod_kpp: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_ids: Optional[List[str]] = None
    is_active: Optional[bool] = None
    polling_enabled: Optional[bool] = None
    # Human-friendly: minutes (1–60). API converts to seconds before DB write.
    polling_interval_minutes: Optional[int] = Field(
        None, ge=1, le=60,
        description="Интервал опроса WB API в минутах (1–60)",
    )
    # Archive reminder settings
    archive_reminder_enabled: Optional[bool] = None
    archive_reminder_days: Optional[int] = Field(None, ge=1, le=30, description="Интервал напоминания о загрузке архива в днях")
    archive_reminder_hour: Optional[int] = Field(None, ge=0, le=23, description="Час напоминания (0–23)")
    archive_reminder_minute: Optional[int] = Field(None, ge=0, le=59, description="Минута напоминания (0–59)")
    # Auto KIZ queue settings (daily automated batch creation)
    auto_kiz_queue_enabled: Optional[bool] = None
    auto_kiz_queue_hour: Optional[int] = Field(None, ge=0, le=23, description="Час формирования очереди КИЗ (0–23)")
    auto_kiz_queue_minute: Optional[int] = Field(None, ge=0, le=59, description="Минута формирования очереди КИЗ (0–59)")
    auto_kiz_auto_sign_server: Optional[bool] = None
    auto_kiz_manager_chat_id: Optional[str] = None
    # Digest settings (flat for simple PATCH, or nested via digest object)
    digest_enabled: Optional[bool] = None
    digest_hour: Optional[int] = Field(None, ge=0, le=23)
    digest_minute: Optional[int] = Field(None, ge=0, le=59)
    digest_timezone: Optional[str] = None
    # Notification schedule settings
    notification_mode: Optional[str] = None
    notification_schedule: Optional[List[str]] = None
    timezone: Optional[str] = None
    # Nested convenience object — takes priority if provided
    digest: Optional[DigestSettings] = Field(
        None,
        description="Настройки дайджеста (вложенный объект — приоритет над плоскими полями)",
    )

    @field_validator("notification_mode")
    @classmethod
    def validate_notification_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ("instant", "scheduled"):
            raise ValueError(f"Недопустимый режим уведомлений: '{v}'. Допустимо: 'instant', 'scheduled'")
        return v

    @field_validator("notification_schedule")
    @classmethod
    def validate_schedule(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        import re
        cleaned = []
        for item in v:
            s = str(item).strip()
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", s):
                raise ValueError(f"Неверный формат времени '{item}'. Ожидается ЧЧ:ММ (00:00–23:59)")
            parts = s.split(":")
            cleaned.append(f"{int(parts[0]):02d}:{int(parts[1]):02d}")
        return sorted(list(set(cleaned)))

    @field_validator("timezone", "digest_timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(v)
        except Exception:
            raise ValueError(
                f"Неверный часовой пояс: '{v}'. "
                f"Используйте IANA-формат, например: {_TZ_EXAMPLES}"
            )
        return v


class SellerResponse(SellerBase):
    id: str
    is_active: bool
    polling_enabled: bool
    polling_interval_seconds: int = 60
    polling_interval_minutes: int = 1  # computed
    cz_oms_id: Optional[str] = None
    cryptopro_cert_thumbprint: Optional[str] = None
    cz_cert_path: Optional[str] = None
    digest_enabled: bool = True
    digest_hour: int = 8
    digest_minute: int = 0
    digest_timezone: Optional[str] = "Europe/Moscow"
    notification_mode: Optional[str] = "instant"
    notification_schedule: Optional[List[str]] = Field(default_factory=lambda: ["10:00", "14:00", "18:00"])
    timezone: Optional[str] = "Europe/Moscow"
    last_polled_at: Optional[datetime] = None
    archive_reminder_enabled: Optional[bool] = True
    archive_reminder_days: Optional[int] = 2
    archive_reminder_hour: Optional[int] = 14
    archive_reminder_minute: Optional[int] = 0
    last_archive_uploaded_at: Optional[datetime] = None
    # Auto KIZ queue settings
    auto_kiz_queue_enabled: Optional[bool] = True
    auto_kiz_queue_hour: Optional[int] = 17
    auto_kiz_queue_minute: Optional[int] = 0
    auto_kiz_auto_sign_server: Optional[bool] = False
    auto_kiz_manager_chat_id: Optional[str] = None
    last_auto_kiz_queue_at: Optional[datetime] = None
    created_at: datetime
    has_wb_token: bool = False
    has_cz_token: bool = False
    has_telegram_token: bool = False
    cz_token_preview: Optional[str] = None
    wb_token_status: str = "valid"
    wb_token_expires_at: Optional[datetime] = None
    wb_token_days_left: Optional[int] = None
    model_config = ConfigDict(from_attributes=True, coerce_numbers_to_str=True)

    @classmethod
    def model_validate(cls, obj, **kwargs):
        instance = super().model_validate(obj, **kwargs)
        # Compute human-friendly minutes from stored seconds
        instance.polling_interval_minutes = max(1, instance.polling_interval_seconds // 60)
        
        sched = getattr(obj, "notification_schedule", None) if not isinstance(obj, dict) else obj.get("notification_schedule")
        if isinstance(sched, list) and sched:
            instance.notification_schedule = sched
        else:
            instance.notification_schedule = ["10:00", "14:00", "18:00"]

        mode = getattr(obj, "notification_mode", None) if not isinstance(obj, dict) else obj.get("notification_mode")
        instance.notification_mode = mode or "instant"

        tz = (getattr(obj, "timezone", None) or getattr(obj, "digest_timezone", None)) if not isinstance(obj, dict) else (obj.get("timezone") or obj.get("digest_timezone"))
        instance.timezone = tz or "Europe/Moscow"

        wb_enc = obj.get("wb_api_token_encrypted") if isinstance(obj, dict) else getattr(obj, "wb_api_token_encrypted", None)
        cz_enc = obj.get("cz_token_encrypted") if isinstance(obj, dict) else getattr(obj, "cz_token_encrypted", None)
        tg_enc = obj.get("telegram_bot_token_encrypted") if isinstance(obj, dict) else getattr(obj, "telegram_bot_token_encrypted", None)
        
        instance.has_wb_token = bool(wb_enc)
        instance.has_cz_token = bool(cz_enc)
        instance.has_telegram_token = bool(tg_enc)
        
        if wb_enc:
            try:
                from app.services.encryption import decrypt
                from app.services.wb_client import get_wb_token_status
                raw_wb = decrypt(wb_enc)
                st, exp, dl = get_wb_token_status(raw_wb)
                instance.wb_token_status = st
                instance.wb_token_expires_at = exp
                instance.wb_token_days_left = dl
            except Exception:
                pass
        else:
            instance.wb_token_status = "missing"

        if cz_enc:
            try:
                from app.services.encryption import decrypt
                dec = decrypt(cz_enc)
                if dec and len(dec) > 14:
                    instance.cz_token_preview = f"{dec[:8]}...{dec[-6:]}"
                elif dec:
                    instance.cz_token_preview = "активен"
            except Exception:
                instance.cz_token_preview = "сохранен"

        return instance


class SellerListItem(BaseModel):
    id: str
    name: str
    is_active: bool
    polling_enabled: bool
    polling_interval_minutes: int = 1
    digest_enabled: bool = True
    digest_hour: int = 8
    digest_timezone: Optional[str] = "Europe/Moscow"
    notification_mode: Optional[str] = "instant"
    timezone: Optional[str] = "Europe/Moscow"
    last_polled_at: Optional[datetime] = None
    archive_reminder_enabled: Optional[bool] = True
    archive_reminder_days: Optional[int] = 2
    archive_reminder_hour: Optional[int] = 14
    archive_reminder_minute: Optional[int] = 0
    last_archive_uploaded_at: Optional[datetime] = None
    auto_kiz_queue_enabled: Optional[bool] = True
    auto_kiz_queue_hour: Optional[int] = 17
    auto_kiz_queue_minute: Optional[int] = 0
    last_auto_kiz_queue_at: Optional[datetime] = None
    wb_token_status: str = "valid"
    wb_token_expires_at: Optional[datetime] = None
    wb_token_days_left: Optional[int] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True, coerce_numbers_to_str=True)

    @field_validator("notification_mode", mode="before")
    @classmethod
    def default_item_mode(cls, v):
        return v or "instant"

    @field_validator("timezone", mode="before")
    @classmethod
    def default_item_tz(cls, v):
        return v or "Europe/Moscow"

    @classmethod
    def model_validate(cls, obj, **kwargs):
        instance = super().model_validate(obj, **kwargs)
        interval_sec = (obj.get("polling_interval_seconds") if isinstance(obj, dict) else getattr(obj, "polling_interval_seconds", 60)) or 60
        instance.polling_interval_minutes = max(1, interval_sec // 60)
        instance.notification_mode = (obj.get("notification_mode") if isinstance(obj, dict) else getattr(obj, "notification_mode", "instant")) or "instant"
        tz = (obj.get("timezone") or obj.get("digest_timezone")) if isinstance(obj, dict) else (getattr(obj, "timezone", None) or getattr(obj, "digest_timezone", None))
        instance.timezone = tz or "Europe/Moscow"

        wb_enc = obj.get("wb_api_token_encrypted") if isinstance(obj, dict) else getattr(obj, "wb_api_token_encrypted", None)
        if wb_enc:
            try:
                from app.services.encryption import decrypt
                from app.services.wb_client import get_wb_token_status
                raw_wb = decrypt(wb_enc)
                st, exp, dl = get_wb_token_status(raw_wb)
                instance.wb_token_status = st
                instance.wb_token_expires_at = exp
                instance.wb_token_days_left = dl
            except Exception:
                pass
        else:
            instance.wb_token_status = "missing"

        return instance
