const CHECKS = [
  ["MUST_MATCH_MISSING", "The reply never said the step this journey requires."],
  ["FORBIDDEN_CONTENT_PRESENT", "The reply said something this journey must not say."],
  ["STALE_CONTEXT_IN_REPLY", "A later reply ignored what the user had just said."],
  ["LOST_EARLIER_CONTEXT", "The last reply dropped a fact from earlier in the chat."],
  ["RE_ASKED_ANSWERED_SLOT", "The user already did or answered this, and the bot asked again."],
  ["FALSE_ALL_CLEAR", "The bot closed the case while required steps were still open."],
  ["CLOSED_WITH_", "The bot said the case was finished before the steps were done."],
  ["NO_TEL_CTA", "It said to call and did not put a call button on the message."],
  ["LANG_NOT_", "The reply came back in the wrong script."],
  ["ENGLISH_SERVED_TO_", "An English reply was sent to someone who wrote in another language."],
  ["ENGLISH_REPLY_NOT_ENGLISH", "An English user was answered in Hinglish or another language."],
  ["NO_REPLY_AT_ALL", "The bot sent nothing."],
  ["DROPPED_TURN", "One turn in the conversation got no reply."],
  ["VERBATIM_REPEAT", "The bot repeated a message after the user had moved on."],
  ["HARNESS_ERROR", "The run itself failed before a verdict. This is not a product failure."],
  ["NO_URGENT_ACTION", "An emergency got no immediate step."],
  ["HANDOFF_NOT_HONOURED", "The user asked for a person and was not handed over."],
  ["SPURIOUS_HANDOFF", "The bot offered a human when this journey should stay in chat."],
];

const STATUS = {
  regression: "Broke since last run",
  still_failing: "Still failing",
  new_fail: "Failing, first time measured",
  fixed: "Fixed since last run",
  pass: "Passing",
};

function explain(code) {
  const text = String(code || "");
  for (const [prefix, meaning] of CHECKS) {
    if (text.startsWith(prefix)) return meaning;
  }
  return "Open the conversation below and compare it with the check name.";
}

function when(iso) {
  if (!iso) return "No run published yet.";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function renderStats(data) {
  const counts = data.counts || {};
  const cards = [
    ["fail", counts.fail || 0, "Failing now"],
    ["broke", counts.regression || 0, "Broke since last run"],
    ["still", counts.still_failing || 0, "Already failing"],
    ["fixed", counts.fixed || 0, "Fixed since last run"],
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
  const rows = Array.isArray(history) ? history : [];
  if (!rows.length) {
    root.textContent = "History appears after the second published run.";
    return;
  }
  root.textContent = "Failing cases on the last " + rows.length + " published runs.";
  const bars = el("div", "bars");
  const max = Math.max(1, ...rows.map((row) => row.fail || 0));
  for (const row of rows) {
    const bar = document.createElement("i");
    bar.style.height = Math.max(4, Math.round(((row.fail || 0) / max) * 36)) + "px";
    bar.title = when(row.published_at) + ": " + (row.fail || 0) + " failing, " + (row.regression || 0) + " newly broken";
    bars.append(bar);
  }
  root.append(bars);
}

function renderCase(row) {
  const details = document.createElement("details");
  details.className = "case";
  const summary = document.createElement("summary");
  summary.append(el("span", "id", row.id));
  summary.append(el("span", "tag " + row.status, STATUS[row.status] || row.status));
  details.append(summary);
  if (row.expected) {
    const expected = el("p", "expected");
    expected.append(el("strong", "", "Expected behaviour. "));
    expected.append(document.createTextNode(row.expected));
    details.append(expected);
  }
  const missing = row.missing || [];
  if (missing.length) {
    const gap = el("div", "missing");
    gap.append(el("strong", "", "Missing in the bot response"));
    const list = document.createElement("ul");
    for (const line of missing) list.append(el("li", "", line));
    gap.append(list);
    details.append(gap);
  } else if ((row.fails || []).length) {
    const why = el("p", "why");
    why.append(el("strong", "", explain(row.fails[0]) + " "));
    why.append(document.createTextNode(row.fails.join(", ")));
    details.append(why);
  }
  if (row.basis) {
    const basis = el("p", "basis");
    basis.append(el("strong", "", "Basis of the check. "));
    basis.append(document.createTextNode(row.basis));
    details.append(basis);
  }
  for (const turn of row.turns || []) {
    const block = el("div", "turn");
    block.append(el("div", "who", "User"));
    block.append(el("div", "", turn.user || ""));
    block.append(el("div", "who", "Bot"));
    block.append(el("div", "", (turn.replies || []).join("\n\n") || "(no reply)"));
    details.append(block);
  }
  return details;
}

function visible(row, filter) {
  if (filter === "all") return true;
  if (filter === "broke") return row.status === "regression";
  if (filter === "still") return row.status === "still_failing" || row.status === "new_fail";
  if (filter === "fixed") return row.status === "fixed";
  return true;
}

function renderList(data, filter) {
  const root = document.getElementById("list");
  root.replaceChildren();
  const rows = (data.results || []).filter((row) => visible(row, filter));
  if (!data.published_at) {
    root.append(el("p", "empty", "No run published yet. After a deploy check, run make eval-conv-publish from the product repo."));
    return;
  }
  if (!rows.length) {
    root.append(el("p", "empty", "Nothing in this view."));
    return;
  }
  const groups = new Map();
  for (const row of rows) {
    const name = row.family || "Other";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(row);
  }
  for (const [name, cases] of groups) {
    const group = el("section", "group");
    group.append(el("h2", "", name + "  " + cases.length));
    for (const row of cases) group.append(renderCase(row));
    root.append(group);
  }
}

async function load() {
  const latest = await fetch("data/latest.json", { cache: "no-store" }).then((res) => res.json());
  let history = [];
  try {
    history = await fetch("data/history.json", { cache: "no-store" }).then((res) => res.json());
  } catch (err) {
    history = [];
  }
  const meta = document.getElementById("meta");
  if (!latest.published_at) {
    meta.textContent = "No run published yet.";
  } else {
    meta.textContent = (latest.label ? latest.label + "  ·  " : "")
      + when(latest.published_at) + (latest.server ? "  ·  " + latest.server : "")
      + "  ·  " + (latest.counts?.pass || 0) + " passing, "
      + (latest.counts?.fail || 0) + " failing, of " + (latest.counts?.cases || 0);
  }
  renderStats(latest);
  renderHistory(history);
  let filter = "broke";
  if (!(latest.counts && latest.counts.regression)) filter = "still";
  renderList(latest, filter);
  document.querySelectorAll(".filters button").forEach((button) => {
    button.classList.toggle("on", button.dataset.filter === filter);
    button.addEventListener("click", () => {
      filter = button.dataset.filter;
      document.querySelectorAll(".filters button").forEach((other) => {
        other.classList.toggle("on", other === button);
      });
      renderList(latest, filter);
    });
  });
}

load().catch((err) => {
  document.getElementById("meta").textContent = "Could not load data/latest.json. " + err.message;
});
