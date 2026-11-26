"""Translate command-line messages without translating archive data."""

import argparse
from contextlib import contextmanager

from .config import DEFAULT_UI


LANGUAGES = ("en", "ru")
_language = DEFAULT_UI

_RUSSIAN = {
    # Argument parser
    "usage: ": "использование: ",
    "options": "параметры",
    "optional arguments": "необязательные параметры",
    "positional arguments": "позиционные аргументы",
    "show this help message and exit": "показать эту справку и выйти",
    "%(prog)s: error: %(message)s\n": "%(prog)s: ошибка: %(message)s\n",
    "argument %(argument_name)s: %(message)s": "аргумент %(argument_name)s: %(message)s",
    "unrecognized arguments: %s": "неизвестные аргументы: %s",
    "ambiguous option: %(option)s could match %(matches)s": "неоднозначный параметр %(option)s: возможны %(matches)s",
    "expected one argument": "требуется один аргумент",
    "expected at most one argument": "допустим не более чем один аргумент",
    "expected at least one argument": "требуется хотя бы один аргумент",
    "expected %s argument": "требуется аргументов: %s",
    "expected %s arguments": "требуется аргументов: %s",
    "invalid %(type)s value: %(value)r": "недопустимое значение типа %(type)s: %(value)r",
    "invalid choice: %(value)r (choose from %(choices)s)": "недопустимое значение %(value)r (допустимы %(choices)s)",
    "ignored explicit argument %r": "лишнее значение %r",
    "not allowed with argument %s": "несовместим с аргументом %s",
    "the following arguments are required: %s": "требуются следующие аргументы: %s",
    "one of the arguments %s is required": "требуется один из аргументов %s",
    # Command help
    "Parallel agy audio diarizer": "Параллельная расшифровка и диаризация аудио через agy",
    "Interface language (default: {default})": "Язык интерфейса (по умолчанию {default})",
    "Archive root (default: current directory)": "Корень архива (по умолчанию текущая папка)",
    "Folder to scan within the archive (default: the whole archive)": "Папка внутри архива (по умолчанию весь архив)",
    "Create empty local catalogs and exit": "Создать пустые справочники и выйти",
    "Number of parallel workers": "Количество параллельных обработчиков",
    "Initial model": "Основная модель",
    "Comma-separated ordered model list (overrides --model + --fallback-models)":
        "Упорядоченный список моделей через запятую (переопределяет --model и --fallback-models)",
    "Comma-separated fallback models for provider and task errors":
        "Список резервных моделей через запятую для ошибок сервиса и заданий",
    "Compatibility option: quota fallback is now automatic": "Параметр совместимости (переключение при квоте автоматическое)",
    "Max pending files to process (0 = all)": "Максимум файлов для обработки (0 — все)",
    "Max short files per agy call": "Максимум коротких файлов за запрос agy",
    "Target max short audio sec per agy call": "Целевая длительность группы коротких записей за запрос agy в секундах",
    "Slice recordings longer than this": "Разрезать записи длиннее этого числа секунд",
    "Length of each segment in seconds": "Длительность части записи в секундах",
    "Overlap between segments in seconds": "Перекрытие частей в секундах",
    "Request timeout in seconds": "Таймаут запроса в секундах",
    "Disable minimal custom agent": "Отключить профиль агента для чтения аудио",
    "Disable automatic untrunc fallback for damaged containers": "Отключить восстановление повреждённых контейнеров через untrunc",
    "Disable libfaad2 raw AAC frame recovery": "Отключить восстановление кадров AAC через libfaad2",
    "Re-try files previously marked status=skipped in manifest.jsonl": "Повторить обработку файлов со статусом skipped в манифесте",
    "Retry files marked needs_recovery and legacy damaged files marked skipped":
        "Повторить обработку файлов со статусом needs_recovery и старых повреждённых файлов со статусом skipped",
    "Re-try files previously marked status=refused in manifest.jsonl": "Повторить обработку файлов со статусом refused в манифесте",
    "Exit on quota exhaustion instead of sleeping": "Завершить работу при исчерпании квоты",
    "Max queue assignments per unchanged file in one run (default: %(default)s)":
        "Максимум попыток обработки неизменившегося файла за запуск (по умолчанию %(default)s)",
    "List pending queue without running agy": "Показать очередь без запуска agy",
    "Run internal parser/validator/recovery self-test": "Проверить разбор, валидацию и восстановление аудио",
    "Find up to {count} samples for the folder owner, or specify a person ID":
        "Подготовить до {count} образцов для владельца папки или явно указанного человека",
    "Approve one candidate and clear its review set": "Подтвердить кандидата и очистить его набор",
    "Discard a pending set by candidate FLAC or folder": "Отклонить набор, указав FLAC или папку кандидатов",
    "Transcribe without preparing or using any voice samples": "Расшифровать без подготовки и использования образцов голоса",
    "Transcribe immediately using any already approved voice samples": "Сразу начать расшифровку с доступными подтверждёнными образцами",
    # Archive and voice review
    "--max-file-attempts must be positive": "--max-file-attempts должен быть больше нуля",
    "Voice sample actions cannot be combined with --dry-run or --init":
        "Действия с образцами несовместимы с --dry-run и --init",
    "Folder must be inside {root}": "Папка должна находиться внутри {root}",
    "Service folder is excluded from scanning: {folder}": "Служебная папка исключена из поиска: {folder}",
    "Folder not found: {folder}": "Папка не найдена: {folder}",
    "Missing executable(s) on PATH: {tools}": "Не найдены исполняемые файлы в PATH: {tools}",
    "Expected a YAML mapping named {key!r} in {path}": "В {path} ожидается словарь YAML с ключом {key!r}",
    "Catalogs: {paths}": "Справочники: {paths}",
    "Choose a folder with a known phone owner or provide the owner ID":
        "Укажите папку с известным владельцем телефона или передайте ID владельца",
    "Person is missing from the catalog: {person}": "Нет карточки в справочнике: {person}",
    "No suitable voice sample found for {person}": "Не найден подходящий образец для {person}",
    "Voice sample approved: {sample}": "Образец подтверждён: {sample}",
    "Voice sample review rejected: {path}": "Набор кандидатов отклонён: {path}",
    "Listen to the voice clips in {folder}/ ({count}).": "Прослушайте клипы в {folder}/ ({count}).",
    "Move the best FLAC to the adjacent confirmed/ folder, leave the JSON files in place, and rerun transcription.":
        "Перенесите лучший FLAC в соседнюю папку confirmed/, оставьте JSON на месте и снова запустите расшифровку.",
    "Details in {path}": "Подробности в {path}",
    "Selected clips are in {folder}. Start transcription to validate them.":
        "Выбранные клипы уже в {folder}. Запустите расшифровку, чтобы подтвердить их.",
    "The model could read {count} of {total} calls. Comparing voices needs at least {required}.":
        "Модель прочитала {count} из {total} звонков. Минимум для сравнения голоса — {required}.",
    "No suitable voice clip found in the reviewed calls.": "В проверенных звонках не найден подходящий образец голоса.",
    "Prepared {count} calls. Voice comparison needs {required} readable calls with different contacts.":
        "Подготовлено звонков: {count}. Для сравнения нужны читаемые звонки от разных контактов (минимум {required}).",
    "The model returned an empty voice sample response": "Модель вернула пустой ответ на запрос образцов",
    "Expected a voice sample report with a summary, calls, and candidates":
        "Ожидался отчёт об образцах с полями summary, calls и candidates",
    "Expected a brief explanation of the voice sample outcome": "Ожидалось краткое пояснение результата подбора",
    "Expected one diagnostic report for every source call": "Ожидался один отчёт по каждому исходному звонку",
    "Invalid source call diagnostic": "Некорректный отчёт об исходном звонке",
    "A candidate cited a call without readable audio": "Кандидат ссылается на звонок без доступного аудио",
    "Invalid voice sample response: {error}": "Некорректный ответ при подборе образцов: {error}",
    "Expected one report per voice clip": "Ожидался один отчёт по каждому клипу",
    "Invalid voice clip report": "Некорректный отчёт по клипу",
    "Invalid speaker check result": "Некорректный результат проверки числа говорящих",
    "Invalid voice clip verification response: {error}": "Некорректный ответ при проверке клипов: {error}",
    "No clip passed the single-speaker check.": "Ни один клип не прошёл проверку на одного говорящего.",
    "Invalid adjusted voice clip interval": "Некорректные границы исправленного клипа",
    "Invalid voice sample person ID": "Недопустимый ID владельца образца",
    "Invalid voice sample channel": "Недопустимый тип записи для образца",
    "Voice sample channel cannot use a symbolic link: {path}": "Папка типа записи не может быть симлинком: {path}",
    "Voice sample review cannot use a symbolic link: {path}": "Папка проверки образцов не может быть симлинком: {path}",
    "Expected a voice sample object or null": "Ожидался объект с образцом или null",
    "Invalid voice sample bounds or cross-call evidence": "Некорректные границы клипа или подтверждение по другим звонкам",
    "Expected up to {count} voice sample candidates": "Ожидалось до {count} образцов-кандидатов",
    "Voice sample candidates overlap": "Голосовые кандидаты пересекаются во времени",
    "Voice sample source changed during preparation: {path}": "Исходник образца изменился при подготовке: {path}",
    "Cannot extract voice sample: {error}": "Не удалось вырезать образец: {error}",
    "Unexpected voice sample duration: {duration}": "Неожиданная длительность образца: {duration}",
    "Expected local sample metadata at {path}": "Ожидался локальный файл метаданных образца: {path}",
    "Invalid voice sample provenance at {path}": "Некорректные метаданные образца: {path}",
    "Voice sample locks cannot use a symbolic link": "Файл блокировки образцов не может быть симлинком",
    "Expected a candidate FLAC at {path}": "Ожидался FLAC кандидата: {path}",
    "The selected sample or its metadata changed: {path}": "Выбранный образец или его метаданные изменились: {path}",
    "Conflicting sample provenance at {path}": "Обнаружены несовместимые метаданные образца: {path}",
    "Voice sample library cannot use a symbolic link": "Папка голосовых образцов не может быть симлинком",
    "Expected a path to a voice sample candidate": "Укажите путь к кандидату на образец",
    "Expected a FLAC for approval or a candidate folder or FLAC for rejection":
        "Для подтверждения укажите FLAC. Для отклонения укажите папку кандидатов или FLAC.",
    "Choose a FLAC to identify one pending voice review": "Укажите FLAC, чтобы выбрать один набор кандидатов",
    "Move the selected clips back into candidate before rejecting this set":
        "Перед отклонением набора переместите выбранные клипы обратно в candidate",
    "Sample already exists at {path}": "Образец уже существует: {path}",
    "Expected the selected FLAC at {path}": "Ожидался выбранный FLAC: {path}",
    # Progress, completion, and cancellation
    "Diagnostic log: {path}": "Журнал диагностики: {path}",
    "Queued files in {folder}: {count} · faad {faad} · untrunc {untrunc}":
        "Очередь {folder}: {count} файлов · faad {faad} · untrunc {untrunc}",
    "disabled": "отключено",
    "missing": "не найдено",
    "[WARN] libfaad2 is unavailable; raw AAC recovery is disabled":
        "[WARN] libfaad2 не найдена — восстановление сырых AAC кадров отключено",
    "[WARN] untrunc is unavailable; automatic container repair is disabled":
        "[WARN] untrunc не найден в PATH — запасная автопочинка контейнеров отключена",
    "Showing the first 20 of {count}:": "Показаны первые 20 из {count}:",
    "Start · workers {workers} · models {models}": "Старт · работников {workers} · модели {models}",
    "s": "с", "min": "мин", "h": "ч",
    "Done": "Готово", "queued": "в очереди", "audio total": "всего аудио",
    "skipped": "пропуски", "needs recovery": "нужно восстановить", "refused": "отказы", "errors": "сбои",
    "still pending after errors {count}": "не обработано после сбоев {count}",
    "input": "вход", "cache": "кэш", "output": "выход", "tokens:": "токены:",
    "date unknown": "дата неизвестна",
    "Speakers: {status}": "Участники: {status}",
    "unresolved": "не установлены", "partially identified": "частично",
    "identified": "установлены", "no speech": "без речи",
    "Stopped": "Остановлено", "Finished": "Завершено", "elapsed": "время работы",
    "[FORCE STOP] Cancelling active processes and restoring the terminal...":
        "[FORCE STOP] Прерываю активные процессы и восстанавливаю терминал...",
    "[STOP] Finishing active jobs (press Ctrl+C again to cancel immediately)...":
        "[STOP] Завершаю текущие задачи (повторный Ctrl+C — прервать сразу)...",
    # Provider and retry messages
    "overload / 503": "перегрузка / 503", "server failure": "сбой сервера",
    "connection failure": "сбой соединения", "timeout": "таймаут", "quota": "квота",
    "model or access error": "ошибка доступа или модели", "content refusal": "отказ по содержимому",
    "invalid response format": "неверный формат ответа",
    "unsupported input or request size": "неподдерживаемые входные данные или размер запроса",
    "damaged media": "повреждённые медиаданные", "unknown error": "неизвестная ошибка",
    "[WARN] Service failure; pausing new requests for {seconds} s":
        "[WARN] Сбой сервиса: пауза перед новыми запросами {seconds} с",
    "[ERR] {path}: {reason} -> retry failed: {error}": "[ERR] {path}: {reason} → повтор не удался: {error}",
    "Resuming {path}: saved parts 1–{saved} of {total}": "Продолжение {path}: сохранены части 1–{saved} из {total}",
    "[WARN] {scope}: content refusal on {model}; retrying one file at a time":
        "[WARN] {scope}: отказ по содержимому на {model}; разбиваю по одному файлу",
    "[WARN] {scope}: damaged media; retrying one file at a time":
        "[WARN] {scope}: повреждённые медиаданные; проверяю файлы по одному",
    "[WARN] {prefix}{reason} on {model}; paused for all workers for {seconds} s (until {resume:%H:%M:%S %z})":
        "[WARN] {prefix}{reason} на {model}; пауза {seconds} с для всех работников (до {resume:%H:%M:%S %z})",
    "[WARN] {prefix}{model}: {reason}; disabled for this run. {error}":
        "[WARN] {prefix}{model}: {reason}; исключена до конца запуска. {error}",
    "No models available: {reasons}": "Нет доступных моделей: {reasons}",
    "excluded for this job": "исключена для текущего задания",
    "[WARN] All available models are paused; waiting {seconds} s (until {resume:%H:%M:%S %z}; Ctrl+C to stop)":
        "[WARN] Все доступные модели на паузе; жду {seconds} с (до {resume:%H:%M:%S %z}; Ctrl+C — остановить)",
    "The model chain could not process the job: {details}": "Цепочка моделей не обработала задание: {details}",
    "[WARN] {prefix}{reason} on {model}; trying another model for this job":
        "[WARN] {prefix}{reason} на {model}; пробую другую модель для этого задания",
    "Quota limit ({error}). Waiting {seconds}s (until {resume:%Y-%m-%d %H:%M:%S %z})...":
        "Лимит квоты ({error}). Жду {seconds}с (до {resume:%Y-%m-%d %H:%M:%S %z})...",
}


# Render an interface message using English as the untranslated source.
def tr(message, /, **values):
    template = _RUSSIAN.get(message, message) if _language == "ru" else message
    return template.format(**values) if values else template


# Share the selected interface language across all workers in one CLI run.
@contextmanager
def use_ui(language):
    global _language
    previous, _language = _language, language
    try:
        yield
    finally:
        _language = previous


# Provide an argparse plural template for the selected interface language.
def _argparse_plural(singular, plural, count):
    return tr(singular if count == 1 else plural)


# Translate argparse's own help headings and errors for one CLI invocation.
@contextmanager
def argparse_ui():
    previous = argparse._, argparse.ngettext
    argparse._, argparse.ngettext = tr, _argparse_plural
    try:
        yield
    finally:
        argparse._, argparse.ngettext = previous
