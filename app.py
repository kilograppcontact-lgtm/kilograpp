import os
from datetime import datetime, date, timedelta, time as dt_time
from urllib.parse import urlparse
import base64
import json
from flask import jsonify # Убедись, что jsonify импортирован вверху файла
import requests
import uuid  # Добавлено для генерации уникальных ID заказов
import time  # Добавлено для симуляции
from flask import Flask, render_template, request, redirect, session, jsonify, url_for, flash, abort, \
    send_from_directory
from flask_bcrypt import Bcrypt
from werkzeug.utils import secure_filename
from openai import OpenAI
from dotenv import load_dotenv
import random
import string
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError
import re
from sqlalchemy import func
from functools import wraps
from PIL import Image  # Import Pillow
from sqlalchemy import text # Убедитесь, что text импортирован из sqlalchemy
from meal_reminders import start_meal_scheduler
from flask import Blueprint, request, jsonify
from flask_login import current_user

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecret")
app.jinja_env.globals.update(getattr=getattr)

app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Config DB — задаём ДО init_app
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///35healthclubs.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

from extensions import db
db.init_app(app)

from models import (
    User, Subscription, Order, Group, GroupMember, GroupMessage, MessageReaction,
    GroupTask, MealLog, Activity, Diet, Training, TrainingSignup, BodyAnalysis,
    UserSettings, MealReminderLog
)

with app.app_context():
    db.create_all()

# --- Image Resizing Configuration ---
CHAT_IMAGE_MAX_SIZE = (200, 200)  # Max width and height for chat images


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


@app.route('/uploads/<path:filename>')
def serve_uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


ADMIN_EMAIL = "admin@healthclub.local"


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
                    db.extract('hour', Training.start_time) == target.hour,
                    db.extract('minute', Training.start_time) == target.minute
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

                # 2) НОВОЕ: рассылка в момент начала — со ссылкой на участие
                startings = Training.query.filter(
                    Training.date == now.date(),
                    db.extract('hour', Training.start_time) == now.hour,
                    db.extract('minute', Training.start_time) == now.minute
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

                db.session.commit()
            except Exception:
                db.session.rollback()
            finally:
                db.session.remove()
                time_mod.sleep(60)

def create_app():
    app = Flask(__name__)

    with app.app_context():
        start_meal_scheduler(app)

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

with app.app_context():
    db.create_all()
    # Мини-миграции для новых полей в user

    # Запускаем фоновые задачи ТОЛЬКО после инициализации БД
    try:
        from meal_reminders import start_meal_scheduler
        start_meal_scheduler(app)
    except Exception:
        pass
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            session['user_id'] = user.id
            return redirect('/profile')
        return render_template('login.html', error="Неверный логин или пароль")
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    errors = []

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        date_str = request.form.get('date_of_birth', '').strip()

        # Проверка обязательных полей
        if not name:
            errors.append("Имя обязательно.")
        if not email:
            errors.append("Email обязателен.")
        if not password or len(password) < 6:
            errors.append("Пароль обязателен и должен содержать минимум 6 символов.")

        # Проверка уникальности email
        if User.query.filter_by(email=email).first():
            errors.append("Этот email уже зарегистрирован.")

        # Проверка даты рождения
        date_of_birth = None
        if date_str:
            try:
                date_of_birth = datetime.strptime(date_str, "%Y-%m-%d")
                if date_of_birth > datetime.now():
                    errors.append("Дата рождения не может быть в будущем.")
            except ValueError:
                errors.append("Некорректный формат даты рождения.")
        else:
            errors.append("Дата рождения обязательна.")

        if errors:
            return render_template('register.html', errors=errors)

        # Хеширование пароля и сохранение пользователя
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            date_of_birth=date_of_birth
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

    diet_obj = Diet.query.filter_by(user_id=user_id).order_by(Diet.date.desc()).first()
    diet = None
    if diet_obj:
        diet = {
            "total_kcal": getattr(diet_obj, "total_kcal", None) or getattr(diet_obj, "calories", None),
            "protein": getattr(diet_obj, "protein", None),
            "fat": getattr(diet_obj, "fat", None),
            "carbs": getattr(diet_obj, "carbs", None),
            "meals": {"breakfast": [], "lunch": [], "dinner": [], "snack": []}
        }

        # Источники блюд могут быть разные: JSON-поле, Python-список словарей, relation items и т.п.
        meals_source = None

        if getattr(diet_obj, "meals", None):
            meals_source = diet_obj.meals

            # 2) diet_obj.meals_json (строка JSON)
        if meals_source is None and getattr(diet_obj, "meals_json", None):
            try:
                meals_source = json.loads(diet_obj.meals_json)
            except Exception:
                meals_source = None

            # 3) отдельные поля breakfast/lunch/dinner/snack (списки/JSON-строки)
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

            # 4) relation items (например Diet.items)
        if meals_source is None and getattr(diet_obj, "items", None):
            meals_source = diet_obj.items

            # Утилита добавления пункта

        def push(meal_type, name, grams=None, kcal=None):
            mt = (meal_type or "").lower()
            if mt in diet["meals"]:
                diet["meals"][mt].append({
                    "name": name or "Блюдо",
                    "grams": grams,
                    "kcal": kcal
                })

            # Заполняем блюда из разных форматов

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

            # Если суммарные калории не заданы — считаем из блюд
        if not diet["total_kcal"]:
            try:
                diet["total_kcal"] = sum(
                    (i.get("kcal") or 0)
                    for lst in diet["meals"].values() for i in lst
                ) or None
            except Exception:
                pass

    # --- Прогресс жиросжигания ---
    fat_loss_progress = None
    if latest_analysis and latest_analysis.fat_mass and user.fat_mass_goal and latest_analysis.fat_mass > user.fat_mass_goal:
        start_datetime = latest_analysis.timestamp
        today = date.today()

        meal_data = (db.session.query(MealLog.date, func.sum(MealLog.calories))
                     .filter(MealLog.user_id == user_id, MealLog.date >= start_datetime.date())
                     .group_by(MealLog.date)
                     .all())
        meal_map = dict(meal_data)

        activity_data = (db.session.query(Activity.date, Activity.active_kcal)
                         .filter(Activity.user_id == user_id, Activity.date >= start_datetime.date())
                         .all())
        activity_map = dict(activity_data)

        total_accumulated_deficit = 0
        delta_days = (today - start_datetime.date()).days

        if delta_days >= 0:
            for i in range(delta_days + 1):
                current_day = start_datetime.date() + timedelta(days=i)
                consumed = meal_map.get(current_day, 0)
                burned_active = activity_map.get(current_day, 0)

                # День анализа: исключаем еду до замера и не учитываем активность
                if i == 0:
                    calories_before_analysis = (db.session.query(func.sum(MealLog.calories))
                                                .filter(MealLog.user_id == user_id,
                                                        MealLog.date == current_day,
                                                        MealLog.created_at < start_datetime)
                                                .scalar() or 0)
                    consumed -= calories_before_analysis
                    burned_active = 0

                daily_deficit = (metabolism + burned_active) - consumed
                if daily_deficit > 0:
                    total_accumulated_deficit += daily_deficit

        KCAL_PER_KG_FAT = 7700
        total_fat_to_lose_kg = latest_analysis.fat_mass - user.fat_mass_goal
        estimated_fat_burned_kg = min(total_accumulated_deficit / KCAL_PER_KG_FAT, total_fat_to_lose_kg)

        percentage = 0
        if total_fat_to_lose_kg > 0:
            percentage = (estimated_fat_burned_kg / total_fat_to_lose_kg) * 100

        fat_loss_progress = {
            'percentage': min(100, max(0, percentage)),
            'burned_kg': estimated_fat_burned_kg,
            'total_to_lose_kg': total_fat_to_lose_kg,
            'initial_kg': latest_analysis.fat_mass,
            'goal_kg': user.fat_mass_goal,
            'current_kg': latest_analysis.fat_mass - estimated_fat_burned_kg
        }

    return render_template(
        'profile.html',
        user=user,
        age=age,
        diet=diet,                          # <- нормализованная структура для шаблона
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
        fat_loss_progress=fat_loss_progress,
        just_activated=just_activated
    )

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/upload_analysis', methods=['POST'])
def upload_analysis():
    file = request.files.get('file')
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)
    if not file or not user:
        flash("Файл не загружен или пользователь не авторизован.", "error")
        return redirect('/profile')

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    with open(filepath, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode("utf-8")

    try:
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

        if "error" in result:
            missing = ', '.join(result.get("missing", []))
            flash(f"Недостаточно данных в анализе: {missing}.", "error")
            return redirect('/profile')

        # --- ШАГ 2: Генерация целей на основе извлеченных данных ---
        age = calculate_age(user.date_of_birth) if user.date_of_birth else 'не указан'
        prompt_goals = (
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

        # Объединяем результаты
        result.update(goals_result)

        session['temp_analysis'] = result
        return render_template('confirm_analysis.html', data=result)


    except Exception as e:

        # ДОБАВЬТЕ ЭТУ СТРОКУ ДЛЯ ДИАГНОСТИКИ

        print(f"!!! ОШИБКА В UPLOAD_ANALYSIS: {e}")

        flash(f"Не удалось проанализировать изображение. Проверьте консоль сервера для деталей.", "error")

        return redirect('/profile')


# УДАЛИТЕ СТАРУЮ ФУНКЦИЮ @app.route('/add_meal')

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
@app.route('/confirm_analysis', methods=['POST'])
def confirm_analysis():
    user_id = session.get('user_id')
    if not user_id or 'temp_analysis' not in session:
        flash("Нет данных для подтверждения анализа.", "error")
        return redirect('/profile')

    analysis_data = session.pop('temp_analysis')
    user = db.session.get(User, user_id)

    # 1. Обновляем ТОЛЬКО цели в таблице User.
    #    Метрики состава тела больше здесь не сохраняются.
    user.fat_mass_goal = request.form.get('fat_mass_goal', user.fat_mass_goal, type=float)
    user.muscle_mass_goal = request.form.get('muscle_mass_goal', user.muscle_mass_goal, type=float)
    user.analysis_comment = analysis_data.get("analysis")
    user.updated_at = datetime.utcnow()

    # 2. Создаем НОВУЮ запись в истории BodyAnalysis.
    #    Это позволяет хранить полную историю всех замеров.
    new_analysis_entry = BodyAnalysis(
        user_id=user.id,
        timestamp=datetime.utcnow()
    )

    # 3. Заполняем новую запись всеми данными из анализа, полученными от AI.
    for field, value in analysis_data.items():
        # Проверяем, есть ли такое поле в модели BodyAnalysis, чтобы избежать ошибок.
        if hasattr(new_analysis_entry, field):
            setattr(new_analysis_entry, field, value)

    # 4. Проверяем, не отредактировал ли пользователь рост в форме подтверждения.
    #    Если да, обновляем значение в НАШЕЙ НОВОЙ ЗАПИСИ.
    edited_height = request.form.get('height', type=int)
    if edited_height is not None:
        new_analysis_entry.height = edited_height

    # Сохраняем в БД новую запись анализа и обновленные цели пользователя.
    db.session.add(new_analysis_entry)
    db.session.commit()

    flash("Данные анализа тела и цели успешно сохранены!", "success")
    return redirect('/profile')
# --- КОНЕЦ ИЗМЕНЕНИЙ ---


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
    gender = request.args.get("gender", "male")
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
        # Эта проверка на всякий случай, т.к. login_required уже есть
        return redirect(url_for('login'))

    # --- Обновление текстовых полей ---
    user.name = request.form.get('name', user.name)
    user.email = request.form.get('email', user.email)
    date_of_birth_str = request.form.get('date_of_birth')
    if date_of_birth_str:
        try:
            user.date_of_birth = datetime.strptime(date_of_birth_str, '%Y-%m-%d').date()
        except ValueError:
            flash("Неверный формат даты рождения.", "error")
            return redirect(url_for('profile'))

    # --- Проверка на уникальность нового email ---
    # Проверяем, только если email был изменен
    if 'email' in request.form and user.email != session.get('user_email_before_edit'):
        existing_user = User.query.filter(User.email == user.email, User.id != user.id).first()
        if existing_user:
            flash("Этот email уже используется другим пользователем.", "error")
            # Откатываем изменение email обратно, чтобы не сохранять
            user.email = session.get('user_email_before_edit')
            return redirect(url_for('profile'))

    # --- Обновление пароля (если он был введен) ---
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    if new_password:
        if new_password != confirm_password:
            flash("Пароли не совпадают.", "error")
            return redirect(url_for('profile'))
        if len(new_password) < 6:
            flash("Пароль должен содержать не менее 6 символов.", "error")
            return redirect(url_for('profile'))

        user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')

    # --- Загрузка новой аватарки (если она была отправлена) ---
    if 'avatar' in request.files:
        file = request.files['avatar']
        if file.filename != '':
            # Удаляем старый аватар, если он есть и это не дефолтный
            if user.avatar:
                old_avatar_path = os.path.join(app.config['UPLOAD_FOLDER'], user.avatar)
                if os.path.exists(old_avatar_path):
                    try:
                        os.remove(old_avatar_path)
                    except OSError as e:
                        print(f"Error deleting old avatar: {e}")

            filename = secure_filename(f"avatar_{user.id}_{file.filename}")
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            user.avatar = filename

    try:
        db.session.commit()
        flash("Профиль успешно обновлен!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Произошла ошибка при обновлении профиля: {e}", "error")

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
        system_prompt = (
            "Ты — профессиональный диетолог. Проанализируй фото еды. Определи:"
            "\n- Каллорий должен быть максимально реалистичным, не ровно 400, 500. А числа в которые хочется верить что то вроде 370, 420.."
            "\n- Название блюда (в поле 'name')."
            "\n- Калорийность, Белки, Жиры, Углеводы (в полях 'calories', 'protein', 'fat', 'carbs')."
            "\n- Дай подробный текстовый анализ блюда (в поле 'analysis')."
            "\n- Сделай краткий вывод: насколько блюдо полезно или вредно для диеты (в поле 'verdict')."
            '\nВерни JSON СТРОГО в формате: {"name": "...", "calories": 0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "analysis": "...", "verdict": "..."}'
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
            "image_url": url_for('serve_uploaded_file', filename=msg.image_file) if msg.image_file else None,
            "user": {
                "name": msg.user.name,
                "avatar_url": url_for('serve_uploaded_file', filename=msg.user.avatar) if msg.user.avatar else url_for(
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
        filename = secure_filename(file.filename)
        # ... (здесь может быть ваша логика проверки расширений)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        resize_image(filepath, CHAT_IMAGE_MAX_SIZE)
        image_filename = filename

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
            "image_url": url_for('serve_uploaded_file', filename=msg.image_file) if msg.image_file else None,
            "user": {
                "name": user.name,
                "avatar_url": url_for('serve_uploaded_file', filename=user.avatar) if user.avatar else url_for('static', filename='default-avatar.png')
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

with app.app_context():
    db.create_all()

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
    weight_data = db.session.execute(text(f"""
        SELECT strftime('%w', timestamp) as day_of_week, AVG(weight) as avg_weight
        FROM body_analysis
        WHERE user_id = {user_id} AND date(timestamp) BETWEEN '{week_ago}' AND '{today}'
        GROUP BY day_of_week
        ORDER BY day_of_week
    """)).fetchall()

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

    # Убедимся, что есть данные для расчета
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

        # Особая логика для первого дня (как и в основном расчете)
        if i == 0:
            calories_before_analysis = db.session.query(func.sum(MealLog.calories)).filter(
                MealLog.user_id == user.id,
                MealLog.date == current_day,
                MealLog.created_at < start_datetime
            ).scalar() or 0
            consumed -= calories_before_analysis
            burned_active = 0  # Активность за первый день не учитываем для точности

        total_burned = metabolism + burned_active
        daily_deficit = total_burned - consumed

        history_data.append({
            "date": current_day.strftime('%d.%m.%Y'),
            "consumed": consumed,
            "base_metabolism": metabolism,
            "burned_active": burned_active,
            "total_burned": total_burned,
            "deficit": daily_deficit if daily_deficit > 0 else 0  # Считаем только положительный дефицит
        })

    return jsonify(history_data)

@app.route("/purchase")
def purchase_page():
    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "DietaAIBot")
    return render_template("purchase.html", bot_username=bot_username)


@app.route('/api/kaspi/generate_qr', methods=['POST'])
@login_required
def generate_kaspi_qr():
    """Создает заказ в нашей системе и генерирует QR-код для оплаты."""
    user = get_current_user()
    data = request.get_json()
    sub_type = data.get('subscription_type')
    amount = data.get('amount')

    if not sub_type or not amount:
        return jsonify({"error": "Отсутствуют данные о подписке."}), 400

    # 1. Создаем заказ в нашей базе данных
    new_order = Order(
        user_id=user.id,
        subscription_type=sub_type,
        amount=float(amount)
    )
    db.session.add(new_order)
    db.session.commit()

    # 2. --- SIMULATE KASPI API CALL ---
    #    Здесь должен быть реальный HTTP-запрос к API Kaspi для создания счета.
    #    Вам нужно будет передать `new_order.order_id` и `new_order.amount`.
    #    В заголовках необходимо передать 'X-Auth-Token': KASPI_API_TOKEN

    #    Пример тела запроса (уточните по документации Kaspi):
    #    payload = { "merchantInvoiceId": new_order.order_id, "amount": new_order.amount }
    #    headers = { "X-Auth-Token": KASPI_API_TOKEN }
    #    response = requests.post(f"{KASPI_API_URL}/invoices", json=payload, headers=headers)

    #    Вместо реального запроса мы симулируем успешный ответ:
    print(f"SIMULATING: Generating Kaspi QR for order {new_order.order_id} with amount {new_order.amount}")

    # Kaspi в ответ вернет ID своего счета и данные для QR
    kaspi_invoice_id = f"KASPI_{new_order.order_id}"
    qr_data_string = f"https://kaspi.kz/pay/{kaspi_invoice_id}"

    # Сохраняем ID от Kaspi в наш заказ
    new_order.kaspi_invoice_id = kaspi_invoice_id
    db.session.commit()

    return jsonify({
        "orderId": new_order.order_id,
        "qrData": qr_data_string
    })


@app.route('/api/kaspi/status/<order_id>')
@login_required
def get_payment_status(order_id):
    """Проверяет статус оплаты заказа. Вызывается с фронтенда каждые несколько секунд."""
    order = Order.query.filter_by(order_id=order_id, user_id=get_current_user().id).first_or_404()

    # Если заказ уже оплачен, просто возвращаем статус
    if order.status == 'paid':
        return jsonify({"status": "paid"})

    # --- SIMULATE KASPI STATUS CHECK ---
    #    Здесь должен быть реальный HTTP-запрос к API Kaspi для проверки статуса счета.
    #    response = requests.get(f"{KASPI_API_URL}/invoices/{order.kaspi_invoice_id}", headers=headers)
    #    kaspi_status = response.json().get('status')

    #    Вместо этого мы симулируем оплату через 10 секунд после создания заказа
    seconds_since_creation = (datetime.utcnow() - order.created_at).total_seconds()

    if seconds_since_creation > 10:
        # Симулируем успешную оплату
        order.status = 'paid'
        order.paid_at = datetime.utcnow()

        # Выдаем подписку пользователю
        # Логика скопирована из manage_subscription
        months_map = {'1m': 1, '6m': 6, '12m': 12}
        months_to_add = months_map.get(order.subscription_type, 1)

        today = date.today()
        end_date = today + timedelta(days=30 * months_to_add)

        sub = Subscription.query.filter_by(user_id=order.user_id).first()
        if sub:
            sub.start_date = today
            sub.end_date = end_date
            sub.status = 'active'
            sub.source = 'kaspi_payment'
        else:
            sub = Subscription(user_id=order.user_id, start_date=today, end_date=end_date, source='kaspi_payment')
            db.session.add(sub)

        user = User.query.get(order.user_id)
        user.show_welcome_popup = True

        db.session.commit()
        print(f"SIMULATING: Order {order.order_id} is PAID. Subscription granted.")
        return jsonify({"status": "paid"})
    else:
        # Пока 10 секунд не прошло, возвращаем "в ожидании"
        return jsonify({"status": "pending"})

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


# регистрация блюпринта (добавь после определения маршрутов)
app.register_blueprint(bp)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
