/* Help view (Phase 9): renders docs/user-manual.md via mdToHtml, mirroring the
   read-only "How scoring works" rubric viewer. Static single-user doc — no id
   segment, no leave-guard. The view supplies the page H1; the markdown body
   starts at "#" so its sections render as .md-doc h2/h3 under it. */

import { api } from "../api.js";
import { mdToHtml, renderLoading, renderLoadError, setStats } from "../lib/ui.js";

let cached = null; // markdown text; cleared on a full page reload

async function load() {
  if (cached === null) cached = (await api.getUserManual()).markdown;
  return cached;
}

export async function render(container) {
  renderLoading(container);
  // Every other view stamps the header strip on mount; without this the manual
  // inherits whatever the previous view left there (Calendar's "8 pending"),
  // which reads as a stat about the page you're looking at.
  setStats([]);
  let markdown;
  try {
    markdown = await load();
  } catch (error) {
    renderLoadError(container, error, () => render(container));
    return;
  }
  container.innerHTML = `
    <div class="help-view">
      <div class="help-inner">
        <h1 class="help-h1">User manual</h1>
        <div class="md-doc">${mdToHtml(markdown)}</div>
        <div class="help-credits">
          <p>Job Search HQ is built by
            <a href="https://noestudios.com" target="_blank" rel="noopener">Chris Hays</a> ·
            <a href="https://github.com/noestudios/jshq" target="_blank" rel="noopener">source on GitHub</a> (AGPL-3.0).</p>
          <p>The ranked wish list and the fulfillment matrix in onboarding are
            adapted from tier-list ranking and fulfillment-matrix exercises by
            <a href="https://www.kristinmchen.com" target="_blank" rel="noopener">Kristin Chen</a>
            (career coaching, startup advising, and fractional CPO services at
            www.kristinmchen.com). Thank you, Kristin.</p>
        </div>
      </div>
    </div>`;
}
