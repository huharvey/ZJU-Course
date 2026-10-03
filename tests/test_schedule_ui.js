const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('index.html', 'utf8');
for (const script of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
  new vm.Script(script[1]);
}
const stateCode = html.slice(html.indexOf('function newScheduleState()'), html.indexOf('let SCHEDULE_COURSES'));
const loadCode = html.slice(html.indexOf('function invalidateScheduleWeeks()'), html.indexOf('function renderWeekSchedule()'));
const todoCode = html.slice(html.indexOf('async function reloadScheduleTodo()'), html.indexOf('async function addManualTask()'));

function harness(api) {
  const elements = new Map();
  let skeletonCalls = 0;
  const ctx = vm.createContext({
    Map, Set, Date, URLSearchParams, api,
    $: (key) => {
      if (!elements.has(key)) elements.set(key, { innerHTML: 'existing grid', textContent: '', hidden: false });
      return elements.get(key);
    },
    addDaysISO: (iso, days) => {
      const date = new Date(iso + 'T12:00:00Z');
      date.setUTCDate(date.getUTCDate() + days);
      return date.toISOString().slice(0, 10);
    },
    currentMondayISO: () => '2026-10-12',
    fmtMD: (date) => date,
    skeletonHtml: () => {skeletonCalls++; return 'loading skeleton';},
    emptyBox: (_, text) => text,
    renderWeekSchedule: () => {}, renderScheduleTodo: () => {},
    TODO_CTX: { course_id: 123, name: 'Course' },
  });
  vm.runInContext(stateCode + loadCode + todoCode, ctx);
  return {ctx, elements, skeletonCalls: () => skeletonCalls, run: (code) => vm.runInContext(code, ctx)};
}
function batch(start = '2026-10-12', stale = false) {
  const weeks = Array.from({length: 9}, (_, i) => {
    const date = new Date(start + 'T12:00:00Z');
    date.setUTCDate(date.getUTCDate() + (i - 4) * 7);
    return { week_start: date.toISOString().slice(0, 10), stale, days: [], manual_tasks: [] };
  });
  return {ok: true, ...weeks[4], weeks};
}
function deferred() {
  let resolve;
  const promise = new Promise(r => {resolve = r;});
  return {promise, resolve};
}

async function main() {
  {
    let calls = 0;
    const h = harness(async () => {calls++; return batch();});
    await h.run('loadSchedule()');
    for (const start of ['2026-10-19', '2026-10-05', '2026-10-12']) {
      h.run(`SCHEDULE.weekStart = '${start}'`);
      await h.run('loadSchedule()');
      assert.equal(h.run('SCHEDULE.data.week_start'), start);
    }
    assert.equal(calls, 1, 'cached week switches must make zero additional requests');
    assert.equal(h.skeletonCalls(), 1, 'switches should not show a loading skeleton again');
  }
  {
    const network = deferred();
    const h = harness((url) => url.includes('cache_only') ? Promise.resolve(batch(undefined, true)) : network.promise);
    const loading = h.run('loadSchedule()');
    await new Promise(r => setImmediate(r));
    assert.equal(h.run('SCHEDULE.data.week_start'), '2026-10-12', 'disk cache renders before network completes');
    h.run("SCHEDULE.weekStart = '2026-10-19'");
    await h.run('loadSchedule()');
    network.resolve(batch());
    await loading;
    assert.equal(h.run('SCHEDULE.data.week_start'), '2026-10-19', 'late response must preserve the selected week');
    assert.equal(h.run('SCHEDULE.loading'), false);
  }
  {
    let fail = false;
    const h = harness(async () => fail ? {ok: false, error: 'offline'} : batch());
    await h.run('loadSchedule()');
    h.elements.get('#weekGrid').innerHTML = 'saved course grid';
    fail = true;
    await h.run('loadSchedule(true)');
    assert.equal(h.elements.get('#weekGrid').innerHTML, 'saved course grid', 'refresh must not erase old grid');
    assert.equal(h.run('SCHEDULE.data.week_start'), '2026-10-12');
    assert.match(h.run('SCHEDULE.notice'), /继续显示/);
    h.run("SCHEDULE.weekStart = '2027-04-19'");
    await h.run('loadSchedule()');
    assert.equal(h.run('SCHEDULE.weekStart'), '2026-10-12', 'failure must not relabel old week as requested week');
  }
  {
    const request = deferred();
    const h = harness(() => request.promise);
    const loading = h.run('loadSchedule()');
    h.run('SCHEDULE = newScheduleState()');
    request.resolve(batch());
    await loading;
    assert.equal(h.run('SCHEDULE.data'), null, 'response after logout must be discarded');
  }
  {
    let calls = 0;
    const h = harness(async () => {calls++; return batch(undefined, true);});
    await h.run('loadSchedule(false, true)');
    assert.equal(calls, 1, 'local adjustment refresh must not wait for network even with old cache');
    h.run('invalidateScheduleWeeks()');
    await h.run('loadSchedule(false, true)');
    assert.equal(calls, 2, 'local adjustment must rebuild the batch instead of reusing rendered weeks');
  }
  {
    const request = deferred();
    const h = harness(() => request.promise);
    const loading = h.run('loadSchedule()');
    h.run('invalidateScheduleWeeks()');
    request.resolve(batch());
    await loading;
    assert.equal(h.run('SCHEDULE.weeks.size'), 0, 'response predating a local edit must be discarded');
  }
  {
    const h = harness(async () => ({ok: true, course_id: 123, course_name: 'Course', manual_tasks: [{id: 'new'}]}));
    h.run(`
      for (const start of ['2026-10-12', '2026-10-19']) {
        SCHEDULE.weeks.set(start, {week_start: start, manual_tasks: [], days: [{blocks: [{courses: [{course_id: 123}]}]}]});
      }
      SCHEDULE.data = SCHEDULE.weeks.get('2026-10-12');
    `);
    await h.run('reloadScheduleTodo()');
    assert.equal(h.run("SCHEDULE.weeks.get('2026-10-19').manual_tasks[0].id"), 'new');
    assert.equal(h.run("SCHEDULE.weeks.get('2026-10-19').days[0].blocks[0].courses[0].manual_task_todo"), 1);
  }
  console.log('Passed 7 UI scenarios; all inline scripts compile.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
