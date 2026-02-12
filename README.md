# Telegram Admin Bot

Бот для управления тестовой инфраструктурой ONLYOFFICE: почтовые ящики на iRedMail-сервере и дроплеты DigitalOcean.

- Создание и сброс паролей почтовых ящиков
- Создание инстансов в DigitalOcean на определённый срок
- Продление или удаление инстансов
- Управление своими инстансами через меню «Управление инстансами»
- Уведомления о событиях дроплетов в Telegram-канал
- Работа в группах с разграничением доступа

---

## Структура проекта

```
telegram-admin-bot/
├── .env.example              # Шаблон переменных окружения
├── docker-compose.yml        # Запуск через Docker
├── Dockerfile                # Конфигурация контейнера
├── requirements.txt          # Python-зависимости
├── pyproject.toml            # Конфигурация Ruff (линтер/форматтер)
│
├── .github/workflows/
│   └── ci.yml                # GitHub Actions: lint, test, Docker build
│
├── src/
│   ├── bot.py                # Основной файл бота
│   ├── config.py             # Конфигурация (загрузка .env)
│   └── modules/
│       ├── authorization.py      # Проверка прав доступа
│       ├── database.py           # SQLite CRUD для инстансов
│       ├── mail.py               # Управление почтовыми ящиками (SSH)
│       ├── create_test_instance.py  # DigitalOcean API
│       └── notifications.py      # Уведомления в Telegram-канал
│
├── tests/
│   ├── conftest.py               # Фикстуры pytest
│   ├── test_authorization.py     # Тесты авторизации
│   ├── test_database.py          # Тесты базы данных
│   ├── test_mail.py              # Тесты почтового модуля
│   └── test_notifications.py     # Тесты уведомлений
│
└── instances.db              # SQLite (создаётся автоматически)
```

---

## Настройка

Скопируйте шаблон и заполните значения:

```bash
cp .env.example .env
```

Полный список переменных — см. `.env.example`.

### Обязательные переменные

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен Telegram-бота |
| `SSH_HOST`, `SSH_PORT`, `SSH_USERNAME`, `SSH_KEY_PATH` | SSH-доступ к почтовому серверу |
| `DIGITALOCEAN_TOKEN` | API-токен DigitalOcean |
| `AUTHORIZED_MAIL_USERS` | Telegram user ID с доступом к почте (через запятую) |
| `AUTHORIZED_DROPLET_USERS` | Telegram user ID с доступом к дроплетам (через запятую) |
| `MAIL_DEFAULT_DOMAIN` | Домен почтового сервера |
| `MAIL_DB_USER`, `MAIL_DB_PASSWORD` | Учётные данные БД почтового сервера |

### Опциональные переменные

| Переменная | Описание | По умолчанию |
|---|---|---|
| `DB_PATH` | Путь к файлу SQLite | `./instances.db` |
| `NOTIFICATION_CHANNEL_ID` | ID Telegram-канала для уведомлений | не задано (уведомления отключены) |

---

## Установка и запуск

### Локально

```bash
pip install -r requirements.txt
python src/bot.py
```

### Docker

```bash
docker-compose up --build -d
```

---

## Линтинг и тесты

```bash
# Проверка стиля кода (Ruff)
ruff check src/
ruff format --check src/

# Запуск тестов
pip install pytest pytest-asyncio
pytest tests/ -v
```

---

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`) автоматически запускает:
- **lint** + **test** — на push/PR в `main`
- **Docker build & push** — на каждый push в `main` и на теги `v*.*.*`

Для Docker push необходимо настроить секреты в репозитории (Settings > Secrets):
- `DOCKERHUB_USERNAME` — логин DockerHub
- `DOCKERHUB_TOKEN` — токен доступа DockerHub

---

## Как работает бот

### Создание почтового ящика
1. Бот запрашивает имя ящика (без домена)
2. Генерирует случайный пароль
3. Создаёт ящик на почтовом сервере через SSH
4. Выдаёт настройки для подключения (IMAP/SMTP)

### Создание инстанса в DigitalOcean
1. Выбор SSH-ключа
2. Выбор образа (Ubuntu, CentOS, Fedora и др.)
3. Выбор конфигурации CPU/RAM
4. Выбор длительности аренды (1 день — 1 месяц)
5. Ввод имени инстанса
6. Бот создаёт дроплет и сохраняет в БД

### Управление инстансами
- Кнопка «Управление инстансами» в меню /start
- Просмотр своих активных дроплетов
- Продление (3 или 7 дней) и удаление через inline-кнопки

### Автоматическое управление сроком
- За 24 часа до истечения — уведомление владельцу с кнопками продления/удаления
- По истечении срока — автоматическое удаление дроплета

### Уведомления в канал
При установленном `NOTIFICATION_CHANNEL_ID` бот отправляет уведомления о:
- Создании нового инстанса
- Продлении срока
- Удалении (ручном и автоматическом)
