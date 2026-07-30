# Сайт-заглушка старого кабинета (cabinet.stable-vpn.net)

Статичная страница в старом дизайне: старое лого-щит сверху, по центру «Stable VPN
переехал — теперь мы Bulka VPN», новое лого и кнопки на новый сайт/бота.

## Подготовка

Уже настроено: новый сайт `https://cabinet.bulkavpn.net`, бот `https://t.me/vpnbulka_bot`,
лого `stable_logo.png` (старое) и `bulka_logo.png` (новое Bulka VPN) лежат рядом с `index.html`.
Если адреса поменяются — правь ссылки в конце `index.html`.

## ⚠️ Важно: держать заглушку ВНЕ `dist` кабинета

Раньше заглушка лежала в `/opt/bedolaga-cabinet/dist/stub-page/`. Это ошибка: `npm run build`
(vite) при пересборке **очищает `dist/` целиком** и удаляет заглушку → `cabinet.stable-vpn.net`
отдаёт 404. Поэтому заглушку отдаём напрямую из этой папки репозитория (`/opt/bulka-bot/stub-site`),
которая обновляется через `git pull` и никогда не затрагивается сборкой кабинета.

## Отдача через Caddy (финальная схема)

Заглушка живёт в `/opt/bulka-bot/stub-site` (этот каталог). Монтируем его в контейнер Caddy
и отдаём как статику.

1. В `docker-compose.yml` Caddy (`/opt/caddy/docker-compose.yml`) добавить том (ro):

   ```yaml
   volumes:
     - /opt/bulka-bot/stub-site:/srv/stub-site:ro
   ```

2. В `Caddyfile` (`/opt/caddy/Caddyfile`) блок старого кабинета:

   ```
   cabinet.stable-vpn.net {
       root * /srv/stub-site
       try_files {path} /index.html
       file_server
   }
   ```

3. Применить:

   ```bash
   cd /opt/caddy
   docker compose up -d            # подхватит новый том
   docker exec <caddy> caddy validate --config /etc/caddy/Caddyfile
   docker exec <caddy> caddy reload --config /etc/caddy/Caddyfile
   ```

После этого обновление заглушки = `git pull` в `/opt/bulka-bot` (файлы в `stub-site/`
обновятся, Caddy отдаёт их напрямую). `npm run build` кабинета заглушку больше не трогает.

> Новый `cabinet.bulkavpn.net` настраивается отдельно на основной (новый) кабинет.
> Старый домен держим на заглушке столько, сколько нужно.
