const state = { chats: [], active: null, identity: null, employee: null, developmentMode: false, busy: false, authBusy: false, conversationAction: null, inFlight: null };
const $ = id => document.getElementById(id);
const storageKey = identity => `knowvia-demo-chats-v2:${identity}`;
const LEGACY_STORAGE_KEY = "knowvia-demo-chats-v1";
const MAX_TITLE_LENGTH = 80;
const SIDEBAR_WIDTH_KEY = "knowvia-sidebar-width-v1";
const SIDEBAR_MIN_WIDTH = 240;
const SIDEBAR_DEFAULT_WIDTH = 300;
const SIDEBAR_MAX_WIDTH = 480;

function boundedSidebarWidth(value) {
  const width = Number(value);
  return Number.isFinite(width) && width >= SIDEBAR_MIN_WIDTH && width <= SIDEBAR_MAX_WIDTH ? Math.round(width) : SIDEBAR_DEFAULT_WIDTH;
}
function initializeSidebarResize() {
  const shell = $("app-shell"); const divider = $("sidebar-resizer");
  let width = boundedSidebarWidth(localStorage.getItem(SIDEBAR_WIDTH_KEY));
  const apply = value => {
    width = boundedSidebarWidth(value); shell.style.setProperty("--sidebar-width", `${width}px`);
    divider.setAttribute("aria-valuemin", String(SIDEBAR_MIN_WIDTH)); divider.setAttribute("aria-valuemax", String(SIDEBAR_MAX_WIDTH)); divider.setAttribute("aria-valuenow", String(width));
  };
  apply(width);
  divider.addEventListener("pointerdown", event => {
    if (event.button !== 0 || window.matchMedia("(max-width: 780px)").matches) return;
    event.preventDefault(); divider.setPointerCapture(event.pointerId); divider.classList.add("dragging"); document.body.classList.add("sidebar-resizing");
  });
  divider.addEventListener("pointermove", event => {
    if (!divider.hasPointerCapture(event.pointerId)) return;
    const left = shell.getBoundingClientRect().left;
    apply(Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, event.clientX - left)));
  });
  const finishResize = event => {
    if (!divider.hasPointerCapture(event.pointerId)) return;
    divider.releasePointerCapture(event.pointerId); divider.classList.remove("dragging"); document.body.classList.remove("sidebar-resizing"); localStorage.setItem(SIDEBAR_WIDTH_KEY, String(width));
  };
  divider.addEventListener("pointerup", finishResize); divider.addEventListener("pointercancel", finishResize);
  divider.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? SIDEBAR_MIN_WIDTH : event.key === "End" ? SIDEBAR_MAX_WIDTH : width + (event.key === "ArrowLeft" ? -10 : 10);
    apply(Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, next))); localStorage.setItem(SIDEBAR_WIDTH_KEY, String(width));
  });
}

function uid() { return crypto.randomUUID ? crypto.randomUUID() : `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
class AuthenticationRequiredError extends Error {}
async function apiFetch(url, options) {
  const response = await fetch(url, options);
  if (response.status === 401 && !state.developmentMode) {
    showAuthentication("Your session has expired. Please sign in again.");
    throw new AuthenticationRequiredError();
  }
  return response;
}
function save() {
  if (!state.identity) return;
  const chats = state.chats.filter(chat => chat.saved);
  const active = chats.some(chat => chat.id === state.active) ? state.active : null;
  localStorage.setItem(storageKey(state.identity), JSON.stringify({ chats, active }));
}
function loadLocal() {
  if (!state.identity) { state.chats = []; state.active = null; return; }
  try {
    const value = JSON.parse(localStorage.getItem(storageKey(state.identity)));
    state.chats = Array.isArray(value?.chats) ? value.chats.filter(chat => chat?.saved) : [];
    state.active = typeof value?.active === "string" && state.chats.some(chat => chat.id === value.active) ? value.active : null;
  } catch { state.chats = []; state.active = null; }
}
function legacyTranscriptCache() {
  try {
    const value = JSON.parse(localStorage.getItem(LEGACY_STORAGE_KEY));
    return new Map((Array.isArray(value?.chats) ? value.chats : []).map(chat => [chat.id, chat]));
  } catch { return new Map(); }
}
function activeChat() { return state.chats.find(chat => chat.id === state.active); }
function newChat(render = true) {
  state.chats = state.chats.filter(chat => chat.saved || chat.pending);
  const chat = { id: uid(), title: "New conversation", messages: [], created: Date.now(), updated: Date.now(), pinned: false, saved: false };
  state.chats.unshift(chat); state.active = chat.id; save(); if (render) renderAll();
}
function ensureActiveChat() {
  if (!state.active || !state.chats.some(chat => chat.id === state.active)) {
    if (state.chats.length) state.active = state.chats[0].id;
    else newChat(false);
  }
}
function escapeHtml(value) { const div = document.createElement("div"); div.textContent = value ?? ""; return div.innerHTML; }
function initials(name) { return name.split(/\s+/).map(x => x[0]).join("").slice(0,2).toUpperCase(); }

async function loadConversations() {
  const requestedIdentity = state.identity;
  const cached = new Map(state.chats.map(chat => [chat.id, chat]));
  const legacy = legacyTranscriptCache();
  try {
    const response = await apiFetch(`/api/conversations?identity_id=${encodeURIComponent(requestedIdentity)}`);
    if (!response.ok) throw new Error("history unavailable");
    const data = await response.json();
    if (state.identity !== requestedIdentity) return;
    const records = Array.isArray(data.conversations) ? data.conversations : [];
    const unsaved = state.chats.filter(
      chat => !chat.saved && (chat.messages.length === 0 || chat.pending)
    );
    state.chats = records.map(item => {
      // Legacy transcripts are adopted only after the server has confirmed that
      // this session belongs to the selected identity.
      const previous = cached.get(item.session_id) || legacy.get(item.session_id);
      return { id: item.session_id, title: item.title, pinned: Boolean(item.pinned), created: Date.parse(item.created_at) || previous?.created || Date.now(), updated: Date.parse(item.updated_at) || previous?.updated || Date.now(), messages: previous?.messages || [], historyStatus: previous?.historyStatus || "unknown", saved: true, pending: Boolean(previous?.pending) };
    });
    for (const draft of [...unsaved].reverse()) if (!state.chats.some(chat => chat.id === draft.id)) state.chats.unshift(draft);
    ensureActiveChat(); save(); renderAll();
    const selected = activeChat();
    // Saved transcripts are server-authoritative. Always hydrate the selected
    // conversation so legacy proposal lifecycle changes replace stale cache.
    if (selected?.saved) await loadConversation(selected.id, false);
  } catch (error) { if (error instanceof AuthenticationRequiredError || state.identity !== requestedIdentity) return; ensureActiveChat(); renderAll(); }
}

async function loadConversation(id, closeSidebar = true) {
  const chat = state.chats.find(item => item.id === id); if (!chat) return;
  const requestedIdentity = state.identity;
  state.active = id; save(); renderAll();
  if (closeSidebar) $("sidebar").classList.remove("open");
  if (!chat.saved || chat.pending) return;
  try {
    const response = await apiFetch(`/api/conversations/${encodeURIComponent(id)}?identity_id=${encodeURIComponent(requestedIdentity)}`);
    if (!response.ok) throw new Error("conversation unavailable");
    const data = await response.json();
    if (state.identity !== requestedIdentity) return;
    const current = state.chats.find(item => item.id === id);
    if (!current) return;
    const serverMessages = Array.isArray(data.messages) ? data.messages : [];
    const cachedHasAssistant = current.messages.some(message => message.role === "assistant");
    if (data.history_status === "complete" || !cachedHasAssistant) {
      current.messages = serverMessages;
      current.historyStatus = data.history_status || (serverMessages.length ? "complete" : "unavailable");
    } else {
      // A full legacy browser transcript is retained only after the server has
      // already confirmed this session belongs to the selected identity.
      current.historyStatus = "complete";
    }
    save(); if (state.active === id) renderAll();
  } catch { /* Keep any identity-scoped cached transcript and the main UI usable. */ }
}

function conversationRow(chat) {
  const label = escapeHtml(chat.title);
  return `<div class="chat-row ${chat.id === state.active ? "active" : ""}" data-row="${escapeHtml(chat.id)}">
    <button class="chat-item" data-chat="${escapeHtml(chat.id)}" title="${label}">${label}</button>
    ${chat.saved ? `<button class="chat-menu-trigger" data-menu-trigger="${escapeHtml(chat.id)}" aria-label="Conversation options" aria-expanded="false">⋯</button>
    <div class="chat-menu" data-menu="${escapeHtml(chat.id)}" hidden>
      <button data-chat-action="pin" data-chat-id="${escapeHtml(chat.id)}">${chat.pinned ? "Unpin" : "Pin"}</button>
      <button data-chat-action="rename" data-chat-id="${escapeHtml(chat.id)}">Rename</button>
      <button data-chat-action="delete" data-chat-id="${escapeHtml(chat.id)}" class="danger">Delete</button>
    </div>` : ""}</div>`;
}
function conversationGroup(label, chats, hiddenWhenEmpty = false) {
  if (hiddenWhenEmpty && !chats.length) return "";
  return `<section class="chat-group"><div class="sidebar-label">${label}</div>${chats.map(conversationRow).join("")}</section>`;
}
function renderSidebar() {
  const pinned = state.chats.filter(chat => chat.saved && chat.pinned);
  const recent = state.chats.filter(chat => chat.saved && !chat.pinned);
  $("chat-list").innerHTML = conversationGroup("Pinned", pinned, true) + conversationGroup("Recent", recent);
  document.querySelectorAll("[data-chat]").forEach(button => button.onclick = () => loadConversation(button.dataset.chat));
  document.querySelectorAll("[data-menu-trigger]").forEach(button => button.onclick = event => {
    event.stopPropagation(); const menu = document.querySelector(`[data-menu="${CSS.escape(button.dataset.menuTrigger)}"]`); const opening = menu.hidden;
    closeConversationMenus(); menu.hidden = !opening; button.setAttribute("aria-expanded", String(opening));
  });
  document.querySelectorAll("[data-chat-action]").forEach(button => button.onclick = event => { event.stopPropagation(); manageConversation(button.dataset.chatAction, button.dataset.chatId); });
}
function closeConversationMenus() {
  document.querySelectorAll(".chat-menu").forEach(menu => menu.hidden = true);
  document.querySelectorAll("[data-menu-trigger]").forEach(button => button.setAttribute("aria-expanded", "false"));
}
async function updateConversation(id, update) {
  const response = await apiFetch(`/api/conversations/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ identity_id: state.identity, ...update }) });
  if (!response.ok) throw new Error("conversation update failed");
  await loadConversations();
}
async function manageConversation(action, id) {
  closeConversationMenus(); const chat = state.chats.find(item => item.id === id); if (!chat) return;
  try {
    if (action === "pin") await updateConversation(id, { pinned: !chat.pinned });
    if (action === "rename" || action === "delete") openConversationModal(action, chat);
  } catch { openConversationModal("error", chat); }
}

function openConversationModal(mode, chat) {
  const modal = $("conversation-modal");
  state.conversationAction = { mode, id: chat.id, busy: false, returnFocus: document.activeElement };
  $("conversation-modal-error").hidden = true;
  $("conversation-modal-name").hidden = mode !== "delete";
  $("conversation-modal-name").textContent = mode === "delete" ? chat.title : "";
  $("conversation-rename-field").hidden = mode !== "rename";
  $("conversation-modal-cancel").textContent = mode === "error" ? "Close" : "Cancel";
  $("conversation-modal-submit").hidden = mode === "error";
  $("conversation-modal-submit").classList.toggle("danger", mode === "delete");
  if (mode === "delete") {
    $("conversation-modal-title").textContent = "Delete conversation?";
    $("conversation-modal-copy").textContent = "This conversation will be permanently deleted.";
    $("conversation-modal-submit").textContent = "Delete";
  } else if (mode === "rename") {
    $("conversation-modal-title").textContent = "Rename conversation";
    $("conversation-modal-copy").textContent = "Choose a concise title for this conversation.";
    $("conversation-modal-submit").textContent = "Save";
    $("conversation-rename-input").value = chat.title;
  } else {
    $("conversation-modal-title").textContent = "Couldn't update conversation";
    $("conversation-modal-copy").textContent = "Please try again.";
  }
  modal.showModal();
  requestAnimationFrame(() => mode === "rename" ? $("conversation-rename-input").focus() : $("conversation-modal-cancel").focus());
}

function closeConversationModal() {
  const modal = $("conversation-modal"); const action = state.conversationAction;
  if (action?.busy) return;
  if (modal.open) modal.close(); state.conversationAction = null;
  if (action?.returnFocus?.isConnected) action.returnFocus.focus();
}

async function submitConversationModal(event) {
  event.preventDefault(); const action = state.conversationAction; if (!action || action.busy) return;
  const chat = state.chats.find(item => item.id === action.id); if (!chat) { closeConversationModal(); return; }
  let title = "";
  if (action.mode === "rename") {
    title = $("conversation-rename-input").value.trim();
    if (!title || title.length > MAX_TITLE_LENGTH) {
      $("conversation-modal-error").textContent = `Enter a title between 1 and ${MAX_TITLE_LENGTH} characters.`;
      $("conversation-modal-error").hidden = false; $("conversation-rename-input").focus(); return;
    }
  }
  action.busy = true; $("conversation-modal-submit").disabled = true; $("conversation-modal-cancel").disabled = true;
  try {
    if (action.mode === "rename") await updateConversation(action.id, { title });
    if (action.mode === "delete") {
      if (state.inFlight?.sessionId === action.id) {
        action.busy = false;
        $("conversation-modal-error").textContent = "Wait for this conversation's response before deleting it.";
        $("conversation-modal-error").hidden = false;
        return;
      }
      const response = await apiFetch(`/api/conversations/${encodeURIComponent(action.id)}?identity_id=${encodeURIComponent(state.identity)}`, { method: "DELETE" });
      if (!response.ok) throw new Error("conversation deletion failed");
      state.chats = state.chats.filter(item => item.id !== action.id);
      if (state.active === action.id) { state.active = null; newChat(false); }
      ensureActiveChat(); save(); renderAll();
    }
    action.busy = false; closeConversationModal();
  } catch {
    action.busy = false; $("conversation-modal-error").textContent = "I couldn't update that conversation. Please try again.";
    $("conversation-modal-error").hidden = false;
  } finally {
    $("conversation-modal-submit").disabled = false; $("conversation-modal-cancel").disabled = false;
  }
}

// Deliberately bounded Markdown: only generated paragraphs, lists and bold.
// Escape all input first; HTML and Markdown links remain inert visible text.
function renderAnswerMarkdown(value) {
  const inline = text => escapeHtml(text).replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  const blocks = [];
  let paragraph = [], items = [], listType = null;
  const flushParagraph = () => { if (paragraph.length) blocks.push(`<p>${paragraph.map(inline).join("<br>")}</p>`); paragraph = []; };
  const flushList = () => { if (items.length) blocks.push(`<${listType}>${items.map(item => `<li>${inline(item)}</li>`).join("")}</${listType}>`); items = []; listType = null; };
  for (const line of String(value ?? "").replace(/\r\n?/g, "\n").split("\n")) {
    if (!line.trim()) { flushParagraph(); flushList(); continue; }
    const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (bullet || numbered) {
      flushParagraph();
      const type = bullet ? "ul" : "ol";
      if (listType && listType !== type) flushList();
      listType = type; items.push((bullet || numbered)[1]);
    } else { flushList(); paragraph.push(line); }
  }
  flushParagraph(); flushList();
  return blocks.join("");
}

function renderMessage(message) {
  if (message.role === "user") return `<article class="message user"><div class="bubble">${escapeHtml(message.text)}</div></article>`;
  const sources = (message.sources || []).length ? `<div class="source-block"><div class="source-label">Supporting sources</div><div class="source-grid" aria-label="Supporting sources">${message.sources.map(s => `<div class="source-card"><small>${escapeHtml(s.document)}</small><strong>${escapeHtml(s.section)}</strong></div>`).join("")}</div></div>` : "";
  const actionStatus = message.action?.status || "PROPOSED";
  const actionPending = actionStatus === "PROPOSED" || actionStatus === "PENDING";
  const actionLabel = actionPending ? "Ready for your approval" : actionStatus === "CANCELLED" ? "Cancelled" : actionStatus === "EXECUTED" ? (message.action?.ticket_id ? "Created" : "Completed") : actionStatus === "FAILED" ? "Failed" : actionStatus === "EXPIRED" ? "Expired" : "Unavailable";
  const actionReference = message.action?.ticket_id ? `<div class="card-reference">Ticket: ${escapeHtml(message.action.ticket_id)}</div>` : "";
  const action = message.action ? `<div class="action-card ${actionPending ? "" : `terminal ${actionStatus.toLowerCase()}`}" data-proposal="${escapeHtml(message.action.proposal_id)}"><div class="card-kicker">${actionLabel}</div><div class="card-title">${escapeHtml(message.action.title)}</div><div class="card-description">${escapeHtml(message.action.description)}</div>${actionReference}${actionPending ? `<div class="card-actions"><button class="confirm">Create ticket</button><button class="cancel">Cancel</button></div>` : ""}</div>` : "";
  const ticket = message.ticket ? `<div class="ticket-card"><div class="ticket-icon">✓</div><div><small>IT ticket</small><div class="card-title">${escapeHtml(message.ticket.ticket_id)}</div></div><span class="status-pill">${escapeHtml(message.ticket.status)}</span></div>` : "";
  const demo = message.demo ? `<details class="demo-card"><summary>Demo details</summary><div>Skills: ${escapeHtml((message.demo.skills || []).join(", ") || "none")}</div><div>Capabilities: ${escapeHtml((message.demo.capabilities || []).join(" → "))}</div><div>Grounding: ${escapeHtml(message.demo.grounding)}</div><div>Action transport: ${escapeHtml(message.demo.action_transport)}</div><div>Authorization: ${escapeHtml(message.demo.authorization)}</div></details>` : "";
  return `<article class="message assistant ${escapeHtml((message.kind || "").toLowerCase())}"><div class="assistant-label">Knowvia</div><div class="bubble answer-markdown">${renderAnswerMarkdown(message.text)}</div>${sources}${action}${ticket}${demo}</article>`;
}
function bindActionCards() { document.querySelectorAll(".action-card").forEach(card => { const confirm = card.querySelector(".confirm"); const cancel = card.querySelector(".cancel"); if (confirm) confirm.onclick = () => proposal(card, "confirm"); if (cancel) cancel.onclick = () => proposal(card, "cancel"); }); }
function renderAll() {
  renderSidebar(); ensureActiveChat(); const chat = activeChat(); const has = chat.messages.length > 0;
  const historyNotice = chat.historyStatus === "unavailable"
    ? `<div class="history-notice">This older conversation's message history is unavailable.</div>`
    : chat.historyStatus === "partial"
      ? `<div class="history-notice">Some messages from this older conversation are unavailable.</div>`
      : "";
  const hasContent = has || Boolean(historyNotice);
  $("welcome").hidden = hasContent; document.querySelector(".workspace").classList.toggle("welcome-state", !hasContent);
  const typing = chat.pending ? `<article class="message assistant"><div class="assistant-label">Knowvia</div><div class="typing"><i></i><i></i><i></i></div></article>` : "";
  $("messages").innerHTML = chat.messages.map(renderMessage).join("") + historyNotice + typing; bindActionCards(); requestAnimationFrame(() => { $("chat").scrollTop = $("chat").scrollHeight; });
}
function addMessageToConversation(sessionId, message) {
  const chat = state.chats.find(item => item.id === sessionId);
  if (!chat) return false;
  chat.messages.push(message); save();
  if (state.active === sessionId) renderAll(); else renderSidebar();
  return true;
}
function setBusy(value) { state.busy = value; $("send-button").disabled = value; }
async function send(text) {
  if (!text.trim() || state.busy) return;
  const chat = activeChat(); const sessionId = chat.id; const identityId = state.identity; const requestId = uid();
  if (!chat.messages.length) chat.title = text.trim().slice(0, 38);
  chat.pending = true; state.inFlight = { requestId, sessionId, identityId, kind: "chat" };
  addMessageToConversation(sessionId, { role: "user", text: text.trim() }); setBusy(true);
  try {
    const accepted = await apiFetch(`/api/conversations/${encodeURIComponent(sessionId)}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ identity_id: identityId, message: text.trim() }) });
    if (!accepted.ok) throw new Error("conversation message not accepted");
    chat.saved = true;
    await loadConversations();
    const response = await apiFetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId, identity_id: identityId, message: text.trim(), demo: $("demo-toggle").checked }) });
    const data = await response.json(); const owner = state.chats.find(item => item.id === sessionId);
    if (state.identity === identityId && owner) { owner.saved = response.ok; addMessageToConversation(sessionId, { role: "assistant", ...data }); await loadConversations(); }
  } catch (error) { if (!(error instanceof AuthenticationRequiredError) && state.identity === identityId) addMessageToConversation(sessionId, { role: "assistant", kind: "UNSUPPORTED", text: "I couldn't reach Knowvia. Please try again later.", sources: [] }); }
  finally {
    const owner = state.chats.find(item => item.id === sessionId); if (owner) owner.pending = false;
    if (state.inFlight?.requestId === requestId) state.inFlight = null;
    setBusy(false); if (state.active === sessionId) renderAll(); else renderSidebar(); $("message-input").focus();
  }
}
async function proposal(card, action) {
  if (state.busy) return; const sessionId = state.active; const identityId = state.identity; const requestId = uid();
  const owner = state.chats.find(item => item.id === sessionId); if (owner) owner.pending = true;
  state.inFlight = { requestId, sessionId, identityId, kind: "proposal" }; card.querySelectorAll("button").forEach(button => button.disabled = true); setBusy(true); renderAll();
  try {
    const response = await apiFetch(`/api/proposals/${encodeURIComponent(card.dataset.proposal)}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId, identity_id: identityId, demo: $("demo-toggle").checked }) });
    const data = await response.json();
    if (response.ok && data.proposal_update?.status) {
      const proposalId = data.proposal_update.proposal_id;
      for (const chat of state.chats) for (const message of chat.messages) {
        if (message.action?.proposal_id === proposalId) {
          message.action.status = data.proposal_update.status;
          if (data.proposal_update.ticket_id) message.action.ticket_id = data.proposal_update.ticket_id;
        }
      }
    }
    if (state.identity === identityId) addMessageToConversation(sessionId, { role: "assistant", ...data });
  } catch (error) { if (!(error instanceof AuthenticationRequiredError) && state.identity === identityId) addMessageToConversation(sessionId, { role: "assistant", text: "I couldn't safely process that confirmation.", sources: [] }); }
  finally {
    const current = state.chats.find(item => item.id === sessionId); if (current) current.pending = false;
    if (state.inFlight?.requestId === requestId) state.inFlight = null;
    setBusy(false); if (state.active === sessionId) renderAll(); else renderSidebar();
  }
}
async function loadNotifications() {
  try {
    const response = await apiFetch(`/api/notifications?identity_id=${encodeURIComponent(state.identity)}`); const data = await response.json(); const list = data.notifications || [];
    $("notification-count").style.display = list.length ? "block" : "none";
    $("notification-list").innerHTML = list.length ? list.map(n => `<div class="notification"><strong>${escapeHtml(n.ticket_id)}</strong><div>${escapeHtml(n.message)}</div></div>`).join("") : `<div class="empty">No notifications yet.</div>`;
  } catch { $("notification-count").style.display = "none"; }
}
function clearEmployeeState(removeCache = false) {
  if (removeCache && state.identity) localStorage.removeItem(storageKey(state.identity));
  state.chats = []; state.active = null; state.employee = null; state.identity = null; state.inFlight = null; state.busy = false;
  $("notification-list").innerHTML = ""; $("notification-count").style.display = "none";
  $("notification-panel").hidden = true; $("profile-popover").hidden = true;
}
function showAuthentication(message = "") {
  clearEmployeeState(true); $("app-shell").hidden = true; $("auth-screen").hidden = false;
  $("auth-loading").hidden = true; $("auth-content").hidden = false;
  selectAuthMode("signin"); $("signin-message").textContent = message;
  $("signin-password").value = ""; $("register-password").value = ""; $("register-confirmation").value = "";
  requestAnimationFrame(() => $("signin-email").focus());
}
function selectAuthMode(mode) {
  const signingIn = mode === "signin";
  $("signin-form").hidden = !signingIn; $("register-form").hidden = signingIn;
  $("signin-tab").setAttribute("aria-selected", String(signingIn)); $("register-tab").setAttribute("aria-selected", String(!signingIn));
  $("auth-title").textContent = signingIn ? "Welcome back" : "Create your account";
  $("auth-intro").textContent = signingIn ? "Sign in with your company account to continue to Knowvia." : "Verify your NovaTech employee record and choose a secure password.";
  $("signin-message").textContent = ""; $("register-message").textContent = "";
}
function renderProfile(employee) {
  const name = employee.display_name || employee.work_email;
  $("profile-avatar").textContent = initials(name); $("profile-popover-avatar").textContent = initials(name);
  $("profile-name").textContent = name; $("profile-department").textContent = employee.department;
  $("profile-full-name").textContent = name; $("profile-email").textContent = employee.work_email;
  $("profile-employee-id").textContent = employee.employee_id; $("profile-detail-department").textContent = employee.department;
  $("profile-role").textContent = employee.role;
}
async function enterAuthenticatedApplication(employee) {
  if (!employee?.employee_id) { showAuthentication(); return; }
  if (state.identity && state.identity !== employee.employee_id) clearEmployeeState(true);
  state.employee = employee; state.identity = employee.employee_id; state.developmentMode = false;
  loadLocal(); ensureActiveChat(); renderProfile(employee);
  $("developer-menu").hidden = true; $("profile-menu").hidden = false;
  $("auth-screen").hidden = true; $("app-shell").hidden = false; renderAll();
  await Promise.all([loadConversations(), loadNotifications()]);
}
async function initializeDevelopmentMode(config) {
  state.developmentMode = true; $("auth-screen").hidden = true; $("app-shell").hidden = false;
  $("developer-menu").hidden = false; $("profile-menu").hidden = true;
  $("identity-select").innerHTML = config.identities.map(identity => `<option value="${escapeHtml(identity.id)}">${escapeHtml(identity.name)}</option>`).join("");
  state.identity = config.default_identity; $("identity-select").value = state.identity; loadLocal();
  renderIdentityOptions(config.identities); updateIdentity(config.identities.find(identity => identity.id === state.identity)); ensureActiveChat(); renderAll();
  await Promise.all([loadConversations(), loadNotifications()]);
}
async function initialize() {
  try {
    const configResponse = await fetch("/api/config"); const config = await configResponse.json();
    $("auth-demo-guidance").hidden = !config.demo_registration_guidance;
    $("register-tab").hidden = !config.registration_enabled;
    $("developer-details-control").hidden = !config.developer_details_enabled;
    if (!config.developer_details_enabled) $("demo-toggle").checked = false;
    if (config.development_identity_override) { await initializeDevelopmentMode(config); return; }
    const response = await fetch("/api/auth/me");
    if (!response.ok) { showAuthentication(); return; }
    const data = await response.json(); await enterAuthenticatedApplication(data.employee);
  } catch { showAuthentication("Knowvia couldn't check your session. Please try signing in."); }
}
function updateIdentity(identity) {
  const name = identity?.name || state.identity; $("avatar").textContent = initials(name); $("identity-name").textContent = name;
  document.querySelectorAll(".identity-option").forEach(button => button.classList.toggle("active", button.dataset.identity === state.identity));
}
function renderIdentityOptions(identities) {
  $("identity-options").innerHTML = identities.map(identity => `<button class="identity-option ${identity.id === state.identity ? "active" : ""}" data-identity="${escapeHtml(identity.id)}">${escapeHtml(identity.name)}</button>`).join("");
  document.querySelectorAll(".identity-option").forEach(button => button.onclick = async () => {
    save(); state.identity = button.dataset.identity; $("identity-select").value = state.identity; loadLocal(); ensureActiveChat(); updateIdentity(identities.find(identity => identity.id === state.identity)); renderAll();
    await Promise.all([loadConversations(), loadNotifications()]); $("developer-popover").hidden = true; $("developer-trigger").setAttribute("aria-expanded", "false");
  });
}
async function submitSignIn(event) {
  event.preventDefault(); if (state.authBusy) return;
  state.authBusy = true; $("signin-submit").disabled = true; $("signin-message").textContent = "Signing in…";
  try {
    const response = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ work_email: $("signin-email").value.trim(), password: $("signin-password").value }) });
    const data = await response.json();
    if (!response.ok) { $("signin-password").value = ""; $("signin-message").textContent = "Authentication failed. Check your details and try again."; return; }
    $("signin-password").value = ""; await enterAuthenticatedApplication(data.employee);
  } catch { $("signin-password").value = ""; $("signin-message").textContent = "Knowvia couldn't sign you in. Please try again."; }
  finally { state.authBusy = false; $("signin-submit").disabled = false; }
}
async function submitRegistration(event) {
  event.preventDefault(); if (state.authBusy) return;
  const password = $("register-password").value; const confirmation = $("register-confirmation").value;
  if (password !== confirmation) { $("register-message").textContent = "Passwords do not match."; return; }
  state.authBusy = true; $("register-submit").disabled = true; $("register-message").textContent = "Creating your account…";
  const request = { employee_id: $("register-employee-id").value.trim(), work_email: $("register-email").value.trim(), password, password_confirmation: confirmation };
  try {
    const response = await fetch("/api/auth/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
    if (!response.ok) { $("register-password").value = ""; $("register-confirmation").value = ""; $("register-message").textContent = "Registration could not be completed. Check your details and try again."; return; }
    $("register-password").value = ""; $("register-confirmation").value = ""; $("signin-email").value = request.work_email;
    selectAuthMode("signin"); $("signin-message").textContent = "Account created. Sign in to continue."; $("signin-password").focus();
  } catch { $("register-password").value = ""; $("register-confirmation").value = ""; $("register-message").textContent = "Knowvia couldn't create your account. Please try again."; }
  finally { state.authBusy = false; $("register-submit").disabled = false; }
}
async function signOut() {
  if (state.authBusy) return; state.authBusy = true; $("signout-button").disabled = true;
  try { await fetch("/api/auth/logout", { method: "POST" }); }
  finally { state.authBusy = false; $("signout-button").disabled = false; showAuthentication(); }
}
$("composer").onsubmit = event => { event.preventDefault(); const input = $("message-input"); const text = input.value; input.value = ""; input.style.height = "auto"; send(text); };
$("message-input").oninput = event => { event.target.style.height = "auto"; event.target.style.height = `${Math.min(event.target.scrollHeight, 140)}px`; };
$("message-input").onkeydown = event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("composer").requestSubmit(); } };
$("new-chat").onclick = () => newChat(); $("mobile-menu").onclick = () => $("sidebar").classList.toggle("open");
$("conversation-modal-form").onsubmit = submitConversationModal;
$("conversation-modal-cancel").onclick = closeConversationModal;
$("conversation-modal").oncancel = event => { event.preventDefault(); closeConversationModal(); };
$("conversation-modal").onclick = event => { if (event.target === $("conversation-modal")) closeConversationModal(); };
document.querySelectorAll("[data-prompt]").forEach(button => button.onclick = () => send(button.dataset.prompt));
$("developer-trigger").onclick = event => { event.stopPropagation(); const popover = $("developer-popover"); popover.hidden = !popover.hidden; $("developer-trigger").setAttribute("aria-expanded", String(!popover.hidden)); };
$("developer-popover").onclick = event => event.stopPropagation();
$("profile-trigger").onclick = event => { event.stopPropagation(); const popover = $("profile-popover"); popover.hidden = !popover.hidden; $("profile-trigger").setAttribute("aria-expanded", String(!popover.hidden)); };
$("profile-popover").onclick = event => event.stopPropagation();
$("signin-tab").onclick = () => selectAuthMode("signin"); $("register-tab").onclick = () => selectAuthMode("register");
$("signin-form").onsubmit = submitSignIn; $("register-form").onsubmit = submitRegistration; $("signout-button").onclick = signOut;
document.querySelectorAll("[data-password-toggle]").forEach(button => button.onclick = () => { const field = $(button.dataset.passwordToggle); const showing = field.type === "text"; field.type = showing ? "password" : "text"; button.textContent = showing ? "Show" : "Hide"; button.setAttribute("aria-label", `${showing ? "Show" : "Hide"} password`); });
document.addEventListener("click", () => { $("developer-popover").hidden = true; $("developer-trigger").setAttribute("aria-expanded", "false"); $("profile-popover").hidden = true; $("profile-trigger").setAttribute("aria-expanded", "false"); closeConversationMenus(); });
$("notification-button").onclick = async () => { await loadNotifications(); $("notification-panel").hidden = !$("notification-panel").hidden; };
$("close-notifications").onclick = () => $("notification-panel").hidden = true;
initializeSidebarResize();
initialize();
