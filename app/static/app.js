const $ = id => document.getElementById(id);
const tokenKey = "jarvisApiToken";

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) headers.set("Content-Type", "application/json");
  const token = sessionStorage.getItem(tokenKey);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

function esc(value = "") {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}
function nice(value = "") {
  return value.replaceAll("_", " ").replace(/\b\w/g, char => char.toUpperCase());
}
function showNotice(message, isError = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", isError);
  $("notice").classList.toggle("visible", Boolean(message));
}
function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

function renderChatText(value) {
  return String(value).split(/(https?:\/\/[^\s]+)/g).map(part => {
    const url = safeUrl(part);
    return url
      ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(part)}</a>`
      : esc(part);
  }).join("");
}

async function health() {
  try {
    await api("/api/health");
    const system = await api("/api/system");
    $("health").textContent = "Backend online";
    $("statusText").textContent = system.emergency_stop ? "Emergency stop" : "Operational";
    $("emergencyButton").textContent = system.emergency_stop ? "Resume execution" : "Emergency stop";
    $("emergencyButton").classList.toggle("danger", !system.emergency_stop);
  } catch (error) {
    $("health").textContent = error.message.includes("token") ? "Authentication required" : "Backend unavailable";
    $("statusText").textContent = "Needs attention";
  }
}

async function loadTasks() {
  const tasks = await api("/api/tasks");
  $("tasks").innerHTML = tasks.length ? tasks.map(task => {
    const isCancelled = task.status === "cancelled";
    return `<article class="task">
      <div class="task-top"><div><h4>${esc(task.title)}</h4><div class="meta">${esc(task.instruction)}<br>
      Location: ${esc(task.location || "Any")} · Keywords: ${esc((task.keywords || []).join(", ") || "Default")}<br>
      Schedule: ${task.interval_minutes ? `every ${task.interval_minutes} minutes` : "manual"} · Last run: ${esc(task.last_run_at || "Never")}</div></div>
      <span class="status">${esc(nice(task.status))}</span></div>
      <div class="actions">
      <button data-action="run" data-id="${task.id}" ${isCancelled ? "disabled" : ""}>Run discovery</button>
      <button class="secondary" data-action="edit" data-id="${task.id}" ${isCancelled ? "disabled" : ""}>Edit</button>
      <button class="secondary" data-action="delete" data-id="${task.id}">Delete permanently</button>
      ${task.status === "active" ? `<button class="secondary" data-action="pause" data-id="${task.id}">Pause</button>` : ""}
      ${task.status === "paused" ? `<button class="secondary" data-action="resume" data-id="${task.id}">Resume</button>` : ""}
      ${!isCancelled ? `<button class="secondary" data-action="cancel" data-id="${task.id}">Cancel</button>` : ""}
      </div></article>`;
  }).join("") : '<p class="empty">No tasks yet. Create one above.</p>';
  return tasks;
}

async function loadFindings() {
  const status = $("findingFilter").value;
  const rows = await api(`/api/findings${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  $("findings").innerHTML = rows.length ? rows.map(finding => {
    const url = safeUrl(finding.url);
    return `<article class="finding">
      <div class="finding-top"><div><h4>${esc(finding.title)}</h4><div class="meta">${esc(finding.task_title)} · ${esc(finding.company || "Company not listed")} · ${esc(finding.location || "Location not listed")} · ${esc(finding.source)} · ${esc(finding.discovered_at)}</div></div>
      <span class="score">${finding.score}/100 · ${esc(nice(finding.status))}</span></div>
      <p>${esc(finding.summary || "No summary available.")}</p>
      <p class="match-reason">${esc(finding.match_reasons || "Match explanation unavailable for imported item.")}</p>
      <div class="actions"><label class="status-control">Review status<select data-finding-id="${finding.id}">
        ${["new", "relevant", "irrelevant", "duplicate", "contacted", "completed"].map(state => `<option value="${state}" ${state === finding.status ? "selected" : ""}>${esc(nice(state))}</option>`).join("")}
      </select></label>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open original source ↗</a>` : '<span class="meta">Unsafe source URL hidden</span>'}</div>
    </article>`;
  }).join("") : '<p class="empty">No findings match this filter.</p>';
}

async function loadRuns() {
  const rows = await api("/api/runs");
  $("runs").innerHTML = rows.length ? rows.map(run => `<div class="run">
    <div class="task-top"><strong>${esc(run.task_title)}</strong><span class="status">${esc(nice(run.status))}</span></div>
    <div class="meta">${esc(run.created_at)} · ${esc(run.message)} · Checked: ${run.checked_count} · New: ${run.item_count}</div>
  </div>`).join("") : '<p class="empty">No executions recorded yet.</p>';
}

async function loadChat() {
  const rows = await api("/api/assistant/messages");
  $("chatMessages").innerHTML = rows.length ? rows.map(message => `
    <article class="chat-message ${message.role === "user" ? "user" : "assistant"}">
      <strong>${message.role === "user" ? "You" : "JARVIS"}</strong>
      <p>${renderChatText(message.content)}</p>
    </article>`).join("") : '<p class="muted">Ask a question, request a web search, or ask JARVIS to create a discovery task.</p>';
  $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
}

async function loadWhatsAppMessages() {
  const rows = await api("/api/whatsapp/messages");
  $("whatsappMessages").innerHTML = rows.length ? rows.map(message => {
    const body = message.body || `Received ${message.message_type} message (media is not downloaded).`;
    return `<div class="memory">
      <div><strong>${message.direction === "inbound" ? "Received" : "Sent"} · ${esc(message.contact || "Unknown number")}</strong>
      <div class="meta">${esc(nice(message.status))} · ${esc(message.created_at)}</div>
      <div class="whatsapp-message">${esc(body)}</div></div>
    </div>`;
  }).join("") : '<p class="empty">No WhatsApp messages received or sent yet.</p>';
}

function googleLink(url, label) {
  const safe = safeUrl(url);
  return safe
    ? `<a class="google-item-link" href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`
    : esc(label);
}

async function renderGoogleResults(endpoint, targetId, renderer) {
  const target = $(targetId);
  target.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const items = await api(endpoint);
    target.innerHTML = items.length
      ? items.map(renderer).join("")
      : '<p class="empty">No results.</p>';
  } catch (error) {
    target.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
}

async function loadBriefing() {
  const data = await api("/api/briefing");
  $("lastCheck").textContent = `Last check: ${data.last_discovery_at || "not yet run"}`;
  const opportunities = data.new_findings.length
    ? data.new_findings.map(item => `<li><strong>${esc(item.title)}</strong> — ${esc(item.company || "Company not listed")} (${item.score}/100)</li>`).join("")
    : "<li>No new findings awaiting review.</li>";
  const failures = data.failed_runs.length
    ? data.failed_runs.map(item => `<li>${esc(item.title)}: ${esc(item.message)}</li>`).join("")
    : "<li>No recorded discovery failures.</li>";
  const sources = data.source_health.length
    ? data.source_health.map(source => `<li>${esc(source.source)}: ${esc(nice(source.status))}, checked ${esc(source.last_checked_at)} (${esc(source.details)})</li>`).join("")
    : "<li>No source has been checked yet.</li>";
  $("briefing").innerHTML = `<div class="briefing-grid">
    <div><strong>${data.awaiting_review}</strong><span>findings awaiting review</span></div>
    <div><strong>${data.failed_runs.length}</strong><span>recent failed checks</span></div>
    <div><strong>${esc(data.generated_at)}</strong><span>briefing generated</span></div>
  </div><div class="form-grid"><div><h4>New opportunities</h4><ul>${opportunities}</ul></div>
  <div><h4>Source health &amp; failures</h4><ul>${sources}${failures}</ul></div></div>`;
}

async function loadIntegrations() {
  const data = await api("/api/integrations");
  const cards = data.sources.map(source => `<article class="integration">
    <strong>${esc(source.name)}</strong><span class="status">${esc(nice(source.status))}</span>
    <div class="meta">${esc(source.details || source.type || "")}${source.last_checked_at ? `<br>Last check: ${esc(source.last_checked_at)}` : ""}</div>
  </article>`).join("") + `<article class="integration">
    <strong>Language model</strong><span class="status">${esc(nice(data.language_model.status))}</span>
    <div class="meta">${esc(data.language_model.model || "Using local rule-based task extraction")}</div>
  </article><article class="integration">
    <strong>Web search</strong><span class="status">${esc(nice(data.web_search.status))}</span>
    <div class="meta">${esc(data.web_search.provider)} search API</div>
  </article>`;
  $("integrations").innerHTML = `${cards}<p class="meta">Backend scheduler: ${esc(nice(data.scheduler.status))}; scan interval ${data.scheduler.scan_interval_minutes} minutes. App API authentication: ${esc(nice(data.application_api_authentication.status))}.</p>`;
  $("googleConnection").textContent = `${nice(data.google_workspace.status)} — ${data.google_workspace.details}`;
  $("connectGoogle").hidden = data.google_workspace.connected;
  $("connectGoogle").disabled = !data.google_workspace.configured;
  $("disconnectGoogle").hidden = !data.google_workspace.connected;
  $("googleTools").hidden = !data.google_workspace.connected;
  const instagram = data.sources.find(source => source.name === "Instagram");
  $("instagramConnection").textContent = instagram
    ? `${nice(instagram.status)} — ${instagram.details}`
    : "Instagram Login status is unavailable.";
  $("connectInstagram").hidden = data.instagram.connected;
  $("connectInstagram").disabled = !data.instagram.configured;
  $("disconnectInstagram").hidden = !data.instagram.connected;
  $("instagramTools").hidden = !data.instagram.connected;
  const whatsapp = data.sources.find(source => source.name === "WhatsApp");
  $("whatsappConnection").textContent = whatsapp
    ? `${nice(whatsapp.status)} — ${whatsapp.details}`
    : "WhatsApp Cloud API status is unavailable.";
  $("whatsappWebhookUrl").textContent = `${window.location.origin}/api/webhooks/whatsapp`;
}

async function loadTaskSources() {
  const [tasks, sources] = await Promise.all([api("/api/tasks"), api("/api/task-sources")]);
  $("sourceTask").innerHTML = tasks.length
    ? tasks.filter(task => task.status !== "cancelled").map(task =>
      `<option value="${task.id}">${esc(task.title)} (${esc(nice(task.status))})</option>`
    ).join("")
    : '<option value="">Create a task first</option>';
  $("sourceTask").disabled = !tasks.some(task => task.status !== "cancelled");
  $("taskSources").innerHTML = sources.length ? sources.map(source => `<div class="memory">
    <div><strong>${esc(source.name)}</strong><div class="meta">${esc(source.task_title)} · ${esc(source.health_status ? nice(source.health_status) : "Configured, not checked")} · ${esc(source.url)}${source.last_checked_at ? ` · Last check: ${esc(source.last_checked_at)}` : ""}${source.health_details ? ` · ${esc(source.health_details)}` : ""}</div></div>
    <button class="secondary source-delete" data-source-delete="${source.id}" type="button">Remove</button>
  </div>`).join("") : '<p class="empty">No additional feeds configured. Arbeitnow remains available as the built-in public jobs source.</p>';
}

async function loadPreferences() {
  const settings = await api("/api/preferences");
  $("prefLocation").value = settings.default_location || "";
  $("prefKeywords").value = (settings.default_keywords || []).join(", ");
  $("quietStart").value = settings.quiet_hours_start || "";
  $("quietEnd").value = settings.quiet_hours_end || "";
  $("morningBriefing").checked = Boolean(settings.morning_briefing);
  $("briefing").closest(".panel").hidden = !Boolean(settings.morning_briefing);
  if (settings.default_location) $("location").placeholder = settings.default_location;
  if (settings.default_keywords?.length) $("keywords").value = settings.default_keywords.join(", ");
}

async function loadMemory() {
  const rows = await api("/api/memory");
  $("memories").innerHTML = rows.length ? rows.map(row => `<div class="memory">
    <div><strong>${esc(row.key)}</strong><div class="meta">${esc(row.value)} · Updated ${esc(row.updated_at)}</div></div>
    <button class="secondary" data-memory-delete="${esc(row.key)}" type="button">Delete</button>
  </div>`).join("") : '<p class="empty">No saved memories.</p>';
}

async function refreshAll(clearNotice = true) {
  try {
    await Promise.all([health(), loadTasks(), loadFindings(), loadRuns(), loadChat(), loadBriefing(), loadIntegrations(), loadTaskSources(), loadWhatsAppMessages(), loadPreferences(), loadMemory()]);
    if (clearNotice) showNotice("");
  } catch (error) {
    showNotice(error.message, true);
  }
}

$("taskForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("formMessage").textContent = "Saving…";
  try {
    await api("/api/tasks", { method: "POST", body: JSON.stringify({
      title: $("title").value.trim(),
      instruction: $("instruction").value.trim(),
      location: $("location").value.trim(),
      keywords: $("keywords").value.split(",").map(word => word.trim()).filter(Boolean),
      interval_minutes: Number($("interval").value || 0)
    }) });
    $("formMessage").textContent = "Task saved.";
    $("taskForm").reset();
    $("keywords").value = "assistant, computer, IT support, technical support, help desk";
    $("interval").value = "0";
    await Promise.all([loadTasks(), loadIntegrations()]);
  } catch (error) {
    $("formMessage").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("sourceForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api(`/api/tasks/${$("sourceTask").value}/sources`, {
      method: "POST",
      body: JSON.stringify({ name: $("sourceName").value.trim(), url: $("sourceUrl").value.trim() })
    });
    $("sourceForm").reset();
    await Promise.all([loadTaskSources(), loadIntegrations()]);
    showNotice("Public feed configured. It will be fetched the next time its task runs.");
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("connectGoogle").addEventListener("click", async () => {
  $("connectGoogle").disabled = true;
  try {
    const result = await api("/api/google/authorize");
    window.location.assign(result.authorization_url);
  } catch (error) {
    showNotice(error.message, true);
    $("connectGoogle").disabled = false;
  }
});

$("disconnectGoogle").addEventListener("click", async () => {
  if (!window.confirm("Disconnect Google and remove the locally stored OAuth tokens?")) return;
  $("disconnectGoogle").disabled = true;
  try {
    await api("/api/google/disconnect", { method: "POST" });
    $("gmailResults").innerHTML = "";
    $("calendarResults").innerHTML = "";
    $("driveResults").innerHTML = "";
    await loadIntegrations();
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    $("disconnectGoogle").disabled = false;
  }
});

$("gmailSearchForm").addEventListener("submit", event => {
  event.preventDefault();
  const query = $("gmailQuery").value.trim();
  renderGoogleResults(
    `/api/google/gmail?q=${encodeURIComponent(query)}`,
    "gmailResults",
    item => `<div class="memory"><div><strong>${esc(item.subject)}</strong>
      <div class="meta">${esc(item.sender)} · ${esc(item.date)}</div>
      ${googleLink(item.url, "Open in Gmail ↗")}</div></div>`
  );
});

$("loadCalendar").addEventListener("click", () => renderGoogleResults(
  "/api/google/calendar",
  "calendarResults",
  item => `<div class="memory"><div><strong>${esc(item.summary)}</strong>
    <div class="meta">${esc(item.start)} – ${esc(item.end)}${item.location ? ` · ${esc(item.location)}` : ""}</div>
    ${googleLink(item.url, "Open event ↗")}</div></div>`
));

$("driveSearchForm").addEventListener("submit", event => {
  event.preventDefault();
  const query = $("driveQuery").value.trim();
  renderGoogleResults(
    `/api/google/drive?q=${encodeURIComponent(query)}`,
    "driveResults",
    item => `<div class="memory"><div><strong>${esc(item.name)}</strong>
      <div class="meta">${esc(item.type)} · Modified ${esc(item.modified)}</div>
      ${googleLink(item.url, "Open in Drive ↗")}</div></div>`
  );
});

$("connectInstagram").addEventListener("click", async () => {
  $("connectInstagram").disabled = true;
  try {
    const result = await api("/api/instagram/authorize");
    window.location.assign(result.authorization_url);
  } catch (error) {
    showNotice(error.message, true);
    $("connectInstagram").disabled = false;
  }
});

$("disconnectInstagram").addEventListener("click", async () => {
  if (!window.confirm("Disconnect Instagram and remove the locally stored OAuth token?")) return;
  $("disconnectInstagram").disabled = true;
  try {
    await api("/api/instagram/disconnect", { method: "POST" });
    $("instagramProfile").innerHTML = "";
    $("instagramMedia").innerHTML = "";
    await loadIntegrations();
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    $("disconnectInstagram").disabled = false;
  }
});

$("loadInstagram").addEventListener("click", async () => {
  const button = $("loadInstagram");
  button.disabled = true;
  $("instagramProfile").innerHTML = '<p class="muted">Loading profile…</p>';
  $("instagramMedia").innerHTML = '<p class="muted">Loading recent media…</p>';
  try {
    const [profile, media] = await Promise.all([
      api("/api/instagram/profile"),
      api("/api/instagram/media")
    ]);
    $("instagramProfile").innerHTML = `<div class="memory"><div><strong>@${esc(profile.username || "Instagram account")}</strong>
      <div class="meta">${esc(profile.account_type || "Professional account")} · ${esc(profile.media_count ?? "Unknown")} total media items</div></div></div>`;
    $("instagramMedia").innerHTML = media.length
      ? media.map(item => `<div class="memory"><div><strong>${esc(item.media_type || "Media item")}</strong>
        <div class="meta">${esc(item.timestamp)}</div>
        ${googleLink(item.permalink, "Open Instagram post ↗")}</div></div>`).join("")
      : '<p class="empty">No media items found.</p>';
  } catch (error) {
    $("instagramProfile").innerHTML = `<p class="empty">${esc(error.message)}</p>`;
    $("instagramMedia").innerHTML = "";
  } finally {
    button.disabled = false;
  }
});

$("whatsappForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("whatsappSend");
  button.disabled = true;
  $("whatsappSendStatus").textContent = "Sending…";
  try {
    const result = await api("/api/whatsapp/messages", {
      method: "POST",
      body: JSON.stringify({
        recipient: $("whatsappRecipient").value.trim(),
        message: $("whatsappMessage").value.trim(),
        consent_confirmed: $("whatsappConsent").checked
      })
    });
    $("whatsappMessage").value = "";
    $("whatsappConsent").checked = false;
    $("whatsappSendStatus").textContent = `Accepted by WhatsApp (${result.status}).`;
    await loadWhatsAppMessages();
  } catch (error) {
    $("whatsappSendStatus").textContent = error.message;
    showNotice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("commandForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("commandMessage").textContent = "Interpreting command…";
  try {
    const result = await api("/api/assistant/command", {
      method: "POST",
      body: JSON.stringify({ instruction: $("command").value.trim() })
    });
    $("commandMessage").textContent = result.reply;
    $("command").value = "";
    await Promise.all([loadTasks(), loadBriefing(), loadChat()]);
  } catch (error) {
    $("commandMessage").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("chatForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("chatSend");
  const message = $("chatInput").value.trim();
  if (!message) return;
  button.disabled = true;
  $("voiceStatus").textContent = "JARVIS is responding…";
  try {
    const result = await api("/api/assistant/chat", {
      method: "POST",
      body: JSON.stringify({
        message,
        web_search: $("webSearch").checked
      })
    });
    $("chatInput").value = "";
    await loadChat();
    if (result.created_task) await Promise.all([loadTasks(), loadBriefing()]);
    if ($("readAloud").checked && "speechSynthesis" in window) {
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(result.reply));
    }
    $("voiceStatus").textContent = result.sources.length
      ? `Answered using ${result.sources.length} web search result(s).`
      : "Reply received.";
  } catch (error) {
    $("voiceStatus").textContent = error.message;
    showNotice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("voiceButton").addEventListener("click", () => {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    $("voiceStatus").textContent = "Voice input is not supported by this browser.";
    return;
  }
  const recognition = new Recognition();
  recognition.lang = navigator.language || "en-US";
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  $("voiceButton").disabled = true;
  $("voiceStatus").textContent = "Listening…";
  recognition.onresult = event => {
    $("chatInput").value = event.results[0][0].transcript;
    $("chatInput").focus();
    $("voiceStatus").textContent = "Speech captured. Review it, then send.";
  };
  recognition.onerror = event => {
    $("voiceStatus").textContent = `Voice input failed: ${event.error}`;
  };
  recognition.onend = () => {
    $("voiceButton").disabled = false;
  };
  recognition.start();
});

document.addEventListener("click", async event => {
  const actionButton = event.target.closest("button[data-action]");
  const memoryButton = event.target.closest("button[data-memory-delete]");
  const sourceButton = event.target.closest("button[data-source-delete]");
  try {
    if (sourceButton) {
      await api(`/api/task-sources/${sourceButton.dataset.sourceDelete}`, { method: "DELETE" });
      await Promise.all([loadTaskSources(), loadIntegrations()]);
      return;
    }
    if (memoryButton) {
      await api(`/api/memory/${encodeURIComponent(memoryButton.dataset.memoryDelete)}`, { method: "DELETE" });
      await loadMemory();
      return;
    }
    if (!actionButton) return;
    const { action, id } = actionButton.dataset;
    actionButton.disabled = true;
    if (action === "delete") {
      const task = (await api("/api/tasks")).find(item => String(item.id) === id);
      if (!task || !window.confirm(`Permanently delete "${task.title}" and its findings, run history, and configured feeds?`)) return;
      await api(`/api/tasks/${id}`, { method: "DELETE" });
      showNotice(`Deleted task "${task.title}" and its related records. Audit history was retained.`);
      await Promise.all([loadTasks(), loadFindings(), loadRuns(), loadBriefing(), loadTaskSources(), loadIntegrations()]);
    } else if (action === "edit") {
      const task = (await api("/api/tasks")).find(item => String(item.id) === id);
      if (!task) throw new Error("Task not found");
      $("editId").value = task.id;
      $("editTitle").value = task.title;
      $("editInstruction").value = task.instruction;
      $("editLocation").value = task.location;
      $("editKeywords").value = task.keywords.join(", ");
      $("editInterval").value = task.interval_minutes;
      $("editDialog").showModal();
    } else if (action === "run") {
      const result = await api(`/api/tasks/${id}/run`, { method: "POST" });
      const issues = result.errors?.length ? ` Partial source errors: ${result.errors.join("; ")}` : "";
      showNotice(`Discovery checked ${result.checked} items and saved ${result.new_findings} new findings.${issues}`, Boolean(result.errors?.length));
      await refreshAll(false);
    } else {
      await api(`/api/tasks/${id}/${action}`, { method: "POST" });
      await Promise.all([loadTasks(), health()]);
    }
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    if (actionButton) actionButton.disabled = false;
  }
});

$("saveEdit").addEventListener("click", async () => {
  $("saveEdit").disabled = true;
  try {
    await api(`/api/tasks/${$("editId").value}`, { method: "PATCH", body: JSON.stringify({
      title: $("editTitle").value.trim(),
      instruction: $("editInstruction").value.trim(),
      location: $("editLocation").value.trim(),
      keywords: $("editKeywords").value.split(",").map(word => word.trim()).filter(Boolean),
      interval_minutes: Number($("editInterval").value || 0)
    }) });
    $("editDialog").close();
    await loadTasks();
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    $("saveEdit").disabled = false;
  }
});

document.addEventListener("change", async event => {
  const select = event.target.closest("select[data-finding-id]");
  if (!select) return;
  select.disabled = true;
  try {
    await api(`/api/findings/${select.dataset.findingId}`, {
      method: "PATCH", body: JSON.stringify({ status: select.value })
    });
    await Promise.all([loadFindings(), loadBriefing()]);
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    select.disabled = false;
  }
});

$("preferencesForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/preferences", { method: "PUT", body: JSON.stringify({
      default_location: $("prefLocation").value.trim(),
      default_keywords: $("prefKeywords").value.split(",").map(word => word.trim()).filter(Boolean),
      quiet_hours_start: $("quietStart").value || null,
      quiet_hours_end: $("quietEnd").value || null,
      morning_briefing: $("morningBriefing").checked
    }) });
    $("briefing").closest(".panel").hidden = !$("morningBriefing").checked;
    $("preferencesMessage").textContent = "Preferences saved.";
  } catch (error) {
    $("preferencesMessage").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("memoryForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/memory", { method: "PUT", body: JSON.stringify({
      key: $("memoryKey").value.trim(), value: $("memoryValue").value.trim()
    }) });
    $("memoryForm").reset();
    await loadMemory();
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("emergencyButton").addEventListener("click", async () => {
  const stopping = $("emergencyButton").textContent === "Emergency stop";
  try {
    await api(`/api/system/${stopping ? "stop" : "resume"}`, { method: "POST" });
    await health();
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("saveToken").addEventListener("click", async () => {
  const value = $("apiToken").value.trim();
  if (value) sessionStorage.setItem(tokenKey, value);
  else sessionStorage.removeItem(tokenKey);
  await refreshAll();
});
$("clearToken").addEventListener("click", () => {
  sessionStorage.removeItem(tokenKey);
  $("apiToken").value = "";
  showNotice("Browser-tab token cleared.");
});
$("apiToken").value = sessionStorage.getItem(tokenKey) || "";
$("refreshTasks").onclick = () => loadTasks().catch(error => showNotice(error.message, true));
$("refreshFindings").onclick = () => loadFindings().catch(error => showNotice(error.message, true));
$("refreshRuns").onclick = () => loadRuns().catch(error => showNotice(error.message, true));
$("refreshBriefing").onclick = () => loadBriefing().catch(error => showNotice(error.message, true));
$("refreshIntegrations").onclick = () => loadIntegrations().catch(error => showNotice(error.message, true));
$("refreshIntegrations").addEventListener("click", () => loadTaskSources().catch(error => showNotice(error.message, true)));
$("refreshWhatsApp").onclick = () => loadWhatsAppMessages().catch(error => showNotice(error.message, true));
$("findingFilter").onchange = () => loadFindings().catch(error => showNotice(error.message, true));
$("exportData").addEventListener("click", async () => {
  try {
    const data = await api("/api/export");
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `jarvis-export-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    showNotice("Data export downloaded.");
  } catch (error) {
    showNotice(error.message, true);
  }
});
refreshAll();
