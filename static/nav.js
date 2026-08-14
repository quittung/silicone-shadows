(() => {
  const root = document.querySelector('[data-app-nav]');
  if (!root) return;

  const nav = document.createElement('nav');
  nav.setAttribute('aria-label', 'Primary');
  const links = [
    ['editor', '/editor', 'Editor'],
    ['stats', '/stats', 'Stats'],
    ['compare', '/compare', 'Compare'],
    ['moderate', '/moderate', 'Moderate'],
  ];
  for (const [name, href, label] of links) {
    const link = document.createElement('a');
    link.href = href;
    link.textContent = label;
    link.classList.toggle('active', root.dataset.appNav === name);
    if (name === 'moderate') {
      link.id = 'moderate-link';
      link.hidden = true;
      const count = document.createElement('span');
      count.id = 'moderate-count';
      count.className = 'nav-count';
      count.textContent = '0';
      link.append(' ', count);
    }
    nav.appendChild(link);
  }

  const logout = document.createElement('button');
  logout.id = 'logout';
  logout.className = 'nav-logout';
  logout.type = 'button';
  logout.textContent = 'Log out';
  logout.hidden = true;
  root.append(nav, logout);

  const moderateLink = document.querySelector('#moderate-link');
  function setModerationCount(count) {
    document.querySelector('#moderate-count').textContent = count;
    moderateLink.setAttribute('aria-label', `Moderate, ${count} pending`);
  }
  async function refreshModerationCount() {
    if (moderateLink.hidden) return;
    const response = await fetch('/api/moderation/submissions');
    if (response.ok) setModerationCount((await response.json()).submissions.length);
  }
  window.setModerationCount = setModerationCount;
  window.refreshModerationCount = refreshModerationCount;

  window.appNavigationReady = (async () => {
    const response = await fetch('/api/session');
    if (!response.ok) return {hosted: false, user: null};
    const session = await response.json();
    logout.hidden = !session.user;
    moderateLink.hidden = !session.user?.reviewer;
    if (session.user?.reviewer) {
      await refreshModerationCount();
      setInterval(refreshModerationCount, 30_000);
    }
    return session;
  })();

  logout.addEventListener('click', async () => {
    logout.disabled = true;
    try {
      await window.beforeAppLogout?.();
      await fetch('/api/logout', {method: 'POST'});
      location.href = '/';
    } catch (_) {
      logout.disabled = false;
    }
  });
})();
