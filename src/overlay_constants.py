# src/overlay_constants.py
"""Константы оверлея перевода."""

TRANSPARENT_COLOR = "#010101"

ALPHA_SOLID_THRESHOLD = 240
ALPHA_TRANSPARENT_THRESHOLD = 15

# ИЗМЕНЕНО: 3 → 5. Блок не удаляется из _stable_blocks после
# 3 пропущенных кадров, а живёт 5 — это даёт запас, пока
# Android восстанавливает разбитый блок.
STABLE_MISS_LIMIT = 5

# ИЗМЕНЕНО: 2 → 3. Блок не исчезает с оверлея после 2 пропущенных
# кадров, а живёт 3 — сглаживает flapping ("Запустить главу"
# мигает по паттерну [1, 0, 1, 0, 1, ...]).
VISIBLE_MISS_LIMIT = 3

STABLE_TEXT_UPDATE_THRESHOLD = 4
STABLE_IOU_THRESHOLD = 0.2

DEDUP_IOU_THRESHOLD = 0.8
DEDUP_TEXT_MAX_DIST = 30.0

SIZE_CHANGE_THRESHOLD = 30
POS_CHANGE_THRESHOLD = 25

BBOX_SETTLE_FRAMES = 1
CENTER_MATCH_DIST = 60.0
SIZE_ROUND_TO = 4
FRAME_SIZE_TOLERANCE = 0.05
STALE_DATA_TIMEOUT = 3.0
EMPTY_FRAME_STREAK_TO_CLEAR = 2

_REDRAW_TRIGGER_KEYS = ("ocr_padding", "debug", "overlay_alpha")

# ИЗМЕНЕНО: 4000 → 2000. Порог отбрасывания мелких блоков
# уменьшен вдвое. Раньше блоки "Время" (100x35 = 3500) и
# "Кледр" (122x22 = 2684) отбрасывались как "слишком мелкие",
# хотя это осмысленные тексты. Теперь они проходят фильтр.
# Настоящий мелкий мусор (< 2000 px²) по-прежнему отбрасывается.
# HUD-элементы ("Speed: Max:", 3135 px²) фильтруются на Android
# через android.pc_ocr_drop_small и до ПК не доходят.
MIN_BLOCK_AREA = 2000

STABLE_KEY_GRID = 64
TEXT_WIDTH_TOLERANCE = 4
TEXT_SIMILARITY_THRESHOLD = 0.7
MIXED_ALPHABET_LATIN_RATIO = 0.30
GEOMETRY_RECREATE_THRESHOLD = 3.0
HUD_MAX_LEN = 3

# Ранее: 0.85 → 0.95.
# "chapter" vs "chaper" (ratio ~0.92) не считается фрагментом.
FRAGMENT_FUZZY_THRESHOLD = 0.95

FRAGMENT_KNOWN_MISS_LIMIT = 2
FRAGMENT_MULTIWORD_MIN_LEN = 10
FRAGMENT_MULTIWORD_MIN_WORDS = 2
SINGLE_LINE_MAX_CHARS = 20
MIN_SINGLE_LINE_FONT = 10
TRUNCATE_ELLIPSIS = "…"
SETTINGS_DEBOUNCE_MS = 150

RARE_CHARS = set(
    "şŞţŢģĢżŻýÝþÞðÐłŁńŃćĆęĘąĄśŚźŹğĞıİřŘěĚůŮžŽ"
    "áÁéÉíÍóÓúÚäÄöÖüÜßçÇñÑõÕãÃåÅæÆøØđĐħĦĸŀŉŧŦ"
    "ƀƃƈƌƒƙƚƞơƠưƯƴƵǆǉǌǎǏǒǔǖǘǚǜșȘțȚȝ"
)

MAX_RARE_CHARS = 2
MAX_CONSECUTIVE_CONSONANTS = 5
VOWELS = set("aeiouyAEIOUYаеёиоуыэюяАЕЁИОУЫЭЮЯ")