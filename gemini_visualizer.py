import os
import io
import time
from datetime import datetime
from typing import Tuple, Dict
import uuid

from PIL import Image
from google import genai
from google.genai import types

from extensions import db
from models import BodyVisualization, UploadedFile

MODEL_NAME = "gemini-2.5-flash-image-preview"


def _build_prompt(sex: str, metrics: Dict[str, float], variant_label: str, scene_id: str) -> str:
    """
    Финальная версия промпта, сфокусированная на абсолютном фотореализме,
    бескомпромиссной точности по метрикам и максимальном сходстве с аватаром.
    Версия 3.0: Ультра-строгие требования к реализму, детализации, анатомии,
    идентичности лица и запрету стилизации.
    """
    height = metrics.get("height_cm")
    weight = metrics.get("weight_kg")
    fat_pct = metrics.get("fat_pct")
    muscle_pct = metrics.get("muscle_pct")

    if sex == 'female':
        clothing_description = "Plain black sports bra (top) and plain black athletic shorts. Simple, functional, no logos, no embellishments. Matte fabric."
    else:  # male
        clothing_description = "Plain black athletic shorts, bare torso. Simple, functional, no logos, no embellishments. Matte fabric."

    # NEW: Ультра-конкретные фотографические термины для бескомпромиссного реализма
    photo_style = "Hyper-realistic, clinical, high-fidelity studio photograph. Captured with a professional medium-format camera (e.g., Hasselblad H6D-100c) and a prime 120mm macro lens set to f/11 for maximum depth of field and sharpness across the entire body. RAW photo, unedited, unprocessed. Focus on factual visual data, not artistic expression."

    return f"""
# PRIMARY OBJECTIVE: ABSOLUTE PHOTOREALISM & SCIENTIFIC DATA ACCURACY - CRITICAL FAILURE IF NOT MET
The output MUST be a physically accurate, hyper-realistic photographic representation of the human body strictly based on the provided metrics. Any deviation from real-world photographic appearance or metric accuracy is a critical failure. The visual is for medical and analytical purposes only.

# PERSONA
You are a senior forensic medical illustrator and photogrammetry expert. Your only task is to generate a completely objective, non-interpretive, photorealistic reconstruction of a human body from precise scientific data. Your output must serve as irrefutable visual evidence.

# SCENE SETUP
- **Scene ID:** {scene_id} (Maintain 100% visual and environmental consistency across all generations for this ID).
- **Style:** {photo_style}
- **Background:** Pure white (#FFFFFF) seamless studio cyclorama. Absolutely no shadows, textures, or gradients on the background.
- **Camera:** Static, precisely eye-level, directly facing the subject (0-degree angle horizontally and vertically). Focal length 120mm equivalent. NO lens distortion. Full-height shot of the subject.
- **Lighting:** Ultra-flat, multi-point, high-key diffused studio lighting. Eliminate ALL shadows on the subject and background. The goal is to illuminate every surface evenly and expose all details without artistic flair.

# COMPOSITION & FRAMING (ABSOLUTELY NON-NEGOTIABLE)
- **Visibility:** The ENTIRE subject, from the absolute top of the head (including hair) to the absolute soles of the feet, MUST be fully visible within the frame.
- **Margins:** A minimal, consistent white margin (approx. 5-10% of total image height) MUST be present above the head and below the feet.
- **NO CROPPING:** Cropping ANY part of the subject (head, hair, fingers, toes, shoulders, etc.) at the image edges is a CRITICAL FAILURE.

# SUBJECT
- **Pose:** Strict anatomical A-pose (T-pose with arms slightly lowered, palms facing forward, fingers naturally relaxed). Standing perfectly straight, feet slightly apart, neutral posture.
- **Identity (CRITICAL):** The face MUST be an **EXACT, UNALTERED** match to the provided avatar image. Do NOT generate a different face. Do NOT stylize or "beautify" the face. Integrate it seamlessly into the generated body, maintaining all facial features, expression, and likeness precisely as in the avatar.
- **Clothing:** {clothing_description}

# BODY SPECIFICATION FOR "{variant_label}" (SCIENTIFICALLY BINDING)
Render the body with anatomically and physiologically accurate proportions and mass distribution based EXCLUSIVELY on these exact numerical values. NO artistic interpretation of these metrics.
- **sex:** {sex}
- **height_cm:** {height} (Precise height from head crown to sole of foot).
- **weight_kg:** {weight} (Total body mass reflected accurately in overall volume and density).
- **fat_percent:** {fat_pct} (This is a DIRECT, LITERAL translation to subcutaneous fat accumulation. High values MUST show significant adipose tissue, softer contours, and minimal to no muscle definition. Low values MUST show tight skin and visible muscle striations if muscle_percent is high).
- **muscle_percent:** {muscle_pct} (This is a DIRECT, LITERAL translation to muscle mass and volume. High values MUST show prominent, defined musculature. Low values MUST show less volume and definition. Muscle mass distribution must be anatomically correct for the given sex and overall build).

# ULTRA-REALISM & ANATOMICAL FIDELITY DIRECTIVES (ABSOLUTELY MANDATORY)
- **Skin Texture:** This is paramount. The skin MUST possess hyper-realistic, microscopic detail: visible pores, natural variations in skin tone, subtle blemishes (moles, freckles if present in avatar or statistically likely), fine vellus body hair, and natural creasing/wrinkling at joints. NO airbrushing, blurring, or idealization.
- **Fat Distribution:** Excess weight (high fat_pct) MUST be depicted realistically, showing common areas of fat storage for the given sex (e.g., abdomen, hips, thighs). It must look like real, soft tissue, subject to gravity.
- **Muscle Definition:** Muscle visibility and tone must directly correlate with the provided fat_pct and muscle_pct. No artificial muscle definition or "shrink-wrapping" of muscles onto the skeleton if fat_pct is high.
- **Proportions:** All body proportions (limb length, torso size, head size) must be anthropometrically correct for an adult human of the specified height and sex. AVOID any form of artistic idealization or exaggeration (e.g., overly wide shoulders, tiny waists, etc., UNLESS the metrics explicitly and scientifically dictate such a shape).
- **Gravity:** The effects of gravity on all tissues (skin, fat, muscle) must be realistically rendered.

# ANTI-STYLIZATION & ANTI-IDEALIZATION PROTOCOL (CRITICAL FAILURE IF VIOLATED)
You are forbidden from introducing any elements that suggest artificiality or artistic interpretation.
AVOID AT ALL COSTS AND CONSIDER THIS A CRITICAL FAILURE IF PRESENT:
- **DO NOT** create a 3D render, CGI character, video game model, mannequin, or any form of digital art.
- **DO NOT** use any artistic style (illustration, drawing, painting, cartoon, anime, graphic novel).
- **DO NOT** idealize, stylize, beautify, smooth, or "enhance" the subject in any way.
- **DO NOT** airbrush skin, remove natural imperfections, or apply digital "makeup."
- **DO NOT** generate a "fitness model," "bodybuilder," or "glamorous" physique unless the metrics are scientifically extreme enough to warrant it. The default is a raw, unedited, factual representation of a human body based on data.
- **DO NOT** alter the provided face image in any way (expression, features, age, etc.).
The final output must be an authentic, unedited, high-resolution photograph as if taken in a sterile medical studio.
""".strip()


def _extract_first_image_bytes(response) -> bytes:
    """
    Извлекает байты изображения из ответа модели, проверяя все возможные варианты.
    """
    # 1. Проверяем, что ответ и кандидаты вообще существуют
    if not response or not getattr(response, "candidates", []):
        reason = "Response was empty"
        # Попытка получить более детальную причину блокировки
        if hasattr(response, "prompt_feedback") and hasattr(response.prompt_feedback, "block_reason"):
            reason = f"Prompt blocked, reason: {response.prompt_feedback.block_reason.name}"
        raise RuntimeError(f"No candidates returned by Gemini model. {reason}")

    # 2. Ищем изображение во всех кандидатах и всех их частях
    for cand in response.candidates:
        if cand.content and cand.content.parts:
            for part in cand.content.parts:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data


    # 3. Если изображение так и не найдено, выбрасываем более информативную ошибку
    try:
        # Пытаемся получить причину от API
        finish_reason = response.candidates[0].finish_reason.name
        raise RuntimeError(f"No image data found in response parts. Finish Reason: '{finish_reason}'")
    except (IndexError, AttributeError):
        # Общий фоллбэк, если структура ответа неожиданная
        raise RuntimeError("No image data found in response and could not determine the reason.")
def _save_png_to_db(raw_bytes: bytes, user_id: int, base_name: str) -> str:
    """Сохраняет PNG в БД и возвращает уникальное имя файла."""
    unique_filename = f"viz_{user_id}_{base_name}_{uuid.uuid4().hex}.png"
    new_file = UploadedFile(
        filename=unique_filename,
        content_type='image/png',
        data=raw_bytes,
        size=len(raw_bytes),
        user_id=user_id
    )
    db.session.add(new_file)
    return unique_filename


def _compute_pct(value: float, weight: float) -> float:
    if not value or not weight or weight <= 0:
        return 0.0
    return round(100.0 * float(value) / float(weight), 2)


def generate_for_user(user, avatar_bytes: bytes, metrics_current: Dict[str, float], metrics_target: Dict[str, float]) -> Tuple[str, str]:
    """
    Генерирует 2 изображения и СОХРАНЯЕТ В БД.
    Возвращает имена файлов.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    client = genai.Client(api_key=api_key)

    ts = int(time.time())
    scene_id = f"scene-{uuid.uuid4().hex}"

    # текущая
    prompt_curr = _build_prompt(user.sex or "male", metrics_current, "current", scene_id)
    contents_curr = [
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes)),
        types.Part(text=prompt_curr),
    ]
    resp_curr = client.models.generate_content(model=MODEL_NAME, contents=contents_curr)
    curr_png = _extract_first_image_bytes(resp_curr)

    # целевая
    prompt_tgt = _build_prompt(user.sex or "male", metrics_target, "target", scene_id)
    contents_tgt = [
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=avatar_bytes)),
        types.Part(text=prompt_tgt),
    ]
    resp_tgt = client.models.generate_content(model=MODEL_NAME, contents=contents_tgt)
    tgt_png = _extract_first_image_bytes(resp_tgt)

    # сохраняем в БД
    curr_filename = _save_png_to_db(curr_png, user.id, f"{ts}_current")
    tgt_filename = _save_png_to_db(tgt_png, user.id, f"{ts}_target")

    return curr_filename, tgt_filename


def create_record(user, curr_filename: str, tgt_filename: str, metrics_current: Dict[str, float],
                  metrics_target: Dict[str, float]):
    vis = BodyVisualization(
        user_id=user.id,
        metrics_current=metrics_current,
        metrics_target=metrics_target,
        image_current_path=curr_filename,
        image_target_path=tgt_filename,
        status="done",
        provider="gemini"
    )
    db.session.add(vis)
    db.session.commit()
    return vis