/* Weekly hours: Sunday first; null means unknown, [] means closed, otherwise
   [{"opens":"09:00","closes":"17:00"}]. A closing time earlier than or
   equal to opening means the interval continues into the following day. */
"use strict";

const DAY_LABELS = ["יום א׳", "יום ב׳", "יום ג׳", "יום ד׳", "יום ה׳", "יום ו׳", "שבת"];
const DAY_SHORT = ["א׳", "ב׳", "ג׳", "ד׳", "ה׳", "ו׳", "ש׳"];
// A shared line-icon vocabulary keeps categories consistent across platforms.
const TYPE_ICON_PATHS = {
  "בר": ["M5 4h14l-7 8-7-8Z", "M12 12v7", "M8 20h8"],
  "בית קפה": ["M4 7h13v7a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5V7Z", "M17 9h1.5a2.5 2.5 0 0 1 0 5H17", "M7 3v2M12 3v2"],
  "מסעדה": ["M5 3v6a3 3 0 0 0 6 0V3", "M8 3v18", "M17 3c-2 2-3 5-3 8h3v10", "M17 3v8"],
  "ספרייה": ["M4 5h4v14H4zM9 4h4v15H9zM15 6l3-1 3 13-3 1z", "M4 21h17"],
  "יד שנייה": ["M3 5v7l8 8 9-9-8-8H5a2 2 0 0 0-2 2Z", "M8 8h.01"]
};

function israelTime(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Jerusalem", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return { day: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(values.weekday),
    minutes: Number(values.hour) * 60 + Number(values.minute) };
}

function parseTime(time) {
  if (typeof time !== "string" || !/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) return null;
  const [hour, minute] = time.split(":").map(Number);
  return hour * 60 + minute;
}

function validDay(day) {
  return Array.isArray(day) && day.every(period => period && parseTime(period.opens) !== null && parseTime(period.closes) !== null);
}

function getStatus(hours, now = israelTime()) {
  const today = hours?.[now.day];
  const yesterday = hours?.[(now.day + 6) % 7];
  if (validDay(yesterday)) {
    const overnight = yesterday.find(period => {
      const start = parseTime(period.opens), end = parseTime(period.closes);
      return end <= start && now.minutes < end;
    });
    if (overnight) return { key: "open", label: "פתוח עכשיו" };
  }
  if (!validDay(today)) return { key: "unknown", label: "שעות לא ידועות" };
  for (const period of today) {
    const start = parseTime(period.opens), end = parseTime(period.closes);
    if (now.minutes >= start && (end <= start || now.minutes < end)) return { key: "open", label: "פתוח עכשיו" };
  }
  // An unknown previous day might contain an overnight interval still open.
  if (!validDay(yesterday)) return { key: "unknown", label: "שעות לא ידועות" };
  const next = today.map(period => parseTime(period.opens)).filter(time => time > now.minutes).sort((a, b) => a - b)[0];
  if (next !== undefined) return { key: "later", label: `ייפתח היום ב־${String(Math.floor(next / 60)).padStart(2, "0")}:${String(next % 60).padStart(2, "0")}` };
  return { key: "closed", label: today.length ? "סגור עכשיו" : "סגור היום" };
}

function formatDay(day) {
  if (!validDay(day)) return "לא ידוע";
  if (!day.length) return "סגור";
  return day.map(period => `${period.opens}–${period.closes}${parseTime(period.closes) <= parseTime(period.opens) ? " למחרת" : ""}`).join(" / ");
}

function formatUpdated(value) {
  if (!value) return "עדכון שעות: לא ידוע";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "עדכון שעות: לא ידוע" : `עדכון שעות: ${new Intl.DateTimeFormat("he-IL", { timeZone: "Asia/Jerusalem", day: "numeric", month: "numeric", year: "numeric" }).format(date)}`;
}

if (typeof module !== "undefined" && module.exports) module.exports = { israelTime, parseTime, validDay, getStatus, formatDay, formatUpdated };

if (typeof document !== "undefined") {
  const $ = id => document.getElementById(id);
  const list = $("places-list"), search = $("search"), sort = $("sort"), count = $("results-count");
  const categoryFilters = Array.from(document.querySelectorAll(".category-filter"));
  const dialog = $("place-dialog");
  let places = [];
  let lastTrigger = null;
  let activeCategory = "all";

  const span = (className, text) => {
    const element = document.createElement("span");
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };

  function typeIcon(type) {
    const wrapper = span("place-type-icon");
    wrapper.setAttribute("aria-hidden", "true");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    const paths = TYPE_ICON_PATHS[type] || ["M4 10 12 4l8 6v10H4V10Z", "M9 20v-6h6v6"];
    for (const drawing of paths) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", drawing);
      svg.append(path);
    }
    wrapper.append(svg);
    return wrapper;
  }

  function dayHours(day) {
    const element = span("day-hours");
    if (!validDay(day) || !day.length) {
      element.textContent = formatDay(day);
      return element;
    }
    day.forEach((period, index) => {
      if (index) element.append(document.createTextNode(" / "));
      const range = document.createElement("bdi");
      range.dir = "ltr";
      range.textContent = `${period.opens}–${period.closes}`;
      element.append(range);
      if (parseTime(period.closes) <= parseTime(period.opens)) element.append(document.createTextNode(" למחרת"));
    });
    return element;
  }

  function openDetails(place, trigger) {
    lastTrigger = trigger;
    $("dialog-title").textContent = place.name;
    $("dialog-category").textContent = place.type;
    $("dialog-address").textContent = place.address || "";
    $("dialog-address").hidden = !place.address;
    $("dialog-description").textContent = place.description || "";
    $("dialog-description").hidden = !place.description;
    const link = $("dialog-link");
    const handle = typeof place.instagram === "string" && /^[A-Za-z0-9._]+$/.test(place.instagram) ? place.instagram : null;
    link.hidden = !handle;
    if (handle) {
      link.href = `https://www.instagram.com/${encodeURIComponent(handle)}/`;
      link.textContent = `@${handle}`;
    } else link.removeAttribute("href");
    dialog.showModal();
  }

  function render() {
    const now = israelTime();
    const focusedId = document.activeElement?.classList.contains("place-row") ? document.activeElement.dataset.placeId : null;
    const query = search.value.trim().normalize("NFKC").toLocaleLowerCase("he");
    const visible = places.filter(place =>
      (activeCategory === "all" || place.type === activeCategory) &&
      place.name.normalize("NFKC").toLocaleLowerCase("he").includes(query));
    const order = { open: 0, later: 1, closed: 2, unknown: 3 };
    const byName = (a, b) => a.name.localeCompare(b.name, "he");
    visible.sort((a, b) => {
      if (sort.value === "name") return byName(a, b);
      return order[getStatus(a.hours, now).key] - order[getStatus(b.hours, now).key] || byName(a, b);
    });
    count.textContent = `${visible.length} ${visible.length === 1 ? "מקום" : "מקומות"}`;
    $("empty-state").hidden = visible.length !== 0;
    list.replaceChildren();
    for (const place of visible) {
      const status = getStatus(place.hours, now);
      const row = document.createElement("button");
      row.type = "button";
      row.className = "place-row";
      row.dataset.placeId = place.id;
      const main = span("place-main"), identity = span("place-identity"), icon = typeIcon(place.type), text = span("place-identity-text");
      text.append(span("place-name", place.name), span("place-type", place.type));
      identity.append(icon, text);
      main.append(identity, span(`place-status status-${status.key}`, status.label));
      const schedule = span("place-schedule");
      for (let day = 0; day < 7; day++) {
        const cell = span(`day-cell${day === now.day ? " today" : ""}`);
        const label = span("day-label");
        const shortLabel = span("", DAY_SHORT[day]);
        shortLabel.setAttribute("aria-hidden", "true");
        label.append(shortLabel, span("sr-only", DAY_LABELS[day]));
        const hours = dayHours(place.hours?.[day]);
        cell.append(label, hours);
        schedule.append(cell);
      }
      const footer = span("place-footer"), prompt = span("place-open", "לפרטים ←");
      prompt.setAttribute("aria-hidden", "true");
      footer.append(span("place-updated", formatUpdated(place.hoursUpdatedAt)), prompt);
      row.append(main, schedule, footer);
      row.addEventListener("click", () => openDetails(place, row));
      list.append(row);
    }
    if (focusedId) {
      const restored = Array.from(list.children).find(row => row.dataset.placeId === focusedId);
      restored?.focus({ preventScroll: true });
    }
  }

  search.addEventListener("input", render);
  sort.addEventListener("change", render);
  categoryFilters.forEach(button => button.addEventListener("click", () => {
    activeCategory = button.dataset.category;
    categoryFilters.forEach(filter => {
      const selected = filter === button;
      filter.classList.toggle("is-active", selected);
      filter.setAttribute("aria-pressed", String(selected));
    });
    render();
  }));
  $("dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => { if (lastTrigger?.isConnected) lastTrigger.focus(); });

  fetch("https://galilinfo.elifdev.com/data/places.json")
    .then(response => { if (!response.ok) throw new Error("Data unavailable"); return response.json(); })
    .then(data => {
      if (!Array.isArray(data) || !data.every(place => place && typeof place.name === "string" && typeof place.type === "string")) throw new Error("Invalid data");
      places = data;
      render();
      // Current status can change without a page reload.
      setInterval(() => { if (!dialog.open) render(); }, 60000);
    })
    .catch(() => { $("load-error").hidden = false; count.textContent = ""; });
}
