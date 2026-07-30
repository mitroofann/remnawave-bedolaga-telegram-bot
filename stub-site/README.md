# Сайт-заглушка старого кабинета (cabinet.stable-vpn.net)

Статичная страница в старом дизайне: старое лого-щит сверху, по центру «Stable VPN
переехал — теперь мы Bulka VPN», новое лого и кнопки на новый сайт/бота.

## Подготовка

Уже настроено: новый сайт `https://cabinet.bulkavpn.net`, бот `https://t.me/vpnbulka_bot`,
лого `stable_logo.png` (старое) и `bulka_logo.png` (новое Bulka VPN) лежат рядом с `index.html`.
Если адреса поменяются — правь ссылки в конце `index.html`.

## Отдача через Caddy

Скопируй папку на сервер (напр. в `/srv/stub-site`) и в Caddyfile замени блок
старого кабинета на отдачу статики:

```
cabinet.stable-vpn.net {
    root * /srv/stub-site
    file_server
}
```

Перезапусти Caddy:
```
docker exec <caddy> caddy validate --config /etc/caddy/Caddyfile
docker exec <caddy> caddy reload --config /etc/caddy/Caddyfile
```

После этого контейнер старого кабинета можно остановить (`docker compose stop <cabinet>`) —
страницу отдаёт сам Caddy.

> Новый `cabinet.bulkavpn.net` настраивается отдельно на основной (новый) кабинет.
> Старый домен держим на заглушке столько, сколько нужно.
