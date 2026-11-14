const ROW_H = 52;
const COLUMNS = [
  { id: "hash", label: "Tx Hash", kind: "text" },
  { id: "method", label: "Method", kind: "text" },
  { id: "block", label: "Block", kind: "number", align: "num" },
  { id: "age", label: "Age", kind: "number" },
  { id: "from", label: "From", kind: "text" },
  { id: "to", label: "To", kind: "text" },
  { id: "amount_token", label: "Amount (Token)", kind: "number", align: "num" },
  { id: "amount_eth", label: "Amount (ETH)", kind: "number", align: "num" },
  { id: "token", label: "Token", kind: "text" },
  { id: "count", label: "Count", kind: "number", align: "num" },
];
const TIPS = {
  hash: "Sort by transaction hash.",
  method: "Sort by method and protocol.",
  block: "Sort by block number.",
  age: "Sorts by the block timestamp. Relative and Absolute only change the writing.",
  from: "Sort by the sender label or address.",
  to: "Sort by the recipient label or address.",
  amount_token: "ERC-20 amount moved in this transfer.",
  amount_eth: "Net ETH plus WETH for this wallet in the whole transaction. Rows that share a hash share this value.",
  token: "Sort by token name.",
  count: "Distinct transactions that moved this token in the selected range.",
};
const CHIPS = [
  { id: "v2", label: "Uniswap V2", test: (row) => row.protocol.includes("Uniswap V2") || row.search.includes("uniswap v2") },
  { id: "v3", label: "Uniswap V3", test: (row) => row.protocol.includes("Uniswap V3") || row.search.includes("uniswap v3") },
  { id: "v4", label: "Uniswap V4", test: (row) => row.protocol.includes("Uniswap V4") || row.search.includes("uniswap v4") },
  { id: "usdt", label: "USDT", test: (row) => (row.token_symbol || "").toUpperCase() === "USDT" },
  { id: "usdc", label: "USDC", test: (row) => (row.token_symbol || "").toUpperCase() === "USDC" },
  { id: "in", label: "IN", test: (row) => row.direction === "in" },
  { id: "out", label: "OUT", test: (row) => row.direction === "out" },
];

const state = {
  wallets: [],
  keys: [],
  rows: [],
  view: [],
  meta: null,
  loaded: false,
  mode: "time",
  sortCol: "block",
  sortDir: "desc",
  filter: "",
  chips: new Set(),
  hideZero: false,
  ageMode: "relative",
  page: 1,
  pageSize: 50,
  running: false,
};
const PAGE_SIZES = [25, 50, 100, 250];

const $ = (id) => document.getElementById(id);
const addressInput = $("address");
const labelInput = $("label");
const walletList = $("walletList");
const walletEmpty = $("walletEmpty");
const walletSearch = $("walletSearch");
const sideStatus = $("sideStatus");
const tableWrap = $("tableWrap");
const thead = $("thead");
const spacer = $("spacer");
const windowEl = $("window");
const emptyResults = $("emptyResults");
const filterInput = $("filter");
const summary = $("summary");
const visibleCount = $("visibleCount");
const warnings = $("warnings");
const ethNote = $("ethNote");
const formError = $("formError");
const progress = $("progress");
const progressText = $("progressText");
const settingsDialog = $("settingsDialog");

let controller = null;
let sliceStart = -1;
let sliceEnd = -1;
let filterTimer = 0;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function shortHash(hash) {
  return hash && hash.length > 14 ? `${hash.slice(0, 12)}…` : (hash || "");
}

function shortAddr(addr) {
  return addr && addr.length > 12 ? `${addr.slice(0, 8)}…${addr.slice(-4)}` : (addr || "");
}

function pad(n) { return String(n).padStart(2, "0"); }

function absoluteAge(ts) {
  const date = new Date(ts * 1000);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function relativeAge(ts) {
  const delta = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (delta < 60) return delta === 1 ? "1 sec ago" : `${delta} secs ago`;
  const minutes = Math.floor(delta / 60);
  if (minutes < 60) return minutes === 1 ? "1 min ago" : `${minutes} mins ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours === 1 ? "1 hr ago" : `${hours} hrs ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return days === 1 ? "1 day ago" : `${days} days ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return months === 1 ? "1 mo ago" : `${months} mos ago`;
  const years = Math.floor(days / 365);
  return years === 1 ? "1 yr ago" : `${years} yrs ago`;
}

function ageText(ts) {
  if (!ts) return "";
  return state.ageMode === "absolute" ? absoluteAge(ts) : relativeAge(ts);
}

function toLocalInput(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function readTime(id) {
  const value = $(id).value;
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}

function showError(message) {
  formError.hidden = !message;
  formError.textContent = message || "";
}

async function getJSON(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.error || data.detail || "Request failed.";
    throw new Error(typeof detail === "string" ? detail : "Request failed.");
  }
  return data;
}

function renderWallets() {
  const query = walletSearch.value.trim().toLowerCase();
  const selected = addressInput.value.trim().toLowerCase();
  const items = state.wallets.filter((wallet) => {
    if (!query) return true;
    return wallet.address.toLowerCase().includes(query) || (wallet.label || "").toLowerCase().includes(query);
  });
  walletList.replaceChildren();
  walletEmpty.hidden = items.length !== 0;
  for (const wallet of items) {
    const item = document.createElement("li");
    item.className = "wallet" + (wallet.address.toLowerCase() === selected ? " selected" : "");
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "wallet-pick";
    const name = document.createElement("span");
    name.className = "wallet-name";
    name.textContent = wallet.label || shortAddr(wallet.address);
    const sub = document.createElement("span");
    sub.className = "wallet-sub";
    sub.textContent = wallet.label ? wallet.address : "No label";
    pick.append(name, sub);
    pick.addEventListener("click", () => selectWallet(wallet));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => removeWallet(wallet));
    item.append(pick, remove);
    walletList.append(item);
  }
  const list = $("addressList");
  list.replaceChildren();
  for (const wallet of state.wallets) {
    const option = document.createElement("option");
    option.value = wallet.address;
    option.label = wallet.label || wallet.address;
    list.append(option);
  }
}

async function loadWallets() {
  state.wallets = await getJSON("/api/wallets");
  renderWallets();
}

function selectWallet(wallet) {
  addressInput.value = wallet.address;
  labelInput.value = wallet.label || "";
  renderWallets();
  saveForm();
}

async function removeWallet(wallet) {
  if (!confirm(`Remove ${wallet.label || wallet.address}?`)) return;
  await getJSON(`/api/wallets/${encodeURIComponent(wallet.address)}`, { method: "DELETE" });
  if (addressInput.value.trim().toLowerCase() === wallet.address.toLowerCase()) labelInput.value = "";
  sideStatus.textContent = "Removed wallet";
  await loadWallets();
}

async function saveWallet() {
  showError("");
  const data = await getJSON("/api/wallets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address: addressInput.value.trim(), label: labelInput.value }),
  });
  addressInput.value = data.address;
  labelInput.value = data.label;
  sideStatus.textContent = data.label ? `Saved ${data.label}` : "Saved wallet without a label";
  await loadWallets();
  saveForm();
}

function renderKeys() {
  const list = $("keyList");
  list.replaceChildren();
  $("keyEmpty").hidden = state.keys.length !== 0;
  for (const item of state.keys) {
    const row = document.createElement("li");
    row.className = "wallet" + (item.active ? " selected" : "");
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "wallet-pick";
    const name = document.createElement("span");
    name.className = "wallet-name";
    name.textContent = item.label || item.hint;
    const sub = document.createElement("span");
    sub.className = "wallet-sub";
    if (item.active) sub.textContent = item.label ? `${item.hint} · Active` : "Active";
    else sub.textContent = item.label ? item.hint : "No label";
    pick.append(name, sub);
    pick.addEventListener("click", () => activateKey(item).catch((error) => {
      $("keyStatus").textContent = error.message;
    }));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => removeKey(item).catch((error) => {
      $("keyStatus").textContent = error.message;
    }));
    row.append(pick, remove);
    list.append(row);
  }
  $("keyBanner").hidden = state.keys.some((item) => item.active);
}

async function loadKeys() {
  state.keys = await getJSON("/api/keys");
  renderKeys();
}

async function activateKey(item) {
  if (item.active) return;
  await getJSON(`/api/keys/${item.id}/activate`, { method: "POST" });
  $("keyStatus").textContent = item.label ? `Using ${item.label}` : `Using ${item.hint}`;
  await loadKeys();
}

async function removeKey(item) {
  const name = item.label || item.hint;
  if (!confirm(`Remove ${name}?`)) return;
  await getJSON(`/api/keys/${item.id}`, { method: "DELETE" });
  $("keyStatus").textContent = `Removed ${name}`;
  await loadKeys();
}

async function saveKey() {
  showError("");
  const data = await getJSON("/api/keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: $("apiKeyInput").value.trim(), label: $("apiLabel").value }),
  });
  $("apiKeyInput").value = "";
  $("apiLabel").value = "";
  $("showNewKey").checked = false;
  $("apiKeyInput").type = "password";
  $("keyStatus").textContent = data.label ? `Saved ${data.label}` : "Saved API key without a label";
  await loadKeys();
}

function focusKeyForm() {
  const input = $("apiKeyInput");
  input.scrollIntoView({ block: "center" });
  input.focus();
}

function setMode(mode, fillBlocksOnSelect = true) {
  state.mode = mode;
  $("modeTime").setAttribute("aria-selected", String(mode === "time"));
  $("modeBlock").setAttribute("aria-selected", String(mode === "block"));
  $("timeFields").hidden = mode !== "time";
  $("blockFields").hidden = mode !== "block";
  if (mode === "block" && fillBlocksOnSelect) fillBlocks();
  saveForm();
}

async function fillBlocks() {
  if ($("startBlock").value && $("endBlock").value) return;
  try {
    const data = await getJSON("/api/latest-block");
    if (!$("endBlock").value) $("endBlock").value = String(data.block);
    if (!$("startBlock").value) $("startBlock").value = String(Math.max(0, data.block - 7200));
  } catch (error) {
    showError(error.message);
  }
}

async function loadSettings() {
  const data = await getJSON("/api/settings");
  $("rate").value = data.rate;
  $("pageSize").value = data.page_size;
  return data;
}

function openSettings() {
  $("settingsError").hidden = true;
  settingsDialog.showModal();
}

async function saveSettings(event) {
  event.preventDefault();
  $("settingsError").hidden = true;
  try {
    await getJSON("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rate: Number($("rate").value),
        page_size: Number($("pageSize").value),
      }),
    });
    settingsDialog.close();
  } catch (error) {
    $("settingsError").hidden = false;
    $("settingsError").textContent = error.message;
  }
}

function buildSearch(row) {
  const symbol = row.token_symbol || "";
  const bits = [
    row.hash, row.method, row.method_raw, row.protocol, row.block,
    row.from, row.from_label, row.to, row.to_label,
    row.token, row.token_name, row.token_symbol, row.token_contract,
    row.amount_token, row.amount_token_exact, row.amount_eth, row.amount_eth_exact,
    row.direction, String(row.direction || "").toUpperCase(), row.count,
  ];
  if (symbol) bits.push(`${symbol} (${symbol})`);
  row.search = bits.join(" ").toLowerCase();
}

function partyName(row, side) {
  const label = side === "from" ? row.from_label : row.to_label;
  const addr = side === "from" ? row.from : row.to;
  return (label || addr || "").toLowerCase();
}

function cmpBig(a, b) {
  const left = BigInt(a);
  const right = BigInt(b);
  return left < right ? -1 : left > right ? 1 : 0;
}

function compare(a, b) {
  let primary = 0;
  switch (state.sortCol) {
    case "hash": primary = a.hash.localeCompare(b.hash); break;
    case "method": primary = `${a.method} ${a.protocol}`.localeCompare(`${b.method} ${b.protocol}`); break;
    case "block": primary = a.block - b.block; break;
    case "age": primary = a.timestamp - b.timestamp; break;
    case "from": primary = partyName(a, "from").localeCompare(partyName(b, "from")); break;
    case "to": primary = partyName(a, "to").localeCompare(partyName(b, "to")); break;
    case "amount_token": primary = cmpBig(a.amount_token_sort, b.amount_token_sort); break;
    case "amount_eth": primary = cmpBig(a.amount_eth_sort, b.amount_eth_sort); break;
    case "token": primary = a.token.localeCompare(b.token); break;
    case "count": primary = a.count - b.count; break;
    default: primary = 0;
  }
  if (state.sortDir === "desc") primary = -primary;
  if (primary !== 0) return primary;
  return b.block - a.block || a.seq - b.seq;
}

function pageWindow() {
  const total = state.view.length;
  const size = state.pageSize;
  const pages = Math.max(1, Math.ceil(total / size) || 1);
  if (state.page > pages) state.page = pages;
  if (state.page < 1) state.page = 1;
  const start = total === 0 ? 0 : (state.page - 1) * size;
  const end = Math.min(total, start + size);
  return { start, end, total, pages, size };
}

function pageList(pages, current) {
  const wanted = new Set([1, pages, current - 2, current - 1, current, current + 1, current + 2]);
  const numbers = [...wanted].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
  const items = [];
  let previous = 0;
  for (const number of numbers) {
    if (number - previous > 1) items.push("…");
    items.push(number);
    previous = number;
  }
  return items;
}

function renderPager() {
  const pager = $("pager");
  const { start, end, total, pages } = pageWindow();
  pager.hidden = !state.loaded;
  $("pageRange").textContent = total
    ? `${(start + 1).toLocaleString()}–${end.toLocaleString()} of ${total.toLocaleString()}`
    : "0 rows";
  $("pageCount").textContent = total ? `of ${pages.toLocaleString()}` : "";
  $("pageFirst").disabled = state.page <= 1 || total === 0;
  $("pagePrev").disabled = state.page <= 1 || total === 0;
  $("pageNext").disabled = state.page >= pages || total === 0;
  $("pageLast").disabled = state.page >= pages || total === 0;
  const box = $("pageNumbers");
  box.replaceChildren();
  if (!total) return;
  for (const item of pageList(pages, state.page)) {
    if (item === "…") {
      const gap = document.createElement("span");
      gap.className = "page-ellipsis";
      gap.textContent = "…";
      box.append(gap);
      continue;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.toLocaleString();
    button.setAttribute("aria-label", `Page ${item}`);
    if (item === state.page) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => setPage(item));
    box.append(button);
  }
}

function setPage(page) {
  const { pages } = pageWindow();
  const next = Math.min(pages, Math.max(1, page));
  if (next === state.page) return;
  state.page = next;
  tableWrap.scrollTop = 0;
  sliceStart = -1;
  paint(true);
  renderPager();
}

function setPageSize(size) {
  if (!PAGE_SIZES.includes(size)) return;
  const { start } = pageWindow();
  state.pageSize = size;
  state.page = Math.floor(start / size) + 1;
  $("pageSizeSelect").value = String(size);
  tableWrap.scrollTop = 0;
  sliceStart = -1;
  paint(true);
  renderPager();
  saveForm();
}

function applyView() {
  const query = state.filter.trim().toLowerCase();
  const chips = CHIPS.filter((chip) => state.chips.has(chip.id));
  const view = [];
  for (let index = 0; index < state.rows.length; index += 1) {
    const row = state.rows[index];
    if (state.hideZero && row.zero) continue;
    if (chips.length && !chips.some((chip) => chip.test(row))) continue;
    if (query && !row.search.includes(query)) continue;
    view.push(index);
  }
  view.sort((a, b) => compare(state.rows[a], state.rows[b]));
  state.view = view;
  state.page = 1;
  renderSummary();
  renderHead();
  renderPager();
  tableWrap.scrollTop = 0;
  sliceStart = -1;
  paint(true);
}

function renderHead() {
  thead.innerHTML = COLUMNS.map((column) => {
    const active = state.sortCol === column.id;
    const arrow = active ? (state.sortDir === "asc" ? "↑" : "↓") : "";
    const align = column.align || "";
    return `<button type="button" class="th ${align} ${active ? "active" : ""}" data-sort="${column.id}" title="${esc(TIPS[column.id] || "Sort")}" aria-sort="${active ? (state.sortDir === "asc" ? "ascending" : "descending") : "none"}"><span>${esc(column.label)}</span><span class="arrow">${arrow}</span></button>`;
  }).join("");
}

function badge(side, direction) {
  if (direction === "self") return '<span class="badge self">SELF</span>';
  if (side === "from" && direction === "out") return '<span class="badge out">OUT</span>';
  if (side === "to" && direction === "in") return '<span class="badge in">IN</span>';
  return "";
}

function addressLink(address, label) {
  const text = label || shortAddr(address);
  const safe = /^0x[0-9a-fA-F]{40}$/.test(address || "");
  const title = esc(label ? `${label} (${address})` : address);
  if (!safe) return `<span class="who" title="${title}">${esc(text)}</span>`;
  return `<a class="who" title="${title}" href="https://etherscan.io/address/${address}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>`;
}

function renderRow(row) {
  const hashSafe = /^0x[0-9a-fA-F]{64}$/.test(row.hash || "");
  const hashHtml = hashSafe
    ? `<a class="hash" title="${esc(row.hash)}" href="https://etherscan.io/tx/${row.hash}" target="_blank" rel="noopener noreferrer">${esc(shortHash(row.hash))}</a>`
    : `<span class="hash">${esc(shortHash(row.hash))}</span>`;
  const ethClass = BigInt(row.amount_eth_sort) < 0n ? "neg" : BigInt(row.amount_eth_sort) > 0n ? "pos" : "flat";
  const protocol = row.protocol
    ? `<button type="button" class="proto-btn" data-filter="${esc(row.protocol)}">${esc(row.protocol)}</button>`
    : "";
  const unsafe = row.unsafe ? '<span class="unsafe" title="Etherscan marks this token unsafe">UNSAFE</span>' : "";
  return `<div class="row grid" role="row">
    <div class="cell">${hashHtml}</div>
    <div class="cell method" title="${esc(row.method_raw || row.method)}"><span class="method-line">${esc(row.method)}</span>${protocol}</div>
    <div class="cell num">${row.block.toLocaleString()}</div>
    <div class="cell" title="${esc(absoluteAge(row.timestamp))}">${esc(ageText(row.timestamp))}</div>
    <div class="cell"><div class="party">${addressLink(row.from, row.from_label)}${badge("from", row.direction)}</div></div>
    <div class="cell"><div class="party">${addressLink(row.to, row.to_label)}${badge("to", row.direction)}</div></div>
    <div class="cell num" title="${esc(row.amount_token_exact)}">${esc(row.amount_token)}</div>
    <div class="cell num ${ethClass}" title="${esc(row.amount_eth_exact)} ETH">${esc(row.amount_eth)}</div>
    <div class="cell"><button type="button" class="token-btn" data-filter="${esc(row.token)}" title="${esc(row.token_contract)}">${esc(row.token)}</button>${unsafe}</div>
    <div class="cell num" title="${esc(TIPS.count)}">${row.count.toLocaleString()}</div>
  </div>`;
}

function paint(force) {
  const { start: pageStart, end: pageEnd } = pageWindow();
  const count = pageEnd - pageStart;
  spacer.style.height = `${count * ROW_H}px`;
  const headerH = thead.offsetHeight || 42;
  const startY = tableWrap.scrollTop;
  const endY = startY + tableWrap.clientHeight - headerH;
  const start = Math.max(0, Math.floor(startY / ROW_H) - 8);
  const end = Math.min(count, Math.ceil(Math.max(endY, ROW_H) / ROW_H) + 8);
  if (count === 0) {
    windowEl.innerHTML = "";
    emptyResults.hidden = false;
    emptyResults.textContent = state.loaded && state.rows.length
      ? "No rows match this filter."
      : state.loaded
        ? "No ERC-20 transfers in that range."
        : "Track a wallet to see its ERC-20 transfers.";
    return;
  }
  emptyResults.hidden = true;
  if (!force && start === sliceStart && end === sliceEnd) return;
  sliceStart = start;
  sliceEnd = end;
  const html = [];
  for (let index = start; index < end; index += 1) {
    html.push(renderRow(state.rows[state.view[pageStart + index]]));
  }
  windowEl.style.transform = `translateY(${start * ROW_H}px)`;
  windowEl.innerHTML = html.join("");
}

function renderSummary() {
  const meta = state.meta;
  if (!meta) {
    summary.textContent = "";
    visibleCount.textContent = "";
    warnings.hidden = true;
    ethNote.hidden = true;
    return;
  }
  const parts = [
    `${meta.transfers.toLocaleString()} transfers`,
    `${meta.tokens.toLocaleString()} tokens`,
  ];
  if (meta.start_block != null && meta.end_block != null) {
    parts.push(`blocks ${Number(meta.start_block).toLocaleString()}–${Number(meta.end_block).toLocaleString()}`);
  }
  if (meta.mode === "time" && meta.start_time && meta.end_time) {
    parts.push(`${absoluteAge(meta.start_time)} → ${absoluteAge(meta.end_time)}`);
  }
  parts.push(`${meta.api_calls.toLocaleString()} API calls`);
  parts.push(`${(meta.elapsed_ms / 1000).toFixed(1)}s`);
  summary.textContent = parts.join(" · ");
  const shown = state.view.length;
  visibleCount.textContent = shown === meta.transfers
    ? `${shown.toLocaleString()} rows`
    : `${shown.toLocaleString()} of ${meta.transfers.toLocaleString()} rows`;
  warnings.replaceChildren();
  warnings.hidden = !(meta.warnings && meta.warnings.length);
  for (const text of meta.warnings || []) {
    const line = document.createElement("p");
    line.textContent = text;
    warnings.append(line);
  }
  ethNote.hidden = !meta.transfers;
}

function showResults(payload) {
  state.rows = payload.rows || [];
  state.meta = payload.meta || null;
  state.loaded = true;
  state.page = 1;
  for (const row of state.rows) buildSearch(row);
  tableWrap.scrollTop = 0;
  applyView();
  requestAnimationFrame(() => paint(true));
}

function renderChips() {
  const row = $("chipRow");
  row.replaceChildren();
  for (const chip of CHIPS) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = chip.label;
    button.setAttribute("aria-pressed", String(state.chips.has(chip.id)));
    button.addEventListener("click", () => {
      if (state.chips.has(chip.id)) state.chips.delete(chip.id);
      else state.chips.add(chip.id);
      button.setAttribute("aria-pressed", String(state.chips.has(chip.id)));
      applyView();
    });
    row.append(button);
  }
}

function setRunning(running) {
  state.running = running;
  $("trackBtn").disabled = running;
  $("stopBtn").hidden = !running;
  progress.hidden = !running;
  if (running) $("trackBtn").textContent = "Tracking…";
  else $("trackBtn").textContent = "Track transfers";
}

async function trackTransfers(event) {
  event.preventDefault();
  showError("");
  const address = addressInput.value.trim();
  if (!/^0x[0-9a-fA-F]{40}$/.test(address)) {
    showError("Enter a wallet address (0x and 40 hex characters).");
    return;
  }
  const payload = { address, label: labelInput.value, mode: state.mode };
  if (state.mode === "time") {
    payload.start_time = readTime("startTime");
    payload.end_time = readTime("endTime");
    if (payload.start_time == null || payload.end_time == null) {
      showError("Choose a start and end time.");
      return;
    }
    if (payload.start_time > payload.end_time) {
      showError("Start time must be before the end time.");
      return;
    }
    if (payload.end_time - payload.start_time > 180 * 86400) {
      if (!confirm("This time range is longer than 180 days and can take a while. Continue?")) return;
    }
  } else {
    const startText = $("startBlock").value.trim();
    const endText = $("endBlock").value.trim();
    if (!/^\d+$/.test(startText) || !/^\d+$/.test(endText)) {
      showError("Enter a start and end block.");
      return;
    }
    const start = Number(startText);
    const end = Number(endText);
    if (start > end) {
      showError("Start block must be less than or equal to the end block.");
      return;
    }
    if (end - start > 1_500_000) {
      if (!confirm("This block range is very wide and can take a while. Continue?")) return;
    }
    payload.start_block = start;
    payload.end_block = end;
  }
  saveForm();
  if (controller) controller.abort();
  controller = new AbortController();
  setRunning(true);
  progressText.textContent = "Starting…";
  try {
    const response = await fetch("/api/track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "Request failed.");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let index = buffer.indexOf("\n");
      while (index >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (line) handleEvent(JSON.parse(line));
        index = buffer.indexOf("\n");
      }
    }
    if (buffer.trim()) handleEvent(JSON.parse(buffer.trim()));
  } catch (error) {
    if (error.name !== "AbortError") showError(error.message);
  } finally {
    setRunning(false);
  }
}

function handleEvent(message) {
  if (message.type === "progress") {
    progressText.textContent = message.detail ? `${message.message} · ${message.detail}` : message.message;
    return;
  }
  if (message.type === "error") {
    showError(message.message);
    return;
  }
  if (message.type === "done") showResults(message);
}

function saveForm() {
  try {
    localStorage.setItem("buyerTracker.form", JSON.stringify({
      address: addressInput.value,
      label: labelInput.value,
      mode: state.mode,
      startTime: $("startTime").value,
      endTime: $("endTime").value,
      startBlock: $("startBlock").value,
      endBlock: $("endBlock").value,
      ageMode: state.ageMode,
      hideZero: state.hideZero,
      pageSize: state.pageSize,
    }));
  } catch { /* private mode */ }
}

function restoreForm() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("buyerTracker.form") || "null"); } catch { saved = null; }
  const end = new Date();
  const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
  $("endTime").value = saved?.endTime || toLocalInput(end);
  $("startTime").value = saved?.startTime || toLocalInput(start);
  if (saved?.address) addressInput.value = saved.address;
  if (saved?.label) labelInput.value = saved.label;
  if (saved?.startBlock) $("startBlock").value = saved.startBlock;
  if (saved?.endBlock) $("endBlock").value = saved.endBlock;
  state.hideZero = Boolean(saved?.hideZero);
  $("hideZero").checked = state.hideZero;
  const savedSize = Number(saved?.pageSize);
  state.pageSize = PAGE_SIZES.includes(savedSize) ? savedSize : 50;
  $("pageSizeSelect").value = String(state.pageSize);
  setAgeMode(saved?.ageMode === "absolute" ? "absolute" : "relative");
  setMode(saved?.mode === "block" ? "block" : "time", false);
}

function setAgeMode(mode) {
  state.ageMode = mode;
  $("ageRelative").setAttribute("aria-pressed", String(mode === "relative"));
  $("ageAbsolute").setAttribute("aria-pressed", String(mode === "absolute"));
  if (state.rows.length) paint(true);
  saveForm();
}

function bind() {
  $("queryForm").addEventListener("submit", (event) => {
    trackTransfers(event).catch((error) => showError(error.message));
  });
  $("saveBtn").addEventListener("click", () => {
    saveWallet().catch((error) => showError(error.message));
  });
  $("modeTime").addEventListener("click", () => setMode("time"));
  $("modeBlock").addEventListener("click", () => setMode("block"));
  $("stopBtn").addEventListener("click", () => controller && controller.abort());
  $("settingsBtn").addEventListener("click", openSettings);
  $("addKey").addEventListener("click", focusKeyForm);
  $("saveKeyBtn").addEventListener("click", () => {
    saveKey().catch((error) => {
      $("keyStatus").textContent = error.message;
    });
  });
  $("showNewKey").addEventListener("change", () => {
    $("apiKeyInput").type = $("showNewKey").checked ? "text" : "password";
  });
  $("closeSettings").addEventListener("click", () => settingsDialog.close());
  $("settingsForm").addEventListener("submit", saveSettings);
  walletSearch.addEventListener("input", renderWallets);
  addressInput.addEventListener("change", () => {
    const match = state.wallets.find((wallet) => wallet.address.toLowerCase() === addressInput.value.trim().toLowerCase());
    if (match) labelInput.value = match.label || "";
    renderWallets();
  });
  filterInput.addEventListener("input", () => {
    window.clearTimeout(filterTimer);
    filterTimer = window.setTimeout(() => {
      state.filter = filterInput.value;
      applyView();
    }, 40);
  });
  $("pageFirst").addEventListener("click", () => setPage(1));
  $("pagePrev").addEventListener("click", () => setPage(state.page - 1));
  $("pageNext").addEventListener("click", () => setPage(state.page + 1));
  $("pageLast").addEventListener("click", () => setPage(pageWindow().pages));
  $("pageSizeSelect").addEventListener("change", () => setPageSize(Number($("pageSizeSelect").value)));
  $("pageJump").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("pageJumpInput");
    if (!/^\d+$/.test(input.value.trim())) return;
    const page = Number(input.value);
    setPage(page);
    input.value = "";
  });
  $("hideZero").addEventListener("change", () => {
    state.hideZero = $("hideZero").checked;
    applyView();
    saveForm();
  });
  $("ageRelative").addEventListener("click", () => setAgeMode("relative"));
  $("ageAbsolute").addEventListener("click", () => setAgeMode("absolute"));
  thead.addEventListener("click", (event) => {
    const button = event.target.closest("[data-sort]");
    if (!button) return;
    const id = button.dataset.sort;
    if (state.sortCol === id) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    else {
      state.sortCol = id;
      const column = COLUMNS.find((item) => item.id === id);
      state.sortDir = column && column.kind === "number" ? "desc" : "asc";
    }
    applyView();
  });
  windowEl.addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    filterInput.value = button.dataset.filter;
    state.filter = button.dataset.filter;
    applyView();
  });
  tableWrap.addEventListener("scroll", () => paint(false), { passive: true });
  window.addEventListener("resize", () => paint(true));
  window.setInterval(() => {
    if (state.ageMode === "relative" && state.view.length) paint(true);
  }, 30000);
}

async function init() {
  renderChips();
  renderHead();
  bind();
  restoreForm();
  try {
    await Promise.all([loadWallets(), loadKeys(), loadSettings()]);
  } catch (error) {
    showError(error.message);
  }
}

window.TrackerApp = { showResults, state, applyView, absoluteAge, relativeAge };

init();
