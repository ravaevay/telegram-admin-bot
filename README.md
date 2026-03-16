# Admin Bot (Telegram + Mattermost)

Этот бот предназначен для управления почтовыми ящиками, виртуальными машинами (Droplets) и кластерами Kubernetes в DigitalOcean. Поддерживаются две платформы — **Telegram** и **Mattermost** (оба бота работают одновременно, используя общую базу данных и бэкенд-модули). Он позволяет:
- ✅ Создавать и сбрасывать пароли почтовых ящиков
- ✅ Создавать инстансы в DigitalOcean на определенный срок
- ✅ Автоматически создавать DNS A-записи для новых инстансов
- ✅ Использовать FQDN как имя дроплета при выборе DNS
- ✅ Отображать стоимость инстансов при выборе типа и срока аренды
- ✅ Показывать потраченные средства для каждого инстанса в управлении
- ✅ Добавлять тег с Telegram-никнеймом создателя на дроплет
- ✅ Создавать снэпшот перед автоматическим удалением по истечении срока
- ✅ Продлевать или удалять инстансы (с автоматической очисткой DNS)
- ✅ Запоминать предпочтения SSH-ключей и показывать часто используемые первыми
- ✅ Показывать читаемые имена пользователей (@username) в уведомлениях
- ✅ Работать в группах, предоставляя доступ всем участникам
- ✅ Создавать управляемые K8s кластеры (DOKS) на определённый срок
- ✅ Отслеживать статус создания кластера и уведомлять, когда кластер готов
- ✅ Продлевать или удалять K8s кластеры из интерфейса бота

---

## 📂 **Структура проекта**
```
telegram-admin-bot/
│── .env                  # Файл с переменными окружения
│── CLAUDE.md              # Инструкции для Claude Code
│── docker-compose.yml     # Файл для запуска через Docker (Telegram + Mattermost)
│── Dockerfile             # Конфигурация контейнера
│── requirements.txt       # Python-зависимости
│── pyproject.toml         # Конфигурация Ruff (линтер/форматтер)
│── README.md              # Документация проекта
│
├── .github/workflows/
│   └── ci.yml             # GitHub Actions: lint, test, Docker build
│
├── src/
│   ├── bot.py             # Основной файл Telegram-бота
│   ├── mattermost_bot.py  # Основной файл Mattermost-бота
│   ├── config.py          # Конфигурация проекта (загрузка .env)
│   └── modules/           # Логика бота вынесена в модули
│       ├── database.py            # Работа с БД (SQLite): дроплеты, K8s, SSH-ключи, platform
│       ├── authorization.py       # Проверка прав доступа (Telegram + Mattermost)
│       ├── mail.py                # Управление почтовыми ящиками
│       ├── notifications.py       # Уведомления в Telegram-канал (дроплеты и K8s)
│       ├── mm_notifications.py    # Уведомления в Mattermost-канал (дроплеты и K8s)
│       ├── mm_conversation.py     # Менеджер состояний для Mattermost-бота
│       ├── create_test_instance.py  # Управление дроплетами DigitalOcean (API, DNS, цены)
│       └── create_k8s_cluster.py    # Управление K8s кластерами DOKS (API, retry, кэш)
│
├── tests/
│   ├── conftest.py                   # Фикстуры pytest
│   ├── test_mail.py                  # Тесты модуля mail
│   ├── test_database.py              # Тесты модуля database (дроплеты)
│   ├── test_database_k8s.py          # Тесты модуля database (K8s кластеры)
│   ├── test_database_platform.py     # Тесты колонки platform
│   ├── test_notifications.py         # Тесты модуля notifications (Telegram)
│   ├── test_mm_notifications.py      # Тесты модуля mm_notifications (Mattermost)
│   ├── test_mm_conversation.py       # Тесты ConversationManager
│   ├── test_authorization.py         # Тесты модуля authorization
│   ├── test_create_test_instance.py  # Тесты модуля create_test_instance
│   └── test_create_k8s_cluster.py    # Тесты модуля create_k8s_cluster
│
└── instances.db           # База данных SQLite (создаётся автоматически)
```
---

## **Переменные окружения**

Создайте файл `.env` и добавьте в него переменные:
```ini
# Telegram API
BOT_TOKEN=your-telegram-bot-token

# SSH конфигурация
SSH_HOST=your-ssh-server
SSH_PORT=22
SSH_USERNAME=root
SSH_KEY_PATH=/path/to/private/key

# DigitalOcean API
DIGITALOCEAN_TOKEN=your-do-api-token

# Авторизация (Telegram user IDs через запятую)
AUTHORIZED_MAIL_USERS=123456789,987654321
AUTHORIZED_DROPLET_USERS=123456789
AUTHORIZED_K8S_USERS=123456789

# Настройки почтового сервера
MAIL_DB_USER=root
MAIL_DB_PASSWORD=my-secret-password
MAIL_DEFAULT_DOMAIN=example.com

# Опционально
NOTIFICATION_CHANNEL_ID=-100123456789
DB_PATH=./instances.db

# Mattermost (опционально — для Mattermost-бота)
MM_BOT_TOKEN=your-mattermost-bot-token
MM_SERVER_URL=https://mm.example.com
MM_WEBHOOK_PORT=8065
MM_WEBHOOK_HOST=localhost
MM_AUTHORIZED_MAIL_USERS=mm-user-id-1,mm-user-id-2
MM_AUTHORIZED_DROPLET_USERS=mm-user-id-1
MM_AUTHORIZED_K8S_USERS=mm-user-id-1
MM_NOTIFICATION_CHANNEL_ID=mm-channel-id
```



## 🚀 **Установка и запуск**
### **1️⃣ Установка зависимостей**
Если запускаете **локально**, установите зависимости:
```bash
pip install -r requirements.txt

# Telegram-бот
python src/bot.py

# Mattermost-бот (опционально, требуется MM_BOT_TOKEN и MM_SERVER_URL)
python src/mattermost_bot.py
```
Запуск через Docker (оба бота одновременно):
```bash
docker-compose up --build -d
```

### **2️⃣ Линтинг и тесты**
```bash
# Проверка стиля кода (Ruff)
ruff check src/
ruff format --check src/

# Запуск тестов
pip install pytest pytest-asyncio
pytest tests/ -v
```

### **3️⃣ CI/CD**
GitHub Actions (`.github/workflows/ci.yml`) автоматически запускает:
- **lint** + **test** — на push/PR в `main`
- **Docker build & push** — на push в `main` и теги `v*.*` / `v*.*.*`

Для Docker push необходимо настроить секреты в репозитории (Settings > Secrets):
- `DOCKERHUB_USERNAME` — логин DockerHub
- `DOCKERHUB_TOKEN` — токен доступа DockerHub

## 📬 **Как работает бот?**
1.	Создание почтового ящика
	-	Бот запрашивает имя ящика (без домена)
	-	Генерирует случайный пароль
	-	Создаёт ящик на почтовом сервере
	-	Выдаёт пользователю настройки для подключения
2.	Создание инстанса в DigitalOcean
	-	Бот запрашивает SSH-ключи пользователя (мультивыбор)
	-	Часто используемые ключи показываются первыми и автоматически предвыбираются (на основе истории)
	-	Предлагает выбрать образ (Ubuntu, CentOS, Fedora)
	-	Предлагает выбрать DNS-зону и ввести субдомен (или пропустить)
	-	При выборе DNS — FQDN автоматически становится именем дроплета
	-	Показывает выбор конфигурации CPU/RAM с ценами ($/мес)
	-	Показывает длительность аренды с расчётом стоимости (~$X.XX)
	-	Создаёт инстанс с тегом `creator:<telegram_nickname>`, DNS A-запись и сохраняет всё в базе данных
	-	Отправляет уведомление в канал с @username создателя, DNS-именем и стоимостью
3.	Управление инстансами
	-	Отображение всех инстансов пользователя с потраченными средствами (~$X.XX)
	-	Бот отправляет уведомление за 24 часа до удаления
	-	Пользователь может продлить срок аренды или удалить инстанс сразу
	-	При удалении DNS-запись автоматически удаляется из DigitalOcean
	-	Если пользователь ничего не делает — создаётся снэпшот, затем инстанс удаляется автоматически (DNS тоже)
4.	Создание K8s кластера (DOKS)
	-	Бот предлагает выбрать версию Kubernetes (последняя выделяется по умолчанию)
	-	Предлагает выбрать тип узла с ценой ($/мес), количество узлов (1/2/3), срок аренды
	-	После ввода имени кластер создаётся немедленно со статусом `provisioning`
	-	Бот уведомляет создателя и канал о начале создания
	-	Фоновая задача (каждые 12ч) опрашивает статус и уведомляет, когда кластер готов (`running`)
5.	Управление K8s кластерами
	-	Отображение всех активных кластеров пользователя со статусом и потраченными средствами
	-	Бот отправляет уведомление за 24 часа до удаления
	-	Пользователь может продлить срок аренды или удалить кластер сразу
	-	Снэпшот перед удалением не создаётся (DOKS не поддерживает эту операцию)

## 📌 **Дополнительная информация**
-	Поддерживаемые образы DigitalOcean: Ubuntu, CentOS, Fedora
-	Подключение к почте: IMAP (143, STARTTLS), SMTP (587, STARTTLS)
-	База данных: SQLite (instances.db — таблицы `instances`, `k8s_clusters`, `ssh_key_usage`; WAL-режим для параллельного доступа; колонка `platform` для разделения ресурсов между Telegram и Mattermost)
-	DNS: автоматическое создание/удаление A-записей через DigitalOcean DNS API (только для дроплетов)
-	Цены дроплетов: подгружаются из DO API `/v2/sizes` с кэшированием на 1 час
-	Цены K8s узлов: подгружаются из DO API `/v2/kubernetes/options` с кэшированием на 1 час
-	Стоимость: трекинг потраченных средств на основе `created_at` и `price_hourly`
-	Теги: каждый дроплет и K8s кластер получает тег `createdby:telegram-admin-bot` в DigitalOcean
-	Снэпшоты: автоматическое создание перед удалением дроплета по истечении срока (таймаут 600с); для K8s кластеров снэпшоты не поддерживаются
-	Уведомления: в канал отправляются с @username вместо числового ID; поддерживаются события дроплетов и K8s кластеров
-	K8s кластеры: регион `fra1`, поддерживается HA-режим (через API)
