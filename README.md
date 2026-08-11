# MORAL SQUAD

Лендинг [moralsqd.ru](https://moralsqd.ru) с API петиции и страницей стрим-аукционов
[`/auc/`](https://moralsqd.ru/auc/).

Аукцион не принимает платежи. Модератор только учитывает внешние донаты;
отмена аукциона или ошибочной записи не возвращает деньги в донат-сервисе.

## Локальный запуск

### Петиция

```bash
PETITION_SECRET=local-secret python3 petition_api.py --port 8080
```

После запуска сайт и API будут доступны по адресу [http://localhost:8080](http://localhost:8080).

API хранит подписи в локальной SQLite-базе `petition.db`. Один браузер получает
подписанный `HttpOnly`-идентификатор и может оставить одну подпись. Повторную
выдачу идентификаторов после очистки cookie ограничивает дневной лимит по
хешированному сочетанию подсети и User-Agent; сырые IP-адреса не сохраняются.

### Аукционы

Для локальной разработки допустим отдельный plaintext-пароль. Не используйте его в production.

```bash
export AUCTION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export AUCTION_ADMIN_PASSWORD='local-admin-password'
export AUCTION_DB_PATH="$PWD/auction.db"
export AUCTION_STATIC_DIR="$PWD"
export AUCTION_SECURE_COOKIE=0
python3 auction_api.py --host 127.0.0.1 --port 8788
```

Страница будет доступна по адресу [http://127.0.0.1:8788/auc](http://127.0.0.1:8788/auc), API — по
`http://127.0.0.1:8788/api/auc/`. База и секрет этого сервиса не должны совпадать с петицией.

Commit/reveal позволяет проверить, что опубликованный при старте seed не заменили, снимок ставок
не изменили после закрытия, а сохранённый победитель следует описанному алгоритму. Это не защищает
от оператора, который знает seed и намеренно выбирает момент досрочного закрытия; для такой модели
угроз на следующем этапе нужен независимый внешний источник энтропии.

### Предложения видео для DK

Сервис использует официальный YouTube Data API для проверки ссылки, названия, даты публикации и
длительности. API-ключ вводится в терминале без попадания в shell history:

```bash
export VIDEO_SUGGESTIONS_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export VIDEO_SUGGESTIONS_ADMIN_USERNAME='dk'
export VIDEO_SUGGESTIONS_ADMIN_PASSWORD='local-dk-password'
export VIDEO_SUGGESTIONS_DB_PATH="$PWD/video-suggestions.db"
export VIDEO_SUGGESTIONS_STATIC_DIR="$PWD"
export VIDEO_SUGGESTIONS_SECURE_COOKIE=0
export VIDEO_SUGGESTIONS_ALLOWED_ORIGIN='http://127.0.0.1:8790'
read -r -s VIDEO_SUGGESTIONS_YOUTUBE_API_KEY
export VIDEO_SUGGESTIONS_YOUTUBE_API_KEY
python3 video_suggestions_api.py --host 127.0.0.1 --port 8790
```

Главная будет доступна по адресу [http://127.0.0.1:8790](http://127.0.0.1:8790), скрытый кабинет —
по [http://127.0.0.1:8790/dk-video-inbox/](http://127.0.0.1:8790/dk-video-inbox/). Plaintext-пароль
разрешён только при локальном запуске без Secure-cookie. В production сервис требует scrypt-хеш.

Один подписанный анонимный браузерный идентификатор может добавить ровно одну просьбу за каждый
YouTube Video ID. Другой ролик предложить можно. В базе нет сырых IP-адресов; HMAC-хеш подсети
используется только для антиспама. Метаданные YouTube обновляются до 30-дневного предела хранения,
а просроченные значения удаляются.

## Структура

- `index.html` — разметка и стили лендинга.
- `auc/` — публичная и административная страница аукциона.
- `auc_plan.md` — архитектурный план и модель угроз аукциона.
- `auction_api.py` — серверное состояние, авторизация, ставки и выбор победителя.
- `test_auction_api.py` — тесты доменной логики и HTTP API аукциона.
- `video_suggestions_api.py` — предложения YouTube-видео, анонимная дедупликация и кабинет DK.
- `test_video_suggestions_api.py` — доменные и HTTP-тесты предложений видео.
- `video-suggestions.js` — публичное модальное окно отправки ссылки.
- `dk-video-inbox/` — отдельный вход в кабинет «блокнотик мелстроя» и таблица видео.
- `privacy/` — правила хранения анонимных данных и YouTube-метаданных.
- `robots.txt` и `sitemap.xml` — правила обхода и список канонических индексируемых страниц.
- `site.webmanifest`, `icon-*.png`, `apple-touch-icon.png` — название сайта и иконки платформ.
- `og-moral-squad.png` — социальная карточка 1200×630 для Open Graph и Twitter Card.
- `google*.html` и `yandex_*.html` — публичные файлы подтверждения владения сайтом.
- `petition_api.py` — API петиции и локальный сервер статики.
- `test_petition_api.py` — интеграционные тесты API.
- `test_seo.py` — контракт брендовых метаданных, JSON-LD, sitemap, robots и размеров изображений.
- `deploy/` — unit-файл systemd и nginx location для продакшена.
- `deploy/nginx-seo.conf` — канонические редиректы и content types/cache для SEO-файлов.
- `support.js` — прежний generated runtime; статическая главная больше его не загружает.
- `favicon.png` — иконка вкладки браузера.
- `*.jpg`, `*.png` — изображения участников и декоративные элементы.

## Публикация петиции

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

## SEO и индексация

Канонический адрес сайта — `https://moralsqd.ru/`. Главная и `/privacy/` индексируются и входят в
`sitemap.xml`; динамический `/auc/` и закрытый `/dk-video-inbox/` имеют `noindex`. Прямые дубли
`/index.html`, `/auc`, `/auc/index.html` и `/privacy/index.html` перенаправляются на канонические URL.

Перед публикацией выполните проверки:

```bash
python3 -m unittest -v test_seo.py
python3 -m unittest discover -v
xmllint --noout sitemap.xml
python3 -m json.tool site.webmanifest >/dev/null
git diff --check
```

SEO-файлы устанавливаются в `/var/www/moralsqd`, а nginx-сниппеты — после резервной копии:

```bash
sudo install -o root -g root -m 0644 robots.txt /var/www/moralsqd/robots.txt
sudo install -o root -g root -m 0644 sitemap.xml /var/www/moralsqd/sitemap.xml
sudo install -o root -g root -m 0644 site.webmanifest /var/www/moralsqd/site.webmanifest
sudo install -o root -g root -m 0644 og-moral-squad.png /var/www/moralsqd/og-moral-squad.png
sudo install -o root -g root -m 0644 icon-192.png /var/www/moralsqd/icon-192.png
sudo install -o root -g root -m 0644 icon-512.png /var/www/moralsqd/icon-512.png
sudo install -o root -g root -m 0644 apple-touch-icon.png /var/www/moralsqd/apple-touch-icon.png
sudo install -o root -g root -m 0644 google69b756812808de0a.html /var/www/moralsqd/google69b756812808de0a.html
sudo install -o root -g root -m 0644 yandex_c4f49ad75f584106.html /var/www/moralsqd/yandex_c4f49ad75f584106.html
sudo install -o root -g root -m 0644 deploy/nginx-seo.conf /etc/nginx/snippets/moralsqd-seo.conf
sudo nginx -t
sudo systemctl reload nginx
```

В HTTPS-блоке `moralsqd.ru` должен быть подключён
`include /etc/nginx/snippets/moralsqd-seo.conf;`. После публикации отправьте
`https://moralsqd.ru/sitemap.xml` в Google Search Console и Яндекс Вебмастер и запросите переобход
главной. Verification-файлы публичны по своей природе и хранятся в репозитории вместе со статикой.

## Production-установка аукционов

Аукционы работают отдельно от петиции: Python-сервис слушает только `127.0.0.1:8788`,
SQLite-база лежит в `/var/lib/moralsqd-auction/auction.db`, а nginx отдает статику и проксирует `/api/auc/`.

### 1. Каталог данных и секреты

```bash
sudo install -d -o root -g root -m 0755 /opt/moralsqd
sudo adduser --system --group --home /nonexistent --no-create-home moralsqd-auction
sudo install -d -o moralsqd-auction -g moralsqd-auction -m 0700 /var/lib/moralsqd-auction

sudo touch /etc/moralsqd-auction.env
sudo chown root:root /etc/moralsqd-auction.env
sudo chmod 0600 /etc/moralsqd-auction.env
sudoedit /etc/moralsqd-auction.env
```

Отдельный пользователь и каталог `moralsqd-auction` не дают nginx (`www-data`) или сервису
петиции открыть, удалить либо подменить базу аукциона. `UMask=0077` оставляет саму БД и WAL
доступными только их владельцу. Команда создания пользователя выполняется один раз; при повторной
установке сначала проверьте его через `getent passwd moralsqd-auction`.

Пример production-окружения:

```dotenv
AUCTION_SECRET=replace-with-at-least-32-random-bytes
AUCTION_ADMIN_PASSWORD_HASH=replace-with-a-generated-scrypt-hash
AUCTION_DB_PATH=/var/lib/moralsqd-auction/auction.db
AUCTION_STATIC_DIR=/var/www/moralsqd
AUCTION_SECURE_COOKIE=1
AUCTION_HOST=127.0.0.1
AUCTION_PORT=8788
```

Секрет можно сгенерировать командой `python3 -c 'import secrets;
print(secrets.token_urlsafe(48))'`. Хеш пароля генерируется интерактивно, без попадания
пароля в shell history: `python3 auction_api.py --hash-password`. В production сервис должен запускаться только с хешем пароля;
`AUCTION_ADMIN_PASSWORD` допустим только для локальной разработки. Не коммитьте env-файл и не совмещайте
`AUCTION_SECRET` с `PETITION_SECRET`.

### 2. Корректный backup SQLite

Не копируйте работающий `auction.db` обычным `cp`: часть зафиксированных данных может находиться
в WAL-файле. Перед обновлением создайте consistent snapshot через SQLite Backup API:

```bash
release="$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -o root -g root -m 0750 /var/backups/moralsqd

if sudo test -f /var/lib/moralsqd-auction/auction.db; then
  backup="/var/backups/moralsqd/auction-$release.db"
  sudo python3 - "$backup" <<'PY'
import os
import pathlib
import sqlite3
import sys

source_path = pathlib.Path("/var/lib/moralsqd-auction/auction.db")
backup_path = pathlib.Path(sys.argv[1])
if backup_path.exists():
    raise SystemExit(f"backup already exists: {backup_path}")

os.umask(0o177)
with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as destination:
    source.backup(destination)
    result = destination.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"backup integrity check failed: {result}")
PY
  sudo chmod 0600 "$backup"
fi
```

Восстанавливать backup можно только при остановленном `moralsqd-auction.service`, после проверки
совместимости схемы с возвращаемой версией кода.

### 3. Публикация API и всей папки `/auc`

Сначала положите новый API рядом со старым файлом и атомарно замените его. Всю папку статики также
копируйте через staging-каталог: это не оставит на сервере смесь файлов двух версий.

```bash
release="${release:-$(date -u +%Y%m%dT%H%M%SZ)}"
previous="/var/backups/moralsqd/auc-$release"
sudo install -d -o root -g root -m 0750 /var/backups/moralsqd

if sudo test -e "$previous"; then
  echo "backup target already exists: $previous" >&2
  exit 1
fi

if sudo test -f /opt/moralsqd/auction_api.py; then
  sudo install -o root -g root -m 0600 \
    /opt/moralsqd/auction_api.py "/var/backups/moralsqd/auction_api-$release.py"
fi
if sudo test -f /var/www/moralsqd/index.html; then
  sudo install -o root -g root -m 0644 \
    /var/www/moralsqd/index.html "/var/backups/moralsqd/index.html-$release"
fi
sudo install -o root -g root -m 0644 auction_api.py /opt/moralsqd/.auction_api.py.new
sudo mv /opt/moralsqd/.auction_api.py.new /opt/moralsqd/auction_api.py

stage="$(sudo mktemp -d /var/www/moralsqd/.auc-release.XXXXXX)"
sudo cp -a auc/. "$stage/"
sudo chown -R root:root "$stage"
sudo find "$stage" -type d -exec chmod 0755 {} +
sudo find "$stage" -type f -exec chmod 0644 {} +

if sudo test -e /var/www/moralsqd/auc; then
  sudo mv /var/www/moralsqd/auc "$previous"
fi
sudo mv "$stage" /var/www/moralsqd/auc
```

В репозитории нет универсального deploy-скрипта: активный nginx vhost и точка его include живут вне
репозитория. Автоматически угадывать и перезаписывать его небезопасно.

### 4. systemd и nginx

```bash
sudo install -o root -g root -m 0644 \
  deploy/moralsqd-auction.service /etc/systemd/system/moralsqd-auction.service
sudo install -o root -g root -m 0644 \
  deploy/nginx-auc.conf /etc/nginx/snippets/moralsqd-auc.conf
```

Добавьте один раз внутрь активного HTTPS `server {}` для `moralsqd.ru`:

```nginx
include /etc/nginx/snippets/moralsqd-auc.conf;
```

Затем проверьте конфигурацию до reload и запустите сервис. Выполняйте команды поочередно
и не переходите к reload при ошибке health-check.

```bash
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable moralsqd-auction.service
sudo systemctl restart moralsqd-auction.service
curl --fail --silent --show-error http://127.0.0.1:8788/api/auc/current
sudo systemctl reload nginx
```

`Secure` cookie админа требует HTTPS. Не отключайте `AUCTION_SECURE_COOKIE=1` в production ради обхода ошибки
входа — сначала проверьте TLS, `X-Forwarded-Proto` и cookie в DevTools.

### 5. Внешняя проверка

```bash
test "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  https://moralsqd.ru/auc)" = 308
test "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  https://moralsqd.ru/auc/)" = 200
curl --fail --silent --show-error https://moralsqd.ru/auc/auc.css >/dev/null
curl --fail --silent --show-error https://moralsqd.ru/auc/auc.js >/dev/null
curl --fail --silent --show-error https://moralsqd.ru/api/auc/current
sudo systemctl --no-pager --full status moralsqd-auction.service
```

Только после успешных проверок `/auc` и API атомарно опубликуйте главную страницу со ссылкой
на аукцион и проверьте её снаружи:

```bash
sudo install -o root -g root -m 0644 index.html /var/www/moralsqd/.index.html.new
sudo mv /var/www/moralsqd/.index.html.new /var/www/moralsqd/index.html
curl --fail --silent --show-error https://moralsqd.ru/ | grep -F 'href="/auc/"'
```

После публикации дополнительно проверьте прямой refresh `/auc`, вход и одну тестовую админскую
операцию, мобильную верстку и OBS Browser Source. Для компактной сцены OBS используйте
`https://moralsqd.ru/auc?obs=1`: в ней скрыты админка и второстепенные блоки, а сохранённый
результат остаётся в кадре. Открывайте Browser Source без админской cookie.

## Production-подготовка предложений видео

Этот раздел описывает подготовленные файлы; сама функция не развёрнута. Сервис должен работать
от отдельного системного пользователя `moralsqd-video-suggestions`, слушать только
`127.0.0.1:8790` и иметь единственный writable-каталог
`/var/lib/moralsqd-video-suggestions` с правами `0700`.

Защищённый `/etc/moralsqd-video-suggestions.env` (`root:root`, `0600`) должен содержать:

- отдельный случайный `VIDEO_SUGGESTIONS_SECRET` длиной не менее 32 байт;
- ограниченный по YouTube Data API и IP сервера `VIDEO_SUGGESTIONS_YOUTUBE_API_KEY`;
- `VIDEO_SUGGESTIONS_ADMIN_USERNAME=dk` и только scrypt-значение
  `VIDEO_SUGGESTIONS_ADMIN_PASSWORD_HASH`;
- `VIDEO_SUGGESTIONS_DB_PATH=/var/lib/moralsqd-video-suggestions/video-suggestions.db`;
- `VIDEO_SUGGESTIONS_STATIC_DIR=/var/www/moralsqd`;
- `VIDEO_SUGGESTIONS_SECURE_COOKIE=1`;
- `VIDEO_SUGGESTIONS_ALLOWED_ORIGIN=https://moralsqd.ru`;
- `VIDEO_SUGGESTIONS_HOST=127.0.0.1` и `VIDEO_SUGGESTIONS_PORT=8790`.

Команда `python3 video_suggestions_api.py --generate-admin-credentials` печатает один новый пароль
и его scrypt-хеш. Пароль передаётся DK один раз, а в env сохраняется только строка хеша. Не
записывайте plaintext-пароль или YouTube-ключ в Git, shell history либо deploy-логи.

Подготовлены следующие production-файлы:

- `deploy/moralsqd-video-suggestions.service` — изолированный API;
- `deploy/moralsqd-video-suggestions-maintenance.service` и `.timer` — обновление дважды в сутки и
  удаление просроченных YouTube-метаданных;
- `deploy/nginx-video-suggestions.conf` — точные маршруты API, кабинета и privacy page, лимиты
  методов/тела, security headers и отключённые access-логи для чувствительных маршрутов.

Перед будущей публикацией нужно сделать SQLite Backup API snapshot, атомарно установить backend и
статику, выполнить `nginx -t`, локальный health-check на порту `8790`, включить timer дважды в сутки и
проверить снаружи: согласие/отправку, повтор той же ссылки, вход `dk`, сортировку таблицы, refresh и
logout. Главная страница с кнопкой публикуется последней, после успешной проверки API и кабинета.
