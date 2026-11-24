"use strict";

const byId = (id) => document.getElementById(id);
const platformNames = {xianyu: "闲鱼", xiaohongshu: "小红书"};
const states = {active: "在售", sold: "已售", delisted: "下架"};
const jobNames = {queued: "待采样", success: "成功", missed: "缺采", failed: "失败", canceled: "已取消", unavailable: "无数据源"};
const deliveryNames = {queued: "待发送", sent: "已送达", unknown: "待核查"};
const sourceNames = {manual: "手工录入", refresh: "立即采样", scheduled: "定时采样", catalog: "店铺商品快照"};
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  hourCycle: "h23",
});
const price = (fen) => fen == null ? "—" : "¥" + (fen / 100).toFixed(fen % 100 === 0 ? 0 : 2);
const wantLabel = (count) => count == null ? null : count + " 人想要";
const time = (value) => value ? dateFormat.format(new Date(value)) : "—";
const observationFormat = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const observedTime = (value) => value ? observationFormat.format(new Date(value)) : "—";
const delta = (value) => value == null ? "—" : (value > 0 ? "+" : "") + value;
const deltaOrZero = (value, readable) => value == null ? (readable ? "0" : "—") : delta(value);
// 增量按哪个字段计算，与后端 metric_column 一致：小红书看精确展示销量，闲鱼商品看想要数，闲鱼店铺看在售数。
// 与后端 metric_column 保持一致：店铺看采集到的在售数，小红书商品看精确销量，闲鱼商品看想要数。
const deltaMetric = (row, target) => target.kind === "shop"
  ? row.shop_active_count ?? null
  : target.platform === "xiaohongshu" ? row.exact_sales ?? null : row.want_count ?? null;
const isXianyuProduct = (row) => row.platform === "xianyu" && row.kind === "product";
const targetHasSource = (row) => {
  if (!row) return false;
  const kinds = sourceKinds[row.platform];
  if (Array.isArray(kinds)) return kinds.includes(row.kind);
  return Boolean(sources[row.platform] || row.platform === publicShop);
};
// 店铺统一按在售数展示与计算增量，商品按销量或想要数。
const metric = (row, target) => isXianyuProduct(target)
  ? wantLabel(row.want_count)
  : target.kind === "shop"
    ? row.shop_active_count == null ? (row.raw_sales ?? row.exact_sales) : "在售 " + row.shop_active_count
    : row.raw_sales ?? row.exact_sales;
const metricName = (platform, kind) => platform === "xianyu" ? (
  kind === "shop" ? "在售数" : kind === "product" ? "想要数" : "想要数 / 在售数"
)
  : platform === "xiaohongshu" ? (
    kind === "shop" ? "在售数" : kind === "product" ? "展示销量" : "展示销量 / 在售数"
  )
    : "展示销量 / 想要数 / 在售数";

let platform = "";
let offset = 0;
let searchTimer;
let poolRequest = 0;
let current = null;
let currentReadings = [];
let dailyRows = [];
let dailyPage = 0;
const dailyPageSize = 8;
const dayFormat = new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"});
let detailRequest = 0;
let sources = {};
let sourceKinds = {};
let publicShop = null;
let activeView = "pool";
let shopId = null;
let shopRequest = 0;
let shopPage = 1;
let shopPageCount = 1;
const collectingShops = new Set();
const pageSize = 50;

function node(tag, value, className) {
  const element = document.createElement(tag);
  if (value != null) element.textContent = value;
  if (className) element.className = className;
  return element;
}

async function request(path, method = "GET", body) {
  const options = {method, headers: {"Accept": "application/json"}};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/api/v1" + path, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = data.detail;
    const message = Array.isArray(detail) ? detail.map((item) => item.msg).join("；") : detail;
    throw new Error(message || "请求失败（" + response.status + "）");
  }
  return data;
}

function message(error) {
  const notice = byId("app-message");
  notice.hidden = !error;
  notice.textContent = error?.message || "";
}

function perform(task) {
  return Promise.resolve().then(task).catch(message);
}

function line(title, description) {
  const entry = node("div", null, "record");
  entry.append(node("strong", title));
  if (description) entry.append(node("p", description));
  return entry;
}

function drawRecords(container, records, empty) {
  container.replaceChildren(...(records.length ? records : [line(empty)]));
}

async function loadHealth() {
  const health = await request("/health");
  sources = health.sources;
  sourceKinds = health.source_kinds || {};
  publicShop = health.public_shop;
  byId("health").textContent = "后台运行中";
  byId("health-dot").classList.add("ready");
  byId("sources").replaceChildren(...Object.entries(sources).map(([key, configured]) => {
    const item = node("div", null, "source");
    const kinds = sourceKinds[key] || [];
    const note = kinds.length >= 2 ? " · 自动数据源已配置"
      : kinds.length === 1 ? (kinds[0] === "product" ? " · 商品支持公开采样" : " · 店铺支持公开采样")
        : configured ? " · 自动数据源已配置" : " · 待配置自动数据源";
    item.append(node("span", null, configured ? "dot ready" : "dot"),
      node("span", platformNames[key] + note));
    return item;
  }));
}

async function loadPool() {
  const version = ++poolRequest;
  const params = new URLSearchParams({
    platform, q: byId("search").value.trim(), kind: byId("kind").value,
    sort: byId("sort").value, offset, limit: pageSize,
  });
  const {items, total} = await request("/targets?" + params);
  if (version !== poolRequest) return;
  if (offset > 0 && offset >= total) {
    offset = Math.max(0, Math.floor((total - 1) / pageSize) * pageSize);
    return loadPool();
  }
  byId("pool-metric").textContent = metricName(platform, byId("kind").value);
  document.querySelectorAll("[data-sort]").forEach((button) => {
    const order = params.get("sort");
    const key = button.dataset.sort;
    const direction = order === key ? "descending" : order === key + "_asc" ? "ascending" : null;
    if (direction) button.parentElement.setAttribute("aria-sort", direction);
    else button.parentElement.removeAttribute("aria-sort");
    button.querySelector(".sort-indicator").textContent = direction === "descending" ? "↓" : direction === "ascending" ? "↑" : "↕";
  });
  byId("targets").replaceChildren(...items.map(targetRow));
  byId("empty").hidden = items.length > 0;
  byId("count").textContent = total ? (offset + 1) + "–" + (offset + items.length) + " / 共 " + total + " 条" : "共 0 条";
  byId("prev").disabled = offset === 0;
  byId("next").disabled = offset + pageSize >= total;
}

function targetRow(item) {
  const row = node("tr");
  const target = node("td");
  const button = node(item.kind === "shop" ? "a" : "button", item.title, "target-button");
  button.title = item.title;
  if (item.kind === "shop") button.href = "#shop/" + item.id;
  else button.addEventListener("click", () => perform(() => openDetail(item.id)));
  const meta = node("span", null, "meta");
  meta.append(node("span", platformNames[item.platform], "badge " + item.platform),
    document.createTextNode(item.kind === "product" ? " 商品" : " 店铺"));
  target.append(button, meta);
  const statusCell = node("td");
  const monitoring = item.enabled && item.configured;
  const statusLabel = !item.enabled ? "已暂停" : monitoring ? "监控中" : "待配置数据源";
  statusCell.append(node("span", statusLabel, "badge " + (monitoring ? "monitoring" : "")));
  const samplingIssue = item.job_state === "failed" || item.job_state === "unavailable";
  if (monitoring && samplingIssue && !item.sampling_recovered) {
    const canReview = item.kind === "product" && item.listing_state !== "delisted";
    statusCell.title = item.job_error || "";
    if (canReview && item.reviewed_sampling_slot && item.reviewed_sampling_slot === item.job_slot) {
      statusCell.append(node("span", "已打开核对 · 上次采样失败", "meta"));
    } else {
      statusCell.append(node("span", canReview ? "采样异常 · 商品状态待确认" : "采样异常", "meta sampling-issue"));
      if (canReview) {
        const review = node("a", "打开核对", "meta");
        review.href = item.url;
        review.target = "_blank";
        review.rel = "noopener noreferrer";
        review.addEventListener("click", () => markSamplingReviewed(item.id, item.job_slot));
        statusCell.append(review);
      }
    }
  } else if (monitoring && item.job_state === "queued") {
    statusCell.append(node("span", "等待采样", "meta"));
  } else if (monitoring && item.job_state === "missed") {
    statusCell.append(node("span", "上次采样缺失", "meta"));
  }
  if (item.listing_state && states[item.listing_state]) {
    statusCell.append(node("span", "最近商品状态 · " + states[item.listing_state], "meta"));
  }
  if (item.observed_at && item.catalog_observed_at) {
    statusCell.append(node("span", "含店铺采集值 · " + time(item.catalog_observed_at), "meta"));
  }
  const displayedPrice = node("td", price(item.price_fen));
  if (item.price_label) displayedPrice.append(node("span", item.price_label, "meta"));
  const readable = item.enabled && item.configured && deltaMetric(item, item) != null;
  row.append(target, displayedPrice, node("td", metric(item, item) ?? "—"),
    node("td", deltaOrZero(item.hourly_delta, readable)), node("td", deltaOrZero(item.daily_delta, readable)),
    statusCell, node("td", observedTime(item.first_observed_at)),
    node("td", observedTime(item.last_observed_at)));
  const action = node("td");
  const remove = node("button", "删除", "danger");
  remove.type = "button";
  remove.setAttribute("aria-label", "删除 " + item.title);
  remove.addEventListener("click", () => perform(() => removeTarget(item, remove)));
  action.append(remove);
  row.append(action);
  return row;
}

function markSamplingReviewed(id, slot) {
  if (!slot) return;
  perform(async () => {
    await request("/targets/" + id + "/review-sampling", "POST", {slot});
    await loadPool();
  });
}

async function openDetail(id) {
  const version = ++detailRequest;
  const {target, items, observation, hours, days, preview, three_hour_delta: threeHour, categories} = await request("/targets/" + id + "/series");
  if (version !== detailRequest) return;
  const changed = current?.id !== id || !byId("detail-dialog").open;
  current = target;
  currentReadings = items;
  dailyRows = days;
  if (changed) dailyPage = 0;
  renderTargetSummary(target, items, hours, days, preview, threeHour);
  renderDetailCategory(target, categories);
  renderDailyTable();
  if (changed) showDetailView(target.kind === "shop" ? "hourly" : "daily");
  else if (!byId("detail-daily").hidden) drawDailyChart();
  byId("first-observed-at").textContent = observedTime(observation.first_observed_at);
  byId("last-observed-at").textContent = observedTime(observation.last_observed_at);
  renderHourly(target, hours);
  const wants = isXianyuProduct(target);
  byId("detail-history").hidden = false;
  byId("reading-details").hidden = false;
  byId("download").hidden = false;
  byId("detail-metric").textContent = target.kind === "shop" ? "在售数"
    : wants ? "想要数" : target.platform === "xiaohongshu" ? "展示销量" : "商品读数";
  byId("want-field").hidden = !wants;
  byId("visible-reading-form").hidden = !wants;
  byId("sales-field").hidden = target.platform !== "xiaohongshu";
  byId("chart-metric-label").hidden = !wants;
  if (changed) byId("chart-metric").value = items.some((item) => item.want_count != null) ? "want_count" : "price_fen";
  byId("detail-title").textContent = target.official_title || target.title;
  byId("detail-platform").textContent = platformNames[target.platform] + (target.kind === "product" ? " · 商品详情" : " · 店铺详情");
  byId("detail-platform-id").textContent = "ID " + target.external_id;
  byId("detail-shop-name").hidden = target.kind !== "product";
  byId("detail-shop-name").textContent = "店铺 " + (target.shop_name || "—");
  byId("detail-last-fetched").textContent = "最近抓取 " + observedTime(observation.last_fetched_at);
  const cover = byId("detail-cover");
  cover.hidden = !target.cover_url;
  if (target.cover_url) cover.src = target.cover_url;
  else cover.removeAttribute("src");
  const sourceLink = byId("source-link");
  sourceLink.href = target.url;
  const latestJob = hours[0];
  sourceLink.dataset.reviewSlot = target.kind === "product" && ["failed", "unavailable"].includes(latestJob?.state)
    ? latestJob.slot : "";
  byId("download").href = "/api/v1/targets/" + id + "/series.csv";
  byId("toggle-target").textContent = target.enabled ? "暂停监控" : "恢复监控";
  byId("refresh-target").disabled = !targetHasSource(target);
  byId("refresh-target").title = targetHasSource(target) ? "" : "该目标尚未配置数据源";
  byId("rename-form").elements.title.value = target.title;
  const rows = items.map((item) => {
    const row = node("tr");
    const observed = node("td", time(item.observed_at));
    observed.append(node("span", sourceNames[item.source], "meta"));
    const value = node("td", metric(item, target) ?? "—");
    const notes = {counter_reset: "计数回退", display_only: "仅展示值"};
    if (target.platform === "xiaohongshu" && notes[item.quality]) value.append(node("span", notes[item.quality], "meta"));
    row.append(observed, value, node("td", price(item.price_fen)),
      node("td", states[item.listing_state] || "—"),
      node("td", delta(item.hourly_delta)), node("td", delta(item.daily_delta)));
    return row;
  });
  if (!rows.length) {
    const row = node("tr");
    const cell = node("td", "暂无观测记录。可先手工记录读数。");
    cell.colSpan = 6;
    row.append(cell);
    rows.push(row);
  }
  byId("history").replaceChildren(...rows);
  drawChart(items, target);
  if (!byId("detail-dialog").open) {
    byId("detail-message").textContent = "";
    byId("reading-form").reset();
    byId("visible-reading-form").reset();
    byId("detail-dialog").showModal();
    requestAnimationFrame(redrawVisibleChart);
  }
}

function detailMetricName(target) {
  if (target.platform === "xiaohongshu") return "展示销量";
  return target.kind === "shop" ? "在售数" : "想要数";
}

function renderTargetSummary(target, items, hours, days, preview, threeHour) {
  const key = detailMetricName(target);
  byId("detail-count-label").textContent = target.platform === "xiaohongshu" ? "总销量" : key;
  byId("daily-metric-label").textContent = key;
  byId("daily-title").textContent = "每日" + key + "趋势";
  const latest = days[0];
  const observedCount = latest?.value ?? latest?.display;
  const previewCount = preview ? (
    target.platform === "xiaohongshu" ? preview.exact_sales ?? preview.raw_sales : preview.want_count
  ) : null;
  const previewIsNewer = preview && (!latest || new Date(preview.observed_at) > new Date(latest.observed_at));
  const count = previewIsNewer && previewCount != null ? previewCount : observedCount;
  byId("detail-current-count").textContent = typeof count === "number" ? count.toLocaleString("zh-CN") : count ?? "—";
  const priceReading = items.find((item) => item.price_fen != null);
  const readPrice = preview?.price_fen != null && (
    !priceReading || new Date(preview.observed_at) > new Date(priceReading.observed_at)
  ) ? preview : priceReading;
  byId("detail-price-label").textContent = readPrice?.price_label || "观测价格";
  byId("detail-current-price").textContent = price(readPrice?.price_fen);
  const hour = hours.find((item) => item.state === "success");
  const today = hour && dayFormat.format(new Date(hour.slot)) === dayFormat.format(new Date());
  const latestHour = hours[0];
  const latestReadable = latestHour?.state === "success" && deltaMetric(latestHour, target) != null;
  byId("detail-today-change").textContent = today ? delta(hour.daily_delta ?? 0) : "—";
  byId("detail-hour-change").textContent = deltaOrZero(latestHour?.hourly_delta, latestReadable);
  byId("detail-three-hour-change").textContent = delta(threeHour);
}

function renderDetailCategory(target, customCategories) {
  byId("category-form").hidden = target.kind !== "product";
  if (target.kind !== "product") return;
  const select = byId("detail-category");
  const custom = byId("custom-category");
  const names = new Set(["笔试", "面试", "教程", "模板", "资料", "其他",
    ...customCategories, target.category].filter(Boolean));
  select.replaceChildren(new Option("未分类", ""), ...[...names].map((name) => new Option(name, "c:" + name)),
    new Option("自定义分类…", "custom"));
  select.value = target.category ? "c:" + target.category : "";
  custom.value = "";
  toggleCategoryInput();
}

function toggleCategoryInput() {
  const custom = byId("custom-category");
  const active = byId("detail-category").value === "custom";
  custom.hidden = !active;
  custom.required = active;
}

function showDetailView(view) {
  ["daily", "hourly", "raw"].forEach((key) => {
    byId("detail-" + key).hidden = key !== view;
  });
  document.querySelectorAll("[data-detail-view]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.detailView === view));
  });
  redrawVisibleChart();
}

function renderDailyTable() {
  const lastPage = Math.max(0, Math.ceil(dailyRows.length / dailyPageSize) - 1);
  dailyPage = Math.min(dailyPage, lastPage);
  const start = dailyPage * dailyPageSize;
  const items = dailyRows.slice(start, start + dailyPageSize);
  const list = byId("daily-history");
  const rows = items.map((item) => {
    const row = node("tr");
    const change = node("td", delta(item.delta), item.delta > 0 ? "delta-positive" : item.delta < 0 ? "delta-negative" : "");
    row.append(node("td", item.day), node("td", observedTime(item.observed_at)),
      node("td", item.display ?? item.value ?? "—"), change);
    return row;
  });
  if (!items.length) {
    const row = node("tr");
    const cell = node("td", "暂无每日观测记录");
    cell.colSpan = 4;
    row.append(cell);
    rows.push(row);
  }
  list.replaceChildren(...rows);
  byId("daily-range").textContent = items.length
    ? "第 " + (dailyPage + 1) + " / " + (lastPage + 1) + " 页 · " + (start + 1) + "–" + (start + items.length)
        + " / 共 " + dailyRows.length + " 天"
    : "共 0 天";
  byId("daily-prev").disabled = dailyPage === 0;
  byId("daily-next").disabled = dailyPage === lastPage;
}

function chartSurface(canvas) {
  const {width, height} = canvas.getBoundingClientRect();
  if (!width || !height) return null;
  const ratio = window.devicePixelRatio || 1;
  const pixelsWide = Math.max(1, Math.round(width * ratio));
  const pixelsHigh = Math.max(1, Math.round(height * ratio));
  if (canvas.width !== pixelsWide) canvas.width = pixelsWide;
  if (canvas.height !== pixelsHigh) canvas.height = pixelsHigh;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.setTransform(pixelsWide / width, 0, 0, pixelsHigh / height, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return {ctx, width, height};
}

function redrawVisibleChart() {
  if (!byId("detail-dialog").open || !current) return;
  if (!byId("detail-daily").hidden) drawDailyChart();
  else if (!byId("detail-raw").hidden) drawChart(currentReadings, current);
}

function drawDailyChart() {
  const canvas = byId("daily-chart");
  const records = dailyRows.slice(0, 60).reverse();
  const values = records.map((row) => row.value).filter((value) => value != null);
  canvas.hidden = !values.length;
  byId("daily-chart-empty").hidden = values.length > 0;
  if (!values.length) return;

  const surface = chartSurface(canvas);
  if (!surface) return;
  const {ctx, width, height} = surface;
  const left = width < 420 ? 55 : 75;
  const right = width - 20, top = 20, bottom = height - 37;
  const dayStamp = (value) => Date.parse(value + "T00:00:00Z");
  const firstDay = dayStamp(records[0].day);
  const lastDay = dayStamp(records.at(-1).day);
  const minimum = Math.min(...values), maximum = Math.max(...values);
  const range = maximum - minimum;
  ctx.fillStyle = "#304c43";
  ctx.strokeStyle = "#d1e1d9";
  ctx.lineWidth = 1;
  ctx.font = (width < 420 ? 12 : 14) + "px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let index = 0; index <= 4; index++) {
    const y = top + (bottom - top) * index / 4;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
    if (range ? range >= 4 || index === 0 || index === 4 : index === 2) {
      const label = range ? Math.round(maximum - range * index / 4) : maximum;
      ctx.fillText(String(label), left - 12, y);
    }
  }

  ctx.lineWidth = 2.5;
  ctx.strokeStyle = "#087b60";
  ctx.fillStyle = "#087b60";
  let previous = null;
  records.forEach((row) => {
    if (row.value == null) {
      previous = null;
      return;
    }
    const day = dayStamp(row.day);
    const x = lastDay > firstDay ? left + (right - left) * (day - firstDay) / (lastDay - firstDay) : (left + right) / 2;
    const y = range ? bottom - (bottom - top) * (row.value - minimum) / range : (bottom + top) / 2;
    if (previous && day - previous.day === 86400000 && row.quality !== "counter_reset") {
      ctx.beginPath();
      ctx.moveTo(previous.x, previous.y);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(x, y, records.length > 30 ? 3 : 4, 0, Math.PI * 2);
    ctx.fill();
    previous = {day, x, y};
  });
  ctx.fillStyle = "#4a615b";
  ctx.textAlign = "left";
  ctx.fillText(records[0].day, firstDay === lastDay ? (left + right) / 2 - 45 : left, bottom + 21);
  if (firstDay !== lastDay) {
    ctx.textAlign = "right";
    ctx.fillText(records.at(-1).day, right, bottom + 21);
  }
}

function renderHourly(target, hours) {
  const body = byId("hourly-history");
  if (!hours.length) {
    const row = node("tr");
    const cell = node("td", "暂无整点观测记录");
    cell.colSpan = 7;
    row.append(cell);
    body.replaceChildren(row);
    return;
  }
  const readableHours = hours.filter((item) => item.state === "success" && deltaMetric(item, target) != null);
  body.replaceChildren(...hours.map((hour) => {
    const row = node("tr");
    const values = [metric(hour, target), hour.price_fen != null ? price(hour.price_fen) : null]
      .filter((value) => value != null);
    const readable = readableHours.includes(hour);
    const status = node("td", jobNames[hour.state] || hour.state);
    if (hour.error) {
      status.title = hour.error;
      status.append(node("span", hour.error, "meta"));
    }
    const hourlyCell = node("td", deltaOrZero(hour.hourly_delta, readable));
    const dailyCell = node("td", deltaOrZero(hour.daily_delta, readable));
    if (readable && hour.hourly_delta != null) {
      // 中间整点缺采时，后端会回溯到最近一条有效读数，这里如实标出实际跨度。
      const earlier = readableHours.find((item) => item.slot < hour.slot);
      const gap = earlier ? Math.round((new Date(hour.slot) - new Date(earlier.slot)) / 3600000) : 0;
      if (gap > 1) hourlyCell.title = `与 ${gap} 小时前的读数相比（中间整点缺采）`;
    }
    if (readable && hour.hourly_delta == null) hourlyCell.title = "与上一小时不可比（缺采或计数回退），按 0 显示";
    if (readable && hour.daily_delta == null) dailyCell.title = "当日基准缺失，按 0 显示";
    row.append(node("td", observedTime(hour.slot)), node("td", observedTime(hour.observed_at)),
      node("td", values.join(" · ") || "—"), hourlyCell, dailyCell,
      node("td", observedTime(hour.daily_baseline_at)), status);
    return row;
  }));
}

async function loadShop() {
  const version = ++shopRequest;
  const params = new URLSearchParams({page: shopPage, page_size: byId("shop-page-size").value});
  const {target, latest, observation, comparison, products, pagination} =
    await request("/targets/" + shopId + "/products?" + params);
  if (version !== shopRequest || activeView !== "shop-page") return;
  byId("shop-name").textContent = target.title + "的商品";
  byId("shop-observation-times").textContent =
    "首次观测 " + observedTime(observation.first_observed_at) + " · 最后观测 " + observedTime(observation.last_observed_at);
  const xhs = target.platform === "xiaohongshu";
  byId("shop-product-metric").textContent = xhs ? "展示销量" : "想要数";
  byId("shop-source").textContent = platformNames[target.platform] + "店铺主页";
  byId("shop-source").href = target.url;
  byId("shop-source").hidden = false;
  byId("manage-shop").disabled = false;
  byId("collect-shop").disabled = collectingShops.has(target.id);
  const status = byId("shop-catalog-status");
  const comparisonText = byId("shop-catalog-change");
  const download = byId("shop-catalog-download");
  const list = byId("shop-catalog-items");
  const {page, page_count: pageCount, total, offset: first} = pagination;
  shopPage = page;
  shopPageCount = pageCount;
  byId("shop-prev").disabled = page <= 1;
  byId("shop-next").disabled = page >= pageCount;
  byId("shop-page-number").max = pageCount;
  byId("shop-page-number").value = page;
  byId("shop-page-total").textContent = "/ " + pageCount + " 页";
  byId("shop-page-range").textContent = total
    ? "第 " + (first + 1) + "–" + (first + products.length) + " 条 / 共 " + total + " 条"
    : "共 0 条";
  let coverage = "";
  if (latest) {
    coverage = latest.complete ? "清单已采齐"
      : total < latest.active_count ? "清单未采齐，尚缺 " + (latest.active_count - total) + " 条链接"
        : total > latest.active_count ? "商品条数与在售数不一致" : "清单完整性待确认";
  }
  status.textContent = latest ? time(latest.observed_at) + " · 在售 " + latest.active_count
    + " · 已采集 " + total + " 条链接 · " + coverage : "暂无在售商品采集记录。";

  comparisonText.hidden = !comparison;
  if (comparison) {
    comparisonText.textContent = "在售数较上次 " + delta(comparison.active_change);
    if (comparison.added != null) {
      comparisonText.textContent += " · 新出现在售列表 " + comparison.added
        + " 条 · 移出在售列表 " + comparison.removed + " 条";
    }
  }
  download.hidden = total === 0 && !latest?.complete;
  download.textContent = latest?.complete ? "导出商品链接 CSV" : "导出已采商品 CSV";
  download.href = "/api/v1/targets/" + target.id + "/catalog.csv";
  if (products.length) {
    list.replaceChildren(...products.map((item, index) => shopProductRow(item, first + index + 1, xhs)));
  } else {
    const row = node("tr");
    const cell = node("td", "暂无已采集商品。");
    cell.colSpan = 5;
    row.append(cell);
    list.replaceChildren(row);
  }
}

function shopProductRow(item, number, xhs) {
  const row = node("tr");
  const product = node("td");
  const link = node("a", item.title, "shop-product-link");
  link.href = item.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  product.append(link, node("span", item.id, "meta"));

  const cost = node("td", price(item.price_fen));
  if (item.price_observed_at) {
    const label = item.price_source === "catalog" ? "店铺标价" : "商品观测";
    cost.append(node("span", label + " · " + time(item.price_observed_at), "meta"));
  }
  const metric = node("td", (xhs ? item.raw_sales : wantLabel(item.want_count)) ?? "—");
  const metricAt = xhs ? item.sales_observed_at : item.want_observed_at;
  if (metricAt) metric.append(node("span", time(metricAt), "meta"));

  const action = node("td");
  if (item.target_id != null) {
    const details = node("button", "查看记录");
    details.addEventListener("click", () => perform(() => openDetail(item.target_id)));
    action.append(details);
  } else {
    action.textContent = "未加入单品监控";
  }
  row.append(node("td", number, "shop-row-number"), product, cost, metric, action);
  return row;
}

const HOUR_MS = 3600000;
const hourFormat = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const shortDayFormat = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit"});

function drawChart(items, target) {
  const key = target.platform === "xiaohongshu" ? "exact_sales"
    : isXianyuProduct(target) ? byId("chart-metric").value : "price_fen";
  const preferred = ["scheduled", "manual", "refresh", "catalog"].find((source) =>
    items.some((item) => item.source === source && item[key] != null));
  const records = items.filter((item) => item.source === preferred).slice(0, 35).reverse();
  const points = records.map((item) => item[key]);
  const values = points.filter((value) => value != null);
  byId("chart-wrap").hidden = values.length < 2;
  if (values.length < 2) return;
  const labels = {exact_sales: "展示销量", price_fen: "价格", want_count: "想要数"};
  byId("chart-title").textContent = labels[key] + " · " + sourceNames[preferred];
  const surface = chartSurface(byId("chart"));
  if (!surface) return;
  const {ctx, width, height} = surface;
  const left = width < 420 ? 58 : 75;
  const right = width - 21, top = 20, bottom = height - 43;
  const lowest = Math.min(...values);
  const highest = Math.max(...values);
  const distance = (highest - lowest) || 1;
  const stamps = records.map((item) => Date.parse(item.slot || item.observed_at) || 0);
  const firstStamp = stamps[0], lastStamp = stamps.at(-1);
  const span = Math.max(lastStamp - firstStamp, 1);
  const xAt = (instant) => left + (instant - firstStamp) / span * (right - left);
  ctx.font = (width < 420 ? 11 : 12) + "px system-ui, sans-serif";
  ctx.lineWidth = 1;

  // 横轴先按整点铺网格：每格一小时，标签按可用宽度自动稀疏，避免互相压住。
  const roomPerHour = (right - left) / Math.max(span / HOUR_MS, 1);
  ctx.font = "10px system-ui, sans-serif";
  const labelSpace = ctx.measureText("00:00").width + 6;
  const step = [1, 2, 3, 4, 6, 8, 12, 24, 48, 72, 168]
    .find((hours) => hours * roomPerHour >= labelSpace) || 168;
  const ticks = [];
  for (let instant = Math.ceil(firstStamp / HOUR_MS) * HOUR_MS; instant <= lastStamp; instant += step * HOUR_MS) {
    ticks.push(instant);
  }

  ctx.strokeStyle = "#d4e5dc";
  ctx.fillStyle = "#415953";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.fillText(key === "price_fen" ? price(highest) : String(highest), left - 10, top);
  if (highest !== lowest) ctx.fillText(key === "price_fen" ? price(lowest) : String(lowest), left - 10, bottom);
  ctx.strokeStyle = "#d4e5dc";
  [top, (top + bottom) / 2, bottom].forEach((y) => {
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
  });

  ctx.strokeStyle = "#e3ede8";
  ticks.forEach((instant) => {
    const x = xAt(instant);
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
  });
  ctx.strokeStyle = "#b9d2c7";
  ctx.fillStyle = "#4a615b";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ticks.forEach((instant) => {
    const tickX = xAt(instant);
    ctx.beginPath();
    ctx.moveTo(tickX, bottom);
    ctx.lineTo(tickX, bottom + 4);
    ctx.stroke();
    const label = hourFormat.format(new Date(instant));
    const text = label === "00:00" ? shortDayFormat.format(new Date(instant)) : label;
    const half = ctx.measureText(text).width / 2;
    // 首尾刻度贴着画布边缘，把文字收回绘图区内，避免被裁掉。
    ctx.fillText(text, Math.min(Math.max(tickX, left + half), right - half), bottom + 8);
  });
  if (!ticks.length) {
    ctx.fillText(time(records[0].observed_at), left, bottom + 8);
    ctx.fillText(time(records.at(-1).observed_at), right, bottom + 8);
  }

  ctx.strokeStyle = "#17644d";
  ctx.fillStyle = "#17644d";
  ctx.lineWidth = 2.5;
  let previous = null;
  points.forEach((value, index) => {
    if (value == null) {
      previous = null;
      return;
    }
    const x = xAt(stamps[index]);
    const y = highest === lowest ? (top + bottom) / 2 : bottom - (value - lowest) * (bottom - top) / distance;
    const gap = records[index].slot && records[index - 1]?.slot
      && Date.parse(records[index].slot) - Date.parse(records[index - 1].slot)
        > (current?.kind === "shop" ? 86400000 : HOUR_MS);
    if (previous && !gap && records[index].quality !== "counter_reset") {
      ctx.beginPath();
      ctx.moveTo(...previous);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, 2 * Math.PI);
    ctx.fill();
    previous = [x, y];
  });
}

async function detailAction(button, action, success) {
  const notice = byId("detail-message");
  const id = current.id;
  button.disabled = true;
  try {
    await action(id);
    if (current?.id === id && byId("detail-dialog").open) {
      await openDetail(id);
      notice.textContent = success;
    }
    await (activeView === "shop-page" ? loadShop() : loadPool());
  } catch (error) {
    notice.textContent = error.message;
  } finally {
    button.disabled = button.id === "refresh-target" && !targetHasSource(current);
  }
}

async function removeTarget(target, button) {
  if (!window.confirm("删除「" + target.title + "」的监控？重新添加同一链接可恢复历史。")) return;
  button.disabled = true;
  try {
    await request("/targets/" + target.id, "DELETE");
    if (current?.id === target.id && byId("detail-dialog").open) {
      detailRequest++;
      current = null;
      byId("detail-dialog").close();
    }
    if (activeView === "shop-page" && shopId === target.id) {
      await show("pool");
      history.replaceState(null, "", "#");
    } else {
      await loaders[activeView]();
    }
    const notice = byId("app-status");
    notice.textContent = "已从监控池删除「" + target.title + "」。";
    notice.hidden = false;
  } catch (error) {
    if (byId("detail-dialog").open) byId("detail-message").textContent = error.message;
    else message(error);
  } finally {
    button.disabled = false;
  }
}

async function loadEvents() {
  const {items} = await request("/events");
  drawRecords(byId("event-list"), items.map((event) => {
    const entry = line("", event.summary + (event.matched.length ? " · " + event.matched.join("、") : ""));
    const button = node("button", event.title, "target-button");
    button.addEventListener("click", () => perform(() => openDetail(event.target_id)));
    entry.firstChild.replaceWith(button, node("span", platformNames[event.platform] + " · " + time(event.created_at), "meta"));
    return entry;
  }), "暂无异动");
}

async function loadRuns() {
  const {jobs, deliveries} = await request("/ops/runs");
  drawRecords(byId("job-list"), jobs.map((job) => line(job.title,
    platformNames[job.platform] + " · " + time(job.slot) + " · " + jobNames[job.state]
      + (job.error ? " · " + job.error : ""))), "暂无定时任务");
  drawRecords(byId("deliveries"), deliveries.map((delivery) => line(
    delivery.channel === "dingtalk" ? "钉钉" : "企业微信",
    deliveryNames[delivery.state] + (delivery.error ? " · " + delivery.error : ""))), "暂无通知投递");
}

async function loadRules() {
  const settings = await request("/rules");
  const form = byId("rule-form").elements;
  form.hourly_units.value = settings.hourly_units;
  form.premium_price.value = settings.premium_price_fen / 100;
  form.premium_units.value = settings.premium_units;
  form.price_drop.value = settings.price_drop_fen / 100;
  form.state_alerts.checked = settings.state_alerts;
}

const loaders = {pool: loadPool, "shop-page": loadShop, events: loadEvents, rules: loadRules, runs: loadRuns};
function show(view) {
  activeView = view;
  for (const name of Object.keys(loaders)) byId(name).hidden = name !== view;
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === (view === "shop-page" ? "pool" : view);
    button.classList.toggle("selected", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  message();
  byId("app-status").hidden = true;
  return loaders[view]();
}

function route() {
  const match = /^#shop\/([1-9][0-9]*)$/.exec(location.hash);
  if (match) {
    const nextShopId = Number(match[1]);
    if (shopId !== nextShopId || activeView !== "shop-page") shopPage = 1;
    shopId = nextShopId;
    byId("shop-prev").disabled = true;
    byId("shop-next").disabled = true;
    byId("shop-page-range").textContent = "";
    byId("shop-name").textContent = "店铺商品";
    byId("shop-catalog-status").textContent = "正在加载商品清单";
    byId("shop-observation-times").textContent = "";
    byId("shop-catalog-download").hidden = true;
    byId("shop-catalog-change").hidden = true;
    const collectionMessage = byId("shop-collection-message");
    collectionMessage.textContent = collectingShops.has(shopId) ? "正在采集店铺商品…" : "";
    collectionMessage.hidden = !collectionMessage.textContent;
    byId("shop-source").hidden = true;
    byId("manage-shop").disabled = true;
    byId("collect-shop").disabled = true;
    byId("shop-catalog-items").replaceChildren();
    return show("shop-page");
  }
  const view = location.hash.slice(1);
  return show(["events", "rules", "runs"].includes(view) ? view : "pool");
}

function go(view) {
  const hash = view === "pool" ? "#" : "#" + view;
  if (location.hash === hash || (view === "pool" && !location.hash)) return perform(route);
  location.hash = hash;
}

window.addEventListener("hashchange", () => perform(route));
document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => go(button.dataset.view));
});
document.querySelector(".brand").addEventListener("click", (event) => {
  event.preventDefault();
  go("pool");
});
byId("back-to-shops").addEventListener("click", () => {
  byId("kind").value = "shop";
  offset = 0;
  go("pool");
});
byId("manage-shop").addEventListener("click", () => perform(() => openDetail(shopId)));
byId("collect-shop").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const id = shopId;
  const notice = byId("shop-collection-message");
  const stillHere = () => activeView === "shop-page" && shopId === id;
  collectingShops.add(id);
  button.disabled = true;
  notice.hidden = false;
  notice.textContent = "正在采集店铺商品…";
  try {
    await request("/targets/" + id + "/refresh", "POST");
    if (stillHere()) {
      shopPage = 1;
      await loadShop();
      if (stillHere()) notice.hidden = true;
    }
  } catch (error) {
    if (stillHere()) notice.textContent = error.message;
  } finally {
    collectingShops.delete(id);
    if (stillHere()) button.disabled = false;
  }
});

function changeShopPage(page) {
  shopPage = Math.max(1, Math.min(page, shopPageCount));
  perform(loadShop);
}
byId("shop-prev").addEventListener("click", () => changeShopPage(shopPage - 1));
byId("shop-next").addEventListener("click", () => changeShopPage(shopPage + 1));
byId("shop-page-size").addEventListener("change", () => changeShopPage(1));
byId("shop-page-form").addEventListener("submit", (event) => {
  event.preventDefault();
  changeShopPage(byId("shop-page-number").valueAsNumber);
});
document.querySelectorAll("[data-platform]").forEach((button) => {
  button.addEventListener("click", () => {
    platform = button.dataset.platform;
    document.querySelectorAll("[data-platform]").forEach((tab) => {
      tab.classList.toggle("selected", tab === button);
      tab.setAttribute("aria-pressed", String(tab === button));
    });
    changeFilter();
  });
});
function changeFilter() {
  offset = 0;
  perform(loadPool);
}
byId("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(changeFilter, 200);
});
byId("kind").addEventListener("change", changeFilter);
byId("sort").addEventListener("change", changeFilter);
document.querySelectorAll("[data-sort]").forEach((button) => {
  button.addEventListener("click", () => {
    const sort = byId("sort");
    const key = button.dataset.sort;
    sort.value = sort.value === key ? key + "_asc" : key;
    changeFilter();
  });
});
byId("prev").addEventListener("click", () => {
  offset = Math.max(0, offset - pageSize);
  perform(loadPool);
});
byId("next").addEventListener("click", () => {
  offset += pageSize;
  perform(loadPool);
});
document.querySelectorAll("dialog .close, dialog .cancel").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});
byId("open-import").addEventListener("click", () => byId("import-dialog").showModal());
byId("import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const results = byId("import-result");
  button.disabled = true;
  results.replaceChildren(line("正在识别链接"));
  try {
    const {items} = await request("/targets/batch", "POST", {text: event.currentTarget.elements.links.value});
    const created = items.filter((item) => item.status === "created").length;
    const existing = items.filter((item) => item.status === "existing").length;
    const restored = items.filter((item) => item.status === "restored").length;
    const rejected = items.filter((item) => item.status === "rejected");
    results.replaceChildren(line("新增 " + created + " · 恢复 " + restored + " · 已存在 " + existing + " · 失败 " + rejected.length),
      ...items.map(importResult));
    if (!rejected.length) byId("import-form").reset();
    changeFilter();
  } catch (error) {
    results.replaceChildren(line(error.message));
  } finally {
    button.disabled = false;
  }
});

function importResult(item) {
  if (item.status === "rejected") return line(item.reason, item.url);
  const sources = {
    share: "标题来自分享文案",
    catalog: "标题来自已采集的店铺商品",
    saved: "沿用已保存名称",
    unavailable: "标题待核实",
  };
  const status = {created: "已添加", restored: "已恢复", existing: "已存在"}[item.status];
  const type = item.kind === "shop" ? "店铺" : "商品";
  const entry = line(item.title, [status, platformNames[item.platform], type,
    "ID " + item.external_id, sources[item.title_source]].join(" · "));
  const link = node("a", "打开识别结果");
  link.href = item.resolved_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  const actions = node("div", null, "detail-actions");
  actions.append(link);
  if (item.kind === "shop") {
    const catalog = node("a", "查看店铺商品");
    catalog.href = "#shop/" + item.id;
    catalog.addEventListener("click", () => {
      byId("import-dialog").close();
      if (location.hash === catalog.hash) perform(route);
    });
    actions.append(catalog);
  } else {
    const reading = item.reading;
    const shown = item.source_preview || reading || item.catalog_preview;
    if (shown) {
      const shownPrice = shown.price_fen == null ? null
        : [shown.price_label, price(shown.price_fen)].filter(Boolean).join(" ");
      const values = [shownPrice, metric(shown, item)];
      const label = item.source_preview ? "本次读取" : reading ? "已记录" : "店铺采集值";
      const source = shown === reading ? sourceNames[shown.source] : "";
      entry.append(node("p", [label, ...values, source, time(shown.observed_at)]
        .filter((value) => value != null && value !== "").join(" · ")));
    } else {
      entry.append(node("p", "链接已记录"));
    }
    if (item.preview_error) entry.append(node("p", item.preview_error));
    const detail = node("button", reading ? "查看记录" : "补录读数");
    detail.type = "button";
    detail.addEventListener("click", () => perform(async () => {
      byId("import-dialog").close();
      await openDetail(item.id);
      if (!reading) byId("reading-details").open = true;
    }));
    actions.append(detail);
  }
  entry.append(actions);
  return entry;
}

document.querySelectorAll("[data-detail-view]").forEach((button) => {
  button.addEventListener("click", () => showDetailView(button.dataset.detailView));
});
byId("detail-cover").addEventListener("error", (event) => { event.currentTarget.hidden = true; });
byId("daily-prev").addEventListener("click", () => {
  dailyPage--;
  renderDailyTable();
});
byId("daily-next").addEventListener("click", () => {
  dailyPage++;
  renderDailyTable();
});
byId("delete-target").addEventListener("click", (event) => {
  const button = event.currentTarget;
  perform(() => removeTarget(current, button));
});
byId("toggle-target").addEventListener("click", (event) => {
  const enable = !current.enabled;
  detailAction(event.currentTarget, (id) => request("/targets/" + id, "PATCH", {enabled: enable}),
    enable ? "监控已恢复" : "监控已暂停");
});
byId("refresh-target").addEventListener("click", (event) => {
  detailAction(event.currentTarget, (id) => request("/targets/" + id + "/refresh", "POST"), "采样已保存");
});
byId("source-link").addEventListener("click", (event) => {
  if (current) markSamplingReviewed(current.id, event.currentTarget.dataset.reviewSlot);
});
byId("detail-category").addEventListener("change", () => {
  toggleCategoryInput();
  if (!byId("custom-category").hidden) byId("custom-category").focus();
});
byId("category-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = byId("detail-category").value;
  const category = value === "custom" ? byId("custom-category").value.trim() : value.slice(2);
  if (value === "custom" && !category) {
    byId("detail-message").textContent = "请输入分类名称";
    return;
  }
  detailAction(event.submitter, (id) => request("/targets/" + id, "PATCH", {category: category || null}),
    "分类已保存");
});
byId("rename-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const title = event.currentTarget.elements.title.value;
  detailAction(event.submitter, (id) => request("/targets/" + id, "PATCH", {title}), "名称已更新");
});
byId("chart-metric").addEventListener("change", () => drawChart(currentReadings, current));
byId("visible-reading-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  detailAction(event.submitter, async (id) => {
    await request("/targets/" + id + "/readings/visible-text", "POST", {text: form.elements.text.value});
    form.reset();
  }, "商品读数已保存");
});
byId("reading-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const raw = form.elements;
  const reading = {
    raw_sales: current.platform === "xiaohongshu" ? raw.raw_sales.value.trim() || null : null,
    want_count: isXianyuProduct(current) && raw.want_count.value !== "" ? raw.want_count.valueAsNumber : null,
    price_fen: raw.price.value === "" ? null : Math.round(raw.price.valueAsNumber * 100),
    listing_state: raw.listing_state.value || null,
  };
  detailAction(event.submitter, async (id) => {
    await request("/targets/" + id + "/readings", "POST", reading);
    form.reset();
  }, "读数已保存");
});
byId("rule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget.elements;
  const notice = byId("rule-message");
  try {
    await request("/rules", "PUT", {
      hourly_units: form.hourly_units.valueAsNumber,
      premium_price_fen: Math.round(form.premium_price.valueAsNumber * 100),
      premium_units: form.premium_units.valueAsNumber,
      price_drop_fen: Math.round(form.price_drop.valueAsNumber * 100),
      state_alerts: form.state_alerts.checked,
    });
    notice.textContent = "规则已保存";
  } catch (error) {
    notice.textContent = error.message;
  }
});
byId("reload-runs").addEventListener("click", () => perform(loadRuns));

if (typeof ResizeObserver !== "undefined") {
  const charts = new ResizeObserver(redrawVisibleChart);
  charts.observe(byId("daily-chart"));
  charts.observe(byId("chart"));
}
window.addEventListener("resize", redrawVisibleChart);

perform(() => Promise.all([loadHealth(), route()]));
setInterval(() => {
  if (!document.hidden && !document.querySelector("dialog[open]") && activeView !== "rules") {
    perform(loaders[activeView]);
  }
}, 30000);
