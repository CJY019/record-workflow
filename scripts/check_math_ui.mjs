// Run: node scripts/check_math_ui.mjs
// Real Chromium, local assets and a disposable mock server; no running app/API is used.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {mkdtemp, readFile, writeFile, mkdir} from 'node:fs/promises';
import {createServer} from 'node:http';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root = fileURLToPath(new URL('../', import.meta.url));
const staticRoot = path.join(root, 'app/static');
// During a safe server rollout the real page may still use its old CDN URLs.
// Serve the exact same versions locally here so offline loading is always tested.
const html = (await readFile(path.join(staticRoot, 'index.html'), 'utf8'))
  .replace('https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.css', '/assets/vendor/katex-0.16.9/katex.min.css')
  .replace('https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js', '/assets/vendor/marked-12.0.2/marked.min.js')
  .replace('https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.js', '/assets/vendor/katex-0.16.9/katex.min.js')
  .replace(/<script src="https:\/\/[^\"]+\/contrib\/auto-render\.min\.js"><\/script>/, '');
const css = await readFile(path.join(staticRoot, 'vendor/katex-0.16.9/katex.min.css'), 'utf8');
for (const match of css.matchAll(/url\(([^)]+)\)/g)) {
  const relative = match[1].replace(/^['"]|['"]$/g, '');
  assert.ok(existsSync(path.join(staticRoot, 'vendor/katex-0.16.9', relative)), `Missing font: ${relative}`);
}
const assets = new Set(), missing = [], external = [], exceptions = [];
let chatResponse;
const json = (res, value) => { res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify(value)); };
const chat = {id: 'math-conv', title: 'Formula check', updated_at: 1, count: 0};
let menuChat = {id: 'menu-conv', title: 'Menu check', updated_at: 1, count: 0};
const menuOperations = [];
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/') { res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'}); res.end(html); return; }
    if (url.pathname.startsWith('/assets/')) {
      const file = path.resolve(staticRoot, decodeURIComponent(url.pathname.slice('/assets/'.length)));
      const relative = path.relative(staticRoot, file);
      if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('Asset path escaped static directory');
      const body = await readFile(file);
      const type = {'.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2',
                    '.woff': 'font/woff', '.ttf': 'font/ttf'}[path.extname(file)] || 'application/octet-stream';
      assets.add(url.pathname); res.writeHead(200, {'Content-Type': type}); res.end(body); return;
    }
    if (url.pathname === '/info') return json(res, {local: false, prompts: ['lecture'], models: [], summary_model: 'test', chat_model: 'test'});
    if (url.pathname === '/projects' || url.pathname === '/jobs') return json(res, []);
    if (url.pathname === '/jobs/math-job/chats') return json(res, [chat, ...(menuChat ? [menuChat] : [])]);
    if (url.pathname === '/jobs/math-job/chats/menu-conv') {
      if (req.method === 'PATCH') {
        const parts=[]; for await (const part of req) parts.push(part);
        const data=JSON.parse(Buffer.concat(parts).toString());
        menuChat.title=data.title; menuOperations.push({method:'PATCH',title:data.title}); return json(res, menuChat);
      }
      if (req.method === 'DELETE') { menuChat=null; menuOperations.push({method:'DELETE'}); return json(res,{deleted:true}); }
      return json(res, {...menuChat,messages:[]});
    }
    if (url.pathname === '/jobs/math-job/chats/math-conv') return json(res, {...chat, messages: []});
    if (url.pathname === '/jobs/math-job/chats/math-conv/messages') {
      req.resume(); chatResponse = res;
      res.writeHead(200, {'Content-Type': 'text/plain; charset=utf-8'});
      res.write(String.raw`角度 \(\theta_i`);
      return;
    }
    if (url.pathname === '/favicon.ico') { res.writeHead(204); res.end(); return; }
    missing.push(url.pathname); res.writeHead(404); res.end('Not found');
  } catch (error) { missing.push(`${req.url}: ${error.message}`); res.writeHead(500); res.end(error.message); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const chrome = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/google-chrome', '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'].find(p => p && existsSync(p));
if (!chrome) { server.close(); throw new Error('Set CHROME_PATH to a Chrome or Chromium executable.'); }
const profile = await mkdtemp(path.join(tmpdir(), 'record-math-check-'));
const browser = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], {windowsHide: true, stdio: ['ignore','ignore','pipe']});
let launchError = '';
browser.stderr.on('data', data => { launchError = (launchError + data.toString()).slice(-4000); });
browser.on('error', error => { launchError = error.message; });
let ws;
try {
  let port;
  const launchDeadline = Date.now() + 30000;
  while (Date.now() < launchDeadline) {
    try {
      port = Number((await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]);
      if (Number.isInteger(port) && port > 0 && port < 65536) break;
    } catch {}
    if (browser.exitCode !== null || browser.signalCode !== null) break;
    await delay(100);
  }
  assert.ok(port, 'Browser did not start: ' + (launchError || 'startup exceeded 30 seconds'));
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let sequence = 0;
  const pending = new Map();
  ws.onmessage = ({data}) => {
    const msg = JSON.parse(data);
    if (msg.method === 'Runtime.exceptionThrown') exceptions.push(msg.params.exceptionDetails);
    if (msg.method === 'Network.requestWillBeSent') {
      const url = msg.params.request.url;
      if (/^https?:/.test(url) && !url.startsWith(base + '/')) external.push(url);
    }
    if (pending.has(msg.id)) {
      const p = pending.get(msg.id); pending.delete(msg.id); clearTimeout(p.timer);
      if (msg.error) p.reject(new Error(JSON.stringify(msg.error))); else p.resolve(msg.result);
    }
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence, timer = setTimeout(() => { pending.delete(id); reject(new Error('Timeout: ' + method)); }, 10000);
    pending.set(id, {resolve, reject, timer}); ws.send(JSON.stringify({id, method, params}));
  });
  const run = async expression => {
    const r = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };
  const until = async (check, label) => {
    for (let i = 0; i < 150; i++) { if (await check()) return; await delay(20); }
    throw new Error('Timed out: ' + label);
  };
  const render = async text => run(`(() => {
    const main = document.querySelector('main'); main.innerHTML = '<article id="math-check" class="md" style="padding:24px;max-width:850px;margin:auto"></article>';
    const el = document.getElementById('math-check'); md(el, ${JSON.stringify(text)});
    return {count:el.querySelectorAll('.katex').length, display:el.querySelectorAll('.katex-display').length,
      annotations:[...el.querySelectorAll('annotation')].map(n=>n.textContent),
      code:[...el.querySelectorAll('code')].map(n=>n.textContent), codeMath:el.querySelectorAll('code .katex').length,
      errors:el.querySelectorAll('.katex-error').length, text:el.textContent, tableMath:el.querySelectorAll('table .katex').length};
  })()`);
  await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
  await send('Emulation.setDeviceMetricsOverride', {width: 1024, height: 900, deviceScaleFactor: 1, mobile: true});
  await send('Page.navigate', {url: base});
  await until(() => run(`!!document.querySelector('#uploadFields') && !!window.marked && !!window.katex`), 'app initialization');
  await run('uiBusy = true');

  let result = await render(String.raw`Inline \(\theta_i\), $E_i$.

\[dE_i=L_i\cos(\theta_i)d\theta_i\]

$$E=\sum_{i=1}^n E_i$$`);
  assert.equal(result.count, 4); assert.equal(result.display, 2); assert.equal(result.errors, 0);
  assert.deepEqual(result.annotations, [String.raw`\theta_i`, 'E_i', String.raw`dE_i=L_i\cos(\theta_i)d\theta_i`, String.raw`E=\sum_{i=1}^n E_i`]);
  console.log('PASS: all four delimiters render before Markdown consumes TeX escapes.');

  const multiline = String.raw`\[
\begin{aligned}
a_i &= b_j * c_k \\
E &= \sum_{i=1}^{n} E_i
\end{aligned}
\]`;
  result = await render(multiline);
  assert.equal(result.count, 1); assert.equal(result.display, 1); assert.equal(result.errors, 0);
  assert.ok(result.annotations[0].includes('a_i &= b_j * c_k'));
  result = await render('| Symbol | Meaning |\n| --- | --- |\n| ' + String.raw`\(\theta_i\)` + ' | Angle |\n| $x_i^2$ | Square |');
  assert.equal(result.tableMath, 2); assert.equal(result.errors, 0);
  console.log('PASS: multiline equations, underscores, alignment and Markdown tables retain their syntax.');

  await render(Array.from({length: 75}, (_, i) => String.raw`Paragraph ${i}: \(x_i^2+\theta_i\).`).join('\n\n'));
  const scroll = await run(`(() => {
    const main=document.querySelector('main'); main.scrollTop=250; window.scrollTo(0,300);
    return {rootHeight:document.documentElement.scrollHeight,viewport:innerHeight,rootScroll:scrollY,mainScroll:main.scrollTop};
  })()`);
  assert.equal(scroll.rootHeight, scroll.viewport, 'Hidden MathML escaped the main scroll container');
  assert.equal(scroll.rootScroll, 0); assert.ok(scroll.mainScroll > 0);
  console.log('PASS: a long equation document scrolls inside the main pane without moving the whole app.');

  const code = ['Inline code: `' + String.raw`\(x_i\)` + '` and ``$x_i$``.', '',
    '```tex', String.raw`\[x_i^2\]`, '$$a_b$$', '```', '',
    '~~~text', String.raw`\(z_j\)`, '~~~', '',
    '    ' + String.raw`\(indented_i\)`, '',
    '<pre><code>' + String.raw`\(raw_i\)` + '</code></pre>', '', String.raw`Outside \(q_i\).`].join('\n');
  result = await render(code);
  assert.equal(result.count, 1); assert.equal(result.codeMath, 0);
  assert.ok(result.code.some(s => s === String.raw`\(x_i\)`));
  assert.ok(result.code.some(s => s.includes('$$a_b$$')));
  result = await render(String.raw`Prices $5 and $10. Escaped \$x\$ stays literal.`);
  assert.equal(result.count, 0); assert.ok(result.text.includes('$5 and $10'));
  result = await render(String.raw`Incomplete \(\theta_i`);
  assert.equal(result.count, 0);
  result = await render(String.raw`Incomplete \(\theta_i\)`);
  assert.equal(result.count, 1);
  result = await render(String.raw`Invalid \(\frac{a}{\).`);
  assert.equal(result.errors, 1);
  console.log('PASS: code, prices and incomplete chunks stay safe; invalid TeX does not crash rendering.');

  const visual = ['# 公式显示验证', String.raw`角度 \(\theta_i\)，代价 $E_i$，以及系数 \(a_i^2+b_j\)。`, '',
    multiline, '', '$$E=\\sum_{i=1}^n E_i$$', '',
    '| 符号 | 说明 |', '| --- | --- |', '| ' + String.raw`\(x_i^2\)` + ' | 表格中的数学公式 |', '',
    '代码示例保持原样：`' + String.raw`\(\theta_i\)` + '`', '', '```tex', String.raw`\[dE_i = L_i\cos(\theta_i)d\theta_i\]`, '```'].join('\n');
  result = await render(visual);
  assert.equal(result.count, 6); assert.equal(result.errors, 0);
  await run('document.fonts.ready');
  assert.ok([...assets].some(a => a.endsWith('.woff2')), 'Browser did not load local KaTeX fonts');
  const screenshot = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  const imagePath = path.join(root, '.cache/math-ui-check.png');
  await mkdir(path.dirname(imagePath), {recursive: true});
  await writeFile(imagePath, Buffer.from(screenshot.data, 'base64'));

  await run(`(() => {
    cur = 'math-job'; conv = 'math-conv'; tab = 'chat'; convs = [${JSON.stringify(chat)}];
    document.querySelector('main').innerHTML = '<div id="pane"><div id="convlist"></div><div id="chatTitle"></div><div id="msgs"></div><textarea id="q"></textarea><button id="send"></button><select id="chatModel"><option value="test">Test</option></select></div>';
    void (window.mathAsk = ask('请解释公式'));
  })()`);
  await until(() => !!chatResponse, 'stream response started');
  await until(() => run(`!!document.querySelector('#msgs .typing')`), 'typing bubble');
  await delay(110);
  chatResponse.write(String.raw`\) 已生成。`);
  await until(() => run(`document.querySelectorAll('#msgs .typing .katex').length === 1`), 'math during streaming');
  await run('loadConv()');
  assert.equal(await run(`document.querySelectorAll('#msgs .typing .katex').length`), 1);
  chatResponse.end('\n\n' + String.raw`\[dE_i=L_i\cos(\theta_i)d\theta_i\]`);
  await run('window.mathAsk');
  assert.equal(await run(`document.querySelectorAll('#msgs .katex').length`), 2);
  assert.equal(await run(`document.querySelectorAll('#msgs .katex-display').length`), 1);
  assert.equal(await run(`document.querySelectorAll('#msgs .typing').length`), 0);
  console.log('PASS: live answers render math before completion and preserve it when reconnecting the conversation.');

  await run(`INFO.models=[{id:'test:offline',label:'Unavailable',provider:'Test',available:false},
    {id:'test:ready',label:'Ready',provider:'Test',available:true}];
    INFO.chat_model='test:offline'; INFO.summary_models=['test:ready'];
    localStorage.setItem('chatModel','test:offline'); window.confirm=()=>true;`);
  for (const [width,height] of [[1440,900],[1120,590],[820,1000],[390,700]]) {
    await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<1440});
    await send('Emulation.setTouchEmulationEnabled',{enabled:width<1440,maxTouchPoints:5});
    await run(`(async()=>{
      cur='math-job'; conv='math-conv'; tab='chat';
      document.querySelector('main').innerHTML='<div class="wrap wide"><div id="pane"></div></div>';
      await renderChat({id:'math-job'});
      if (innerWidth<=960) document.querySelector('.convtoggle').click();
      const list=document.querySelector('.convs'); list.style.maxHeight='180px'; list.scrollTop=1e9;
      addMsg('user','long_unbroken_message_'.repeat(240));
      addMsg('assistant','[出错：ServiceUnavailableError: '+ 'a_very_long_provider_message_'.repeat(160)+']');
      document.querySelector('main').scrollTop=0;
    })()`);
    await delay(60);
    assert.equal(await run(`document.querySelector('#chatModel').value`),'test:ready','Unavailable cached model remained selected');
    const buttonPoint=await run(`(()=>{const r=document.querySelector('.conv[data-id="menu-conv"] .more').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    if (width<1440) {
      await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[buttonPoint]});
      await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    } else {
      await send('Input.dispatchMouseEvent',{type:'mouseMoved',...buttonPoint});
      await delay(20);
      await send('Input.dispatchMouseEvent',{type:'mousePressed',...buttonPoint,button:'left',clickCount:1});
      await send('Input.dispatchMouseEvent',{type:'mouseReleased',...buttonPoint,button:'left',clickCount:1});
    }
    await delay(50);
    assert.equal(await run(`!!document.querySelector('.fmenu')`),true,`Native pointer did not open the conversation menu at ${width}px`);
    const visible=await run(`(()=>{
      const menu=document.querySelector('.fmenu'),r=menu.getBoundingClientRect();
      return {parent:menu.parentElement.tagName,top:r.top,bottom:r.bottom,left:r.left,right:r.right,
        clickable:[...menu.querySelectorAll('button')].every(b=>{const x=b.getBoundingClientRect(); return document.elementFromPoint(x.x+x.width/2,x.y+x.height/2)?.closest('button')===b;}),
        rootWidth:document.documentElement.scrollWidth,rootHeight:document.documentElement.scrollHeight,
        mainWidth:document.querySelector('main').clientWidth,mainScrollWidth:document.querySelector('main').scrollWidth};
    })()`);
    assert.equal(visible.parent,'BODY','Menu is still inside the clipped conversation list');
    assert.ok(visible.top>=8 && visible.left>=8 && visible.right<=width-8 && visible.bottom<=height-8,JSON.stringify(visible));
    assert.ok(visible.clickable,'A menu action is clipped or covered');
    assert.equal(visible.rootWidth,width); assert.equal(visible.rootHeight,height);
    assert.ok(visible.mainScrollWidth<=visible.mainWidth+1,'A long message expanded the chat pane');
    if (width===1120) {
      const image=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      await writeFile(path.join(root,'.cache/chat-menu-check.png'),Buffer.from(image.data,'base64'));
    }
    await run(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))`);
    assert.equal(await run(`!!document.querySelector('.fmenu')`),false);
    await run(`document.querySelector('.conv[data-id="menu-conv"] .more').click(); document.querySelector('.conv[data-id="menu-conv"] .more').click()`);
    assert.equal(await run(`!!document.querySelector('.fmenu')`),false,'More button does not toggle the menu');
    await run(`document.querySelector('.conv[data-id="menu-conv"] .more').click(); document.querySelector('main').scrollTop=100`);
    await delay(40);
    assert.equal(await run(`!!document.querySelector('.fmenu')`),false,'Scrolling left a detached menu open');
  }
  await run(`document.querySelector('main').scrollTop=0`); await delay(40);
  await run(`document.querySelector('.conv[data-id="menu-conv"] .more').click(); document.querySelector('.fmenu button').click()`);
  assert.equal(await run(`!!document.querySelector('.conv[data-id="menu-conv"] input')`),true);
  await run(`(()=>{const input=document.querySelector('.conv[data-id="menu-conv"] input'); input.value='Renamed through menu'; input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));})()`);
  await until(()=>run(`document.querySelector('.conv[data-id="menu-conv"] .ct')?.textContent==='Renamed through menu'`),'rename menu action');
  await run(`document.querySelector('.conv[data-id="menu-conv"] .more').click(); document.querySelectorAll('.fmenu button')[1].click()`);
  await until(()=>run(`!document.querySelector('.conv[data-id="menu-conv"]')`),'delete menu action');
  assert.deepEqual(menuOperations,[{method:'PATCH',title:'Renamed through menu'},{method:'DELETE'}]);
  console.log('PASS: conversation menus remain clickable outside clipped lists at desktop/tablet/phone sizes; rename, delete, toggle and dismissal work.');
  console.log('PASS: long messages stay inside the pane and an unavailable cached model is not selected.');

  assert.deepEqual(external, [], 'Page depended on external CDN requests');
  assert.deepEqual(missing, [], 'Missing assets or mock routes');
  assert.deepEqual(exceptions, [], 'Browser JavaScript exception');
  console.log(`PASS: local JavaScript, CSS and fonts load without external requests. Screenshot: ${imagePath}`);
  await send('Browser.close');
} finally {
  ws?.close();
  if (browser.exitCode === null) browser.kill();
  browser.stderr.destroy();
  chatResponse?.end(); server.closeAllConnections(); server.close();
}
