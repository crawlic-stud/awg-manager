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
