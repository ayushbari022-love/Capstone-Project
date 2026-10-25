/* EventRegister front-end helpers */
document.addEventListener("DOMContentLoaded", () => {

  // Live countdown on the landing-page ticket
  document.querySelectorAll("[data-countdown]").forEach(el => {
    const target = new Date(el.dataset.countdown).getTime();
    const cells = el.querySelectorAll("[data-u]");
    const tick = () => {
      const s = Math.max(0, Math.floor((target - Date.now()) / 1000));
      const v = {d: Math.floor(s / 86400), h: Math.floor(s % 86400 / 3600), m: Math.floor(s % 3600 / 60), s: s % 60};
      cells.forEach(c => c.textContent = String(v[c.dataset.u]).padStart(2, "0"));
    };
    tick();
    setInterval(tick, 1000);
  });

  // Fill progress bars (seats / XP)
  requestAnimationFrame(() =>
    document.querySelectorAll(".bar span[data-w]").forEach(b => b.style.width = b.dataset.w + "%"));

  // Suggestions served by the Trie on the backend (/api/search)
  const search = document.getElementById("search");
  const box = document.getElementById("suggest");
  if (search && box) {
    let timer;
    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = search.value.trim();
        box.replaceChildren();
        if (!q) { box.hidden = true; return; }
        const items = await (await fetch("/api/search?q=" + encodeURIComponent(q))).json();
        items.forEach(title => {
          const b = document.createElement("button");
          b.type = "button";
          b.textContent = title;
          b.onclick = () => { search.value = title; search.form.submit(); };
          box.appendChild(b);
        });
        box.hidden = items.length === 0;
      }, 150);
    });
    document.addEventListener("click", e => {
      if (!box.contains(e.target) && e.target !== search) box.hidden = true;
    });
  }

  // Category chips filter the ticket grid without reloading the page
  const chips = document.querySelectorAll(".filter-chip");
  chips.forEach(chip => chip.addEventListener("click", () => {
    chips.forEach(c => c.classList.remove("on"));
    chip.classList.add("on");
    const f = chip.dataset.filter;
    document.querySelectorAll(".ticket[data-cat]").forEach(t => { t.hidden = !(f === "all" || t.dataset.cat === f); });
  }));

  // Admin event form: show/hide the team-size and payment/QR fields
  const teamToggle = document.getElementById("is_team_event");
  const teamBox = document.getElementById("team-box");
  if (teamToggle && teamBox) {
    const sync = () => { teamBox.hidden = !teamToggle.checked; };
    teamToggle.addEventListener("change", sync); sync();
  }
  const payToggle = document.getElementById("requires_payment");
  const payBox = document.getElementById("pay-box");
  if (payToggle && payBox) {
    const sync = () => { payBox.hidden = !payToggle.checked; };
    payToggle.addEventListener("change", sync); sync();
  }

  // Student registration form: teammate count hint
  const teammates = document.getElementById("teammates");
  if (teammates && teammates.dataset.need) {
    const need = parseInt(teammates.dataset.need, 10);
    const hint = document.getElementById("teammates-hint");
    const update = () => {
      const n = teammates.value.split(/[,\n]+/).map(s => s.trim()).filter(Boolean).length;
      if (hint) hint.textContent = n === need ? `${n} of ${need} teammates - looks good.` : `${n} of ${need} teammates entered.`;
    };
    teammates.addEventListener("input", update); update();
  }
});
