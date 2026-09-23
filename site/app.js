// FPL Squad Recommendation — MVP static site (Phase E step 14,
// .github/context/subsystem3-close-and-mvp-site.md).
//
// No build step, no framework: fetches the two committed JSON artefacts
// straight from the deployed Pages bundle and renders them. `latest.json`
// is a small pointer (mirroring `models/active.json`) at the actual
// `squad.json` for the most recent `fpl optimise` run, since a static site
// has no directory listing to discover it itself.

const POSITION_ORDER = ["GK", "DEF", "MID", "FWD"];
const POSITION_LABELS = { GK: "Goalkeeper", DEF: "Defenders", MID: "Midfielders", FWD: "Forwards" };

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${path} responded ${response.status}`);
  }
  return response.json();
}

function playerLabel(player) {
  return `${player.name} (${player.team_name})`;
}

function renderPlayerRow(player, { captain, viceCaptain }) {
  const row = document.createElement("li");
  row.className = "player-row";

  const label = document.createElement("span");
  label.textContent = playerLabel(player);
  if (captain && player.player_id === captain.player_id) {
    label.append(badge("C"));
  } else if (viceCaptain && player.player_id === viceCaptain.player_id) {
    label.append(badge("VC"));
  }

  const points = document.createElement("span");
  points.className = "points";
  points.textContent = `${player.predicted_points.toFixed(1)} pts · £${player.price.toFixed(1)}m`;

  row.append(label, points);
  return row;
}

function badge(text) {
  const span = document.createElement("span");
  span.className = "badge";
  span.textContent = text;
  return span;
}

function renderSummary(squad) {
  const container = document.createElement("div");
  container.className = "summary";

  const stats = [
    ["Predicted points", squad.total_predicted_points.toFixed(1)],
    ["Squad value", `£${squad.total_price.toFixed(1)}m`],
    ["Budget", `£${squad.budget.toFixed(1)}m`],
  ];
  for (const [label, value] of stats) {
    const stat = document.createElement("div");
    stat.className = "stat";
    stat.innerHTML = `<span class="value">${value}</span><span class="label">${label}</span>`;
    container.append(stat);
  }
  return container;
}

function renderStartingXi(squad) {
  const section = document.createElement("section");
  section.innerHTML = "<h2>Starting XI</h2>";

  for (const position of POSITION_ORDER) {
    const players = squad.starting_xi.filter((player) => player.position === position);
    if (players.length === 0) continue;

    const group = document.createElement("div");
    group.className = "position-group";
    group.innerHTML = `<h3>${POSITION_LABELS[position]}</h3>`;

    const list = document.createElement("ul");
    list.className = "player-list";
    for (const player of players) {
      list.append(
        renderPlayerRow(player, { captain: squad.captain, viceCaptain: squad.vice_captain })
      );
    }
    group.append(list);
    section.append(group);
  }
  return section;
}

function renderBench(squad) {
  const section = document.createElement("section");
  section.innerHTML = "<h2>Bench</h2>";

  const list = document.createElement("ul");
  list.className = "player-list";
  for (const player of squad.bench) {
    list.append(renderPlayerRow(player, {}));
  }
  section.append(list);
  return section;
}

function renderSquad(squad) {
  const meta = document.getElementById("meta");
  meta.textContent = `Season ${squad.season} · as of ${squad.as_of}`;

  const app = document.getElementById("app");
  app.replaceChildren(renderSummary(squad), renderStartingXi(squad), renderBench(squad));
}

function renderError(message) {
  const status = document.getElementById("status");
  if (status) {
    status.textContent = message;
  } else {
    const app = document.getElementById("app");
    app.replaceChildren(Object.assign(document.createElement("p"), { textContent: message }));
  }
}

async function main() {
  let pointer;
  try {
    pointer = await fetchJson("data/predictions/latest.json");
  } catch (error) {
    renderError("No recommendation has been published yet — check back after the next run.");
    return;
  }

  try {
    const squad = await fetchJson(`data/${pointer.path}`);
    renderSquad(squad);
  } catch (error) {
    renderError("The published recommendation could not be loaded.");
  }
}

main();
