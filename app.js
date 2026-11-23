const FAMILIES = {
  "UAT check a message": "Checking a suspicious message",
  "UAT family protection": "Protecting family",
  "UAT live scam": "A scam in progress",
  "UAT policy and claim": "Policy and claim",
  "UAT something else": "Other requests",
  "Enquiry, nothing lost": "Questions, no money lost",
  "Money or device already gone": "Money or device already gone",
  "Safety, not a scam": "Safety, not a scam",
  "Second turn changes the answer": "The user changes the story",
  "Not a fraud story": "Not a fraud story",
  "Same loss, other languages": "Same story, other languages",
  "Abroad and the helpline": "Calling from abroad",
  "Claim": "Claim",
  "Is this message legitimate": "Is this message legitimate",
};

const STATUS = {
  regression: "Was working, now failed",
  still_failing: "Failed",
  new_fail: "Failed",
  fixed: "Fixed",
  pass: "Passed",
};

function when(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function host(server) {
  return String(server || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function clip(text, limit) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= limit) return clean;
  return clean.slice(0, limit - 1).trim() + "…";
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

function titleOf(row) {
  const turn = (row.turns || [])[0] || {};
  return clip(turn.user, 140) || row.id || "Conversation";
}

function plainMissing(line) {
  const text = String(line || "");
  const prefix = "None of these were in the reply: ";
  if (!text.startsWith(prefix)) return text;
  const phrases = text.slice(prefix.length).replace(/\.$/, "").split(";").map((part) => part.trim()).filter(Boolean);
  const readable = phrases.map((part) => part.replace(/"/g, ""));
  const joined = readable.every((part) => part.includes("within a few words"));
  if (joined) return "The reply never connected these ideas: " + readable.join(", ") + ".";
  return "The reply never said any of these: " + readable.join(", ") + ".";
}

function oneLine(row) {
  const missing = row.missing || [];
  if (missing.length) return plainMissing(missing[0]);
  if ((row.fails || []).length) return "The reply did not do what this conversation requires.";
  if (row.status === "fixed") return "This failed last time and passed this time.";
  return "The reply did what this conversation requires.";
}

function counts(data) {
  return data.counts || {};
}

function renderVerdict(data) {
  const box = document.getElementById("verdict");
  const c = counts(data);
  if (!data.published_at || !c.cases) {
    box.hidden = true;
    return;
  }
  let kind = "good";
  let text = "Every conversation in this run passed.";
  if (c.regression > 0) {
    kind = "bad";
    text = c.regression + (c.regression === 1 ? " conversation was passing last time and failed this time. Start there." : " conversations were passing last time and failed this time. Start there.");
  } else if (c.fail > 0) {
    kind = "warn";
    text = "Nothing that was passing broke on this run. The failures below were already failing.";
  }
  box.hidden = false;
  box.className = "verdict " + kind;
  box.textContent = text;
}

function renderStats(data) {
  const c = counts(data);
  const cards = [
    ["pass", c.pass || 0, "Passed"],
    ["fail", c.fail || 0, "Failed"],
    ["broke", c.regression || 0, "Was working, now failed"],
    ["fixed", c.fixed || 0, "Fixed since last run"],
  ];
  const root = document.getElementById("stats");
  root.replaceChildren();
  for (const [kind, value, label] of cards) {
    const card = el("div", "stat " + kind);
    card.append(el("b", "", String(value)));
    card.append(el("span", "", label));
    root.append(card);
  }
}

function renderHistory(history) {
  const root = document.getElementById("history");
  root.replaceChildren();
  const rows = Array.isArray(history) ? history.slice(-8) : [];
  if (rows.length < 2) return;
  root.append(el("div", "", "Earlier published runs"));
  const line = el("div", "runs");
  for (const row of rows) {
    line.append(el("span", "", when(row.published_at) + " · " + (row.pass || 0) + " passed, " + (row.fail || 0) + " failed"));
  }
  root.append(line);
}

function isNotice(text) {
  const reply = String(text || "");
  return reply.includes("I'm CyberCare+") || reply.includes("accept no liability") || reply.includes("This chat is saved securely");
}

function renderCase(row, open) {
  const details = document.createElement("details");
  details.className = "case";
  details.open = open;
  const summary = document.createElement("summary");
  summary.append(el("span", "title", titleOf(row)));
  summary.append(el("span", "code", row.id || ""));
  summary.append(el("span", "tag " + (row.passed ? "pass" : "fail"), STATUS[row.status] || "Checked"));
  summary.append(el("p", "gap", oneLine(row)));
  details.append(summary);

  if (row.expected) {
    const block = el("div", "block");
    block.append(el("strong", "", "What the bot should have done"));
    block.append(el("p", "", row.expected));
    details.append(block);
  }
  const missing = (row.missing || []).map(plainMissing);
  if (missing.length) {
    const block = el("div", "block");
    block.append(el("strong", "", "What the reply left out"));
    const list = document.createElement("ul");
    for (const line of missing) list.append(el("li", "", line));
    block.append(list);
    details.append(block);
  }
  if (row.basis) {
    const block = el("div", "block quiet");
    block.append(el("strong", "", "How this was decided"));
    block.append(el("p", "", row.basis));
    details.append(block);
  }
  for (const turn of row.turns || []) {
    const block = el("div", "turn");
    block.append(el("div", "who", "Person"));
    block.append(el("div", "bubble", turn.user || ""));
    const replies = turn.replies || [];
    const answer = replies.filter((reply) => !isNotice(reply));
    const notice = replies.length - answer.length;
    block.append(el("div", "who", "Bot"));
    block.append(el("div", "bubble", answer.join("\n\n") || "No reply"));
    if (notice) block.append(el("div", "who", "The usual CyberCare+ notice was also sent."));
    details.append(block);
  }
  return details;
}

function matches(row, filter, query) {
  if (filter === "failed" && row.passed) return false;
  if (filter === "broke" && row.status !== "regression") return false;
  if (filter === "fixed" && row.status !== "fixed") return false;
  if (!query) return true;
  const haystack = [
    row.id,
    row.family,
    row.expected,
    titleOf(row),
    ...(row.missing || []),
  ].join(" ").toLowerCase();
  return haystack.includes(query);
}

function renderList(data, filter, query) {
  const root = document.getElementById("list");
  const hint = document.getElementById("hint");
  root.replaceChildren();
  if (!data.published_at) {
    hint.textContent = "";
    root.append(el("p", "empty", "No check has been published yet."));
    return;
  }
  const rows = (data.results || []).filter((row) => matches(row, filter, query));
  const total = (data.results || []).length;
  const shown = rows.length;
  if (filter === "failed") hint.textContent = "Showing the conversations that failed. Open one to read what the bot should have done and what it actually said.";
  else if (filter === "broke") hint.textContent = "These passed on the previous run and failed on this one. An existing conversation changed.";
  else if (filter === "fixed") hint.textContent = "These failed on the previous run and passed on this one.";
  else hint.textContent = "Every conversation in this run, passed and failed.";
  if (query) hint.textContent = shown + " of " + total + " conversations match.";
  if (!rows.length) {
    root.append(el("p", "empty", query ? "No conversation matches that search." : "Nothing in this view."));
    return;
  }
  const groups = new Map();
  for (const row of rows) {
    const name = familyName(row.family);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(row);
  }
  const openFailures = rows.filter((row) => !row.passed).length <= 8;
  for (const [name, cases] of groups) {
    const group = el("section", "group");
    group.append(el("h2", "", name + " · " + cases.length));
    for (const row of cases) group.append(renderCase(row, openFailures && !row.passed));
    root.append(group);
  }
}

function labelButtons(data) {
  const c = counts(data);
  const labels = {
    failed: "Failed (" + (c.fail || 0) + ")",
    broke: "Was working, now failed (" + (c.regression || 0) + ")",
    fixed: "Fixed (" + (c.fixed || 0) + ")",
    all: "All conversations (" + (c.cases || 0) + ")",
  };
  document.querySelectorAll(".filters button").forEach((button) => {
    button.textContent = labels[button.dataset.filter] || button.textContent;
  });
}

async function load() {
  const latest = await fetch("data/latest.json", { cache: "no-store" }).then((res) => res.json());
  let history = [];
  try {
    history = await fetch("data/history.json", { cache: "no-store" }).then((res) => res.json());
  } catch (err) {
    history = [];
  }
  const c = counts(latest);
  const headline = document.getElementById("headline");
  const meta = document.getElementById("meta");
  if (!latest.published_at) {
    headline.textContent = "No check published yet";
    meta.textContent = "A run will appear here after it is published.";
  } else if (!(c.fail || 0)) {
    headline.textContent = "All " + (c.cases || 0) + " conversations passed";
    meta.textContent = [latest.label, when(latest.published_at), host(latest.server)].filter(Boolean).join(" · ");
  } else {
    headline.textContent = (c.fail || 0) + " of " + (c.cases || 0) + " conversations failed";
    meta.textContent = [latest.label, when(latest.published_at), host(latest.server)].filter(Boolean).join(" · ");
  }
  renderVerdict(latest);
  renderStats(latest);
  renderHistory(history);
  labelButtons(latest);
  let filter = "all";
  if (c.regression) filter = "broke";
  else if (c.fail) filter = "failed";
  const search = document.getElementById("search");
  const draw = () => renderList(latest, filter, search.value.trim().toLowerCase());
  draw();
  document.querySelectorAll(".filters button").forEach((button) => {
    button.classList.toggle("on", button.dataset.filter === filter);
    button.addEventListener("click", () => {
      filter = button.dataset.filter;
      document.querySelectorAll(".filters button").forEach((other) => {
        other.classList.toggle("on", other === button);
      });
      draw();
    });
  });
  search.addEventListener("input", draw);
}

load().catch((err) => {
  document.getElementById("headline").textContent = "The latest check could not be loaded";
  document.getElementById("meta").textContent = err.message;
});
