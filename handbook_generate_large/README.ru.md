# handbook_generate_large — конвейер handbook «файл-как-лист»

[English](README.md) | [中文](README.zh-CN.md) | **Русский**

Превращает **большую** кодовую базу в удобный для навигации **handbook** (markdown +
опционально HTML), снизу вверх, где **ФАЙЛ — это листовой узел**. Каждый файл читается и
описывается, файлы группируются в упорядоченный скелет стадий, и всё целиком описывается
повествованием от листьев вверх до системного обзора. Полнота покрытия обеспечена по
построению — ни один файл не отбрасывается молча, и вам не нужно писать скелет вручную.

## Идея: снизу вверх, файл как лист

1. **Прочитать каждый файл** → карточку на файл (назначение; в глубоком режиме — подробное
   описание + инвентарь функций, выведенный из графа, с отношениями вызовов).
2. **Синтезировать скелет стадий** из этих карточек (упорядоченный хребет жизненного цикла)
   и назначить каждый файл стадии.
3. **Организовать каждую стадию внутри** (упорядочить + разбить её файлы на подгруппы).
4. **Формировать повествование снизу вверх**: отрендерить детали файлов/функций на листьях,
   затем с помощью LLM свести подстадию → стадию → систему; извлечь кросс-стадийные регистры
   состояния.

*Порядок* стадий берётся из графа вызовов (точки входа → вызывающие перед вызываемыми), так что
скелет — это повествовательный хребет, а не слепая кластеризация.

## Конвейер

```
Phase 1   run_phase1.py            source → phase1/graph.json              (без LLM)
Phase 2a  phase2/read_files        читать КАЖДЫЙ файл → phase2/cards/       (одна карточка/файл)
Phase 2b  phase2/synth_stages      карточки → phase2/skeleton.yaml + file_stage.json
Phase 2c  phase2/organize_stages   упорядочить + сгруппировать каждую стадию → stage_organization.yaml
Phase 3   phase3/build_handbook    повествование снизу вверх → handbook/ (md + опционально html)
```

## Установка

```bash
pip install tree-sitter tree-sitter-language-pack pyyaml requests markdown pygments

# LLM: любой OpenAI-совместимый эндпоинт (фазам 2/3 он нужен; фазе 1 — нет).
export OPENAI_API_KEY=sk-...                        # обязательно (=EMPTY для локального эндпоинта без ключа)
export OPENAI_MODEL=gpt-4o-mini                     # опционально (по умолчанию: gpt-4o-mini)
export OPENAI_BASE_URL=https://api.openai.com/v1    # опционально; или self-hosted vLLM / прокси
```

`markdown` + `pygments` нужны только для HTML-сайта. Имена `HANDBOOK_LLM_MODEL` /
`HANDBOOK_LLM_BASE_URL` / `HANDBOOK_LLM_API_KEY` по-прежнему учитываются как переопределения.

## Структура

```
run.py            сквозной драйвер (--phase all|1|2a|2b|2c|2|3|список-через-запятую)
run_phase1.py     фаза 1 отдельно (статический граф вызовов)
run_phase3.py     фаза 3 отдельно (повествование; переиспользует артефакты фазы 2)
ir.py  adapters/  языковые адаптеры → языконезависимый IR (rust/python/go/ts/…)
shared/           api_client (OpenAI-совместимый LLM), skeleton_yaml, critic, progress
phase1/           build_graph.py
phase2/           read_files, synth_stages, synth_agent, skeleton_doctor_files,
                  file_assign, nav_pack, organize_stages, agent_tools/
phase3/           load_inputs, render_file, rollup, registers, render_html, build_handbook
```

## Фазы подробно

### 2a — прочитать каждый файл (`phase2/read_files.py`)
Пакетный + параллельный проход O(файлы). `--read-detail deep` читает каждый файл целиком и
пишет листовое содержимое handbook: подробное `description` + пофункциональный инвентарь
(qualname / диапазон строк / сигнатура / отношения вызовов из графа; LLM пишет `purpose` /
`data_flow` / `relations`). Карточки пишутся инкрементально (устойчиво к сбоям), а `--resume`
пропускает хорошие.

### 2b — синтезировать стадии (`phase2/synth_stages.py`)
Сводит назначения по файлам до уровня директорий, передаёт это + точки входа графа вызовов
в LLM и получает **упорядоченный** скелет стадий; затем назначает каждый файл стадии.
`--synth-mode`:
- **`oneshot`** (по умолчанию): один вызов LLM набрасывает скелет, затем однократное назначение.
- **`doctor`**: набросок за один проход + **цикл сходимости «актёр-критик»**
  (`skeleton_doctor_files`, переиспользует `shared/critic.py`), который разбивает / сливает /
  добавляет стадии и переназначает, пока не размещён каждый файл. **NexAU / `LLM_*` не нужны.**
- **`agent`**: агент NexAU набрасывает скелет (нужны `LLM_BASE_URL` / `LLM_MODEL` /
  `LLM_API_KEY`), затем тот же цикл сходимости. Откатывается к oneshot, если этот эндпоинт
  недоступен.

### 2c — организовать каждую стадию (`phase2/organize_stages.py`)
Для каждой стадии: упорядочить её файлы по зависимостям графа вызовов (вызывающие перед
вызываемыми, алгоритм Кана) и разбить на 2–8 упорядоченных подгрупп. ~O(стадии) вызовов LLM.

### 3 — повествование (`phase3/build_handbook.py`)
Обход дерева стадий в обратном порядке (post-order): отрендерить детали файлов/функций на
листьях (без LLM), с помощью LLM свести каждый нелистовой узел из сводок его потомков, затем
системный обзор. Также извлекает **регистры состояния** (кросс-стадийное глобальное состояние)
и индекс. Выдаёт `handbook/`: `overview.md`, `index.md` (по стадиям с обзорами), `register.md`,
`stages/<id>.md`, и опционально многостраничный (`--phase3-html` в `run.py`; `--html` в
`run_phase3.py`) или одностраничный (`--html-single`) HTML-сайт.

## Использование

```bash
# всё (английский): глубокое чтение → синтез → организация → повествование + HTML
python3 run.py --source-root /path/to/repo --work-dir work/repo \
    --read-detail deep --read-batch-size 1 --read-workers 100 \
    --synth-mode doctor --doctor-workers 32 --doctor-llm-workers 100 \
    --organize-workers 100 --phase3-html

# Китайский handbook (используйте СВЕЖИЙ work-dir; 2a нужно перезапустить для zh-карточек)
python3 run.py --source-root /path/to/repo --work-dir work/repo_zh \
    --read-detail deep --read-batch-size 1 --narrate-lang zh --phase3-html

# фаза за фазой
python3 run.py --source-root … --work-dir work/repo --phase 1
python3 run.py --source-root … --work-dir work/repo --phase 2a --read-detail deep
python3 run.py --source-root … --work-dir work/repo --phase 2b --synth-mode doctor
python3 run.py --source-root … --work-dir work/repo --phase 2c --organize-workers 100
python3 run_phase3.py --phase2-dir work/repo/phase2 --out work/repo/handbook \
    --lang zh --workers 100 --html
```

`--narrate-lang {en,zh}` управляет языком всего текста, попадающего в handbook (детали
файлов/функций, обзоры стадий/системы, семантика регистров) на 2a/2b/2c/3. `--lang` не связан с
этим — это подсказка языка исходников для фазы 1 (`auto` определяет и объединяет каждый
поддерживаемый язык под корнем исходников).

## Заметки

- **Нет классификации на уровне функций.** Этот конвейер работает только по схеме «файл-как-лист»;
  поточный путь на уровне функций (iterate / pass_a..d) живёт в `handbook_generate_small`.
- Доступ к LLM — это OpenAI-совместимый `Api` в `shared/api_client.py`, настраиваемый через
  `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` (только набросок скелета в режиме `agent`
  использует вместо этого NexAU, через переменные `LLM_*`).
- `work/` хранит артефакты по проектам (граф, карточки, скелет, handbook) и создаётся по
  требованию; он не коммитится.
