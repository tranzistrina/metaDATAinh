"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  csrf: "", available: false, busy: false,
  id: null, name: "", metadata: [], templates: [],
  edits: new Map(), removals: new Set(), selected: new Set(), newFields: new Map()
};

function notice(message, error = false) {
  const el = $("notice");
  el.textContent = message;
  el.className = error ? "notice error" : "notice";
  el.hidden = !message;
}

async function api(url, options = {}) {
  const method = options.method || "GET";
  const headers = Object.assign({}, options.headers || {});
  if (!["GET", "HEAD"].includes(method)) headers["X-CSRF-Token"] = state.csrf;
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(url, Object.assign({}, options, {headers}));
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Ошибка обращения к серверу.");
  return data;
}

function formatValue(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? "";
}

function currentValue(field) {
  if (!state.edits.has(field.key)) return field.value;
  const text = state.edits.get(field.key);
  if (Array.isArray(field.value)) {
    let parsed;
    try { parsed = JSON.parse(text); }
    catch (_) { throw new Error("Тег " + field.key + ": введите корректный JSON-массив строк."); }
    if (!Array.isArray(parsed) || !parsed.every((item) => typeof item === "string")) {
      throw new Error("Тег " + field.key + ": ожидается JSON-массив строк.");
    }
    return parsed;
  }
  return text;
}

function dirtyTotal() {
  return state.edits.size + state.removals.size + state.newFields.size;
}

function renderActions() {
  const hasFile = Boolean(state.id);
  const dirty = dirtyTotal();
  $("saveChangesBtn").disabled = state.busy || !hasFile || !dirty;
  $("resetBtn").disabled = state.busy || !hasFile;
  $("saveTemplateBtn").disabled = state.busy || !hasFile;
  $("applyTemplateBtn").disabled = state.busy || !hasFile || !$("templateSelect").value;
  $("deleteTemplateBtn").disabled = state.busy || !$("templateSelect").value;
  $("addTagBtn").disabled = state.busy || !hasFile;
  $("fileInput").disabled = state.busy || !state.available;
  $("dirtyCount").textContent = dirty ? "(" + dirty + ")" : "";
}

function setFile(data) {
  state.id = data.id;
  state.name = data.name;
  state.metadata = data.metadata;
  state.edits.clear();
  state.removals.clear();
  state.selected.clear();
  state.newFields.clear();

  $("workspace").hidden = false;
  $("fileCard").hidden = false;
  $("fileName").textContent = state.name;
  $("fileStats").textContent = data.metadata.length + " найденных тегов";
  $("downloadBtn").href = "/api/files/" + state.id + "/download";
  $("tagSearch").value = "";
  $("tagFilter").value = "all";
  renderMetadata();
  renderNewFields();
  renderActions();
  $("workspace").scrollIntoView({behavior: "smooth", block: "start"});
}

function renderMetadata() {
  const query = $("tagSearch").value.trim().toLocaleLowerCase();
  const filter = $("tagFilter").value;
  const body = $("metadataBody");
  body.replaceChildren();
  let visible = 0;

  for (const field of state.metadata) {
    if (filter === "editable" && !field.editable) continue;
    if (filter === "readonly" && field.editable) continue;
    const raw = formatValue(field.value);
    if (query && ![field.group, field.name, field.key, raw]
        .some((part) => String(part).toLocaleLowerCase().includes(query))) continue;
    visible += 1;

    const tr = document.createElement("tr");
    const groupTd = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "group-pill";
    badge.textContent = field.group;
    groupTd.append(badge);

    const tagTd = document.createElement("td");
    const tagName = document.createElement("span");
    tagName.className = "tag-label";
    tagName.textContent = field.name;
    const tagKey = document.createElement("span");
    tagKey.className = "tag-key";
    tagKey.textContent = field.key;
    tagTd.append(tagName, tagKey);

    const valueTd = document.createElement("td");
    if (field.editable) {
      const input = document.createElement("textarea");
      input.className = "edit-area";
      input.rows = Math.min(4, Math.max(1, raw.split("\n").length));
      input.setAttribute("aria-label", "Значение " + field.key);
      input.value = state.edits.has(field.key) ? state.edits.get(field.key) : raw;
      input.disabled = state.removals.has(field.key);
      input.addEventListener("input", () => {
        if (input.value === raw) state.edits.delete(field.key);
        else state.edits.set(field.key, input.value);
        renderActions();
      });

      const tools = document.createElement("div");
      tools.className = "value-tools";
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "clear-btn";
      clear.textContent = state.removals.has(field.key) ? "Отменить очистку" : "Очистить тег";
      clear.addEventListener("click", () => {
        if (state.removals.has(field.key)) {
          state.removals.delete(field.key);
          clear.textContent = "Очистить тег";
          input.disabled = false;
        } else {
          state.removals.add(field.key);
          state.edits.delete(field.key);
          input.value = raw;
          input.disabled = true;
          clear.textContent = "Отменить очистку";
        }
        renderActions();
      });
      const hint = document.createElement("span");
      hint.className = "edit-hint";
      hint.textContent = Array.isArray(field.value) ? "JSON-массив" : "Можно редактировать";
      tools.append(clear, hint);
      valueTd.append(input, tools);
    } else {
      const read = document.createElement("span");
      read.className = "readonly";
      read.textContent = raw;
      valueTd.append(read);
    }

    const selectTd = document.createElement("td");
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.className = "tick";
    tick.disabled = !field.editable;
    tick.checked = state.selected.has(field.key);
    tick.setAttribute("aria-label", "Добавить " + field.key + " в шаблон");
    tick.addEventListener("change", () => {
      if (tick.checked) state.selected.add(field.key);
      else state.selected.delete(field.key);
    });
    selectTd.append(tick);
    tr.append(groupTd, tagTd, valueTd, selectTd);
    body.append(tr);
  }
  $("noTags").hidden = visible > 0;
  const editable = state.metadata.filter((field) => field.editable).length;
  $("metaSummary").textContent =
    state.metadata.length + " тегов обнаружено · " + editable + " потенциально доступны для редактирования";
}

function renderNewFields() {
  const el = $("newFieldsList");
  el.replaceChildren();
  el.hidden = state.newFields.size === 0;
  for (const [tag, value] of state.newFields) {
    const chip = document.createElement("span");
    chip.className = "pending-tag";
    chip.append(document.createTextNode(tag + ": " + value));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "✕";
    remove.setAttribute("aria-label", "Убрать " + tag);
    remove.addEventListener("click", () => {
      state.newFields.delete(tag);
      renderNewFields();
      renderActions();
    });
    chip.append(remove);
    el.append(chip);
  }
}

function renderTemplates() {
  const select = $("templateSelect");
  const current = select.value;
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = state.templates.length ? "Выберите шаблон…" : "Нет сохранённых шаблонов";
  select.append(placeholder);
  for (const item of state.templates) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name;
    select.append(option);
  }
  if (state.templates.some((item) => item.id === current)) select.value = current;
  $("templateCount").textContent = state.templates.length + " СОХРАНЕНО";
  renderTemplatePreview();
}

function renderTemplatePreview() {
  const item = state.templates.find((entry) => entry.id === $("templateSelect").value);
  const area = $("templatePreview");
  area.replaceChildren();
  if (!item) {
    area.textContent = "Выберите шаблон, чтобы увидеть его поля.";
  } else {
    const title = document.createElement("span");
    title.className = "preview-name";
    title.textContent = item.name + " · " + Object.keys(item.fields).length + " тегов";
    const info = document.createElement("span");
    info.textContent = Object.keys(item.fields).join(" · ");
    area.append(title, info);
  }
  renderActions();
}

async function refreshTemplates() {
  const data = await api("/api/templates");
  state.templates = data.templates;
  renderTemplates();
}

async function runAction(operation) {
  if (state.busy) return;
  state.busy = true;
  renderActions();
  try { await operation(); }
  catch (error) { notice(error.message || "Неизвестная ошибка.", true); }
  finally {
    state.busy = false;
    renderActions();
  }
}

async function upload(file) {
  if (!file) return;
  if (file.size > 100 * 1024 * 1024) {
    notice("Файл превышает лимит 100 МБ.", true);
    return;
  }
  await runAction(async () => {
    notice("Читаем метаданные файла…");
    const data = new FormData();
    data.append("file", file, file.name);
    const result = await api("/api/files", {method: "POST", body: data});
    setFile(result);
    notice("Файл загружен. Все найденные теги показаны ниже.");
  });
}

function collectChanges() {
  const changes = Object.fromEntries(state.newFields);
  for (const field of state.metadata) {
    if (state.edits.has(field.key) && !state.removals.has(field.key)) {
      changes[field.key] = currentValue(field);
    }
  }
  return {changes, removals: Array.from(state.removals)};
}

async function saveChanges() {
  await runAction(async () => {
    const payload = collectChanges();
    const data = await api("/api/files/" + state.id + "/apply", {
      method: "POST", body: JSON.stringify(payload)
    });
    setFile(data);
    notice("Правки сохранены в рабочую копию. Можно скачать готовый файл.");
  });
}

function collectTemplateFields() {
  const fields = Object.fromEntries(state.newFields);
  for (const field of state.metadata) {
    if (state.selected.has(field.key) && !state.removals.has(field.key)) {
      fields[field.key] = currentValue(field);
    }
  }
  return fields;
}

async function createTemplate() {
  await runAction(async () => {
    const name = $("templateName").value.trim();
    if (!name) throw new Error("Введите название шаблона.");
    const fields = collectTemplateFields();
    if (!Object.keys(fields).length) {
      throw new Error("Отметьте редактируемые поля в таблице или добавьте новый тег.");
    }
    const result = await api("/api/templates", {
      method: "POST", body: JSON.stringify({name, fields})
    });
    $("templateName").value = "";
    await refreshTemplates();
    $("templateSelect").value = result.template.id;
    renderTemplatePreview();
    notice("Шаблон «" + name + "» сохранён на этом Mac.");
  });
}

async function applyTemplate() {
  const selected = $("templateSelect").value;
  if (!selected) return;
  if (dirtyTotal() > 0 && !confirm("Несохранённые изменения будут потеряны. Продолжить?")) return;
  await runAction(async () => {
    const data = await api("/api/files/" + state.id + "/apply-template", {
      method: "POST", body: JSON.stringify({template_id: selected})
    });
    setFile(data);
    notice("Шаблон применён к рабочей копии. Остальные теги не удалялись.");
  });
}

async function deleteTemplate() {
  const selected = $("templateSelect").value;
  const item = state.templates.find((t) => t.id === selected);
  if (!item || !confirm("Удалить шаблон «" + item.name + "»?")) return;
  await runAction(async () => {
    await api("/api/templates/" + selected, {method: "DELETE"});
    await refreshTemplates();
    notice("Шаблон удалён.");
  });
}

async function resetFile() {
  if (!state.id || !confirm("Восстановить исходник? Все правки рабочей копии будут потеряны.")) return;
  await runAction(async () => {
    const data = await api("/api/files/" + state.id + "/reset", {method: "POST"});
    setFile(data);
    notice("Исходные метаданные восстановлены.");
  });
}

function addField() {
  const tag = $("newTag").value.trim();
  const value = $("newValue").value;
  if (!/^(?:[A-Za-z][A-Za-z0-9_-]{0,49}:)?[A-Za-z][A-Za-z0-9_]{0,79}$/.test(tag)) {
    notice("Укажите корректное имя тега, например XMP-dc:Title.", true);
    return;
  }
  if (state.metadata.some((field) => field.key.toLowerCase() === tag.toLowerCase())) {
    notice("Такой тег уже есть в таблице. Отредактируйте его там.", true);
    return;
  }
  if (!value) { notice("Введите значение для нового тега.", true); return; }
  state.newFields.set(tag, value);
  $("newTag").value = "";
  $("newValue").value = "";
  renderNewFields();
  renderActions();
  notice("Тег добавлен в ожидающие изменения. Запись проверится при сохранении.");
}

async function initialize() {
  $("fileInput").addEventListener("change", (event) => {
    upload(event.target.files[0]);
    event.target.value = "";
  });
  const dropzone = $("dropzone");
  ["dragenter", "dragover"].forEach((type) => {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add("drag-over");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.remove("drag-over");
    });
  });
  dropzone.addEventListener("drop", (event) => {
    if (state.available && !state.busy) upload(event.dataTransfer.files[0]);
  });
  $("tagSearch").addEventListener("input", renderMetadata);
  $("tagFilter").addEventListener("change", renderMetadata);
  $("templateSelect").addEventListener("change", renderTemplatePreview);
  $("saveChangesBtn").addEventListener("click", saveChanges);
  $("resetBtn").addEventListener("click", resetFile);
  $("applyTemplateBtn").addEventListener("click", applyTemplate);
  $("deleteTemplateBtn").addEventListener("click", deleteTemplate);
  $("saveTemplateBtn").addEventListener("click", createTemplate);
  $("addTagBtn").addEventListener("click", addField);

  try {
    const status = await api("/api/status");
    state.csrf = status.csrf_token;
    state.available = status.available;
    $("engineStatus").textContent = status.available
      ? "ExifTool " + status.version + " · готов"
      : "ExifTool не установлен";
    if (!status.available) {
      notice("Для работы приложения установите ExifTool: brew install exiftool. Затем обновите страницу.", true);
    }
    await refreshTemplates();
  } catch (error) {
    $("engineStatus").textContent = "Ошибка подключения";
    notice(error.message, true);
  }
  renderActions();
}
document.addEventListener("DOMContentLoaded", initialize);
