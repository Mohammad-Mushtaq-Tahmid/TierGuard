(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const EXAMPLES = ["帮我省点钱，停掉用得少的订阅", "有什么低风险理财可以买", "买一百块货币基金", "我买了哪些理财", "给我妈转五百", "转一千二给王小明", "上个月花了多少，有没有异常", "给李华转3万", "卡丢了先冻上", "我订阅了哪些服务", "把爱奇艺会员停了", "把每个月自动扣的视频会员停了", "我还剩多少钱", "Send 300 to mom"];
  const WARN = { FIRST_TIME_PAYEE: "First transfer to this payee." };
  const TIER = { green: "AUTO", yellow: "CONFIRM", red: "STRONG AUTH", black: "BLOCKED" };
  let sid = null, activeCard = null, busy = false, planEl = null, planId = null, planStage = null;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;      // text only, never HTML: bank data is untrusted
    return e;
  }
  async function api(path, body) {
    const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sid, ...body }) } : {};
    const url = body ? path : path + (sid ? "?sid=" + encodeURIComponent(sid) : "");
    const r = await fetch(url, opt);
    return r.json();
  }
  function scrollDown(node) { node.scrollTop = node.scrollHeight; }

  function renderState(st, mode) {
    if (mode) $("#mode").textContent = { record: "live model (answers cached)", replay: "offline replay", test: "test model" }[mode] || mode;
    const a = $("#acct"); a.replaceChildren();
    const chip = (label, value) => { const c = el("span", "chip-stat", label + " "); c.appendChild(el("b", "", value)); a.appendChild(c); };
    chip("Balance", st.balance_cny + " CNY");
    chip("Today's transfers", st.daily_transfer_cny + " / " + st.yellow_limit_cny);
    st.cards.forEach((c) => chip("Card " + c.last4_masked, c.status));
    const active = st.subscriptions.filter((s) => s.status === "active").length;
    chip("Subscriptions", active + " active");
    if (st.locked) chip("Session", "LOCKED");
    const ch = $("#chain");
    ch.className = st.audit_ok ? "ok" : "bad";
    ch.replaceChildren(el("b", "", st.audit_ok ? "Audit chain valid" : "AUDIT CHAIN BROKEN"), document.createTextNode(" · " + st.audit_len + " entries, hash-chained"));
  }
  function renderEvents(evs) {
    const box = $("#events");
    evs.forEach((e) => {
      const row = el("div", "ev " + e.tone);
      row.appendChild(el("span", "n", String(e.seq)));
      row.appendChild(el("span", "", e.text));
      box.appendChild(row);
    });
    scrollDown(box);
  }
  function bubble(role, text, extra) {
    const m = el("div", "msg " + role + (extra ? " " + extra : ""), text);
    $("#log").appendChild(m); scrollDown($("#log")); return m;
  }
  function closeCard() {
    if (!activeCard) return;
    activeCard.classList.add("done");
    activeCard.querySelectorAll("button,input").forEach((x) => (x.disabled = true));
    activeCard = null;
  }
  const STATE_LABEL = { pending: "pending", running: "running", awaiting_confirmation: "needs your OK", succeeded: "done",
                        failed: "failed", skipped: "skipped", cancelled: "not run" };
  const DECISION = { preview: ["Start plan", "Cancel"], offer: ["Yes, invest", "No thanks"], undo_offer: ["Undo (resume)", "Keep as is"] };
  function renderPlan(p) {
    if (!p) return;
    planStage = p.stage;
    if (!planEl || planId !== p.id) { planEl = el("div", "card plancard"); planId = p.id; }
    planEl.replaceChildren();
    const head = el("div", "card-head");
    head.appendChild(el("span", "", p.title));
    head.appendChild(el("span", "tier", p.stage.replace("_", " ")));
    planEl.appendChild(head);
    const list = el("div", "steps");
    p.steps.forEach((st) => {
      const row = el("div", "srow");
      row.appendChild(el("span", "sdot " + st.state));
      const name = el("span", "sname", st.id + "  " + st.title);
      if (st.deps && st.deps.length) name.appendChild(el("small", "", "  after " + st.deps.join(", ")));
      row.appendChild(name);
      if (st.tier) row.appendChild(el("span", "tier " + st.tier, st.tier));
      row.appendChild(el("span", "sstate", STATE_LABEL[st.state] || st.state));
      list.appendChild(row);
    });
    planEl.appendChild(list);
    if (DECISION[p.stage]) {
      const acts = el("div", "actions");
      const yes = el("button", "", DECISION[p.stage][0]), no = el("button", "secondary", DECISION[p.stage][1]);
      yes.onclick = () => send({ action: "confirm" }, ""); no.onclick = () => send({ action: "cancel" }, "");
      acts.append(yes, no); planEl.appendChild(acts);
    }
    $("#log").appendChild(planEl);                 // keep the plan board just above the newest card
    scrollDown($("#log"));
  }
  function renderCard(card) {
    const w = el("div", "card tier-" + card.tier);
    const head = el("div", "card-head");
    head.appendChild(el("span", "", card.summary.action || card.tool));
    head.appendChild(el("span", "tier " + card.tier, TIER[card.tier] || card.tier));
    w.appendChild(head);
    const rows = el("div", "rows");
    Object.entries(card.summary).forEach(([k, v]) => {
      if (k === "action" || v === "" || v === null) return;
      rows.appendChild(el("span", "k", k.replace(/_/g, " ")));
      rows.appendChild(el("span", "", String(v)));
    });
    w.appendChild(rows);
    (card.warnings || []).forEach((c) => w.appendChild(el("div", "warn", WARN[c] || c)));
    if (card.untrusted_values && card.untrusted_values.length)
      w.appendChild(el("div", "risk", "These values came from untrusted text: " + card.untrusted_values.join(", ")));
    if (card.risk_warning) w.appendChild(el("div", "risk", card.risk_warning));
    w.appendChild(el("div", "hash", "bound to action hash " + card.action_hash + " · expires in " + card.expires_in_s + "s"));
    const acts = el("div", "actions");
    const ok = el("button", "", card.needs_step_up ? "Confirm (SMS code needed)" : "Confirm");
    const no = el("button", "secondary", "Cancel");
    ok.onclick = () => send({ action: "confirm" }, ""); no.onclick = () => send({ action: "cancel" }, "");
    acts.append(ok, no);
    if (planStage === "executing") { const sk = el("button", "secondary", "Skip this step"); sk.onclick = () => send({ action: "skip" }, ""); acts.append(sk); }
    w.appendChild(acts);
    $("#log").appendChild(w); scrollDown($("#log"));
    activeCard = w;
  }
  function renderOtp(sms) {
    if (!activeCard) return;
    let box = activeCard.querySelector(".otp");
    if (!box) {
      box = el("div", "otp");
      box.appendChild(el("div", "", "Strong authentication: enter the 6-digit SMS code"));
      const inp = el("input"); inp.maxLength = 6; inp.inputMode = "numeric"; inp.placeholder = "••••••";
      const go = el("button", "", "Verify");
      const submit = () => { if (/^\d{6}$/.test(inp.value)) { const v = inp.value; inp.value = ""; send({ message: v }, "(code entered)"); } };
      go.onclick = submit; inp.onkeydown = (e) => { if (e.key === "Enter") submit(); };
      const row = el("div", "actions"); row.append(inp, go); box.appendChild(row);
      box.appendChild(el("div", "sms")); activeCard.appendChild(box);
    }
    const s = box.querySelector(".sms");
    s.replaceChildren();
    if (sms) {
      s.appendChild(document.createTextNode("Simulated SMS: your code is "));
      s.appendChild(el("b", "", sms));
      s.appendChild(document.createTextNode(" (click to fill)"));
      s.onclick = () => { box.querySelector("input").value = sms; };
    }
  }
  async function send(payload, shown) {
    if (busy) return;
    busy = true; $("#send").disabled = true;
    if (shown === undefined) shown = payload.message;
    if (shown) bubble("user", shown);
    const wait = bubble("bot", "…", "typing");
    try {
      const d = await api("/api/chat", payload);
      sid = d.sid || sid; wait.remove();
      if (d.error) { bubble("bot", "Error: " + d.error, "err"); return; }
      const r = d.reply;
      bubble("bot", r.message, r.kind === "error" ? "err" : "");
      if (d.plan) renderPlan(d.plan);
      if (r.card) { closeCard(); renderCard(r.card); }
      if (r.awaiting_otp) renderOtp(d.sms); else if (!r.card) closeCard();
      renderState(d.state, d.mode); renderEvents(d.events);
    } catch (e) {
      wait.remove(); bubble("bot", "Could not reach the server.", "err");
    } finally { busy = false; $("#send").disabled = false; $("#msg").focus(); }
  }
  function submitInput() {
    const i = $("#msg"), v = i.value.trim();
    if (!v) return; i.value = ""; send({ message: v });
  }
  async function start(resetFirst) {
    const d = resetFirst ? await api("/api/reset", {}) : await api("/api/state");
    sid = d.sid; activeCard = null; planEl = null; planId = null; planStage = null;
    $("#log").replaceChildren(); $("#events").replaceChildren();
    bubble("bot", "Hi! I'm TierGuard. I can check balances, analyse spending, make transfers and manage cards. Risky actions always need your confirmation, and large or sensitive ones need an SMS code.");
    renderState(d.state, d.mode); renderEvents(d.events || []);
  }
  EXAMPLES.forEach((t) => { const b = el("button", "", t); b.onclick = () => send({ message: t }); $("#chips").appendChild(b); });
  $("#send").onclick = submitInput;
  $("#msg").onkeydown = (e) => { if (e.key === "Enter") submitInput(); };
  $("#reset").onclick = () => start(true);
  $("#human").onclick = () => send({ action: "human" }, "Talk to a human");
  start(false);
})();