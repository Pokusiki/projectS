# Telegram Lyrics Bot (Genius API)

```bash
pip install -r requirements.txt
```

```bash
python bot.py
```

## Команды в Telegram

- `/start` - подсказка по использованию.
- `/help` - список команд.
- `/admin` - админ панель.
- `/status` - проверка конфигурации (токены и `history.csv`).
- `/search [original|ru|en|any] Eminem Lose Yourself` - поиск с приоритетом языка.

Примеры:
- `/search original kizaru ride or die` — предпочитать оригинал (режим по умолчанию).
- `/search ru kizaru ride or die` — предпочитать русские переводы.
- `/search en kizaru ride or die` — предпочитать английские переводы.
- `/search any kizaru ride or die` — без жесткого приоритета языка.

## Формат логов

Файл `history.csv` создается автоматически. Формат колонок:

`Дата и время;ID пользователя;Username;Поисковый запрос;Результат`

