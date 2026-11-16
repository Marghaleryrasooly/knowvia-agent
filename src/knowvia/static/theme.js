/* Appearance only: no identity, API, credential or conversation access. */
(() => {
  'use strict';
  const key = 'knowvia.appearance';
  const valid = value => ['light', 'dark', 'system'].includes(value);
  let preference = 'light';
  try { const saved = localStorage.getItem(key); if (valid(saved)) preference = saved; } catch (_) { /* Private/restricted storage: session-only choice. */ }
  const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null;
  function apply() {
    document.documentElement.dataset.appearance = preference;
    document.documentElement.dataset.theme = preference === 'system' ? (media?.matches ? 'dark' : 'light') : preference;
    document.querySelectorAll('[data-appearance-select]').forEach(select => { select.value = preference; });
    document.querySelectorAll('[data-appearance-value]').forEach(button => {
      const selected = button.dataset.appearanceValue === preference;
      button.setAttribute('aria-checked', String(selected)); button.tabIndex = selected ? 0 : -1;
    });
  }
  function choose(value) {
    preference = valid(value) ? value : 'light';
    try { localStorage.setItem(key, preference); } catch (_) { /* Session-only preference. */ }
    apply();
  }
  apply(); // Runs in the head, before page styles and first content paint.
  function mount() {
    document.querySelectorAll('[data-appearance-control]').forEach(host => {
      if (!host.hasAttribute('data-appearance-segments')) {
        const heading = document.createElement('span'); heading.className = 'appearance-heading'; heading.textContent = 'Appearance';
        const group = document.createElement('div'); group.className = 'appearance-segments';
        group.setAttribute('role', 'radiogroup'); group.setAttribute('aria-label', 'Appearance'); group.setAttribute('aria-orientation', 'horizontal');
        const icons = {
          light: [['circle', {cx:12, cy:12, r:4}], ['path', {d:'M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5'}]],
          dark: [['path', {d:'M20 15.5A9 9 0 0 1 8.5 4 9 9 0 1 0 20 15.5Z'}]],
          system: [['rect', {x:3, y:4, width:18, height:13, rx:2}], ['path', {d:'M8 21h8m-4-4v4'}]]
        };
        for (const value of ['light', 'dark', 'system']) {
          const button = document.createElement('button'); button.type = 'button'; button.dataset.appearanceValue = value; button.setAttribute('role', 'radio');
          const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
          for (const [tag, attributes] of icons[value]) {
            const shape = document.createElementNS(svg.namespaceURI, tag);
            for (const [key, attribute] of Object.entries(attributes)) shape.setAttribute(key, attribute);
            svg.append(shape);
          }
          const label = document.createElement('span'); label.textContent = value[0].toUpperCase() + value.slice(1);
          button.append(svg, label); group.append(button);
        }
        host.replaceChildren(heading, group); host.setAttribute('data-appearance-segments', '');
      }
      if (host.hasAttribute('data-appearance-segments')) {
        const buttons = Array.from(host.querySelectorAll('[data-appearance-value]'));
        buttons.forEach((button, index) => {
          button.addEventListener('click', () => choose(button.dataset.appearanceValue));
          button.addEventListener('keydown', event => {
            let next;
            if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % buttons.length;
            else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index + buttons.length - 1) % buttons.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = buttons.length - 1;
            if (next === undefined) return;
            event.preventDefault(); choose(buttons[next].dataset.appearanceValue); buttons[next].focus();
          });
        });
        return;
      }
    });
  }
  function initializeControls() { mount(); apply(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initializeControls, {once:true}); else initializeControls();
  if (media?.addEventListener) media.addEventListener('change', () => { if (preference === 'system') apply(); });
  else if (media?.addListener) media.addListener(() => { if (preference === 'system') apply(); });
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    preference = valid(event.newValue) ? event.newValue : 'light'; apply();
  });
})();
