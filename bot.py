import csv
import os
import re
import time
import asyncio
import logging
import html
from io import BytesIO
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

logging.basicConfig(
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('telegram.ext').setLevel(logging.WARNING)

GENIUS_API_BASE = 'https://api.genius.com'
TELEGRAM_MESSAGE_LIMIT = 4096
HISTORY_PATH = Path('history.csv')
CSV_HEADER = ['Дата и время', 'ID пользователя', 'Username', 'Поисковый запрос', 'Результат']
SEARCH_COOLDOWN_SECONDS = 5
SECTION_LINE_PATTERN = re.compile(r'^\[[^\[\]]+\]$')
LANGUAGE_MODES = {'original', 'ru', 'en', 'any'}
ADMIN_CALLBACK_HISTORY = 'admin_history_txt'
ADMIN_CALLBACK_TODO = 'admin_todo_stub'


def ensure_history_file():
    if HISTORY_PATH.exists():
        return

    with HISTORY_PATH.open('w', newline='', encoding='utf-8') as csv_file:
        writer = csv.writer(csv_file, delimiter=';')
        writer.writerow(CSV_HEADER)


def log_query(user_id, username, query, status):
    ensure_history_file()
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with HISTORY_PATH.open('a', newline='', encoding='utf-8') as csv_file:
        writer = csv.writer(csv_file, delimiter=';')
        writer.writerow([timestamp, user_id, username, query, status])


def get_admin_ids():
    raw = (os.getenv('ADMIN_IDS') or os.getenv('ADMIN_ID') or '').strip()
    if not raw:
        return set()

    parts = [p.strip() for p in raw.replace(';', ',').split(',')]
    ids = set()
    for p in parts:
        if not p:
            continue
        if not p.isdigit():
            continue
        ids.add(int(p))
    return ids


def is_admin_user(user):
    admin_ids = get_admin_ids()
    if not admin_ids or not user:
        return False
    return user.id in admin_ids


def build_history_txt():
    ensure_history_file()
    content = HISTORY_PATH.read_text(encoding='utf-8')
    return content


async def admin(update, context):
    if not is_admin_user(update.effective_user):
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton('История (txt)', callback_data=ADMIN_CALLBACK_HISTORY)],
            [InlineKeyboardButton('Скоро (заглушка)', callback_data=ADMIN_CALLBACK_TODO)],
        ]
    )
    await update.message.reply_text('Админ панель', reply_markup=keyboard)


async def admin_callback(update, context):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    if not is_admin_user(query.from_user):
        return

    if query.data == ADMIN_CALLBACK_TODO:
        await query.message.reply_text('Потом сделаю')
        return

    if query.data != ADMIN_CALLBACK_HISTORY:
        return

    txt = build_history_txt()
    data = txt.encode('utf-8-sig')
    bio = BytesIO(data)
    bio.seek(0)
    await query.message.reply_document(
        document=InputFile(bio, filename='history.txt'),
        caption='История поисков',
    )


def extract_lyrics_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    containers = soup.select("div[data-lyrics-container='true']")
    if not containers:
        return ''

    chunks = []
    for container in containers:
        text = container.get_text('\n', strip=True)
        if text:
            chunks.append(text)

    raw_lyrics = '\n'.join(chunks)
    return clean_lyrics_text(raw_lyrics)


def is_noise_line(line):
    lowered = line.lower().strip()
    if not lowered:
        return False

    if re.match(r'^\d+\s+contributors?$', lowered):
        return True
    if lowered in {
        'translations',
        'read more',
        'you might also like',
        'embed',
        'see upcoming rap shows',
    }:
        return True
    if 'translation' in lowered and len(lowered.split()) <= 3:
        return True
    if re.match(r'^[a-zа-яё\s]+ lyrics$', lowered):
        return True
    if re.match(r'^[a-zа-яё\s\(\)]+$', lowered) and len(lowered.split()) <= 2 and 'lyrics' not in lowered:
        return lowered.istitle() or lowered.isalpha()

    return False


def merge_broken_structural_lines(lines):
    merged_lines = []
    idx = 0

    while idx < len(lines):
        current = lines[idx].strip()
        if not current:
            idx += 1
            continue

        if '[' in current and ']' not in current:
            parts = [current]
            idx += 1
            while idx < len(lines):
                nxt = lines[idx].strip()
                if nxt:
                    parts.append(nxt)
                if ']' in nxt:
                    break
                idx += 1

            merged = ' '.join(parts)
            merged = re.sub(r'\[\s+', '[', merged)
            merged = re.sub(r'\s+\]', ']', merged)
            merged = re.sub(r'\s{2,}', ' ', merged).strip()
            merged_lines.append(merged)
            idx += 1
            continue

        if current.count('(') > current.count(')'):
            parts = [current]
            balance = current.count('(') - current.count(')')
            idx += 1
            while idx < len(lines) and balance > 0:
                nxt = lines[idx].strip()
                if nxt:
                    parts.append(nxt)
                    balance += nxt.count('(') - nxt.count(')')
                idx += 1

            merged = ' '.join(parts)
            merged = re.sub(r'\(\s+', '(', merged)
            merged = re.sub(r'\s+\)', ')', merged)
            merged = re.sub(r'\s{2,}', ' ', merged).strip()
            merged_lines.append(merged)
            continue

        if current == ']' and merged_lines and '[' in merged_lines[-1] and ']' not in merged_lines[-1]:
            merged_lines[-1] = f'{merged_lines[-1]}]'
        elif current == ')' and merged_lines and merged_lines[-1].count('(') > merged_lines[-1].count(')'):
            merged_lines[-1] = f'{merged_lines[-1]})'
        else:
            merged_lines.append(current)

        idx += 1

    return merged_lines


def clean_lyrics_text(raw_lyrics):
    lines = [line.strip() for line in raw_lyrics.splitlines()]
    lines = merge_broken_structural_lines(lines)
    lines = [line for line in lines if line]
    lines = [line for line in lines if not is_noise_line(line)]

    first_section_idx = next(
        (idx for idx, line in enumerate(lines) if SECTION_LINE_PATTERN.match(line)),
        None,
    )
    if first_section_idx is not None:
        lines = lines[first_section_idx:]

    cleaned = '\n'.join(lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


def normalize_text(value):
    cleaned = re.sub(r'[^a-z0-9а-яё\s]', ' ', value.lower())
    return re.sub(r'\s+', ' ', cleaned).strip()


def is_translation_hit(result):
    haystack = ' '.join(
        [
            str(result.get('title', '')),
            str(result.get('full_title', '')),
            str(result.get('url', '')),
        ]
    ).lower()
    markers = [
        'translation',
        'translated',
        'english translation',
        'русский перевод',
        'перевод',
        'traduccion',
        'tradução',
        'übersetzung',
    ]
    return any(marker in haystack for marker in markers)


def detect_hit_language_category(result):
    haystack = ' '.join(
        [
            str(result.get('title', '')),
            str(result.get('full_title', '')),
            str(result.get('url', '')),
        ]
    ).lower()
    ru_markers = ['русский перевод', 'перевод', 'russian translation', 'na russkom', 'на русском']
    en_markers = ['english translation', 'translated', 'translation', 'in english']
    translit_markers = ['transliteration', 'romanized', 'translit']

    if any(marker in haystack for marker in translit_markers):
        return 'translit'
    if any(marker in haystack for marker in ru_markers):
        return 'ru_translation'
    if any(marker in haystack for marker in en_markers):
        return 'en_translation'
    return 'original'


def select_best_hit(hits, query, preferred_language='original'):
    if not hits:
        return None

    query_norm = normalize_text(query)
    query_tokens = set(query_norm.split())
    best_original_hit = None
    best_original_score = -10_000
    best_any_hit = None
    best_any_score = -10_000

    for hit in hits:
        result = hit.get('result', {})
        title = result.get('title', '')
        artist = result.get('primary_artist', {}).get('name', '')
        combined_norm = normalize_text(f'{artist} {title}')
        if not combined_norm:
            continue

        combined_tokens = set(combined_norm.split())
        overlap = len(query_tokens & combined_tokens)
        substring_bonus = 2 if query_norm and query_norm in combined_norm else 0
        category = detect_hit_language_category(result)
        score = overlap + substring_bonus

        if preferred_language == 'ru':
            if category == 'ru_translation':
                score += 10
            elif category in {'en_translation', 'translit'}:
                score -= 4
            else:
                score -= 1
        elif preferred_language == 'en':
            if category == 'en_translation':
                score += 10
            elif category in {'ru_translation', 'translit'}:
                score -= 4
            else:
                score -= 1
        elif preferred_language == 'any':
            score += 1 if category != 'translit' else -1
        else:
            if category == 'original':
                score += 1
            else:
                score -= 8

        if score > best_any_score:
            best_any_score = score
            best_any_hit = result

        if category == 'original' and score > best_original_score:
            best_original_score = score
            best_original_hit = result

    if preferred_language == 'original' and best_original_hit is not None:
        return best_original_hit
    return best_any_hit or hits[0].get('result')


def fetch_lyrics_from_genius(
    query, genius_token, preferred_language='original'
):
    headers = {'Authorization': f'Bearer {genius_token}'}
    search_response = requests.get(
        f'{GENIUS_API_BASE}/search',
        headers=headers,
        params={'q': query},
        timeout=15,
    )
    search_response.raise_for_status()

    hits = search_response.json().get('response', {}).get('hits', [])
    if not hits:
        return None

    song_data = select_best_hit(hits, query, preferred_language=preferred_language)
    if not song_data:
        return None

    artist = song_data.get('primary_artist', {}).get('name', 'Неизвестный исполнитель')
    title = song_data.get('title', 'Без названия')
    song_url = song_data.get('url')
    if not song_url:
        return None

    song_page_response = requests.get(song_url, timeout=15)
    song_page_response.raise_for_status()
    lyrics = extract_lyrics_from_html(song_page_response.text)
    if not lyrics:
        return None

    return artist, title, lyrics


def split_for_telegram(text, limit=TELEGRAM_MESSAGE_LIMIT):
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        line_len = len(line)
        if current_len + line_len > limit and current:
            chunks.append(''.join(current).rstrip('\n'))
            current = [line]
            current_len = line_len
        elif line_len > limit:
            for i in range(0, line_len, limit):
                part = line[i : i + limit]
                if current:
                    chunks.append(''.join(current).rstrip('\n'))
                    current = []
                    current_len = 0
                chunks.append(part)
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append(''.join(current).rstrip('\n'))

    return [chunk for chunk in chunks if chunk]


def format_parenthetical_italics(line):
    escaped = html.escape(line)
    return re.sub(r'(\([^)\n]+\))', r'<i>\1</i>', escaped)


def format_lyrics_for_telegram(lyrics):
    formatted_lines = []
    for line in lyrics.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if SECTION_LINE_PATTERN.match(stripped):
            if formatted_lines and formatted_lines[-1] != '':
                formatted_lines.append('')
            formatted_lines.append(f'<b>{html.escape(stripped)}</b>')
            formatted_lines.append('')
            continue

        formatted_lines.append(format_parenthetical_italics(stripped))

    formatted = '\n'.join(formatted_lines)
    formatted = re.sub(r'\n{3,}', '\n\n', formatted).strip()
    return formatted


def check_and_update_cooldown(
    user_id,
    last_request_at,
    cooldown_seconds=SEARCH_COOLDOWN_SECONDS,
    now=None,
):
    current_time = now if now is not None else time.monotonic()
    previous_time = last_request_at.get(user_id)
    if previous_time is None:
        last_request_at[user_id] = current_time
        return 0

    elapsed = current_time - previous_time
    if elapsed >= cooldown_seconds:
        last_request_at[user_id] = current_time
        return 0

    return max(1, int(cooldown_seconds - elapsed))


async def start(update, context):
    await update.message.reply_text(
        'Привет! Я бот для поиска текстов песен. Для начала обязательно прочти /help')


async def help_command(update, context):
    await update.message.reply_text(
        'Команды:\n'
        '/start — приветствие\n'
        '/help — показать справку\n'
        '/search [original|ru|en|any] <Исполнитель> <Название> — найти текст песни\n\n'
        'Пояснение:\n'
        'регистр слов неважен\n'
        'можно указать только название трека\n'
        'можно даже искать по словам из текста!\n'
        'можно указать исполнителя и название в любом порядке\n'
        'чем больше информации, тем точнее поиск\n\n'
        'Примеры:\n'
        '/search Lose Yourself\n'
        '/search eminem Lose Yourself\n'
        '/search Lose Yourself eminem\n'
        '/search original Lose Yourself Eminem\n'
        '/search original Eminem Lose Yourself\n\n'
        'Если язык не указан, используется режим original.'
    )



async def status(update, context):
    bot_token_ok = bool(os.getenv('BOT_TOKEN'))
    genius_token_ok = bool(os.getenv('GENIUS_TOKEN') or os.getenv('GENIUS_ACCESS_TOKEN'))
    history_exists = HISTORY_PATH.exists()
    ensure_history_file()

    await update.message.reply_text(
        'Статус:\n'
        f'BOT_TOKEN: {'OK' if bot_token_ok else 'MISSING'}\n'
        f'GENIUS_TOKEN: {'OK' if genius_token_ok else 'MISSING'}\n'
        f'history.csv: {'OK' if history_exists or HISTORY_PATH.exists() else 'MISSING'}'
    )


def parse_search_args(args):
    if not args:
        return 'original', ''

    first = args[0].lower()
    if first in LANGUAGE_MODES:
        return first, ' '.join(args[1:]).strip()

    return 'original', ' '.join(args).strip()


def is_valid_search_query(query):
    return bool(re.search(r"[a-zA-Zа-яА-ЯёЁ0-9]", query))


async def search(update, context):
    if not context.args:
        await update.message.reply_text(
            'Укажи исполнителя и название песни.\n'
            'Пример: /search original Eminem Lose Yourself'
        )
        return

    preferred_language, query = parse_search_args(context.args)
    if not query:
        await update.message.reply_text(
            'Укажи запрос после режима языка.\n'
            'Пример: /search en eminem lose yourself'
        )
        return
    if not is_valid_search_query(query):
        await update.message.reply_text(
            'че, самый умный что-ли? пиши только словами.'
        )
        return

    user = update.effective_user
    user_id = user.id if user else 0
    username = f'@{user.username}' if user and user.username else '-'
    cooldown_map = context.application.bot_data.setdefault('last_search_at', {})
    wait_seconds = check_and_update_cooldown(user_id, cooldown_map)
    if wait_seconds > 0:
        await update.message.reply_text(
            f'Слишком часто. Подождите {wait_seconds} сек. и повторите запрос.'
        )
        return

    genius_token = os.getenv('GENIUS_TOKEN') or os.getenv('GENIUS_ACCESS_TOKEN')
    if not genius_token:
        await update.message.reply_text('Ошибка конфигурации: отсутствует GENIUS_TOKEN/GENIUS_ACCESS_TOKEN')
        return

    try:
        result = await asyncio.to_thread(fetch_lyrics_from_genius, query, genius_token, preferred_language)
    except requests.RequestException:
        log_query(user_id, username, query, 'Не найдено')
        await update.message.reply_text('Песня не найдена')
        return
    except Exception as exc:
        logger.exception('Search handler failed for query=%r: %s', query, exc)
        log_query(user_id, username, query, 'Не найдено')
        await update.message.reply_text('Произошла ошибка при обработке запроса')
        return

    if not result:
        log_query(user_id, username, query, 'Не найдено')
        await update.message.reply_text('Песня не найдена')
        return

    artist, title, lyrics = result
    formatted_lyrics = format_lyrics_for_telegram(lyrics)
    response_text = (
        f'Исполнитель — {html.escape(artist)}\n'
        f'Название — {html.escape(title)}\n\n'
        f'{formatted_lyrics}'
    )
    for part in split_for_telegram(response_text):
        await update.message.reply_text(part, parse_mode=ParseMode.HTML)

    log_query(user_id, username, query, 'Найдено')


async def on_error(update, context):
    if hasattr(update, 'effective_message') and update.effective_message:
        await update.effective_message.reply_text('Временная ошибка, попробуйте еще раз.')


def main():
    load_dotenv()
    bot_token = os.getenv('BOT_TOKEN')
    if not bot_token:
        raise RuntimeError('Не задан BOT_TOKEN в переменных окружения')

    ensure_history_file()
    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('status', status))
    app.add_handler(CommandHandler('search', search))
    app.add_handler(CommandHandler('admin', admin))
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == '__main__':
    main()
