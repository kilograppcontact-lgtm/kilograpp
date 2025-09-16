# meal_reminders.py
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app

from extensions import db
from models import User, UserSettings, MealReminderLog

# Фиксированные времена (локальное время пользователя)
MEAL_SCHEDULE = {
    "breakfast": ("🍳 Завтрак", "08:00"),
    "lunch":     ("🍲 Обед",    "12:00"),
    "dinner":    ("🍝 Ужин",    "18:00"),
}

_scheduler = None


def _send_telegram_message(token: str, chat_id: int | str, text: str, reply_markup: dict | None = None) -> bool:
    """Отправка сообщения в Telegram; поддерживает inline-кнопки через reply_markup."""
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10,
        )
        return resp.ok
    except Exception:
        return False


def _should_send_for_user(user, now_local: datetime, meal_key: str) -> bool:
    """Проверяем: включены ли уведомления, время совпало и сегодня ещё не отправляли этот тип."""
    s: UserSettings | None = getattr(user, "settings", None)
    if not s:
        return False
    if not s.telegram_notify_enabled or not s.notify_meals:
        return False
    if not user.telegram_chat_id:
        return False

    # Совпадает ли HH:MM с расписанием
    _, hhmm = MEAL_SCHEDULE[meal_key]
    if now_local.strftime("%H:%M") != hhmm:
        return False

    # Уже отправляли сегодня?
    exists = MealReminderLog.query.filter_by(
        user_id=user.id, meal_type=meal_key, date_sent=now_local.date()
    ).first()

    return exists is None


def _message_for(meal_key: str) -> str:
    title, _ = MEAL_SCHEDULE[meal_key]
    return (
        f"{title}: самое время добавить приём пищи!\n"
        f"Нажмите кнопку ниже, чтобы зафиксировать его."
    )


def _keyboard_for(meal_key: str, base_url: str | None) -> dict:
    """Inline-кнопки: 1) добавить конкретный приём через бота, 2) открыть дневник на сайте (если есть URL)."""
    # красивые подписи
    meal_names = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин"}
    add_btn_text = f"➕ Добавить {meal_names.get(meal_key, 'приём')}"

    rows = [
        [{"text": add_btn_text, "callback_data": f"meal_{meal_key}"}],
    ]
    if base_url:
        rows.append([{"text": "Открыть дневник /meals", "url": f"{base_url}/meals"}])

    return {"inline_keyboard": rows}


def _tick():
    app = current_app._get_current_object()
    token = (
        app.config.get("TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_TOKEN")
    )
    if not token:
        return

    # Базовый URL для кнопки перехода
    base_url = (app.config.get("PUBLIC_BASE_URL") or "").rstrip("/") or None

    # Берём всех пользователей, у кого потенциально могут быть включены уведомления
    users = (
        User.query
        .join(UserSettings, UserSettings.user_id == User.id)
        .filter(
            User.telegram_chat_id.isnot(None),
            UserSettings.telegram_notify_enabled.is_(True),
            UserSettings.notify_meals.is_(True),
        ).all()
    )

    for user in users:
        tz = ZoneInfo("Asia/Almaty")
        now_local = datetime.now(tz)

        for meal_key in ("breakfast", "lunch", "dinner"):
            if _should_send_for_user(user, now_local, meal_key):
                text = _message_for(meal_key)
                markup = _keyboard_for(meal_key, base_url)
                ok = _send_telegram_message(token, user.telegram_chat_id, text, reply_markup=markup)
                if ok:
                    db.session.add(MealReminderLog(
                        user_id=user.id, meal_type=meal_key, date_sent=now_local.date()
                    ))

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def start_meal_scheduler(app):
    global _scheduler
    if _scheduler:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Almaty")

    def _job():
        # даём Flask-контекст внутри джобы
        with app.app_context():
            _tick()

    # Проверяем раз в минуту локальное HH:MM Алматы
    _scheduler.add_job(_job, "interval", minutes=1, id="meal-reminders")
    _scheduler.start()
    return _scheduler
