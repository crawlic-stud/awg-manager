# awg-manager

Минимальный Python-клиент и простой web dashboard для AmneziaWG.

Что есть:

- SSH-подключение к серверу с AmneziaWG
- список текущих пользователей
- `vpn://`-ключ для каждого пользователя
- SQLite-таблицы для серверов, email и `vpn_link`
- простой dashboard
- импорт CSV `name,email` для массового создания клиентов
- отдельная панель для компоновки письма и отправки выбранным пользователям
- переключение между несколькими серверами из дашборда

Запуск web UI через Docker:

```bash
cp .env.example .env
docker compose up --build
```

Открой `http://localhost:8000`.

Для production-запуска через Nginx используй отдельный compose-файл:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

В production-пресете приложение запускается через Gunicorn и доступно только на `127.0.0.1:8888`, чтобы внешний трафик принимал Nginx. В этом режиме автоматически включаются Secure-cookie и доверие к заголовкам `X-Forwarded-*` от Nginx.

Nginx должен передавать приложению схему запроса:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

Для входа используется пароль из `AUTH_PASSWORD` в `.env`. После успешного входа авторизация хранится в cookie-сессии 24 часа. Все маршруты dashboard требуют авторизации.

Если нужно проверить только CLI:

```bash
pip install -r requirements.txt
python wg_manager.py --host 1.2.3.4 --user root check
python wg_manager.py --host 1.2.3.4 --user root list
python wg_manager.py --host 1.2.3.4 --user root add alice
python wg_manager.py --host 1.2.3.4 --user root vpn alice
```

`VPN_ENDPOINT_HOST`, `SERVER_NAME`, `SSH_USER`, `SSH_PASSWORD`, `SSH_PORT` и `DB_PATH` используются как bootstrap для первого сервера.
Дальше серверы живут в таблице `servers` внутри SQLite.

Для отправки писем добавь:

- `RESEND_API_KEY`
- `EMAIL` — адрес отправителя

В шаблоне письма доступны переменные `name`, `email`, `vpn_link`, `server_name`.
