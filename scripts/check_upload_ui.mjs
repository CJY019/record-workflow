// Run: node scripts/check_upload_ui.mjs
// Uses a disposable Chrome profile and an isolated mock server; never uploads to the running app.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {mkdtemp, readFile, writeFile, mkdir} from 'node:fs/promises';
import {createServer} from 'node:http';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const html = (await readFile(new URL('../app/static/index.html', import.meta.url), 'utf8'))
  .replace(/<script src="[^"]+"[^>]*><\/script>/g, '').replace(/<link[^>]+rel="stylesheet"[^>]*>/g, '');
const projects = [{id:'project-a', name:'课程 A'}];
const jobs = [{id:'existing-job', project:'project-a', filename:'existing.m4a', status:'done', files:[], created_at:1}];
const received = [], chunks = new Map(), held = [], released = new Set(), failures = new Set();
let projectResponse, existingResponse, creationResponse, holdExisting = true;
const json = (res, value, status = 200) => { res.writeHead(status, {'Content-Type':'application/json'}); res.end(JSON.stringify(value)); };
const readBody = async req => { const parts = []; for await (const part of req) parts.push(part); return Buffer.concat(parts); };
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/') { res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'}); res.end(html); return; }
    if (url.pathname === '/info') return json(res, {local:false, prompts:['lecture','brief'], models:[
      {id:'test:alpha',label:'Alpha',provider:'Test',vision:true}, {id:'test:beta',label:'Beta',provider:'Test',vision:true}], summary_model:'test:alpha'});
    if (url.pathname === '/projects') {
      if (req.method === 'GET') return json(res, projects);
      const body = JSON.parse((await readBody(req)).toString());
      projectResponse = () => { const p = {id:'project-b', name:body.name}; projects.push(p); json(res, p); };
      return;
    }
    const chunk = url.pathname.match(/^\/uploads\/([^/]+)\/(\d+)$/);
    if (chunk) {
      const bytes = await readBody(req), kind = String.fromCharCode(bytes[0]);
      const finish = () => {
        if (failures.has(kind)) return json(res, {detail:'Simulated network failure'}, 503);
        chunks.set(`${chunk[1]}/${chunk[2]}`, bytes);
        json(res, {ok:true,size:bytes.length});
      };
      if (released.has(kind) || failures.has(kind)) finish(); else held.push({kind, finish});
      return;
    }
    if (url.pathname === '/jobs') {
      if (req.method === 'GET') return json(res, jobs);
      const boundary = req.headers['content-type'].split('boundary=')[1];
      const form = Object.fromEntries((await readBody(req)).toString().split('--' + boundary).flatMap(part => {
        const name = part.match(/name="([^"]+)"/), separator = part.indexOf('\r\n\r\n');
        return name && separator >= 0 ? [[name[1], part.slice(separator + 4).replace(/\r\n$/, '')]] : [];
      }));
      for (const field of ['uploads','attach_uploads']) {
        for (const spec of JSON.parse(form[field] || '[]')) {
          const parts = Array.from({length:spec.count}, (_, i) => chunks.get(`${spec.id}/${i}`));
          assert.ok(parts.every(Boolean), 'Job was created before every chunk arrived');
          assert.equal(parts.reduce((n,b) => n + b.length, 0), spec.size);
          assert.equal(new Set(parts.map(b => b[0])).size, 1, 'Chunks from different files were combined');
        }
      }
      received.push(form);
      const finish = () => {
        const j = {id:`created-${received.indexOf(form) + 1}`, project:form.project,
          filename:JSON.parse(form.uploads)[0].name, status:'queued', stage:'preprocess', files:[], created_at:1};
        jobs.push(j); json(res, {id:j.id,ids:[j.id]});
      };
      if (form.project === 'project-a' && received.length === 1) creationResponse = finish; else finish();
      return;
    }
    if (url.pathname === '/jobs/existing-job' && holdExisting) { existingResponse = () => json(res, jobs[0]); return; }
    const job = jobs.find(j => url.pathname === '/jobs/' + j.id);
    if (job) return json(res, job);
    if (/\/materials$/.test(url.pathname)) return json(res, []);
    json(res, {detail:'Not found: ' + url.pathname}, 404);
  } catch (error) { console.error(error); json(res, {detail:String(error)}, 500); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const chrome = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/google-chrome', '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'].find(p => p && existsSync(p));
if (!chrome) { server.close(); throw new Error('Set CHROME_PATH to a Chrome or Chromium executable.'); }
const profile = await mkdtemp(path.join(tmpdir(), 'record-upload-check-'));
const browser = spawn(chrome, ['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
  '--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'], {windowsHide:true,stdio:'ignore'});
let ws;
try {
  let port;
  for (let i=0;i<100;i++) {
    try { port = Number((await readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]); break; }
    catch { await delay(50); }
  }
  assert.ok(port, 'Browser did not start');
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve,reject) => { ws.onopen=resolve; ws.onerror=reject; });
  let sequence=0;
  const pending=new Map(), exceptions=[];
  ws.onmessage = ({data}) => {
    const msg=JSON.parse(data);
    if (msg.method === 'Runtime.exceptionThrown') exceptions.push(msg.params.exceptionDetails);
    if (pending.has(msg.id)) {
      const p=pending.get(msg.id); pending.delete(msg.id); clearTimeout(p.timer);
      if (msg.error) p.reject(new Error(JSON.stringify(msg.error))); else p.resolve(msg.result);
    }
  };
  const send = (method,params={}) => new Promise((resolve,reject) => {
    const id=++sequence, timer=setTimeout(() => { pending.delete(id); reject(new Error('Timeout: '+method)); },10000);
    pending.set(id,{resolve,reject,timer}); ws.send(JSON.stringify({id,method,params}));
  });
  const run = async expression => {
    const r=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };
  const until = async (check,label) => {
    for (let i=0;i<150;i++) { if (await check()) return; await delay(20); }
    throw new Error('Timed out: '+label);
  };
  const waitUI = expression => until(() => run(expression),expression);
  const release = kind => {
    released.add(kind);
    for (let i=held.length-1;i>=0;i--) if (held[i].kind===kind) held.splice(i,1)[0].finish();
  };
  const selectFile = (input,kind,names,size=100) => run(`(() => {
    const dt = new DataTransfer();
    for (const name of ${JSON.stringify(names)}) dt.items.add(new File(['${kind}'.repeat(${size})],name));
    const input = document.querySelector('${input}'); input.files=dt.files; input.dispatchEvent(new Event('change'));
  })()`);
  const fields = values => run(`(() => {
    for (const [id,value] of Object.entries(${JSON.stringify(values)})) {
      const el=document.getElementById(id); el.value=value; el.dispatchEvent(new Event('input'));
    }
  })()`);
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument',{source:`
    const nativeTimeout=window.setTimeout;
    window.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,Math.min(ms,10),...args);
    window.prompt=()=>'课程 B';
  `});
  await send('Emulation.setDeviceMetricsOverride',{width:820,height:1000,deviceScaleFactor:1,mobile:true});
  await send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:5});
  await send('Page.navigate',{url:base});
  await waitUI(`!!document.querySelector('#fileIn') && PROJECTS.length === 1`);

  await run(`newTask('project-a')`);
  await selectFile('#fileIn','a',['A-2.m4a','A-1.m4a'],(4<<20)+17);
  await selectFile('#docIn','d',['A-slides.pdf']);
  await fields({context:'A context',model:'test:alpha',prompt:'lecture',extra:'A notes',spk:'2'});
  await run(`draft.projectFiles=['A/notes.pdf']; void submit(); void submit()`);
  const aId=await run('draft.id');
  await until(() => held.filter(x=>x.kind==='a').length === 3,'parallel A chunks');
  assert.deepEqual(await run(`(() => {
    drawFiles(); removeDraftFile('files',0); addFiles([new File(['x'],'wrong.m4a')]);
    return {disabled:$('#go').disabled,locked:$('#uploadFields').disabled,files:draft.files.map(f=>f.name)};
  })()`),{disabled:true,locked:true,files:['A-1.m4a','A-2.m4a']});
  console.log('PASS: upload stays locked after file-list updates and ignores duplicate submit.');

  await run(`go('existing-job')`);
  await until(() => !!existingResponse,'delayed existing job response');
  await run(`newTask('')`);
  holdExisting=false; existingResponse();
  await delay(80);
  assert.equal(await run(`!cur && !!$('#fileIn') && !$('#jobTitle')`),true);
  await selectFile('#fileIn','b',['B.m4a']);
  await run(`$('#project').value='__new'; $('#project').dispatchEvent(new Event('change'))`);
  await until(() => !!projectResponse,'project creation request');
  assert.equal(await run(`!!draft.projectPending && $('#go').disabled`),true);
  projectResponse();
  await waitUI(`draft.project === 'project-b' && !draft.projectPending`);
  await selectFile('#docIn','e',['B-slides.pdf']);
  await fields({context:'B context',model:'test:beta',prompt:'brief',extra:'B notes',language:'English'});
  await run(`draft.projectFiles=['B/notes.pdf']; void submit()`);
  const bId=await run('draft.id');
  await until(() => held.some(x=>x.kind==='b'),'parallel B upload');
  assert.deepEqual(await run(`({files:draft.files.map(f=>f.name),uploads:UPLOADS.size,disabled:$('#go').disabled})`),
    {files:['B.m4a'],uploads:2,disabled:true});
  assert.equal(await run(`document.querySelector('#uploadOverview').textContent.includes('A-1.m4a')`),true);
  await mkdir(new URL('../.cache/shots/',import.meta.url),{recursive:true});
  const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
  await writeFile(new URL('../.cache/shots/upload-concurrent.png',import.meta.url),Buffer.from(shot.data,'base64'));
  console.log('PASS: creating another project and uploading B preserves A and its progress.');

  release('a');
  await until(() => held.some(x=>x.kind==='d'),'A attachment upload');
  assert.equal(received.length,0,'Jobs must wait for attachments');
  release('d');
  await until(() => !!creationResponse,'A job creation');
  await run(`openUpload('${aId}')`);
  assert.equal(await run(`$('#go').disabled && $('#uploadFields').disabled && draft.status==='creating'`),true);
  await run(`openUpload('${bId}')`);
  creationResponse();
  await waitUI(`UPLOADS.size === 1`);
  assert.equal(await run(`!cur && draft.id==='${bId}' && $('#context').value==='B context' && $('#go').disabled`),true);
  assert.deepEqual(JSON.parse(received[0].uploads).map(x=>x.name),['A-1.m4a','A-2.m4a']);
  assert.deepEqual(JSON.parse(received[0].attach_uploads).map(x=>x.name),['A-slides.pdf']);
  assert.deepEqual(JSON.parse(received[0].project_files),['A/notes.pdf']);
  for (const [key,value] of Object.entries({project:'project-a',merge:'true',context:'A context',model:'test:alpha',prompt:'lecture',extra:'A notes',num_speakers:'2'})) assert.equal(received[0][key],value,key);
  console.log('PASS: A uses its original files, project, merge setting, materials and model; completion leaves B intact.');

  failures.add('c');
  await run(`newTask('project-a')`);
  await selectFile('#fileIn','c',['C.m4a']);
  await fields({context:'C context'});
  await run('void submit()');
  const cId=await run('draft.id');
  await run(`openUpload('${bId}')`);
  await waitUI(`UPLOADS.get('${cId}').status==='error'`);
  assert.equal(await run(`draft.id==='${bId}' && $('#go').disabled && $('#uploadFields').disabled`),true);
  assert.equal(await run(`(() => {const e=new Event('beforeunload',{cancelable:true}); window.dispatchEvent(e); return e.defaultPrevented;})()`),true);
  await run(`openUpload('${cId}')`);
  assert.equal(await run(`!$('#go').disabled && !$('#uploadFields').disabled && $('#go').textContent==='重试'`),true);
  console.log('PASS: failure in C cannot unlock B; failed files remain available to retry.');

  await run(`go('existing-job')`);
  await waitUI(`!!$('#jobTitle')`);
  release('b');
  await until(() => held.some(x=>x.kind==='e'),'B attachment upload');
  release('e');
  await waitUI(`UPLOADS.size===1`);
  assert.equal(await run(`cur==='existing-job' && !!$('#jobTitle')`),true);
  assert.deepEqual(JSON.parse(received[1].uploads).map(x=>x.name),['B.m4a']);
  assert.deepEqual(JSON.parse(received[1].attach_uploads).map(x=>x.name),['B-slides.pdf']);
  assert.deepEqual(JSON.parse(received[1].project_files),['B/notes.pdf']);
  for (const [key,value] of Object.entries({project:'project-b',merge:'false',context:'B context',model:'test:beta',prompt:'brief',extra:'B notes',language:'English'})) assert.equal(received[1][key],value,key);
  console.log('PASS: B remains separate and finishes while an existing task is open.');

  failures.delete('c'); release('c');
  await run(`openUpload('${cId}'); void submit(); void submit()`);
  await waitUI(`UPLOADS.size===0 && cur==='created-3'`);
  assert.equal(received.length,3);
  assert.equal(received[2].context,'C context');
  assert.deepEqual(JSON.parse(received[2].uploads).map(x=>x.name),['C.m4a']);
  assert.equal(await run(`(() => {const e=new Event('beforeunload',{cancelable:true}); window.dispatchEvent(e); return e.defaultPrevented;})()`),false);
  assert.deepEqual(exceptions,[],'Browser JavaScript exception');
  console.log('PASS: retry creates one task, and the leave-page warning clears after all uploads settle.');
  await send('Browser.close');
} finally {
  ws?.close();
  if (browser.exitCode === null) browser.kill();
  for (const item of held) { try { item.finish(); } catch {} }
  server.closeAllConnections(); server.close();
}
