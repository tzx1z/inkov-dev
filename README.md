# inkov-dev

Сайт-визитка Евгения Инькова, Senior Data Engineer / Data Platform Engineer: https://inkov.dev

Статический сайт без сборки: файл в репозитории = файл на сайте. Хостинг - GitHub Pages
(ветка `main`, каталог `/`), домен задан в `CNAME`. Русская версия в корне, английская в `/en/`.

## Структура

```
index.html, en/index.html            главная RU и EN
projects/, en/projects/              список проектов
projects/<slug>/, en/projects/<slug>/   страницы проектов
404.html                             одна страница на оба языка
assets/css/style.css                 единственная таблица стилей, токены на :root
assets/js/main.js                    тема, язык с якорем, копирование email, путь на 404
assets/fonts/                        JetBrains Mono 400 и 700 (woff2, OFL.txt)
assets/img/photos/                   портрет в AVIF, WebP, JPEG
assets/img/og/                       OG-картинки 1200x630
tools/images.py                      подготовка фото
tools/check_site.py                  статическая проверка сайта
tools/project-ru.html, project-en.html   шаблоны страницы проекта
tools/og-ru.html, og-en.html, og.css     макеты OG-картинок
_src/                                исходные фото с EXIF, в .gitignore
```

## Правила

- Все внутренние пути от корня (`/assets/...`), ссылки на каталоги со слешем (`/projects/`).
  Иначе ломается `404.html`, который GitHub Pages отдает на любом несуществующем пути.
- Паритет RU/EN: страницы пары отличаются только текстом, `lang`, `alt`, `aria-label`,
  `data-label-*`, `data-done`, мета-тегами и JSON-LD. Теги, `id`, классы и порядок блоков
  совпадают. Правка вносится сначала в RU, затем в EN копированием разметки.
- В текстах и коде нет длинного и среднего тире (только `-`) и буквы е с точками.
- Нет атрибута `style`, тега `<style>` и inline-скриптов в HTML. Исключение - однострочный
  скрипт темы в `<head>`: он побайтно одинаков на всех страницах, его хэш указан в CSP.
- Нет фреймворков, CDN, внешних шрифтов, счетчиков и трекеров. Цвета только через токены.

Если скрипт темы изменился, пересчитайте хэш и замените его во всех CSP:

```bash
printf '%s' 'СОДЕРЖИМОЕ_СКРИПТА' | openssl dgst -sha256 -binary | openssl base64 -A
```

## Локальный запуск

```bash
python3 -m http.server 8000 --bind 127.0.0.1
```

Сайт открывается по адресу http://127.0.0.1:8000/. Страница 404 локально доступна
только по прямому адресу `/404.html`.

## Добавление проекта

1. Выбрать slug (`^[a-z0-9]+(-[a-z0-9]+)*$`). Исходники скриншотов положить в
   `_src/img/projects/<slug>/` и выполнить:
   ```bash
   python3 tools/images.py _src/img/projects/<slug>/1.png assets/img/projects/<slug>/<slug>-1 640 1280 --q-avif 70 --q-webp 85
   ```
2. Скопировать шаблоны и заменить плейсхолдеры `{{...}}`, удалить пустые блоки и строку `noindex`.
   Если в slug есть дефисы, в Mono-id `project_<slug>` они заменяются на подчеркивания
   (`project_xmpp_server_guides`); в URL и атрибуте `id` остаются дефисы:
   ```bash
   mkdir -p projects/<slug> en/projects/<slug>
   cp tools/project-ru.html projects/<slug>/index.html
   cp tools/project-en.html en/projects/<slug>/index.html
   ```
3. Добавить карточку в начало списка в `projects/index.html` и `en/projects/index.html`.
   На главной плитки проектов стоят на первом экране под плиткой профиля (`.tiles` внутри
   `<section id="projects">`): не больше 3 элементов, реальные проекты (сначала `running`,
   затем `success`), недостающие места - заглушки `queued`. Когда страница проекта
   опубликована, название в заголовке карточки оборачивается в ссылку на `/projects/<slug>/`
   (`/en/projects/<slug>/`).
4. Ссылка "Все проекты" / "All projects" стоит в заголовке блока проектов на главной.
5. Бейдж блока проектов (`data-state` у `<section id="projects">`) - `running`, пока хотя бы
   один проект в работе, иначе `success`. Статус меняется в RU и EN.
6. В `sitemap.xml` добавить два блока `<url>` и обновить `lastmod` измененных страниц.
7. Запустить проверки из раздела ниже.

## Фото и OG-картинки

Исходные JPEG лежат в `_src/img/` и не публикуются: в них EXIF с моделью телефона и датой.
Скрипт переводит цвет в sRGB, кадрирует, удаляет метаданные и сохраняет AVIF, WebP и JPEG:

```bash
python3 tools/images.py _src/img/avatar.jpg assets/img/photos/evgenii-inkov-portrait 256 400 600 --crop 250,30,850,630
```

OG-картинки снимаются с макетов при запущенном локальном сервере:

```bash
mkdir -p /tmp/inkov-dev-work/ff-og
firefox --headless --no-remote --profile /tmp/inkov-dev-work/ff-og --window-size=1200,630 \
  --screenshot /tmp/inkov-dev-work/og-ru.png http://127.0.0.1:8000/tools/og-ru.html
magick /tmp/inkov-dev-work/og-ru.png -strip -quality 85 -sampling-factor 4:2:0 -interlace JPEG assets/img/og/og-ru.jpg
```

Для английской версии то же самое с `og-en`.

## Проверки перед коммитом

- [ ] `python3 tools/check_site.py` - 0 ошибок.
- [ ] Нет запрещенных символов:
      `grep -rnP '[\x{2013}\x{2014}\x{0401}\x{0451}]' --include='*.html' --include='*.css' --include='*.js' --include='*.py' --include='*.xml' --include='README.md' --exclude-dir=_src --exclude-dir=.git .`
- [ ] Валидатор Nu (HTML и SVG) - 0 ошибок.
- [ ] Опубликованные изображения без метаданных:
      `exiftool -r -q -if '$EXIF:all or $XMP:all or $ICC_Profile:all or $IPTC:all' -p '$Directory/$FileName' assets favicon.ico apple-touch-icon.png`
      выводит пустоту.
- [ ] `git status` не содержит `TODO.md`, `_src/` и исходных JPEG.

## Настройки GitHub Pages

Settings -> Pages: источник `main` / `/ (root)`, Enforce HTTPS. Домен `inkov.dev`
подтвержден в настройках GitHub. DNS: A и AAAA записи апекса на адреса GitHub Pages,
`www` - CNAME на `tzx1z.github.io`. Файл `.nojekyll` отключает обработку Jekyll.

## Лицензия

Лицензия MIT в `LICENSE` относится к коду сайта. Тексты и фотографии не лицензируются
(формулировку подтверждает владелец).
