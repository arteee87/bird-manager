# План сборки под Alpine Linux

> **Статус: план, ничего не реализовано.** Документ описывает оценку и способ
> портирования упаковки на Alpine. Он не описывает текущее состояние проекта и
> устареет, как только работа начнётся. Составлен при версии 0.7.6.

## Главный вывод

Код приложения переносить не нужно — трогать придётся только слой упаковки.

Ни в `app/` нет Debian-специфики, ни в maintainer-скриптах нет bashism: все три
скрипта уже на `#!/bin/sh`, и поиск `[[`, `source`, `local`, `<<<` пуст.
Портируются не логика, а конкретные вызовы утилит.

## Что проверено на текущем коде

- Синтаксиса и stdlib-API новее 3.11 в `app/` нет: `match`/`case`, `Self`,
  `StrEnum`, `itertools.batched`, `tomllib` не встречаются.
- Единственный API эпохи 3.12 — `tarfile.extractall(filter='data')` в
  `app/backup.py:425`. Он бэкпортирован в 3.11.4/3.10.12, на текущем Alpine
  есть.
- Пути из `.env` на Alpine идентичны: `/usr/sbin/bird`, `birdc`, `/etc/bird`,
  `/usr/bin/python3`.
- Единственное место с дистрибутивно-зависимым путём — дефолт control-сокета
  `/run/bird/bird.ctl` в `app/config_manager.py:1262`; переопределяется через
  `BIRD_SOCKET`.
- Три теста жёстко ссылаются на `packaging/debian/postinst`:
  `tests/test_api.py:1097`, `:1540`, `:3612`.
- `publish-repo.sh` лежит вне репозитория, в `/usr/local/bin/`, и чисто
  apt-овый: `apt-ftparchive`, `dists/`, `Release`, подпись GPG-ключом
  `3207AA5B920A5419`.
- Побочная находка: `requests>=2.34` в `requirements.txt` не импортируется
  нигде и не значится в `Depends`. Мёртвая запись.
- Docker на сборочном хосте доступен (`/usr/bin/docker` + `/var/run/docker.sock`).

## Принятые решения

| Вопрос | Решение |
| --- | --- |
| Стратегия сборки | Отдельный `scripts/build-apk.sh` + `packaging/alpine/`. Рабочий deb-путь не трогаем. Дублирование staging-логики принято осознанно |
| Права и группа bird | Полный эквивалент: группа `bird`, `chown bird-manager:bird`, reload через `su-exec` |
| debconf-вопросы | Авто-ответ без вопросов: include не добавляем, purge только через `apk --purge` |
| Проверка | docker-контейнер Alpine с реальной установкой и проверкой прав |

## Карта переноса: пользователь, группа bird, права

Это ядро задачи, здесь больше всего расхождений.

| Сейчас (Debian) | Alpine | Почему |
| --- | --- | --- |
| `getent group bird` | `grep -q '^bird:' /etc/group` | busybox не содержит `getent` |
| `addgroup --system bird` | `addgroup -S bird` | флаг `-S` у busybox; создавать только если группы нет |
| `addgroup --system bird-manager` | `addgroup -S bird-manager` | то же |
| `adduser --system --ingroup … --shell /usr/sbin/nologin` | `adduser -S -D -H -h /nonexistent -s /sbin/nologin -G bird-manager` | busybox `adduser`; путь `nologin` другой — `/sbin/`, а не `/usr/sbin/` |
| `usermod -a -G bird bird-manager` | `addgroup bird-manager bird` | `usermod` в busybox отсутствует. Это и есть тот самый шаг «пользователь в группе bird» |
| `chown -R bird-manager:bird /etc/bird/live` + `2750` на каталоги, `0640` на файлы | без изменений | busybox `chown`/`chmod`/`find -exec` годятся; охват остаётся строго на live-каталоге |
| `runuser -u bird-manager -- birdc configure` | `su-exec bird-manager -c 'birdc configure'` | `runuser` в Alpine нет; `su-exec` даёт чистый exit-код, в отличие от `su` |
| `systemctl daemon-reload/enable/restart` | `rc-update add … default`, `rc-service … restart` | OpenRC |
| `Wants=bird.service` / `After=bird.service` | `depend() { need net; after bird; }` + зависимость `bird-openrc` | в Alpine `bird` — только демон, init-скрипты лежат в отдельном пакете `bird-openrc` |
| 2 вопроса debconf | авто-ответ: include не добавляем, purge только через `apk --purge` | debconf в Alpine не существует |
| `Depends: adduser, debconf, passwd` | убрать | всё это даёт busybox |
| `python3-waitress` / `-sqlalchemy` / `-dotenv` | `py3-waitress`, `py3-sqlalchemy`, `py3-dotenv` | схема имён Alpine |
| `bird3 \| bird2 \| bird` | `bird` + `bird-openrc` | оба в **community**, клиенту нужен включённый `community` |
| `Installed-Size` | поле `size` в `.PKGINFO` | формат apk |
| список `conffiles` | ничего | apk считает `/etc` конфигом сам |
| `groff -man` lint | `mandoc -T lint` | в Alpine нет groff |

## Потеря systemd-hardening

Отдельным пунктом, чтобы не потеряться молча: блок в
`packaging/bird-manager.service` — `ProtectSystem=strict`, `ReadWritePaths`,
`ReadOnlyPaths`, `NoNewPrivileges`, `PrivateTmp`, `RestrictAddressFamilies`,
`CapabilityBoundingSet=`, `RestrictSUIDSGID`, `LockPersonality` — в OpenRC не
воспроизводим в принципе.

Что остаётся: запуск от непривилегированного пользователя и `UMask 0027` через
`export`. Это осознанная дельта: её надо записать в `packaging/alpine/README`
и в man page, а не убрать тихо.

## Файлы

### Новые

- `scripts/build-apk.sh` — по образцу `build-deb.sh`: staging-дерево,
  `@@VERSION@@`/`@@SIZE@@`, проверка `apk`, вывод метаданных, флаги
  `--install`, `--no-publish`.
- `packaging/alpine/PKGINFO` — шаблон `.PKGINFO`.
- `packaging/alpine/bird-manager.initd` — OpenRC-сервис.
- `packaging/alpine/bird-manager.post-install`,
  `.pre-deinstall`, `.post-deinstall` — порты `postinst`/`prerm`/`postrm`.
  Самая тяжёлая часть: `postrm` — около 250 строк, порт механически повторяет
  валидацию `bird -p` и `birdc configure`.
- `packaging/alpine/README` — аналог `README.Debian`.

### Изменяемые

- `tests/test_api.py` — в три списка файлов (строки 1097, 1540, 3612) добавить
  alpine-скрипты, чтобы ключи `BIRD_TRUSTED_*`, `APP_HOST=`, комментарий про
  идентификатор пользователя и запрет возврата удалённого состояния
  проверялись для обеих дистрибутивов.
- `scripts/publish-repo.sh` (вне репозитория, `/usr/local/bin/`) — apk-ветка:
  `APKINDEX.tar.gz` + `.sig`. Ключ apt — GPG, ключ apk — RSA через
  `abuild-sign`, то есть нужен отдельный ключ. Дерево `/srv/repo` смешивать
  нельзя: `apt-ftparchive packages pool` сканирует `pool/`, поэтому отдельный
  корень, например `/srv/repo-alpine/<branch>/x86_64`.
- `docs/build.md` — раздел про apk-таргет; заодно снять устаревшее «147
  тестов» (фактически 156).
- `README.md` — упомянуть Alpine.
- `requirements.txt` — убрать мёртвый `requests`.

### Про дублирование `.env`

Текст с семью ключами `BIRD_TRUSTED_*` сейчас живёт в `.env`,
`scripts/build-deb.sh` и `packaging/debian/postinst`. Поскольку выбран
отдельный скрипт, он продублируется ещё в `build-apk.sh` и `.post-install` —
это 5 копий.

Существующие тесты как раз и существуют для этого: расширив списки файлов,
получим гарантию, что дрейф будет ловиться тестом. Альтернатива — вынести
шаблон в общий `packaging/bird-manager.env`, но это потребует править
существующие ассерты; держится как отдельная задача «после», а не в этом
заходе.

## Проверка в docker

1. `sh -n` на всех apk-скриптах.
2. Контейнер `alpine:3.23`:
   `apk add python3 py3-waitress py3-sqlalchemy py3-dotenv bird bird-openrc openrc su-exec`.
3. Прогнать pytest внутри контейнера — это и есть настоящее доказательство
   переносимости кода, а не только упаковки.
4. Установить `.apk` через `apk add --allow-untrusted --no-cache`.
5. Ассерты:
   - `id bird-manager` показывает группу `bird`;
   - `/etc/bird/live` — владелец `bird-manager:bird`, каталоги `2750`,
     файлы `0640`;
   - `rc-service bird-manager start`, затем health по curl;
   - `apk audit --system`.
6. `apk del bird-manager` — убедиться, что `.env` и база остаются (аналог
   поведения при отсутствии purge).

## Открытые пункты — проверить в контейнере до написания кода

1. **Создаёт ли `bird`/`bird-openrc` группу `bird` и от какого пользователя он
   запускается.** Если в Alpine bird работает от root и группы `bird` нет
   вовсе, то модель владения `bird-manager:bird` окажется неверной и
   правильным решением будет `bird-manager:bird-manager` плюс доступ к
   сокету. Это меняет и `chown`, и `su-exec`, поэтому проверка идёт первым
   шагом.
2. **Фактический путь control-сокета** Alpine-bird против дефолта
   `/run/bird/bird.ctl` в `app/config_manager.py:1262`.
3. Нужно ли документировать требование включить `community` в инструкции для
   клиента — да, поскольку `bird` и `bird-openrc` живут именно там.

## Порядок работ

1. Проверить открытые пункты 1–2 в контейнере `alpine`.
2. `packaging/alpine/*` — initd и скрипты, порт блока прав.
3. `scripts/build-apk.sh`.
4. Тесты: расширить три списка, прогнать полный набор на dev.
5. Полная проверка в контейнере по списку выше.
6. `publish-repo.sh` + новый ключ apk, публикация в `/srv/repo-alpine`.
7. Документация и релиз — отдельным шагом, не в этом заходе.

Оценка объёма: около 600–700 строк нового кода, из них подавляющая часть —
механический порт `postrm`.