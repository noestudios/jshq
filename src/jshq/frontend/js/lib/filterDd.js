/* Dropdown filter pills (the "dd-pill" convention, extracted from jobs.js in
   7b2 so companies can share it). Stateless: every function takes the view's
   current `filters` object; event dispatch stays in each view so the repaint
   strategy (repaintList vs paint) remains local.

   Config per pill:
     { key, label, type: "multi" | "radio",
       options: [{ value, label }],
       switches?: [{ key, label, default }],    // radio-only extra rows
       footer?: { label, action } }             // panel footer link (data-action)

   - multi: filters[key] is a Set; empty Set = filter off = show all.
   - radio: filters[key] is a scalar; the "" option is the off state and the
     pill label swaps to the chosen option's label while it's active.
   - switches: independent booleans rendered after a divider in the panel
     (e.g. salary's "comp unknown ok"); a switch deviating from its default
     marks the pill active even when the radio value is off. */

import { esc, hidePop } from "./ui.js";

export function ddActive(dd, filters) {
  if (dd.type === "radio") {
    return (
      filters[dd.key] !== "" ||
      (dd.switches || []).some((sw) => filters[sw.key] !== sw.default)
    );
  }
  return filters[dd.key].size > 0;
}

/* Multi: a hidden in-flow sizer ("LABEL · 9") pins the pill's width to its
   widest (counted) state, and the visible content lives on an absolutely
   positioned, flex-centered layer on top. Because the outer size comes from
   the sizer, the live layer can reflow freely: animating the count's
   max-width 0 ↔ open slides the label off-center to make room — real motion,
   constant pill width by construction. Counts are single-digit by
   construction — the largest option list (level band) has 7 entries.
   Radio: every showable label (the pill's own + each non-empty option) is
   stacked in one grid cell, all hidden except the current one, so the pill is
   always as wide as its widest label and stays centered. */
export function ddButtonHtml(dd, filters) {
  if (dd.type === "radio") {
    const current = filters[dd.key];
    const spans = [
      { label: dd.label, on: current === "" },
      ...dd.options
        .filter((o) => o.value !== "")
        .map((o) => ({ label: o.label, on: o.value === current })),
    ];
    return `<span class="filter-dd-stack">${spans
      .map((s) => `<span${s.on ? ' class="on"' : ""}>${esc(s.label)}</span>`)
      .join("")}</span>`;
  }
  const n = filters[dd.key].size;
  return (
    `<span class="dd-sizer" aria-hidden="true">${esc(dd.label)}&nbsp;· 9</span>` +
    `<span class="dd-live">${esc(dd.label)}<span class="filter-dd-count">&nbsp;· ${n || 0}</span></span>`
  );
}

/* Mutates the existing spans instead of rebuilding (innerHTML would create
   fresh nodes, and fresh nodes render at their final state — the count
   slide / label crossfade / background ease never fire). The radio branch's
   `values` order MUST mirror the span order ddButtonHtml emits above: the
   pill's own label first, then the non-empty options in config order. */
export function updateToggle(root, dd, filters) {
  const toggle = root.querySelector(`.filter-dd-toggle[data-dd="${dd.key}"]`);
  if (dd.type === "radio") {
    const current = filters[dd.key];
    const values = ["", ...dd.options.filter((o) => o.value !== "").map((o) => o.value)];
    toggle.querySelectorAll(".filter-dd-stack > span").forEach((span, i) => {
      span.classList.toggle("on", values[i] === current);
    });
  } else {
    const n = filters[dd.key].size;
    // on n → 0 keep the last number while the count collapses shut — writing
    // "· 0" mid-animation would flash a lie
    if (n) toggle.querySelector(".filter-dd-count").innerHTML = `&nbsp;· ${n}`;
    toggle.classList.toggle("has-count", n > 0);
  }
  toggle.classList.toggle("active", ddActive(dd, filters));
}

export function optionsHtml(dd, filters) {
  if (dd.type === "radio") {
    return (
      dd.options
        .map(
          (o) => `
          <label class="filter-dd-option">
            <input type="radio" name="dd-${dd.key}" data-dd="${dd.key}" value="${esc(o.value)}"${filters[dd.key] === o.value ? " checked" : ""} />
            <span>${esc(o.label)}</span>
          </label>`
        )
        .join("") +
      (dd.switches || [])
        .map(
          (sw) => `
          <div class="filter-dd-divider"></div>
          <label class="filter-dd-option dd-switch">
            <input type="checkbox" data-dd="${dd.key}" data-switch="${sw.key}"${filters[sw.key] ? " checked" : ""} />
            <span>${esc(sw.label)}</span>
          </label>`
        )
        .join("") +
      (dd.footer
        ? `
          <div class="filter-dd-divider"></div>
          <button type="button" class="filter-dd-footer" data-action="${esc(dd.footer.action)}">${esc(dd.footer.label)}</button>`
        : "")
    );
  }
  // multi: "Clear all" header + checkbox rows ("Any …" plays that role for radios)
  return (
    `<button type="button" class="filter-dd-clear" data-action="dd-clear" data-dd="${dd.key}">Clear all</button>` +
    dd.options
      .map(
        (o) => `
          <label class="filter-dd-option">
            <input type="checkbox" data-dd="${dd.key}" value="${esc(o.value)}"${filters[dd.key].has(o.value) ? " checked" : ""} />
            <span>${esc(o.label)}</span>
          </label>`
      )
      .join("")
  );
}

export function ddTemplate(dd, filters) {
  const hasCount = dd.type !== "radio" && filters[dd.key].size > 0;
  return `
    <div class="filter-dd">
      <button class="chip filter-dd-toggle${ddActive(dd, filters) ? " active" : ""}${hasCount ? " has-count" : ""}" data-action="dd-toggle" data-dd="${dd.key}">${ddButtonHtml(dd, filters)}</button>
      <div class="filter-dd-panel" data-dd="${dd.key}" hidden>
        ${optionsHtml(dd, filters)}
      </div>
    </div>`;
}

/* Mobile summary pill ("Filters · N", N = active groups): same multi-pill
   anatomy as ddButtonHtml so the sizer/live/count animation stack applies
   unchanged. N is 0–7 by construction (one per group), so the single-digit
   "· 9" sizer convention holds. No data-dd — updateToggle's selector is
   [data-dd=…]-qualified, so the two never collide. */
export function summaryPillHtml(dds, filters) {
  const n = dds.filter((dd) => ddActive(dd, filters)).length;
  const cls = n > 0 ? " active has-count" : "";
  return (
    `<button type="button" class="chip filter-dd-toggle filters-summary${cls}" data-action="open-filter-sheet">` +
    `<span class="dd-sizer" aria-hidden="true">Filters&nbsp;· 9</span>` +
    `<span class="dd-live">Filters<span class="filter-dd-count">&nbsp;· ${n || 0}</span></span>` +
    `</button>`
  );
}

export function updateSummaryPill(root, dds, filters) {
  const pill = root.querySelector(".filters-summary");
  if (!pill) return;
  const n = dds.filter((dd) => ddActive(dd, filters)).length;
  // same rule as updateToggle: on n → 0 keep the last number while collapsing
  if (n) pill.querySelector(".filter-dd-count").innerHTML = `&nbsp;· ${n}`;
  pill.classList.toggle("has-count", n > 0);
  pill.classList.toggle("active", n > 0);
}

export function closeDropdowns(root, except = null) {
  root.querySelectorAll(".filter-dd-panel").forEach((p) => {
    if (p.dataset.dd !== except) hidePop(p);
  });
}

/* Clicking anywhere outside an open dropdown closes it — document-level so
   clicks on the topbar/nav (outside the view's root) also close panels. Each
   consuming view registers once with a getter for its root; the guard makes
   the listener a no-op while another view is mounted. */
export function bindOutsideClose(getRoot) {
  document.addEventListener("click", (event) => {
    const root = getRoot();
    if (root && root.isConnected && !event.target.closest(".filter-dd")) {
      closeDropdowns(root);
    }
  });
}
