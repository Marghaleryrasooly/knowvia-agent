"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  let catalog = [];
  let epoch = 0;
  let busy = false;
  let knowledge = null;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function notice(text, kind = "") { $("notice").textContent = text; $("notice").className = kind; }
  function locked() {
    epoch += 1;
    catalog = [];
    $("dashboard").hidden = true;
    $("login-section").hidden = false;
    $("logout").hidden = true;
    for (const id of ["accounts", "activity", "provision-roles", "documents", "knowledge-roles"]) $(id).replaceChildren();
    knowledge = null;
    $("knowledge-form").reset();
    $("knowledge-status").textContent = "";
    $("provision-form").reset();
    $("administrator").textContent = "";
  }
  async function api(path, body) {
    const response = await fetch(path, {credentials: "same-origin", cache: "no-store", ...(body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
    })});
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("The server response could not be read."); }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) locked();
      // Display only the application's safe categories, never arbitrary response content.
      const messages = {401: "Please sign in again.", 403: "Administrator permission required or request origin rejected.",
        429: "Too many sign-in attempts. Please wait before trying again."};
      throw new Error(messages[response.status] || "The change could not be completed. Check required fields, role assignments and last-administrator protection.");
    }
    return data;
  }
  function roles(selected) {
    const wrap = node("div", undefined, "roles");
    for (const role of catalog) {
      const label = node("label", undefined, "role-choice");
      const input = node("input"); input.type = "checkbox"; input.value = role.name; input.checked = selected.includes(role.name);
      const copy = node("span", role.name);
      copy.append(node("small", role.permissions.length ? role.permissions.join(", ") : "No additional permissions"));
      label.append(input, copy); wrap.append(label);
    }
    return wrap;
  }
  const selectedRoles = (element) => Array.from(element.querySelectorAll("input:checked"), (input) => input.value);
  function renderAccounts(accounts) {
    const fragment = document.createDocumentFragment();
    for (const account of accounts) {
      const card = node("article", undefined, "account");
      const head = node("div", undefined, "account-head");
      const copy = node("div"); copy.append(node("h3", `${account.first_name} ${account.last_name}`));
      copy.append(node("div", `${account.employee_id} · ${account.work_email}`, "account-copy"));
      copy.append(node("div", `${account.department} · ${account.role}`, "account-copy"));
      head.append(copy, node("span", account.account_status, `badge${account.active ? "" : " disabled"}`));
      const fieldset = node("fieldset"); fieldset.append(node("legend", `Trusted roles for ${account.employee_id}`));
      const options = roles(account.roles); fieldset.append(options);
      const actions = node("div", undefined, "account-actions");
      const save = node("button", "Save trusted roles"); save.type = "button";
      save.addEventListener("click", () => perform(async () => {
        const selection = selectedRoles(options);
        if (!selection.length) throw new Error("Select at least one trusted role.");
        if (!window.confirm(`Update access roles for ${account.employee_id}?`)) return false;
        await api(`/api/admin/accounts/${encodeURIComponent(account.employee_id)}/roles`, {roles: selection});
        return true;
      }));
      actions.append(save);
      if (account.account_status !== "NOT_REGISTERED") {
        const activation = node("button", account.active ? "Deactivate account" : "Activate account", account.active ? "danger" : "");
        activation.type = "button";
        activation.addEventListener("click", () => perform(async () => {
          if (!window.confirm(`${account.active ? "Deactivate" : "Activate"} ${account.employee_id}?${account.active ? " All current sessions will be revoked." : ""}`)) return false;
          await api(`/api/admin/accounts/${encodeURIComponent(account.employee_id)}/activation`, {active: !account.active});
          return true;
        }));
        actions.append(activation);
      } else { actions.append(node("span", "Directory record only — no account yet.", "muted")); }
      card.append(head, fieldset, actions); fragment.append(card);
    }
    $("accounts").replaceChildren(fragment);
    if (!accounts.length) $("accounts").append(node("p", "No employee records available."));
  }
  function renderActivity(events) {
    const fragment = document.createDocumentFragment();
    for (const event of events) {
      const row = node("li", `${event.operation} · ${event.actor} → ${event.target}`);
      const time = node("time", event.timestamp); time.dateTime = event.timestamp; row.append(time);
      fragment.append(row);
    }
    $("activity").replaceChildren(fragment);
    if (!events.length) $("activity").append(node("li", "No administrative activity yet."));
  }
  async function load() {
    const current = epoch;
    const me = await api("/api/admin/me");
    const roleData = await api("/api/admin/roles");
    const accountData = await api("/api/admin/accounts");
    const auditData = await api("/api/admin/audit");
    const knowledgeData = await api("/api/admin/knowledge");
    if (current !== epoch) return;
    catalog = roleData.roles;
    knowledge = knowledgeData;
    $("administrator").textContent = `Signed in as ${me.employee_id}`;
    renderAccounts(accountData.accounts); renderActivity(auditData.events);
    $("provision-roles").replaceChildren(roles(["employee"]));
    renderKnowledge();
    $("login-section").hidden = true; $("dashboard").hidden = false; $("logout").hidden = false;
  }
  function knowledgeRoles() {
    const permission = {hr: "knowledge.hr.read", it_support: "knowledge.it.read", finance: "knowledge.finance.read"}[$("knowledge-skill").value];
    const choices = roles([]);
    for (const label of Array.from(choices.children)) {
      const input = label.querySelector("input");
      if (!catalog.find((role) => role.name === input.value).permissions.includes(permission)) label.remove();
    }
    $("knowledge-roles").replaceChildren(choices);
  }
  function renderKnowledge() {
    $("knowledge-status").textContent = knowledge.integrity_error ? "Knowledge integrity check failed. Employee retrieval remains blocked; restore the previous working version." : `Active version: ${knowledge.active_version || "reference"}. ${knowledge.staged ? "Unpublished changes exist." : "No staged changes."}`;
    const fragment = document.createDocumentFragment();
    for (const doc of knowledge.documents) {
      const row = node("article", undefined, "account");
      row.append(node("h3", doc.name), node("p", `${doc.skill} · ${doc.bytes} bytes · Reading roles: ${doc.roles.join(", ")}`, "muted"));
      const remove = node("button", "Stage removal", "danger"); remove.type = "button";
      remove.addEventListener("click", () => perform(async () => {
        if (!window.confirm(`Stage removal of ${doc.name}? Employees keep the current version until publication.`)) return false;
        await api("/api/admin/knowledge/remove", {name: doc.name, revision: knowledge.revision}); return true;
      }));
      row.append(remove); fragment.append(row);
    }
    $("documents").replaceChildren(fragment); knowledgeRoles();
    $("knowledge-publish").disabled = !knowledge.staged;
    $("knowledge-discard").disabled = !knowledge.staged;
    $("knowledge-rollback").disabled = !knowledge.rollback_available || knowledge.staged;
  }
  async function perform(operation, reload = true) {
    if (busy) return;
    busy = true;
    document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    try {
      const changed = await operation();
      if (changed !== false && reload) await load();
      if (changed !== false) notice("Completed successfully.", "success");
    } catch (error) { notice(error instanceof TypeError ? "Connection unavailable. The request was not retried." : error.message, "error"); }
    finally {
      busy = false; document.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      if (knowledge) {
        $("knowledge-publish").disabled = !knowledge.staged;
        $("knowledge-discard").disabled = !knowledge.staged;
        $("knowledge-rollback").disabled = !knowledge.rollback_available || knowledge.staged;
      }
    }
  }
  $("knowledge-skill").addEventListener("change", knowledgeRoles);
  $("knowledge-form").addEventListener("submit", (event) => {
    event.preventDefault();
    perform(async () => {
      const file = $("knowledge-file").files[0];
      if (!file || file.size > 131072) throw new Error("Choose a Markdown file no larger than 128 KiB.");
      const selected = selectedRoles($("knowledge-roles"));
      if (!selected.length) throw new Error("Select explicit document reading roles.");
      const bytes = await file.arrayBuffer();
      let content;
      try { content = new TextDecoder("utf-8", {fatal: true}).decode(bytes); }
      catch (_) { throw new Error("Markdown must use valid UTF-8 text."); }
      await api("/api/admin/knowledge/stage", {name: file.name, content, skill: $("knowledge-skill").value, roles: selected, revision: knowledge.revision});
      $("knowledge-form").reset(); return true;
    });
  });
  for (const operation of ["publish", "discard", "rollback"]) {
    $("knowledge-" + operation).addEventListener("click", () => perform(async () => {
      if (!window.confirm(`${operation === "publish" ? "Publish the validated staged corpus" : operation === "rollback" ? "Restore the previous working corpus" : "Discard all staged changes"}?`)) return false;
      if (operation === "publish") notice("Building and validating the staged knowledge index. Existing employee requests continue normally.");
      await api("/api/admin/knowledge/" + operation, {revision: knowledge.revision}); return true;
    }));
  }
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    perform(async () => {
      const form = event.currentTarget;
      try { await api("/api/auth/login", {work_email: form.elements.work_email.value, password: form.elements.password.value}); }
      finally { form.elements.password.value = ""; }
      return true;
    });
  });
  $("provision-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    perform(async () => {
      const body = Object.fromEntries(new FormData(form));
      body.roles = selectedRoles($("provision-roles"));
      try {
        if (!body.roles.length) throw new Error("Select at least one trusted role.");
        await api("/api/admin/accounts", body); form.reset();
      } finally { form.elements.password.value = ""; form.elements.password_confirmation.value = ""; }
      return true;
    });
  });
  $("refresh").addEventListener("click", () => perform(async () => { await load(); return true; }, false));
  $("logout").addEventListener("click", () => perform(async () => {
    await api("/api/auth/logout", {}); locked(); notice("Signed out."); return false;
  }, false));
  load().then(() => notice("Administrator access verified.")).catch((error) => { locked(); notice(error instanceof TypeError ? "Connection unavailable. Please try later." : error.message, "error"); });
})();
