import os
import logging
import aiohttp
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# === Конфигурация ===
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:5000").rstrip("/")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_NAME = os.getenv("KILOGRAI_MODEL", "gpt-4o")
CLASSIFICATION_MAX_TOKENS = 10
CLASSIFICATION_TEMPERATURE = 0.0
GENERAL_MAX_TOKENS = 400
GENERAL_TEMPERATURE = 0.5
SPECIALIZED_MAX_TOKENS = 400
SPECIALIZED_TEMPERATURE = 0.4

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# === Промпты ===
CLASSIFICATION_PROMPT_TEMPLATE = """
Твоя задача — классифицировать сообщение пользователя в одну из категорий. Ответь ТОЛЬКО ОДНИМ СЛОВОМ из списка.

КАТЕГОРИИ:
- `INTENT_DIET`: про диету (замена блюда, обсуждение).
- `INTENT_BODY`: про анализ показателей тела (вес, жир, метаболизм).
- `INTENT_MENU`: хочет открыть меню.
- `INTENT_ADD_MEAL_CLARIFY`: хочет добавить еду, но не уточнил тип.
- `INTENT_ADD_MEAL_BREAKFAST`: хочет добавить завтрак.
- `INTENT_ADD_MEAL_LUNCH`: хочет добавить обед.
- `INTENT_ADD_MEAL_DINNER`: хочет добавить ужин.
- `INTENT_ADD_MEAL_SNACK`: хочет добавить перекус.
- `INTENT_GENERAL`: любой другой вопрос о платформе или приветствие.

Сообщение пользователя: "{user_message}"
Твой ответ (одно слово):
"""

# <<< ИЗМЕНЕНО: Обновлены правила для общего промпта
GENERAL_PROMPT_TEMPLATE = """
Ты — Kilo, дружелюбный AI-ассистент платформы Kilogr.app. 😊
Твоя миссия — помогать пользователям в их фитнес-пути.

Правила:
- Будь экспертом по Kilogr.app (Профиль, Диета, Анализ фото, Тренировки, Группы, Визуализация).
- Используй позитивный тон и смайлики (💪, 🥗, ✨).
- Отвечай СТРОГО на вопросы по теме фитнеса и платформы. На остальное — вежливо отказывай.
- Не здоровайся, если пользователь не поздоровался в своем последнем сообщении.
- Используй Markdown.

Предыдущий диалог:
{chat_history}

Ответь на последнее сообщение пользователя: "{user_message}"
"""


# === Декоратор для проверки регистрации (без изменений) ===
def registered_user_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = update.effective_chat.id
        if context.user_data.get('is_registered'):
            return await func(update, context, *args, **kwargs)

        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{BACKEND_URL}/api/is_registered/{chat_id}") as resp:
                    if resp.status == 200:
                        context.user_data['is_registered'] = True
                        return await func(update, context, *args, **kwargs)
                    else:
                        logging.info(f"AI Assistant: Ignoring action from unregistered user {chat_id}.")
                        if update.message:
                            await update.message.reply_text(
                                "Чтобы пользоваться ассистентом, пожалуйста, привяжите ваш аккаунт. Отправьте /start и введите код из личного кабинета.")
                        return ConversationHandler.END
        except aiohttp.ClientError:
            logging.error(f"AI Assistant: Network error checking registration for {chat_id}.")
            if update.message:
                await update.message.reply_text(
                    "Не удалось связаться с сервером для проверки регистрации. Попробуйте позже.")
            return ConversationHandler.END

    return wrapper


# === Специализированные обработчики (ИЗМЕНЕНО) ===
async def _handle_diet_intent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BACKEND_URL}/api/current_diet/{chat_id}") as diet_resp, \
                    session.get(f"{BACKEND_URL}/api/user_info/{chat_id}") as user_resp:

                if diet_resp.status != 200:
                    await update.message.reply_text(
                        "🥗 Чтобы я мог помочь с диетой, сгенерируйте её в профиле на test.kilogr.app")
                else:
                    diet_data = await diet_resp.json()
                    user_data = await user_resp.json() if user_resp.status == 200 else {}

                    user_name = user_data.get('name')
                    # Создаем контекстную строку с данными пользователя
                    user_context_str = f"""Вот информация о пользователе, которую ты можешь использовать для персонализации ответа:
- Имя: {user_name or 'не указано'}
- Пол: {user_data.get('sex', 'не указан')}
- Дата рождения: {user_data.get('date_of_birth', 'не указана')}
- Цель по жировой массе: {user_data.get('fat_mass_goal') or 'не установлена'} кг
- Цель по мышечной массе: {user_data.get('muscle_mass_goal') or 'не установлена'} кг
"""

                    # <<< ИЗМЕНЕНО: Промпт полностью переработан
                    prompt = f"""Ты — Kilo, экспертный диетолог и дружелюбный фитнес-помощник.
Твои ответы всегда короткие, по делу, позитивные и доброжелательные.

{user_context_str}

Вот диета пользователя на сегодня:
{diet_data}

Его вопрос: "{user_message}"

Инструкции:
1.  **Приветствие**: Не здоровайся, если пользователь не поздоровался в своем вопросе.
2.  **Обращение**: Если имя пользователя ({user_name}) известно, обращайся к нему. Если нет, не используй обращение "Пользователь", а отвечай безлично.
3.  **Задачи**: Проанализируй вопрос и выполни одну из задач:
    - Если просят **замену блюда**, предложи 1-2 конкретных варианта с КБЖУ.
    - Если спрашивают **"что мне поесть"**, кратко перечисли его приемы пищи из диеты.
    - Если просят **оценить диету**, дай краткий анализ с учетом его целей.
    - На другие вопросы о **калорийности, продуктах, составе** дай точный ответ.

Твой ответ:
"""

                    response = client.chat.completions.create(model=MODEL_NAME,
                                                              messages=[{"role": "user", "content": prompt}],
                                                              temperature=SPECIALIZED_TEMPERATURE,
                                                              max_tokens=SPECIALIZED_MAX_TOKENS)
                    final_response = response.choices[0].message.content

                    chat_history = context.user_data.setdefault('kilo_chat_history', [])
                    chat_history.append({"role": "assistant", "content": final_response})
                    await update.message.reply_text(final_response, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Diet intent failed for user {chat_id}: {e}")
        await update.message.reply_text("Произошла ошибка при обработке вашего запроса по диете. 😕")


async def _handle_body_intent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BACKEND_URL}/api/user_progress/{chat_id}") as progress_resp, \
                    session.get(f"{BACKEND_URL}/api/user_info/{chat_id}") as user_resp:

                if progress_resp.status != 200:
                    await update.message.reply_text(
                        "📈 Чтобы я мог проанализировать прогресс, загрузите хотя бы один анализ тела в профиле.")
                else:
                    progress_data = await progress_resp.json()
                    user_data = await user_resp.json() if user_resp.status == 200 else {}

                    user_name = user_data.get('name')
                    # Создаем контекстную строку с данными пользователя
                    user_context_str = f"""Вот информация о пользователе, которую ты можешь использовать для персонализации ответа:
- Имя: {user_name or 'не указано'}
- Пол: {user_data.get('sex', 'не указан')}
- Цель по жировой массе: {user_data.get('fat_mass_goal') or 'не установлена'} кг
- Цель по мышечной массе: {user_data.get('muscle_mass_goal') or 'не установлена'} кг
"""

                    # <<< ИЗМЕНЕНО: Промпт полностью переработан
                    prompt = f"""Ты — Kilo, дружелюбный и мотивирующий фитнес-эксперт.
Твои ответы всегда короткие, по делу, позитивные и доброжелательные.

{user_context_str}

Вот последние данные по анализу тела пользователя:
{progress_data}

Его вопрос: "{user_message}"

Инструкции:
1.  **Приветствие**: Не здоровайся, если пользователь не поздоровался в своем вопросе.
2.  **Обращение**: Если имя пользователя ({user_name}) известно, обращайся к нему. Если нет, не используй обращение "Пользователь", а отвечай безлично.
3.  **Анализ**: Кратко и позитивно проанализируй его текущие показатели ('latest'), сравнивая их с целями.
4.  **Динамика**: Если есть данные для сравнения ('previous'), отметь изменения.
5.  **Совет**: Дай 1-2 простых, но конкретных совета для достижения его целей.
6.  **Мотивация**: Заверши ответ мотивирующей фразой.

Твой ответ:
"""
                    response = client.chat.completions.create(model=MODEL_NAME,
                                                              messages=[{"role": "user", "content": prompt}],
                                                              temperature=SPECIALIZED_TEMPERATURE,
                                                              max_tokens=SPECIALIZED_MAX_TOKENS)
                    final_response = response.choices[0].message.content

                    chat_history = context.user_data.setdefault('kilo_chat_history', [])
                    chat_history.append({"role": "assistant", "content": final_response})
                    await update.message.reply_text(final_response, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Body intent failed for user {chat_id}: {e}")
        await update.message.reply_text("Произошла ошибка при анализе ваших показателей. 😕")


# === Основной обработчик текста (точка входа в ConversationHandler) ===
@registered_user_only
async def kilo_entry_point_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # <<< ИСПРАВЛЕНО: Локальный импорт для решения проблемы циклической зависимости
    from telegram_bot import (
        show_main_menu,
        ask_photo_for_meal,
        NUTRITION_MENU_KEYBOARD,
        SELECT_MENU,
        ASK_PHOTO
    )

    user_message = update.message.text
    if not user_message or user_message.startswith('/'):
        return

    chat_history = context.user_data.setdefault('kilo_chat_history', [])
    chat_history.append({"role": "user", "content": user_message})

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    try:
        classification_prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(user_message=user_message)
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": classification_prompt}],
            temperature=CLASSIFICATION_TEMPERATURE,
            max_tokens=CLASSIFICATION_MAX_TOKENS
        )
        intent = response.choices[0].message.content.strip()
        logging.info(f"AI Assistant classified intent: '{intent}' for user {update.effective_chat.id}")

        # --- Логика диспетчеризации с возвратом состояний ---

        if intent == 'INTENT_DIET':
            await _handle_diet_intent(update, context)
            return SELECT_MENU

        elif intent == 'INTENT_BODY':
            await _handle_body_intent(update, context)
            return SELECT_MENU

        elif intent == 'INTENT_MENU':
            await show_main_menu(update, context)
            return SELECT_MENU

        elif intent == 'INTENT_ADD_MEAL_CLARIFY':
            keyboard = InlineKeyboardMarkup(NUTRITION_MENU_KEYBOARD)
            await update.message.reply_text(
                "Конечно! 🍽️ Раздел *Питание* — выберите действие:",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            return SELECT_MENU

        elif intent.startswith('INTENT_ADD_MEAL_'):
            meal_type = intent.replace('INTENT_ADD_MEAL_', '').lower()

            class FakeQuery:
                def __init__(self, meal):
                    self.data = f"meal_{meal}"
                    self.message = update.message

                async def answer(self): pass

                async def edit_message_text(self, *args, **kwargs):
                    await update.message.reply_text(*args, **kwargs)

            fake_update = Update(update.update_id, message=update.message, callback_query=FakeQuery(meal_type))

            return await ask_photo_for_meal(fake_update, context)

        else:  # INTENT_GENERAL или неизвестный интент
            history_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in chat_history[-6:-1]])
            general_prompt = GENERAL_PROMPT_TEMPLATE.format(chat_history=history_str, user_message=user_message)
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": general_prompt}],
                temperature=GENERAL_TEMPERATURE,
                max_tokens=GENERAL_MAX_TOKENS
            )
            general_response = response.choices[0].message.content
            chat_history.append({"role": "assistant", "content": general_response})
            await update.message.reply_text(general_response, parse_mode="Markdown")

            return ConversationHandler.END

    except Exception as e:
        logging.error(f"AI Assistant call failed: {e}")
        await update.message.reply_text("🤔 Ой, что-то пошло не так с моим AI. Попробуйте позже.")
        return ConversationHandler.END

