# Statist

Учёт поступлений, резервирование и передача гуманитарной помощи.

**Сайт:** [rey33.pythonanywhere.com](https://rey33.pythonanywhere.com/)

Python 3.11+ · Flask · SQLite · серверные HTML-шаблоны.

## Структура

| Путь | Содержимое |
|---|---|
| `app.py` | Маршруты, формы, CSRF, CSV, проверка `/healthz` |
| `store.py` | Схема SQLite, остатки, транзакции, резервы и выдачи |
| `ui.py` | Шаблоны и оформление |
| `wsgi.py` | Точка входа хостинга |
| `manage.py` | Инициализация демонстрационных данных |
| `tests/test_workflow.py` | Проверки учёта и HTTP-сценариев |
| `.github/workflows/tests.yml` | CI на Python 3.11 и 3.12 |
| `docs/DEPLOYMENT.md` | Размещение и обновление сервера |
| `instance/` | База данных; исключена из Git |

## Разработка

Создать окружение: `python -m venv .venv`.

Активировать: Windows — `.venv\Scripts\activate`; Linux/macOS — `source .venv/bin/activate`.

```sh
python -m pip install -r requirements.txt
python manage.py init-demo
python app.py
```

`init-demo` сохраняет существующие данные. `app.py` — сервер разработки; хостинг использует `wsgi.application`.

## Тесты

```sh
python -m unittest discover -s tests -v
```

16 проверок: дефицит, конкурирующие резервы, повторная выдача, отмена, сохранение после перезапуска, CSRF, экранирование ввода, CSV и полный HTTP-сценарий.

## Конфигурация

| Переменная | Назначение |
|---|---|
| `STATIST_DATABASE` | Путь SQLite; по умолчанию `instance/statist.sqlite3` |
| `STATIST_SECRET_KEY` | Ключ сессий; по умолчанию сохраняется в метаданных базы |

## Ограничения

Онлайн-версия использует общую вымышленную базу без авторизации. Только тестовые данные.

Целые количества, один склад на заявку, резервирование всего состава. Частичные выдачи, возвраты, партии и межскладские перемещения пока не реализованы.
