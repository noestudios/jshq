/* Compose modal: pick an intent, generate a draft in the user's voice,
   copy it out. The app only drafts — all sending is manual, outside the app.
   Every generate logs a compose activity server-side. */

import { api } from "../api.js";
import { closeModal, esc, isMac, openModal, toast } from "./ui.js";

const INTENTS = [
  { value: "thank_you", label: "thank-you" },
  { value: "follow_up", label: "follow-up" },
  { value: "linkedin_comment", label: "LinkedIn comment" },
  { value: "connection_note", label: "connection note" },
  { value: "reconnect", label: "reconnect" },
  { value: "outreach", label: "outreach" },
  { value: "application_answer", label: "application answer" },
];

export function openComposeModal({ entity_type, entity_id, entity_label, onLogged } = {}) {
  // A generated-then-edited draft lives only in the textarea (nothing persists
  // it), so an accidental backdrop-click or Escape would silently drop invested
  // edits. Confirm inline before those two vectors close a dirty draft. The
  // explicit Cancel and Regenerate stay deliberate and unguarded.
  let savedFooter = null;
  const overlay = openModal({
    title: "Compose",
    beforeClose: () => {
      if (savedFooter !== null) return true; // confirm already up — a repeat Esc/backdrop discards
      if (!draftIsDirty()) return true;
      showDiscardConfirm();
      return false;
    },
    body: `
      <div class="form-field">
        <label>Intent</label>
        <select name="intent">
          ${INTENTS.map((i) => `<option value="${i.value}">${i.label}</option>`).join("")}
        </select>
      </div>
      <div class="form-field" data-role="question-field" hidden>
        <label>Application question <span class="req-mark" aria-hidden="true">*</span></label>
        <textarea name="question" rows="2" placeholder="Paste the question, including any word limit."></textarea>
      </div>
      ${entity_label ? `<div class="form-field"><label>Linked to</label><div class="linked-entity">${esc(entity_label)}</div></div>` : ""}
      <div class="form-field">
        <label>Instructions (optional)</label>
        <textarea name="instructions" rows="2" placeholder="Anything specific to mention, avoid, or aim for."></textarea>
      </div>
      <div data-role="draft-area"></div>`,
    footer: `
      <button type="button" class="btn" data-action="modal-close">Cancel</button>
      <button type="button" class="btn" data-role="refine" title="Rewrite to remove AI tells" hidden>Refine</button>
      <button type="button" class="btn" data-role="copy" hidden>Copy</button>
      <button type="submit" class="btn btn-accent" data-role="generate">Generate</button>`,
    onSubmit: async (form) => {
      const intent = form.intent.value;
      const question = form.question.value.trim();
      if (intent === "application_answer" && !question) {
        toast("Paste the application question first.", { error: true });
        return;
      }
      const button = form.querySelector("[data-role='generate']");
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "Generating…";
      try {
        const result = await api.compose({
          intent,
          entity_type,
          entity_id,
          instructions: form.instructions.value.trim() || null,
          question: intent === "application_answer" ? question : null,
        });
        const area = form.querySelector("[data-role='draft-area']");
        area.innerHTML = `
          <div class="form-field">
            <label>Draft — edit freely, then copy</label>
            <textarea name="draft" class="compose-draft" rows="12">${esc(result.draft)}</textarea>
          </div>`;
        form.querySelector("[data-role='copy']").hidden = false;
        form.querySelector("[data-role='refine']").hidden = false;
        button.textContent = "Regenerate";
        if (onLogged) onLogged();
      } catch (error) {
        toast(error.detail || error.message, { error: true });
        button.textContent = label;
      } finally {
        button.disabled = false;
      }
    },
  });

  function draftIsDirty() {
    const draft = overlay.querySelector("[name='draft']");
    return !!draft && draft.value.trim().length > 0;
  }

  // Confirm inline (the footer), not via confirmModal — that would replace this
  // modal. Swap the footer for a discard/keep prompt; Keep editing restores it.
  function showDiscardConfirm() {
    const foot = overlay.querySelector(".modal-foot");
    if (savedFooter === null) savedFooter = foot.innerHTML;
    foot.innerHTML = `
      <span class="modal-confirm-msg">Discard your edited draft?</span>
      <button type="button" class="btn" data-role="keep-editing">Keep editing</button>
      <button type="button" class="btn btn-danger" data-role="discard-draft">Discard</button>`;
  }

  function restoreFooter() {
    if (savedFooter === null) return;
    overlay.querySelector(".modal-foot").innerHTML = savedFooter;
    savedFooter = null;
  }

  overlay.addEventListener("click", (event) => {
    if (event.target.closest("[data-role='keep-editing']")) {
      restoreFooter();
    } else if (event.target.closest("[data-role='discard-draft']")) {
      closeModal(); // closeModal is the raw close — it never re-runs beforeClose
    }
  });

  // Question field only applies to application_answer.
  const intentSelect = overlay.querySelector("[name='intent']");
  intentSelect.addEventListener("change", () => {
    overlay.querySelector("[data-role='question-field']").hidden =
      intentSelect.value !== "application_answer";
  });

  overlay.addEventListener("click", async (event) => {
    if (!event.target.closest("[data-role='copy']")) return;
    const draft = overlay.querySelector("[name='draft']");
    if (!draft) return;
    try {
      await navigator.clipboard.writeText(draft.value);
      toast("Draft copied");
    } catch {
      // Clipboard API is blocked on non-HTTPS LAN origins; hand off to the
      // platform's copy shortcut.
      draft.focus();
      draft.select();
      toast(`Clipboard blocked, press ${isMac ? "⌘C" : "Ctrl+C"} to copy the selected draft`);
    }
  });

  // Opt-in AI-tell scrub: rewrite the current draft in place (one Sonnet call).
  overlay.addEventListener("click", async (event) => {
    const btn = event.target.closest("[data-role='refine']");
    if (!btn) return;
    const draft = overlay.querySelector("[name='draft']");
    if (!draft || !draft.value.trim()) return;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Refining…";
    try {
      const result = await api.refineTells({ text: draft.value });
      draft.value = result.refined_text;
      const fixed = result.tells_fixed?.length ? ` (fixed: ${result.tells_fixed.join(", ")})` : "";
      toast(`Refined: reads-as-human ${result.score ?? "?"}/10${fixed}`);
    } catch (error) {
      toast(error.detail || error.message, { error: true });
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
}
