// 사이트(app-core.js + app.js)의 구원자 아르카나 로직을 node 로 실제 실행해 최종 PVE/대체 슬롯을 JSON 으로 출력한다.
// extract_savior_reco.py 의 포팅 결과와 비교하는 교차검증용 (tests/test_savior.py 가 node 가 있을 때만 사용).
// 사용: node tools/site_logic_check.js <사이트사본폴더>  -> stdout: {id: {kind, pve:[{name,note}|null], alt:[...], pveChoices:[[이름..]], altChoices:[[..]]}}
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const dir = process.argv[2];
const read = (n) => fs.readFileSync(path.join(dir, n), "utf8").replace(/^﻿/, "");

// 어떤 속성/호출/반복도 받아 주는 만능 스텁 (사이트 최상단의 DOM 코드가 죽지 않게)
function stub() {
  const f = function () {};
  const p = new Proxy(f, {
    get: (t, k) => (k === Symbol.toPrimitive ? () => "" : k === "then" ? undefined : k === Symbol.iterator ? function* () {} : p),
    apply: () => p, construct: () => p, set: () => true, has: () => true,
  });
  return p;
}
let loadCb = null;
const doc = stub();
const sandboxDoc = new Proxy({}, {
  get: (t, k) => {
    if (k === "createElement") return () => ({ addEventListener: (ev, cb) => { if (ev === "load" && !loadCb) loadCb = cb; }, set src(v) {} });
    if (k === "body") return { appendChild: () => {} };
    return doc[k];
  },
});
const ctx = vm.createContext({
  document: sandboxDoc, window: stub(), localStorage: stub(), navigator: stub(), location: { hash: "", search: "", href: "" },
  console: { log() {}, error() {}, warn() {} }, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, requestAnimationFrame: () => 0,
  fetch: () => new Promise(() => {}), URL, URLSearchParams, Intl, structuredClone,
  IntersectionObserver: function () { return stub(); }, MutationObserver: function () { return stub(); }, matchMedia: () => stub(),
  history: stub(), addEventListener() {}, CSS: stub(), Image: function () { return stub(); },
});
ctx.window = ctx;
try { vm.runInContext(read("app-core.js"), ctx, { filename: "app-core.js" }); } catch (e) { process.stderr.write("app-core.js 실행 중 예외(무시): " + e.message + "\n"); }
vm.runInContext(`hydrateArcanaRecommendationLibrary(${read("arcanas.json")});`, ctx);
try { vm.runInContext(read("app.js"), ctx, { filename: "app.js" }); } catch (e) { process.stderr.write("app.js 예외: " + e.message + "\n"); }
if (loadCb) { try { loadCb.call(ctx); } catch (e) { process.stderr.write("app.js load 콜백 후반 예외(패치 이후면 무시): " + e.message + "\n"); } }

const out = vm.runInContext(`(() => {
  const res = {};
  const names = (slot) => (slot ? resolveArcanaChoices(slot.name).map((c) => c.name) : []);
  const orig = createSaviorArcanaSettingMarkup;
  for (const s of SAVIORS) {
    let cap = null;
    createSaviorArcanaSettingMarkup = (sv, pve, alt) => { cap = { pve, alt }; return ""; };
    try { createDetailMarkup(s); } catch (e) { if (!cap) { res[s.id] = { error: String(e) }; continue; } }
    const defender = s.className === "디펜더" && s.id !== "white-pearl-luna";
    const statMarkup = createJourneyStatPriorityMarkup(s);
    res[s.id] = { kind: defender ? "defender" : "normal", pve: cap.pve, alt: cap.alt,
      pveChoices: (cap.pve || []).map(names), altChoices: (cap.alt || []).map(names),
      stat: getJourneyStatPriorityRows(s), equipPve: getBuild(s).equipment.pve,
      statNotes: [...statMarkup.matchAll(/class="journey-stat-priority-note"[^>]*>([^<]*)</g)].map((m) => m[1]) };
  }
  createSaviorArcanaSettingMarkup = orig;
  return JSON.stringify(res);
})()`, ctx);
process.stdout.write(out);
