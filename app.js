const FAMILIES = {
  "UAT check a message": "Suspicious message",
  "UAT family protection": "Family",
  "UAT live scam": "Scam in progress",
  "UAT policy and claim": "Policy and claim",
  "UAT something else": "Other requests",
  "Enquiry, nothing lost": "Questions",
  "Money or device already gone": "Money already gone",
  "Safety, not a scam": "Not a scam",
  "Second turn changes the answer": "Follow-up",
  "Not a fraud story": "Not a fraud story",
  "Same loss, other languages": "Other languages",
  "Abroad and the helpline": "Calling from abroad",
  "Claim": "Claim",
  "Is this message legitimate": "Is this legitimate",
};

const LANG = new Set(["en", "hi", "hing", "mr", "ta", "te", "bn", "pa"]);

function when(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function host(server) {
  return String(server || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function familyName(name) {
  return FAMILIES[name] || name || "Other";
}

function displayTitle(row) {
  const title = row.title || "";
  if (title.trim()) return title.trim();
  return shortTitle(row.id);
}

function shortTitle(id) {
  let parts = String(id || "").split("-").filter(Boolean);
  if (["UAT", "SYN", "CL"].includes(parts[0])) parts = parts.slice(1);
  if (parts.length > 1 && /^[A-Za-z0-9]{1,6}$/.test(parts[0])) parts = parts.slice(1);
  if (parts.length > 1 && /^\d+[a-z]?$/i.test(parts[0])) parts = parts.slice(1);
  if (LANG.has(parts[parts.length - 1])) parts.pop();
  const text = parts.join(" ");
  if (!text) return id || "Conversation";
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function phrase(raw) {
  return String(raw || "").replace(/"/g, "").replace(/\.$/, "").trim();
}

function gaps(row) {
  const lines = [];
  for (const item of row.missing || []) {
    const text = String(item);
    const prefix = "None of these were in the reply: ";
    if (!text.startsWith(prefix)) {
      lines.push(text.replace(/\.$/, ""));
      continue;
    }
    const first = phrase(text.slice(prefix.length).split(";")[0]);
    const near = first.match(/^(.+?) within a few words of (.+)$/);
    lines.push(near ? "Never linked “" + near[1].trim() + "” with “" + near[2].trim() + "”" : "Never said “" + first + "”");
  }
  return lines;
}

function isNotice(text) {
  const reply = String(text || "");
  return reply.includes("I'm CyberCare+") || reply.includes("accept no liability") || reply.includes("This chat is saved securely");
}

function counts(data) {
  return data.counts || {};
}

function renderScore(data) {
  const root = document.getElementById("score");
  const c = counts(data);
  const total = c.cases || 0;
  const pass = c.pass || 0;
  const fail = c.fail || 0;
  root.replaceChildren();
  if (!data.published_at || !total) {
    root.append(el("p", "meta", "No check published yet."));
    return;
  }
  const meter = el("div", "meter");
  const ok = document.createElement("i");
  ok.className = "ok";
  ok.style.width = Math.round((pass / total) * 100) + "%";
  const no = document.createElement("i");
  no.className = "no";
  no.style.width = Math.round((fail / total) * 100) + "%";
  meter.append(ok, no);
  root.append(meter);
  const row = el("div", "score-row");
  row.append(el("b", "ok", pass + " passed"));
  row.append(el("b", "no", fail + " failed"));
  if (data.environment_label) {
    const env = el("span", "env " + (data.environment || "other"), data.environment_label);
    row.append(env);
  }
  const bits = [when(data.published_at), host(data.server)].filter(Boolean);
  if (c.regression) bits.push(c.regression + " newly broken");
  if (c.fixed) bits.push(c.fixed + " fixed");
  row.append(el("span", "", bits.join(" · ")));
  root.append(row);
}

function renderCase(row) {
  const failed = !row.passed;
  const details = document.createElement("details");
  details.className = "case " + (failed ? "fail" : "pass");
  const summary = document.createElement("summary");
  const missing = gaps(row);
  const meta = [];
  if (row.sheet_row) meta.push("Sheet row " + row.sheet_row);
  if (row.turn_count > 1) meta.push(row.turn_count + " steps");
  if (row.journey && row.journey.mode === "adaptive") {
    const j = row.journey;
    if (j.resolution_met) meta.push("resolution reached");
    else if (j.first_fail_step) meta.push("broke at step " + j.first_fail_step);
    else if (j.journey_complete) meta.push("reached end");
    else if (j.stop_reason) meta.push("stopped: " + j.stop_reason.replace(/_/g, " "));
  }
  summary.append(el("div", "name", displayTitle(row)));
  if (meta.length) summary.append(el("div", "meta", meta.join(" · ")));
  summary.append(el("div", "reason", missing.slice(0, 2).join(" · ") || (failed ? "A required step is missing" : "Did what this check asks")));
  summary.append(el("span", "pill " + (failed ? "fail" : "pass"), failed ? "Failed" : "Passed"));
  details.append(summary);

  const panel = el("div", "panel");
  if (row.expected) {
    panel.append(el("strong", "", "Should have"));
    panel.append(el("p", "", row.expected));
  }
  if (row.journey && row.journey.steps && row.journey.steps.length) {
    panel.append(el("strong", "", "Step check"));
    for (const s of row.journey.steps) {
      const line = "Step " + s.step + ": " + s.status + (s.issues && s.issues.length ? " — " + s.issues.join(", ") : "");
      panel.append(el("p", "", line));
    }
  }
  if (missing.length) {
    panel.append(el("strong", "", "Left out"));
    for (const line of missing) panel.append(el("p", "", line));
  }
  const chat = el("div", "chat");
  const planned = row.turn_count || (row.turns || []).length;
  const adaptive = row.journey && row.journey.mode === "adaptive";
  (row.turns || []).forEach((turn, index) => {
    const user = el("div", "bubble user");
    const stepLabel = adaptive
      ? "Person · step " + (index + 1) + (planned > 1 ? " (" + planned + " turns)" : "")
      : planned > 1
        ? "Person · step " + (index + 1) + " of " + planned
        : "Person · step " + (index + 1);
    user.append(el("div", "who", stepLabel));
    user.append(document.createTextNode(turn.user || ""));
    chat.append(user);
    const replies = (turn.replies || []).filter((reply) => !isNotice(reply));
    const bot = el("div", "bubble bot");
    bot.append(el("div", "who", "Bot"));
    bot.append(document.createTextNode(replies.join("\n\n") || "No reply"));
    chat.append(bot);
  });
  panel.append(chat);
  details.append(panel);
  return details;
}

function matches(row, filter, query) {
  if (filter === "failed" && row.passed) return false;
  if (filter === "broke" && row.status !== "regression") return false;
  if (filter === "fixed" && row.status !== "fixed") return false;
  if (!query) return true;
  const haystack = [row.id, shortTitle(row.id), row.family, row.expected, ...(row.missing || [])].join(" ").toLowerCase();
  return haystack.includes(query);
}

function renderList(data, filter, query) {
  const root = document.getElementById("list");
  root.replaceChildren();
  const rows = (data.results || []).filter((row) => matches(row, filter, query));
  if (!data.published_at) {
    root.append(el("p", "empty", "No check has been published yet."));
    return;
  }
  if (!rows.length) {
    root.append(el("p", "empty", query ? "No match." : "None in this view."));
    return;
  }
  const groups = new Map();
  for (const row of rows) {
    const name = familyName(row.family);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(row);
  }
  for (const [name, cases] of groups) {
    const group = el("section", "group");
    group.append(el("h2", "", name));
    for (const row of cases) group.append(renderCase(row));
    root.append(group);
  }
}

function labelButtons(data) {
  const c = counts(data);
  const labels = {
    failed: "Failed " + (c.fail || 0),
    broke: "Newly broken " + (c.regression || 0),
    fixed: "Fixed " + (c.fixed || 0),
    all: "All " + (c.cases || 0),
  };
  document.querySelectorAll(".filters button").forEach((button) => {
    button.textContent = labels[button.dataset.filter] || button.textContent;
  });
}

async function load() {
  const latest = await fetch("data/latest.json", { cache: "no-store" }).then((res) => res.json());
  const c = counts(latest);
  const headline = document.getElementById("headline");
  if (!latest.published_at) headline.textContent = "No check yet";
  else if (!c.fail) headline.textContent = "All " + (c.cases || 0) + " passed";
  else {
    const env = latest.environment_label ? latest.environment_label + " · " : "";
    headline.textContent = env + c.fail + " of " + (c.cases || 0) + " failed";
  }
  document.querySelector(".mark").style.background = c.fail ? "var(--bad)" : "var(--good)";
  renderScore(latest);
  labelButtons(latest);
  let filter = c.fail ? "failed" : "all";
  if (c.regression) filter = "broke";
  const search = document.getElementById("search");
  const draw = () => renderList(latest, filter, search.value.trim().toLowerCase());
  draw();
  document.querySelectorAll(".filters button").forEach((button) => {
    button.classList.toggle("on", button.dataset.filter === filter);
    button.addEventListener("click", () => {
      filter = button.dataset.filter;
      document.querySelectorAll(".filters button").forEach((other) => other.classList.toggle("on", other === button));
      draw();
    });
  });
  search.addEventListener("input", draw);
}

load().catch((err) => {
  document.getElementById("headline").textContent = "Could not load";
  document.getElementById("score").textContent = err.message;
});
