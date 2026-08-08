# MORAL SQUAD

Лендинг [moralsqd.ru](https://moralsqd.ru) с небольшим API для общей петиции.

## Локальный запуск

```bash
PETITION_SECRET=local-secret python3 petition_api.py --port 8080
```

После запуска сайт и API будут доступны по адресу [http://localhost:8080](http://localhost:8080).

API хранит подписи в локальной SQLite-базе `petition.db`. Один браузер получает
подписанный `HttpOnly`-идентификатор и может оставить одну подпись. Повторную
выдачу идентификаторов после очистки cookie ограничивает дневной лимит по
хешированному сочетанию подсети и User-Agent; сырые IP-адреса не сохраняются.

## Структура

- `index.html` — разметка и стили лендинга.
- `petition_api.py` — API петиции и локальный сервер статики.
- `test_petition_api.py` — интеграционные тесты API.
- `deploy/` — unit-файл systemd и nginx location для продакшена.
- `support.js` — runtime для рендеринга страницы.
- `favicon.png` — иконка вкладки браузера.
- `*.jpg`, `*.png` — изображения участников и декоративные элементы.

## Публикация

Продакшен обслуживается nginx из каталога `/var/www/moralsqd`. API запускается
отдельным systemd-сервисом на `127.0.0.1:8787`, а nginx проксирует только
`/api/petition`. Секрет и путь к базе задаются в `/etc/moralsqd-petition.env`:

```dotenv
PETITION_SECRET=replace-with-a-long-random-value
PETITION_DB_PATH=/var/lib/moralsqd/petition.db
PETITION_STATIC_DIR=/var/www/moralsqd
PETITION_SECURE_COOKIE=1
```

Перед установкой сервиса каталог `/var/lib/moralsqd` должен принадлежать
`www-data:www-data` и иметь права `750`.
