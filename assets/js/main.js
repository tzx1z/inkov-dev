// Прогрессивное улучшение: без этого файла сайт работает полностью.
const root = document.documentElement;

// 1. Тема: цикл авто -> светлая -> темная -> авто, выбор хранится в localStorage
const themeButton = document.getElementById('theme-toggle');
if (themeButton) {
  const { labelAuto: auto, labelLight: light, labelDark: dark } = themeButton.dataset;
  const labels = { auto, light, dark };
  const next = { auto: 'light', light: 'dark', dark: 'auto' };
  const text = themeButton.querySelector('.theme-toggle__text');
  const current = () => root.dataset.theme || 'auto';
  const apply = (theme) => {
    if (theme === 'light' || theme === 'dark') root.dataset.theme = theme;
    else delete root.dataset.theme;
    text.textContent = labels[current()];
  };

  themeButton.addEventListener('click', () => {
    const theme = next[current()];
    apply(theme);
    try {
      if (theme === 'auto') localStorage.removeItem('theme');
      else localStorage.setItem('theme', theme);
    } catch {
      // localStorage недоступен: тема только на этой странице
    }
  });

  // страница из bfcache: тема могла смениться
  addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    try { apply(localStorage.getItem('theme')); } catch { /* localStorage недоступен */ }
  });

  apply(root.dataset.theme);
  themeButton.hidden = false;
}

// 2. Язык: парная страница открывается на том же разделе
for (const link of document.querySelectorAll('.lang a')) {
  link.addEventListener('click', () => { link.hash = location.hash; });
}

// 3. Копирование адресов (email, XMPP): кнопки видны, только если доступна запись в буфер
const copyStatus = document.getElementById('copy-status');
if (copyStatus && window.isSecureContext && navigator.clipboard?.writeText) {
  let timer;
  for (const button of document.querySelectorAll('.copy-btn')) {
    // адрес берется из ссылки в той же ячейке без схемы (mailto:, xmpp:)
    const link = button.parentElement.querySelector('a.contact');
    if (!link) continue;
    const address = link.href.slice(link.protocol.length);
    button.hidden = false;
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(address);
        copyStatus.textContent = button.dataset.done;
        clearTimeout(timer);
        timer = setTimeout(() => { copyStatus.textContent = ''; }, 2000);
      } catch {
        // запись запрещена: адрес доступен по ссылке
      }
    });
  }
}

// 4. Страница 404: показать запрошенный путь
const requestedPath = document.getElementById('requested-path');
if (requestedPath) {
  let path = location.pathname;
  try {
    path = decodeURIComponent(path);
  } catch {
    // некорректная кодировка: путь выводится как есть
  }
  requestedPath.textContent = path.slice(0, 120);
}
