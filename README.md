# Dzen Agent V0

Локальное приложение для генерации, редактирования и хранения статей, а также подготовки
черновика в редакторе Яндекс Дзена. V0 **никогда не нажимает кнопку публикации**.

## Установка

Нужен Python 3.11 или новее.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=.playwright python -m playwright install chromium
```

## Настройка OpenAI

Скопируйте `.env.example` в `.env` и замените тестовое значение своим ключом:

```bash
cp .env.example .env
```

`OPENAI_API_KEY` хранится только в локальном `.env`; этот файл исключён из Git.

## Запуск

Из корня проекта с активированным окружением:

```bash
python -m streamlit run dzen_agent/ui.py
```

Откройте адрес, который покажет Streamlit (обычно `http://localhost:8501`). Введите тему,
создайте статью, отредактируйте её и сохраните.

## Первый запуск Дзена

Нажмите «Подготовить в Дзене». Откроется отдельное окно Chromium с постоянным локальным
профилем в `browser-profile/`. При необходимости войдите в Яндекс вручную, закройте процесс
в терминале и повторно нажмите кнопку. CAPTCHA и двухфакторная авторизация выполняются только
вами. После заполнения проверьте материал в браузере. Приложение не выполняет публикацию.

Селекторы Дзена находятся в `dzen_agent/browser/selectors.py`; если внешний интерфейс
изменится, их можно обновить независимо от остального приложения.

## Проверка и остановка

```bash
pytest
ruff check .
```

Чтобы остановить Streamlit, вернитесь в терминал и нажмите `Ctrl+C`. Закройте отдельное окно
Chromium после работы с черновиком.

Если Playwright не может скачать свой Chromium, но Chromium уже установлен в системе,
укажите в `.env` путь, например `DZEN_BROWSER_EXECUTABLE=/usr/bin/chromium`.

## Данные

Статьи сохраняются локально в `data/dzen_agent.db`. Папки с БД и браузерной сессией не
попадают в Git. Текущие статусы: `DRAFT`, `READY_FOR_REVIEW`, `APPROVED`,
`PREPARED_IN_DZEN`; V0 автоматически использует `DRAFT` и `PREPARED_IN_DZEN`.

