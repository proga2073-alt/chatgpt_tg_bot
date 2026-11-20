"""Настройки приложения. Всё читается из переменных окружения / .env — секретов в коде нет."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- обязательные ---
    openai_api_key: str
    bot_token: str

    # --- API провайдера ---
    openai_base_url: str = "https://zenvy.su/v1"
    openai_model: str = "gpt-5.6-terra"
    request_timeout: float = 90.0

    # --- хранилище / логи ---
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    log_level: str = "INFO"

    # --- администраторы: Telegram ID через запятую ---
    admin_ids: str = ""

    # --- контекст диалога (скользящее окно) ---
    history_max_messages: int = 20
    history_max_chars: int = 12000  # ≈ 3000 токенов

    # --- лимиты ---
    rate_limit_per_minute: int = 10
    daily_limit: int = 0  # 0 = выключен
    stream_edit_interval: float = 1.0  # как часто правим сообщение во время стрима, сек

    # --- платные модели (Telegram Stars) ---
    free_models: str = "gpt-5.6-terra"  # через запятую; все остальные модели платные
    welcome_credits: int = 0  # стартовый бонус новым пользователям, в кредитах (10 кредитов = 1 ⭐)
    credits_per_star: int = 10
    message_base_credits: int = 10  # цена сообщения на модели 1x
    default_multiplier: float = 1.0
    model_multipliers: dict[str, float] = {
        "gpt-6.2-astra": 2.0,
        "gpt-6.2-sol": 1.8,
        "gpt-6-astra": 2.0,
        "gpt-6.1-sol": 1.4,
        "gpt-6.1-terra": 0.9,
        "gpt-6-sol": 1.0,
        "gpt-6-luna": 0.8,
        "gpt-5.6-sol": 0.7,
        "gpt-5.6-luna": 0.6,
        "gpt-5.6-terra": 0.5,
    }
    star_packs: str = "50,100,250,500"
    support_contact: str = ""

    # --- инструменты модели: изображения, файлы, команды ---
    tools_enabled: bool = True  # function calling; если провайдер не поддерживает — бот работает без инструментов
    max_tool_steps: int = 5  # сколько раз модель может вызвать инструменты за одно сообщение
    image_model: str = "gpt-image-1"  # модель для /images/generations и /images/edits
    image_cost_credits: int = 30  # цена одной картинки (30 кредитов = 3 ⭐); админы бесплатно
    workspace_dir: str = "data/workspace"  # у каждого пользователя своя папка внутри
    max_upload_mb: int = 10
    enable_shell: bool = False  # выполнение команд cmd: только админам и только с подтверждением кнопкой
    shell_timeout: int = 60

    default_system_prompt: str = (
        "Ты — полезный ассистент. Отвечай по делу, на языке пользователя. "
        "Для кода используй блоки Markdown."
    )

    @property
    def free_model_set(self) -> set[str]:
        return {m.strip() for m in self.free_models.split(",") if m.strip()}

    @property
    def star_pack_list(self) -> list[int]:
        return [int(x) for x in self.star_packs.split(",") if x.strip().isdigit() and int(x) > 0]

    @property
    def admin_set(self) -> set[int]:
        return {int(x) for x in self.admin_ids.split(",") if x.strip().isdigit()}


settings = Settings()  # type: ignore[call-arg]
