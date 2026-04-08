/**
 * Shared API helper and app state — imported by all other modules
 */
export const state = {
  activeTab: localStorage.getItem('reef_tab') || 'discovery',
  uptimeStart: Date.now(),
  stats: null,
};

export async function api(path, opts = {}) {
  const script = document.querySelector('script[type="module"][src]') || document.querySelector('script[type="module"]');
  const src = script?.src || '';
  const idx = src.indexOf('/static/');
  const base = idx >= 0 ? src.slice(0, idx) : '';
  const url = base + path;
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  return res.json();
}
