// jobbot helper: content script, injected into the current tab when you click the button.
// Reads the empty form fields on this page, asks the local jobbot server for answers (same
// rules and drafter as the bot), fills them, and shows what still needs you.
// It never clicks Next or Submit, and never changes a field that already has a value.

(() => {
  const VERSION = 4;
  if (window.__jobbot && window.__jobbot.version === VERSION) { window.__jobbot.run(); return; }

  // ---------------------------------------------------------------- helpers
  // A step-by-step log of what the filler tried, for troubleshooting: run
  // `__jobbot.log` in the page's DevTools console after a fill.
  const log = [];
  const debug = (line) => { log.push(line); if (log.length > 400) log.shift(); };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/\*/g, "").trim();
  const ask = (path, body) => new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ path, body }, (reply) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (reply && reply.ok) resolve(reply.data); else reject(new Error(reply ? reply.error : "no reply"));
    });
  });
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const textOfIds = (ids) => clean((ids || "").split(/\s+/).map((id) => {
    const e = document.getElementById(id); return e ? e.innerText : "";
  }).join(" "));
  const inPanel = (el) => !!el.closest("#jobbot-panel-host");

  function rawLabel(el) {
    if (el.getAttribute("aria-labelledby")) {
      const t = (el.getAttribute("aria-labelledby") || "").split(/\s+/)
        .map((id) => (document.getElementById(id) || {}).innerText || "").join(" ");
      if (clean(t)) return t;
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && clean(l.innerText)) return l.innerText;
    }
    const wrap = el.closest("label");
    if (wrap && clean(wrap.innerText)) return wrap.innerText;
    const wd = el.closest('[data-automation-id^="formField-"]'); // Workday
    if (wd) { const l = wd.querySelector("label, legend"); if (l && clean(l.innerText)) return l.innerText; }
    const fs = el.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg && clean(lg.innerText)) return lg.innerText; }
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    let node = el;
    for (let i = 0; i < 4 && node; i++) {
      node = node.parentElement;
      if (!node) break;
      const lab = node.querySelector("label, legend, [class*='label'], [class*='Label']");
      if (lab && !lab.contains(el) && clean(lab.innerText)) return lab.innerText;
    }
    return el.placeholder || "";
  }

  // File inputs are often labelled by their button ("Attach", "Upload"); the real label is the
  // section heading above them ("Resume/CV", "Cover Letter").
  // "Autofill from resume" boxes (Ashby and others) read the resume and then refill the whole
  // form a few seconds later, wiping what was typed. Only the real Resume field gets the file.
  function isAutofillBox(el) {
    let node = el.parentElement;
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      const t = clean(node.innerText).slice(0, 200);
      if (/(resume|\bcv\b)/i.test(t)) return /auto-?fill|parse (your|my) resume|import (from|your) resume/i.test(t);
    }
    return false;
  }

  // Yes / No asked with two buttons instead of radio inputs (Ashby).
  const isPressed = (b) => b.getAttribute("aria-pressed") === "true" || /active|selected|checked/i.test(b.className || "");
  function buttonGroupLabel(group) {
    let node = group;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const lab = [...node.querySelectorAll("label, legend, [class*='question-title'], [class*='label']")]
        .find((l) => !group.contains(l) && clean(l.innerText) && !/^(yes|no)$/i.test(clean(l.innerText)));
      if (lab) return lab.innerText;
    }
    return "";
  }

  function fileLabel(el) {
    let node = el.parentElement;
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      const t = clean(node.innerText).slice(0, 160);
      const m = t.match(/(resume\s*\/?\s*cv|resume|\bcv\b|cover letter|transcript|portfolio|writing sample|work sample)/i);
      if (m) return m[0];
    }
    return "";
  }

  function optionText(input) {
    if (input.id) {
      const l = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
      if (l && clean(l.innerText)) return clean(l.innerText);
    }
    const w = input.closest("label");
    if (w && clean(w.innerText)) return clean(w.innerText);
    if (input.getAttribute("aria-label")) return clean(input.getAttribute("aria-label"));
    return clean(input.value !== "on" ? input.value : "");
  }

  function groupRawLabel(inputs) {
    const first = inputs[0];
    const group = first.closest('[role="radiogroup"], [role="group"]');
    if (group) {
      const t = textOfIds(group.getAttribute("aria-labelledby")) || clean(group.getAttribute("aria-label"));
      if (t) return t;
    }
    const fs = first.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg && clean(lg.innerText)) return lg.innerText; }
    const wd = first.closest('[data-automation-id^="formField-"]');
    if (wd) { const l = wd.querySelector("label, legend"); if (l && !l.contains(first) && clean(l.innerText)) return l.innerText; }
    const opts = new Set(inputs.map(optionText));
    let node = first.parentElement;
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      if (!inputs.every((x) => node.contains(x))) continue;
      const lab = [...node.querySelectorAll("label, legend, p, span, div")].find((e) =>
        !inputs.some((x) => e.contains(x)) && clean(e.innerText).length > 3 && !opts.has(clean(e.innerText)));
      if (lab) return lab.innerText;
    }
    return "";
  }

  const isRequired = (el, raw) => el.required || el.getAttribute("aria-required") === "true" ||
    /\*|\(required\)/i.test(raw || "");

  function setValue(el, value, leave = true) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
      : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value); // React-safe
    el.dispatchEvent(new Event("input", { bubbles: true }));
    if (!leave) return; // type-ahead boxes clear an unpicked value on change/blur
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function bestIndex(options, wanted) {
    const low = options.map((o) => clean(o).toLowerCase());
    const w = clean(wanted).toLowerCase();
    let i = low.indexOf(w);
    if (i >= 0) return i;
    const starts = low.map((o, k) => (o.startsWith(w) ? k : -1)).filter((k) => k >= 0);
    if (starts.length === 1) return starts[0];
    const contains = low.map((o, k) => (o.includes(w) ? k : -1)).filter((k) => k >= 0);
    if (contains.length === 1) return contains[0];
    return -1;
  }

  // A full press: many dropdowns and type-ahead lists act on mousedown, not click.
  function press(el) {
    const opts = { bubbles: true, cancelable: true, view: window, button: 0 };
    el.dispatchEvent(new PointerEvent("pointerdown", opts));
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    el.dispatchEvent(new PointerEvent("pointerup", opts));
    el.dispatchEvent(new MouseEvent("mouseup", opts));
    el.click();
  }

  const openListbox = () => [...document.querySelectorAll('[role="listbox"]')].find((l) => visible(l) && !inPanel(l));
  async function waitFor(fn, ms = 2500) {
    for (let t = 0; t < ms; t += 100) { const v = fn(); if (v) return v; await sleep(100); }
    return null;
  }
  const escape = () => document.activeElement && document.activeElement.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true }));

  // ---------------------------------------------------------------- scan
  const registry = new Map();

  // Searchable dropdowns (react-select and similar) only render options when opened.
  // Search-as-you-type boxes (location, school) have none until you type; that's fine.
  const controlOf = (el) => el.closest('[class*="control"], [class*="Control"]') || el;

  // The value a custom dropdown shows once something is picked (react-select empties the input).
  function shownValue(el) {
    const c = controlOf(el);
    const v = c.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value"], [class*="multiValue"]');
    return clean(v ? v.innerText : el.value);
  }

  async function openCombobox(el) {
    press(controlOf(el)); // react-select opens on a mouse press on its control, not on ArrowDown
    const listId = await waitFor(() => el.getAttribute("aria-controls") || el.getAttribute("aria-owns"), 800);
    return listId ? waitFor(() => { const l = document.getElementById(listId); return l && visible(l) ? l : null; }, 800) : null;
  }

  async function readComboboxOptions(el) {
    const list = await openCombobox(el);
    const options = list ? [...list.querySelectorAll('[role="option"]')].map((o) => clean(o.innerText)).filter(Boolean) : [];
    escape();
    el.blur();
    await sleep(150);
    return options.length > 60 ? [] : options; // huge lists (countries) are searched by typing instead
  }

  async function scan() {
    registry.clear();
    const fields = [];
    let n = 0;
    const add = (field, ref) => { const key = `f${n++}`; registry.set(key, { ...ref, ...field }); fields.push({ key, ...field }); };

    for (const el of document.querySelectorAll("input, textarea, select")) {
      const type = (el.type || "").toLowerCase();
      if (inPanel(el) || ["hidden", "submit", "button", "reset", "image", "password", "radio", "checkbox"].includes(type)) continue;
      if (type !== "file" && (!visible(el) || el.disabled || el.readOnly)) continue;
      if (type === "search" && !el.closest('[data-automation-id="multiselectInputContainer"]')) continue;
      // Helper inputs behind custom dropdowns (react-select's required shadow input, phone-country search).
      if (type !== "file" && el.getAttribute("role") !== "combobox" &&
          (el.getAttribute("aria-hidden") === "true" || el.tabIndex === -1)) continue;
      const raw = type === "file" ? (fileLabel(el) || rawLabel(el)) : rawLabel(el);
      const label = clean(raw);
      if (/^(select|choose|search|type here)\W*$/i.test(label)) continue; // only a placeholder, no real label
      let kind = el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select"
        : type === "file" ? "file" : type === "date" ? "date"
        : (el.getAttribute("role") === "combobox" || type === "search") ? "combobox"
        : ["email", "tel", "url", "number"].includes(type) ? type : "text";
      let options = [];
      if (kind === "select") {
        if (el.selectedIndex > 0 && clean(el.options[el.selectedIndex].text) && !/^(select|choose|--)/i.test(el.options[el.selectedIndex].text)) continue;
        options = [...el.options].map((o) => clean(o.text)).filter((t) => t && !/^(select|choose|--)/i.test(t));
      } else if (kind !== "file" && el.value && el.value.trim()) {
        continue; // already filled (by you, autofill, or a parsed resume)
      }
      if (kind === "file" && el.files && el.files.length) continue;
      if (kind === "file" && isAutofillBox(el)) { debug("skip the 'autofill from resume' box (it would wipe typed answers)"); continue; }
      if (!label) continue;
      // Searchable dropdowns keep their typing box empty and show the choice elsewhere.
      if (kind === "combobox" && shownValue(el) && !/^(select|choose|start typing|type to search)/i.test(shownValue(el))) continue;
      if (kind === "combobox") options = await readComboboxOptions(el);
      add({ label, kind, options, required: isRequired(el, raw) }, { el });
    }

    // Radio and checkbox groups.
    const groups = new Map();
    for (const el of document.querySelectorAll('input[type="radio"], input[type="checkbox"]')) {
      if (inPanel(el) || !visible(el.closest("label") || el.parentElement || el)) continue;
      const container = el.closest('fieldset, [role="radiogroup"], [role="group"], [data-automation-id^="formField-"]');
      const gkey = el.type === "radio" ? `r:${el.name}` : container ? container : `c:${el.name || el.id}`;
      if (!groups.has(gkey)) groups.set(gkey, []);
      groups.get(gkey).push(el);
    }
    for (const inputs of groups.values()) {
      if (inputs.some((i) => i.checked)) continue;
      const type = inputs[0].type;
      const options = inputs.map(optionText);
      let raw = groupRawLabel(inputs);
      if (type === "checkbox" && inputs.length === 1) raw = raw || options[0];
      const label = clean(raw) || options.join(" / ");
      add({ label, kind: type === "radio" ? "radio" : "checkbox", options, required: inputs.some((i) => isRequired(i, raw)) },
          { inputs });
    }

    // Yes / No buttons (Ashby): two buttons in one group, neither pressed yet.
    const pairs = new Set();
    for (const btn of document.querySelectorAll("button")) {
      if (inPanel(btn) || !visible(btn) || !/^(yes|no)$/i.test(clean(btn.innerText))) continue;
      const group = btn.parentElement;
      if (!group || pairs.has(group)) continue;
      const buttons = [...group.querySelectorAll("button")].filter((b) => /^(yes|no)$/i.test(clean(b.innerText)));
      if (buttons.length !== 2) continue;
      pairs.add(group);
      if (buttons.some(isPressed)) continue;  // already answered
      const raw = buttonGroupLabel(group);
      if (!clean(raw)) continue;
      add({ label: clean(raw), kind: "buttons", options: buttons.map((b) => clean(b.innerText)), required: /\*/.test(raw) },
          { buttons, group });
    }

    // Workday-style dropdown buttons: open each briefly to read its options.
    for (const btn of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
      if (inPanel(btn) || !visible(btn) || btn.disabled) continue;
      const current = clean(btn.innerText);
      if (current && !/^select/i.test(current)) continue;
      const raw = rawLabel(btn);
      btn.click();
      const lb = await waitFor(openListbox, 2000);
      const options = lb ? [...lb.querySelectorAll('[role="option"]')].map((o) => clean(o.innerText)).filter(Boolean) : [];
      escape();
      await sleep(200);
      if (openListbox()) { btn.click(); await sleep(150); }
      add({ label: clean(raw) || clean(btn.getAttribute("aria-label")), kind: "listbox", options, required: isRequired(btn, raw) }, { el: btn });
    }
    return fields;
  }

  // ---------------------------------------------------------------- fill
  async function pickFromListbox(trigger, wanted) {
    trigger.click();
    const lb = await waitFor(openListbox, 2500);
    if (!lb) return false;
    const opts = [...lb.querySelectorAll('[role="option"]')];
    const i = bestIndex(opts.map((o) => o.innerText), wanted);
    if (i < 0) { escape(); return false; }
    opts[i].scrollIntoView({ block: "nearest" });
    press(opts[i]);
    await sleep(350);
    const now = clean(trigger.innerText);
    return !!now && !/^select/i.test(now);
  }

  async function attach(el, kind) {
    const data = await ask(`/api/ext/file?kind=${kind}&url=${encodeURIComponent(location.href)}`);
    const bytes = Uint8Array.from(atob(data.pdf), (c) => c.charCodeAt(0));
    const names = (lastPlan && lastPlan.file_names) || {};
    const name = names[kind] || (kind === "resume" ? "Resume.pdf" : "Cover_Letter.pdf");
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], name, { type: "application/pdf" }));
    el.files = dt.files;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  // Suggestion lists that appear under a text box after typing (location / school search).
  const PLACEHOLDER_RE = /^(loading|searching|no (options|results|matches)|type to search|start typing)/i;
  function suggestionsNear(el, before, beforeOptions = new Set()) {
    const box = el.getBoundingClientRect();
    const lists = [...document.querySelectorAll('[role="listbox"], .dropdown-results, .pac-container')]
      .filter((l) => visible(l) && !inPanel(l) && !before.has(l));
    for (const l of lists) {
      const r = l.getBoundingClientRect();
      if (r.top >= box.top - 10 && r.top - box.bottom < 400) {
        // "Loading…" / "No options" placeholders aren't results; keep waiting for real ones.
        let opts = [...l.querySelectorAll('[role="option"], .dropdown-location, .pac-item, :scope > div')]
          .filter((o) => visible(o) && !PLACEHOLDER_RE.test(clean(o.innerText)));
        opts = opts.filter((o) => !opts.some((x) => x !== o && o.contains(x)));  // single options, not their wrapper
        if (opts.length) return opts;
      }
    }
    // Some sites (Ashby) render the suggestions elsewhere on the page: any new option counts.
    const loose = [...document.querySelectorAll('[role="option"]')]
      .filter((o) => visible(o) && !inPanel(o) && !beforeOptions.has(o) && !PLACEHOLDER_RE.test(clean(o.innerText)));
    return loose.length ? loose : null;
  }

  async function typeText(el, value) {
    const before = new Set([...document.querySelectorAll('[role="listbox"], .dropdown-results, .pac-container')].filter(visible));
    const beforeOptions = new Set([...document.querySelectorAll('[role="option"]')].filter(visible));
    debug(`type "${value}" into #${el.id || el.name}: ${before.size} lists already open`);
    el.focus();
    // Type-ahead boxes (Lever's location, Greenhouse's school/city search) only react to
    // keystroke-by-keystroke input. Short values are typed in full; long ones (URLs, answers)
    // are set at once with only the last few characters typed.
    const tail = value.length <= 40 ? value.length : 3;
    if (tail < value.length) setValue(el, value.slice(0, value.length - tail), false);
    else setValue(el, "", false);
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    for (const ch of value.slice(value.length - tail)) {
      el.dispatchEvent(new KeyboardEvent("keydown", { key: ch, bubbles: true }));
      set.call(el, el.value + ch);
      el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
      el.dispatchEvent(new KeyboardEvent("keyup", { key: ch, bubbles: true }));
      await sleep(40);
    }
    const opts = await waitFor(() => suggestionsNear(el, before, beforeOptions), 4000);
    debug(`  value now "${el.value}", suggestions: ${opts ? opts.map((o) => clean(o.innerText)).slice(0, 3).join(" | ") : "none"}`);
    if (!opts) {  // plain text box: finish the edit normally
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("blur", { bubbles: true }));
      return clean(el.value) === clean(value);
    }
    // Exact match first; otherwise a suggestion whose city ("San Francisco") is the start of
    // what was typed ("San Francisco Bay Area, CA") or vice versa, preferring US results.
    const head = clean(value).toLowerCase().split(",")[0];
    const i = bestIndex(opts.map((o) => o.innerText), value);
    const near = opts.filter((o) => {
      const city = clean(o.innerText).toLowerCase().split(",")[0];
      return city && (head.startsWith(city) || city.startsWith(head));
    });
    const pick = (i >= 0 ? opts[i] : null) || near.find((o) => /\b(usa|united states|us)\b/i.test(o.innerText)) || near[0];
    if (!pick) return false;
    press(pick);
    await sleep(400);
    // Leave the field, then check: a site that rejects an unpicked value clears it here.
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    await sleep(300);
    debug(`  picked "${clean(pick.innerText)}", field now shows "${shownValue(el)}"`);
    const stillOpen = suggestionsNear(el, before, beforeOptions);
    return shownValue(el).length > 0 && !(stillOpen && stillOpen.includes(pick));
  }

  // Phone boxes with their own country picker (+1 flag widgets) want only the local number.
  // True when the dial code (+1) comes from somewhere else: a visible flag picker next to the box,
  // a control showing "+1", or a separate "Country" / "Country Phone Code" field on the form
  // (Greenhouse hides its flag picker and takes +1 from "Country").
  function hasDialCodePicker(el) {
    const flag = el.closest(".iti, .react-tel-input, .PhoneInput, .intl-tel-input") &&
      el.closest(".iti, .react-tel-input, .PhoneInput, .intl-tel-input").querySelector(".iti__selected-country, .flag-dropdown, .PhoneInputCountry");
    if (flag && visible(flag)) return true;
    const box = el.closest("[data-automation-id^='formField-'], .field, fieldset") || el.parentElement;
    if (box && [...box.querySelectorAll("select, button, [role=combobox]")].some((c) => c !== el && /\+\d{1,3}\b/.test(clean(c.innerText || c.value || "")))) return true;
    return !!el.closest(".iti") && [...document.querySelectorAll("label")].some((l) => /^(country( phone)?( code)?)$/i.test(clean(l.innerText)));
  }

  async function fill(field, value, plan) {
    const ref = registry.get(field.key);
    if (value === "__cover_letter__") value = plan.cover_letter || "";
    if (typeof value === "string" && /^\s*\+\d/.test(value) && ref && ref.el && (field.kind === "tel" || /phone|mobile/i.test(field.label)) && hasDialCodePicker(ref.el)) {
      value = value.replace(/^\s*\+\d{1,3}[\s.\-]*/, "");
      debug(`phone box has its own country picker: typing the local number ${value}`);
    }
    if (!value) return false;
    switch (field.kind) {
      case "text": case "email": case "tel": case "url": case "number":
        return typeText(ref.el, value);
      case "buttons": {
        const b = ref.buttons.find((x) => clean(x.innerText).toLowerCase() === String(value).trim().toLowerCase());
        if (!b) return false;
        press(b);
        await sleep(250);
        return isPressed(b) || !ref.buttons.some(isPressed);  // some sites show no pressed state
      }
      case "textarea": case "date":
        ref.el.focus(); setValue(ref.el, value); return clean(ref.el.value) === clean(value);
      case "select": {
        const i = bestIndex([...ref.el.options].map((o) => o.text), value);
        if (i < 0) return false;
        ref.el.selectedIndex = i;
        ref.el.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
      case "radio": case "checkbox": {
        let ok = true;
        for (const v of field.kind === "checkbox" ? value.split(" | ") : [value]) {
          const i = bestIndex(ref.inputs.map(optionText), v);
          if (i < 0) { ok = false; continue; }
          if (!ref.inputs[i].checked) ref.inputs[i].click();
          ok = ok && ref.inputs[i].checked;
        }
        return ok;
      }
      case "listbox":
        return pickFromListbox(ref.el, value);
      case "combobox": {
        if (field.options && field.options.length) { // a dropdown: open it and pick the option
          const list = await openCombobox(ref.el);
          if (!list) return false;
          const opts = [...list.querySelectorAll('[role="option"]')];
          const i = bestIndex(opts.map((o) => o.innerText), value);
          if (i < 0) { escape(); return false; }
          press(opts[i]);
          await sleep(350);
          return shownValue(ref.el).length > 0;
        }
        return typeText(ref.el, value); // a search box (location, school, country): type, then pick
      }
      default:
        return false;
    }
  }

  // What you've put in a field jobbot left for you (for "Save my answers").
  function currentValue(field) {
    const ref = registry.get(field.key);
    if (!ref || field.kind === "file") return "";
    if (ref.buttons) return clean((ref.buttons.find(isPressed) || {}).innerText || "");
    if (ref.inputs) {
      const on = ref.inputs.filter((i) => i.checked).map(optionText);
      return field.kind === "checkbox" && ref.inputs.length > 1 ? on : (on[0] || "");
    }
    const el = ref.el;
    if (!el) return "";
    if (el.tagName === "SELECT") {
      const t = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : "";
      return /^(select|choose|--)/i.test(t) ? "" : t;
    }
    if (field.kind === "listbox") { const t = clean(el.innerText); return /^select/i.test(t) ? "" : t; }
    if (field.kind === "combobox") return shownValue(el);
    return clean(el.value);
  }

  function mark(field, state) {
    const ref = registry.get(field.key);
    const el = ref.el || ref.group || (ref.inputs && (ref.inputs[0].closest("fieldset, [role='radiogroup'], [data-automation-id^='formField-']") || ref.inputs[0].parentElement));
    if (!el) return;
    const target = el.type === "file" ? (el.closest("[data-automation-id^='formField-']") || el.parentElement) : el;
    target.style.outline = state === "ok" ? "2px solid #1f7a4d" : "2px solid #d97706";
    target.style.outlineOffset = "2px";
    field.target = target;
  }

  // ---------------------------------------------------------------- panel
  const host = document.createElement("div");
  host.id = "jobbot-panel-host";
  host.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;";
  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `<style>
    .p{width:340px;max-height:80vh;overflow:auto;background:#fff;color:#1d1d1b;border:1px solid #d9d8d3;border-radius:10px;
       box-shadow:0 8px 28px rgba(0,0,0,.18);font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:12px 14px}
    h1{font-size:14px;margin:0 0 6px;display:flex;justify-content:space-between;align-items:center}
    .muted{color:#6b6b66}.ok{color:#1f7a4d}.warn{color:#9a5b00}.err{color:#b3261e}
    ul{margin:6px 0;padding-left:18px} li{margin:3px 0;cursor:pointer}
    button{font:inherit;border:1px solid #d9d8d3;background:#fff;border-radius:7px;padding:5px 10px;cursor:pointer;margin:6px 6px 0 0}
    button.primary{background:#2f5bd3;border-color:#2f5bd3;color:#fff} .x{border:0;margin:0;padding:0 4px;font-size:16px}
  </style><div class="p"><h1><span>jobbot</span><button class="x" id="close" title="Close">×</button></h1><div id="body"></div></div>`;
  const body = shadow.getElementById("body");
  shadow.getElementById("close").onclick = () => host.remove();
  const show = (htmlString) => { if (!host.isConnected) document.documentElement.appendChild(host); body.innerHTML = htmlString; };
  const esc = (s) => String(s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  let lastPlan = null;

  function report(plan, fields, filled, needs, optionalBlank = 0) {
    const job = plan.job || {};
    const items = needs.map((n, i) => `<li data-i="${i}"><b>${esc(n.label.slice(0, 90))}</b><br><span class="muted">${esc(n.reason)}</span></li>`).join("");
    const warns = (plan.warnings || []).map((w) => `<li class="warn">${esc(w)}</li>`).join("");
    show(`<div class="muted">${esc(job.company)} — ${esc(job.title || document.title)}${job.tracked ? "" : " (not in your tracker yet)"}</div>
      <p class="ok">Filled ${filled} of ${fields.length} empty fields.</p>
      ${needs.length ? `<div><b>Needs you (${needs.length}):</b><ul id="needs">${items}</ul>
        <p class="muted">Answer these on the page, then click <b>Save my answers</b>: jobbot will fill them for you on every form from now on.</p></div>` : `<p class="ok">Nothing left for you on this page.</p>`}
      ${optionalBlank ? `<p class="muted">${optionalBlank} optional field${optionalBlank === 1 ? "" : "s"} left blank (no answer on file).</p>` : ""}
      ${warns ? `<div><b>Heads up:</b><ul>${warns}</ul></div>` : ""}
      <p class="muted">Check the page, then click the site's own Next / Submit. On the next page, click jobbot again.</p>
      ${needs.length ? `<button id="remember">Save my answers</button>` : ""}<button class="primary" id="applied">Mark applied</button><button id="letter">Copy cover letter</button><button id="again">Fill again</button>
      <div id="msg" class="muted"></div>`);
    const list = shadow.getElementById("needs");
    if (list) list.onclick = (e) => {
      const li = e.target.closest("li"); const n = li && needs[+li.dataset.i];
      if (n && n.target) n.target.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    const msg = shadow.getElementById("msg");
    shadow.getElementById("again").onclick = run;
    const remember = shadow.getElementById("remember");
    if (remember) remember.onclick = async () => {
      const items = needs.map((n) => ({ label: n.label, kind: n.kind, options: n.options, value: currentValue(n) }))
        .filter((i) => Array.isArray(i.value) ? i.value.length : i.value);
      if (!items.length) { msg.textContent = "Answer the amber questions on the page first, then click Save my answers."; return; }
      try {
        const r = await ask("/api/ext/remember", { url: location.href, title: job.title || document.title, company: job.company, items });
        const skipped = r.skipped.map((x) => `${x.label} (${x.why})`).join("; ");
        msg.textContent = `Saved ${r.saved} answer${r.saved === 1 ? "" : "s"}; jobbot will use them from now on.` + (skipped ? ` Not saved: ${skipped}` : "");
      } catch (e) { msg.textContent = "Couldn't save: " + e.message; }
    };
    shadow.getElementById("applied").onclick = async () => {
      try { msg.textContent = (await ask("/api/ext/applied", { url: location.href, title: job.title || document.title, company: job.company })).message; }
      catch (e) { msg.textContent = "Couldn't update the tracker: " + e.message; }
    };
    shadow.getElementById("letter").onclick = async () => {
      msg.textContent = "Getting the cover letter…";
      try {
        const text = plan.cover_letter || (await ask("/api/ext/cover-letter", { url: location.href, title: document.title, page_text: document.body.innerText.slice(0, 20000) })).cover_letter;
        await navigator.clipboard.writeText(text);
        msg.textContent = text ? "Cover letter copied." : "No cover letter (this posting restricts AI).";
      } catch (e) { msg.textContent = "Couldn't copy: " + e.message; }
    };
  }

  // ---------------------------------------------------------------- run
  async function run() {
    show(`<p>Reading this page…</p>`);
    try { await ask("/api/ext/ping"); }
    catch (e) { show(`<p class="err">jobbot isn't running.</p><p>Start it in Terminal:<br><code>python3 -m jobbot serve</code></p>`); return; }
    const fields = await scan();
    if (!fields.length) { show(`<p class="muted">No empty fields on this page.</p>`); return; }
    show(`<p>Found ${fields.length} empty fields. Asking jobbot… (new questions can take 30 seconds while Claude drafts)</p>`);
    let plan;
    try {
      plan = await ask("/api/ext/plan", { url: location.href, title: document.title,
        page_text: document.body.innerText.slice(0, 20000), fields: fields.map(({ key, label, kind, options, required }) => ({ key, label, kind, options, required })) });
    } catch (e) { show(`<p class="err">jobbot couldn't plan this page: ${esc(e.message)}</p>`); return; }
    lastPlan = plan;

    let filled = 0;
    const needs = [];
    const blocked = new Map((plan.blockers || []).map((b) => [b.key, b.reason]));
    // Files first, then wait: many sites read an uploaded resume and refill name, phone
    // and location a few seconds later, which would undo anything typed before that.
    const done = new Map();
    for (const field of fields) {
      const fileKind = plan.files && plan.files[field.key];
      if (field.kind !== "file" || !fileKind) continue;
      if (fileKind === "cover_letter" && !(plan.files_available || {}).cover_letter) continue;
      try { done.set(field.key, await attach(registry.get(field.key).el, fileKind)); } catch (e) { done.set(field.key, false); }
    }
    if ([...done.values()].some(Boolean)) {
      show(`<p>Attached your resume. Waiting for the site to read it…</p>`);
      await sleep(4500);
    }
    const okFields = [];
    let optionalBlank = 0;
    for (const field of fields) {
      const answer = plan.answers[field.key];
      let ok = done.get(field.key) || false;
      try {
        if (!done.has(field.key) && answer) ok = await fill(field, answer.value, plan);
      } catch (e) { ok = false; }
      if (ok) { okFields.push(field); mark(field, "ok"); continue; }
      const reason = blocked.get(field.key) || (answer ? `couldn't set "${String(answer.value).slice(0, 40)}"` : field.required ? "no answer on file" : "");
      if (reason && (field.required || blocked.has(field.key) || answer)) { mark(field, "needs"); needs.push({ ...field, reason }); }
      else optionalBlank++;
    }
    // Some pages re-render a moment after filling and clear fields again: check, refill once.
    const stillSet = (f) => { const v = currentValue(f); return Array.isArray(v) ? v.length > 0 : !!v; };
    show(`<p>Checking the page kept everything…</p>`);
    await sleep(1500);
    const cleared = okFields.filter((f) => f.kind !== "file" && !stillSet(f));
    if (cleared.length) {
      debug(`${cleared.length} field(s) were cleared by the page; filling them again`);
      for (const f of cleared) { try { await fill(f, plan.answers[f.key].value, plan); } catch (e) { /* reported below */ } }
      await sleep(1000);
      for (const f of cleared) {
        if (stillSet(f)) continue;
        okFields.splice(okFields.indexOf(f), 1);
        mark(f, "needs"); needs.push({ ...f, reason: "the page cleared it; type it in" });
      }
    }
    report(plan, fields, okFields.length, needs.map((n) => ({ ...n, target: registry.get(n.key) && (n.target || null) })), optionalBlank);
  }

  window.__jobbot = { version: VERSION, run, log };
  run();
})();
