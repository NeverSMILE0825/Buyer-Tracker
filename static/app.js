const ROW_H = 52;
const COLUMNS = [
  { id: "wallet", label: "Wallet", kind: "text" },
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
  wallet: "The monitored wallet this transfer belongs to.",
  hash: "Sort by transaction hash.",
  method: "Sort by method and protocol.",
  block: "Sort by block number.",
  age: "Sorts by the block timestamp. Relative and Absolute only change the writing.",
  from: "Sort by the sender label or address.",
  to: "Sort by the recipient label or address.",
  amount_token: "ERC-20 amount moved in this transfer.",
  amount_eth: "Net ETH plus WETH for this wallet in the whole transaction. Rows that share a hash share this value.",
  token: "Sort by token name.",
  count: "Distinct transactions that moved this token for that wallet in the selected range.",
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
  progressFraction: 0,
  progressDone: false,
  progressFailed: false,
  live: false,
  notify: false,
  notifyFilter: false,
  tokens: [],
  seenByWallet: {},
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
const progressPercent = $("progressPercent");
const progressTime = $("progressTime");
const progressDetail = $("progressDetail");
const progressTrack = $("progressTrack");
const progressFill = $("progressFill");
const settingsDialog = $("settingsDialog");

let controller = null;
let progressTimer = 0;
let progressStarted = 0;
let liveTimer = 0;
let trackKeepPage = false;
const LIVE_MS = 15000;
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
    const on = wallet.enabled !== false;
    item.className = "wallet key-row" + (on ? " on" : " off") + (wallet.address.toLowerCase() === selected ? " selected" : "");
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
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "key-toggle";
    toggle.textContent = on ? "On" : "Off";
    toggle.setAttribute("aria-pressed", String(on));
    const title = wallet.label || shortAddr(wallet.address);
    toggle.setAttribute("aria-label", `${on ? "Disable" : "Enable"} ${title}`);
    toggle.addEventListener("click", () => setWalletEnabled(wallet, !on).catch((error) => {
      sideStatus.textContent = error.message;
    }));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => removeWallet(wallet));
    item.append(pick, toggle, remove);
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

async function setWalletEnabled(wallet, enabled) {
  await getJSON(`/api/wallets/${encodeURIComponent(wallet.address)}/enabled`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  const title = wallet.label || shortAddr(wallet.address);
  sideStatus.textContent = enabled ? `${title} on` : `${title} off`;
  await loadWallets();
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
    row.className = "wallet key-row" + (item.enabled ? " on" : " off");
    const info = document.createElement("div");
    info.className = "wallet-pick";
    const name = document.createElement("span");
    name.className = "wallet-name";
    name.textContent = item.label || item.hint;
    const sub = document.createElement("span");
    sub.className = "wallet-sub";
    sub.textContent = item.label ? item.hint : "No label";
    info.append(name, sub);
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "key-toggle";
    toggle.textContent = item.enabled ? "On" : "Off";
    toggle.setAttribute("aria-pressed", String(Boolean(item.enabled)));
    const label = item.label || item.hint;
    toggle.setAttribute("aria-label", `${item.enabled ? "Disable" : "Enable"} ${label}`);
    toggle.addEventListener("click", () => setKeyEnabled(item, !item.enabled).catch((error) => {
      $("keyStatus").textContent = error.message;
    }));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => removeKey(item).catch((error) => {
      $("keyStatus").textContent = error.message;
    }));
    row.append(info, toggle, remove);
    list.append(row);
  }
  const enabled = state.keys.some((item) => item.enabled);
  const banner = $("keyBanner");
  banner.hidden = enabled;
  const note = banner.querySelector("p");
  if (note) {
    note.textContent = state.keys.length
      ? "Turn on an API key before loading transfers. It stays on this computer."
      : "Add an Etherscan API key before loading transfers. It stays on this computer.";
  }
}

async function loadKeys() {
  state.keys = await getJSON("/api/keys");
  renderKeys();
}

async function setKeyEnabled(item, enabled) {
  await getJSON(`/api/keys/${item.id}/enabled`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  const name = item.label || item.hint;
  $("keyStatus").textContent = enabled ? `${name} on` : `${name} off`;
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

async function uploadKeys(file) {
  const text = await file.text();
  const data = await getJSON("/api/keys/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  $("keyStatus").textContent = data.message;
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

async function fillLatestBlock() {
  const button = $("latestBlockBtn");
  button.disabled = true;
  try {
    const data = await getJSON("/api/latest-block");
    $("endBlock").value = String(data.block);
    showError("");
    saveForm();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
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
    row.wallet, row.wallet_label, row.hash, row.method, row.method_raw, row.protocol, row.block,
    row.from, row.from_label, row.to, row.to_label,
    row.token, row.token_name, row.token_symbol, row.token_contract,
    row.amount_token, row.amount_token_exact, row.amount_eth, row.amount_eth_exact,
    row.direction, String(row.direction || "").toUpperCase(), row.count,
  ];
  if (symbol) bits.push(`${symbol} (${symbol})`);
  row.search = bits.join(" ").toLowerCase();
}

function walletName(row) {
  return (row.wallet_label || row.wallet || "").toLowerCase();
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
    case "wallet": primary = walletName(a).localeCompare(walletName(b)); break;
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

function applyView(keepPage = false) {
  const query = state.filter.trim().toLowerCase();
  const keptPage = state.page;
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
  const pages = Math.max(1, Math.ceil(view.length / state.pageSize) || 1);
  state.page = keepPage ? Math.min(Math.max(1, keptPage), pages) : 1;
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
  const walletText = row.wallet_label || shortAddr(row.wallet);
  const walletHtml = row.wallet
    ? `<button type="button" class="token-btn" data-filter="${esc(walletText)}" title="${esc(row.wallet)}">${esc(walletText)}</button>`
    : "";
  return `<div class="row grid" role="row">
    <div class="cell">${walletHtml}</div>
    <div class="cell">${hashHtml}</div>
    <div class="cell method" title="${esc(row.method_raw || row.method)}"><span class="method-line">${esc(row.method)}</span>${protocol}</div>
    <div class="cell num">${row.block.toLocaleString()}</div>
    <div class="cell" title="${esc(absoluteAge(row.timestamp))}">${esc(ageText(row.timestamp))}</div>
    <div class="cell"><div class="party">${addressLink(row.from, row.from_label)}${badge("from", row.direction)}</div></div>
    <div class="cell"><div class="party">${addressLink(row.to, row.to_label)}${badge("to", row.direction)}</div></div>
    <div class="cell num" title="${esc(row.amount_token_exact)}">${esc(row.amount_token)}</div>
    <div class="cell num ${ethClass}" title="${esc(row.amount_eth_exact)} ETH">${esc(row.amount_eth)}</div>
    <div class="cell token-cell"><button type="button" class="token-btn" data-filter="${esc(row.token)}" title="${esc(row.token_contract)}">${esc(row.token)}</button><button type="button" class="watch-btn${watchingToken(row.token_contract) ? " saved" : ""}" data-watch data-contract="${esc(row.token_contract)}" data-symbol="${esc(row.token_symbol)}" data-name="${esc(row.token_name)}">${watchingToken(row.token_contract) ? "Watching" : "Watch"}</button>${unsafe}</div>
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
        : "Turn on a saved wallet, or enter an address, to see ERC-20 transfers.";
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
  const names = (meta.wallets || []).map((wallet) => wallet.label || shortAddr(wallet.address)).filter(Boolean);
  if (names.length > 1) parts.splice(1, 0, names.join(", "));
  if (meta.start_block != null && meta.end_block != null) {
    parts.push(`blocks ${Number(meta.start_block).toLocaleString()}–${Number(meta.end_block).toLocaleString()}`);
  }
  if (meta.mode === "time" && meta.start_time && meta.end_time) {
    parts.push(`${absoluteAge(meta.start_time)} → ${absoluteAge(meta.end_time)}`);
  }
  parts.push(`${meta.api_calls.toLocaleString()} API calls`);
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

function showResults(payload, keepPage = false) {
  state.rows = payload.rows || [];
  state.meta = payload.meta || null;
  state.loaded = true;
  if (!keepPage) {
    state.page = 1;
    tableWrap.scrollTop = 0;
  }
  for (const row of state.rows) buildSearch(row);
  applyView(keepPage);
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

function formatDuration(ms) {
  const total = Math.max(0, Number(ms) || 0) / 1000;
  if (total < 60) return `${total.toFixed(1)}s`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = Math.round(total % 60);
  if (hours) return `${hours}h ${minutes}m ${seconds}s`;
  return `${minutes}m ${seconds}s`;
}

function setProgressBar(percent, complete) {
  const pct = Math.max(0, Math.min(100, percent));
  progressFill.style.width = `${pct}%`;
  progressPercent.textContent = `${pct}%`;
  progressTrack.setAttribute("aria-valuenow", String(pct));
  progressTrack.setAttribute("aria-valuetext", complete ? `Complete, ${progressTime.textContent}` : `${pct}%`);
}

function stopProgressClock() {
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = 0;
  }
}

function startProgressClock() {
  stopProgressClock();
  progressStarted = performance.now();
  progressTime.textContent = "0.0s";
  progressTimer = window.setInterval(() => {
    if (state.progressDone) return;
    progressTime.textContent = formatDuration(performance.now() - progressStarted);
  }, 100);
}

function beginProgress() {
  state.progressFraction = 0;
  state.progressDone = false;
  state.progressFailed = false;
  progress.hidden = false;
  progress.classList.add("running");
  progress.classList.remove("complete", "failed");
  progressText.textContent = "Starting…";
  progressDetail.textContent = "";
  setProgressBar(0, false);
  startProgressClock();
}

function finishProgress(meta) {
  state.progressDone = true;
  state.progressFailed = false;
  stopProgressClock();
  progress.classList.remove("running", "failed");
  progress.classList.add("complete");
  const count = Number(meta.transfers || 0);
  const liveLabel = state.live ? " · live" : "";
  progressText.textContent = count
    ? `${count.toLocaleString()} transfers ready${liveLabel}`
    : `No transfers in this range${liveLabel}`;
  const fromComputer = Number(meta.fetched_blocks) === 0 && Number(meta.cached_blocks) > 0;
  progressTime.textContent = `${fromComputer ? "Loaded" : "Fetched"} in ${formatDuration(meta.elapsed_ms)}`;
  const bits = [];
  if (meta.start_block != null && meta.end_block != null) {
    bits.push(`Blocks ${Number(meta.start_block).toLocaleString()}–${Number(meta.end_block).toLocaleString()}`);
  }
  if (fromComputer) bits.push("Loaded from this computer");
  else if (Number(meta.cached_blocks) > 0 && Number(meta.fetched_blocks) > 0) {
    bits.push(`${Number(meta.cached_blocks).toLocaleString()} saved blocks`);
    bits.push(`${Number(meta.fetched_blocks).toLocaleString()} fetched`);
  }
  if (meta.api_calls != null) bits.push(`${Number(meta.api_calls).toLocaleString()} API calls`);
  if (Number(meta.wallets && meta.wallets.length) > 1) bits.push(`${meta.wallets.length} wallets`);
  if (Number(meta.keys) > 1) bits.push(`${Number(meta.keys)} keys`);
  progressDetail.textContent = bits.join(" · ");
  setProgressBar(100, true);
  progress.hidden = false;
}

function failProgress(text) {
  state.progressFailed = true;
  state.progressDone = false;
  stopProgressClock();
  progress.classList.remove("running", "complete");
  progress.classList.add("failed");
  progressText.textContent = "Fetch failed";
  progressTime.textContent = formatDuration(performance.now() - progressStarted);
  progressDetail.textContent = text || "";
  progress.hidden = false;
}

function renderProgress(message) {
  if (state.progressDone) return;
  const incoming = Number(message.fraction);
  const fraction = Math.max(state.progressFraction, Number.isFinite(incoming) ? incoming : 0);
  state.progressFraction = fraction;
  if (Number.isFinite(Number(message.elapsed_ms))) {
    progressStarted = performance.now() - Number(message.elapsed_ms);
  }
  progressText.textContent = message.message || "Loading…";
  const bits = [];
  if (message.blocks_done != null && message.blocks_total != null) {
    bits.push(`${Number(message.blocks_done).toLocaleString()} of ${Number(message.blocks_total).toLocaleString()} blocks`);
  } else if (message.block != null && message.end_block != null) {
    bits.push(`Block ${Number(message.block).toLocaleString()} of ${Number(message.end_block).toLocaleString()}`);
  } else if (message.start_block != null && message.end_block != null) {
    bits.push(`Blocks ${Number(message.start_block).toLocaleString()}–${Number(message.end_block).toLocaleString()}`);
  }
  if (message.detail) bits.push(message.detail);
  else if (message.rows != null) bits.push(`${Number(message.rows).toLocaleString()} rows`);
  if (Number(message.wallet_count) > 1) {
    bits.push(`Wallet ${Number(message.wallet_index)} of ${Number(message.wallet_count)}`);
  }
  if (Number(message.keys) > 1) bits.push(`${Number(message.keys)} keys`);
  progressDetail.textContent = bits.join(" · ");
  setProgressBar(Math.min(99, Math.floor(fraction * 100)), false);
}

function setRunning(running) {
  state.running = running;
  $("trackBtn").disabled = running;
  $("stopBtn").hidden = !running;
  if (running) {
    $("trackBtn").textContent = "Tracking…";
    beginProgress();
  } else {
    $("trackBtn").textContent = "Track transfers";
    progress.classList.remove("running");
    stopProgressClock();
    if (!state.progressDone && !state.progressFailed && !progress.hidden) {
      progressText.textContent = "Stopped";
      progressTime.textContent = formatDuration(performance.now() - progressStarted);
      progressDetail.textContent = "The fetch was stopped before it finished.";
    }
  }
}

async function moveRangeToTip() {
  if (state.mode === "time") {
    const current = readTime("endTime");
    const now = Math.floor(Date.now() / 1000);
    if (current == null || now > current) $("endTime").value = toLocalInput(new Date());
    return;
  }
  const data = await getJSON("/api/latest-block");
  const current = Number($("endBlock").value);
  if (!Number.isFinite(current) || data.block > current) $("endBlock").value = String(data.block);
}

function stopLive() {
  state.live = false;
  $("liveMode").checked = false;
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = 0;
  }
  saveForm();
}

function armLive() {
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = window.setInterval(() => {
    if (!state.live || state.running) return;
    trackTransfers(null, { fromLive: true }).catch((error) => showError(error.message));
  }, LIVE_MS);
}

function setLive(on) {
  state.live = on;
  $("liveMode").checked = on;
  saveForm();
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = 0;
  }
  if (!on) {
    if (progressText.textContent.endsWith(" · live")) {
      progressText.textContent = progressText.textContent.slice(0, -" · live".length);
    }
    return;
  }
  armLive();
  if (!state.running) {
    trackTransfers(null, { fromLive: true }).catch((error) => showError(error.message));
  }
}

async function setNotify(on) {
  if (!on) {
    state.notify = false;
    $("notifyMode").checked = false;
    saveForm();
    return;
  }
  if (!("Notification" in window)) {
    state.notify = false;
    $("notifyMode").checked = false;
    showError("This browser cannot show system notifications.");
    saveForm();
    return;
  }
  let permission = Notification.permission;
  if (permission === "default") permission = await Notification.requestPermission();
  if (permission !== "granted") {
    state.notify = false;
    $("notifyMode").checked = false;
    showError("Allow notifications for this site to get alerts while you are in another window.");
    saveForm();
    return;
  }
  state.notify = true;
  $("notifyMode").checked = true;
  showError("");
  saveForm();
}

function setNotifyFilter(on) {
  state.notifyFilter = on;
  $("notifyFilter").checked = on;
  saveForm();
}

function watchingToken(address) {
  const key = (address || "").toLowerCase();
  return state.tokens.some((token) => token.address.toLowerCase() === key);
}

function tokenTitle(token) {
  return token.label || token.symbol || token.name || shortAddr(token.address);
}

function renderTokens() {
  const list = $("tokenList");
  list.replaceChildren();
  $("tokenEmpty").hidden = state.tokens.length !== 0;
  for (const token of state.tokens) {
    const item = document.createElement("li");
    item.className = "wallet";
    const info = document.createElement("div");
    info.className = "wallet-pick";
    const name = document.createElement("span");
    name.className = "wallet-name";
    name.textContent = tokenTitle(token);
    const sub = document.createElement("span");
    sub.className = "wallet-sub";
    sub.textContent = token.address;
    info.append(name, sub);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => {
      removeToken(token).catch((error) => {
        $("tokenStatus").textContent = error.message;
      });
    });
    item.append(info, remove);
    list.append(item);
  }
}

async function loadTokens() {
  state.tokens = await getJSON("/api/tokens");
  renderTokens();
  if (state.loaded) paint(true);
}

async function saveToken(body) {
  const data = await getJSON("/api/tokens", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  $("tokenStatus").textContent = `Saved ${tokenTitle(data)}`;
  await loadTokens();
  return data;
}

async function removeToken(token) {
  if (!confirm(`Remove ${tokenTitle(token)} from notify tokens?`)) return;
  await getJSON(`/api/tokens/${encodeURIComponent(token.address)}`, { method: "DELETE" });
  $("tokenStatus").textContent = `Removed ${tokenTitle(token)}`;
  await loadTokens();
}

function rowsForNotify(rows) {
  if (!state.notifyFilter) return rows;
  const saved = new Set(state.tokens.map((token) => token.address.toLowerCase()));
  return rows.filter((row) => saved.has((row.token_contract || "").toLowerCase()));
}

function announceTransfers(rows) {
  const groups = new Map();
  for (const row of rows) {
    const key = (row.wallet || state.meta?.address || "").toLowerCase();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  const fresh = [];
  for (const [key, group] of groups) {
    const seen = state.seenByWallet[key] || { ready: false, hashes: new Set(), maxBlock: 0 };
    const hashes = new Set(seen.hashes);
    let maxBlock = seen.maxBlock;
    for (const row of group) {
      if (row.hash) hashes.add(row.hash);
      const block = Number(row.block) || 0;
      if (block > maxBlock) maxBlock = block;
    }
    const ready = seen.ready;
    const previous = seen.hashes;
    const previousMax = seen.maxBlock;
    state.seenByWallet[key] = { ready: true, hashes, maxBlock };
    if (!ready) continue;
    fresh.push(...group.filter((row) => row.hash && !previous.has(row.hash) && (Number(row.block) || 0) >= previousMax));
  }
  if (!state.notify || Notification.permission !== "granted") return;
  const notable = rowsForNotify(fresh);
  if (!notable.length) return;
  const who = [...new Set(notable.map((row) => (row.wallet_label || "").trim() || shortAddr(row.wallet)).filter(Boolean))].slice(0, 3);
  const symbols = [...new Set(notable.map((row) => row.token_symbol).filter(Boolean))].slice(0, 3);
  const freshCount = notable.length;
  const noun = freshCount === 1 ? "transfer" : "transfers";
  const detail = symbols.length ? ` · ${symbols.join(", ")}` : "";
  const whoText = who.join(", ") || "a watched wallet";
  let note;
  try {
    note = new Notification("Buyer Tracker", {
      body: `${freshCount} new ${noun} for ${whoText}${detail}`,
      tag: "buyer-tracker",
      renotify: true,
    });
  } catch {
    note = new Notification("Buyer Tracker", {
      body: `${freshCount} new ${noun} for ${whoText}${detail}`,
      tag: "buyer-tracker",
    });
  }
  note.onclick = () => {
    window.focus();
    note.close();
  };
}

async function trackTransfers(event, options = {}) {
  if (event) event.preventDefault();
  const fromLive = Boolean(options.fromLive);
  showError("");
  if (fromLive) {
    try {
      await moveRangeToTip();
    } catch (error) {
      showError(error.message);
      return;
    }
  }
  const enabled = state.wallets.filter((wallet) => wallet.enabled !== false);
  let targets = enabled;
  if (!targets.length) {
    const address = addressInput.value.trim();
    if (!/^0x[0-9a-fA-F]{40}$/.test(address)) {
      showError("Turn on a saved wallet, or enter a wallet address.");
      return;
    }
    targets = [{ address, label: labelInput.value }];
  }
  const payload = {
    address: targets[0].address,
    label: targets[0].label || "",
    addresses: targets.map((wallet) => wallet.address),
    mode: state.mode,
  };
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
    if (!fromLive && payload.end_time - payload.start_time > 180 * 86400) {
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
    if (!fromLive && end - start > 1_500_000) {
      if (!confirm("This block range is very wide and can take a while. Continue?")) return;
    }
    payload.start_block = start;
    payload.end_block = end;
  }
  saveForm();
  trackKeepPage = fromLive && state.loaded;
  if (controller) controller.abort();
  controller = new AbortController();
  setRunning(true);
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
    if (error.name === "AbortError") return;
    failProgress(error.message);
    showError(error.message);
  } finally {
    setRunning(false);
  }
}

function handleEvent(message) {
  if (message.type === "progress") {
    renderProgress(message);
    return;
  }
  if (message.type === "error") {
    failProgress(message.message);
    showError(message.message);
    return;
  }
  if (message.type === "done") {
    finishProgress(message.meta || {});
    showResults(message, trackKeepPage);
    announceTransfers(message.rows || []);
  }
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
      live: state.live,
      notify: state.notify,
      notifyFilter: state.notifyFilter,
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
  state.live = Boolean(saved?.live);
  state.notify = Boolean(saved?.notify) && "Notification" in window && Notification.permission === "granted";
  state.notifyFilter = Boolean(saved?.notifyFilter);
  $("hideZero").checked = state.hideZero;
  $("liveMode").checked = state.live;
  $("notifyMode").checked = state.notify;
  $("notifyFilter").checked = state.notifyFilter;
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
  $("latestBlockBtn").addEventListener("click", () => {
    fillLatestBlock().catch((error) => showError(error.message));
  });
  $("stopBtn").addEventListener("click", () => {
    stopLive();
    if (controller) controller.abort();
  });
  $("liveMode").addEventListener("change", () => setLive($("liveMode").checked));
  $("notifyMode").addEventListener("change", () => {
    setNotify($("notifyMode").checked).catch((error) => showError(error.message));
  });
  $("notifyFilter").addEventListener("change", () => setNotifyFilter($("notifyFilter").checked));
  $("saveTokenBtn").addEventListener("click", () => {
    saveToken({
      address: $("tokenAddress").value.trim(),
      symbol: $("tokenSymbol").value.trim(),
      label: $("tokenSymbol").value.trim(),
    }).then((data) => {
      $("tokenAddress").value = data.address;
    }).catch((error) => {
      $("tokenStatus").textContent = error.message;
    });
  });
  $("settingsBtn").addEventListener("click", openSettings);
  $("addKey").addEventListener("click", focusKeyForm);
  $("saveKeyBtn").addEventListener("click", () => {
    saveKey().catch((error) => {
      $("keyStatus").textContent = error.message;
    });
  });
  $("uploadKeysBtn").addEventListener("click", () => $("keyFile").click());
  $("keyFile").addEventListener("change", () => {
    const file = $("keyFile").files && $("keyFile").files[0];
    $("keyFile").value = "";
    if (!file) return;
    uploadKeys(file).catch((error) => {
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
    const watch = event.target.closest("[data-watch]");
    if (watch) {
      if (watchingToken(watch.dataset.contract)) {
        $("tokenStatus").textContent = "That token is already in the notify list.";
        return;
      }
      saveToken({
        address: watch.dataset.contract,
        symbol: watch.dataset.symbol || "",
        name: watch.dataset.name || "",
      }).catch((error) => {
        $("tokenStatus").textContent = error.message;
      });
      return;
    }
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
    await Promise.all([loadWallets(), loadKeys(), loadTokens(), loadSettings()]);
  } catch (error) {
    showError(error.message);
  }
  if (state.live) {
    armLive();
    trackTransfers(null, { fromLive: true }).catch((error) => showError(error.message));
  }
}

window.TrackerApp = { showResults, state, applyView, absoluteAge, relativeAge };

init();
