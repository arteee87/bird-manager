/* i18n: lightweight client-side localization (RU/EN). No build step, no deps.
   Dictionaries are embedded below so the module works offline and under CSP. */
(function (global) {
    'use strict';

    const STORAGE_KEY = 'bm.lang';

    // Russian plural rules: one / few / many.
    function ruPlural(n) {
        n = Math.abs(n) % 100;
        if (n >= 5 && n <= 20) return 'many';
        n = n % 10;
        if (n === 1) return 'one';
        if (n >= 2 && n <= 4) return 'few';
        return 'many';
    }

    // English plural rules: one / other.
    function enPlural(n) {
        return (Math.abs(n) === 1) ? 'one' : 'other';
    }

    const DICTS = {
        ru: {
            "app.title": "BIRD Manager — Дашборд",
            "banner.offline": "Нет связи с сервером",
            "btn.refresh": "Обновить активную вкладку",
            "bulk.cancel": "Прервать",
            "bulk.phase": "Разбор: чанк 1 из 1",
            "bulk.title": "Импорт сетей",
            "cidr.dropBtn": "Удалить",
            "cidr.fix": "исправление",
            "cidr.fixBtn": "Исправить",
            "cidr.found": "найдено",
            "cidr.title": "Найдены некорректные CIDR. Исправить или удалить?",
            "common.loading": "Загрузка…",
            "config.apply": "Применить изменения",
            "config.diffAria": "staged — построчное сравнение с live",
            "config.hint": "staged-файлы публикуются в live только после «Применить изменения»; сети, сообщества и BGP-пиры управляются базой данных на вкладке «Сети».",
            "config.otherFiles": "Остальные файлы — live | staged",
            "config.placeholder": "Активный конфиг ещё не создан",
            "config.stagedNote": "staged/networks.conf — редактируемый, live | staged",
            "config.title": "Конфиг bird",
            "config.updating": "обновление: —",
            "err.asn_already_in_use": "ASN уже используется другим пиром",
            "err.asn_empty": "Значение не может быть пустым",
            "err.asn_not_int": "Должно быть целым числом",
            "err.asn_range": "Вне допустимого диапазона",
            "err.bgp_peer_not_found": "BGP-пир не найден",
            "err.community_not_found": "Сообщество не найдено",
            "err.config_generation_failed": "Не удалось сгенерировать конфиг",
            "err.editable_only_networks_conf": "Редактируется только networks.conf",
            "err.empty_network_list": "Пустой список сетей",
            "err.enabled_not_boolean": "enabled должно быть булевым",
            "err.invalid_prefix": "Некорректный префикс",
            "err.missing_filename": "Отсутствует параметр filename",
            "err.missing_name": "Отсутствует поле name",
            "err.missing_prefix": "Отсутствует поле prefix",
            "err.name_empty": "Имя сообщества не может быть пустым",
            "err.name_format": "Недопустимое имя сообщества",
            "err.neighbor_already_in_use": "Neighbor уже используется другим пиром",
            "err.neighbor_duplicate": "Пир с таким neighbor уже существует",
            "err.neighbor_empty": "Neighbor не может быть пустым",
            "err.neighbor_has_mask": "Neighbor указывается без /mask",
            "err.neighbor_invalid": "Neighbor должен быть валидным IPv4-адресом",
            "err.neighbor_not_ipv4": "Neighbor должен быть IPv4-адресом",
            "err.network_not_found": "Сеть не найдена",
            "err.peer_id_empty": "Peer id не может быть пустым",
            "err.peer_id_format": "Недопустимый peer id",
            "err.peer_id_too_long": "Peer id слишком длинный",
            "err.restore_error": "Ошибка восстановления",
            "err.server_error": "Внутренняя ошибка сервера",
            "err.unknown_bundle_file": "Неизвестный файл бандла",
            "err.value_empty": "Значение не может быть пустым",
            "err.value_format": "Недопустимое значение",
            "err.value_range": "Значение вне диапазона",
            "err.value_zero_reserved": "Значение 0 зарезервировано",
            "m001": "ASN ${peer.local_as} не найдена среди сообществ — пиру не будет отдано ни одной сети",
            "m002": "Добавлено: пир «${peer.name}» (${peer.neighbor} as ${peer.neighbor_as})",
            "m003": " · этому пиру уходит ${peer.export_count} префиксов с его ASN",
            "m004": "Префикс отброшен по выбору оператора — запись не сохранена.",
            "m005": "Проверь вручную input filter и ip address на микротике ",
            "m006": ". Импорт прерван — повторите, добавленное не задвоится",
            "m007": "Префикс починить нельзя — запись не сохранена.",
            "m008": "Нечего добавлять: корректных префиксов нет",
            "m009": " — ничего не добавлено, повторите импорт",
            "m010": "Очистить default (65534)? Будет удалено ",
            "m011": "Среди сообществ нет ни одной сети с ASN ",
            "m012": "Разбор остановлен. Ничего не добавлено.",
            "m013": "Имя (AS:COMM), напр. 64500 или 100:200",
            "m014": " — пиру не будет отдано ни одной сети",
            "m015": "Останавливаю после текущего запроса…",
            "m016": "Сгенерировать конфиг RouterOS для ",
            "m017": "Этот пир получает все сети таблицы",
            "m018": "Ввести вручную, напр. 65500:1000",
            "m019": "Конфиг скопирован в буфер обмена",
            "m020": "Сети (одна строка, любой формат)",
            "m021": "Пир в базе данных не заведён",
            "m022": ", исправлено по подсказке: ",
            "m023": "Введите данные для разбора",
            "m024": "Разбор не удался на чанке ",
            "m025": "Массовое добавление сетей",
            "m026": " исправленных префиксов.",
            "m027": "\"? Будет так же удалено ",
            "m028": ", пропущено дубликатов: ",
            "m029": "Редактировать сообщество",
            "m030": "Применение конфигурации",
            "m031": "Проверка найденных CIDR",
            "m032": ", отброшено сервером: ",
            "m033": " сетей без сообщества",
            "m034": "Непредвиденная ошибка",
            "m035": "Ошибка запроса: HTTP ",
            "m036": "Префикс исправлен на ",
            "m037": "неизвестная причина",
            "m038": "+ Новое сообщество",
            "m039": "Откат конфигурации",
            "m040": "Ошибка применения",
            "m041": "Будет добавлено ",
            "m042": "Ошибка скрипта: ",
            "m043": "Добавить в базу",
            "m044": "вписать вручную",
            "m045": "починить нельзя",
            "m046": "ASN не найдена",
            "m047": "Префикс (CIDR)",
            "m048": "после создания",
            "m049": ", отброшено: ",
            "m050": "Имя (AS:COMM)",
            "m051": "Удалить сеть ",
            "m052": "обновление: —",
            "m053": "Конфигурация",
            "m054": "Сообщество: ",
            "m055": "Удалить AS \"",
            "m056": "Удалить пир ",
            "m057": "обновление: ",
            "m058": "Добавлено: ",
            "m059": "Конфиг для ",
            "m060": "Напр. 65001",
            "m061": "Сети с ASN ",
            "m062": "не читается",
            "m063": " префиксов",
            "m064": "(счётчик: ",
            "m065": "Подготовка",
            "m066": "Сообщества",
            "m067": "Добавлено",
            "m068": "Применяю…",
            "m069": "Сетей нет",
            "m070": "Сохранить",
            "m071": "BGP-пиры",
            "m072": "Добавить",
            "m073": "Описание",
            "m074": "Очистить",
            "m075": "Ошибка: ",
            "m076": "все сети",
            "m077": ": чанк ",
            "m078": "Удалить",
            "m079": " сетей",
            "m080": "+ сеть",
            "m081": "Бекапы",
            "m082": "Запись",
            "m083": "Отмена",
            "m084": "Разбор",
            "m085": "Сервер",
            "m086": "Статус",
            "m087": " из ",
            "m088": "Сети",
            "m092": " строк починить нельзя — они будут отброшены в любом случае.",
            "m093": "Автоматически скопировать не вышло — текст выделен, нажмите Ctrl+C",
            "m094": "с",
            "m095": "Восстановить",
            "m096": "Восстановить бекап от ",
            "m097": "? База данных, staged и live будут заменены его содержимым.",
            "m098": "Активный конфиг ещё не создан — правки подготовят контент под управляемые блоки.",
            "m099": "<div class=\"form-row\"><span class=\"field\">Последнее применение: <strong>",
            "m100": "<span class=\"field\"><strong>bird daemon:</strong> не запущен</span>",
            "m101": "<span class=\"field\"><strong>bird daemon:</strong> работает</span>",
            "m102": "<div class=\"form-row\"><span class=\"field\">Пользователь: <strong>",
            "m103": "<div class=\"form-row\"><span class=\"field\">Действие: <strong>",
            "m104": "По умолчанию экспортируется только указанный Local AS.",
            "m105": "Пир настроен в панели, но не найден в выводе birdc",
            "m106": "Файл не читается как UTF-8 — счётчики недостоверны",
            "m107": "\"><strong>Последняя ошибка reload:</strong> ",
            "m108": "Заполните peer id, neighbor и neighbor ASN",
            "m109": "Не удалось создать Community «65534»",
            "m110": "Применено, но BIRD не перезагружен: ",
            "m111": "Не удалось загрузить networks.conf",
            "m112": "Построчное сравнение не показано: ",
            "m113": "Ошибка переконфигурирования BIRD",
            "m114": "State (сводная колонка birdc): ",
            "m115": ". Счётчики в заголовке точные.",
            "m116": "Не удалось загрузить сравнение",
            "m117": "Канал: Channel ipv4 → State: ",
            "m118": "Показать построчное сравнение",
            "m119": "Ошибка соединения с сервером",
            "m120": "BIRD переконфигурирован",
            "m121": "Загрузка networks.conf…",
            "m122": "Не удалось создать AS «",
            "m123": "? Действие необратимо.",
            "m124": "Ошибка создания бекапа",
            "m125": "Ошибка удаления бекапа",
            "m126": "Переконфигурирую BIRD…",
            "m127": "Изменения применены (",
            "m128": "Ошибка восстановления",
            "m129": "Синтаксическая ошибка",
            "m130": "Нет связи с сервером",
            "m131": "Загрузка сравнения…",
            "m132": "Сервисные протоколы",
            "m133": "Бекап восстановлен",
            "m134": "причина не указана",
            "m135": "Удалить бекап от ",
            "m136": "Отдавать все ASN",
            "m137": "Откат (снимок): ",
            "m138": "Показаны первые ",
            "m139": " строк (всего +",
            "m140": "Восстанавливаю ",
            "m141": " строк, порог ",
            "m142": "BGP-сессий нет",
            "m143": "Добавить пира",
            "m144": "Изменений нет",
            "m145": "Сделать бекап",
            "m146": "Создаю бекап…",
            "m147": "Бекап создан",
            "m148": "Бекап удалён",
            "m149": "Бекапов нет",
            "m150": "Записей нет",
            "m151": "Сервисных: ",
            "m152": "BGP-сессии",
            "m153": "Свободно: ",
            "m154": "не указана",
            "m155": "недоступен",
            "m156": "нет в BIRD",
            "m157": "обновлено ",
            "m158": "Добавляю…",
            "m159": "Состояние",
            "m160": " сетей).",
            "m161": "Префиксы",
            "m162": "Протокол",
            "m163": "Слоты: ",
            "m164": "Удаляю ",
            "m165": "Канал",
            "m166": "есть",
            "m167": "Тип",
            "m168": "нет",
            "m169": "Ничего не найдено",
            "m170": "Ввод текста — поиск по открытым строкам; Enter — поиск по всей базе",
            "m171": "Поиск…",
            "m_t1": " · этому пиру уходит {n} префиксов с его ASN",
            "m_t2": "ASN {asn} не найдена среди сообществ — пиру не будет отдано ни одной сети",
            "m_t3": "Добавлено: пир «{name}» ({neighbor} as {asn})",
            "panel.addNetworks": "Добавить сети",
            "panel.addPeer": "Добавить BGP Peer",
            "panel.audit": "Журнал",
            "panel.backup": "Бекап",
            "panel.bgpSessions": "BGP-сессии",
            "panel.serviceProtocols": "Сервисные протоколы",
            "panel.birdPeers": "BIRD peers",
            "panel.configStatus": "Статус конфигурации",
            "panel.networks": "Сети и сообщества",
            "panel.peers": "BGP peers",
            "ros.close": "Закрыть",
            "ros.copy": "Копировать",
            "ros.title": "Конфиг",
            "tab.bird": "BIRD",
            "tab.networks": "Сети",
            "tab.system": "Система",
            "tabs.ariaLabel": "Разделы bird-manager"
        },
        en: {
            "app.title": "BIRD Manager — Dashboard",
            "banner.offline": "No connection to server",
            "btn.refresh": "Refresh active tab",
            "bulk.cancel": "Abort",
            "bulk.phase": "Parsing: chunk 1 of 1",
            "bulk.title": "Import networks",
            "cidr.dropBtn": "Drop",
            "cidr.fix": "fix",
            "cidr.fixBtn": "Fix",
            "cidr.found": "found",
            "cidr.title": "Malformed CIDRs found. Fix or drop?",
            "common.loading": "Loading…",
            "config.apply": "Apply changes",
            "config.diffAria": "staged — line-by-line comparison with live",
            "config.hint": "staged files are published to live only after \"Apply changes\"; networks, communities and BGP peers are managed in the database on the Networks tab.",
            "config.otherFiles": "Other files — live | staged",
            "config.placeholder": "Active config not created yet",
            "config.stagedNote": "staged/networks.conf — editable, live | staged",
            "config.title": "BIRD config",
            "config.updating": "update: —",
            "err.asn_already_in_use": "ASN already used by another peer",
            "err.asn_empty": "Value cannot be empty",
            "err.asn_not_int": "Must be an integer",
            "err.asn_range": "Out of allowed range",
            "err.bgp_peer_not_found": "BGP peer not found",
            "err.community_not_found": "Community not found",
            "err.config_generation_failed": "Config generation failed",
            "err.editable_only_networks_conf": "Only networks.conf is editable",
            "err.empty_network_list": "Empty network list",
            "err.enabled_not_boolean": "enabled must be a boolean",
            "err.invalid_prefix": "Invalid prefix",
            "err.missing_filename": "Missing required parameter: filename",
            "err.missing_name": "Missing required field: name",
            "err.missing_prefix": "Missing required field: prefix",
            "err.name_empty": "Community name cannot be empty",
            "err.name_format": "Invalid community name",
            "err.neighbor_already_in_use": "Neighbor already used by another peer",
            "err.neighbor_duplicate": "Peer with this neighbor already exists",
            "err.neighbor_empty": "Neighbor cannot be empty",
            "err.neighbor_has_mask": "Neighbor specified without /mask",
            "err.neighbor_invalid": "Neighbor must be a valid IPv4 address",
            "err.neighbor_not_ipv4": "Neighbor must be an IPv4 address",
            "err.network_not_found": "Network not found",
            "err.peer_id_empty": "Peer id cannot be empty",
            "err.peer_id_format": "Invalid peer id",
            "err.peer_id_too_long": "Peer id too long",
            "err.restore_error": "Restore error",
            "err.server_error": "Internal server error",
            "err.unknown_bundle_file": "Unknown bundle file",
            "err.value_empty": "Value cannot be empty",
            "err.value_format": "Invalid value",
            "err.value_range": "Value out of range",
            "err.value_zero_reserved": "Value 0 is reserved",
            "m001": "ASN ${peer.local_as} not found among communities — peer will receive no networks",
            "m002": "Added: peer \"${peer.name}\" (${peer.neighbor} as ${peer.neighbor_as})",
            "m003": " · this peer receives ${peer.export_count} prefixes with its ASN",
            "m004": "Prefix dropped by operator choice — record not saved.",
            "m005": "Check input filter and ip address on MikroTik manually ",
            "m006": ". Import aborted — retry; already added will not be duplicated",
            "m007": "Prefix cannot be fixed — record not saved.",
            "m008": "Nothing to add: no valid prefixes",
            "m009": " — nothing added, retry import",
            "m010": "Clear default (65534)? Will be deleted ",
            "m011": "No networks with ASN among communities ",
            "m012": "Parsing stopped. Nothing added.",
            "m013": "Name (AS:COMM), e.g. 64500 or 100:200",
            "m014": " — peer will receive no networks",
            "m015": "Stopping after current request…",
            "m016": "Generate RouterOS config for ",
            "m017": "This peer receives all networks",
            "m018": "Enter manually, e.g. 65500:1000",
            "m019": "Config copied to clipboard",
            "m020": "Networks (one per line, any format)",
            "m021": "Peer not found in database",
            "m022": ", corrected by suggestion: ",
            "m023": "Enter data to parse",
            "m024": "Parsing failed on chunk ",
            "m025": "Bulk add networks",
            "m026": " prefixes corrected.",
            "m027": "\"? Will also be deleted ",
            "m028": ", duplicates skipped: ",
            "m029": "Edit community",
            "m030": "Apply configuration",
            "m031": "Checking found CIDRs",
            "m032": ", rejected by server: ",
            "m033": " networks without community",
            "m034": "Unexpected error",
            "m035": "Request error: HTTP ",
            "m036": "Prefix corrected to ",
            "m037": "unknown reason",
            "m038": "+ New community",
            "m039": "Roll back configuration",
            "m040": "Apply error",
            "m041": "Will be added ",
            "m042": "Script error: ",
            "m043": "Add to database",
            "m044": "enter manually",
            "m045": "cannot be fixed",
            "m046": "ASN not found",
            "m047": "Prefix (CIDR)",
            "m048": "after creation",
            "m049": ", dropped: ",
            "m050": "Name (AS:COMM)",
            "m051": "Delete network ",
            "m052": "update: —",
            "m053": "Configuration",
            "m054": "Community: ",
            "m055": "Delete AS \"",
            "m056": "Delete peer ",
            "m057": "update: ",
            "m058": "Added: ",
            "m059": "Config for ",
            "m060": "e.g. 65001",
            "m061": "Networks with ASN ",
            "m062": "not readable",
            "m063": " prefixes",
            "m064": "(count: ",
            "m065": "Preparing",
            "m066": "Communities",
            "m067": "Added",
            "m068": "Applying…",
            "m069": "No networks",
            "m070": "Save",
            "m071": "BGP peers",
            "m072": "Add",
            "m073": "Description",
            "m074": "Clear",
            "m075": "Error: ",
            "m076": "all networks",
            "m077": ": chunk ",
            "m078": "Delete",
            "m079": " networks",
            "m080": "+ network",
            "m081": "Backups",
            "m082": "Writing",
            "m083": "Cancel",
            "m084": "Parsing",
            "m085": "Server",
            "m086": "Status",
            "m087": " of ",
            "m088": "Networks",
            "m092": " lines cannot be fixed — they will be dropped anyway.",
            "m093": "Could not copy automatically — text selected, press Ctrl+C",
            "m094": "s",
            "m095": "Restore",
            "m096": "Restore backup from ",
            "m097": "? Database, staged and live will be replaced with its contents.",
            "m098": "Active config not created yet — edits will prepare content for managed blocks.",
            "m099": "<div class=\"form-row\"><span class=\"field\">Last applied: <strong>",
            "m100": "<span class=\"field\"><strong>bird daemon:</strong> not running</span>",
            "m101": "<span class=\"field\"><strong>bird daemon:</strong> running</span>",
            "m102": "<div class=\"form-row\"><span class=\"field\">User: <strong>",
            "m103": "<div class=\"form-row\"><span class=\"field\">Action: <strong>",
            "m104": "By default only the specified Local AS is exported.",
            "m105": "Peer configured in panel but not found in birdc output",
            "m106": "File not readable as UTF-8 — counters unreliable",
            "m107": "\"><strong>Last reload error:</strong> ",
            "m108": "Fill in peer id, neighbor and neighbor ASN",
            "m109": "Failed to create Community «65534»",
            "m110": "Applied, but BIRD not reloaded: ",
            "m111": "Failed to load networks.conf",
            "m112": "Line-by-line comparison not shown: ",
            "m113": "BIRD reconfiguration error",
            "m114": "State (birdc summary column): ",
            "m115": ". Counters in header are exact.",
            "m116": "Failed to load comparison",
            "m117": "Channel: Channel ipv4 → State: ",
            "m118": "Show line-by-line comparison",
            "m119": "Server connection error",
            "m120": "BIRD reconfigured",
            "m121": "Loading networks.conf…",
            "m122": "Failed to create AS «",
            "m123": "? Action is irreversible.",
            "m124": "Backup creation error",
            "m125": "Backup deletion error",
            "m126": "Reconfiguring BIRD…",
            "m127": "Changes applied (",
            "m128": "Restore error",
            "m129": "Syntax error",
            "m130": "No connection to server",
            "m131": "Loading comparison…",
            "m132": "Service protocols",
            "m133": "Backup restored",
            "m134": "reason not specified",
            "m135": "Delete backup from ",
            "m136": "Export all ASNs",
            "m137": "Rollback (snapshot): ",
            "m138": "Showing first ",
            "m139": " lines (total +",
            "m140": "Restoring ",
            "m141": " lines, threshold ",
            "m142": "No BGP sessions",
            "m143": "Add peer",
            "m144": "No changes",
            "m145": "Create backup",
            "m146": "Creating backup…",
            "m147": "Backup created",
            "m148": "Backup deleted",
            "m149": "No backups",
            "m150": "No records",
            "m151": "Service: ",
            "m152": "BGP sessions",
            "m153": "Free: ",
            "m154": "not specified",
            "m155": "unavailable",
            "m156": "not in BIRD",
            "m157": "updated ",
            "m158": "Adding…",
            "m159": "Status",
            "m160": " networks).",
            "m161": "Prefixes",
            "m162": "Protocol",
            "m163": "Slots: ",
            "m164": "Deleting ",
            "m165": "Channel",
            "m166": "yes",
            "m167": "Type",
            "m168": "no",
            "m169": "Nothing found",
            "m170": "Type to search open rows; Enter to search all records",
            "m171": "Search…",
            "m_t1": " · this peer receives {n} prefixes with its ASN",
            "m_t2": "ASN {asn} not found among communities — peer will receive no networks",
            "m_t3": "Added: peer \"{name}\" ({neighbor} as {asn})",
            "panel.addNetworks": "Add networks",
            "panel.addPeer": "Add BGP Peer",
            "panel.audit": "Audit log",
            "panel.backup": "Backup",
            "panel.bgpSessions": "BGP sessions",
            "panel.serviceProtocols": "Service protocols",
            "panel.birdPeers": "BIRD peers",
            "panel.configStatus": "Config status",
            "panel.networks": "Networks & communities",
            "panel.peers": "BGP peers",
            "ros.close": "Close",
            "ros.copy": "Copy",
            "ros.title": "Config",
            "tab.bird": "BIRD",
            "tab.networks": "Networks",
            "tab.system": "System",
            "tabs.ariaLabel": "bird-manager sections"
        }
    };

    let currentLang = 'ru';

    function detectLang() {
        try {
            const saved = localStorage.getItem(STORAGE_KEY);
            if (saved === 'ru' || saved === 'en') return saved;
        } catch (e) { /* ignore */ }
        const nav = (navigator.language || navigator.userLanguage || 'ru').toLowerCase();
        return nav.startsWith('en') ? 'en' : 'ru';
    }

    function interpolate(str, vars) {
        if (!vars) return str;
        return str.replace(/\{(\w+)\}/g, function (_, k) {
            return (vars[k] !== undefined) ? String(vars[k]) : '{' + k + '}';
        });
    }

    function pluralize(key, n, vars) {
        const dict = DICTS[currentLang] || {};
        const entry = dict[key];
        if (!entry || typeof entry !== 'object') return key;
        const form = (currentLang === 'ru') ? ruPlural(n) : enPlural(n);
        const str = entry[form] || entry.one || entry.other || key;
        return interpolate(str, Object.assign({ n: n }, vars || {}));
    }

    function t(key, vars) {
        const dict = DICTS[currentLang] || {};
        let str = dict[key];
        if (str === undefined) {
            // fallback to Russian, then key
            str = (DICTS.ru || {})[key];
        }
        if (str === undefined) return key;
        if (typeof str === 'object') {
            // plural form requested via vars.n
            if (vars && vars.n !== undefined) return pluralize(key, vars.n, vars);
            return interpolate(JSON.stringify(str), vars);
        }
        return interpolate(String(str), vars);
    }

    function setLang(lang) {
        if (lang !== 'ru' && lang !== 'en') return;
        currentLang = lang;
        try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* ignore */ }
        const root = document.getElementById('html-root') || document.documentElement;
        if (root) root.setAttribute('lang', lang);
        applyToDom();
        document.dispatchEvent(new CustomEvent('langchange', { detail: { lang: lang } }));
    }

    function getLang() { return currentLang; }

    function applyToDom() {
        document.querySelectorAll('[data-i18n]').forEach(function (el) {
            const key = el.getAttribute('data-i18n');
            const attr = el.getAttribute('data-i18n-attr');
            const val = t(key);
            if (attr) {
                el.setAttribute(attr, val);
            } else {
                el.textContent = val;
            }
        });
        // attribute-only translations
        ['title', 'placeholder', 'aria-label'].forEach(function (attr) {
            const dataAttr = 'data-i18n-' + attr;
            document.querySelectorAll('[' + dataAttr + ']').forEach(function (el) {
                const key = el.getAttribute(dataAttr);
                el.setAttribute(attr, t(key));
            });
        });
    }

    // init
    currentLang = detectLang();

    global.t = t;
    global.I18n = {
        t: t,
        plural: function (n, one, few, many) {
            // convenience: pass array [one, few, many] for ru
            if (Array.isArray(one)) { few = one[1]; many = one[2]; one = one[0]; }
            const form = ruPlural(n);
            return form === 'one' ? one : (form === 'few' ? few : many);
        },
        setLang: setLang,
        getLang: getLang,
        applyToDom: applyToDom,
        dicts: DICTS
    };

    // apply after DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { applyToDom(); });
    } else {
        applyToDom();
    }
})(window);
