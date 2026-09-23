#!/usr/bin/env python3
"""Статическая проверка сайта inkov.dev.

Только стандартная библиотека Python. Каждая ошибка выводится одной строкой
"файл: [пункт] проблема", итог - в stderr. Код выхода: 0 - ошибок нет,
1 - найдена хотя бы одна ошибка.

Запуск из корня репозитория:

    python3 tools/check_site.py
    python3 tools/check_site.py --root /путь/к/сайту   # проверка другого каталога

Корень сайта по умолчанию - каталог на уровень выше скрипта.
Страницы - все *.html вне _src/, .git, node_modules, скрытых каталогов и tools/.
tools/*.html (шаблоны с плейсхолдерами) проверяются только по пунктам 7 и 9.
404.html - отдельная страница без языковой пары.
"""

import argparse
import base64
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

SITE = "https://inkov.dev"
SITE_HOSTS = {"inkov.dev", "www.inkov.dev"}
# Не публикуются и не проверяются; каталоги с точкой в начале пропускаются тоже
SKIP_DIRS = {".git", "_src", "node_modules"}
# Отчеты Lighthouse (в .gitignore), если их сохранили в репозиторий
SKIP_FILE = re.compile(r"\.report\.(?:html|json)$")
# П.9: файлы, в которых запрещены символы U+2013, U+2014, U+0401, U+0451
CHAR_EXT = {".html", ".css", ".js", ".py", ".xml", ".webmanifest", ".txt"}
FORBIDDEN_CHARS = re.compile("[\u2013\u2014\u0401\u0451]")
# П.10: файлы сайта вне tools/, в которых не должно быть "{{".
# README.md не проверяется: он описывает плейсхолдеры шаблона проекта (раздел 8)
PLACEHOLDER_EXT = {".html", ".css", ".js", ".xml", ".webmanifest", ".txt", ".json", ".svg"}
# П.7: запрещенные конструкции JS (7.5)
JS_FORBIDDEN = re.compile(
    r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|eval)\b|\bdocument\s*\.\s*write|\bnew\s+Function\b"
)
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# Типы <script>, которые браузер исполняет (для них нужен хэш в CSP)
EXEC_SCRIPT_TYPES = {"", "text/javascript", "application/javascript", "module"}
HREFLANGS = {"ru", "en", "x-default"}
SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
XHTML = "{http://www.w3.org/1999/xhtml}"
LASTMOD = re.compile(r"\d{4}-\d{2}-\d{2}(?:T[0-9:.+\-Z]+)?")


class Doc(HTMLParser):
    """Разбор HTML/SVG: список элементов с атрибутами и исходный текст скриптов."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text = text
        # Начало каждой строки: getpos() дает (строка, столбец), нужен сдвиг в тексте
        self.line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.elements = []  # (тег, атрибуты, номер строки)
        self.scripts = []  # (индекс элемента, атрибуты, исходный текст, номер строки)
        self._script = None
        self.feed(text)
        self.close()
        self.ids = {a["id"] for _, a, _ in self.elements if "id" in a}

    def _offset(self):
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        a = {}
        for k, v in attrs:
            a.setdefault(k, v or "")  # при повторе атрибута действует первый, как в браузере
        self.elements.append((tag, a, self.getpos()[0]))
        if tag == "script":
            # Содержимое скрипта берется из исходного текста без преобразований:
            # по нему считается sha256 для CSP
            self._script = (len(self.elements) - 1, self._offset() + len(self.get_starttag_text()))

    def handle_startendtag(self, tag, attrs):
        # <script/> не открывает скрипт для разбора: содержимого у него нет
        self.handle_starttag(tag, attrs)
        if tag == "script":
            self._script = None

    def handle_endtag(self, tag):
        if tag == "script" and self._script:
            idx, start = self._script
            _, a, line = self.elements[idx]
            self.scripts.append((idx, a, self.text[start:self._offset()], line))
            self._script = None

    def meta(self, key, value):
        """content всех <meta key="value"> с номерами строк."""
        return [(a.get("content", ""), line) for t, a, line in self.elements
                if t == "meta" and a.get(key, "").strip().lower() == value]

    def links(self, rel):
        return [(a, line) for t, a, line in self.elements
                if t == "link" and rel in a.get("rel", "").lower().split()]

    def noindex(self):
        for content, _ in self.meta("name", "robots"):
            tokens = re.split(r"[\s,]+", content.lower())
            if "noindex" in tokens or "none" in tokens:
                return True
        return False


def is_exec(attrs):
    """Inline-скрипт, который исполняется браузером (не JSON-LD и не внешний)."""
    return "src" not in attrs and attrs.get("type", "").strip().lower() in EXEC_SCRIPT_TYPES


def is_ld(attrs):
    return attrs.get("type", "").strip().lower() == "application/ld+json"


def page_url(rel):
    """Абсолютный URL страницы: X/index.html -> https://inkov.dev/X/."""
    if rel == "index.html":
        return SITE + "/"
    if rel.endswith("/index.html"):
        return f"{SITE}/{rel[:-len('index.html')]}"
    return f"{SITE}/{rel}"


def ld_urls(node):
    """Пары (ключ, URL) из ключей url и image JSON-LD, рекурсивно."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("url", "image"):
                for x in v if isinstance(v, list) else [v]:
                    if isinstance(x, str):
                        yield k, x
            yield from ld_urls(v)
    elif isinstance(node, list):
        for x in node:
            yield from ld_urls(x)


def fmt(key):
    tag, id_, cls = key
    return "<" + tag + (f' id="{id_}"' if id_ is not None else "") + (f' class="{cls}"' if cls else "") + ">"


class Site:
    def __init__(self, root):
        self.root = root
        self.errors = []
        self._texts = {}
        self.files = []
        for d, dirs, names in root.walk():
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
            self.files += [(d / n).relative_to(root).as_posix() for n in sorted(names) if not SKIP_FILE.search(n)]
        self.html = [f for f in self.files if f.endswith(".html")]
        self.pages = [f for f in self.html if not f.startswith("tools/")]
        # Разбор HTML и SVG (SVG нужен для п.7, favicon.svg не проверяется)
        self.docs = {}
        for rel in self.html + [f for f in self.files if f.endswith(".svg") and f != "assets/img/favicon.svg"]:
            text = self.text(rel)
            if text is not None:
                self.docs[rel] = Doc(text)
        self.pages = [p for p in self.pages if p in self.docs]
        # JSON-LD страниц: (объект, строка, ошибка разбора)
        self.ld = {}
        for rel in self.pages:
            self.ld[rel] = []
            for _, a, body, line in self.docs[rel].scripts:
                if is_ld(a):
                    try:
                        self.ld[rel].append((json.loads(body), line, None))
                    except json.JSONDecodeError as e:
                        self.ld[rel].append((None, line, f"{e.msg}, строка {e.lineno}, столбец {e.colno}"))

    def err(self, rel, point, msg):
        line = f"{rel}: [{point}] {msg}"
        if line not in self.errors:  # одинаковые ошибки (например, hreflang=en и x-default) выводятся один раз
            self.errors.append(line)

    def text(self, rel):
        """Текст файла в UTF-8 (с кэшем) или None, если файла нет или он не в UTF-8."""
        if rel not in self._texts:
            path = self.root / rel
            try:
                self._texts[rel] = path.read_text(encoding="utf-8") if path.is_file() else None
            except UnicodeDecodeError:
                self.err(rel, 9, "файл не в кодировке UTF-8")
                self._texts[rel] = None
            except OSError as e:
                self.err(rel, 9, f"файл не читается: {e.strerror}")
                self._texts[rel] = None
        return self._texts[rel]

    def resolve(self, path):
        """Путь от корня сайта -> (файл от корня, None) или (None, текст ошибки)."""
        parts = [p for p in path.split("/") if p]
        if any(p in (".", "..") for p in parts):
            return None, f"путь {path} содержит . или .."
        if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
            return None, f"путь {path} ведет в неопубликованный каталог или файл"
        target = self.root.joinpath(*parts)
        if not parts or path.endswith("/"):
            index = target / "index.html"
            if index.is_file():
                return index.relative_to(self.root).as_posix(), None
            return None, f"нет файла {path.rstrip('/')}/index.html"
        if target.is_dir():
            return None, f"ссылка на каталог {path} без / в конце"
        if target.is_file():
            return target.relative_to(self.root).as_posix(), None
        return None, f"файл {path} не найден"

    def site_file(self, url):
        """Абсолютный URL https://inkov.dev/... -> (файл от корня, None) или (None, ошибка)."""
        u = urlsplit(url)
        if u.scheme != "https" or u.netloc != "inkov.dev" or not u.path.startswith("/"):
            return None, f"адрес {url} не начинается с {SITE}/"
        if u.query:
            return None, f"адрес {url} содержит query"
        return self.resolve(unquote(u.path))

    # П.1-3: пары RU/EN, совпадение разметки, lang
    def check_pairs(self):
        pages = set(self.pages)
        for rel in self.pages:
            if rel == "404.html":
                pass
            elif not (rel == "index.html" or rel.endswith("/index.html")):
                self.err(rel, 1, "страница должна быть index.html в своем каталоге (7.2), пара не определяется")
            else:
                pair = rel[3:] if rel.startswith("en/") else "en/" + rel
                if pair not in pages:
                    self.err(rel, 1, f"нет парной страницы {pair}")
                elif not rel.startswith("en/"):
                    self.compare_markup(rel, pair)
            want = "en" if rel.startswith("en/") else "ru"
            html = next((a for t, a, _ in self.docs[rel].elements if t == "html"), None)
            if html is None or "lang" not in html:
                self.err(rel, 3, f'у <html> нет lang, ожидается lang="{want}"')
            elif html["lang"] != want:
                self.err(rel, 3, f'<html lang="{html["lang"]}">, ожидается lang="{want}"')

    def compare_markup(self, ru, en):
        def seq(rel):
            out = []
            for tag, a, line in self.docs[rel].elements:
                if tag == "span" and list(a) == ["lang"]:
                    continue  # <span lang> размечает иноязычный фрагмент только на одной странице пары
                out.append(((tag, a.get("id"), " ".join(a.get("class", "").split())), line))
            return out

        a, b = seq(ru), seq(en)
        for (ka, la), (kb, lb) in zip(a, b):
            if ka != kb:
                self.err(en, 2, f"разметка расходится с {ru}: строка {lb} {fmt(kb)}, в {ru} строка {la} {fmt(ka)}")
                return
        if len(a) != len(b):
            longer, (k, line) = (ru, a[len(b)]) if len(a) > len(b) else (en, b[len(a)])
            self.err(en, 2, f"разметка расходится с {ru}: лишние элементы в {longer} со строки {line} {fmt(k)}")

    # П.4: canonical, hreflang, Open Graph, JSON-LD, sitemap, robots, noindex
    def check_seo(self):
        alts_by_page = {}
        for rel in self.pages:
            doc = self.docs[rel]
            canon = [a.get("href", "") for a, _ in doc.links("canonical")]
            alts = {}
            for a, line in doc.links("alternate"):
                if "hreflang" in a:
                    h = a["hreflang"].strip().lower()
                    if h in alts:
                        self.err(rel, 4, f"строка {line}: hreflang={h} повторяется")
                    alts.setdefault(h, a.get("href", ""))
            og_url = [c for c, _ in doc.meta("property", "og:url")]
            og_img = [c for c, _ in doc.meta("property", "og:image")]

            if rel == "404.html":
                if not doc.noindex():
                    self.err(rel, 4, 'нет <meta name="robots" content="noindex">')
                extra = [name for name, found in (
                    ("canonical", canon), ("hreflang", alts),
                    ("Open Graph", [1 for t, a, _ in doc.elements if t == "meta" and a.get("property", "").startswith("og:")]),
                    ("JSON-LD", self.ld[rel])) if found]
                if extra:
                    self.err(rel, 4, "на 404.html не должно быть: " + ", ".join(extra) + " (7.3)")
                continue

            alts_by_page[rel] = alts
            url = page_url(rel)
            lang = "en" if rel.startswith("en/") else "ru"
            if len(canon) != 1:
                self.err(rel, 4, f"canonical должен быть один, найдено {len(canon)}")
            elif canon[0] != url:
                self.err(rel, 4, f"canonical {canon[0]}, ожидается {url}")
            if len(og_url) != 1:
                self.err(rel, 4, f"og:url должен быть один, найдено {len(og_url)}")
            elif og_url[0] != url:
                self.err(rel, 4, f"og:url {og_url[0]}, ожидается {url}")
            if not og_img:
                self.err(rel, 4, "нет og:image")
            if set(alts) != HREFLANGS:
                self.err(rel, 4, f"набор hreflang: {', '.join(sorted(alts)) or 'пусто'}; ожидается en, ru, x-default")
            if lang in alts and alts[lang] != url:
                self.err(rel, 4, f"hreflang={lang} указывает на {alts[lang]}, ожидается адрес самой страницы {url}")
            if "en" in alts and "x-default" in alts and alts["x-default"] != alts["en"]:
                self.err(rel, 4, f"hreflang=x-default ({alts['x-default']}) должен совпадать с hreflang=en ({alts['en']})")

            checks = ([("canonical", u) for u in canon] + [(f"hreflang={h}", u) for h, u in alts.items()]
                      + [("og:url", u) for u in og_url] + [("og:image", u) for u in og_img]
                      + [(f"JSON-LD {k}", u) for obj, _, _ in self.ld[rel] for k, u in ld_urls(obj)])
            for what, u in checks:
                _, e = self.site_file(u)
                if e:
                    self.err(rel, 4, f"{what}: {e}")

        # Взаимность hreflang: набор на странице-цели совпадает с набором на исходной
        for rel, alts in alts_by_page.items():
            for h, u in alts.items():
                target, e = self.site_file(u)
                if e or target == rel:
                    continue
                if target not in alts_by_page:
                    self.err(rel, 4, f"hreflang={h}: {u} не является страницей с hreflang")
                elif alts_by_page[target] != alts:
                    self.err(rel, 4, f"hreflang не взаимны: набор hreflang на {target} отличается от этой страницы")

        indexable = {page_url(r): r for r in alts_by_page if not self.docs[r].noindex()}
        self.check_sitemap(indexable, alts_by_page)

        rel = "robots.txt"
        text = self.text(rel)
        if text is None:
            self.err(rel, 4, "файл не найден")
        elif not re.search(r"(?mi)^sitemap:\s*https://inkov\.dev/sitemap\.xml\s*$", text):
            self.err(rel, 4, f"нет строки Sitemap: {SITE}/sitemap.xml")

    def check_sitemap(self, indexable, alts_by_page):
        rel = "sitemap.xml"
        path = self.root / rel
        if not path.is_file():
            self.err(rel, 4, "файл не найден")
            return
        try:
            urlset = ET.parse(path).getroot()
        except ET.ParseError as e:
            self.err(rel, 4, f"XML не разбирается: {e}")
            return
        if urlset.tag != SM + "urlset":
            self.err(rel, 4, f"корневой элемент {urlset.tag}, ожидается {SM}urlset")
            return
        seen = set()
        for u in urlset.findall(SM + "url"):
            loc = (u.findtext(SM + "loc") or "").strip()
            if not loc:
                self.err(rel, 4, "<url> без <loc>")
                continue
            if loc in seen:
                self.err(rel, 4, f"{loc} повторяется")
                continue
            seen.add(loc)
            if not LASTMOD.fullmatch((u.findtext(SM + "lastmod") or "").strip()):
                self.err(rel, 4, f"{loc}: нет <lastmod> в формате YYYY-MM-DD")
            for tag in ("changefreq", "priority"):
                if u.find(SM + tag) is not None:
                    self.err(rel, 4, f"{loc}: <{tag}> не используется (7.8)")
            target, e = self.site_file(loc)
            if e:
                self.err(rel, 4, f"{loc}: {e}")
            elif target not in alts_by_page:
                self.err(rel, 4, f"{loc}: не является страницей сайта с hreflang")
            elif self.docs[target].noindex():
                self.err(rel, 4, f"{loc}: страница с noindex не должна быть в sitemap")
            elif loc != page_url(target):
                self.err(rel, 4, f"{loc}: адрес отличается от canonical {page_url(target)}")
            else:
                alts = {x.get("hreflang", "").strip().lower(): x.get("href", "")
                        for x in u.findall(XHTML + "link") if x.get("rel") == "alternate"}
                if alts != alts_by_page[target]:
                    self.err(rel, 4, f"{loc}: xhtml:link hreflang не совпадают с hreflang на {target}")
        for url, r in indexable.items():
            if url not in seen:
                self.err(rel, 4, f"нет индексируемой страницы {url} ({r})")

    # П.5: внутренние href, src, srcset (и ссылки на noindex-страницы для п.4)
    def check_refs(self):
        for rel in self.pages:
            doc = self.docs[rel]
            indexable = rel != "404.html" and not doc.noindex()
            for tag, a, line in doc.elements:
                rels = set(a.get("rel", "").lower().split())
                for attr in ("href", "src", "srcset"):
                    if attr not in a or (tag == "link" and attr == "href" and rels & {"canonical", "alternate"}):
                        continue  # абсолютные адреса canonical и hreflang проверяются в п.4
                    if attr == "srcset":
                        values = [c.split()[0] for c in a[attr].split(",") if c.strip()]
                    else:
                        values = [a[attr].strip()]
                    for v in values:
                        target = self.check_ref(rel, line, attr, v)
                        if (target and tag == "a" and indexable and target != rel
                                and target in self.docs and self.docs[target].noindex()):
                            self.err(rel, 4, f'строка {line}: ссылка на noindex-страницу href="{v}"')
        # Адреса в site.webmanifest тоже должны вести на существующие файлы
        text = self.text("site.webmanifest")
        try:
            man = json.loads(text) if text is not None else None
        except json.JSONDecodeError:
            man = None  # ошибка разбора выводится в п.11
        if isinstance(man, dict):
            refs = [("start_url", man.get("start_url"))]
            refs += [("src", i.get("src")) for i in man.get("icons", []) if isinstance(i, dict)]
            for attr, v in refs:
                if isinstance(v, str):
                    self.check_ref("site.webmanifest", None, attr, v)

    def check_ref(self, rel, line, attr, v):
        """Проверка одного внутреннего адреса. Возвращает файл цели от корня или None."""
        where = (f"строка {line}: " if line else "") + f'{attr}="{v}"'
        if not v:
            self.err(rel, 5, f"{where}: пустой адрес")
            return None
        u = urlsplit(v)
        if v.startswith("//") or SCHEME.match(v):
            if u.netloc.lower() in SITE_HOSTS:
                self.err(rel, 5, f"{where}: внутренний адрес должен начинаться с /, а не с {u.scheme}://{u.netloc}")
            return None  # внешний адрес
        if not v.startswith(("/", "#")):
            self.err(rel, 5, f"{where}: путь не от корня, должен начинаться с / или #")
            return None
        if u.query:
            self.err(rel, 5, f"{where}: query во внутреннем адресе (версионирование запрещено, 7.10)")
        if v.startswith("#"):
            target = rel
        else:
            path = unquote(u.path)
            if attr == "href" and path.endswith("/index.html"):
                self.err(rel, 5, f"{where}: ссылка на index.html, нужен адрес каталога со / в конце")
            target, e = self.resolve(path)
            if e:
                self.err(rel, 5, f"{where}: {e}")
                return None
        frag = unquote(u.fragment)
        if frag and target in self.docs and frag not in self.docs[target].ids:
            self.err(rel, 5, f"{where}: якорь #{frag} не найден в {target}")
        return target

    # П.6: inline-скрипт темы и CSP
    def check_csp(self):
        csp_by_page, script_by_page = {}, {}
        for rel in self.pages:
            els = self.docs[rel].elements
            charset = next((i for i, (t, a, _) in enumerate(els) if t == "meta" and "charset" in a), None)
            csp = [i for i, (t, a, _) in enumerate(els)
                   if t == "meta" and a.get("http-equiv", "").strip().lower() == "content-security-policy"]
            if charset is None:
                self.err(rel, 6, "нет <meta charset>")
            if len(csp) != 1:
                self.err(rel, 6, f'<meta http-equiv="Content-Security-Policy"> должен быть один, найдено {len(csp)}')
            policy = els[csp[0]][1].get("content", "") if csp else ""
            if csp:
                csp_by_page[rel] = policy
                if charset is not None and csp[0] != charset + 1:
                    self.err(rel, 6, f"строка {els[csp[0]][2]}: CSP стоит не сразу после <meta charset>")

            inline = [s for s in self.docs[rel].scripts if is_exec(s[1])]
            if not inline:
                self.err(rel, 6, "нет inline-скрипта темы")
                continue
            for s in inline[1:]:
                self.err(rel, 6, f"строка {s[3]}: лишний inline-скрипт, разрешен только скрипт темы")
            idx, _, body, line = inline[0]
            script_by_page[rel] = body
            if csp and idx < csp[0]:
                self.err(rel, 6, f"строка {line}: inline-скрипт темы стоит до CSP")
            sheet = next((i for i, (t, a, _) in enumerate(els)
                          if t == "link" and "stylesheet" in a.get("rel", "").lower().split()), None)
            if sheet is not None and idx > sheet:
                self.err(rel, 6, f'строка {line}: inline-скрипт темы стоит после <link rel="stylesheet"> (строка {els[sheet][2]})')
            if csp:
                token = "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"
                directives = {d.split()[0].lower(): d.split()[1:] for d in policy.split(";") if d.split()}
                if token not in directives.get("script-src", directives.get("default-src", [])):
                    self.err(rel, 6, f"хэш inline-скрипта темы {token} отсутствует в script-src CSP")

        # Побайтное совпадение на всех страницах; эталон - самый частый вариант
        for name, values in (("inline-скрипт темы", script_by_page), ("CSP", csp_by_page)):
            if values:
                common = Counter(values.values()).most_common(1)[0][0]
                ref = "index.html" if values.get("index.html") == common else next(r for r, v in values.items() if v == common)
                for r, v in values.items():
                    if v != common:
                        self.err(r, 6, f"{name} побайтно отличается от {ref}")

    # П.7-8: запрещенная разметка и JS, атрибуты <img>
    def check_markup(self):
        for rel, doc in self.docs.items():
            for tag, a, line in doc.elements:
                if tag == "style":
                    self.err(rel, 7, f"строка {line}: тег <style>")
                bad = [k for k in a if k == "style" or k.startswith("on")]
                if bad:
                    self.err(rel, 7, f"строка {line}: <{tag}> с атрибутом {', '.join(bad)}")
            for _, a, body, line in doc.scripts:
                if is_exec(a):
                    self.check_js(rel, body, line)
        for rel in self.files:
            if rel.endswith((".js", ".mjs")):
                text = self.text(rel)
                if text is not None:
                    self.check_js(rel, text, 1)

        for rel in self.pages:
            imgs = [(a, line) for t, a, line in self.docs[rel].elements if t == "img"]
            high = [line for a, line in imgs if a.get("fetchpriority", "").strip().lower() == "high"]
            if len(high) > 1:
                self.err(rel, 8, f'fetchpriority="high" у {len(high)} изображений (строки {", ".join(map(str, high))}), допускается одно')
            for a, line in imgs:
                where = f'строка {line}: <img src="{a.get("src", "")}">'
                missing = [k for k in ("alt", "width", "height") if k not in a]
                if missing:
                    self.err(rel, 8, f"{where} без {', '.join(missing)}")
                for k in ("width", "height"):
                    if k in a and not re.fullmatch(r"[1-9]\d*", a[k].strip()):
                        self.err(rel, 8, f'{where}: {k}="{a[k]}" не положительное целое число')
                if a.get("fetchpriority", "").strip().lower() == "high":
                    if "loading" in a:
                        self.err(rel, 8, f'{where}: у изображения с fetchpriority="high" не должно быть loading')
                elif a.get("loading", "").strip().lower() != "lazy":
                    self.err(rel, 8, f'{where} без loading="lazy"')

    def check_js(self, rel, text, first_line):
        for n, s in enumerate(text.split("\n"), first_line):
            for m in JS_FORBIDDEN.finditer(s):
                self.err(rel, 7, f"строка {n}: запрещенная конструкция JS {m.group(0)}")

    # П.9-10: запрещенные символы и плейсхолдеры
    def check_text(self):
        for rel in self.files:
            p = Path(rel)
            chars = p.suffix in CHAR_EXT or p.name == "README.md"
            placeholders = p.suffix in PLACEHOLDER_EXT and not rel.startswith("tools/")
            text = self.text(rel) if chars or placeholders else None
            if text is None:
                continue
            for n, line in enumerate(text.split("\n"), 1):
                if chars:
                    found = sorted({f"U+{ord(c):04X}" for c in FORBIDDEN_CHARS.findall(line)})
                    if found:
                        self.err(rel, 9, f"строка {n}: запрещенный символ {', '.join(found)}")
                if placeholders and "{{" in line:
                    self.err(rel, 10, f"строка {n}: плейсхолдер {{{{ вне tools/")

    # П.11: JSON-LD и site.webmanifest
    def check_json(self):
        for rel in self.pages:
            for _, line, e in self.ld[rel]:
                if e:
                    self.err(rel, 11, f"строка {line}: JSON-LD не разбирается: {e}")
        rel = "site.webmanifest"
        text = self.text(rel)
        if text is None:
            if not (self.root / rel).is_file():
                self.err(rel, 11, "файл не найден")
            return
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            self.err(rel, 11, f"не разбирается как JSON: {e.msg}, строка {e.lineno}, столбец {e.colno}")

    def run(self):
        if not self.pages:
            self.err(".", 1, "не найдено ни одной HTML-страницы")
        self.check_pairs()
        self.check_seo()
        self.check_refs()
        self.check_csp()
        self.check_markup()
        self.check_text()
        self.check_json()
        return self.errors


def main():
    ap = argparse.ArgumentParser(description="Статическая проверка сайта inkov.dev")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="корень сайта, по умолчанию каталог на уровень выше скрипта")
    root = ap.parse_args().root.resolve()
    if not root.is_dir():
        print(f"{root}: каталог не найден")
        return 1
    site = Site(root)
    errors = site.run()
    for e in errors:
        print(e)
    sys.stdout.flush()  # итог в stderr выводится после списка ошибок
    if errors:
        print(f"check_site: ошибок: {len(errors)}", file=sys.stderr)
        return 1
    print(f"check_site: OK, HTML-страниц: {len(site.pages)}, файлов: {len(site.files)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
