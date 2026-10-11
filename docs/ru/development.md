# Разработка

[English](../en/development.md) · **Русский**

## Структура репозитория

```
src/msdoc/        библиотека (только стандартная библиотека); blocks.py и markdown.py — структура и Markdown
tests/            юнит-тесты (pytest)
oracle/           стенд проверки: корпус, эталон LibreOffice, оценка, храповик
examples/         to_markdown.py, llm_messages.py, extract_folder.py
docs/             документация
prompts/          задания агентам, которыми писалась реализация
scripts/          запуск агентов, статус, слияние веток
reviews/          отчёты независимых ревью (если есть)
data/             (вне git) корпус, эталоны, baseline.json, клоны LibreOffice/POI
```

## Тесты

```bash
python3 -m pip install -r requirements-dev.txt      # pytest, olefile, Pillow, rapidfuzz — только для разработки
python3 -m pytest -q
```

Тесты используют синтетические байты для граничных случаев и реальные файлы из корпуса (если
`data/` есть; иначе такие тесты пропускаются).

## Корпус и оракул

Требования: LibreOffice (для эталона), Pillow, rapidfuzz.

```bash
scripts/bootstrap.sh                       # зависимости, корпус (~500 файлов), эталоны (10–30 мин), baseline
python3 oracle/compare.py --quiet          # прогон по dev
python3 oracle/compare.py --check          # то же + храповик (код возврата 1 при регрессии)
python3 oracle/compare.py --diff <id>      # разница с LibreOffice по одному файлу
python3 oracle/compare.py --split holdout --quiet --label holdout   # отложенная выборка
```

Отчёты — в `reports/`. Для обычных файлов считаются `text mean` и `image F1`, структура (`structure score`), а
также отдельная строка `text mean excluding fuzzer-origin files` (файлы из фаззер-корпуса
LibreOffice оцениваются отдельно: эталон на них ненадёжен).

### Оценка структуры

`make_golden.py` кладёт в эталон `struct` (заголовки, списки, таблицы, места картинок — из ODT), `run_one.py` выгружает такую же
структуру из `Document.blocks`, `oracle/structscore.py` сравнивает их по токенам (`H1`, `L0o`, `T3x4s1`, `I`). Для ratchet'а есть
`data/struct_baseline.json`: `compare.py --check` падает, если `struct` какого-либо файла упал. Эталоны, созданные до 0.2.0, не имеют
`struct`: пересоздайте их `python3 oracle/make_golden.py --missing-struct` (или `--force`).

**Важно.** Запускайте `--update-baseline` в том же окружении, где идёт проверка (с тем же `rapidfuzz`): оценки текста слегка
зависят от него, и baseline, поднятый в другом окружении, даёт ложные «регрессии».

### Добавить свои документы

Это лучший способ проверить качество на ваших реальных файлах:

```bash
python3 oracle/fetch_corpus.py --no-lo --no-poi --extra /путь/к/вашим/doc
python3 oracle/make_golden.py              # эталон LibreOffice для новых файлов
python3 oracle/compare.py --quiet
python3 oracle/compare.py --diff <id>      # посмотреть расхождения
python3 oracle/compare.py --update-baseline --quiet   # зафиксировать, когда результат вас устраивает
```

Новые файлы автоматически делятся на dev и holdout в пропорции 85/15. Если документы конфиденциальны,
следите, чтобы `data/` не покидала машину (она в `.gitignore`).

Документы с известным ожидаемым содержимым можно сгенерировать автоматически:
`python3 oracle/synth_corpus.py --out data/synth --count 40` (нужны LibreOffice и Pillow);
проверка по ожидаемым строкам — `python3 oracle/synth_corpus.py --check data/synth`.

## Правила изменений

1. Только стандартная библиотека в `src/` (Python ≥ 3.9).
2. Наружу — только `MsDocError`; всё, что читается из файла, недоверенно: проверяйте границы,
   ограничивайте циклы и рекурсию.
3. Никаких подгонок под конкретные файлы (по id, хэшу, имени, размеру). Исправляйте общее правило
   и указывайте раздел спецификации или файл LibreOffice, на который опираетесь.
4. Заголовок MPL-2.0 в каждом файле; если логика перенесена из LibreOffice — строка
   `# Portions of this file are derived from LibreOffice <путь> (MPL-2.0).`
5. Код из GPL/LGPL-проектов (antiword, wvWare, catdoc, AbiWord) копировать нельзя.
6. После изменения: `python3 -m pytest -q` и `python3 oracle/compare.py --check` (ни один файл не должен
   стать хуже). Среднюю ссылочную оценку смотрите отдельно на dev и holdout.

### Если нужно ослабить храповик

Когда вы удаляете правило, которое давало балл только на фаззер-файлах, `compare.py --check`
упадёт. В таком случае используйте переключатель (только для файлов фаззерного происхождения):

```bash
MSDOC_WAIVE_FUZZ=1 python3 oracle/compare.py --check
MSDOC_WAIVE_FUZZ=1 python3 oracle/compare.py --update-baseline    # пересчитать baseline для них вниз
```

## Выпуск версии

1. Обновите версию в `pyproject.toml` **и** `src/msdoc/__init__.py` (`__version__`).
2. `python3 -m pytest -q` и обе оценки (dev, holdout).
3. `git tag vX.Y.Z && python3 -m build` → `dist/`.
4. Проверьте колесо в чистом окружении:
   `python3 -m venv /tmp/t && /tmp/t/bin/pip install dist/msdoc-X.Y.Z-py3-none-any.whl && /tmp/t/bin/python -c "import msdoc"`.

## Как писалась реализация (для истории)

Слой структуры (`blocks.py`, `markdown.py`) добавлен позже тем же способом: каркас и оценка структуры подготовлены вручную, а
заголовки, списки, таблицы, картинки, сноски, ссылки и метки списков реализованы агентами по заданиям `prompts/40-blocks.md` и
`prompts/41-blocks-rest.md` (ветки `agent/blocks-headings`, `agent/blocks-rest`).

Каркас, оракул и задания (`prompts/`) подготовлены вручную; модули `cfb`, `fib`, `pieces/props`, `stories`,
`images` написаны отдельными агентами Claude Code, каждый в своём git-worktree и ветке `agent/<имя>`,
со Stop-хуком, который запускает храповик. Ветки сливались скриптом `scripts/integrate.sh`
только после прохождения тестов и храповика. Затем независимые ревьюеры проверили лицензионную
чистоту (MPL-заголовки и атрибуция LibreOffice), отсутствие подгонки под отдельные файлы и безопасность
разбора; найденные замечания исправлены.
