# Задача: структура документа (blocks) → Markdown

Модуль: `src/msdoc/blocks.py` (сейчас только запасной вариант `plain_blocks`), рендерер
`src/msdoc/markdown.py` и модель в `src/msdoc/model.py` (Paragraph/Table/Row/Cell/Link,
IMAGE_MARK, NOTE_MARK) уже есть. Плоские `Document.stories` НЕ менять: оракул по тексту
не должен ухудшиться ни на одном файле (`compare.py --check`).

Реализуйте `build_blocks(doc)` по реальной структуре файла (передавайте нужные данные из
`api.extract`/`stories.build_stories`, минимально меняя интерфейсы), по шагам, каждый
шаг — отдельный коммит с тестами; при любой ошибке в шаге — откат к plain-абзацам, текст не терять:
1. Заголовки: стиль "Heading N"/"Заголовок N" (STSH, sprmPIstd) и sprmPOutLvl → Paragraph.heading.
2. Списки: sprmPIlfo/sprmPIlvl, LSTF/LFO (PlfLst/PlfLfo), bullet vs. number, уровень.
3. Таблицы: ячейки/строки по 0x07 и sprmPFInTable/PFTtp/PFInnerTtp, TDefTable (sprmTDefTable)
   → границы ячеек, colspan (по ширинам), vMerge → rowspan, вложенность, sprmTTableHeader.
4. Картинки: IMAGE_MARK в тексте абзаца на месте якоря 0x01/0x08; Paragraph.images[k] =
   индекс в doc.images (сопоставление по story+cp).
5. Сноски: NOTE_MARK на месте ссылки, doc.notes; гиперссылки (поля HYPERLINK) → Link.
6. Надписи/колонтитулы/комментарии → doc.extra.
Проверка: `python3 -m pytest -q`, `python3 oracle/compare.py --check`, и просмотр
`python3 -c "import msdoc,sys; print(msdoc.extract(sys.argv[1]).markdown)" файл.doc`.
Правила — CLAUDE.md (stdlib, только MsDocError, без подгонки, MPL-заголовки).

## Как измеряется прогресс
`compare.py` считает отдельный балл структуры `struct` (oracle/structscore.py): токены H<уровень>, L<уровень><o|u>,
T<строки>x<столбцы>[s<объединённых>], I — картинка, плюс число ссылок и ссылок на сноски. Сравнивается с ODT от LibreOffice.
`python3 oracle/compare.py --diff <id>` показывает разницу токенов. Храповик: `data/struct_baseline.json`
(`--check` падает, если struct файла упал). Обычный балл (`score`) структурой не меняется и не должен ухудшаться.
