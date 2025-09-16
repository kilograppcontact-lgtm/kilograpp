# models.py
from datetime import datetime, date, timedelta, time as dt_time
from sqlalchemy import UniqueConstraint
from extensions import db

# ------------------ USERS / SUBSCRIPTION ------------------

class User(db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    date_of_birth = db.Column(db.Date)
    renewal_reminder_last_shown_on = db.Column(db.Date)
    renewal_telegram_sent = db.Column(db.Boolean, default=False)

    # Глобальные флаги уведомлений
    telegram_notify_enabled = db.Column(db.Boolean, default=True)
    notify_trainings = db.Column(db.Boolean, default=True)
    notify_subscription = db.Column(db.Boolean, default=True)

    # Цели
    fat_mass_goal = db.Column(db.Float, nullable=True)
    muscle_mass_goal = db.Column(db.Float, nullable=True)

    is_trainer = db.Column(db.Boolean, default=False, nullable=False)
    avatar = db.Column(db.String(200), nullable=False, default='i.webp')

    analysis_comment = db.Column(db.Text)
    telegram_chat_id = db.Column(db.String(50), nullable=True)
    telegram_code = db.Column(db.String(10), nullable=True)
    show_welcome_popup = db.Column(db.Boolean, default=False, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    # отношения
    subscription = db.relationship('Subscription', backref=db.backref('user', uselist=False), uselist=False)

    @property
    def has_subscription(self):
        return self.is_trainer or (self.subscription and self.subscription.is_active)

    # ----- Доступ к последнему анализу (динамические свойства) -----
    def _get_latest_analysis(self):
        if not hasattr(self, '_cached_latest_analysis'):
            self._cached_latest_analysis = BodyAnalysis.query.filter_by(user_id=self.id)\
                .order_by(BodyAnalysis.timestamp.desc()).first()
        return self._cached_latest_analysis

    @property
    def latest_analysis(self):
        return self._get_latest_analysis()

    @property
    def height(self):
        a = self._get_latest_analysis()
        return a.height if a else None

    @property
    def weight(self):
        a = self._get_latest_analysis()
        return a.weight if a else None

    @property
    def muscle_mass(self):
        a = self._get_latest_analysis()
        return a.muscle_mass if a else None

    @property
    def muscle_percentage(self):
        a = self._get_latest_analysis()
        return a.muscle_percentage if a else None

    @property
    def body_water(self):
        a = self._get_latest_analysis()
        return a.body_water if a else None

    @property
    def protein_percentage(self):
        a = self._get_latest_analysis()
        return a.protein_percentage if a else None

    @property
    def bone_mineral_percentage(self):
        a = self._get_latest_analysis()
        return a.bone_mineral_percentage if a else None

    @property
    def skeletal_muscle_mass(self):
        a = self._get_latest_analysis()
        return a.skeletal_muscle_mass if a else None

    @property
    def visceral_fat_rating(self):
        a = self._get_latest_analysis()
        return a.visceral_fat_rating if a else None

    @property
    def metabolism(self):
        a = self._get_latest_analysis()
        return a.metabolism if a else None

    @property
    def waist_hip_ratio(self):
        a = self._get_latest_analysis()
        return a.waist_hip_ratio if a else None

    @property
    def body_age(self):
        a = self._get_latest_analysis()
        return a.body_age if a else None

    @property
    def fat_mass(self):
        a = self._get_latest_analysis()
        return a.fat_mass if a else None

    @property
    def bmi(self):
        a = self._get_latest_analysis()
        return a.bmi if a else None

    @property
    def fat_free_body_weight(self):
        a = self._get_latest_analysis()
        return a.fat_free_body_weight if a else None


class Subscription(db.Model):
    __tablename__ = "subscription"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    start_date = db.Column(db.Date, default=date.today)
    end_date = db.Column(db.Date, nullable=True)
    source = db.Column(db.String(50))
    status = db.Column(db.String(20), nullable=False, default='active')  # active, frozen, cancelled
    remaining_days_on_freeze = db.Column(db.Integer, nullable=True)

    @property
    def is_active(self):
        today = date.today()
        return (self.status == 'active'
                and self.start_date <= today
                and (self.end_date is None or self.end_date >= today))


class Order(db.Model):
    __tablename__ = "order"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    order_id = db.Column(db.String(36), unique=True, nullable=False)
    kaspi_invoice_id = db.Column(db.String(100), nullable=True)
    subscription_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('orders', lazy=True))


# ------------------ GROUPS / CHAT ------------------

class Group(db.Model):
    __tablename__ = "group"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    trainer_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    trainer = db.relationship('User', backref=db.backref('own_group', uselist=False))
    members = db.relationship('GroupMember', back_populates='group', cascade='all, delete-orphan')
    messages = db.relationship('GroupMessage', back_populates='group', cascade='all, delete-orphan')
    tasks = db.relationship('GroupTask', backref=db.backref('group'), cascade='all, delete-orphan', lazy='dynamic')


class GroupMember(db.Model):
    __tablename__ = "group_member"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('group_id', 'user_id', name='uq_group_user'),)

    group = db.relationship('Group', back_populates='members')
    user = db.relationship('User', backref=db.backref('groups', lazy='dynamic'))


class GroupMessage(db.Model):
    __tablename__ = "group_message"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    image_file = db.Column(db.String(200), nullable=True)

    group = db.relationship('Group', back_populates='messages')
    user = db.relationship('User')
    reactions = db.relationship('MessageReaction', back_populates='message', cascade='all, delete-orphan')


class MessageReaction(db.Model):
    __tablename__ = "message_reaction"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('group_message.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reaction_type = db.Column(db.String(20), nullable=False, default='👍')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', name='uq_message_user_reaction'),)

    message = db.relationship('GroupMessage', back_populates='reactions')
    user = db.relationship('User')


class GroupTask(db.Model):
    __tablename__ = "group_task"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    trainer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_announcement = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.Date, nullable=True)

    trainer = db.relationship('User')


# ------------------ DIET / ACTIVITY ------------------

class MealLog(db.Model):
    __tablename__ = 'meal_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    meal_type = db.Column(db.String(20), nullable=False)  # 'breakfast','lunch','dinner','snack'
    name = db.Column(db.String(100), nullable=True)
    verdict = db.Column(db.String(200), nullable=True)
    calories = db.Column(db.Integer, nullable=False)
    protein = db.Column(db.Float, nullable=False)
    fat = db.Column(db.Float, nullable=False)
    carbs = db.Column(db.Float, nullable=False)
    analysis = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('meals', lazy=True))
    __table_args__ = (UniqueConstraint('user_id', 'date', 'meal_type', name='uq_user_date_meal'),)


class Activity(db.Model):
    __tablename__ = "activity"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.Date, default=date.today)
    steps = db.Column(db.Integer)
    active_kcal = db.Column(db.Integer)
    resting_kcal = db.Column(db.Integer)
    distance_km = db.Column(db.Float)
    heart_rate_avg = db.Column(db.Integer)
    source = db.Column(db.String(50))

    user = db.relationship("User", backref=db.backref("activities", lazy=True))


class Diet(db.Model):
    __tablename__ = "diet"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, default=date.today)
    breakfast = db.Column(db.Text)
    lunch = db.Column(db.Text)
    dinner = db.Column(db.Text)
    snack = db.Column(db.Text)
    total_kcal = db.Column(db.Integer)
    protein = db.Column(db.Float)
    fat = db.Column(db.Float)
    carbs = db.Column(db.Float)

    user = db.relationship('User', backref=db.backref('diets', lazy=True))


# ------------------ TRAININGS ------------------

class Training(db.Model):
    __tablename__ = 'trainings'

    id = db.Column(db.Integer, primary_key=True)
    trainer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    meeting_link = db.Column(db.String(255), nullable=False)

    title = db.Column(db.String(120), nullable=False, default="Онлайн-тренировка")
    description = db.Column(db.Text, default="")
    date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    location = db.Column(db.String(120))
    capacity = db.Column(db.Integer, default=10)
    is_public = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    trainer = db.relationship('User', backref=db.backref('trainings', lazy=True))
    signups = db.relationship('TrainingSignup', backref='training', cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint('trainer_id', 'date', 'start_time', name='uq_trainer_date_start'),)

    # UI helper
    def to_dict(self, me_id=None):
        mine = (me_id is not None and self.trainer_id == me_id)

        now = datetime.now()
        start_dt = datetime.combine(self.date, self.start_time)
        end_dt = datetime.combine(self.date, self.end_time)
        is_past = now >= end_dt
        link_visible_at = start_dt - timedelta(minutes=10)

        joined = False
        if me_id:
            joined = any(s.user_id == me_id for s in self.signups)

        seats_taken = len(self.signups)
        spots_left = max(0, (self.capacity or 0) - seats_taken)

        can_open_link = False
        if mine:
            can_open_link = True
        elif joined and (now >= link_visible_at) and not is_past:
            can_open_link = True

        payload = {
            "id": self.id,
            "trainer_id": self.trainer_id,
            "trainer_name": (self.trainer.name if self.trainer and getattr(self.trainer, "name", None) else "Тренер"),
            "title": self.title or "Онлайн-тренировка",
            "date": self.date.strftime("%Y-%m-%d"),
            "start_time": self.start_time.strftime("%H:%M"),
            "end_time": self.end_time.strftime("%H:%M"),
            "mine": mine,
            "joined": joined,
            "is_past": is_past,
            "spots_left": spots_left,
            "link_visible_at": link_visible_at.isoformat(timespec="minutes"),
            "can_open_link": can_open_link
        }
        if can_open_link:
            payload["meeting_link"] = self.meeting_link
        return payload


class TrainingSignup(db.Model):
    __tablename__ = 'training_signups'

    id = db.Column(db.Integer, primary_key=True)
    training_id = db.Column(db.Integer, db.ForeignKey('trainings.id', ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete="CASCADE"), nullable=False, index=True)
    notified_1h = db.Column(db.Boolean, default=False)
    notified_start = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('training_id', 'user_id', name='uq_training_user'),)


# ------------------ BODY ANALYSIS ------------------

class BodyAnalysis(db.Model):
    __tablename__ = "body_analysis"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    height = db.Column(db.Integer)
    weight = db.Column(db.Float)
    muscle_mass = db.Column(db.Float)
    muscle_percentage = db.Column(db.Float)
    body_water = db.Column(db.Float)
    protein_percentage = db.Column(db.Float)
    bone_mineral_percentage = db.Column(db.Float)
    skeletal_muscle_mass = db.Column(db.Float)
    visceral_fat_rating = db.Column(db.Float)
    metabolism = db.Column(db.Integer)
    waist_hip_ratio = db.Column(db.Float)
    body_age = db.Column(db.Integer)
    fat_mass = db.Column(db.Float)
    bmi = db.Column(db.Float)
    fat_free_body_weight = db.Column(db.Float)

    user = db.relationship('User', backref=db.backref('analyses', lazy=True))


# ------------------ SETTINGS / REMINDERS ------------------

class UserSettings(db.Model):
    __tablename__ = "user_settings"

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    telegram_notify_enabled = db.Column(db.Boolean, default=True, nullable=False)
    notify_trainings = db.Column(db.Boolean, default=True, nullable=False)
    notify_subscription = db.Column(db.Boolean, default=True, nullable=False)
    notify_meals = db.Column(db.Boolean, default=True, nullable=False)
    meal_timezone = db.Column(db.String(64), default="Europe/Moscow")

    user = db.relationship("User", backref=db.backref("settings", uselist=False))


class MealReminderLog(db.Model):
    __tablename__ = "meal_reminder_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True, nullable=False)
    meal_type = db.Column(db.String(16), nullable=False)  # breakfast|lunch|dinner
    date_sent = db.Column(db.Date, nullable=False, default=date.today, index=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "meal_type", "date_sent", name="u_meal_reminder_once_per_day"),
    )
