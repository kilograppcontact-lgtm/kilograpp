from datetime import datetime, date, timedelta, time as dt_time, UTC
from urllib.parse import urlparse
import base64
from zoneinfo import ZoneInfo
from flask import Flask, render_template, redirect, session, url_for, flash, abort
from flask_bcrypt import Bcrypt
from werkzeug.utils import secure_filename
from openai import OpenAI
from dotenv import load_dotenv
import random
import string
import re
from sqlalchemy import func
from functools import wraps
from PIL import Image
from meal_reminders import start_meal_scheduler, get_scheduler, run_tick_now, pause_job, resume_job
from diet_autogen import start_diet_autogen_scheduler
from gemini_visualizer import generate_for_user, create_record, _compute_pct
from assistant_bp import assistant_bp
from flask import Blueprint, request
from flask_login import current_user
from shopping_bp import shopping_bp
import json, os
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask import make_response
from sqlalchemy import inspect
import uuid
from models import BodyVisualization
from flask import send_file
from io import BytesIO
from progress_analyzer import generate_progress_commentary

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecret")
app.jinja_env.globals.update(getattr=getattr)

# Config DB — задаём ДО init_app
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///35healthclubs.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

from extensions import db
db.init_app(app)

from models import (
    User, Subscription, Order, Group, GroupMember, GroupMessage, MessageReaction,
    GroupTask, MealLog, Activity, Diet, Training, TrainingSignup, BodyAnalysis,
    UserSettings, MealReminderLog, AuditLog, PromptTemplate, UploadedFile)



# --- Image Resizing Configuration ---
CHAT_IMAGE_MAX_SIZE = (200, 200)  # Max width and height for chat images

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def resize_image(filepath, max_size):
    """Resizes an image and saves it back to the same path."""
    try:
        with Image.open(filepath) as img:
            print(f"DEBUG: Resizing image: {filepath}, original size: {img.size}")
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            img.save(filepath)  # Overwrites the original
            print(f"DEBUG: Image resized to: {img.size}")
    except Exception as e:
        print(f"ERROR: Failed to resize image {filepath}: {e}")



ADMIN_EMAIL = "admin@healthclub.local"

def _magic_serializer():
    # соль зафиксирована, чтобы токены были совместимы между рестартами
    secret = app.secret_key or app.config.get("SECRET_KEY")
    return URLSafeTimedSerializer(secret, salt="magic-login")


def log_audit(action: str, entity: str, entity_id: str, old=None, new=None):
    try:
        entry = AuditLog(
            actor_id=session.get('user_id'),
            action=action,
            entity=entity,
            entity_id=str(entity_id),
            old_data=old,
            new_data=new,
            ip=request.headers.get('X-Forwarded-For') or request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        return f(*args, **kwargs)

    return decorated_function


def get_current_user():
    user_id = session.get('user_id')
    if user_id:
        return db.session.get(User, user_id)
    return None


def is_admin():
    user = get_current_user()
    return user and user.email == ADMIN_EMAIL


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.url))
        if not is_admin():
            abort(403)  # Forbidden
        return f(*args, **kwargs)

    return decorated_function


# --- MAGIC LOGIN (вход по ссылке, 1 час) ---
if "magic_login" not in app.view_functions:
    @app.get("/auth/magic/<token>", endpoint="magic_login")
    def magic_login(token):
        s = _magic_serializer()
        try:
            user_id = int(s.loads(token, max_age=3600))
        except SignatureExpired:
            flash("Ссылка истекла. Сгенерируйте новую.", "error")
            return redirect(url_for("login"))
        except BadSignature:
            flash("Ссылка недействительна.", "error")
            return redirect(url_for("login"))
        user = db.session.get(User, user_id) or abort(404)
        session["user_id"] = user.id
        flash("Вы вошли через магическую ссылку.", "success")
        return redirect(url_for("profile"))



client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
bcrypt = Bcrypt(app)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# важно: чтобы meal_reminders видел токен/базовый урл
app.config["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
app.config["PUBLIC_BASE_URL"]    = os.getenv("APP_BASE_URL", "").rstrip("/")


import os, threading, time as time_mod, requests

def _dt(date_obj, time_obj):
    return datetime.combine(date_obj, time_obj)

def _send_telegram(chat_id: str, text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        return r.ok
    except Exception:
        return False

@app.before_request
def set_tz():
    if db.engine.url.get_backend_name() == "postgresql":
        with db.engine.connect() as con:
            con.exec_driver_sql("SET TIME ZONE 'Asia/Almaty'")

@app.route('/api/activity/today/<int:chat_id>')
def activity_today(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    if not user:
        return jsonify({"error": "not found"}), 404
    a = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
    if not a:
        return jsonify({"present": False})
    return jsonify({"present": True, "steps": a.steps or 0, "active_kcal": a.active_kcal or 0})

_notifier_started = False
def _notification_worker():
    # ВАЖНО: весь цикл работает внутри контекста приложения
    with app.app_context():
        while True:
            try:
                now = datetime.now()
                now_d = now.date()
                target = now + timedelta(hours=1)

                # 1) Напоминания за 1 час (как было)
                trainings = Training.query.filter(
                    Training.date == target.date(),
                    func.extract('hour', Training.start_time) == target.hour,
                    func.extract('minute', Training.start_time) == target.minute
                ).all()

                for t in trainings:
                    rows = TrainingSignup.query.filter_by(training_id=t.id, notified_1h=False).all()
                    for s in rows:
                        u = db.session.get(User, s.user_id)
                        if (not u or not getattr(u, "telegram_chat_id", None)
                                or not getattr(u, "telegram_notify_enabled", True)
                                or not getattr(u, "notify_trainings", True)):
                            s.notified_1h = True
                            continue

                        when = t.start_time.strftime("%H:%M")
                        date_s = t.date.strftime("%d.%m.%Y")
                        text = (
                            f"⏰ Напоминание!\n"
                            f"Через 1 час — онлайн-тренировка «{t.title or 'Онлайн-тренировка'}» с "
                            f"{(t.trainer.name if t.trainer and getattr(t.trainer, 'name', None) else 'тренером')}\n"
                            f"📅 {date_s}  •  🕒 {when}\n"
                            f"🔗 Ссылка появится за 10 минут до начала в вашем расписании.\n"
                            f"🆔 ID занятия: {t.id}"
                        )
                        if _send_telegram(u.telegram_chat_id, text):
                            s.notified_1h = True

                startings = Training.query.filter(
                    Training.date == now.date(),
                    func.extract('hour', Training.start_time) == now.hour,
                    func.extract('minute', Training.start_time) == now.minute
                ).all()

                for t in startings:
                    rows = TrainingSignup.query.filter_by(training_id=t.id).all()
                    for s in rows:
                        # пропускаем, если уже отмечали старт
                        if getattr(s, "notified_start", False):
                            continue
                        u = db.session.get(User, s.user_id)
                        # даже если у пользователя нет чата — помечаем, чтобы не долбить каждую минуту
                        if (not u or not getattr(u, "telegram_chat_id", None)
                                or not getattr(u, "telegram_notify_enabled", True)
                                or not getattr(u, "notify_trainings", True)):
                            s.notified_start = True
                            continue

                        when = t.start_time.strftime("%H:%M")
                        date_s = t.date.strftime("%d.%m.%Y")
                        text = (
                            f"🏁 Старт!\n"
                            f"Тренировка «{t.title or 'Онлайн-тренировка'}» началась.\n"
                            f"👤 Тренер: {(t.trainer.name if t.trainer and getattr(t.trainer, 'name', None) else 'тренер')}\n"
                            f"📅 {date_s}  •  🕒 {when}\n"
                            f"🔗 Присоединиться: {t.meeting_link}\n"
                            f"🆔 ID занятия: {t.id}"
                        )
                        if _send_telegram(u.telegram_chat_id, text):
                            s.notified_start = True

                users = User.query.all()
                for u in users:
                    sub = getattr(u, "subscription", None)
                    if not sub or sub.status != 'active' or not sub.end_date:
                        continue
                    days_left = (sub.end_date - now_d).days
                    if days_left == 5 and not u.renewal_telegram_sent and getattr(u, "telegram_chat_id", None):
                        try:
                            # ссылка на продление
                            base = os.getenv("APP_BASE_URL", "").rstrip("/")
                            purchase_path = url_for("purchase_page") if app and app.app_context else "/purchase"
                            link = f"{base}{purchase_path}" if base else purchase_path
                            # текст
                            txt = (
                                "⏳ Подписка заканчивается через 5 дней.\n"
                                "Не теряйте доступ к тренировкам и ИИ-диетам — продлите сейчас.\n"
                                f"👉 {link}"
                            )
                            if _send_telegram(u.telegram_chat_id, txt):
                                u.renewal_telegram_sent = True
                        except Exception:
                            pass

                if now.minute == 0 and now.hour == 10:
                    two_weeks_ago = now_d - timedelta(days=14)
                    users_to_remind = User.query.filter(User.telegram_chat_id.isnot(None)).all()

                    for u in users_to_remind:
                        # Проверяем настройки уведомлений пользователя
                        settings = get_effective_user_settings(u)
                        if not settings.telegram_notify_enabled:
                            continue

                        # Найти последний замер пользователя
                        latest_analysis = BodyAnalysis.query.filter_by(user_id=u.id).order_by(
                            BodyAnalysis.timestamp.desc()).first()

                        if latest_analysis:
                            # Проверяем, прошло ли 14 дней с последнего замера
                            if latest_analysis.timestamp.date() <= two_weeks_ago:
                                # Проверяем, не отправляли ли мы уже напоминание в последние 13 дней
                                if u.last_measurement_reminder_sent_at is None or \
                                        (now - u.last_measurement_reminder_sent_at).days >= 14:

                                    reminder_text = (
                                        f"Привет, {u.name}! 👋 Прошло 2 недели с вашего последнего замера. "
                                        "Пора обновить данные и посмотреть на свой прогресс! "
                                        "Загрузите новое фото анализа тела в приложении, чтобы не терять динамику. "
                                        "У вас все получится! 💪"
                                    )

                                    if _send_telegram(u.telegram_chat_id, reminder_text):
                                        u.last_measurement_reminder_sent_at = now
                                        db.session.commit()

                db.session.commit()
            except Exception:
                db.session.rollback()
            finally:
                db.session.remove()
                time_mod.sleep(60)

def create_app():
    app = Flask(__name__)

    with app.app_context():
        # Автозапуск напоминалок по приёмам пищи
        start_meal_scheduler(app)
        # Автогенерация диет: 05:00 — GPT генерация почанково, 06:00 — промоут + уведомления
        start_diet_autogen_scheduler(app)

    return app

def get_effective_user_settings(u):
    from models import UserSettings, db
    s = getattr(u, "settings", None)
    if s is None:
        # создаём и сразу наполняем значениями из User (если там уже выставлено)
        s = UserSettings(
            user_id=u.id,
            telegram_notify_enabled=bool(getattr(u, "telegram_notify_enabled", False)),
            notify_trainings=bool(getattr(u, "notify_trainings", False)),
            notify_subscription=bool(getattr(u, "notify_subscription", False)),
            notify_meals=bool(getattr(u, "notify_meals", False)),
            meal_timezone="Asia/Almaty",  # ← дефолт

        )
        db.session.add(s)
        db.session.commit()
    return s
def start_training_notifier():
    global _notifier_started
    if _notifier_started:
        return
    _notifier_started = True
    if os.getenv("ENABLE_TRAINING_NOTIFIER", "1") == "1":
        th = threading.Thread(target=_notification_worker, daemon=True)
        th.start()

def _ensure_column(table, column, ddl):
    # инспектору передаём «сырое» имя (без кавычек), он сам разберётся
    insp = inspect(db.engine)
    cols = [c['name'] for c in insp.get_columns(table)]
    if column not in cols:
        # но в самом SQL-выражении имена нужно корректно квотировать под конкретный диалект
        preparer = db.engine.dialect.identifier_preparer
        table_q = preparer.quote(table)     # например -> "user"
        column_q = preparer.quote(column)   # например -> "sex"
        with db.engine.connect() as con:
            con.execute(text(f'ALTER TABLE {table_q} ADD COLUMN {column_q} {ddl}'))


def _auto_migrate_diet_schema():
    insp = inspect(db.engine)
    # Создадим недостающие таблицы по моделям
    db.create_all()

    # === Новые поля пользователя для визуализаций ===
    _ensure_column("user", "sex", "TEXT DEFAULT 'male'")
    _ensure_column("user", "face_consent", "BOOLEAN DEFAULT FALSE")

    # === ВАЖНО: meal_logs нужные поля ===
    _ensure_column("meal_logs", "image_path", "TEXT")
    _ensure_column("meal_logs", "is_flagged", "BOOLEAN DEFAULT FALSE")
    _ensure_column("meal_logs", "created_at", "TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP)")

    # (опционально) Заполнить created_at там где NULL
    try:
        with db.engine.connect() as con:
            con.execute(text("UPDATE meal_logs SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
    except Exception as e:
        print(f"[auto-migrate] backfill created_at failed: {e}")

with app.app_context():
    # Мини-миграции для новых полей в user
    _auto_migrate_diet_schema()

    # Запускаем фоновые задачи ТОЛЬКО после инициализации БД
    try:
        start_meal_scheduler(app)
    except Exception:
        pass

    # Запускаем автогенерацию диет один раз (не в мастер-процессе reloader’a)
    import os as _os
    if _os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        try:
            start_diet_autogen_scheduler(app)
            print("[diet_autogen] scheduler started")
        except Exception as e:
            print(f"[diet_autogen] scheduler error: {e}")

    start_training_notifier()



def calculate_age(born):
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


# ------------------ TRAININGS API ------------------

def _parse_date_yyyy_mm_dd(s: str) -> date:
    try:
        y, m, d = map(int, s.split('-'))
        return date(y, m, d)
    except Exception:
        abort(400, description="Некорректная дата (ожидается YYYY-MM-DD)")

def _parse_hh_mm(s: str):
    try:
        hh, mm = map(int, s.split(':'))
        return dt_time(hh, mm)
    except Exception:
        abort(400, description="Некорректное время (ожидается HH:MM)")

def _validate_meeting_link(url: str):
    url = (url or "").strip()
    try:
        u = urlparse(url)
        if u.scheme in ("http", "https") and u.netloc:
            return url
    except Exception:
        pass
    abort(400, description="Некорректная ссылка на занятие (ожидается http/https)")

def _month_bounds(yyyy_mm: str):
    try:
        y, m = map(int, yyyy_mm.split('-'))
        start = date(y, m, 1)
    except Exception:
        abort(400, description="Некорректный параметр month (ожидается YYYY-MM)")
    if m == 12:
        next_month = date(y+1, 1, 1)
    else:
        next_month = date(y, m+1, 1)
    end = next_month - timedelta(days=1)
    return start, end


@app.route('/trainings')
def trainings_page():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    u = get_current_user()
    return render_template('trainings.html', is_trainer=bool(u and u.is_trainer), me_id=(u.id if u else None))

@app.route('/api/trainings', methods=['GET'])
def list_trainings():
    if not session.get('user_id'):
        abort(401)
    month = request.args.get('month')
    if not month:
        today = date.today()
        month = f"{today.year:04d}-{today.month:02d}"
    start, end = _month_bounds(month)
    me = get_current_user()
    me_id = me.id if me else None

    items = Training.query.filter(Training.date >= start, Training.date <= end)\
                          .order_by(Training.date, Training.start_time).all()
    return jsonify({"ok": True, "data": [t.to_dict(me_id) for t in items]})

@app.route('/api/trainings/mine', methods=['GET'])
def my_trainings():
    u = get_current_user()
    if not u:
        abort(401)
    if not u.is_trainer:
        abort(403)
    items = Training.query.filter_by(trainer_id=u.id)\
                          .order_by(Training.date.desc(), Training.start_time).all()
    return jsonify({"ok": True, "data": [t.to_dict(u.id) for t in items]})

@app.route('/api/trainings', methods=['POST'])
def create_training():
    u = get_current_user()
    if not u:
        abort(401)
    if not u.is_trainer:
        abort(403, description="Доступ только для тренеров")

    data = request.get_json(force=True, silent=True) or {}

    dt = _parse_date_yyyy_mm_dd(data.get('date') or '')
    st = _parse_hh_mm(data.get('start_time') or '')
    et = _parse_hh_mm(data.get('end_time') or '')
    if et <= st:
        abort(400, description="Время окончания должно быть позже начала")

    meeting_link = _validate_meeting_link(data.get('meeting_link') or '')

    # Глобальная защита: в этот слот уже есть ЛЮБАЯ тренировка
    exists = Training.query.filter(Training.date == dt, Training.start_time == st).first()
    if exists:
        abort(409, description="На это время уже есть тренировка")

    t = Training(
        trainer_id=u.id,
        meeting_link=meeting_link,
        # опциональные поля (для совместимости)
        title=(data.get('title') or 'Онлайн-тренировка').strip() or "Онлайн-тренировка",
        description=data.get('description') or '',
        date=dt,
        start_time=st,
        end_time=et,
        location=(data.get('location') or '').strip(),
        capacity=int(data.get('capacity') or 10),
        is_public=bool(data.get('is_public')) if data.get('is_public') is not None else True
    )
    db.session.add(t)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # страхуемся на случай гонок по trainer_id uniq
        abort(409, description="На это время уже есть тренировка")

    return jsonify({"ok": True, "data": t.to_dict(u.id)})

@app.route('/api/trainings/<int:tid>', methods=['PUT'])
def update_training(tid):
    u = get_current_user()
    if not u:
        abort(401)
    t = Training.query.get_or_404(tid)
    if t.trainer_id != u.id:
        abort(403)

    data = request.get_json(force=True, silent=True) or {}

    if 'meeting_link' in data:
        t.meeting_link = _validate_meeting_link(data.get('meeting_link') or '')

    if 'date' in data:
        t.date = _parse_date_yyyy_mm_dd(data.get('date') or '')
    if 'start_time' in data:
        t.start_time = _parse_hh_mm(data.get('start_time') or '')
    if 'end_time' in data:
        t.end_time = _parse_hh_mm(data.get('end_time') or '')
    if t.end_time <= t.start_time:
        abort(400, description="Время окончания должно быть позже начала")

    # опциональные поля — оставляем совместимость
    if 'title' in data:
        title = (data.get('title') or '').strip()
        t.title = title or "Онлайн-тренировка"
    if 'description' in data:
        t.description = data.get('description') or ''
    if 'location' in data:
        t.location = (data.get('location') or '').strip()
    if 'capacity' in data:
        try:
            t.capacity = int(data.get('capacity') or 10)
        except Exception:
            abort(400, description="Некорректная вместимость")
    if 'is_public' in data:
        t.is_public = bool(data.get('is_public'))

    # Глобальная защита: проверяем конфликт по дата+старт (кроме самой записи)
    conflict = Training.query.filter(
        Training.id != t.id,
        Training.date == t.date,
        Training.start_time == t.start_time
    ).first()
    if conflict:
        abort(409, description="На это время уже есть тренировка")

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, description="На это время уже есть тренировка")

    return jsonify({"ok": True, "data": t.to_dict(u.id)})

@app.route('/api/trainings/<int:tid>', methods=['DELETE'])
def delete_training(tid):
    u = get_current_user()
    if not u:
        abort(401)
    t = Training.query.get_or_404(tid)
    if t.trainer_id != u.id:
        abort(403)
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})

# ------------------ UTILS ------------------
@app.context_processor
def inject_flags():
    u = get_current_user()
    return dict(is_trainer_user=bool(u and u.is_trainer))

@app.context_processor
def utility_processor():
    def get_bmi_category(bmi):
        if bmi is None:
            return ""
        if bmi < 18.5:
            return "Недостаточный вес"
        elif bmi < 25:
            return "Норма"
        elif bmi < 30:
            return "Избыточный вес"
        else:
            return "Ожирение"

    return dict(
        get_bmi_category=get_bmi_category,
        calculate_age=calculate_age,  # <-- теперь в шаблоне доступна
        today=date.today(),  # <-- и переменная today
    )


@app.context_processor
def inject_user():
    return {'current_user': get_current_user()}

# NEW: глобальные флаги для помощи новичкам и наличия анализа тела
@app.context_processor
def inject_help_flags():
    u = get_current_user()

    # Есть ли уже анализы тела
    has_body_analysis = False
    if u:
        try:
            has_body_analysis = db.session.query(BodyAnalysis.id).filter_by(user_id=u.id).first() is not None
        except Exception:
            has_body_analysis = False

    # Новичок: либо нет анализов, либо профиль «моложе 7 дней».
    # Безопасно берём первую доступную дату: created_at / created / registered_at / updated_at.
    is_newbie = False
    if u:
        joined = (
            getattr(u, 'created_at', None)
            or getattr(u, 'created', None)
            or getattr(u, 'registered_at', None)
            or getattr(u, 'updated_at', None)
        )
        try:
            if joined:
                # поддержка date и datetime
                if isinstance(joined, date) and not isinstance(joined, datetime):
                    is_newbie = (date.today() - joined).days < 7
                else:
                    is_newbie = (datetime.utcnow().date() - joined.date()).days < 7
        except Exception:
            # в крайнем случае ориентируемся только на отсутствие анализов
            is_newbie = False

    # если анализов нет — всё равно показываем кнопку помощи
    if not has_body_analysis:
        is_newbie = True

    return dict(show_help_button=is_newbie, has_body_analysis=has_body_analysis)

def _month_deltas(user):
    # Первый день месяца в виде datetime, чтобы сравнивать с BodyAnalysis.timestamp
    start_dt = datetime.combine(date.today().replace(day=1), dt_time.min)

    # Берём первый и последний анализ ТОЛЬКО за текущий месяц по timestamp
    first = BodyAnalysis.query.filter(
        BodyAnalysis.user_id == user.id,
        BodyAnalysis.timestamp >= start_dt
    ).order_by(BodyAnalysis.timestamp.asc()).first()

    last = BodyAnalysis.query.filter(
        BodyAnalysis.user_id == user.id,
        BodyAnalysis.timestamp >= start_dt
    ).order_by(BodyAnalysis.timestamp.desc()).first()

    fat_delta = 0.0
    muscle_delta = 0.0
    if first and last and first.id != last.id:
        try:
            fat_delta = float((last.fat_mass or 0) - (first.fat_mass or 0))
            muscle_delta = float((last.muscle_mass or 0) - (first.muscle_mass or 0))
        except Exception:
            pass
    return {"fat_delta": fat_delta, "muscle_delta": muscle_delta}

@app.context_processor
def inject_renewal_reminder():
    u = get_current_user()  # у тебя уже есть helper для текущего пользователя
    show = False
    summary = {"fat_delta": 0.0, "muscle_delta": 0.0}
    days_left = None
    if u and getattr(u, "subscription", None) and u.subscription.status == 'active' and u.subscription.end_date:
        days_left = (u.subscription.end_date - date.today()).days
        if days_left is not None and 0 < days_left <= 5:
            # показываем 1 раз в день
            last = u.renewal_reminder_last_shown_on
            if last != date.today():
                show = True
        summary = _month_deltas(u)
    return dict(renewal_reminder_due=show, monthly_summary=summary, subscription_days_left=days_left)

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    """Обработчик для ошибки 404 (страница не найдена)."""
    return render_template('errors/404.html'), 404

@app.errorhandler(403)
def forbidden_error(error):
    """Обработчик для ошибки 403 (доступ запрещен)."""
    return render_template('errors/403.html'), 403

@app.errorhandler(500)
def internal_error(error):
    """Обработчик для ошибки 500 (внутренняя ошибка сервера)."""
    # Важно откатить сессию, чтобы избежать "зависших" транзакций в БД
    db.session.rollback()
    return render_template('errors/500.html'), 500
# ------------------ ROUTES ------------------

@app.route('/')
def index():
    if session.get('user_id'):
        return redirect(url_for('profile'))
    return render_template('index.html')

# алиас для /index, чтобы не было дубля логики
@app.route('/index')
def index_alias():
    return redirect(url_for('index'))

@app.route('/instructions')
def instructions_page():
    # Можно прокинуть ?section=scales чтобы автоскроллить к «весам»
    section = request.args.get('section')
    return render_template('instructions.html', scroll_to=section)

# Убедись, что у тебя есть:
# from sqlalchemy import func
# from flask import url_for

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email_input = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        # Нормализуем email на стороне Python (работает и с не-ASCII)
        email_norm = email_input.casefold()

        # Ищем пользователя по email без учета регистра
        user = User.query.filter(func.lower(User.email) == email_norm).first()

        if user and bcrypt.check_password_hash(user.password, password):
            session['user_id'] = user.id
            return redirect(url_for('profile'))

        return render_template('login.html', error="Неверный логин или пароль")

    return render_template('login.html')


@app.route('/api/check_email', methods=['POST'])
def check_email():
    data = request.get_json()
    if not data or 'email' not in data:
        return jsonify({"error": "Email not provided"}), 400

    email = data['email'].strip().lower()
    # Поиск без учета регистра
    user = User.query.filter(func.lower(User.email) == email).first()

    return jsonify({"exists": user is not None})


@app.route('/register', methods=['GET', 'POST'])
def register():
    errors = []

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        date_str = request.form.get('date_of_birth', '').strip()
        sex = (request.form.get('sex') or '').strip().lower()
        face_consent = bool(request.form.get('face_consent'))

        # Проверка обязательных полей
        if not name:
            errors.append("Имя обязательно.")
        if not email:
            errors.append("Email обязателен.")
        if not password or len(password) < 6:
            errors.append("Пароль обязателен и должен содержать минимум 6 символов.")
        if sex not in ('male', 'female'):
            errors.append("Пожалуйста, выберите пол.")

        # Проверка уникальности email
        if User.query.filter_by(email=email).first():
            errors.append("Этот email уже зарегистрирован.")

        # Проверка даты рождения
        date_of_birth = None
        if date_str:
            try:
                date_of_birth = datetime.strptime(date_str, "%Y-%m-%d").date()
                if date_of_birth > datetime.now().date():
                    errors.append("Дата рождения не может быть в будущем.")
            except ValueError:
                errors.append("Некорректный формат даты рождения.")
        else:
            errors.append("Дата рождения обязательна.")

        if errors:
            return render_template('register.html', errors=errors)

        # Обработка аватара (опционально)
        avatar_file_id = None
        file = request.files.get('avatar')
        if file and file.filename:
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            if ext in {'jpg', 'jpeg', 'png', 'webp'}:
                unique_filename = f"avatar_{uuid.uuid4().hex}.{ext}"
                file_data = file.read()

                new_file = UploadedFile(
                    filename=unique_filename,
                    content_type=file.mimetype,
                    data=file_data,
                    size=len(file_data)
                )
                db.session.add(new_file)
                db.session.flush()
                avatar_file_id = new_file.id
            else:
                errors.append("Неверный формат аватара (разрешены: jpg, jpeg, png, webp).")
                return render_template('register.html', errors=errors)

        # Хеширование пароля и сохранение пользователя
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            date_of_birth=date_of_birth,
            sex=sex,
            face_consent=face_consent,
            avatar_file_id=avatar_file_id
        )

        db.session.add(user)
        db.session.commit()
        return redirect('/login')

    return render_template('register.html')


@app.route('/profile')
@login_required
def profile():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)

    # --- Проверка валидности пользователя из сессии ---
    if not user:
        session.clear()
        flash("Ваша сессия была недействительна. Пожалуйста, войдите снова.", "warning")
        return redirect(url_for('login'))

    # Можно убрать, @login_required уже защищает, но оставим как «страховку»
    if not user_id:
        return redirect(url_for('login'))

    # Сохраняем «до изменений» email (нужно в UI)
    session['user_email_before_edit'] = user.email

    # Базовые данные
    age = calculate_age(user.date_of_birth) if user.date_of_birth else None
    diet_obj = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
    today_activity = Activity.query.filter_by(user_id=user_id, date=date.today()).first()

    analyses = (BodyAnalysis.query
                .filter_by(user_id=user_id)
                .order_by(BodyAnalysis.timestamp.desc())
                .limit(2)
                .all())
    latest_analysis = analyses[0] if len(analyses) > 0 else None
    previous_analysis = analyses[1] if len(analyses) > 1 else None

    total_meals = (db.session.query(func.sum(MealLog.calories))
                   .filter_by(user_id=user.id, date=date.today())
                   .scalar() or 0)
    today_meals = MealLog.query.filter_by(user_id=user.id, date=date.today()).all()

    metabolism = latest_analysis.metabolism if latest_analysis else 0
    active_kcal = today_activity.active_kcal if today_activity else None
    steps = today_activity.steps if today_activity else None
    distance_km = today_activity.distance_km if today_activity else None
    resting_kcal = today_activity.resting_kcal if today_activity else None

    missing_meals = (total_meals == 0)
    missing_activity = (active_kcal is None)
    just_activated = user.show_welcome_popup

    deficit = None
    if not missing_meals and not missing_activity and metabolism is not None:
        deficit = (metabolism + (active_kcal or 0)) - total_meals

    # --- Какая у пользователя «основная» группа (если есть) ---
    user_memberships = GroupMember.query.filter_by(user_id=user.id).all()
    user_joined_group = user.own_group if user.own_group else (user_memberships[0].group if user_memberships else None)

    all_analyses_for_progress_data = []  # Use a new variable name
    if user.initial_body_analysis_id:
        initial_analysis_for_chart = db.session.get(BodyAnalysis, user.initial_body_analysis_id)
        if initial_analysis_for_chart:
            # Fetch the SQLAlchemy objects
            analyses_objects = BodyAnalysis.query.filter(
                BodyAnalysis.user_id == user.id,
                BodyAnalysis.timestamp >= initial_analysis_for_chart.timestamp
            ).order_by(BodyAnalysis.timestamp.asc()).all()

            # Convert objects to a list of dictionaries
            all_analyses_for_progress_data = [
                {
                    "timestamp": analysis.timestamp.isoformat(),
                    "fat_mass": analysis.fat_mass
                }
                for analysis in analyses_objects
            ]

    diet = None
    if diet_obj:
        diet = {
            "total_kcal": getattr(diet_obj, "total_kcal", None) or getattr(diet_obj, "calories", None),
            "protein": getattr(diet_obj, "protein", None),
            "fat": getattr(diet_obj, "fat", None),
            "carbs": getattr(diet_obj, "carbs", None),
            "meals": {"breakfast": [], "lunch": [], "dinner": [], "snack": []}
        }

        meals_source = None
        if getattr(diet_obj, "meals", None):
            meals_source = diet_obj.meals
        if meals_source is None and getattr(diet_obj, "meals_json", None):
            try:
                meals_source = json.loads(diet_obj.meals_json)
            except Exception:
                meals_source = None
        if meals_source is None:
            per_meal = {}
            for key in ("breakfast", "lunch", "dinner", "snack"):
                val = getattr(diet_obj, key, None)
                if val:
                    if isinstance(val, str):
                        try:
                            per_meal[key] = json.loads(val)
                        except Exception:
                            per_meal[key] = []
                    else:
                        per_meal[key] = val
            if per_meal:
                meals_source = per_meal
        if meals_source is None and getattr(diet_obj, "items", None):
            meals_source = diet_obj.items

        def push(meal_type, name, grams=None, kcal=None):
            mt = (meal_type or "").lower()
            if mt in diet["meals"]:
                diet["meals"][mt].append({"name": name or "Блюдо", "grams": grams, "kcal": kcal})

        if isinstance(meals_source, dict):
            for k in ("breakfast", "lunch", "dinner", "snack"):
                for it in (meals_source.get(k, []) or []):
                    if isinstance(it, dict):
                        grams = it.get("grams") or it.get("weight_g")
                        kcal = it.get("kcal") or it.get("calories")
                        name = it.get("name") or it.get("title")
                    else:
                        grams = getattr(it, "grams", None) or getattr(it, "weight_g", None)
                        kcal = getattr(it, "kcal", None) or getattr(it, "calories", None)
                        name = getattr(it, "name", None) or getattr(it, "title", None)
                    push(k, name, grams, kcal)
        elif isinstance(meals_source, list):
            for it in meals_source:
                if isinstance(it, dict):
                    mt = it.get("meal_type") or it.get("type") or it.get("meal")
                    grams = it.get("grams") or it.get("weight_g")
                    kcal = it.get("kcal") or it.get("calories")
                    name = it.get("name") or it.get("title")
                else:
                    mt = getattr(it, "meal_type", None)
                    grams = getattr(it, "grams", None) or getattr(it, "weight_g", None)
                    kcal = getattr(it, "kcal", None) or getattr(it, "calories", None)
                    name = getattr(it, "name", None) or getattr(it, "title", None)
                push(mt, name, grams, kcal)

        if not diet["total_kcal"]:
            try:
                diet["total_kcal"] = sum((i.get("kcal") or 0) for lst in diet["meals"].values() for i in lst) or None
            except Exception:
                pass

    # --- Прогресс жиросжигания (УЛУЧШЕННАЯ ЛОГИКА С ПРОГНОЗОМ) ---
    fat_loss_progress = None
    KCAL_PER_KG_FAT = 7700  # Энергетическая ценность 1 кг жира

    # Получаем стартовый и последний анализы
    initial_analysis = db.session.get(BodyAnalysis,
                                      user.initial_body_analysis_id) if user.initial_body_analysis_id else None

    if initial_analysis and latest_analysis and latest_analysis.fat_mass and user.fat_mass_goal and initial_analysis.fat_mass > user.fat_mass_goal:

        # --- 1. Расчет фактического прогресса на момент последнего замера ---
        initial_fat_mass = initial_analysis.fat_mass
        last_measured_fat_mass = latest_analysis.fat_mass
        goal_fat_mass = user.fat_mass_goal

        total_fat_to_lose_kg = initial_fat_mass - goal_fat_mass
        fact_lost_so_far_kg = initial_fat_mass - last_measured_fat_mass

        # --- 2. Расчет прогнозируемого прогресса на основе дефицита калорий ПОСЛЕ последнего замера ---
        start_datetime = latest_analysis.timestamp
        today_date = date.today()

        # Получаем все логи приемов пищи и активности одним запросом
        meal_logs_since_last_analysis = MealLog.query.filter(MealLog.user_id == user.id,
                                                             MealLog.date >= start_datetime.date()).all()
        activity_logs_since_last_analysis = Activity.query.filter(Activity.user_id == user.id,
                                                                  Activity.date >= start_datetime.date()).all()

        # Создаем словари для быстрого доступа
        meals_map = {}
        for log in meal_logs_since_last_analysis:
            meals_map.setdefault(log.date, 0)
            meals_map[log.date] += log.calories

        activity_map = {log.date: log.active_kcal for log in activity_logs_since_last_analysis}

        # Считаем накопленный дефицит
        total_accumulated_deficit = 0
        metabolism = latest_analysis.metabolism or 0

        # Убедимся, что не считаем дни из будущего
        delta_days = (today_date - start_datetime.date()).days

        if delta_days >= 0:
            for i in range(delta_days + 1):
                current_day = start_datetime.date() + timedelta(days=i)
                consumed = meals_map.get(current_day, 0)
                burned_active = activity_map.get(current_day, 0)

                # Особая логика для дня замера: не учитываем калории и активность ДО момента замера
                if i == 0:
                    calories_before_analysis = db.session.query(func.sum(MealLog.calories)).filter(
                        MealLog.user_id == user.id,
                        MealLog.date == current_day,
                        MealLog.created_at < start_datetime
                    ).scalar() or 0
                    consumed -= calories_before_analysis
                    # Активность за день замера игнорируем, т.к. нет точного времени
                    burned_active = 0

                daily_deficit = (metabolism + burned_active) - consumed
                if daily_deficit > 0:
                    total_accumulated_deficit += daily_deficit

        # Конвертируем дефицит в килограммы
        estimated_burned_since_last_measurement_kg = total_accumulated_deficit / KCAL_PER_KG_FAT

        # --- 3. Объединение фактического и прогнозируемого прогресса ---

        # Текущая предполагаемая жировая масса
        estimated_current_fat_mass = last_measured_fat_mass - estimated_burned_since_last_measurement_kg

        # Общий прогресс от начальной точки
        total_lost_so_far_kg = initial_fat_mass - estimated_current_fat_mass

        # Итоговый процент
        percentage = 0
        if total_fat_to_lose_kg > 0:
            percentage = (total_lost_so_far_kg / total_fat_to_lose_kg) * 100

        fat_loss_progress = {
            'percentage': min(100, max(0, percentage)),  # Ограничиваем 0-100%
            'burned_kg': total_lost_so_far_kg,
            'total_to_lose_kg': total_fat_to_lose_kg,
            'initial_kg': initial_fat_mass,
            'goal_kg': goal_fat_mass,
            'current_kg': estimated_current_fat_mass  # Теперь это расчетное значение
        }

    return render_template(
        'profile.html',
        user=user,
        age=age,
        diet=diet,
        today_activity=today_activity,
        latest_analysis=latest_analysis,
        previous_analysis=previous_analysis,
        total_meals=total_meals,
        today_meals=today_meals,
        metabolism=metabolism,
        active_kcal=active_kcal,
        steps=steps,
        distance_km=distance_km,
        resting_kcal=resting_kcal,
        deficit=deficit,
        missing_meals=missing_meals,
        missing_activity=missing_activity,
        user_joined_group=user_joined_group,
        all_analyses_for_progress=all_analyses_for_progress_data,
        fat_loss_progress=fat_loss_progress,
        just_activated=just_activated
    )

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# Убедитесь, что jsonify импортирован в начале файла: from flask import jsonify

@app.route('/upload_analysis', methods=['POST'])
@login_required
def upload_analysis():
    file = request.files.get('file')
    user = get_current_user()
    if not file or not user:
        return jsonify({"success": False, "error": "Файл не загружен или вы не авторизованы."}), 400

    # Временно сохраняем файл
    filename = secure_filename(file.filename)
    upload_folder = app.config.get('UPLOAD_FOLDER', 'uploads')
    if not os.path.exists(upload_folder):
        os.makedirs(upload_folder)
    filepath = os.path.join(upload_folder, filename)
    file.save(filepath)

    try:
        with open(filepath, "rb") as image_file:
            base64_image = base64.b64encode(image_file.read()).decode("utf-8")

        # --- ШАГ 1: Извлечение данных с изображения ---
        response_metrics = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — фитнес-аналитик. Извлеки следующие параметры из фото анализа тела (bioimpedance):"
                        "height, weight, muscle_mass, muscle_percentage, body_water, protein_percentage, "
                        "bone_mineral_percentage, skeletal_muscle_mass, visceral_fat_rating, metabolism, "
                        "waist_hip_ratio, body_age, fat_mass, bmi, fat_free_body_weight. "
                        "Верни СТРОГО JSON с найденными числовыми значениями."
                    )
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        {"type": "text", "text": "Извлеки параметры из анализа тела."}
                    ]
                }
            ],
            max_tokens=1000,
            response_format={"type": "json_object"}
        )
        content = response_metrics.choices[0].message.content.strip()
        result = json.loads(content)

        # --- НАЧАЛО ИЗМЕНЕНИЙ ---

        # Если рост не найден, пытаемся взять его из последнего анализа
        if not result.get('height'):
            last_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()
            if last_analysis and last_analysis.height:
                result['height'] = last_analysis.height

        # Список обязательных полей (рост теперь не в нем)
        required_keys = [
            'weight', 'muscle_mass', 'muscle_percentage', 'body_water',
            'protein_percentage', 'bone_mineral_percentage', 'skeletal_muscle_mass',
            'visceral_fat_rating', 'metabolism', 'waist_hip_ratio', 'body_age',
            'fat_mass', 'bmi', 'fat_free_body_weight'
        ]
        missing_keys = [key for key in required_keys if key not in result or result.get(key) is None]

        if missing_keys:
            missing_str = ', '.join(missing_keys)
            return jsonify({
                "success": False,
                "error": f"Не удалось распознать все показатели. Попробуйте другое фото. Отсутствуют: {missing_str}"
            }), 400
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

        # --- ШАГ 2: Генерация целей ---
        age = calculate_age(user.date_of_birth) if user.date_of_birth else 'не указан'
        prompt_goals = (
            # Промпт теперь будет использовать либо новый, либо старый рост
            f"Для пользователя с параметрами: возраст {age}, рост {result.get('height')} см, "
            f"вес {result.get('weight')} кг, жировая масса {result.get('fat_mass')} кг, "
            f"мышечная масса {result.get('muscle_mass')} кг. "
            f"Предложи реалистичные цели по снижению жировой массы и увеличению мышечной массы. "
            f"Верни СТРОГО JSON в формате: "
            f'{{"fat_mass_goal": <число>, "muscle_mass_goal": <число>}}'
        )
        response_goals = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты — профессиональный фитнес-тренер. Давай цели в формате JSON."},
                {"role": "user", "content": prompt_goals}
            ],
            max_tokens=200,
            response_format={"type": "json_object"}
        )
        goals_content = response_goals.choices[0].message.content.strip()
        goals_result = json.loads(goals_content)
        result.update(goals_result)

        session['temp_analysis'] = result
        return jsonify({"success": True, "redirect_url": url_for('confirm_analysis')})

    except Exception as e:
        print(f"!!! ОШИБКА В UPLOAD_ANALYSIS: {e}")
        return jsonify({
            "success": False,
            "error": "Не удалось проанализировать изображение. Пожалуйста, попробуйте другое фото или загрузите файл лучшего качества."
        }), 500
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


# ЗАМЕНИТЕ СТАРУЮ ФУНКЦИЮ meals НА ЭТУ
@app.route("/meals", methods=["GET", "POST"])
@login_required
def meals():
    user = get_current_user()

    # --- ЛОГИКА СОХРАНЕНИЯ (POST-ЗАПРОС) ---
    if request.method == "POST":
        meal_type = request.form.get('meal_type')
        if not meal_type:
            flash("Произошла ошибка: не указан тип приёма пищи.", "error")
            return redirect(url_for('meals'))

        try:
            calories = int(request.form.get('calories', 0))
            protein = float(request.form.get('protein', 0.0))
            fat = float(request.form.get('fat', 0.0))
            carbs = float(request.form.get('carbs', 0.0))
            name = request.form.get('name')
            verdict = request.form.get('verdict')
            analysis = request.form.get('analysis', '')

            existing_meal = MealLog.query.filter_by(
                user_id=user.id, date=date.today(), meal_type=meal_type
            ).first()

            if existing_meal:
                existing_meal.calories = calories
                existing_meal.protein = protein
                existing_meal.fat = fat
                existing_meal.carbs = carbs
                existing_meal.name = name
                existing_meal.verdict = verdict
                existing_meal.analysis = analysis
                flash(f"Приём пищи '{meal_type.capitalize()}' успешно обновлён!", "success")
            else:
                new_meal = MealLog(
                    user_id=user.id, date=date.today(), meal_type=meal_type,
                    calories=calories, protein=protein, fat=fat, carbs=carbs,
                    name=name, verdict=verdict, analysis=analysis
                )
                db.session.add(new_meal)
                flash(f"Приём пищи '{meal_type.capitalize()}' успешно добавлен!", "success")

            db.session.commit()

        except (ValueError, TypeError) as e:
            db.session.rollback()
            flash(f"Ошибка в формате данных от AI. Не удалось сохранить. ({e})", "error")

        # После обработки POST-запроса, перенаправляем на ту же страницу
        # чтобы избежать повторной отправки формы при обновлении
        return redirect(url_for('meals'))

    # --- ЛОГИКА ОТОБРАЖЕНИЯ (GET-ЗАПРОС) ---
    today_meals = MealLog.query.filter_by(user_id=user.id, date=date.today()).all()
    grouped = {
        "breakfast": [], "lunch": [], "dinner": [], "snack": []
    }
    for m in today_meals:
        grouped[m.meal_type].append(m)

    latest_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).first()

    return render_template("profile.html",
                           user=user,
                           meals=grouped,
                           latest_analysis=latest_analysis,
                           tab='meals')

# --- НАЧАЛО ИЗМЕНЕНИЙ: Обновлённая функция для сохранения анализа ---
from flask import jsonify # Убедись, что jsonify импортирован вверху файла


@app.route('/confirm_analysis', methods=['GET', 'POST'])
@login_required
def confirm_analysis():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)

    # --- ЛОГИКА POST-ЗАПРОСА (Сохранение данных) ---
    if request.method == 'POST':
        if 'temp_analysis' not in session:
            flash("Данные для сохранения устарели. Пожалуйста, попробуйте снова.", "warning")
            return redirect(url_for('profile'))

        analysis_data = session['temp_analysis']

        # Получаем предыдущий замер ДО сохранения нового
        previous_analysis = BodyAnalysis.query.filter_by(user_id=user.id).order_by(
            BodyAnalysis.timestamp.desc()).first()

        # Создаем и наполняем новую запись анализа
        new_analysis_entry = BodyAnalysis(user_id=user.id, timestamp=datetime.now(UTC))
        for field, value in analysis_data.items():
            if hasattr(new_analysis_entry, field):
                setattr(new_analysis_entry, field, value)

        edited_height = request.form.get('height', type=float)
        if edited_height is not None:
            new_analysis_entry.height = edited_height

        # Обновляем цели пользователя, если они были отправлены
        if 'fat_mass_goal' in request.form:
            user.fat_mass_goal = request.form.get('fat_mass_goal', type=float)
        if 'muscle_mass_goal' in request.form:
            user.muscle_mass_goal = request.form.get('muscle_mass_goal', type=float)

        user.updated_at = datetime.now(UTC)

        db.session.add(new_analysis_entry)
        db.session.flush()  # Получаем ID новой записи до коммита

        # Если это самый первый анализ, устанавливаем его как стартовую точку
        if not user.initial_body_analysis_id:
            user.initial_body_analysis_id = new_analysis_entry.id

        # --- Вызов генератора комментария ИИ ---
        if previous_analysis:
            print("DEBUG: Найден предыдущий анализ. Вызываю генератор комментария ИИ...")
            ai_comment_text = generate_progress_commentary(user, previous_analysis, new_analysis_entry)
            print(f"DEBUG: Генератор ИИ вернул: {str(ai_comment_text)[:150]}...")  # Логгируем первые 150 символов
            if ai_comment_text:
                new_analysis_entry.ai_comment = ai_comment_text
                # Сохраняем комментарий в сессии, чтобы сразу показать его пользователю
                session['last_ai_comment'] = ai_comment_text

        db.session.commit()
        session.pop('temp_analysis', None)  # Очищаем временные данные

        flash("Анализ и цели успешно сохранены!", "success")
        # Перенаправляем на GET-запрос этой же страницы, чтобы показать результат
        return redirect(url_for('confirm_analysis'))

    # --- ЛОГИКА GET-ЗАПРОСА (Отображение страницы) ---

    # 1. Проверяем, есть ли готовый комментарий для отображения (после редиректа)
    last_ai_comment = session.pop('last_ai_comment', None)
    if last_ai_comment:
        # Комментарий есть, значит, мы только что сохранили данные.
        # Показываем страницу с комментарием. Форма не нужна.
        return render_template('confirm_analysis.html',
                               data={},  # Передаем пустой словарь, т.к. форма не будет показана
                               user=user,
                               ai_comment=last_ai_comment)

    # 2. Если комментария нет, проверяем, есть ли данные для подтверждения
    if 'temp_analysis' in session:
        analysis_data = session['temp_analysis']
        # Показываем страницу с формой для подтверждения данных
        return render_template('confirm_analysis.html',
                               data=analysis_data,
                               user=user,
                               ai_comment=None)

    # 3. Если нет ни комментария, ни данных для подтверждения — отправляем в профиль
    # (Это может случиться, если пользователь просто зайдет по прямой ссылке)
    flash("Нет данных для подтверждения. Пожалуйста, загрузите анализ снова.", "warning")
    return redirect(url_for('profile'))


@app.route('/generate_telegram_code')
def generate_telegram_code():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    code = ''.join(random.choices(string.digits, k=8))
    user = db.session.get(User, user_id)
    user.telegram_code = code
    db.session.commit()

    return jsonify({'code': code})


@app.route('/generate_diet')
@login_required
def generate_diet():
    user = get_current_user()
    if not getattr(user, 'has_subscription', False):
        flash("Генерация диеты доступна только по подписке.", "warning")
        return redirect(url_for('profile'))

    user_id = session.get('user_id')
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    goal = request.args.get("goal", "maintain")
    # пол больше не из query; берём из профиля
    gender = (user.sex or "male")
    preferences = request.args.get("preferences", "")

    latest_analysis = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()
    # Проверка наличия всех необходимых данных для генерации диеты
    if not (latest_analysis and
            all(getattr(latest_analysis, attr, None) is not None
                for attr in ['height', 'weight', 'muscle_mass', 'fat_mass', 'metabolism'])):
        flash("Пожалуйста, загрузите актуальный анализ тела для генерации диеты.", "warning")
        # Возвращаем JSON с командой на редирект, чтобы фронтенд мог обработать это
        return jsonify({"redirect": url_for('profile')})

    prompt = f"""
    У пользователя следующие параметры:
    Рост: {latest_analysis.height} см
    Вес: {latest_analysis.weight} кг
    Мышечная масса: {latest_analysis.muscle_mass} кг
    Жировая масса: {latest_analysis.fat_mass} кг
    Метаболизм: {latest_analysis.metabolism} ккал
    Цель: {goal}
    Пол: {gender}
    Предпочтения: {preferences}

    Составь рацион питания на 1 день: завтрак, обед, ужин, перекус. Для каждого укажи:
    - название блюда ("name")
    - граммовку ("grams")
    - калории ("kcal")
    - подробный пошаговый рецепт приготовления ("recipe")

    Верни JSON строго по формату:
    ```json
    {{
        "breakfast": [{{"name": "...", "grams": 0, "kcal": 0, "recipe": "..."}}],
        "lunch": [...],
        "dinner": [...],
        "snack": [...],
        "total_kcal": 0,
        "protein": 0,
        "fat": 0,
        "carbs": 0
    }}
    ```
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты профессиональный диетолог. Отвечай строго в формате JSON."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1500
        )

        content = response.choices[0].message.content.strip()
        if '```json' in content:
            content = content.split('```json')[1].split('```')[0].strip()
        diet_data = json.loads(content)

        # Удаляем старую диету за сегодня, если она есть
        existing_diet = Diet.query.filter_by(user_id=user_id, date=date.today()).first()
        if existing_diet:
            db.session.delete(existing_diet)
            db.session.commit()

        diet = Diet(
            user_id=user_id,
            date=date.today(),
            breakfast=json.dumps(diet_data.get('breakfast', []), ensure_ascii=False),
            lunch=json.dumps(diet_data.get('lunch', []), ensure_ascii=False),
            dinner=json.dumps(diet_data.get('dinner', []), ensure_ascii=False),
            snack=json.dumps(diet_data.get('snack', []), ensure_ascii=False),
            total_kcal=diet_data.get('total_kcal'),
            protein=diet_data.get('protein'),
            fat=diet_data.get('fat'),
            carbs=diet_data.get('carbs')
        )
        db.session.add(diet)
        db.session.commit()

        flash("Диета успешно сгенерирована!", "success")

        # Отправка в Telegram
        if user.telegram_chat_id:
            message = f"🍽️ Ваша диета на сегодня:\n\n"

            def format_meal(title, items):
                lines = [f"🍱 {title}:"]
                for it in items:
                    lines.append(f"- {it['name']} ({it['grams']} г, {it['kcal']} ккал)")
                return "\n".join(lines)

            message += format_meal("Завтрак", diet_data.get("breakfast", [])) + "\n\n"
            message += format_meal("Обед", diet_data.get("lunch", [])) + "\n\n"
            message += format_meal("Ужин", diet_data.get("dinner", [])) + "\n\n"
            message += format_meal("Перекус", diet_data.get("snack", [])) + "\n\n"
            message += f"🔥 Калории: {diet_data['total_kcal']} ккал\n"
            message += f"🍗 Белки: {diet_data['protein']} г\n"
            message += f"🥑 Жиры: {diet_data['fat']} г\n"
            message += f"🥔 Углеводы: {diet_data['carbs']} г"

            try:
                requests.post(TELEGRAM_API_URL, data={
                    "chat_id": user.telegram_chat_id,
                    "text": message
                })
            except Exception as e:
                print(f"[Telegram Error] {e}")

        return jsonify({"redirect": "/diet"})

    except Exception as e:
        flash(f"Ошибка генерации диеты: {e}", "error")
        return jsonify({"error": str(e)}), 500


@app.route('/edit_profile', methods=['POST'])
@login_required
def edit_profile():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    try:
        # --- Обновление текстовых полей ---
        new_name = request.form.get('name')
        if new_name and new_name.strip():
            user.name = new_name.strip()

        new_email = request.form.get('email')
        if new_email and new_email.strip() and new_email.strip().lower() != (user.email or '').lower():
            if User.query.filter(func.lower(User.email) == new_email.strip().lower(), User.id != user.id).first():
                flash("Этот email уже используется другим пользователем.", "error")
                return redirect(url_for('profile'))
            user.email = new_email.strip()

        date_of_birth_str = request.form.get('date_of_birth')
        if date_of_birth_str:
            user.date_of_birth = datetime.strptime(date_of_birth_str, '%Y-%m-%d').date()

        # --- Обработка аватара (ИСПРАВЛЕННАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ) ---
        file = request.files.get('avatar')
        if file and file.filename:
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            if ext not in {'jpg', 'jpeg', 'png', 'webp'}:
                flash("Неверный формат аватара (разрешены: jpg, jpeg, png, webp).", "error")
                return redirect(url_for('profile'))

            old_avatar_to_delete = user.avatar if user.avatar_file_id else None

            if old_avatar_to_delete:
                user.avatar_file_id = None
                db.session.flush()

            unique_filename = f"avatar_{user.id}_{uuid.uuid4().hex}.{ext}"
            file_data = file.read()
            new_file = UploadedFile(
                filename=unique_filename,
                content_type=file.mimetype,
                data=file_data,
                size=len(file_data),
                user_id=user.id
            )
            db.session.add(new_file)
            db.session.flush()

            user.avatar_file_id = new_file.id

            if old_avatar_to_delete:
                db.session.delete(old_avatar_to_delete)

        db.session.commit()
        flash("Профиль успешно обновлен!", "success")

    except ValueError:
        db.session.rollback()
        flash("Неверный формат даты рождения.", "error")
    except Exception as e:
        db.session.rollback()
        print(f"!!! ОШИБКА ПРИ ОБНОВЛЕНИИ ПРОФИЛЯ: {e}")
        flash("Произошла ошибка при обновлении профиля.", "error")

    return redirect(url_for('profile'))


@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    if not new_password:
        flash("Новый пароль не может быть пустым.", "error")
        return redirect(url_for('profile'))

    if new_password != confirm_password:
        flash("Пароли не совпадают.", "error")
        return redirect(url_for('profile'))

    if len(new_password) < 6:
        flash("Пароль должен содержать не менее 6 символов.", "error")
        return redirect(url_for('profile'))

    try:
        user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')
        db.session.commit()
        flash("Пароль успешно изменен!", "success")
    except Exception as e:
        db.session.rollback()
        print(f"!!! ОШИБКА ПРИ СМЕНЕ ПАРОЛЯ: {e}")
        flash("Произошла ошибка при смене пароля.", "error")

    return redirect(url_for('profile'))

@app.route('/diet')
@login_required
def diet():
    if not get_current_user().has_subscription:
        flash("Просмотр диеты доступен только по подписке.", "warning")
        return redirect(url_for('profile'))

    user = get_current_user()
    if not user.has_subscription:
        flash("Доступно только по подписке. Активируйте подписку для полного доступа.", "warning")
        return redirect('/profile')

    diet = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first()
    if not diet:
        flash("Диета ещё не сгенерирована. Сгенерируйте ее из профиля.", "info")
        return redirect('/profile')

    return render_template("confirm_diet.html", diet=diet,
                           breakfast=json.loads(diet.breakfast),
                           lunch=json.loads(diet.lunch),
                           dinner=json.loads(diet.dinner),
                           snack=json.loads(diet.snack))

@app.route('/upload_activity', methods=['POST'])
def upload_activity():
    data = request.json
    email = data.get('email')
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404

    # Удаляем старую активность за сегодня, если она есть
    existing_activity = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
    if existing_activity:
        db.session.delete(existing_activity)
        db.session.commit()

    activity = Activity(
        user_id=user.id,
        date=date.today(),
        steps=data.get('steps'),
        active_kcal=data.get('active_kcal'),
        resting_kcal=data.get('resting_kcal'),
        heart_rate_avg=data.get('heart_rate_avg'),
        distance_km=data.get('distance_km'),
        source=data.get('source', 'manual')
    )
    db.session.add(activity)
    db.session.commit()

    return jsonify({'message': 'Активность сохранена'})


@app.route('/manual_activity', methods=['GET', 'POST'])
@login_required
def manual_activity():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)

    if request.method == 'POST':
        steps = request.form.get('steps')
        active_kcal = request.form.get('active_kcal')
        resting_kcal = request.form.get('resting_kcal')
        heart_rate_avg = request.form.get('heart_rate_avg')
        distance_km = request.form.get('distance_km')

        # Удаляем старую активность за сегодня, если она есть
        existing_activity = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
        if existing_activity:
            db.session.delete(existing_activity)
            db.session.commit()

        activity = Activity(
            user_id=user.id,
            date=date.today(),
            steps=int(steps or 0),
            active_kcal=int(active_kcal or 0),
            resting_kcal=int(resting_kcal or 0),
            heart_rate_avg=int(heart_rate_avg or 0),
            distance_km=float(distance_km or 0),
            source='manual'
        )
        db.session.add(activity)
        db.session.commit()
        flash("Активность за сегодня успешно обновлена!", "success")
        return redirect('/profile')

    # Предзаполнение формы текущими данными, если они есть
    today_activity = Activity.query.filter_by(user_id=user_id, date=date.today()).first()
    return render_template('manual_activity.html', user=user, today_activity=today_activity)


@app.route('/diet_history')
@login_required
def diet_history():
    if not get_current_user().has_subscription:
        flash("История диет доступна только по подписке.", "warning")
        return redirect(url_for('profile'))

    user_id = session.get('user_id')

    today = date.today()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    diets = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).all()
    week_total = db.session.query(func.sum(Diet.total_kcal)).filter(
        Diet.user_id == user_id,
        Diet.date >= week_ago
    ).scalar() or 0

    month_total = db.session.query(func.sum(Diet.total_kcal)).filter(
        Diet.user_id == user_id,
        Diet.date >= month_ago
    ).scalar() or 0

    # 📊 График за 7 дней
    last_7_days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    chart_labels = [d.strftime("%d.%m") for d in last_7_days]
    chart_values = []

    for d in last_7_days:
        total = db.session.query(func.sum(Diet.total_kcal)).filter_by(user_id=user_id, date=d).scalar()
        chart_values.append(total or 0)

    return render_template(
        "diet_history.html",
        diets=diets,
        week_total=week_total,
        month_total=month_total,
        chart_labels=json.dumps(chart_labels),
        chart_values=json.dumps(chart_values)
    )

# === TELEGRAM: лог активности по chat_id ===
@app.route('/api/activity/log', methods=['POST'])
def api_activity_log():
    data = request.get_json(force=True, silent=True) or {}
    chat_id = str(data.get('chat_id') or '').strip()
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    user = User.query.filter_by(telegram_chat_id=chat_id).first()
    if not user:
        return jsonify({"error": "user not found"}), 404

    try:
        steps = int(data.get('steps') or 0)
        active_kcal = int(data.get('active_kcal') or 0)
    except Exception:
        return jsonify({"error": "invalid numbers"}), 400

    # перезаписываем активность за сегодня
    today = date.today()
    existing = Activity.query.filter_by(user_id=user.id, date=today).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()

    act = Activity(
        user_id=user.id,
        date=today,
        steps=steps,
        active_kcal=active_kcal,
        source='telegram'
    )
    db.session.add(act)
    db.session.commit()
    return jsonify({"ok": True, "message": "activity saved"})

@app.route('/add_meal', methods=['POST'])
@login_required
def add_meal():
    if not get_current_user().has_subscription:
        flash("Доступ к группам и сообществу открыт только по подписке.", "warning")
        return redirect(url_for('profile'))

    user_id = session.get('user_id')
    meal_type = request.form.get('meal_type')
    today = date.today()

    if not meal_type:
        flash("Произошла ошибка: не указан тип приёма пищи.", "error")
        return redirect(url_for('meals')) # Перенаправляем на страницу с приёмами пищи

    try:
        # Безопасно получаем данные из формы с помощью .get()
        name = request.form.get('name')
        verdict = request.form.get('verdict')
        analysis = request.form.get('analysis', '')
        # Преобразуем в числа с обработкой ошибок
        calories = int(request.form.get('calories', 0))
        protein = float(request.form.get('protein', 0.0))
        fat = float(request.form.get('fat', 0.0))
        carbs = float(request.form.get('carbs', 0.0))

        # Ищем существующую запись для обновления или создаём новую
        existing_meal = MealLog.query.filter_by(
            user_id=user_id,
            date=today,
            meal_type=meal_type
        ).first()

        if existing_meal:
            # Обновляем существующую запись
            existing_meal.name = name
            existing_meal.verdict = verdict
            existing_meal.calories = calories
            existing_meal.protein = protein
            existing_meal.fat = fat
            existing_meal.carbs = carbs
            existing_meal.analysis = analysis
            flash(f"Приём пищи '{meal_type.capitalize()}' успешно обновлён!", "success")
        else:
            # Создаём новую запись
            new_meal = MealLog(
                user_id=user_id,
                date=today,
                meal_type=meal_type,
                name=name,
                verdict=verdict,
                calories=calories,
                protein=protein,
                fat=fat,
                carbs=carbs,
                analysis=analysis
            )
            db.session.add(new_meal)
            flash(f"Приём пищи '{meal_type.capitalize()}' успешно добавлен!", "success")

        db.session.commit()

    except (ValueError, TypeError) as e:
        # Ловим ошибки, если данные от AI пришли в неверном формате
        db.session.rollback()
        flash(f"Ошибка сохранения данных. Пожалуйста, попробуйте снова. ({e})", "error")

    # Перенаправляем пользователя обратно на вкладку "Приёмы пищи"
    return redirect(url_for('meals'))

@app.route('/diet/<int:diet_id>')
@login_required
def view_diet(diet_id):
    user_id = session.get('user_id')
    diet = Diet.query.filter_by(id=diet_id, user_id=user_id).first()
    if not diet:
        flash("Диета не найдена.", "error")
        return redirect('/diet_history')

    return render_template("confirm_diet.html", diet=diet,
                           breakfast=json.loads(diet.breakfast),
                           lunch=json.loads(diet.lunch),
                           dinner=json.loads(diet.dinner),
                           snack=json.loads(diet.snack))


@app.route('/reset_diet', methods=['POST'])
@login_required
def reset_diet():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)

    diet = Diet.query.filter_by(user_id=user.id, date=date.today()).first()
    if diet:
        try:
            db.session.delete(diet)
            db.session.commit()
            return jsonify({'success': True, 'message': 'Рацион успешно сброшен.'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(e)}), 500
    else:
        # Этот случай тоже обрабатываем, хотя он маловероятен
        return jsonify({'success': True, 'message': 'Нет рациона для сброса.'})

@app.route('/api/link_telegram', methods=['POST'])
def link_telegram():
    data = request.json
    code = data.get("code")
    chat_id = data.get("chat_id")

    user = User.query.filter_by(telegram_code=code).first()
    if not user:
        return jsonify({"error": "Неверный код"}), 404

    user.telegram_chat_id = str(chat_id)
    user.telegram_code = None
    db.session.commit()
    return jsonify({"message": "OK"}), 200


@app.route('/api/is_registered/<int:chat_id>')
def is_registered(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    if user:
        return jsonify({"ok": True}), 200
    return jsonify({"ok": False}), 404


@app.route('/api/current_diet/<int:chat_id>')
def api_current_diet(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    if not user:
        return jsonify({"error": "not found"}), 404

    diet = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first()
    if not diet:
        return jsonify({"error": "no diet"}), 404

    return jsonify({
        "date": diet.date.isoformat(),
        "breakfast": json.loads(diet.breakfast),
        "lunch": json.loads(diet.lunch),
        "dinner": json.loads(diet.dinner),
        "snack": json.loads(diet.snack),
        "total_kcal": diet.total_kcal,
        "protein": diet.protein,
        "fat": diet.fat,
        "carbs": diet.carbs
    })


@app.route('/activity')
@login_required
def activity():
    user_id = session.get('user_id')

    user = db.session.get(User, user_id)
    today_activity = Activity.query.filter_by(user_id=user_id, date=date.today()).first()

    # Получаем активность за последние 7 дней для графиков
    week_ago = date.today() - timedelta(days=7)
    activities = Activity.query.filter(
        Activity.user_id == user_id,
        Activity.date >= week_ago
    ).order_by(Activity.date).all()

    # Подготавливаем данные для графиков
    chart_data = {
        'dates': [],
        'steps': [],
        'calories': [],
        'heart_rate': []
    }

    for day in (date.today() - timedelta(days=i) for i in range(6, -1, -1)):
        chart_data['dates'].append(day.strftime('%d.%m'))
        activity_for_day = next((a for a in activities if a.date == day),
                                None)  # Переименовано, чтобы избежать конфликта
        chart_data['steps'].append(activity_for_day.steps if activity_for_day else 0)
        chart_data['calories'].append(activity_for_day.active_kcal if activity_for_day else 0)
        chart_data['heart_rate'].append(activity_for_day.heart_rate_avg if activity_for_day else 0)

    # Здесь возвращаем activity.html, если он есть, или используем profile.html с нужным табом
    return render_template(
        'profile.html',
        user=user,
        today_activity=today_activity,
        chart_data=chart_data,
        tab='activity'  # Указываем активный таб
    )


@app.route('/api/log_meal', methods=['POST'])
def log_meal():
    data = request.get_json()
    user = User.query.filter_by(telegram_chat_id=str(data['chat_id'])).first_or_404()

    # Сначала попробуем взять готовые числа из payload
    calories = data.get("calories")
    protein = data.get("protein")
    fat = data.get("fat")
    carbs = data.get("carbs")

    raw = data.get("analysis", "")

    # Если хоть одно из полей не пришло — падём на разбор текста
    if None in (calories, protein, fat, carbs):
        # парсим из raw
        def ptn(p):
            m = re.search(p, raw, flags=re.IGNORECASE)
            return float(m.group(1)) if m else None

        calories = ptn(r'Калории[:\s]+(\d+)')
        protein = ptn(r'Белки[:\s]+([\d.]+)')
        fat = ptn(r'Жиры[:\s]+([\d.]+)')
        carbs = ptn(r'Углеводы[:\s]+([\d.]+)')

    # если всё ещё что‑то не распарсилось — 400
    if None in (calories, protein, fat, carbs):
        return jsonify({"error": "cannot parse BJU"}), 400

    meal = MealLog(
        user_id=user.id,
        date=date.today(),
        meal_type=data['meal_type'],
        calories=int(calories),
        protein=float(protein),
        fat=float(fat),
        carbs=float(carbs),
        analysis=raw
    )

    try:
        db.session.add(meal)
        db.session.commit()
        return jsonify({"status": "ok"}), 200

    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "exists"}), 409


@app.route('/api/log_meal', methods=['DELETE'])
def delete_meal():
    data = request.get_json()
    user = User.query.filter_by(telegram_chat_id=str(data['chat_id'])).first_or_404()
    meal = MealLog.query.filter_by(
        user_id=user.id,
        date=date.today(),
        meal_type=data['meal_type']
    ).first_or_404()
    db.session.delete(meal)
    db.session.commit()
    return '', 200


# ЭТО ПРАВИЛЬНЫЙ КОД
from flask import jsonify # Убедись, что jsonify импортирован вверху файла

@app.route('/analyze_meal_photo', methods=['POST'])
def analyze_meal_photo():
    # Поддержка вызова из Telegram: принимаем chat_id в форме или query
    chat_id = request.form.get('chat_id') or request.args.get('chat_id')
    user = None
    if chat_id:
        user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    else:
        user = get_current_user()

    if not user:
        return jsonify({"error": "unauthorized", "reason": "no_user"}), 401

    if not getattr(user, 'has_subscription', False):
        return jsonify({"error": "Эта функция доступна только по подписке.", "subscription_required": True}), 403

    file = request.files.get('file')
    if not file:
        return jsonify({"error": "Файл не найден"}), 400

    # ... (код сохранения файла) ...
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        with open(filepath, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')

        # --- ИЗМЕНЕННЫЙ ПРОМПТ ---
        tmpl = PromptTemplate.query.filter_by(name='meal_photo', is_active=True) \
            .order_by(PromptTemplate.version.desc()).first()

        system_prompt = (tmpl.body if tmpl else
                         "Ты — профессиональный диетолог. Проанализируй фото еды. Определи:"
                         "\n- Каллорий должен быть максимально реалистичным, ..., 500. А числа в которые хочется верить что то вроде 370, 420.."
                         "\n- Название блюда (в поле 'name')."
                         "\n- Калорийность, Белки, Жиры, Углеводы (в полях 'calories', 'protein', 'fat', 'carbs')."
                         "\n- Дай подробный текстовый анализ блюда (в поле 'analysis')."
                         "\n- Сделай краткий вывод: насколько блюдо полезно или вредно для диеты (в поле 'verdict')."
                         '\nВерни JSON СТРОГО в формате: {"name": "...", "cal... "fat": 0.0, "carbs": 0.0, "analysis": "...", "verdict": "..."}'
                         )

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "Проанализируй блюдо на фото."}
                ]}
            ],
            max_tokens=500,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content.strip()
        data = json.loads(content)

        return jsonify(data)

    except Exception as e:
        return jsonify({"error": f"Ошибка анализа фото: {e}"}), 500

@app.route('/api/subscription/status')
def subscription_status():
    chat_id = request.args.get('chat_id')
    user = None
    if chat_id:
        user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    else:
        user = get_current_user()

    if not user:
        return jsonify({"ok": False, "reason": "no_user"}), 401

    return jsonify({"ok": True, "has_subscription": bool(getattr(user, 'has_subscription', False))})

@app.route('/api/trainings/my')
def api_trainings_my():
    # Фильтрация по локальному времени Алматы, поддержка start_time как datetime *и* как time (+ отдельная дата)
    from zoneinfo import ZoneInfo
    from datetime import datetime, date, time as dt_time

    chat_id = request.args.get('chat_id')
    user = None
    if chat_id:
        user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    else:
        user = get_current_user()

    if not user:
        return jsonify({"ok": False, "reason": "no_user"}), 401

    tz_almaty = ZoneInfo("Asia/Almaty")
    tz_utc = ZoneInfo("UTC")
    now_local = datetime.now(tz_almaty)

    # Берём список тренировок пользователя без фильтра по времени (типы полей могут отличаться),
    # дальше фильтруем и сортируем в Python.
    q = (
        db.session.query(Training)
        .join(TrainingSignup, TrainingSignup.training_id == Training.id)
        .filter(TrainingSignup.user_id == user.id)
        .limit(200)
    )

    entries = []
    for t in q.all():
        # Поля времени/даты могут называться по-разному
        start_field = getattr(t, 'start_time', None)
        date_field = (
            getattr(t, 'start_date', None)
            or getattr(t, 'date', None)
            or getattr(t, 'day', None)
        )

        if not start_field:
            continue

        # Собираем локальный datetime Алматы
        local_dt = None
        if isinstance(start_field, datetime):
            local_dt = start_field if start_field.tzinfo else start_field.replace(tzinfo=tz_almaty)
        elif isinstance(start_field, dt_time):
            # Нужна дата: пробуем взять из date_field
            if date_field:
                if isinstance(date_field, datetime):
                    d = date_field.date()
                elif isinstance(date_field, date):
                    d = date_field
                else:
                    d = None
                if d is not None:
                    local_dt = datetime.combine(d, start_field).replace(tzinfo=tz_almaty)
        elif isinstance(start_field, date):
            # Редкий случай: есть только дата — считаем 00:00
            local_dt = datetime.combine(start_field, dt_time(0, 0)).replace(tzinfo=tz_almaty)

        if not local_dt:
            # Не смогли восстановить полный datetime — пропускаем
            continue

        # Отфильтруем прошедшие
        if local_dt < now_local:
            continue

        start_utc = local_dt.astimezone(tz_utc)

        entries.append({
            "id": t.id,
            "title": getattr(t, 'title', getattr(t, 'name', 'Тренировка')),
            "start_utc": start_utc,  # для сортировки
            "start_time": start_utc.isoformat().replace("+00:00", "Z"),
            "location": getattr(t, 'location', None),
        })

    # Сортируем по времени начала
    entries.sort(key=lambda x: (x["start_utc"], x["id"]))
    # Возвращаем первые 50
    items = [{k: v for k, v in e.items() if k != "start_utc"} for e in entries[:50]]

    return jsonify({"ok": True, "items": items})

@app.route('/api/meals/today/<int:chat_id>')
def get_today_meals_api(chat_id):
    # Находим пользователя по ID чата в телеграме
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first_or_404()

    # Ищем все записи о приемах пищи для этого пользователя за сегодня
    logs = MealLog.query.filter_by(user_id=user.id, date=date.today()).order_by(MealLog.created_at).all()

    # Считаем итоговые калории
    total_calories = sum(m.calories for m in logs)

    # Формируем данные для ответа
    meal_data = [
        {
            'meal_type': m.meal_type,
            'name': m.name or "Без названия",
            'calories': m.calories,
            'protein': m.protein,
            'fat': m.fat,
            'carbs': m.carbs
        }
        for m in logs
    ]

    return jsonify({"meals": meal_data, "total_calories": total_calories}), 200




@app.route('/metrics')
@login_required
def metrics():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)
    latest_analysis = BodyAnalysis.query.filter_by(user_id=user_id).order_by(BodyAnalysis.timestamp.desc()).first()

    # 1) Суммарные калории по приёмам пищи за сегодня
    total_meals = db.session.query(func.sum(MealLog.calories)) \
                      .filter_by(user_id=user.id, date=date.today()) \
                      .scalar() or 0

    # Получаем список приёмов пищи
    today_meals = MealLog.query \
        .filter_by(user_id=user.id, date=date.today()) \
        .all()

    # 2) Базовый метаболизм из последнего замера
    metabolism = latest_analysis.metabolism if latest_analysis else 0

    # 3) Активная калорийность
    activity = Activity.query.filter_by(user_id=user.id, date=date.today()).first()
    active_kcal = activity.active_kcal if activity else None
    steps = activity.steps if activity else None
    distance_km = activity.distance_km if activity else None
    resting_kcal = activity.resting_kcal if activity else None

    # Проверяем данные
    missing_meals = (total_meals == 0)
    missing_activity = (active_kcal is None)

    # 4) Дефицит
    deficit = None
    if not missing_meals and not missing_activity and metabolism is not None:
        deficit = (metabolism + active_kcal) - total_meals

    return render_template(
        'profile.html',
        user=user,
        age=calculate_age(user.date_of_birth) if user.date_of_birth else None,
        # для табов профиля и активности
        diet=Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).first(),
        today_activity=activity,
        latest_analysis=latest_analysis,
        previous_analysis=BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).offset(
            1).first(),
        chart_data=None,  # Отключаем для этой страницы, если не нужно

        # новые переменные для metrics
        total_meals=total_meals,
        today_meals=today_meals,
        metabolism=metabolism,
        active_kcal=active_kcal,
        steps=steps,
        distance_km=distance_km,
        resting_kcal=resting_kcal,
        deficit=deficit,
        missing_meals=missing_meals,
        missing_activity=missing_activity,
        tab='metrics'  # Указываем активный таб
    )


@app.route('/api/registered_chats')
def registered_chats():
    """Возвращает список всех телеграм‑chat_id, которые привязаны к пользователям."""
    chats = (
        db.session.query(User.telegram_chat_id)
        .filter(User.telegram_chat_id.isnot(None))
        .all()
    )
    # chats — список кортежей, поэтому разбираем
    chat_ids = [c[0] for c in chats]
    return jsonify({"chat_ids": chat_ids})


# ---------------- ADMIN PANEL ----------------

@app.route("/admin")
@admin_required  # Защита маршрута для админа
def admin_dashboard():
    users = User.query.order_by(User.id).all()  # Order by ID for stable display
    today = date.today()

    statuses = {}
    details = {}

    # Define metrics consistent with profile.html
    metrics_def = [
        ('Рост', 'height', '📏', 'см', True),
        ('Вес', 'weight', '⚖️', 'кг', False),
        ('Мышцы', 'muscle_mass', '💪', 'кг', True),
        ('Жир', 'fat_mass', '🧈', 'кг', False),
        ('Вода', 'body_water', '💧', '%', True),
        ('Метаболизм', 'metabolism', '⚡', 'ккал', True),
        ('Белок', 'protein_percentage', '🥚', '%', True),
        ('Висц. жир', 'visceral_fat_rating', '🔥', '', False),
        ('ИМТ', 'bmi', '📐', '', False),
    ]

    for u in users:
        # statuses
        has_meal = MealLog.query.filter_by(user_id=u.id, date=today).count() > 0
        has_activity = Activity.query.filter_by(user_id=u.id, date=today).count() > 0
        statuses[u.id] = {
            'meal': has_meal,
            'activity': has_activity,
            'subscription_active': u.has_subscription  # Проверяем наличие активной подписки
        }
        # meals
        meals = MealLog.query.filter_by(user_id=u.id, date=today).all()
        meals_data = [{
            'type': m.meal_type,
            'cal': m.calories,
            'prot': m.protein,
            'fat': m.fat,
            'carbs': m.carbs
        } for m in meals]

        # activity
        act = Activity.query.filter_by(user_id=u.id, date=today).first()
        activity_data = None
        if act:
            activity_data = {
                'steps': act.steps,
                'active_kcal': act.active_kcal,
                'resting_kcal': act.resting_kcal,
                'distance_km': act.distance_km,
                'hr_avg': act.heart_rate_avg
            }

        # body analysis
        last = BodyAnalysis.query.filter_by(user_id=u.id) \
            .order_by(BodyAnalysis.timestamp.desc()).first()
        prev = BodyAnalysis.query.filter_by(user_id=u.id) \
            .order_by(BodyAnalysis.timestamp.desc()).offset(1).first()

        # metrics with deltas
        metrics = []
        for label, field, icon, unit, good_up in metrics_def:
            cur = getattr(last, field, None)
            pr = getattr(prev, field, None)
            diff = pct = arrow = None
            is_good = None
            if cur is not None and pr is not None:
                diff = cur - pr
                if pr != 0:
                    pct = diff / pr * 100
                arrow = '↑' if diff > 0 else '↓' if diff < 0 else ''
                # Handle cases where diff is 0 for arrow display
                if diff == 0:
                    arrow = ''  # No arrow for no change
                    is_good = True  # Can consider no change as good/neutral
                else:
                    is_good = (diff > 0 and good_up) or (diff < 0 and not good_up)
            metrics.append({
                'label': label,
                'icon': icon,
                'unit': unit,
                'cur': cur,
                'diff': diff,
                'pct': pct,
                'arrow': arrow,
                'is_good': is_good
            })

        details[u.id] = {
            'meals': meals_data,
            'activity': activity_data,
            'metrics': metrics
        }

    return render_template(
        "admin_dashboard.html",
        users=users,
        statuses=statuses,
        details=details,
        today=today
    )


@app.route("/admin/user/create", methods=["GET", "POST"])
@admin_required
def admin_create_user():
    errors = []
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        date_str = request.form.get('date_of_birth', '').strip()
        is_trainer = 'is_trainer' in request.form

        if not name:
            errors.append("Имя обязательно.")
        if not email:
            errors.append("Email обязателен.")
        if not password or len(password) < 6:
            errors.append("Пароль обязателен и должен содержать минимум 6 символов.")
        if User.query.filter_by(email=email).first():
            errors.append("Этот email уже зарегистрирован.")

        date_of_birth = None
        if date_str:
            try:
                date_of_birth = datetime.strptime(date_str, "%Y-%m-%d").date()
                if date_of_birth > date.today():
                    errors.append("Дата рождения не может быть в будущем.")
            except ValueError:
                errors.append("Некорректный формат даты рождения.")

        if errors:
            return render_template('admin_create_user.html', errors=errors, form_data=request.form)

        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        new_user = User(
            name=name,
            email=email,
            password=hashed_pw,
            date_of_birth=date_of_birth,
            is_trainer=is_trainer
        )
        db.session.add(new_user)
        db.session.commit()
        flash(f"Пользователь '{new_user.name}' успешно создан!", "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_create_user.html", errors=errors, form_data={})


@app.route("/admin/user/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("admin_dashboard"))

    # Fetch all historical data for the user
    meal_logs = MealLog.query.filter_by(user_id=user.id).order_by(MealLog.date.desc()).all()
    activities = Activity.query.filter_by(user_id=user.id).order_by(Activity.date.desc()).all()
    body_analyses = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).all()
    diets = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).all()

    # Determine current status for today
    today = date.today()
    has_meal_today = any(m.date == today for m in meal_logs)
    has_activity_today = any(a.date == today for a in activities)

    # For charts: last 30 days activity
    last_30_days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    activity_chart_labels = [d.strftime("%d.%m") for d in last_30_days]
    activity_steps_values = []
    activity_kcal_values = []

    activity_map = {a.date: a for a in activities if a.date in last_30_days}  # optimize lookup
    for d in last_30_days:
        activity_for_day = activity_map.get(d)
        activity_steps_values.append(activity_for_day.steps if activity_for_day else 0)
        activity_kcal_values.append(activity_for_day.active_kcal if activity_for_day else 0)

    # For charts: last 30 days diet (calories)
    diet_chart_labels = [d.strftime("%d.%m") for d in last_30_days]
    diet_kcal_values = []

    diet_map = {d.date: d for d in diets if d.date in last_30_days}  # optimize lookup
    for d in last_30_days:
        diet_for_day = diet_map.get(d)
        diet_kcal_values.append(diet_for_day.total_kcal if diet_for_day else 0)

    return render_template(
        "admin_user_detail.html",
        user=user,
        meal_logs=meal_logs,
        activities=activities,
        body_analyses=body_analyses,
        diets=diets,
        has_meal_today=has_meal_today,
        has_activity_today=has_activity_today,
        # Chart data
        activity_chart_labels=json.dumps(activity_chart_labels),
        activity_steps_values=json.dumps(activity_steps_values),
        activity_kcal_values=json.dumps(activity_kcal_values),
        diet_chart_labels=json.dumps(diet_chart_labels),
        diet_kcal_values=json.dumps(diet_kcal_values),
        today=today
    )


@app.route("/admin/user/<int:user_id>/edit", methods=["POST"])
@admin_required
def admin_user_edit(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("admin_dashboard"))

    original_email = user.email  # Keep original email for unique check

    user.name = request.form["name"].strip()
    user.email = request.form["email"].strip()
    user.is_trainer = 'is_trainer' in request.form  # Update trainer status

    dob = request.form.get("date_of_birth")
    user.date_of_birth = datetime.strptime(dob, "%Y-%m-%d").date() if dob else None

    # Handle password change if provided
    new_password = request.form.get("password")
    if new_password:
        if len(new_password) < 6:
            flash("Новый пароль должен быть не менее 6 символов.", "error")
            return redirect(url_for("admin_user_detail", user_id=user.id))
        user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')

    # Check for duplicate email only if changed
    if user.email != original_email and User.query.filter_by(email=user.email).first():
        flash("Этот email уже занят другим пользователем.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    # Handle avatar upload
    if 'avatar' in request.files:
        file = request.files['avatar']
        if file.filename != '':
            filename = secure_filename(file.filename)
            # You might want to delete the old avatar file here if it exists
            # os.remove(os.path.join(app.config['UPLOAD_FOLDER'], user.avatar))
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            user.avatar = filename

    try:
        db.session.commit()
        flash("Данные пользователя обновлены", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Ошибка при обновлении пользователя. Возможно, email уже используется.", "error")

    return redirect(url_for("admin_user_detail", user_id=user.id))


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        # === 0) Если у пользователя есть собственная группа — чистим всё, что к ней привязано
        if getattr(user, "own_group", None):
            gid = user.own_group.id

            # реакции к сообщениям группы
            msg_ids = [row[0] for row in db.session.query(GroupMessage.id).filter_by(group_id=gid).all()]
            if msg_ids:
                MessageReaction.query.filter(MessageReaction.message_id.in_(msg_ids))\
                                     .delete(synchronize_session=False)
            # сообщения группы
            GroupMessage.query.filter_by(group_id=gid).delete(synchronize_session=False)
            # задачи/объявления группы
            GroupTask.query.filter_by(group_id=gid).delete(synchronize_session=False)
            # участники группы
            GroupMember.query.filter_by(group_id=gid).delete(synchronize_session=False)
            # сама группа
            db.session.delete(user.own_group)

        # === 1) Членства пользователя в чужих группах
        GroupMember.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 2) Сообщения пользователя и реакции на них
        user_msg_ids = [row[0] for row in db.session.query(GroupMessage.id).filter_by(user_id=user.id).all()]
        if user_msg_ids:
            MessageReaction.query.filter(MessageReaction.message_id.in_(user_msg_ids))\
                                 .delete(synchronize_session=False)
        GroupMessage.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 3) Реакции, поставленные пользователем
        MessageReaction.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 4) Тренировки, где он тренер, и записи на них
        trainer_tids = [row[0] for row in db.session.query(Training.id).filter_by(trainer_id=user.id).all()]
        if trainer_tids:
            TrainingSignup.query.filter(TrainingSignup.training_id.in_(trainer_tids))\
                                .delete(synchronize_session=False)
            Training.query.filter(Training.id.in_(trainer_tids)).delete(synchronize_session=False)

        # === 5) Записи пользователя на тренировки
        TrainingSignup.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 6) Пищевые логи / активность / анализы / диеты / логи напоминаний
        MealReminderLog.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        MealLog.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Activity.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        BodyAnalysis.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Diet.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 7) Подписки / заказы / настройки
        Subscription.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Order.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        UserSettings.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # === 8) Наконец, сам пользователь
        db.session.delete(user)
        db.session.commit()
        flash(f"Пользователь '{user.name}' и все связанные данные удалены.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Ошибка при удалении пользователя: {e}", "error")

    return redirect(url_for("admin_dashboard"))


@app.route('/groups')
@login_required
def groups_list():
    if not get_current_user().has_subscription:
        flash("Доступ к группам и сообществу открыт только по подписке.", "warning")
        return redirect(url_for('profile'))
    user = get_current_user()
    # если тренер — показываем его группу (или кнопку создания)
    if user.is_trainer:
        return render_template('groups_list.html', group=user.own_group)
    # обычный пользователь — список всех групп
    groups = Group.query.all()
    return render_template('groups_list.html', groups=groups)


@app.route('/groups/new', methods=['GET', 'POST'])
@login_required
def create_group():
    user = get_current_user()
    if not user.is_trainer:
        abort(403)
    if user.own_group:
        flash("Вы уже являетесь тренером группы. Вы можете создать только одну группу.", "warning")
        return redirect(url_for('group_detail', group_id=user.own_group.id))
    if request.method == 'POST':
        name = request.form['name']
        description = request.form.get('description', '').strip()
        if not name:
            flash("Название группы обязательно!", "error")
            return render_template('group_new.html')

        group = Group(name=name, description=description, trainer=user)
        db.session.add(group)
        db.session.commit()
        flash(f"Группа '{group.name}' успешно создана!", "success")
        return redirect(url_for('group_detail', group_id=group.id))
    return render_template('group_new.html')


@app.route('/groups/<int:group_id>')
@login_required

def group_detail(group_id):
    # Ваша проверка подписки здесь
    if not get_current_user().has_subscription:
        flash("Доступ к группам и сообществу открыт только по подписке.", "warning")
        # ИСПРАВЛЕНИЕ: Добавьте эту строку
        return redirect(url_for('profile'))

    group = Group.query.get_or_404(group_id)
    user = get_current_user()
    is_member = any(m.user_id == user.id for m in group.members)

    raw_messages = GroupMessage.query.filter_by(group_id=group.id).order_by(GroupMessage.timestamp.desc()).all()
    processed_messages = []
    last_sender_id = None
    for message in raw_messages:
        show_avatar = (message.user_id != last_sender_id)
        processed_messages.append({
            'id': message.id, 'group_id': message.group_id, 'user_id': message.user_id, 'user': message.user,
            'text': message.text, 'timestamp': message.timestamp, 'image_file': message.image_file,
            'reactions': message.reactions, 'show_avatar': show_avatar, 'is_current_user': (message.user_id == user.id)
        })
        last_sender_id = message.user_id
    all_posts = GroupTask.query.filter_by(group_id=group.id).order_by(GroupTask.created_at.desc()).all()

    group_member_stats = []
    if user.is_trainer and group.trainer_id == user.id:
        today = date.today()
        all_relevant_members = [m.user for m in group.members]
        if group.trainer not in all_relevant_members and group.trainer.email != ADMIN_EMAIL:
            all_relevant_members.append(group.trainer)

        for member_user in all_relevant_members:
            if member_user.email == ADMIN_EMAIL and not any(m.user_id == member_user.id for m in group.members):
                continue

            latest_analysis = BodyAnalysis.query.filter_by(user_id=member_user.id).order_by(
                BodyAnalysis.timestamp.desc()).first()

            fat_loss_progress = None
            if latest_analysis and member_user.fat_mass_goal and latest_analysis.fat_mass > member_user.fat_mass_goal:
                start_datetime = latest_analysis.timestamp

                meal_data = db.session.query(MealLog.date, func.sum(MealLog.calories)).filter(
                    MealLog.user_id == member_user.id, MealLog.date >= start_datetime.date()
                ).group_by(MealLog.date).all()
                meal_map = dict(meal_data)

                activity_data = db.session.query(Activity.date, Activity.active_kcal).filter(
                    Activity.user_id == member_user.id, Activity.date >= start_datetime.date()
                ).all()
                activity_map = dict(activity_data)

                member_metabolism = latest_analysis.metabolism or 0
                total_accumulated_deficit = 0
                delta_days = (today - start_datetime.date()).days

                if delta_days >= 0:
                    for i in range(delta_days + 1):
                        current_day = start_datetime.date() + timedelta(days=i)
                        consumed = meal_map.get(current_day, 0)
                        burned_active = activity_map.get(current_day, 0)

                        # --- ИЗМЕНЕНИЯ ЗДЕСЬ ---
                        if i == 0:  # Это день анализа
                            # Убираем калории, съеденные ДО замера
                            calories_before_analysis = db.session.query(func.sum(MealLog.calories)).filter(
                                MealLog.user_id == member_user.id,
                                MealLog.date == current_day,
                                MealLog.created_at < start_datetime
                            ).scalar() or 0
                            consumed -= calories_before_analysis
                            # Игнорируем активность за день замера, т.к. нет точного времени
                            burned_active = 0
                        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

                        daily_deficit = (member_metabolism + burned_active) - consumed
                        if daily_deficit > 0:
                            total_accumulated_deficit += daily_deficit

                KCAL_PER_KG_FAT = 7700
                total_fat_to_lose_kg = latest_analysis.fat_mass - member_user.fat_mass_goal

                estimated_fat_burned_kg = min(total_accumulated_deficit / KCAL_PER_KG_FAT, total_fat_to_lose_kg)

                percentage = 0
                if total_fat_to_lose_kg > 0:
                    percentage = (estimated_fat_burned_kg / total_fat_to_lose_kg) * 100

                fat_loss_progress = {
                    'percentage': min(100, max(0, percentage)),
                    'initial_kg': latest_analysis.fat_mass,
                    'goal_kg': member_user.fat_mass_goal,
                    'current_kg': latest_analysis.fat_mass - estimated_fat_burned_kg
                }

            group_member_stats.append({
                'user': member_user,
                'fat_loss_progress': fat_loss_progress,
                'is_trainer_in_group': (member_user.id == group.trainer_id)
            })
        group_member_stats.sort(key=lambda x: (not x['is_trainer_in_group'], x['user'].name.lower()))

    return render_template('group_detail.html',
                           group=group,
                           is_member=is_member,
                           processed_messages=processed_messages,
                           group_member_stats=group_member_stats,
                           all_posts=all_posts)

@app.route('/group_message/<int:message_id>/react', methods=['POST'])
@login_required
def react_to_message(message_id):
    message = GroupMessage.query.get_or_404(message_id)
    user = get_current_user()

    existing_reaction = MessageReaction.query.filter_by(
        message_id=message_id,
        user_id=user.id
    ).first()

    user_reacted = False
    if existing_reaction:
        db.session.delete(existing_reaction)
    else:
        reaction = MessageReaction(message=message, user=user, reaction_type='👍')
        db.session.add(reaction)
        user_reacted = True

    db.session.commit()

    new_like_count = MessageReaction.query.filter_by(message_id=message_id).count()

    return jsonify({
        "success": True,
        "new_like_count": new_like_count,
        "user_reacted": user_reacted
    })


@app.route('/api/groups/<int:group_id>/messages')
@login_required
def get_group_messages(group_id):
    # Убедимся, что группа существует
    Group.query.get_or_404(group_id)
    user_id = get_current_user().id

    messages = GroupMessage.query.filter_by(group_id=group_id).order_by(GroupMessage.timestamp.asc()).all()

    # Собираем данные в нужный формат
    results = []
    for msg in messages:
        reactions_data = []
        user_has_reacted = False
        for reaction in msg.reactions:
            reactions_data.append({'user_id': reaction.user_id})
            if reaction.user_id == user_id:
                user_has_reacted = True

        results.append({
            "id": msg.id,
            "text": msg.text,
            "image_url": url_for('serve_file', filename=msg.image_file) if msg.image_file else None,
            "user": {
                "name": msg.user.name,
                "avatar_url": url_for('serve_file', filename=msg.user.avatar.filename) if msg.user.avatar else url_for(
                    'static', filename='default-avatar.png')
            },
            "is_current_user": msg.user_id == user_id,
            "reactions_count": len(reactions_data),
            "current_user_reacted": user_has_reacted
        })
    return jsonify(results)

@app.route('/groups/<int:group_id>/tasks/new', methods=['POST'])
@login_required
def create_group_task(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()

    # Only the group's trainer can create tasks/announcements
    if not (user.is_trainer and group.trainer_id == user.id):
        abort(403)

    title = request.form['title'].strip()
    description = request.form.get('description', '').strip()
    is_announcement = 'is_announcement' in request.form
    due_date_str = request.form.get('due_date')

    if not title:
        flash("Заголовок обязателен.", "error")
        return redirect(url_for('group_detail', group_id=group_id))

    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash("Неверный формат даты. Используйте ГГГГ-ММ-ДД.", "error")
            return redirect(url_for('group_detail', group_id=group_id))

    task = GroupTask(
        group=group,
        trainer=user,
        title=title,
        description=description,
        is_announcement=is_announcement,
        due_date=due_date
    )
    db.session.add(task)
    db.session.commit()  # Сначала сохраняем задачу

    # --- НАЧАЛО НОВОГО КОДА ---
    try:
        # Собираем chat_id всех участников группы
        chat_ids = [member.user.telegram_chat_id for member in group.members if member.user.telegram_chat_id]

        if chat_ids:
            # Формируем сообщение
            task_type = "Объявление" if is_announcement else "Новая задача"
            message_text = f"🔔 **{task_type} от тренера {user.name}**\n\n**{title}**\n\n_{description}_"

            # URL вашего бота (нужно будет указать, когда бот будет на сервере)
            BOT_WEBHOOK_URL = os.getenv("BOT_WEBHOOK_URL")  # Например, [https://your-bot-domain.com/notify](https://your-bot-domain.com/notify)
            BOT_SECRET_TOKEN = os.getenv("BOT_SECRET_TOKEN")  # Секретный токен для безопасности

            if BOT_WEBHOOK_URL and BOT_SECRET_TOKEN:
                payload = {
                    "chat_ids": chat_ids,
                    "message": message_text,
                    "secret": BOT_SECRET_TOKEN
                }
                # Отправляем запрос боту, не дожидаясь ответа
                print(f"INFO: Sending notification to bot at {BOT_WEBHOOK_URL} for {len(chat_ids)} users.")
                requests.post(BOT_WEBHOOK_URL, json=payload, timeout=2)
            else:
                print("WARNING: BOT_WEBHOOK_URL or BOT_SECRET_TOKEN not set in .env. Skipping notification.")

    except Exception as e:
        print(f"Failed to send notification to bot: {e}")
    # --- КОНЕЦ НОВОГО КОДА ---

    flash(f"{'Объявление' if is_announcement else 'Задача'} '{title}' успешно добавлено!", "success")
    return redirect(url_for('group_detail', group_id=group_id))


# Добавьте в app.py
@app.route('/api/user_progress/<int:chat_id>')
def get_user_progress(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first_or_404()

    analyses = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).limit(2).all()

    if len(analyses) == 0:
        return jsonify({"error": "Нет данных для сравнения"}), 404

    latest = analyses[0]
    previous = analyses[1] if len(analyses) > 1 else None

    def serialize(analysis):
        if not analysis: return None
        return {
            "date": analysis.timestamp.strftime('%d.%m.%Y'),
            "weight": analysis.weight,
            "fat_mass": analysis.fat_mass,
            "muscle_mass": analysis.muscle_mass
        }

    return jsonify({
        "latest": serialize(latest),
        "previous": serialize(previous)
    })

# Добавьте в app.py

@app.route('/api/meal_history/<int:chat_id>')
def get_meal_history(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first_or_404()
    page = request.args.get('page', 1, type=int)

    # Группируем приемы пищи по дням и считаем сумму калорий
    daily_meals = db.session.query(
        MealLog.date,
        func.sum(MealLog.calories).label('total_calories'),
        func.count(MealLog.id).label('meal_count')
    ).filter_by(user_id=user.id).group_by(MealLog.date).order_by(MealLog.date.desc()).paginate(page=page, per_page=5, error_out=False)

    return jsonify({
        "days": [
            {"date": d.date.strftime('%d.%m.%Y'), "total_calories": d.total_calories, "meal_count": d.meal_count}
            for d in daily_meals.items
        ],
        "has_next": daily_meals.has_next,
        "has_prev": daily_meals.has_prev,
        "page": page
    })

@app.route('/api/activity_history/<int:chat_id>')
def get_activity_history(chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first_or_404()
    page = request.args.get('page', 1, type=int)

    daily_activity = Activity.query.filter_by(user_id=user.id).order_by(Activity.date.desc()).paginate(page=page, per_page=5, error_out=False)

    return jsonify({
        "days": [
            {"date": a.date.strftime('%d.%m.%Y'), "steps": a.steps, "active_kcal": a.active_kcal}
            for a in daily_activity.items
        ],
        "has_next": daily_activity.has_next,
        "has_prev": daily_activity.has_prev,
        "page": page
    })

@app.route('/groups/tasks/<int:task_id>/delete', methods=['POST'])
@login_required
def delete_group_task(task_id):
    task = GroupTask.query.get_or_404(task_id)
    user = get_current_user()

    # Only the trainer who created it (or group's trainer) can delete
    if not (user.is_trainer and task.trainer_id == user.id):
        abort(403)

    db.session.delete(task)
    db.session.commit()
    flash(f"{'Объявление' if task.is_announcement else 'Задача'} '{task.title}' удалено.", "info")
    return redirect(url_for('group_detail', group_id=task.group_id))


# Route for handling image uploads with chat messages
@app.route('/groups/<int:group_id>/message/image', methods=['POST'])
@login_required
def post_group_image_message(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()
    is_member = any(m.user_id == user.id for m in group.members)

    if not (user.is_trainer and group.trainer_id == user.id or is_member):
        abort(403)

    text = request.form.get('text', '').strip()
    file = request.files.get('image')

    image_filename = None
    if file and file.filename != '':
        unique_filename = f"chat_{group_id}_{uuid.uuid4().hex}.png"
        image_data = file.read()

        output_buffer = BytesIO()
        with Image.open(BytesIO(image_data)) as img:
            img.thumbnail(CHAT_IMAGE_MAX_SIZE, Image.Resampling.LANCZOS)
            img.save(output_buffer, format="PNG")
        resized_data = output_buffer.getvalue()

        new_file = UploadedFile(
            filename=unique_filename,
            content_type='image/png',
            data=resized_data,
            size=len(resized_data),
            user_id=user.id
        )
        db.session.add(new_file)
        image_filename = unique_filename

    if not text and not image_filename:
        return jsonify({"error": "Сообщение не может быть пустым"}), 400

    msg = GroupMessage(group=group, user=user, text=text, image_file=image_filename)
    db.session.add(msg)
    db.session.commit()

    # Вместо редиректа возвращаем JSON с данными нового сообщения
    return jsonify({
        "success": True,
        "message": {
            "id": msg.id,
            "text": msg.text,
            "image_url": url_for('serve_file', filename=msg.image_file) if msg.image_file else None,
            "user": {
                "name": user.name,
                "avatar_url": url_for('serve_file', filename=user.avatar.filename) if user.avatar else url_for('static',
                                                                                                               filename='default-avatar.png')
            },
            "is_current_user": True,
            "reactions": []
        }
    })

@app.route('/groups/<int:group_id>/join', methods=['POST'])
@login_required
def join_group(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()

    # Prevent joining if already a member
    if GroupMember.query.filter_by(group_id=group.id, user_id=user.id).first():
        flash("Вы уже состоите в этой группе.", "info")
        return redirect(url_for('group_detail', group_id=group.id))

    # Prevent trainer from joining another group as a member
    if user.is_trainer and user.own_group and user.own_group.id != group_id:
        flash("Как тренер, вы не можете присоединиться к другой группе.", "error")
        return redirect(url_for('groups_list'))

    member = GroupMember(group=group, user=user)
    db.session.add(member)
    db.session.commit()
    flash(f"Вы успешно присоединились к группе '{group.name}'!", "success")
    return redirect(url_for('group_detail', group_id=group.id))


@app.route('/groups/<int:group_id>/leave', methods=['POST'])
@login_required
def leave_group(group_id):
    group = Group.query.get_or_404(group_id)
    user = get_current_user()

    member = GroupMember.query.filter_by(group_id=group.id, user_id=user.id).first()
    if not member:
        flash("Вы не состоите в этой группе.", "info")
        return redirect(url_for('group_detail', group_id=group_id))

    # Prevent trainers from leaving their own group if they are the trainer
    if user.is_trainer and group.trainer_id == user.id:
        flash("Как тренер, вы не можете покинуть свою собственную группу.", "error")
        return redirect(url_for('group_detail', group_id=group_id))

    db.session.delete(member)
    db.session.commit()
    flash(f"Вы покинули группу '{group.name}'.", "success")
    return redirect(url_for('groups_list'))


# --- Admin Group Management ---

@app.route("/admin/groups")
@admin_required
def admin_groups_list():
    groups = Group.query.all()
    return render_template("admin_groups_list.html", groups=groups)


@app.route("/admin/groups/<int:group_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_group(group_id):
    group = db.session.get(Group, group_id)
    if not group:
        flash("Группа не найдена.", "error")
        return redirect(url_for("admin_groups_list"))

    trainers = User.query.filter_by(is_trainer=True).all()  # For assigning new trainer

    if request.method == "POST":
        group.name = request.form['name'].strip()
        group.description = request.form.get('description', '').strip()
        new_trainer_id = request.form.get('trainer_id')

        # Check for unique group name (if you want to enforce this)
        # existing_group = Group.query.filter(Group.name == group.name, Group.id != group_id).first()
        # if existing_group:
        #     flash("Группа с таким названием уже существует.", "error")
        #     return render_template("admin_edit_group.html", group=group, trainers=trainers)

        if new_trainer_id and int(new_trainer_id) != group.trainer_id:
            # Check if new trainer already owns a group
            potential_trainer = db.session.get(User, int(new_trainer_id))
            if potential_trainer and potential_trainer.own_group and potential_trainer.own_group.id != group_id:
                flash(f"Тренер {potential_trainer.name} уже руководит другой группой.", "error")
                return render_template("admin_edit_group.html", group=group, trainers=trainers)
            group.trainer_id = int(new_trainer_id)
            group.trainer.is_trainer = True  # Ensure new trainer is marked as trainer

        db.session.commit()
        flash("Группа успешно обновлена.", "success")
        return redirect(url_for("admin_groups_list"))

    return render_template("admin_edit_group.html", group=group, trainers=trainers)


@app.route("/admin/groups/<int:group_id>/delete", methods=["POST"])
@admin_required
def admin_delete_group(group_id):
    group = db.session.get(Group, group_id)
    if not group:
        flash("Группа не найдена.", "error")
        return redirect(url_for("admin_groups_list"))

    try:
        db.session.delete(group)  # Cascade will delete members, messages, tasks
        db.session.commit()
        flash(f"Группа '{group.name}' и все связанные данные удалены.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Ошибка при удалении группы: {e}", "error")
    return redirect(url_for("admin_groups_list"))


# Найдите и замените существующую функцию admin_grant_subscription

@app.route("/admin/user/<int:user_id>/subscribe", methods=["POST"])
@admin_required
def admin_grant_subscription(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("admin_dashboard"))

    duration = request.form.get('duration')
    if not duration:
        flash("Не выбран период подписки.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    today = date.today()
    end_date = None

    # Определяем дату окончания на основе выбора
    if duration == '1m':
        end_date = today + timedelta(days=30)
        message = "Подписка на 1 месяц успешно выдана!"
    elif duration == '3m':
        end_date = today + timedelta(days=90)
        message = "Подписка на 3 месяца успешно выдана!"
    elif duration == '6m':
        end_date = today + timedelta(days=180)
        message = "Подписка на 6 месяцев успешно выдана!"
    elif duration == '12m':
        end_date = today + timedelta(days=365)
        message = "Подписка на 1 год успешно выдана!"
    elif duration == 'unlimited':
        end_date = None  # None означает безлимитную подписку
        message = "Безлимитная подписка успешно выдана!"
    else:
        flash("Некорректный период подписки.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    existing_subscription = Subscription.query.filter_by(user_id=user.id).first()

    if existing_subscription:
        # Если подписка уже есть, обновляем её
        existing_subscription.start_date = today
        existing_subscription.end_date = end_date
        existing_subscription.source = 'admin_update'
    else:
        # Если подписки нет, создаем новую
        new_subscription = Subscription(
            user_id=user.id,
            start_date=today,
            end_date=end_date,
            source='admin_grant'
        )
        db.session.add(new_subscription)

    db.session.commit()
    flash(message, "success")
    return redirect(url_for("admin_user_detail", user_id=user.id))



@app.route("/admin/user/<int:user_id>/manage_subscription", methods=["POST"])
@admin_required
def manage_subscription(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("admin_dashboard"))

    action = request.form.get('action')
    sub = Subscription.query.filter_by(user_id=user_id).first()
    today = date.today()

    try:
        if action == 'grant':
            duration = request.form.get('duration')
            start_date_str = request.form.get('start_date')

            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else today

            end_date = None
            if duration == 'unlimited':
                end_date = None
            else:  # 1m, 3m, 6m, 12m
                months = {'1m': 1, '3m': 3, '6m': 6, '12m': 12}
                # Рассчитываем дельту от даты старта
                end_date = start_date + timedelta(days=30 * months.get(duration, 0))

            if sub:
                sub.start_date = start_date
                sub.end_date = end_date
                sub.status = 'active'
                sub.remaining_days_on_freeze = None
                flash("Подписка успешно обновлена.", "success")
            else:
                sub = Subscription(user_id=user.id, start_date=start_date, end_date=end_date, source='admin_grant')
                db.session.add(sub)
                flash("Подписка успешно выдана.", "success")

                # --- ДОБАВЬТЕ ЭТУ СТРОКУ ---
                # Устанавливаем флаг, чтобы показать пользователю приветствие
            user.show_welcome_popup = True
        elif action == 'remove':
            if sub:
                db.session.delete(sub)
                flash("Подписка успешно удалена.", "success")
            else:
                flash("У пользователя нет подписки для удаления.", "warning")

        elif action == 'freeze':
            if sub and sub.status == 'active' and sub.end_date:
                remaining = (sub.end_date - today).days
                sub.remaining_days_on_freeze = max(0, remaining)  # Сохраняем оставшиеся дни
                sub.status = 'frozen'
                flash(f"Подписка заморожена. Оставалось дней: {sub.remaining_days_on_freeze}", "success")
            else:
                flash("Невозможно заморозить: подписка неактивна, безлимитная или уже заморожена.", "warning")

        elif action == 'unfreeze':
            if sub and sub.status == 'frozen':
                days_to_add = sub.remaining_days_on_freeze or 0
                sub.end_date = today + timedelta(days=days_to_add)  # Восстанавливаем срок
                sub.status = 'active'
                sub.remaining_days_on_freeze = None
                flash(f"Подписка разморожена. Новая дата окончания: {sub.end_date.strftime('%d.%m.%Y')}", "success")
            else:
                flash("Подписка не была заморожена.", "warning")

        else:
            flash("Неизвестное действие.", "error")

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        flash(f"Произошла ошибка: {e}", "error")

    return redirect(url_for("admin_user_detail", user_id=user.id))

@app.route('/api/dismiss_welcome_popup', methods=['POST'])
@login_required
def dismiss_welcome_popup():
    """API-маршрут, который вызывается, когда пользователь закрывает приветственное окно."""
    user = get_current_user()
    if user:
        user.show_welcome_popup = False
        db.session.commit()
        return jsonify({'status': 'ok'}), 200
    return jsonify({'status': 'error', 'message': 'User not found'}), 404


# ... импорты datetime, date, timedelta должны быть вверху файла ...

@app.route('/subscription/manage', methods=['POST'])
@login_required
def manage_user_subscription():
    user = get_current_user()
    action = request.form.get('action')
    sub = user.subscription  # Получаем подписку текущего пользователя

    if not sub:
        flash("У вас нет активной подписки для управления.", "warning")
        return redirect(url_for('profile'))

    today = date.today()

    try:
        if action == 'freeze':
            if sub.status == 'active' and sub.end_date:
                remaining_days = (sub.end_date - today).days
                if remaining_days > 0:
                    sub.status = 'frozen'
                    sub.remaining_days_on_freeze = remaining_days
                    flash(f"Подписка успешно заморожена. Оставалось {remaining_days} дней.", "success")
                else:
                    flash("Срок действия подписки уже истёк, заморозка невозможна.", "warning")
            else:
                flash("Эту подписку невозможно заморозить.", "warning")

        elif action == 'unfreeze':
            if sub.status == 'frozen':
                days_to_add = sub.remaining_days_on_freeze or 0
                sub.end_date = today + timedelta(days=days_to_add)
                sub.status = 'active'
                sub.remaining_days_on_freeze = None
                flash(f"Подписка разморожена! Новая дата окончания: {sub.end_date.strftime('%d.%m.%Y')}", "success")
            else:
                flash("Подписка не была заморожена.", "warning")

        else:
            flash("Неизвестное действие.", "error")

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        flash(f"Произошла ошибка: {e}", "error")

    return redirect(url_for('profile'))


# ... другие маршруты

@app.route('/welcome-guide')
@login_required  # Только для залогиненных пользователей
def welcome_guide():
    # Убедимся, что у пользователя есть подписка, чтобы видеть эту страницу
    if not get_current_user().has_subscription:
        flash("Эта страница доступна только для пользователей с активной подпиской.", "warning")
        return redirect(url_for('profile'))

    return render_template('welcome_guide.html')


from sqlalchemy import text  # Убедитесь, что text импортирован из sqlalchemy


@app.route('/api/user/weekly_summary')
@login_required
def weekly_summary():
    if not get_current_user().has_subscription:
        return jsonify({"error": "Subscription required"}), 403

    user_id = session.get('user_id')
    today = date.today()
    week_ago = today - timedelta(days=6)

    labels = [(week_ago + timedelta(days=i)).strftime("%a") for i in range(7)]

    # 1. Данные по весу (здесь ошибки не было, код без изменений)
    from sqlalchemy import text  # у тебя уже импортирован

    weight_sql = text("""
        SELECT EXTRACT(DOW FROM timestamp) AS day_of_week, AVG(weight) AS avg_weight
        FROM body_analysis
        WHERE user_id = :user_id AND DATE(timestamp) BETWEEN :week_ago AND :today
        GROUP BY day_of_week
        ORDER BY day_of_week
    """)
    weight_data = db.session.execute(
        weight_sql, {"user_id": user_id, "week_ago": week_ago, "today": today}
    ).fetchall()

    # 2. Потребленные калории (сумма за каждый день)
    meals_sql = text("""
        SELECT date, SUM(calories) as total_calories FROM meal_logs 
        WHERE user_id = :user_id AND date BETWEEN :week_ago AND :today 
        GROUP BY date
    """)
    meal_logs = db.session.execute(meals_sql, {'user_id': user_id, 'week_ago': week_ago, 'today': today}).fetchall()

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # Убираем .strftime(), так как row.date уже является строкой 'YYYY-MM-DD'
    meals_map = {row.date: row.total_calories for row in meal_logs}

    # 3. Сожженные активные калории
    activity_sql = text("""
        SELECT date, active_kcal FROM activity 
        WHERE user_id = :user_id AND date BETWEEN :week_ago AND :today
    """)
    activities = db.session.execute(activity_sql, {'user_id': user_id, 'week_ago': week_ago, 'today': today}).fetchall()

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # То же самое: убираем .strftime()
    activity_map = {row.date: row.active_kcal for row in activities}

    # Собираем данные в массивы по дням
    weight_values = [
        next((w.avg_weight for w in weight_data if int(w.day_of_week) == (week_ago + timedelta(days=i)).weekday()),
             None) for i in range(7)]
    consumed_kcal_values = [meals_map.get((week_ago + timedelta(days=i)).strftime('%Y-%m-%d'), 0) for i in range(7)]
    burned_kcal_values = [activity_map.get((week_ago + timedelta(days=i)).strftime('%Y-%m-%d'), 0) for i in range(7)]

    return jsonify({
        "labels": labels,
        "datasets": {
            "weight": weight_values,
            "consumed_kcal": consumed_kcal_values,
            "burned_kcal": burned_kcal_values
        }
    })


@app.route('/api/user/deficit_history')
@login_required
def deficit_history():
    user = get_current_user()
    latest_analysis = user.latest_analysis

    if not (latest_analysis and latest_analysis.fat_mass and user.fat_mass_goal):
        return jsonify({"error": "Недостаточно данных для расчета истории дефицита."}), 404

    start_datetime = latest_analysis.timestamp
    today = date.today()

    # Запрашиваем все нужные данные за период одним разом
    meal_logs = MealLog.query.filter(
        MealLog.user_id == user.id,
        MealLog.date >= start_datetime.date()
    ).all()
    activity_logs = Activity.query.filter(
        Activity.user_id == user.id,
        Activity.date >= start_datetime.date()
    ).all()

    # --- НАЧАЛО ИЗМЕНЕНИЙ ---
    # Получаем все замеры тела за этот период
    body_analyses = BodyAnalysis.query.filter(
        BodyAnalysis.user_id == user.id,
        func.date(BodyAnalysis.timestamp) >= start_datetime.date()
    ).all()
    # Создаем set для быстрой проверки дат
    measurement_dates = {b.timestamp.date() for b in body_analyses}
    # --- КОНЕЦ ИЗМЕНЕНИЙ ---

    # Создаем словари для быстрого доступа
    meals_map = {}
    for log in meal_logs:
        if log.date not in meals_map:
            meals_map[log.date] = 0
        meals_map[log.date] += log.calories

    activity_map = {log.date: log.active_kcal for log in activity_logs}

    history_data = []
    metabolism = latest_analysis.metabolism or 0
    delta_days = (today - start_datetime.date()).days

    for i in range(delta_days + 1):
        current_day = start_datetime.date() + timedelta(days=i)
        consumed = meals_map.get(current_day, 0)
        burned_active = activity_map.get(current_day, 0)

        if i == 0:
            calories_before_analysis = db.session.query(func.sum(MealLog.calories)).filter(
                MealLog.user_id == user.id,
                MealLog.date == current_day,
                MealLog.created_at < start_datetime
            ).scalar() or 0
            consumed -= calories_before_analysis
            burned_active = 0

        total_burned = metabolism + burned_active
        daily_deficit = total_burned - consumed

        history_data.append({
            "date": current_day.strftime('%d.%m.%Y'),
            "consumed": consumed,
            "base_metabolism": metabolism,
            "burned_active": burned_active,
            "total_burned": total_burned,
            "deficit": daily_deficit if daily_deficit > 0 else 0,
            "is_measurement_day": current_day in measurement_dates  # <-- НОВЫЙ ФЛАГ
        })

    return jsonify(history_data)

@app.route("/purchase")
def purchase_page():
    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "kilograpptestbot")
    return render_template("purchase.html", bot_username=bot_username)


from sqlalchemy.exc import IntegrityError

@app.route('/api/trainings/<int:tid>/signup', methods=['POST'])
def signup_training(tid):
    u = get_current_user()
    if not u:
        abort(401)

    t = Training.query.get_or_404(tid)

    # Нельзя записываться на прошедшие
    now = datetime.now()
    if datetime.combine(t.date, t.end_time) <= now:
        abort(400, description="Тренировка уже прошла")

    # Проверка на лимит мест
    seats_taken = len(t.signups)
    capacity = t.capacity or 0
    already = TrainingSignup.query.filter_by(training_id=t.id, user_id=u.id).first()
    if not already and seats_taken >= capacity:
        abort(409, description="Нет свободных мест")

    if already:
        # Идемпотентность — просто вернём текущий статус
        return jsonify({"ok": True, "data": t.to_dict(u.id)})

    s = TrainingSignup(training_id=t.id, user_id=u.id)
    db.session.add(s)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # На случай гонки — считаем, что уже записан
        return jsonify({"ok": True, "data": t.to_dict(u.id)})

    return jsonify({"ok": True, "data": t.to_dict(u.id)})


@app.route('/api/trainings/<int:tid>/signup', methods=['DELETE'])
def cancel_signup(tid):
    u = get_current_user()
    if not u:
        abort(401)

    t = Training.query.get_or_404(tid)
    s = TrainingSignup.query.filter_by(training_id=t.id, user_id=u.id).first()
    if not s:
        abort(404, description="Запись не найдена")

    db.session.delete(s)
    db.session.commit()

    return jsonify({"ok": True, "data": t.to_dict(u.id)})

@app.route('/trainings-calendar')
def trainings_calendar_page():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    u = get_current_user()
    return render_template('trainings-calendar.html', me_id=(u.id if u else None))

@app.post("/api/dismiss_renewal_reminder")
@login_required
def dismiss_renewal_reminder():
    u = get_current_user()
    if not u:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    u.renewal_reminder_last_shown_on = date.today()
    db.session.commit()
    return jsonify({"ok": True})

@app.get("/api/me/telegram/status")
@login_required
def telegram_status():
    u = get_current_user()
    return jsonify({"linked": bool(u and u.telegram_chat_id)})

@app.route('/api/me/telegram/settings')
@login_required
def get_tg_settings():
    from models import db
    u = get_current_user()
    s = get_effective_user_settings(u)  # <-- синхронизация, если пусто

    payload = {
        "ok": True,
        "telegram_notify_enabled": bool(s.telegram_notify_enabled),
        "notify_trainings":        bool(s.notify_trainings),
        "notify_subscription":     bool(s.notify_subscription),
        "notify_meals":            bool(s.notify_meals),
        # алиас для старого фронта
        "notify_promos":           bool(s.notify_subscription),
    "meal_timezone":           s.meal_timezone or "Asia/Almaty",  # ← дефолт Алматы

    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp




# app.py
@app.route("/devices")
def devices():
    return render_template("devices.html")
bp = Blueprint("settings_api", __name__, url_prefix="/bp")

@bp.route("/api/me/telegram/settings", methods=["GET"])
def get_tg_settings():
    s = current_user.settings or UserSettings(user_id=current_user.id)
    if not current_user.settings:
        db.session.add(s); db.session.commit()
    return jsonify({
        "ok": True,
        "telegram_notify_enabled": bool(s.telegram_notify_enabled),
        "notify_trainings":        bool(s.notify_trainings),
        "notify_subscription":     bool(s.notify_subscription),
        # НОВОЕ
        "notify_meals":            bool(s.notify_meals),
        "meal_timezone": s.meal_timezone or "Asia/Almaty",
    })


@app.route('/api/me/telegram/settings', methods=['POST','PATCH'])
@login_required
def patch_tg_settings():
    u = get_current_user()
    s = get_effective_user_settings(u)

    data = request.get_json(silent=True) or request.form.to_dict(flat=True) or {}

    def to_bool(v):
        if isinstance(v, bool): return v
        if isinstance(v, (int, float)): return v != 0
        if v is None: return False
        return str(v).strip().lower() in ("1","true","yes","on","y")

    alias_map = {
        "telegram_notify_enabled":         "telegram_notify_enabled",
        "telegram_notifications_enabled":  "telegram_notify_enabled",  # алиас
        "notify_trainings":                "notify_trainings",
        "notify_subscription":             "notify_subscription",
        "notify_promos":                   "notify_subscription",       # алиас
        "notify_meals":                    "notify_meals",
    }

    touched = {}
    for incoming_key, model_attr in alias_map.items():
        if incoming_key in data:
            val = to_bool(data[incoming_key])
            setattr(s, model_attr, val)   # источник истины
            setattr(u, model_attr, val)   # для обратной совместимости
            touched[model_attr] = val

    if "meal_timezone" in data:
        tz = (data.get("meal_timezone") or "").strip()
        try:
            ZoneInfo(tz)  # валидация
        except Exception:
            return jsonify({"ok": False, "error": "invalid_timezone"}), 400
        s.meal_timezone = tz
        touched["meal_timezone"] = tz

    db.session.add_all([s, u])
    db.session.commit()

    resp = jsonify({
        "ok": True,
        "saved": touched,
        "telegram_notify_enabled": bool(s.telegram_notify_enabled),
        "notify_trainings":        bool(s.notify_trainings),
        "notify_subscription":     bool(s.notify_subscription),
        "notify_meals":            bool(s.notify_meals),
        "notify_promos":           bool(s.notify_subscription),
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ===== ADMIN: AI Очередь (модерация MealLog) =====

@app.route("/admin/ai")
@admin_required
def admin_ai_queue():
    q = MealLog.query.order_by(MealLog.created_at.desc()).limit(200).all()
    return render_template("admin_ai_queue.html", logs=q)

@app.route("/admin/ai/<int:meal_id>/flag", methods=["POST"])
@admin_required
def admin_ai_flag(meal_id):
    m = db.session.get(MealLog, meal_id)
    if not m: abort(404)
    old = {"is_flagged": m.is_flagged}
    m.is_flagged = True
    db.session.commit()
    log_audit("ai_flag", "MealLog", meal_id, old=old, new={"is_flagged": True})
    flash("Помечено как требующее внимания", "success")
    return redirect(url_for("admin_ai_queue"))

@app.route("/admin/ai/<int:meal_id>/unflag", methods=["POST"])
@admin_required
def admin_ai_unflag(meal_id):
    m = db.session.get(MealLog, meal_id)
    if not m: abort(404)
    old = {"is_flagged": m.is_flagged}
    m.is_flagged = False
    db.session.commit()
    log_audit("ai_unflag", "MealLog", meal_id, old=old, new={"is_flagged": False})
    flash("Снята пометка", "success")
    return redirect(url_for("admin_ai_queue"))

@app.route("/admin/ai/<int:meal_id>/edit", methods=["POST"])
@admin_required
def admin_ai_edit(meal_id):
    m = db.session.get(MealLog, meal_id)
    if not m: abort(404)
    old = {"name": m.name, "verdict": m.verdict, "analysis": m.analysis,
           "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs}
    m.name = request.form.get("name", m.name)
    m.verdict = request.form.get("verdict", m.verdict)
    m.analysis = request.form.get("analysis", m.analysis)
    m.calories = int(request.form.get("calories", m.calories) or m.calories)
    m.protein = float(request.form.get("protein", m.protein) or m.protein)
    m.fat = float(request.form.get("fat", m.fat) or m.fat)
    m.carbs = float(request.form.get("carbs", m.carbs) or m.carbs)
    db.session.commit()
    log_audit("ai_edit", "MealLog", meal_id, old=old,
              new={"name": m.name, "verdict": m.verdict, "analysis": m.analysis,
                   "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs})
    flash("Сохранено", "success")
    return redirect(url_for("admin_ai_queue"))

@app.route("/admin/ai/<int:meal_id>/reanalyse", methods=["POST"])
@admin_required
def admin_ai_reanalyse(meal_id):
    """Перегенерировать анализ по загруженной здесь фотке (админом)."""
    m = db.session.get(MealLog, meal_id)
    if not m: abort(404)

    file = request.files.get('file')
    if not file:
        flash("Загрузите изображение для перегенерации", "error")
        return redirect(url_for("admin_ai_queue"))

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        with open(filepath, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')

        tmpl = PromptTemplate.query.filter_by(name='meal_photo', is_active=True) \
            .order_by(PromptTemplate.version.desc()).first()
        system_prompt = (tmpl.body if tmpl else
            "Ты — профессиональный диетолог. Проанализируй фото еды. Определи: ...")

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "Проанализируй блюдо на фото."}
                ]}
            ],
            max_tokens=500,
        )

        # парсинг ответа (как в твоём коде)
        content = response.choices[0].message.content
        data = json.loads(content)
        old = {"name": m.name, "verdict": m.verdict, "analysis": m.analysis,
               "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs}

        m.name = data.get("name") or m.name
        m.verdict = data.get("verdict") or m.verdict
        m.analysis = data.get("analysis") or m.analysis
        m.calories = int(float(data.get("calories", m.calories)))
        m.protein = float(data.get("protein", m.protein))
        m.fat = float(data.get("fat", m.fat))
        m.carbs = float(data.get("carbs", m.carbs))
        m.image_path = filepath
        db.session.commit()

        log_audit("ai_reanalyse", "MealLog", meal_id, old=old,
                  new={"name": m.name, "verdict": m.verdict, "analysis": m.analysis,
                       "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs,
                       "image_path": filepath})
        flash("Перегенерировано", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Ошибка перегенерации: {e}", "error")

    return redirect(url_for("admin_ai_queue"))


# ===== ADMIN: Планировщик (APScheduler) =====

@app.route("/admin/jobs")
@admin_required
def admin_jobs():
    sched = get_scheduler()
    jobs = []
    if sched:
        for j in sched.get_jobs():
            jobs.append({
                "id": j.id,
                "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
                "paused": getattr(j, "paused", False)
            })
    return render_template("admin_jobs.html", jobs=jobs)

@app.route("/admin/jobs/<job_id>/pause", methods=["POST"])
@admin_required
def admin_jobs_pause(job_id):
    pause_job(job_id)
    log_audit("job_pause", "Job", job_id)
    flash("Задача приостановлена", "success")
    return redirect(url_for("admin_jobs"))

@app.route("/admin/jobs/<job_id>/resume", methods=["POST"])
@admin_required
def admin_jobs_resume(job_id):
    resume_job(job_id)
    log_audit("job_resume", "Job", job_id)
    flash("Задача возобновлена", "success")
    return redirect(url_for("admin_jobs"))

@app.route("/admin/jobs/run_tick_now", methods=["POST"])
@admin_required
def admin_jobs_run_tick_now():
    run_tick_now(app)
    log_audit("job_run", "MealReminders", "tick_now")
    flash("Тик запущен", "success")
    return redirect(url_for("admin_jobs"))


# ===== ADMIN: Промпты =====

@app.route("/admin/prompts", methods=["GET", "POST"])
@admin_required
def admin_prompts():
    if request.method == "POST":
        name = request.form["name"].strip()
        version = int(request.form["version"])
        body = request.form["body"]
        p = PromptTemplate(name=name, version=version, body=body, is_active=False)
        db.session.add(p)
        db.session.commit()
        log_audit("prompt_create", "PromptTemplate", p.id, new={"name": name, "version": version})
        flash("Шаблон сохранён", "success")
        return redirect(url_for("admin_prompts"))

    prompts = PromptTemplate.query.order_by(PromptTemplate.name, PromptTemplate.version.desc()).all()
    return render_template("admin_prompts.html", prompts=prompts)

@app.route("/admin/prompts/<int:pid>/activate", methods=["POST"])
@admin_required
def admin_prompts_activate(pid):
    p = db.session.get(PromptTemplate, pid)
    if not p: abort(404)
    # деактивируем остальные с тем же name
    db.session.query(PromptTemplate).filter(
        PromptTemplate.name == p.name,
        PromptTemplate.id != p.id
    ).update({"is_active": False})
    p.is_active = True
    db.session.commit()
    log_audit("prompt_activate", "PromptTemplate", pid, new={"name": p.name, "version": p.version})
    flash("Активирован", "success")
    return redirect(url_for("admin_prompts"))


# ===== ADMIN: Рассылки в Telegram =====

@app.route("/admin/broadcast", methods=["GET", "POST"])
@admin_required
def admin_broadcast():
    if request.method == "POST":
        text = request.form["text"].strip()
        only_active = bool(request.form.get("only_active"))
        q = db.session.query(User.telegram_chat_id, User.id).filter(User.telegram_chat_id.isnot(None))
        if only_active:
            q = q.join(Subscription, Subscription.user_id == User.id).filter(Subscription.status == 'active')
        rows = q.all()
        sent = 0
        for chat_id, uid in rows:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=10
                )
                sent += 1
            except Exception:
                pass
        log_audit("broadcast_send", "Telegram", "bulk", new={"text": text, "only_active": only_active, "sent": sent})
        flash(f"Отправлено: {sent}", "success")
        return redirect(url_for("admin_broadcast"))
    return render_template("admin_broadcast.html")



@app.get("/admin/impersonate/<int:user_id>")
@admin_required
def admin_impersonate_user(user_id):
    target = db.session.get(User, user_id) or abort(404)
    admin_id = session.get("user_id")
    # сохраняем, чтобы можно было вернуться
    session["impersonator_id"] = admin_id
    session["user_id"] = target.id
    flash(f"Вы вошли как {target.name} (ID {target.id}).", "success")
    try:
        log_audit("impersonate_start", "User", target.id, old={"admin": admin_id})
    except Exception:
        pass
    return redirect(url_for("profile"))


@app.get("/admin/impersonate/stop")
def admin_stop_impersonation():
    impersonator = session.pop("impersonator_id", None)
    if impersonator:
        session["user_id"] = impersonator
        flash("Возвращён доступ администратора.", "success")
        try:
            log_audit("impersonate_stop", "User", impersonator)
        except Exception:
            pass
    else:
        flash("Режим имперсонации не активен.", "error")
    # возвращаемся в админку, если есть user_id, иначе на дашборд
    return redirect(url_for("admin_dashboard"))

@app.post("/admin/users/<int:user_id>/telegram/test")
@admin_required
def admin_user_send_test_tg(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if not user.telegram_chat_id:
        flash("Telegram не привязан у пользователя.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    text = (request.form.get("text") or f"Привет, {user.name}! Это тестовое сообщение 💬").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        flash("TELEGRAM_BOT_TOKEN не задан в окружении.", "error")
        return redirect(url_for("admin_user_detail", user_id=user.id))

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": user.telegram_chat_id, "text": text},
            timeout=10
        )
        r.raise_for_status()
        flash("Тестовое сообщение отправлено.", "success")
        try:
            log_audit("telegram_test_sent", "User", user.id, new={"text_len": len(text)})
        except Exception:
            pass
    except Exception as e:
        flash(f"Ошибка отправки: {e}", "error")

    return redirect(url_for("admin_user_detail", user_id=user.id))


@app.post("/admin/users/<int:user_id>/telegram/unlink")
@admin_required
def admin_user_unlink_telegram(user_id):
    user = db.session.get(User, user_id) or abort(404)
    old = {"telegram_chat_id": user.telegram_chat_id}
    user.telegram_chat_id = None
    db.session.commit()
    flash("Telegram отвязан.", "success")
    try:
        log_audit("telegram_unlink", "User", user.id, old=old, new={"telegram_chat_id": None})
    except Exception:
        pass
    return redirect(url_for("admin_user_detail", user_id=user.id))



@app.route("/admin/user/<int:user_id>/reset_telegram", methods=["GET","POST"], endpoint="admin_reset_telegram")
@admin_required
def admin_reset_telegram(user_id):
    user = db.session.get(User, user_id) or abort(404)
    old = {
        "telegram_chat_id": getattr(user, "telegram_chat_id", None),
        "telegram_code": getattr(user, "telegram_code", None),
    }
    user.telegram_chat_id = None
    user.telegram_code = None
    if hasattr(user, "renewal_telegram_sent"):
        user.renewal_telegram_sent = False
    db.session.commit()
    try:
        log_audit("reset_telegram", "User", user.id, old=old, new={"telegram_chat_id": None, "telegram_code": None})
    except Exception:
        pass
    flash("Связка с Telegram сброшена.", "success")
    return redirect(url_for("admin_user_detail", user_id=user.id))


# генерация/отправка магической ссылки из админки
@app.route("/admin/user/<int:user_id>/send_magic_link", methods=["GET","POST"], endpoint="admin_send_magic_link")
@admin_required
def admin_send_magic_link(user_id):
    user = db.session.get(User, user_id) or abort(404)
    s = _magic_serializer()
    token = s.dumps(str(user.id))

    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    magic_url = (
        f"{base}{url_for('magic_login', token=token)}"
        if base else url_for("magic_login", token=token, _external=True)
    )

    sent = False
    if getattr(user, "telegram_chat_id", None):
        sent = _send_telegram(user.telegram_chat_id, f"🔑 Вход без пароля: {magic_url}")

    try:
        log_audit("magic_link", "User", user.id, new={"sent_to_telegram": bool(sent)})
    except Exception:
        pass

    msg = "Ссылка сгенерирована. " + ("Отправлена в Telegram. " if sent else "")
    flash(f"{msg}Скопируйте при необходимости: {magic_url}", "success")
    return redirect(url_for("admin_user_detail", user_id=user.id))

@app.route("/admin/users/<int:user_id>/export", methods=["GET","POST"], endpoint="admin_user_export")
@admin_required
def admin_user_export(user_id):
    user = db.session.get(User, user_id) or abort(404)
    fmt = (request.form.get("format") or request.args.get("format") or "json").lower()

    meals = MealLog.query.filter_by(user_id=user.id).order_by(MealLog.date.desc()).all()
    acts  = Activity.query.filter_by(user_id=user.id).order_by(Activity.date.desc()).all()
    diets = Diet.query.filter_by(user_id=user.id).order_by(Diet.date.desc()).all()
    bodies = BodyAnalysis.query.filter_by(user_id=user.id).order_by(BodyAnalysis.timestamp.desc()).all()

    if fmt == "csv":
        import io, csv
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["date","meal_type","name","calories","protein","fat","carbs","verdict","analysis"])
        for m in meals:
            w.writerow([
                m.date.isoformat() if getattr(m, "date", None) else "",
                m.meal_type, m.name or "",
                m.calories, m.protein, m.fat, m.carbs,
                m.verdict or "", (m.analysis or "").replace("\n", " ")
            ])
        resp = make_response(sio.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="user_{user.id}_meals.csv"'
        try: log_audit("export_csv", "User", user.id, new={"rows": len(meals)})
        except Exception: pass
        return resp

    import json as _json
    data = {
        "user": {
            "id": user.id, "name": user.name, "email": user.email,
            "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
            "telegram_chat_id": getattr(user, "telegram_chat_id", None)
        },
        "meals": [{
            "id": m.id, "date": m.date.isoformat() if m.date else None,
            "meal_type": m.meal_type, "name": m.name,
            "calories": m.calories, "protein": m.protein, "fat": m.fat, "carbs": m.carbs,
            "verdict": m.verdict, "analysis": m.analysis,
            "image_path": getattr(m, "image_path", None)
        } for m in meals],
        "activities": [{
            "id": a.id, "date": a.date.isoformat() if a.date else None,
            "steps": a.steps, "active_kcal": a.active_kcal,
            "resting_kcal": getattr(a, "resting_kcal", None),
            "distance_km": getattr(a, "distance_km", None)
        } for a in acts],
        "diets": [{
            "id": d.id, "date": d.date.isoformat() if d.date else None,
            "total_kcal": d.total_kcal, "protein": d.protein, "fat": d.fat, "carbs": d.carbs,
            "breakfast": json.loads(d.breakfast or "[]"),
            "lunch": json.loads(d.lunch or "[]"),
            "dinner": json.loads(d.dinner or "[]"),
            "snack": json.loads(d.snack or "[]"),
        } for d in diets],
        "body_analyses": [{
            "id": b.id,
            "timestamp": b.timestamp.isoformat() if b.timestamp else None,
            "height": getattr(b, "height", None),
            "weight": getattr(b, "weight", None),
            "muscle_mass": getattr(b, "muscle_mass", None),
            "fat_mass": getattr(b, "fat_mass", None),
            "bmi": getattr(b, "bmi", None),
            "metabolism": getattr(b, "metabolism", None)
        } for b in bodies]
    }
    resp = make_response(_json.dumps(data, ensure_ascii=False, default=str))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="user_{user.id}.json"'
    try:
        log_audit("export_json", "User", user.id,
                  new={"meals": len(meals), "activities": len(acts), "diets": len(diets), "body_analyses": len(bodies)})
    except Exception:
        pass
    return resp
# --- ВИЗУАЛИЗАЦИЯ ТЕЛА -------------------------------------------------------

def _latest_analysis_for(user_id: int):
    return (BodyAnalysis.query
            .filter(BodyAnalysis.user_id == user_id)
            .order_by(BodyAnalysis.timestamp.desc())
            .first())

@app.get("/visualize", endpoint="visualize")
@login_required
def visualize_page():
    u = get_current_user()
    latest_analysis = _latest_analysis_for(u.id)

    fat_loss_progress = None
    # --- НАЧАЛО ИЗМЕНЕНИЙ: Новая логика расчета прогресса ---
    initial_analysis = db.session.get(BodyAnalysis, u.initial_body_analysis_id) if u.initial_body_analysis_id else None

    if initial_analysis and latest_analysis and latest_analysis.fat_mass and u.fat_mass_goal and initial_analysis.fat_mass > u.fat_mass_goal:
        initial_fat_mass = initial_analysis.fat_mass
        current_fat_mass = latest_analysis.fat_mass
        goal_fat_mass = u.fat_mass_goal

        total_fat_to_lose_kg = initial_fat_mass - goal_fat_mass
        fat_lost_so_far_kg = initial_fat_mass - current_fat_mass

        percentage = 0
        if total_fat_to_lose_kg > 0:
            percentage = (fat_lost_so_far_kg / total_fat_to_lose_kg) * 100
        percentage = min(100, max(0, percentage))
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

        # --- НАЧАЛО ИЗМЕНЕНИЙ: Выбор мотивационного сообщения ---
        motivation_text = ""
        if percentage == 0:
            motivation_text = "Путь в тысячу ли начинается с первого шага. Начнем?"
        elif 0 < percentage < 10:
            motivation_text = "Отличное начало! Первые результаты уже есть."
        elif 10 <= percentage < 40:
            motivation_text = "Вы на верном пути! Продолжайте в том же духе."
        elif 40 <= percentage < 70:
            motivation_text = "Больше половины позади! Выглядит впечатляюще."
        elif 70 <= percentage < 100:
            motivation_text = "Финишная прямая! Цель совсем близко."
        elif percentage >= 100:
            motivation_text = "Поздравляю! Цель достигнута. Вы великолепны!"
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

        fat_loss_progress = {
            'percentage': percentage,
            'burned_kg': fat_lost_so_far_kg,
            'total_to_lose_kg': total_fat_to_lose_kg,
            'initial_kg': initial_fat_mass,
            'goal_kg': goal_fat_mass,
            'current_kg': current_fat_mass,
            'motivation_text': motivation_text  # Добавляем сообщение в словарь
        }

    latest_visualization = BodyVisualization.query.filter_by(user_id=u.id).order_by(BodyVisualization.id.desc()).first()

    return render_template(
        'visualize.html',
        latest_analysis=latest_analysis,
        latest_visualization=latest_visualization,
        fat_loss_progress=fat_loss_progress
    )


@app.route('/visualize/run', methods=['POST'])
@login_required
def visualize_run():
    u = get_current_user()
    if not u:
        abort(401)

    if not getattr(u, 'face_consent', False):
        return jsonify({"success": False,
                        "error": "Чтобы сгенерировать визуализацию, нужно разрешить использование аватара (галочка в профиле)."}), 400

    latest = BodyAnalysis.query.filter_by(user_id=u.id).order_by(BodyAnalysis.timestamp.desc()).first()
    if not latest:
        return jsonify(
            {"success": False, "error": "Загрузите актуальный анализ тела — без него визуализация не строится."}), 400

    # --- Получаем байты аватара ---
    avatar_bytes = None
    if u.avatar:
        avatar_bytes = u.avatar.data

    if not avatar_bytes:
        # Если у пользователя нет аватара, загружаем дефолтный из static
        try:
            with open(os.path.join(app.static_folder, 'i.webp'), 'rb') as f:
                avatar_bytes = f.read()
        except FileNotFoundError:
            app.logger.error("[visualize] Default avatar i.webp not found in static folder.")
            return jsonify({"success": False, "error": "Файл аватара по умолчанию не найден."}), 500

    # --- metrics_current ---
    current_weight = latest.weight or 0
    metrics_current = {
        "height_cm": latest.height,
        "weight_kg": current_weight,
        "fat_mass": latest.fat_mass,
        "muscle_mass": latest.muscle_mass,
        "metabolism": latest.metabolism,
        "fat_pct": _compute_pct(latest.fat_mass, current_weight),
        "muscle_pct": _compute_pct(latest.muscle_mass, current_weight),
        "sex": getattr(u, "sex", None),
    }

    # --- metrics_target (Полный расчет) ---
    metrics_target = metrics_current.copy()
    fat_mass_goal = getattr(u, "fat_mass_goal", None)
    muscle_mass_goal = getattr(u, "muscle_mass_goal", None)

    if fat_mass_goal is not None and muscle_mass_goal is not None:
        metrics_target["fat_mass"] = fat_mass_goal
        metrics_target["muscle_mass"] = muscle_mass_goal

        delta_fat = (metrics_current.get("fat_mass") or 0) - fat_mass_goal
        delta_muscle = muscle_mass_goal - (metrics_current.get("muscle_mass") or 0)
        target_weight = current_weight - delta_fat + delta_muscle
        metrics_target["weight_kg"] = target_weight

        metrics_target["fat_pct"] = _compute_pct(fat_mass_goal, target_weight)
        metrics_target["muscle_pct"] = _compute_pct(muscle_mass_goal, target_weight)

    try:
        # Вызываем обновленную функцию, передавая байты аватара
        current_image_filename, target_image_filename = generate_for_user(
            user=u,
            avatar_bytes=avatar_bytes,
            metrics_current=metrics_current,
            metrics_target=metrics_target
        )

        # Функция create_record теперь принимает имена файлов
        new_viz_record = create_record(
            user=u,
            curr_filename=current_image_filename,
            tgt_filename=target_image_filename,
            metrics_current=metrics_current,
            metrics_target=metrics_target
        )

        # Используем новый маршрут 'serve_file'
        return jsonify({
            "success": True,
            "visualization": {
                "image_current_path": url_for('serve_file', filename=new_viz_record.image_current_path),
                "image_target_path": url_for('serve_file', filename=new_viz_record.image_target_path),
                "created_at": new_viz_record.created_at.strftime('%d.%m.%Y %H:%M')
            }
        })

    except Exception as e:
        app.logger.error("[visualize] generation failed: %s", e, exc_info=True)
        db.session.rollback()  # Откатываем транзакцию в случае ошибки
        return jsonify({"success": False, "error": f"Не удалось сгенерировать визуализацию: {e}"}), 500

# ===== ADMIN: Аудит =====

@app.route("/admin/audit")
@admin_required
def admin_audit():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    return render_template("admin_audit.html", logs=logs)

# регистрация блюпринта (добавь после определения маршрутов)
app.register_blueprint(bp)
app.register_blueprint(shopping_bp, url_prefix="/shopping")
app.register_blueprint(assistant_bp) # <--- И ЭТУ СТРОКУ

@app.route('/files/<path:filename>')
def serve_file(filename):
    """Отдаёт загруженный файл из БД."""
    f = UploadedFile.query.filter_by(filename=filename).first_or_404()
    return send_file(BytesIO(f.data), mimetype=f.content_type)

@app.route('/ai-instructions')
@login_required
def ai_instructions_page():
    """Отображает страницу с инструкциями по работе с ИИ-ассистентом."""
    return render_template('ai_instructions.html')


# Добавьте этот код после функции logout или в конце блока с маршрутами профиля

@app.route('/profile/reset_goals', methods=['POST'])
@login_required
def reset_goals():
    """Сбрасывает цели пользователя и стартовую точку для нового отсчета."""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    user.fat_mass_goal = None
    user.muscle_mass_goal = None
    user.initial_body_analysis_id = None

    db.session.commit()

    flash("Ваши цели сброшены. Загрузите новый анализ, чтобы начать отсчет заново!", "success")
    return redirect(url_for('profile'))



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
