# Ручная приёмка remote-режима (карта тестирования)

Пошаговая инструкция: **что запустить, что нажать, что увидеть и что записать**.
Это карта для ручной приёмки A–F. Полная матрица offline-доказательств (unit /
headless AppTest / live HTTP со stub) **не дублируется** здесь — она уже есть в
чек-листе, и на него ведут ссылки.

Сопутствующие документы (источники нумерации и ожидаемых результатов):

- `docs/REMOTE_PARITY_ACCEPTANCE.md` — полная карта A–F + G, легенда статусов,
  «Manual-run order», «Quick status table». Нумерация и ожидаемые результаты
  взяты отсюда.
- `docs/REMOTE_PARITY_STATUS.md` — живой чекпоинт (base checkpoint **223**,
  **289 OK**), статус 502-смоук, пересчёт по suite.
- `docs/REMOTE_PARITY_PLAN.md` — порядок стадий A–F и инварианты.
- `scripts/` — Remote Lab: `Start-RemoteLab.ps1`, `Stop-RemoteLab.ps1`,
  `Status-RemoteLab.ps1`, `remote-lab.common.ps1`, `lab/labctl.sh`,
  `remote-lab-diag.py`.
- Offline-доказательства по пунктам — в модулях `tests/` (см. колонку
  «Доказательство / ссылка» в итоговой таблице).

**Ограничения сессии (важно):** не менять код наугад, не деплоить/рестартить
сервисы, не вызывать живую модель, не коммитить/пушить. Это руководство — только
прочтение, запуск стенда и ручные клики.

---

## 1. Подготовка и запуск

Стенд — три сервиса на ВМ (document / workspace / processing) за четырьмя SSH-
туннелями плюс локальный Streamlit UI. Backend — **stub** («Test backend»).
Для A–F живая модель **не нужна**.

### 1.1. Вариант (a) — штатный лаунчер (если ВМ-сервисы разрешены)

`scripts/Start-RemoteLab.ps1` делает всё сам: переиспользует/открывает 4 туннеля,
preflight, `bash /home/vagrant/lab/labctl.sh start-all`, старт локального
Streamlit (только если порт ещё не занят), ожидание health, сообщение
«Test backend (stub)».

```powershell
cd C:\memoq-harness\memoq-harness-qwen
pwsh -File scripts\Start-RemoteLab.ps1
```

Что он ставит (из `scripts/remote-lab.common.ps1`):

- Туннели: `18101→8101` (document), `18102→8102` (workspace),
  `18103→8103` (processing), `15432→5432` (database — для UI-проверок не нужен).
- UI: `127.0.0.1:8502` (headless).
- Env UI (remote-режим): `HARNESS_BACKEND=remote`,
  `HARNESS_DOCUMENT_URL=http://127.0.0.1:18101`,
  `HARNESS_WORKSPACE_URL=http://127.0.0.1:18102`,
  `HARNESS_PROCESSING_URL=http://127.0.0.1:18103`,
  `HARNESS_HTTP_TIMEOUT_SECONDS=30`.
- Локальные логи UI: `%LOCALAPPDATA%\memoq-harness-remote-lab\ui.log` и
  `ui.err.log`.

Health-ожидание лаунчера = UI `/_stcore/health`=200 **и** document `/healthz`=200
через туннель. Это **не** доказательство Open — Open проверяется отдельно (п. 2).

### 1.2. Вариант (b) — SSH-туннель + ручной Streamlit (если сервисы уже работают)

Использовать **вместо** (a), не оба сразу. SSH-доступ из
`scripts/remote-lab.common.ps1`: ключ `$env:USERPROFILE\.ssh\memoq_lab`,
хост `vagrant@192.168.56.10`.

Туннели (только три нужны для UI, database-туннель опционально):

```
ssh -i "$env:USERPROFILE\.ssh\memoq_lab" -N -L 18101:8101 -L 18102:8102 -L 18103:8103 vagrant@192.168.56.10
```

Затем в отдельной сессии — env + Streamlit:

```powershell
$env:HARNESS_BACKEND="remote"
$env:HARNESS_DOCUMENT_URL="http://127.0.0.1:18101"
$env:HARNESS_WORKSPACE_URL="http://127.0.0.1:18102"
$env:HARNESS_PROCESSING_URL="http://127.0.0.1:18103"
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502
```

### 1.3. Что проверить до начала (оба варианта)

1. **`/readyz`** на всех трёх сервисах через туннели — 200
   (document `:18101`, workspace `:18102`, processing `:18103`).
2. **Remote-режим в UI**: UI отвечает по `http://127.0.0.1:8502`,
   backend показывает **stub** («Test backend»).
3. Для **Analyze** дополнительно: `engine: stub` на самом processing-сервисе
   (это подтверждает, что Analyse my corrections пойдёт в stub, а не в живую
   модель).
4. **Важно:** `Status-RemoteLab` / `scripts/remote-lab-diag.py` с вердиктом
   `FRESH` подтверждают **только** совместимость с `prefill=false`
   (неизвестный workspace → 404). **FRESH ≠ Open**. Open проверяется кликом
   (п. 2).
5. **Не смешивать** Remote-Lab-туннели (18101–18103) с отдельным
   Ollama-туннелем Windows→Mac на `11434`. Это разные стенды.

> Fixtures для прогона (в `tests/fixtures/`):
> `sample.mqxliff`, `sample1.mqxliff`, `21133_242441_en.html_rus.mqxliff`,
> `invalid-remote-test.mqxliff`. Для A–C нужен документ с **редактируемыми
> строками и тегами**. Для **D.1** — **23 сегмента**. Для **D.4** — **29
> сегментов**. Для **F** — **3 строки при batch size 2**. Для **E.2** —
> **валидный memoQ CSV** (по образцу `MQL term base.csv`) и **CSV только с
> заголовком**. Перед каждым прогоном сверяй имя fixture и число строк (см. п. 3).

---

## 2. Ручной прогон A–F

Общая точка входа у всех A–F: открыть документ (Open). Пока Open не проходит
(см. п. 3), сценарии, которые требуют открытого документа, имеют статус **Blocked
не Fail** (зависимость от Open, а не провал функции).

Ниже — по пунктам: fixture + начальное состояние, порядок действий в UI,
ожидаемый результат, метод проверки, Pass/Fail/Blocked, где нужен новый
workspace.

### A. Apply (скопировать proposal в draft) + сохранение neighbor-draft

- **Fixture / состояние:** документ A–C (редактируемые строки + теги),
  открыт, есть proposal для строки; рядом есть другая строка со своим draft.
- **Действия в UI (по порядку):**
  1. Внести правку в draft строки.
  2. `Apply Changes & Calculate Diffs` → `st.rerun`.
  3. (по сценарию) `Discard` на соседней строке — проверить, что draft соседней
     строки не затронут.
- **Ожидаемый результат:** proposal попал в draft, только явный Save persist'ит;
  neighbor-draft сохранён; explicit empty-string правка сохраняется как пустая.
- **Проверка:** UI показывает обновлённый draft; `st.rerun` сработал.
- **Доказательство (offline):** `tests/test_remote_edit.py`
  `RemoteEditTestCase` (6 apply-тестов: creates_unsaved_draft_then_save_persists,
  preserves_held_comment, noop_when_already_shown, button_absent_without_
  proposal, apply_then_discard_restores_saved_and_keeps_proposal,
  keeps_neighbour_draft).
- **Статус:** Blocked (зависит от Open).

### B. Export MQXLIFF через document-service

- **Fixture / состояние:** сохранённый (user_final) workspace; есть выбор/статус/
  теги как в local.
- **Действия:**
  1. `4. Export` в сайдбаре → `Export Valid MQXLIFF`.
  2. Получить файл на клиенте.
  3. Перепарсить/проверить: source-фingerprint и locked-сегменты не изменились.
- **Ожидаемый результат:** export несёт human-текст (user_final) и оставляет
  остальное без изменений; XML valid.
- **Проверка:** повторный парс экспортированного MQXLIFF.
- **Отдельный уровень доказательства:** импорт в **живом memoQ** — это live-
  уровень, XML-parse сам по себе импорт **не доказывает**.
- **Доказательство (offline):** `test_export_carries_saved_human_text_and_
  preserves_rest` (+ отдельная проверка реального memoQ-import).
- **Статус:** Blocked (зависит от Open).

### C. Per-row Process / Rerun

- **Fixture / состояние:** открытый документ; строка со своим draft+комментом.
- **Действия:**
  1. Per-row `Rerun` (один сегмент, `operation="rerun"`).
  2. Per-row `Process` (один сегмент, без `operation`).
  3. After each — Copy + Save, проверить, что generation не стёр сохранённый
     текст/коммент.
- **Ожидаемый результат:** proposal обновлён без стирания user_final/коммента;
  `prefill:false`; stale write не перезаписан.
- **Доказательство (offline):** `test_rerun_remote_row_survives_copy_save`,
  `..._stale_write_is_not_retried`, `test_process_remote_row_sends_one_
  segment_and_keeps_drafts`, `..._stale_write_is_not_retried`.
- **Статус:** Blocked (зависит от Open).

### D. Batch / processing settings

#### D.1 — Batch size (23 сегмента)
- **Fixture / состояние:** документ **23 сегмента**; **новый workspace**.
- **Действия:** `Change batch size` → задать размер пачки; Process.
- **Ожидаемый результат:** working batch size работает как в local; вид
  перестроился, draft'ы сохранились.
- **Доказательство (offline):** `tests/test_remote_edit.py`
  `RemoteBatchSizeTestCase` (3 теста).
- **Статус:** Blocked (зависит от Open).

#### D.2 — Set batch size (offline-only, ручной статус N/A)
- **Действия:** не требуется — это чисто offline-проверка.
- **Ожидаемый результат:** 100 → 10 → 100 (пересчёт размера без потерь).
- **Проверка:** только offline. Ручной статус **N/A**.
- **Доказательство (offline):** `RemoteSetBatchSizeTestCase` (7 тестов, 100→10→
  100).
- **Статус:** N/A (manual) / offline OK.

#### D.3 — Chunk / segments-per-request (безопасный trace/stub-stand)
- **Ручной статус: Blocked — нужен безопасный trace или stub-stand**, который
  видит `chunk_size` в самом запросе. Это **НЕ** живой preset и **НЕ** живой
  вызов модели.
- **Ожидаемый результат:** не-default chunk_size доходит до multi-row пачки
  (23 → 10/10/3), temperature/max_tokens доходят до payload.
- **Доказательство (offline):** `test_non_default_chunk_size_reaches_multi_row_
  batch`, `test_real_split_groups_23_segments_into_10_10_3`,
  `test_non_default_temperature_and_max_tokens_reach_the_payload`.
- **Статус:** Blocked (без trace/stub-stand; live preset не использовать).

#### D.4 — Pagination (29 сегментов)
- **Fixture / состояние:** документ **29 сегментов**; `remote_page_size=25`.
- **Действия:** `Rows shown at once` (picker) → 25; навигация по страницам.
- **Ожидаемый результат:** page 1 — 25 строк, page 2 — 4 строки; draft'ы
  выживают между rerun и навигацией по страницам; picker не пишет напрямую.
- **Доказательство (offline):** `RemotePaginationTestCase`
  (`test_rows_shown_picker_drives_page_size_without_direct_write`,
  `test_large_fixture_pagination_visible_count_and_draft_survival`,
  `test_large_fixture_whole_batch_shows_all_rows`) +
  `test_drafts_survive_reruns_and_page_navigation`.
- **Статус:** Blocked (зависит от Open).

### E. Rules / prompts / termbase

#### E.1 — Template override + термбаз-термин в собранном промпте
- **Ожидаемый результат:** user-template и релевантный термбаз-термин попадают в
  собранный промпт (не только форвардятся); нерелевантный фильтр'ится.
- **Доказательство (offline):** `test_template_override_applies_to_the_built_
  prompt`, `test_relevant_termbase_term_reaches_the_built_prompt_and_
  irrelevant_is_filtered`.
- **Статус:** offline OK (не требует Open для доказательства).

#### E.2 — Termbase CSV (валидный + только заголовок)
- **Fixture / состояние:** **валидный memoQ CSV** (по образцу
  `MQL term base.csv`) и **CSV только с заголовком**.
- **Действия:**
  1. Загрузить валидный CSV в «Termbase CSV» uploader (индекс 1, после XLIFF
     uploader индекса 0).
  2. Загрузить CSV только с заголовком.
- **Ожидаемый результат:** валидный CSV → релевантный терм доходит до промпта;
  CSV только с заголовком → понятная ошибка, и **уже загруженная термбаза НЕ
  затирается** (checkpoint 223).
- **Доказательство (offline):** `test_termbase_csv_upload_reaches_the_built_
  prompt`, `test_malformed_termbase_csv_shows_a_clear_error`,
  `test_no_term_csv_does_not_clobber_the_active_termbase`.
- **Статус:** offline OK (проверка не требует Open).

#### E.3 — Не-default generation settings в payload
- **Ожидаемый результат:** temperature/max_tokens доходят до payload (а) и
  применяются в клиентском вызове (b).
- **Доказательство (offline):** `test_non_default_temperature_and_max_tokens_
  reach_the_payload` (a) + `TestPerRequestConfig.test_non_default_temperature_
  and_max_tokens_reach_the_client_call` в `tests/test_processing_service.py` (b).
- **Статус:** offline OK.

### F. Corrections (3 строки, batch size 2)

- **Fixture / состояние:** документ **3 строки** при **batch size 2**; новый
  workspace (2 пачки: 2 + 1).
- **Действия (по порядку):**
  1. `Analyse my corrections` → persist critique, не затирая translation.
  2. Human edit → становится correction, переживает reread, идёт в следующую
     пачку.
  3. Two-batch scenario → correction из пачки 1 питает промпт пачки 2.
  4. Stale revision на apply → не retry/rebase.
- **Ожидаемый результат:** correction выживает, идёт в следующую пачку; stale
  не перезаписан; critique не затирает перевод.
- **Проверка:** UI + последующий промпт; `st.rerun`.
- **Доказательство (offline):** `RemoteCorrectionsTestCase` (7 тестов:
  F.0 human_edit_becomes_correction_survives_reread_and_feeds_next_batch,
  F.1 apply_button_persists_batch_correction_through_ui,
  F.2 two_batch_scenario_correction_feeds_next_batch_prompt,
  F.3 apply_button_stale_revision_is_not_retried_or_rebased,
  F.4 rerun_carry_corrections_block_from_earlier_batch,
  F.5 rerun_uses_current_batch_stage_framing_in_nonfirst_batch,
  F.6 analyse_button_persists_critique_without_overwriting_translation).
- **Статус:** Blocked (зависит от Open).

> **G** (separate) — держать отдельно от A–F; см. отдельную строку ниже.

### Порядок ручного прогона (короткий, из ACCEPTANCE)

A → C → D.1 → D.3 → D.4 → E.2 → F → B → G. Но **пока Open не проходит**, все
сценарии, начинающиеся с Open (A, B, C, D.1, D.4, F), = **Blocked**. D.2 = N/A
(offline), D.3 = Blocked (нет trace/stub-stand). E.1/E.2/E.3 = offline OK.

### Названия элементов UI (актуальные)

- XLIFF uploader (индекс 0); **Termbase CSV** uploader (индекс 1, сразу после
  XLIFF).
- **Rows shown at once** — picker.
- **Segments per request** — виджет (был «Context chunks», переименован).
- **Change batch size** — кнопка.
- **4. Export** — сайдбар (**Export Valid MQXLIFF**).
- **Apply Changes & Calculate Diffs**.
- **Analyse my corrections**.

---

## 3. Текущий результат и порядок возобновления

### 3.1. База

- **Offline base:** checkpoint **223**, **289 OK**, exit 0.
  (Six suites: `test_processing_service` 41, `test_workspace_service` 62,
  `test_remote_app` 42, `test_remote_edit` 79, `test_remote_view` 8,
  `test_remote_client` 57.) См. `docs/REMOTE_PARITY_STATUS.md`.
- **Manual preflight:** **Pass** (все три `/readyz` = 200, processing
  `engine: stub`, UI в remote-режиме, `FRESH` по prefill=false — но FRESH ≠
  Open).

### 3.2. Последний подтверждённый Open

- **После Stop/Start (2026-11-27): Open = Pass** на `sample.mqxliff` в
  **приватном окне** (UI: **7 translatable, Batch 1 of 1, batch size 100**).
  См. §4.1. Это **подтверждает §3.5**: до Stop/Start `bind` падал из-за
  устаревшего процесса workspace_service (400 «Unknown field batch_size»);
  после Stop/Start сервис перезагрузил актуальный контракт → `bind` прошёл.
  **Отдельное наблюдение (не тот же сбой):** в **обычной** сессии browser upload
  давал **403** — причина **не установлена**; 403 ≠ 400.
- **Предыдущее состояние (до Stop/Start): Open = Fail** — UI показывал
  `The request was rejected as malformed` и `Selected, not open (could not be
  opened)`.
- **Контролируемое воспроизведение (эмпирика, 2026-11-26):** прогон
  **фактического клиентского пути** против **рабочего** Remote Lab (туннели
  18101/18102) на чистой fixture `sample.mqxliff` (12 сегм., 7 translatable, 0
  дубликатов/пустых key): `POST /documents` → **200**,
  `GET /originals/{id}/segments?tag_source=target` → **200**,
  `POST /workspaces/{document_id}/bind` → **400**
  `{"error":"bad_request","message":"Unknown request field(s).",
  "details":{"fields":["batch_size"]}}`; `GET /workspaces/{document_id}`
  (snapshot) **не выполнен** (остановка на первом отказе). **Исходный сбой НЕ
  объявляется исправленным** (см. §3.5). Причина 400 — **contract/version
  drift**: **рабочий** workspace_service отвергает поле `batch_size`, хотя
  клиент и источник на диске его принимают.
- **Зависимые сценарии (A, B, C, D.1, D.4, F — всё, что начинается с Open):**
  Open теперь **проходит**, поэтому они **доступны** (больше не «Blocked из-за
  Open»). Частичные наблюдения этого прогона — в §4.2 (**не** полный Pass, A–F
  целиком **не** объявлены пройденными).

### 3.3. Диагноз Open (статический анализ; это НЕ подтверждённая причина)

> Ниже — что подтверждено кодом и что НЕ подтверждено. Гипотеза не выдается за
> диагноз.

**Что это за сообщения (точно, по коду):**
- `The request was rejected as malformed` = **генерическая карта клиента на HTTP
  400** (`harness_remote/client.py:32`, `_DEFAULT_MESSAGES[400]`). Клиент
  **отбрасывает** server `error/message/details` для любого 400, чей `error`
  не в allow-list `{stale_revision, unknown_segment, unavailable,
  engine_unavailable, missing_api_key}`. Поэтому UI **не может показать
  настоящую причину** — `bad_request` не в allow-list и пропадает.
- `Selected, not open (could not be opened)` = ветка `remote_ui.py:274-275`
  (`state="could not be opened"` + caption), срабатывающая, когда
  `maybe_remote_open` бросил `RemoteError`; marker записан, но
  `ss.remote_view` остался `None`.

**Что такое Open (точно, по коду):** `remote_open` (remote_workflow.py, ~L240–
280) = **3 HTTP-вызова + 1**:
1. **Document upload** — `POST /documents` (multipart `{file:(filename, raw,
   "application/xml")}`) на document-service.
2. **Segment read** — `GET /originals/{original_id}/segments?tag_source=target`
   на document-service.
3. **Workspace bind** — `POST /workspaces/{document_id}/bind` (JSON
   `{original_id, source_file, tag_source, segments, present_keys, batch_size}`)
   на workspace-service.
4. **Snapshot** — `GET /workspaces/{document_id}` после.
`ss.remote_view` присваивается **только при полном успехе**; любой
`RemoteError` оставляет live-view нетронутым.

**Различение шагов (по 400-условиям + success-строк в логах):**
- **Upload** 400: пустой файл, сбой парсинга multipart («The multipart body could
  not be read.»), отсутствие поля `file`. (415 не multipart, 411 нет
  Content-Length, 413 too large, 422 не читаемый MQXLIFF — это **другие**
  коды → **не** «rejected as malformed».) Success-строка в
  `document_service.log`: `upload stored/repeated: original <12hex> (document
  <12hex>, N segments)`.
- **Segment read** 400: только если `original_id` не 64-hex — но он приходит из
  ответа upload, значит валиден → **400 почти невозможен**. Success-строки для
  segment read **нет** (логируются только upload/bind/export).
- **Bind** 400: неизвестное поле в теле; сегмент не ровно по 4 строковых поля
  `{key, unit_id, source_text, target_text}`; дубликат/отсутствие key;
  `batch_size` не положительное int; `source_file` > 255 символов;
  `tag_source` не в `{target, source}`. (409 — `TagSourceMismatch` /
  `WouldDiscardWork` — это **другие** коды.) Success-строка в
  `workspace_service.log`: `bind created/merged: document <12hex> revision <N>`.

**Сверка запроса клиента с контрактом (репозитория):** формы запросов клиента
**совпадают с контрактом репозитория** точно (bind body = `BIND_FIELDS`;
segments = `SEGMENT_FIELDS` = `_BIND_REQUIRED_FIELDS` клиента). Значит, против
**репозитория** shape-несовпадение **не ожидается**.
**НО** `services/workspace_service/app.py` и `service.py` **изменены (не
закоммичены)** — значит **рабочая** версия workspace_service на ВМ может иметь
другой контракт, чем клиент. **Contract drift — ведущий подозреваемый, но НЕ
подтверждённый диагноз.**

**Что подтверждено:** отклонение — это HTTP **400** на одном из 3 вызовов;
наиболее вероятный шаг — **bind** (upload валидного MQXLIFF → 201, GET segments
почти не даёт 400). Это **вывод, а не подтверждение**.

**Чего не хватает для диагноза (400 нигде не логируется):**
- 400 **не пишется** ни в один лог: `service_error` в
  `document_service/app.py` и `workspace_service/app.py` логирует `warning`
  только при `status >= 500`; upload-400 возвращаются напрямую через
  `_error(400, ...)` — без log-строки вовсе. При дефолтном `*_LOG_LEVEL=info`
  400-отклонение оставляет **пустоту**.
- Нужно собрать **именно** три вещи:
  - **(a)** ЛОГИ ВМ за провалившуюся попытку:
    `/home/vagrant/lab/logs/document_service.log` и
    `/home/vagrant/lab/logs/workspace_service.log` — success-строки
    (`upload ...`, `bind ...`) локализуют **какой** шаг упал (их **отсутствие**
    после `upload` строки ⇒ упал segment-read или bind).
  - **(b)** Фактический **status + `error/message/details`** тела 400 — клиент
    его отбрасывает. Снимается воспроизведением провалившегося Open против
    **рабочего** сервиса инструментом, который печатает body (по образцу
    `scripts/remote-lab-diag.py`: `POST /workspaces/<id>/bind` с тем же JSON и
    печать `e.read()`); тело 400 несёт `{error, message, details}` (только
    идентификаторы). Альтернатива — рестарт сервиса с `*_LOG_LEVEL=debug`
    (или смена гейта) — **рестарт здесь не авторизован**.
  - **(c)** Подтверждение **версии/контракта** workspace_service, **рабочего на
    ВМ** (репозиторий изменён).

**Ollama Windows↔Mac:** **решено пользователем** (перезапуск Ollama на Mac +
перезагрузка Windows-ВМ). Это **НЕ** результат Open и **НЕ** причина менять
настройки Ollama/Qwen.

**Порядок возобновления после фикса Open:**
1. Первый retry — **один известный тестовый MQXLIFF** (сверить имя + число
   строк), потом A.
2. Различия «Editorial context» vs local UI — **To be discussed**, интерфейс
   **не менять**.

### 3.4. Итоговая таблица результатов

Формат: ID, статус, UI+service version, fixture/row ID, expected, actual,
artifact link. Offline-доказательства — в колонке «artifact link» (ссылки на
чек-лист/тесты, матрица не дублируется).

| ID | Статус | UI + service version | Fixture / row ID | Ожидаемый | Фактический | Artifact / ссылка |
|----|--------|----------------------|------------------|-----------|-------------|-------------------|
| Preflight | Pass | stub (Test backend); UI = репозиторий `review/remote-parity-snapshot` | 3×`/readyz` | 200×3, `engine: stub` | Pass (200×3, stub) | `scripts/remote-lab-diag.py`; STATUS.md |
| Open (MQXLIFF) | **Pass** (этот прогон) | после **Stop/Start** стенда, **приватное окно**; совместимо с §3.5 (гипотеза устаревшей ревизии), **точная ревизия прежнего процесса не установлена** | `sample.mqxliff` (7 translatable, **Batch 1 of 1**, batch size 100) | open (3 вызова + snapshot) | **Pass**: upload/segments/bind/snapshot 200; **прежний bind‑400 не повторился** (совместимо с §3.5, точная ревизия не установлена); **отдельно**: browser upload **403** (обычная сессия, причина не установлена, диагностику не начинаем) | §3.5 + §4.1; **не** полный A–F |
| A | **Partial** | Open Pass (этот прогон) | A–C: editable rows + tags | draft + neighbor-draft | apply proposal на 2 строках; правка сохраняется при смене батча. **Не засчитано:** Discard, Save→Reload. См. §4.2 | `test_remote_edit.py::RemoteEditTestCase` (6) |
| B | **Partial** | Open Pass (этот прогон) | сохранённый workspace | download / XML / import раздельно | **download=Pass**; **XML не проверен**; import на файле из **синтетического `sample.mqxliff`** = Fail «Tool element is not found» (наблюдение сохраняем); приёмка импорта на **нативном экспорте memoQ** = **Pending** (полный цикл позже). См. §4.4 | `test_export_carries_saved_human_text_and_preserves_rest` (+ memoQ-import, live) |
| C | **Partial** | Open Pass (этот прогон) | открытый документ, draft+коммент | per-row Process/Rerun без потерь | Rerun с прежним текстом (proposal обновлён, текст не стёрт). См. §4.2 | `test_rerun_remote_row_*`, `test_process_remote_row_*` |
| D.1 | **Partial** | Open Pass (этот прогон) | batch size **2** | batch size работает | смена batch size на 2; 2 предложения в batch 1 из 4 (rev 5), остальные батчи не затронуты. **Не засчитано:** 23→10/10/3. См. §4.2 | `RemoteBatchSizeTestCase` (3) |
| D.2 | **N/A** (offline OK) | — | — | 100→10→100 | offline OK | `RemoteSetBatchSizeTestCase` (7) |
| D.3 | **Blocked** | нужен trace/stub-stand (НЕ live preset) | 23 → 10/10/3 | chunk_size в payload | нет trace/stub-stand | `test_non_default_chunk_size_*`, `test_real_split_groups_23_*` |
| D.4 | **Pending** | Open Pass (этот прогон) | **29 сегментов**, `remote_page_size=25` | 25+4, draft'ы выживают | **не проведено** этим прогоном (нужен полный прогон pagination) | `RemotePaginationTestCase` (3) + `test_drafts_survive_reruns_and_page_navigation` |
| E.1 | offline OK | — | — | template+термин в промпте | offline OK | `test_template_override_applies_*`, `test_relevant_termbase_term_*` |
| E.2 | offline OK | — | валидный memoQ CSV + CSV только с заголовком | терм в промпте; clobber=нет | offline OK | `test_termbase_csv_upload_*`, `test_malformed_termbase_csv_*`, `test_no_term_csv_does_not_clobber_*` |
| E.3 | offline OK | — | — | temperature/max_tokens в payload | offline OK | `test_non_default_temperature_and_max_tokens_*` (+ `TestPerRequestConfig`) |
| F | **Partial** | Open Pass (этот прогон) | **3 строки, batch size 2** | correction → след. пачка, stale не retry | Apply Changes с 1 correction. **Не засчитано:** доставка feedback (critique) в промпт (F.6). См. §4.2 | `RemoteCorrectionsTestCase` (7) |
| G | **separate** | — | — | (по отдельности) | не в A–F | отдельная строка; см. `REMOTE_PARITY_ACCEPTANCE.md` (G) |

**Офлайн база:** checkpoint **223 / 289 OK**. Г (separate). D.2 = offline-only.
D.3 = Blocked (без trace/stub-stand). **После Stop/Start (этот прогон): Open =
Pass** (прежний bind‑400 не повторился; точная ревизия не установлена),
поэтому A/C/D.1/F = **Partial** (частичные наблюдения, §4.2, **не**
полный Pass), B = **Partial** (download Pass / XML не проверен / import на
синтетическом `sample.mqxliff` = Fail `Tool element is not found`, приёмка на
нативном экспорте memoQ = **Pending**, §4.4), D.4 = **Pending**. A–F целиком
**не** объявлены пройденными.

### 3.5. Контролируемое воспроизведение Open (эмпирика, 2026-11-26)

> Статика **не** считалась подтверждением. Один контролируемый прогон
> **фактического клиентского пути** против **рабочего** Remote Lab (туннели
> 18101/18102), без processing-service и без вызова модели. Это **НЕ** точное
> воспроизведение исходного браузерного отказа: чистая fixture `sample.mqxliff`
> и настройки по умолчанию (`tag_source=target`, `batch_size=100`,
> `source_file=sample.mqxliff`), а не документ пользователя и его
> `remote_tag_source`/`remote_batch_size`.

**Путь клиента (точный, как в `remote_open`):**
`POST /documents` (multipart `{file:(filename, raw, "application/xml")}`) →
`GET /originals/{original_id}/segments?tag_source=target` →
`POST /workspaces/{document_id}/bind` (JSON
`{original_id, source_file, tag_source, segments, present_keys, batch_size}`) →
`GET /workspaces/{document_id}`.

**Результат (fixture `sample.mqxliff`, 3179 байт):**

| Шаг | Метод / маршрут | Статус |
|-----|-----------------|--------|
| 1. upload | `POST /documents` | **200** (document/original созданы) |
| 2. segments | `GET /originals/{id}/segments?tag_source=target` | **200** (12 сегм., 7 translatable) |
| 3. bind | `POST /workspaces/{document_id}/bind` | **400** |
| 4. snapshot | `GET /workspaces/{document_id}` | **не выполнен** (остановка на отказе) |

**Тело 400 (обезличенно, до обработки клиентом):**
`{"error": "bad_request", "message": "Unknown request field(s).", "details": {"fields": ["batch_size"]}}`

**Расхождение запрос ↔ ответ (п. 3):**
- Клиент присылает `batch_size` (одно из 6 полей bind).
- **Рабочий** workspace_service отвечает 400 "Unknown request field(s)" с
  `details.fields=["batch_size"]` — его допустимый набор полей bind **не
  содержит** `batch_size`.
- Это **не** document-specific: чистая fixture (0 дубликатов/пустых key, все
  сегменты с полными 4 полями) падает по той же причине — **поле, а не
  контент**.

**Вывод (эмпирически подтверждённый):** падает именно **bind** (upload и
segment-read оба 200). Причина — **contract/version drift**: **рабочий**
workspace_service отвергает `batch_size`, тогда как **клиент и источник на
диске** (`services/workspace_service/service.py`, `BIND_FIELDS` включает
`batch_size`) его **принимают** ⇒ **рабочий процесс — устаревшая ревизия**
(старее добавления `batch_size` в контракт), а не код из репозитория.

**Важно (п. 3):** это **не** объявление, что исходный сбой исправлен — прогон
различается от браузерного (другой документ, настройки по умолчанию). Он
подтверждает **причину** (bind отклоняет `batch_size` из-за устаревшего
контракта сервиса), а не то, что браузерный Open снова проходит.

**Версия рабочего сервиса — снять нельзя:** `/mnt/memoq-harness` **не
git-репозиторий** (процесс: `python -m services.workspace_service` из этой
каталоги; `git` её не видит). Локальный `HEAD` клиента **НЕ** версия рабочего
процесса.

**Следующее наблюдение / фикс (НЕ выполнен — нет авторизации):** запустить
workspace_service **с актуального источника** (ревизия с `batch_size` в
`BIND_FIELDS`), затем повторить тот же прогон; если bind → 200 и snapshot
прочитан — Open пройдёт. Требует **рестарта/передепоя** (запрещено на этой
задаче).

**Съёманные записи:** `POST /documents` создал document/original
(`sample.mqxliff`); `bind` отклонён → **workspace не создан**. Рабочие
документы не трогались; processing-service и модель не вызывались.

---

## 4. Внеплановой ручной прогон (наблюдения / новые проверки / UX)

> Ручной прогон **в приватном окне после Stop/Start стенда**. Это **не** полный
> A–F и **не** G: **A–F целиком НЕ объявляются пройденными**. Код, конфиги,
> сервисы и модели **не менялись**. Наблюдения ниже привязаны к A–F (§4.2) и
> дублировать сценарии A–F **не** нужно — здесь только **новые** факты,
> **новые** проверки и **UX-предложения**.

### 4.1. Open — Pass (этот прогон)

- **Open = Pass** для `sample.mqxliff` **после Stop/Start в приватном окне**.
  UI: **7 переводимых сегментов, Batch 1 of 1, batch size 100**.
- **Прежний bind‑400 в этом прогоне не повторился** — совместимо с §3.5
  (гипотеза устаревшей ревизии: до Stop/Start рабочий `workspace_service` не
  имел `batch_size` в `BIND_FIELDS` → `bind` 400 «Unknown field batch_size»;
  после Stop/Start — актуальный контракт). **Точная ревизия прежнего процесса
  не установлена** (§3.5); причина прежнего Fail — **устаревший процесс, не
  документ**.
- **Отдельное наблюдение (не тот же сбой):** в **обычной** (не приватной)
  сессии browser upload давал **403**. В приватной сессии Open прошёл; точная
  причина 403 **не установлена**, **403 ≠ 400** (§3.5). **Отдельную диагностику
  403 сейчас не начинаем.**
- **Ограничение:** Open=Pass зафиксирован **только** для `sample.mqxliff` в
  приватном окне. Это **не** подтверждение, что все A–F проходят.

### 4.2. Привязка наблюдений к A/C/D/F (частичные, не полный Pass)

| Наблюдение | Пункт | Покрыто | **Не** засчитано этим прогоном |
|------------|-------|---------|--------------------------------|
| Apply proposal на **2 строках** | **A** | proposal попал в draft (2 строки) | Discard; Save→Reload |
| Сохранность правки при **переходе между батчами** | **A** (neighbor-draft / draft survival) | правка сохранилась при смене батча | explicit empty-string |
| Смена **batch size на 2**; 2 предложения в **batch 1 из 4 (rev 5)**, остальные батчи не затронуты | **D.1** | batch size 2 работает, вид перестроился, соседние батчи не затронуты | 23→10/10/3 |
| **Rerun** с прежним текстом | **C** | per-row Rerun обновил proposal без стирания текста | stale-write не retried (отдельно) |
| **Apply Changes** с **1 correction** | **F** | 1 correction persist | доставка feedback (critique) в промпт — это **F.6**, отдельная проверка |

- **Не покрыто** этим прогоном: **D.4** (29 сегм., pagination), **D.3** (нет
  trace/stub-stand), **E.1/E.2/E.3** (offline OK), **B-import** (см. §4.4), **G**.
- Вывод: частичные наблюдения **не** закрывают ни один пункт A–F целиком.

### 4.3. Новые проверки (добавить; **причину не угадывать**)

- **4.3.1 Индикатор активности после Process.** После успешного Process
  индикатор должен **перестать выглядеть активным**. Наблюдение: UI показывает
  `Stored 5 proposals, rev 10`, а индикатор **всё ещё виден** → **аномалия /
  Fail индикатора**. Проверяемый факт: «после успешного Process индикатор
  деактивируется».
- **4.3.2 Process батча 2 при batch size 2 — «No proposals» ×2, ничего не
  сохранено.** После **2→100** — успех, **но в других условиях**. Записать
  модель, батч, раскладку и число предложений; **причину не угадывать** (нужен
  trace/лог промпта пачки 2).
- **4.3.3 Inline-diff после Apply Changes vs local UI.** Отдельно сверить
  inline-diff **под строкой** с локальным UI (ожидаемый ↔ фактический формат
  diff'а).
- **4.3.4 Analyze: preset и фактический engine.** Отдельно зафиксировать
  **выбранный preset** и **фактический engine** (stub **≠** живая модель).
  Stub-текст **не** считается анализом модели. Отдельно проверить, что
  **critique не меняет перевод** (F.6).
- **4.3.5 Язык файла vs пара запроса (EN→DE fixture).** В прогоне предложения
  были **на русском** при EN→DE fixture. **Предложение (не реализация):**
  определять пару **из файла** и **явно подтверждать/переопределять при Open**
  (подсказка/override), а не молча полагаться на имя файла. Это **предложение**,
  согласование — позже.

### 4.4. B — разделение: download / XML / import (импорт сейчас не расследовать)

- **Download — Pass** (файл получен на клиенте).
- **XML не проверен** (отдельный шаг, не выполнен).
- **Import в memoQ — Fail**: `Tool element is not found`. Наблюдение
  **сохраняем**; оно получено на файле, **произведённом из синтетического
  `sample.mqxliff`**.
- **Приёмка импорта на нативном экспорте memoQ — Pending**: позже проверим
  **полный цикл** (download → XML → import) на файле, **экспортированном из
  memoQ**.
- **Сейчас импорт не расследуем, XML и код не меняем.** Для будущей проверки
  полного цикла: **сохранить именно скачанный файл**; сравнить с исходным
  `sample.mqxliff` и с **нативно экспортированным memoQ MQXLIFF**; проверить
  импорты **исходной fixture** и **нативного файла** **тем же способом**.
- **Не считать**, что экспортёр удалил `<tool>`, **пока это не показано
  сравнением**. Download ≠ import; XML-parse ≠ import (это **три** разных уровня,
  см. §B).

### 4.5. UX-предложения (**пока не реализовывать**)

- **Apply all proposals** над **последней колонкой** для **текущего батча**:
  **не** затирает ручной текст/черновики и **не** выполняет **Save неявно**.
- **Analyze** вместо **Analyse**; и **понятное место** для результата.
- Нужен ли **один шаг экспорта** вместо `Export` + `Download last export`.
- **Ничего из этого пока не реализовывать.**

### 4.6. Test models (**отдельно от G, G не закрывать**)

- **gemma3:1b** — visible text.
- **qwen3-coder-next:q4_K_M** — visible text.
- **qwen3.8:latest** — **inconclusive**: 16 токенов, reasoning present,
  `finish_reason=length`.
- **Не смешивать** с Process; **G остаётся separate** (не закрыт).
