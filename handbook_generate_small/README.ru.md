# handbook_generate_small — конвейер handbook, управляемый скелетом

[English](README.md) | [中文](README.zh-CN.md) | **Русский**

**Проектонезависимый** трёхфазный конвейер (статический граф → LLM-классификация →
LLM-повествование) с единым фронтендом `LanguageAdapter`, так что он может работать с **Python,
Rust, TypeScript, Go** (плюс лёгкие Starlark / Shell / PowerShell). Лучше всего подходит для
кодовых баз, достаточно малых, чтобы описать их коротким написанным вручную **скелетом стадий**,
и когда вам нужен точно подстроенный текст.

Идентичность проекта вставляется во время запуска через `--project-name` / `--project-brief`
/ `--project-kind` (читаются через `project_context.py`), так что ничего не зашито в код —
handbook генерируется для *любой* кодовой базы, на которую вы его укажете.

## Конвейер

```
Phase 1   run_phase1.py   source → phase1/graph.json                  (без LLM)
Phase 2   phase2/          LLM-классификация (итерация «критик-актёр»)   → назначение стадий
Phase 3   phase3/          LLM-повествование (актёр-критик-рефлексия, параллельно по стадиям) → handbook
```

Фазы 2/3 требуют LLM **и** написанного пользователем `skeleton.yaml`, описывающего жизненный
цикл стадий.

## Структура

```
handbook_generate_small/
├── project_context.py        # идентичность проекта, вставляемая в каждый промпт LLM
├── ir.py                     # языконезависимый IR (FunctionNode/BoundaryNode/CallEdge)
├── adapters/                 # ABC LanguageAdapter + фронтенды по языкам
├── phase1/build_graph.py     # языконезависимая сборка графа + эмиттеры
├── run_phase1.py             # CLI фазы 1
├── phase2/                   # LLM-классификация (критик-актёр); api_client живёт здесь
├── phase3/                   # LLM-повествование (актёр-критик-рефлексия), параллельно по стадиям
└── run.py                    # сквозной драйвер (phase1 → phase2 → phase3)
```

## Установка

```bash
pip install tree-sitter tree-sitter-language-pack pyyaml requests markdown pygments

# LLM: любой OpenAI-совместимый эндпоинт (фазам 2/3 он нужен; фазе 1 — нет).
export OPENAI_API_KEY=sk-...                        # обязательно (=EMPTY для локального эндпоинта без ключа)
export OPENAI_MODEL=gpt-4o-mini                     # опционально (по умолчанию: gpt-4o-mini)
export OPENAI_BASE_URL=https://api.openai.com/v1    # опционально; или self-hosted vLLM / прокси
```

`markdown` + `pygments` нужны только для HTML-рендеринга. Клиент живёт в `phase2/api_client.py`;
имена `HANDBOOK_LLM_MODEL` / `HANDBOOK_LLM_BASE_URL` / `HANDBOOK_LLM_API_KEY` по-прежнему
учитываются как переопределения.

## Использование

Сквозной прогон. Опишите проект один раз через `--project-*`, чтобы промпты были подстроены под него:

```bash
python3 run.py \
    --lang rust \
    --source-root /path/to/repo \
    --skeleton skeletons/repo.yaml \
    --work-dir work/repo \
    --title "Repo Handbook" \
    --project-name "Repo" \
    --project-kind "coding agent" \
    --project-brief "A terminal coding agent that edits code and runs commands." \
    --out-lang en \
    --max-stage-workers 4
```

`--project-brief-file path.md` читает описание из файла вместо этого. Если `--project-name`
опущен, используется `--title`.

Только граф вызовов (без LLM, любой язык):

```bash
python3 run.py --lang rust --source-root /path/to/repo --work-dir work/repo --phase 1
# или напрямую:
python3 run_phase1.py --lang go --source-root /path/to/repo --out out/repo
```

`--phase` принимает `all | 1 | 2 | 3 | 1-2 | 2-3`. `--out-lang {zh,en}` задаёт язык handbook
(по умолчанию `zh`). Ограничьте фазу 1 конкретными файлами через `--files a.py,b.py` (иначе она
автоматически находит все файлы выбранного языка под `--source-root`).

**Вывод** → `work/repo/phase3/output/` (handbook в markdown + JSON).

## Поддержка языков

| Язык | Парсер | Узлы (fn/method/sig/async/class) | Рёбра вызовов | Типизация self-атрибутов |
|---|---|---|---|---|
| Python | stdlib `ast` | точно | полностью (все `call_type`) | из присваиваний в `__init__` + аннотаций |
| Rust | tree-sitter | полностью | self / self-field / param / `Type::` / free / macro | из типов полей структур |
| TypeScript | tree-sitter | полностью (методы классов, функции, стрелки) | this / this-field / param / free / import | из полей классов + параметров конструктора |
| Go | tree-sitter | полностью (функции, методы с получателем) | receiver / receiver-field / param / free / pkg | из типов полей структур |
| Starlark | tree-sitter | функции (без классов) | имя вызова → внутренний/граничный | н/д |
| Shell (bash) | tree-sitter | функции (без классов) | имя команды → внутренний/граничный | н/д |
| PowerShell | tree-sitter | функции (без классов) | имя команды → внутренний/граничный | н/д |

Все эмитят **одну и ту же схему `graph.json`**, так что фазы 2/3 потребляют любой из них без
изменений. Starlark / Shell / PowerShell используют лёгкую модель свободных функций (слабая
семантика графа вызовов — большинство команд внешние), так что смешанный репозиторий **не теряет
файлов**.

### Смешанные по языкам репозитории: `--lang auto`

`--lang auto` находит каждый поддерживаемый язык под корнем исходников и объединяет их в один
`graph.json`. Графы вызовов по языкам полны; **кросс-языковые рёбра вызовов рвутся на границе**
(напр. Rust, запускающий Python-скрипт) и попадают в `dropped_calls.json`, как и любой другой
неразрешённый вызов. Ни одна функция никогда не теряется.

```bash
python3 run.py --lang auto --source-root /path/to/repo \
    --skeleton skeletons/repo.yaml --work-dir work/repo --title "Repo Handbook"
```

### Известные упрощения (не-Python)

- Разрешение вызовов — это статический анализ по мере возможности (без полного вывода типов);
  всё, что не удаётся привязать к имени, попадает в `dropped_calls.json` как `unresolved`.
- Разбиение qualname у `boundary` использует сегментацию по `.` (настроено под точечные пути
  Python); граничные узлы Rust с `::` всё ещё разрешаются, но разбиение их метаданных
  модуля/класса приблизительно. Фазы 2/3 это не затрагивает — они опираются на
  qualname + файл + диапазон строк.

## Контекст проекта (делаем промпты универсальными)

Во время запуска `run.py` вставляет три переменные окружения (читаемые `project_context.py`),
которые потребляет каждый промпт фазы 2 / фазы 3:

| переменная окружения (задаётся `run.py`) | CLI-флаг | значение |
|---|---|---|
| `HANDBOOK_PROJECT_NAME` | `--project-name` (откатывается к `--title`) | отображаемое имя, напр. "Redis" |
| `HANDBOOK_PROJECT_BRIEF` | `--project-brief` / `--project-brief-file` | описание из 1–3 предложений |
| `HANDBOOK_PROJECT_KIND` | `--project-kind` | существительное, напр. "web service", "compiler" |

Опциональное обогащение подсистемами (пусто по умолчанию, задаётся напрямую в окружении, если
нужно): `HANDBOOK_SUBSYS_FILE_MAP` (JSON `{"file.py": "subsys-x"}`) и
`HANDBOOK_SUBSYS_BOUNDARY_MAP` (JSON `{"module.path": "subsys-x"}`).

## Параллелизм

- **Фаза 2 · Проход A** уже классифицирует функции в пуле потоков.
- **Фаза 3** генерирует стадии параллельно (`--max-stage-workers`, по умолчанию 4). Единицы
  Tier 3 на уровне функций остаются последовательными внутри стадии, чтобы каждая могла
  ссылаться на уже написанных соседей. Задайте `--max-stage-workers 1` для полностью
  последовательного прогона.

## Добавление языка

1. Установите через `pip` или положитесь на `tree-sitter-language-pack` для грамматики.
2. Добавьте `adapters/<lang>_adapter.py`, реализующий `LanguageAdapter.analyze()` (возвращает
   `ModuleAnalysis`) и опционально `statement_spans()`. Используйте обёртку `TSNode` +
   `parse_tree()` из `base.py`.
3. `register("<lang>", <Adapter>, (".ext",))` внизу; `base._autoregister` подхватит его.
