# MORAL SQUAD

Статический лендинг [moralsqd.ru](https://moralsqd.ru).

## Локальный запуск

```bash
python3 -m http.server 8080
```

После запуска сайт будет доступен по адресу [http://localhost:8080](http://localhost:8080).

## Структура

- `index.html` — разметка и стили лендинга.
- `support.js` — runtime для рендеринга страницы.
- `favicon.png` — иконка вкладки браузера.
- `*.jpg`, `*.png` — изображения участников и декоративные элементы.

## Публикация

Продакшен обслуживается nginx из каталога `/var/www/moralsqd`.
