(() => {
  const root = document.querySelector('[data-app-nav]');
  if (!root) return;

  const nav = document.createElement('nav');
  nav.setAttribute('aria-label', 'Primary');
  const authenticatedLinks = [];
  const links = [
    ['home', '/', 'Home'],
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
    if (name === 'editor' || name === 'stats') {
      link.hidden = true;
      authenticatedLinks.push(link);
    }
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
  const catalogButton = document.createElement('button');
  catalogButton.type = 'button';
  catalogButton.className = 'nav-catalog';
  catalogButton.hidden = true;
  catalogButton.setAttribute('aria-haspopup', 'dialog');
  root.insertBefore(catalogButton, nav);
  const catalogDialog = document.createElement('dialog');
  catalogDialog.className = 'catalog-dialog';
  catalogDialog.setAttribute('aria-labelledby', 'catalog-title');
  catalogDialog.innerHTML = `<h2 id="catalog-title">Toybox catalog</h2>
    <p data-catalog-status role="status" aria-live="polite"></p>
    <p data-catalog-checked class="catalog-note"></p>
    <p data-catalog-error role="alert"></p>
    <div><button type="button" data-catalog-check>Check now</button>
    <button type="button" data-catalog-update hidden>Update catalog</button>
    <button type="button" data-catalog-close>Close</button></div>`;
  document.body.append(catalogDialog);
  const catalogStatus = catalogDialog.querySelector('[data-catalog-status]');
  const catalogChecked = catalogDialog.querySelector('[data-catalog-checked]');
  const catalogError = catalogDialog.querySelector('[data-catalog-error]');
  const catalogCheck = catalogDialog.querySelector('[data-catalog-check]');
  const catalogUpdate = catalogDialog.querySelector('[data-catalog-update]');
  let catalogBusy = false;
  function showCatalog(data) {
    const newer = data.latest_version > data.current_version;
    catalogButton.hidden = false;
    catalogButton.textContent = `v${data.current_version}`;
    const label = `Toybox catalog v${data.current_version}` +
      (newer ? ` — v${data.latest_version} available` :
        data.error ? ' — check failed' : data.checked_at ? ' — up to date' : ' — check pending');
    catalogButton.title = label;
    catalogButton.setAttribute('aria-label', label);
    catalogButton.classList.toggle('catalog-new', newer);
    catalogStatus.textContent = `Using v${data.current_version}. ` +
      (newer ? `v${data.latest_version} is available.` :
        data.error ? '' : data.checked_at ? 'Up to date.' : 'First check is pending.');
    catalogChecked.textContent = 'Checks daily' + (data.checked_at
      ? ` · Last checked ${new Date(data.checked_at * 1000).toLocaleString()}` : '');
    catalogError.textContent = data.error ? `Update check failed: ${data.error}` : '';
    catalogUpdate.hidden = !newer;
    catalogUpdate.textContent = `Update to v${data.latest_version}`;
  }
  async function refreshCatalog() {
    if (catalogBusy) return;
    try {
      const response = await fetch('/api/catalog');
      if (response.ok) showCatalog(await response.json());
    } catch (_) { /* Retry on the next status refresh. */ }
  }
  catalogButton.addEventListener('click', () => {
    catalogDialog.showModal();
    refreshCatalog();
  });
  catalogDialog.querySelector('[data-catalog-close]').addEventListener('click', () => catalogDialog.close());
  async function catalogAction(action) {
    catalogBusy = true;
    catalogCheck.disabled = catalogUpdate.disabled = true;
    catalogError.textContent = '';
    catalogStatus.textContent = action === 'update' ? 'Updating catalog…' : 'Checking for updates…';
    try {
      if (action === 'update') await window.beforeCatalogUpdate?.();
      const response = await fetch(`/api/catalog/${action}`, {method: 'POST'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Catalog request failed');
      showCatalog(data);
      if (action === 'update') location.reload();
    } catch (error) {
      catalogError.textContent = error.message;
    } finally {
      catalogBusy = false;
      catalogCheck.disabled = catalogUpdate.disabled = false;
    }
  }
  catalogCheck.addEventListener('click', () => catalogAction('check'));
  catalogUpdate.addEventListener('click', () => catalogAction('update'));

  function centerActive() {
    const active = nav.querySelector('.active');
    if (!active) return;
    const container = root.getBoundingClientRect();
    const tab = active.getBoundingClientRect();
    root.scrollLeft += tab.left - container.left - (container.width - tab.width) / 2;
  }
  requestAnimationFrame(centerActive);

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
    for (const link of authenticatedLinks) {
      link.hidden = session.hosted && !session.user;
    }
    requestAnimationFrame(centerActive);
    logout.hidden = !session.user;
    moderateLink.hidden = !session.user?.reviewer;
    if (session.user?.reviewer) {
      await refreshModerationCount();
      setInterval(refreshModerationCount, 30_000);
    }
    if (!session.hosted || session.user?.reviewer) {
      await refreshCatalog();
      setInterval(refreshCatalog, 60_000);
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
