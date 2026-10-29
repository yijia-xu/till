const ordersEl = document.querySelector("#orders");
const emptyEl = document.querySelector("#empty");
const detailEl = document.querySelector("#detail");
const flashEl = document.querySelector("#flash");
const policyMeta = document.querySelector("#policy-meta");
const policyRaw = document.querySelector("#policy-raw");
let selected = null;
let busy = false;

function money(minor) {
  if (minor === null || minor === undefined) return "—";
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  return `${sign}$${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

function badge(text, tone) {
  return `<span class="badge ${tone}">${text}</span>`;
}

function toneFor(order, field) {
  if (field === "approval") {
    if (order.approval === "approved") return "ok";
    if (order.approval === "rejected" || order.approval === "blocked") return "bad";
    return "wait";
  }
  if (field === "success") {
    if (order.success === "complete") return "ok";
    if (order.success === "failed") return "bad";
    return "wait";
  }
  if (order.malicious) return "bad";
  if (order.problem) return "wait";
  return "neutral";
}

function kindLabel(order) {
  if (order.malicious) return "Malicious";
  if (order.problem === "budget" || order.problem === "needs") return "Policy";
  if (order.status === "COMPLETE") return "Clear";
  if (order.status === "NEEDS_APPROVAL") return "Needs a person";
  return "Open";
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body.detail?.message || body.message || "Till could not do that.";
    throw new Error(message);
  }
  return body;
}

function setFlash(text, isError) {
  flashEl.textContent = text;
  flashEl.classList.toggle("error", Boolean(isError));
}

async function loadPolicy() {
  const policy = await api("/api/policy");
  policyRaw.value = policy.raw;
  const merchants = (policy.allowed_merchants || []).length
    ? `Allowed merchants: ${policy.allowed_merchants.join(", ")}.`
    : "No merchant allowlist yet.";
  const shipping = policy.include_shipping ? "Shipping counts toward the cap." : "Shipping is outside the cap until you say so.";
  policyMeta.textContent = `Version ${policy.version}. Cap ${money(policy.max_purchase_minor)} per purchase, ${money(policy.budget_remaining_minor)} left today. ${shipping} ${merchants}`;
}

function renderOrders(orders) {
  ordersEl.innerHTML = "";
  emptyEl.hidden = orders.length > 0;
  for (const order of orders) {
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    tr.setAttribute("aria-selected", order.id === selected ? "true" : "false");
    tr.innerHTML = `
      <td class="id">${order.id}</td>
      <td>${badge(order.approval, toneFor(order, "approval"))}</td>
      <td>${badge(order.success.replace("_", " "), toneFor(order, "success"))}</td>
      <td>${order.problem || "—"}</td>
      <td>${badge(kindLabel(order), order.malicious ? "bad" : order.problem ? "wait" : "ok")}</td>
      <td class="num">${money(order.payable_minor)}</td>`;
    tr.addEventListener("click", () => openDetail(order.id));
    tr.addEventListener("keydown", (event) => {
      if (event.key === "Enter") openDetail(order.id);
    });
    ordersEl.appendChild(tr);
  }
}

function view(title, lines) {
  return `<div class="view"><h3>${title}</h3>${lines.map((line) => `<p>${line}</p>`).join("")}</div>`;
}

function renderDetail(order) {
  detailEl.hidden = false;
  const listed = order.listed;
  const paypal = order.paypal;
  detailEl.innerHTML = `
    <h2>${order.id}</h2>
    <p>
      ${badge(order.approval, toneFor(order, "approval"))}
      ${badge(order.success.replaceAll("_", " "), toneFor(order, "success"))}
      ${badge(order.problem || "no problem", order.problem ? "wait" : "ok")}
      ${badge(order.malicious ? "Malicious" : "Not malicious", order.malicious ? "bad" : "ok")}
    </p>
    <p>${order.explanation}</p>
    <p>${order.citation || ""}</p>
    <div class="views">
      ${view("Asked", [order.asked.text, `Cap ${money(order.asked.max_purchase_minor)}`])}
      ${view("Claimed", [`${order.claimed.title}`, `${order.claimed.sku} × ${order.claimed.quantity}`, money(order.claimed.unit_price_minor)])}
      ${view("Listed", listed ? [listed.title, `${listed.sku} × 1`, `${money(listed.unit_price_minor)} + ship ${money(listed.shipping_minor)}`] : ["No catalog row"])}
      ${view("PayPal", paypal ? [`${paypal.order_id || "Order"} ${paypal.status || ""}`, `${paypal.sku || ""} × ${paypal.quantity || "—"}`, money(paypal.amount_minor)] : ["Not sent"])}
    </div>
    <p>Budget left today ${money(order.budget_remaining_minor)}. Comparable total ${money(order.capped_minor)}. PayPal charge ${money(order.payable_minor)}. Policy v${order.policy_version}.</p>
    <div class="row-actions">
      ${order.status === "NEEDS_APPROVAL" ? `<button type="button" data-act="approve" class="primary">Approve this cart</button><button type="button" data-act="reject">Reject</button>` : ""}
      ${order.problem === "budget" && !order.malicious ? `<button type="button" data-act="allow_once" class="primary">Allow once</button>` : ""}
      <button type="button" data-act="allow_merchant">Allow this merchant</button>
      <button type="button" data-act="include_shipping">Count shipping toward the cap</button>
      ${order.approve_url ? `<a href="${order.approve_url}">Open PayPal approval</a>` : ""}
      ${order.status === "PAYER_ACTION" ? `<button type="button" data-act="sync">Check PayPal</button>` : ""}
    </div>`;
  detailEl.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => act(order.id, button.dataset.act, button));
  });
}

async function openDetail(id) {
  selected = id;
  const order = await api(`/api/orders/${id}`);
  renderDetail(order);
  const orders = await api("/api/orders");
  renderOrders(orders.orders);
}

async function refresh(message) {
  await loadPolicy();
  const orders = await api("/api/orders");
  renderOrders(orders.orders);
  if (selected) {
    const still = orders.orders.find((order) => order.id === selected);
    if (still) renderDetail(still);
  }
  if (message) setFlash(message, false);
}

async function act(id, name, button) {
  button.disabled = true;
  try {
    if (name === "approve") await api(`/api/orders/${id}/approve`, { method: "POST" });
    else if (name === "reject") await api(`/api/orders/${id}/reject`, { method: "POST" });
    else if (name === "sync") await api(`/api/orders/${id}/sync`, { method: "POST" });
    else await api(`/api/orders/${id}/learn`, { method: "POST", body: JSON.stringify({ kind: name }) });
    selected = id;
    await refresh("Saved.");
  } catch (error) {
    setFlash(error.message, true);
  } finally {
    button.disabled = false;
  }
}

document.querySelectorAll("[data-scenario]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    document.querySelectorAll("[data-scenario]").forEach((item) => { item.disabled = true; });
    try {
      const order = await api(`/api/demo/${button.dataset.scenario}`, { method: "POST" });
      selected = order.id;
      const label = order.malicious ? "Blocked as malicious." : order.decision === "BLOCK" ? "Blocked by policy." : order.success === "complete" ? "PayPal capture matches this order." : "Till recorded the decision.";
      await refresh(`${order.id}: ${label}`);
      renderDetail(order.status ? await api(`/api/orders/${order.id}`) : order);
    } catch (error) {
      setFlash(error.message, true);
    } finally {
      busy = false;
      document.querySelectorAll("[data-scenario]").forEach((item) => { item.disabled = false; });
    }
  });
});

document.querySelector("#policy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/policy", { method: "PUT", body: JSON.stringify({ raw: policyRaw.value }) });
    await refresh("Policy saved.");
  } catch (error) {
    setFlash(error.message, true);
  }
});

document.querySelector("#reset").addEventListener("click", async () => {
  selected = null;
  detailEl.hidden = true;
  await api("/api/demo/reset", { method: "POST" });
  await refresh("Demo reset.");
});

document.querySelector("#mode").textContent = "Sandbox calls stay behind Till. This page shows the decision, not a chat.";
loadPolicy().then(refresh).catch((error) => setFlash(error.message, true));
