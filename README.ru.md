# Harness Handbook

[English](README.md) | [中文](README.zh-CN.md) | **Русский**

[![Blog](https://img.shields.io/badge/Blog-105864?style=for-the-badge)](https://ruhan-wang.github.io/Harness-Handbook/)
[![arXiv](https://img.shields.io/badge/arXiv-2607.13285-b31b1b?style=for-the-badge&logo=arxiv&logoColor=white)](https://arxiv.org/abs/2607.13285)
[![Hugging Face Daily Paper](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Daily%20Paper-ffd21e?style=for-the-badge)](https://huggingface.co/papers/2607.13285)

Превратите любую кодовую базу в удобный для навигации **handbook** (справочник), а затем
используйте этот handbook, чтобы помочь код-агенту найти *каждое* место, которое затрагивает
изменение.

Проект состоит из двух частей:

1. **Сгенерировать handbook** из вашего исходного кода — структурированную,
   постадийную карту кодовой базы (markdown + опционально HTML).
2. **Использовать handbook как помощник** — подключить его к планировщику код-агента и
   измерить, насколько лучше агент локализует правки с ним, чем без него.

```
handbook_generate_large/    сгенерировать handbook для БОЛЬШОЙ кодовой базы
handbook_generate_small/    сгенерировать handbook для МАЛОЙ кодовой базы
handbook_as_helper/         использовать handbook как планировщик код-агента + ресинхронизация
```

В каждой папке есть собственный подробный `README.md`; эта страница — сквозное руководство.

### Демо и примеры

- **[Handbook Studio](https://ruhan-wang.github.io/Harness-Handbook/studio/index.html)** — интерактивное демо для просмотра handbook
- **[Terminus 2 Handbook](https://ruhan-wang.github.io/Harness-Handbook/terminus-handbook/index.html)** — пример handbook, сгенерированного для Terminus 2

<p align="center">
  <a href="https://ruhan-wang.github.io/Harness-Handbook/studio/index.html">
    <img src="assets/handbook-studio.png" alt="Демо Handbook Studio" width="720"/>
  </a>
  <br/>
  <em>Handbook Studio</em>
</p>

<p align="center">
  <a href="https://ruhan-wang.github.io/Harness-Handbook/terminus-handbook/index.html">
    <img src="assets/terminus-handbook.png" alt="Пример handbook для Terminus 2" width="720"/>
  </a>
  <br/>
  <em>Пример: Terminus 2 Handbook</em>
</p>

---

## 0. Установка

```bash
git clone <your-repo-url>
cd Harness_Handbook          # или как вы назвали клон

python3 -m venv .venv && source .venv/bin/activate
pip install tree-sitter tree-sitter-language-pack pyyaml requests markdown pygments
```

**Доступ к LLM.** Каждая фаза, не являющаяся чисто статическим анализом, вызывает LLM. Как
**генераторы** (`handbook_generate_*/**/api_client.py`), так и **помощник** (его код-агент
+ ресинхронизация) обращаются к одному и тому же **OpenAI-совместимому** эндпоинту,
настраиваемому через стандартные переменные окружения OpenAI — ничего не зашито в код:

```bash
export OPENAI_API_KEY=sk-...                        # ваш ключ OpenAI API (обязательно)
# опциональные переопределения:
export OPENAI_MODEL=gpt-4o-mini                     # по умолчанию: gpt-4o-mini
export OPENAI_BASE_URL=https://api.openai.com/v1    # по умолчанию; или любой OpenAI-совместимый
                                                    # эндпоинт (self-hosted vLLM, прокси, …)
```

Подойдёт любой OpenAI-совместимый эндпоинт — укажите на него `OPENAI_BASE_URL` (локальный
vLLM, LiteLLM и т. п.) и используйте `OPENAI_API_KEY=EMPTY`, если ключ не требуется. *Фаза 1
генераторов не нуждается в LLM* — её всегда можно запустить первой, чтобы проверить парсер.
Имена `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` по-прежнему учитываются как переопределения.

---

## 1. Сгенерировать handbook

Выберите конвейер под вашу кодовую базу. Оба поддерживают Python, Rust, TypeScript, Go
(плюс Starlark / Shell / PowerShell), а также `--lang auto` для автоопределения и объединения
всего под корнем исходников.

### 1A. Большая кодовая база → `handbook_generate_large/`

Снизу вверх, **файл-как-лист**: читаем *каждый* файл, синтезируем упорядоченный скелет стадий,
затем формируем повествование от листьев вверх до системного обзора. Полнота покрытия
обеспечена по построению — ни один файл не отбрасывается молча, и написанный вручную скелет
не требуется.

```bash
cd handbook_generate_large

# (опционально) Только фаза 1 — статический граф вызовов, без LLM. Хороший smoke-тест.
python3 run.py --lang auto --source-root /path/to/repo --work-dir work/repo --phase 1

# Полный прогон: глубокое чтение по файлам → doctor-синтез → организация → повествование + HTML
python3 run.py \
    --source-root /path/to/repo \
    --work-dir work/repo \
    --read-detail deep --read-batch-size 1 --read-workers 100 \
    --synth-mode doctor --doctor-workers 32 --doctor-llm-workers 100 \
    --organize-workers 100 \
    --phase3-html

# Китайский handbook (используйте СВЕЖИЙ work-dir; проход чтения нужно перезапустить для zh)
python3 run.py --source-root /path/to/repo --work-dir work/repo_zh \
    --read-detail deep --read-batch-size 1 --narrate-lang zh --phase3-html
```

Ключевые флаги: `--read-detail deep` (полное чтение файла; сочетайте с `--read-batch-size 1`),
`--synth-mode doctor` (скелет по схеме «актёр-критик», без дополнительных учётных данных),
`--narrate-lang {en,zh}`, `--phase3-html` (многостраничный сайт), `--phase <all|1|2a|2b|2c|2|3>`
для запуска подмножества.

**Вывод** → `work/repo/handbook/`:

```
overview.md            системный обзор
index.md               индекс по стадиям (основа маршрутизации)
register.md            кросс-стадийные регистры состояния
stages/<id>.md         по одной странице на стадию
html/overview.html     точка входа в HTML-сайт (если --phase3-html)
```

### 1B. Малая кодовая база → `handbook_generate_small/`

Три фазы: статический граф → LLM-классификация → LLM-повествование. Этот конвейер
**управляется скелетом**: вы предоставляете короткий `skeleton.yaml`, описывающий жизненный
цикл стадий, и один раз описываете проект через `--project-*`, чтобы текст был подстроен под него.

```bash
cd handbook_generate_small

# Только фаза 1 (без LLM)
python3 run.py --lang auto --source-root /path/to/repo --work-dir work/repo --phase 1

# Полный прогон (фазы 2 и 3 требуют LLM + ваш skeleton.yaml)
python3 run.py \
    --lang auto \
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

Ключевые флаги: `--skeleton` (обязателен для фазы 2+), `--project-name/-kind/-brief[-file]`
(вставляются в каждый промпт), `--out-lang {zh,en}`, `--max-stage-workers`,
`--phase <all|1|2|3|1-2|2-3>`.

**Вывод** → `work/repo/phase3/output/` (handbook в markdown + JSON).

> **Какой выбрать?** Используйте **large**, когда не хотите писать скелет вручную и хотите
> гарантированное полное покрытие всех файлов. Используйте **small**, когда кодовая база
> достаточно мала, чтобы описать её написанным вручную скелетом стадий, и вам нужен более
> точный текст.

---

## 2. Использовать handbook как планировщик → `handbook_as_helper/`

Эта папка содержит всего две вещи (весь код оценки/скоринга/бенчмарков был удалён):

1. **Handbook как планировщик** — превратить handbook в SKILL агента и передать его
   планировщику код-агента, который затем локализует правки для запроса на изменение.
2. **Ресинхронизация handbook** — после изменений кода прокатить производный слой handbook
   вперёд, чтобы он соответствовал диффу.

### 2A. Использовать handbook как планировщик

```bash
cd handbook_as_helper

# 0. укажите агенту вашу модель (см. Установка)
export OPENAI_API_KEY=sk-...                  # + опционально OPENAI_MODEL / OPENAI_BASE_URL
export EVAL_TARGET=codex                      # какой целевой проект (см. pipeline/targets.py)

# 1. Соберите готовый для планировщика SKILL из handbook, сгенерированного на шаге 1.
#    --src — это директория с отрендеренным handbook (напр. work/repo/handbook или .../phase3/output).
python handbook_skills/build_skill_from_handbook.py --target codex \
    --src /path/to/rendered/handbook
#    → создаёт handbook_skills/handbook_skill_codex/ (SKILL.md + references/)
```

Затем запускайте **планировщик на основе handbook** из `pipeline/code_agent.py`. Это ЕДИНСТВЕННЫЙ
агент только для чтения, который маршрутизирует по навигационному handbook (SKILL / index /
registers / страницы стадий) и сам читает НАСТОЯЩИЙ исходный код, прежде чем выдать точный,
дословный план ПРАВОК — без под-агента-локатора, без map-reduce:

```python
import sys; sys.path.insert(0, "pipeline")
from pathlib import Path
from code_agent import run_query             # нужен NexAU + переменные OPENAI_*/LLM_* выше

out = run_query(
    "<запрос рецензента на изменение на естественном языке>",
    Path("/path/to/source"),                # кодовая база, для которой строится план
    Path("runs/case1"),                     # временная песочница (git-копия исходников, затем удаляется)
    # arm="handbook" по умолчанию (единственный вариант)
)
print(out["plan"])                          # план локализации от планировщика
```

Ветка `handbook` **и есть** этот планировщик — единственный планировщик в этом репозитории. Он
работает **только в режиме плана** (выдаёт план; фазы исполнителя/диффа нет). `code_agent.py`
также несёт общий связующий код, на котором он построен (загрузка конфигурации, копия handbook
только для навигации, git-песочница, раннер агента), переиспользуемый ресинхронизацией.

### 2B. Ресинхронизировать handbook после изменений кода

Это **не зависит от планировщика выше.** Планировщик работает только в режиме плана — он
никогда не редактирует код, поэтому не создаёт дерево `edited/` или дифф. Вместо этого
ресинхронизация прокатывает производный слой handbook вперёд к **реальному изменению кода**,
которое вы предоставляете. Подготовьте `<case_dir>`, содержащий:

- `edited/` — изменённое дерево исходников (напр. чекаут с применённым смерженным PR)
- `plan.md` — описание изменения (его объявления управляют согласованием)
- `agent.diff` *(опционально)* — дифф `edited/` относительно исходного; пустой дифф пропускается

```bash
# прокатить производный слой handbook вперёд к этому изменению (любой язык с адаптером)
python pipeline/update_handbook.py <case_dir>
python pipeline/update_handbook.py <case_dir> --no-translate   # пропустить перевод карточек
```

### Что нужно предоставить

- **Целевой проект.** *Исходное (pristine) дерево*, язык и формулировки промптов каждой цели
  живут в `pipeline/targets.py` (одна запись на проект); переопределите путь к исходникам через
  `PRISTINE_ROOT` при необходимости. Активная цель задаётся через `EVAL_TARGET`.
- Полные подробности см. в `handbook_as_helper/README.md` (использование планировщика,
  ресинхронизация, таблица файлов и все переменные окружения).

---

## Структура репозитория

```
Harness_Handbook/
├── README.md                     ← вы здесь (английский)
├── README.zh-CN.md               ← 中文版
├── README.ru.md                  ← русская версия
├── handbook_generate_large/      генератор для больших кодовых баз (run.py, phase1/2/3, adapters, build_site.py)
├── handbook_generate_small/      генератор для малых кодовых баз (run.py, phase1/2/3, adapters, project_context.py)
└── handbook_as_helper/           использование handbook как планировщика + ресинхронизация
    ├── pipeline/                 code_agent.py (планировщик handbook), targets.py, update_handbook.py, resync_*, lang_layer.py
    ├── handbook_skills/          build_skill_from_handbook.py (+ другие сборщики скиллов)
    ├── prompts/                  planner_handbook.md (промпт планировщика handbook)
    └── rerun_resync.py           помощник ресинхронизации
```

**Сгенерированные артефакты не коммитятся** (в `.gitignore`) и воссоздаются запуском конвейеров:
`work/`, `site/`, `site_technical_backup/` (генераторы), `runs/` и собранные
`handbook_skills/handbook_skill_*/` (помощник). Никакие учётные данные не коммитятся.

---

## Цитирование

Если вы используете Harness Handbook, пожалуйста, цитируйте:

```bibtex
@misc{wang2026harnesshandbookmakingevolving,
      title={Harness Handbook: Making Evolving Agent Harnesses Readable,Navigable, and Editable}, 
      author={Ruhan Wang and Yucheng Shi and Zongxia Li and Zhongzhi Li and Yue Yu and Junyao Yang and Kishan Panaganti and Haitao Mi and Dongruo Zhou and Leoweiliang},
      year={2026},
      eprint={2607.13285},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2607.13285}, 
}
```
