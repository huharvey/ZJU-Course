const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('index.html', 'utf8');
const helpers = html.slice(html.indexOf('const LOCAL_DOWNLOAD_PENDING'), html.indexOf('async function downloadUpload'));
const upload = html.slice(html.indexOf('async function downloadUpload'), html.indexOf('// ---------- 浮窗'));
const exportsCode = html.slice(html.indexOf('async function exportLiveSubtitles'), html.indexOf('// 详情页作业列表'));

function deferred() {
  let resolve;
  const promise = new Promise(r => {resolve = r;});
  return {promise, resolve};
}
function button(kind = 'upload', name = 'slides.pdf') {
  const classes = new Set();
  return {
    dataset:{localKind:kind, localId:'12', localName:name, localCourse:'Course', localCourseId:'42'},
    isConnected:true, disabled:false, textContent:'下载', title:'',
    classList:{toggle:(key, on) => on ? classes.add(key) : classes.delete(key), contains:key => classes.has(key)}
  };
}
function harness(fetchImpl, statusImpl) {
  const btn = button();
  const toasts = [];
  const h = {
    btn, buttons:[btn], toasts,
    file:{kind:'upload', id:'12', name:'slides.pdf', course:'Course', course_id:'42'}
  };
  h.ctx = vm.createContext({
    Map, JSON, URLSearchParams, Promise,
    file:h.file, btn,
    document:{querySelectorAll:() => h.buttons},
    window:{TOKEN:'test-token', addEventListener:() => {}},
    fetch:fetchImpl,
    api:statusImpl || (async () => ({ok:false})),
    esc:String,
    showToast:(...args) => toasts.push(args),
    DETAIL_FILES:{12:'slides.pdf'}, DETAIL_COURSE:{12:'Course'}, DETAIL_COURSE_ID_BY_UPLOAD:{12:42},
    DETAIL_COURSE_ID:42, DETAIL_COURSE_NAME:'Course', DETAIL_LIVES:{12:{page_view_url:'https://example.test/live', title:'录播'}},
    fileExt:name => name.split('.').pop(),
    $:() => ({style:{display:'none'}}), refreshLocalCourseFiles:() => {},
  });
  vm.runInContext(helpers + upload + exportsCode, h.ctx);
  h.run = code => vm.runInContext(code, h.ctx);
  return h;
}
const tick = () => new Promise(r => setImmediate(r));

async function main() {
  {
    const request = deferred();
    let calls = 0;
    const h = harness(() => {calls++; return request.promise;});
    const first = h.run("runLocalDownload(file, '/save', d => d.path)");
    const second = h.run("runLocalDownload(file, '/save', d => d.path)");
    await tick();
    assert.equal(calls, 1, 'concurrent clicks share a single save request');
    assert.equal(h.btn.disabled, true);
    request.resolve({json:async () => ({ok:true, existing:true, revealed:true, path:'C:/Course/slides.pdf'})});
    await Promise.all([first, second]);
    assert.equal(h.btn.disabled, false, 'existing buttons remain clickable for reveal');
    assert.equal(h.btn.textContent, '已下载 · 定位');
    assert.equal(h.btn.classList.contains('downloaded'), true);
    assert.match(h.toasts.at(-1)[0], /已在本地文件夹中定位/);
  }
  {
    let exists = true;
    const h = harness(async () => {}, async () => ({ok:true, files:[{exists, path:exists ? 'C:/slides.pdf' : ''}]}));
    await h.run('refreshDownloadButtons()');
    assert.equal(h.btn.textContent, '已下载 · 定位');
    exists = false;
    await h.run('refreshDownloadButtons()');
    assert.equal(h.btn.textContent, '下载', 'deleted files return to download state');
    assert.equal(h.btn.classList.contains('downloaded'), false);
  }
  {
    const stale = deferred();
    let checks = 0;
    const h = harness(async () => ({json:async () => ({ok:true, path:'C:/slides.pdf'})}),
      () => ++checks === 1 ? stale.promise : Promise.resolve({ok:false}));
    const scanning = h.run('refreshDownloadButtons()');
    await h.run("runLocalDownload(file, '/save', d => d.path)");
    stale.resolve({ok:true, files:[{exists:false}]});
    await scanning;
    assert.equal(h.btn.textContent, '已下载 · 定位', 'stale status must not undo completed state');
  }
  {
    let calls = 0;
    const h = harness(() => {calls++; throw new Error('offline');});
    await h.run("runLocalDownload(file, '/save', d => d.path)");
    await h.run("runLocalDownload(file, '/save', d => d.path)");
    assert.equal(calls, 2, 'synchronous failures must allow retry');
    assert.equal(h.btn.disabled, false);
    assert.equal(h.btn.textContent, '下载');
  }
  {
    const request = deferred();
    const h = harness(() => request.promise);
    const saving = h.run("runLocalDownload(file, '/save', d => d.path)");
    await tick();
    h.btn.isConnected = false;
    const replacement = button();
    h.buttons = [replacement];
    await h.run('refreshDownloadButtons()');
    assert.equal(replacement.disabled, true, 'rerendered buttons preserve pending state');
    request.resolve({json:async () => ({ok:true, path:'C:/slides.pdf'})});
    await saving;
    assert.equal(replacement.disabled, false);
    assert.equal(replacement.textContent, '已下载 · 定位');
  }
  {
    let url;
    const h = harness(async path => {url = path; return {json:async () => ({ok:true, existing:true, revealed:true, path:'C:/字幕.txt'})};});
    h.btn.dataset.localKind = 'subtitles';
    h.btn.dataset.localName = '原课程/录播';
    h.run("DETAIL_COURSE_NAME = 'Other Course'; DETAIL_COURSE_ID = 100;");
    await h.run("exportLiveSubtitles('12', btn)");
    const query = new URL('http://localhost' + url).searchParams;
    assert.equal(query.get('course'), 'Course', 'export keeps the clicked row course');
    assert.equal(query.get('course_id'), '42');
    assert.equal(query.get('name'), '原课程/录播');
    assert.equal(h.btn.textContent, '已导出 · 定位');
  }
  {
    let url;
    const h = harness(async path => {url = path; return {json:async () => ({ok:true, path:'C:/slides.pdf'})};});
    await h.run('downloadUpload(12, btn)');
    assert.match(url, /^\/api\/save_download\/12\?/);
    assert.equal(new URL('http://localhost' + url).searchParams.get('course_id'), '42');
  }
  console.log('Passed 7 download UI scenarios.');
}
main().catch(e => {console.error(e); process.exitCode = 1;});
