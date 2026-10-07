// Settings screen. It edits the same `settings` table the CLI reads
// (settings.py), so the panel and `python run.py` can never disagree. Every
// change is saved as it is made and applies to the next run; validation is
// settings.py's, and its message is shown under the field.

const SETTINGS = [
  {
    title: "Transcription",
    note: "Used when a video's captions are missing or fail the quality check.",
    fields: [
      { key: "whisper_model", label: "Whisper model", type: "model" },
      { key: "whisper_threads", label: "CPU threads", type: "number", min: 1, max: 64, step: 1 },
      {
        key: "language", label: "Spoken language", type: "select",
        options: [["auto", "Detect automatically"], ["ru", "Russian"], ["en", "English"],
                  ["uk", "Ukrainian"], ["de", "German"], ["fr", "French"], ["es", "Spanish"]],
        hint: "Setting it skips detection, which can misfire on a short or musical intro.",
      },
      { key: "vad_enabled", label: "Skip silence and music (VAD)", type: "toggle" },
      {
        key: "vad_threshold", label: "VAD threshold", type: "number", min: 0, max: 1, step: 0.05,
        nullable: true, placeholder: "default",
        hint: "Higher trims more. Leave empty for whisper's own default.",
      },
    ],
  },
  {
    title: "Caption quality",
    note: "Captions are used only if all three pass; otherwise the audio goes to whisper.",
    fields: [
      { key: "min_coverage", label: "Minimum coverage", type: "number", min: 0, max: 1, step: 0.05,
        hint: "How close to the end of the video the captions must reach (0–1)." },
      { key: "min_density", label: "Minimum density", type: "number", min: 0, max: 1, step: 0.05,
        hint: "Share of the runtime the captions actually cover (0–1)." },
      { key: "min_words_per_min", label: "Minimum words per minute", type: "number", min: 0, step: 1 },
    ],
  },
  {
    title: "Analysis",
    fields: [
      {
        key: "analysis_language", label: "Answer in", type: "select", nullable: true,
        options: [[null, "Whatever fits the video"], ["Russian", "Russian"], ["English", "English"]],
        hint: "Adds \"Answer in …\" to every new analysis. Follow-ups keep their thread's language.",
      },
    ],
  },
  {
    title: "Storage",
    fields: [
      {
        key: "retention_days", label: "Keep videos for (days)", type: "number", min: 0, step: 1,
        hint: "0 keeps everything. Older videos are deleted the way the Delete button does it: "
          + "files removed, log rows kept for the stats.",
      },
    ],
  },
];

const settingsUi = { data: null, errors: {} };

async function openSettings() {
  settingsUi.errors = {};
  $("set-status").textContent = "";
  $("set-body").replaceChildren(el("p", "loading", "Loading…"));
  openScreen("settings");
  try {
    settingsUi.data = await host.call("settings_get");
  } catch (e) {
    $("set-body").replaceChildren(el("p", "loading", e.message));
    return;
  }
  renderSettings();
}

function showValue(v) {
  if (v === null || v === undefined) return "none";
  if (typeof v === "boolean") return v ? "on" : "off";
  return String(v);
}

async function saveSetting(key, value, reset = false) {
  try {
    settingsUi.data = await host.call("settings_set", reset ? { key, reset: true } : { key, value });
    delete settingsUi.errors[key];
    $("set-status").textContent = "Saved · applies to the next run";
    refresh();          // the worker line shows the model and threads
  } catch (e) {
    settingsUi.errors[key] = e.message;
  }
  renderSettings();
}

function control(f, value) {
  const d = settingsUi.data;
  if (f.type === "toggle") {
    const wrap = el("label", "switch");
    const box = el("input");
    box.type = "checkbox";
    box.checked = !!value;
    // Without the VAD model a run would fail, so it can't be switched on —
    // but it can always be switched off.
    if (f.key === "vad_enabled" && !d.vad_model_present && !value) box.disabled = true;
    box.addEventListener("change", () => saveSetting(f.key, box.checked));
    wrap.append(box, el("span", "slider"));
    return wrap;
  }
  if (f.type === "model" || f.type === "select") {
    const sel = el("select");
    let options = f.options || d.models.map((m) => [m, m.replace(/^.*\/ggml-|\.bin$/g, "")]);
    if (!options.some(([v]) => v === value)) options = [...options, [value, showValue(value)]];
    for (const [v, label] of options) {
      const o = el("option", null, label);
      o.value = v === null ? "" : v;
      o.selected = v === value;
      sel.append(o);
    }
    sel.addEventListener("change", () => saveSetting(f.key, sel.value === "" && f.nullable ? null : sel.value));
    return sel;
  }
  const input = el("input");
  input.type = "number";
  for (const a of ["min", "max", "step", "placeholder"]) if (f[a] !== undefined) input[a] = f[a];
  input.value = value ?? "";
  input.addEventListener("change", () => {
    const raw = input.value.trim();
    saveSetting(f.key, raw === "" ? (f.nullable ? null : raw) : Number(raw));
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
  return input;
}

function renderSettings() {
  const d = settingsUi.data;
  if (!d) return;
  const stored = new Set(d.stored);
  const frag = document.createDocumentFragment();
  for (const group of SETTINGS) {
    const sec = el("section", "set-group");
    sec.append(el("h2", "section", group.title));
    if (group.note) sec.append(el("p", "set-note", group.note));
    for (const f of group.fields) {
      const row = el("div", "set-row");
      const head = el("div", "set-head");
      head.append(el("label", "set-label", f.label), control(f, d.values[f.key]));
      row.append(head);

      const meta = el("div", "set-meta");
      meta.append(el("span", null, `Default: ${showValue(d.defaults[f.key])}`));
      if (stored.has(f.key)) {
        const reset = el("button", "text-btn", "Reset");
        reset.type = "button";
        reset.addEventListener("click", () => saveSetting(f.key, null, true));
        meta.append(reset);
      }
      row.append(meta);
      if (f.hint) row.append(el("p", "hint", f.hint));
      if (f.key === "vad_enabled" && !d.vad_model_present) {
        row.append(el("p", "hint warn", `Needs ${d.vad_model} — from huggingface.co/ggml-org/whisper-vad`));
      }
      if (settingsUi.errors[f.key]) row.append(el("p", "form-error", settingsUi.errors[f.key]));
      sec.append(row);
    }
    frag.append(sec);
  }
  $("set-body").replaceChildren(frag);
}

$("set-back").addEventListener("click", () => closeScreen("settings"));
