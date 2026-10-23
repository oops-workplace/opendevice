const frameEl = document.getElementById('frame');
const fctx = frameEl.getContext('2d');
const cvs = document.getElementById('boxes');
const ctx = cvs.getContext('2d');
const tip = document.getElementById('tip');
const list = document.getElementById('list');
const stat = document.getElementById('stat');
let elements = [], screenSize = null, showBoxes = true, hovering = null, chosen = null;
let decoder = null, tsUs = 0, waitingKey = true, hevcFailed = false, metaCodec = null;
let notice = '';
let msgUntil = 0;
let expectVideo = false;
let waitOn = false;
let drawn = 0, drawnFps = 0, drawnT0 = 0, errCount = 0, lastKeyAsk = 0;

const devNameEl = document.getElementById('devName');
const bannerEl = document.getElementById('banner');
const bannerTitle = document.getElementById('bannerTitle');
const bannerBody = document.getElementById('bannerBody');
const bannerBtn = document.getElementById('bannerBtn');
const devmodeVal = document.getElementById('devmodeVal');
const imageVal = document.getElementById('imageVal');
const pairVal = document.getElementById('pairVal');
const mountNoteEl = document.getElementById('mountNote');
const mountBtn = document.getElementById('mountBtn');
let bannerMode = '';
function setPval(el, text, ok) {
  el.textContent = text;
  el.classList.toggle('ok', ok === true);
  el.classList.toggle('bad', ok === false);
}
function applyPreflight(s) {
  setPval(devmodeVal, s.devmode === true ? 'on' : (s.devmode === false ? 'off' : '—'),
          s.devmode === true ? true : (s.devmode === false ? false : null));
  setPval(imageVal, s.image || 'not mounted', !!s.image);
  setPval(pairVal, s.paired ? 'paired' : 'not paired', s.paired);
  mountBtn.hidden = !!s.image;
  mountBtn.disabled = !!s.mount_note && s.mount_note.endsWith('...');
  mountNoteEl.hidden = !s.mount_note;
  mountNoteEl.textContent = s.mount_note || '';
  if (s.connected === false) { bannerMode = ''; bannerEl.hidden = true; return; }
  let mode = '';
  if (s.devmode === false) mode = 'devmode';
  else if (!s.image) mode = 'image';
  bannerMode = mode;
  bannerEl.hidden = !mode;
  if (!mode) return;
  if (mode === 'devmode') {
    bannerTitle.textContent = 'Developer mode is off';
    bannerBody.textContent = 'Screen mirroring / touch / keyboard / control tree all need it. On the device: Settings -> Privacy & Security -> Developer Mode, turn it on (the device restarts once).';
    bannerBtn.textContent = 'Re-check';
  } else {
    bannerTitle.textContent = 'Personalized image not mounted';
    bannerBody.textContent = 'The screen-mirroring services (com.apple.coredevice.*) are not in the service table. Connect once with Xcode / Device Hub, or push an image up.';
    bannerBtn.textContent = 'Push image';
  }
}
bannerBtn.onclick = () => {
  if (bannerMode === 'devmode') post('/api/preflight', {});
  else if (bannerMode === 'image') post('/api/mount', {});
};
mountBtn.onclick = () => post('/api/mount', {});
document.getElementById('pairBtn').onclick = () => {
  post('/api/pairing', {}).then(j => { if (j) setStat('remote pairing established: ' + (j.path || ''), 8); });
};
document.getElementById('unpairBtn').onclick = () => {
  post('/api/unpairing', {}).then(j => {
    if (j) setStat(j.removed && j.removed.length
      ? ('deleted pairing records: ' + j.removed.join(', '))
      : 'no pairing record for this device on this host', 8);
  });
};

function askKeyframe() {
  const now = performance.now();
  if (now - lastKeyAsk < 2000) return;
  lastKeyAsk = now;
  post('/api/keyframe', {});
}

function layout() {
  const nw = frameEl.width || 1, nh = frameEl.height || 1;
  const r = frameEl.getBoundingClientRect();
  cvs.width = nw; cvs.height = nh;
  cvs.style.width = (r.width || nw) + 'px';
  cvs.style.height = (r.height || nh) + 'px';
  draw();
}
function draw() {
  ctx.clearRect(0, 0, cvs.width, cvs.height);
  if (!showBoxes || !screenSize) return;
  const [dw, dh] = screenSize;
  ctx.lineWidth = 2;
  for (const el of elements) {
    if (!el.frame) continue;
    const [x, y, w, h] = el.frame;
    const hot = el === hovering || el === chosen;
    ctx.strokeStyle = hot ? '#ff4d3d' : 'rgba(26,153,255,.9)';
    ctx.strokeRect(x / dw * cvs.width, y / dh * cvs.height,
                   w / dw * cvs.width, h / dh * cvs.height);
    if (hot && el.text) {
      ctx.font = 'bold 20px sans-serif';
      const t = el.text.slice(0, 30);
      const tw = ctx.measureText(t).width;
      const bx = x / dw * cvs.width, by = Math.max(0, y / dh * cvs.height - 30);
      ctx.fillStyle = 'rgba(0,0,0,.78)';
      ctx.fillRect(bx, by, tw + 12, 26);
      ctx.fillStyle = '#fff';
      ctx.fillText(t, bx + 6, by + 19);
    }
  }
}
function toNorm(px, py) {
  return [px / cvs.width, py / cvs.height];
}
function hit(px, py) {
  if (!screenSize) return null;
  const [dw, dh] = screenSize;
  const x = px / cvs.width * dw, y = py / cvs.height * dh;
  let best = null;
  for (const el of elements) {
    if (!el.frame) continue;
    const [ex, ey, ew, eh] = el.frame;
    if (x >= ex && x <= ex + ew && y >= ey && y <= ey + eh) {
      const a = ew * eh;
      if (!best || a < best.a) best = { el, a };
    }
  }
  return best ? best.el : null;
}
function evtPos(e) {
  const r = cvs.getBoundingClientRect();
  return [(e.clientX - r.left) / r.width * cvs.width,
          (e.clientY - r.top) / r.height * cvs.height];
}
window.addEventListener('resize', layout);

async function post(path, body) {
  try {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body || {})});
    const j = await r.json().catch(() => null);
    if (j && (j.ok === false || j.error)) {
      setStat('failed: ' + (j.error || 'unknown error'), 8);
      return null;
    }
    return j;
  } catch (e) { setStat('request failed: ' + e, 8); return null; }
}
function setStat(t, holdSec = 0) {
  if (holdSec) msgUntil = performance.now() + holdSec * 1000;
  stat.textContent = t + (notice ? '  ·  ' + notice : '');
}

const waitEl = document.getElementById('wait'), waitText = document.getElementById('waitText');
function showWait(text) {
  waitText.textContent = text;
  if (!waitOn) { waitEl.classList.add('on'); waitOn = true; }
}
function hideWait() {
  if (waitOn) { waitEl.classList.remove('on'); waitOn = false; }
}
function resetDecoder() {
  if (decoder) { try { decoder.close(); } catch (_) {} }
  decoder = null; metaCodec = null; tsUs = 0; waitingKey = true;
}

async function grab() {
  setStat('fetching controls...');
  try {
    const r = await fetch('/api/elements').then(r => r.json());
    elements = r.elements || []; screenSize = r.screen || null;
    if (r.error) setStat('control tree failed: ' + r.error);
    else if (!elements.length) setStat('no controls found -- the device AX session may be wedged; a phone restart recovers it');
    else setStat('controls: ' + elements.length + ' · screen ' + JSON.stringify(screenSize));
    renderList(); draw();
  } catch (e) { setStat('grab failed: ' + e); }
}
function renderList() {
  const frag = document.createDocumentFragment();
  const withFrame = elements.filter(el => el.frame);
  document.getElementById('sideTitle').textContent =
    'Controls · ' + withFrame.length;
  withFrame.forEach((el, i) => {
    const div = document.createElement('div');
    div.className = 'item';
    const f = el.frame.map(v => Math.round(v)).join(', ');
    const name = el.text ? escapeHtml(el.text) : ('(' + (el.kind || 'unnamed') + ')');
    div.innerHTML = '<div class="t">' + name +
                    '</div><div class="f">[' + f + ']</div>';
    div.onmouseenter = () => { hovering = el; draw(); };
    div.onmouseleave = () => { hovering = null; draw(); };
    div.onclick = () => {
      chosen = el;
      if (el.center) post('/api/point', {x: el.center[0], y: el.center[1]});
      setStat('clicked control ' + JSON.stringify(el.text || '') + ' @ ' + f);
      draw();
    };
    frag.appendChild(div);
  });
  list.innerHTML = '';
  list.appendChild(frag);
}
function escapeHtml(s) {
  return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

let down = null, lastPt = null;
let ctlLocked = false;
let touchQueue = [], touchBusy = false;
function pumpTouch() {
  if (touchBusy || !touchQueue.length || ctlLocked) return;
  const [phase, x, y] = touchQueue.shift();
  touchBusy = true;
  fetch('/api/touch', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phase, x, y}),
  }).catch(() => {}).finally(() => { touchBusy = false; pumpTouch(); });
}
function pushTouch(phase, x, y) {
  if (ctlLocked) return;
  touchQueue.push([phase, x, y]);
  if (touchQueue.length > 120) touchQueue.splice(0, touchQueue.length - 120);
  pumpTouch();
}
cvs.addEventListener('pointerdown', e => {
  if (ctlLocked) { setStat('screen is down (usb-only policy): controls locked'); return; }
  down = evtPos(e); cvs.setPointerCapture(e.pointerId);
  const n = toNorm(down[0], down[1]);
  pushTouch('down', n[0], n[1]);
});
let cursorPos = null, cursorBusy = false;
function flushCursor() {
  if (cursorBusy || !cursorPos || ctlLocked) return;
  const p = cursorPos;
  cursorPos = null;
  cursorBusy = true;
  fetch('/api/cursor', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({x: p[0], y: p[1]}),
  }).catch(() => {}).finally(() => { cursorBusy = false; });
}
cvs.addEventListener('pointermove', e => {
  const [px, py] = evtPos(e);
  lastPt = [px, py];
  if (!ctlLocked) {
    const n = toNorm(px, py);
    if (down) {
      pushTouch('move', n[0], n[1]);
    } else {
      cursorPos = n; flushCursor();
    }
  }
  const el = hit(px, py);
  if (el !== hovering) {
    hovering = el; draw();
    if (el) {
      tip.style.display = 'block';
      tip.textContent = (el.text || '(' + (el.kind || 'unnamed') + ')') + '  ' + JSON.stringify(el.frame.map(Math.round));
      tip.style.left = px + 'px'; tip.style.top = Math.max(0, py - 22) + 'px';
    } else { tip.style.display = 'none'; }
  }
});
cvs.addEventListener('pointerup', e => {
  const [px, py] = evtPos(e);
  if (down) {
    const n = toNorm(px, py);
    pushTouch('up', n[0], n[1]);
    const moved = Math.abs(px - down[0]) + Math.abs(py - down[1]) > 10;
    const el = moved ? null : hit(px, py);
    setStat(moved ? 'drag end ' + n.map(v => v.toFixed(2)).join(', ')
                  : 'tap ' + JSON.stringify((el && el.text) || ''));
  }
  down = null;
});
cvs.addEventListener('pointerleave', () => {
  cursorPos = null;
  if (down && lastPt) {
    const n = toNorm(lastPt[0], lastPt[1]);
    pushTouch('up', n[0], n[1]);
    down = null;
  }
  if (hovering) { hovering = null; tip.style.display = 'none'; draw(); }
});

document.getElementById('grab').onclick = grab;
document.getElementById('clear').onclick = () => { elements = []; chosen = null; renderList(); draw(); };
document.getElementById('show').onclick = e => {
  showBoxes = !showBoxes; e.target.classList.toggle('on', showBoxes); draw();
};
document.getElementById('reconnect').onclick = () => {
  showWait('reconnecting video...');
  expectVideo = true;
  waitingKey = true;
  post('/api/reconnect');
  runFrames();
  setStat('reconnecting video...');
};
document.getElementById('disconnect').onclick = async () => {
  setStat('disconnecting...');
  if (!await post('/api/disconnect')) return;
  syncStreamBtn(false);
  expectVideo = false; hideWait();
  setStat('disconnected (device released; Xcode / QuickTime can connect now)');
};
let buttonHold = {};
document.querySelectorAll('.btn.key').forEach(b => {
  b.onclick = () => {
    if (ctlLocked) { setStat('screen is down (usb-only policy): controls locked'); return; }
    post('/api/button', {name: b.dataset.key, hold: buttonHold[b.dataset.key] || 0}); setStat('key ' + b.textContent);
  };
});
function sendText() {
  if (ctlLocked) { setStat('screen is down (usb-only policy): controls locked'); return; }
  const t = document.getElementById('text').value;
  if (!t) return;
  post('/api/type', {text: t});
  setStat('typing ' + t.length + ' characters (ASCII only)');
  document.getElementById('text').value = '';
}
function pasteText() {
  if (ctlLocked) { setStat('screen is down (usb-only policy): controls locked'); return; }
  const t = document.getElementById('text').value;
  if (!t) return;
  post('/api/sendtext', {text: t});
  setStat('sent ' + t.length + ' characters directly');
  document.getElementById('text').value = '';
}
document.getElementById('sendPaste').onclick = pasteText;
document.getElementById('send').onclick = sendText;
document.getElementById('text').onkeydown = e => { if (e.key === 'Enter') pasteText(); };
document.getElementById('keyEnter').onclick = () => post('/api/type', {text: 'enter'});
document.getElementById('keyDel').onclick = () => post('/api/type', {text: 'backspace'});

const sBtn = document.getElementById('streamToggle');
function syncStreamBtn(on) {
  sBtn.classList.toggle('on', on);
  sBtn.textContent = on ? 'Stop stream' : 'Start stream';
}
sBtn.onclick = async () => {
  const on = sBtn.classList.contains('on');
  sBtn.disabled = true;
  setStat(on ? 'stopping the stream...' : 'starting the stream...');
  if (!on) { expectVideo = true; showWait('waiting for the screen...'); }
  try {
    await post(on ? '/api/stream/stop' : '/api/stream/start');
    syncStreamBtn(!on);
    setStat(on ? 'stream stopped (device session torn down)' : 'stream started');
    if (!on) { waitingKey = true; runFrames(); }
    else { expectVideo = false; hideWait(); }
  } catch (e) {
    setStat('stream operation failed: ' + e);
    hideWait();
  }
  sBtn.disabled = false;
};

const tUsb = document.getElementById('trUsb'), tWifi = document.getElementById('trWifi');
let switching = false;
function applyTransport(tp) {
  if (!tp) return;
  tUsb.classList.toggle('on', tp.type === 'usb');
  tWifi.classList.toggle('on', tp.type === 'wifi');
  tUsb.textContent = tp.type === 'usb' ? 'USB ✓' : 'USB';
  tWifi.textContent = tp.type === 'wifi' ? 'Wi-Fi ✓' : 'Wi-Fi';
  const el = document.getElementById('usbState');
  let txt = (tp.active === 'wifi' ? 'on Wi-Fi' : 'on USB');
  txt += tp.usb_attached ? ' · cable in' : ' · cable out';
  if (tp.active === 'wifi') txt += ' (unplug-safe)';
  el.textContent = txt;
  el.style.color = tp.active === 'wifi' ? '#3fb950' : '#8b949e';
  tUsb.disabled = tWifi.disabled = !tp.enforced;
}
async function switchTransport(t) {
  if (switching) return;
  switching = true;
  tUsb.disabled = tWifi.disabled = true;
  const want = t === 'wifi' ? 'Wi-Fi' : 'USB';
  showWait('switching to ' + want + ': rebuilding the tunnel...');
  setStat(t === 'wifi'
    ? 'rebuilding the tunnel over Wi-Fi (bonjour discovery, may take tens of seconds)...'
    : 'rebuilding the tunnel over USB...');
  try {
    const j = await post('/api/transport', {type: t});
    if (!j) { hideWait(); return; }
    if (j.transport) applyTransport(j.transport);
    const label = (j.transport && j.transport.label) || want;
    setStat(j.changed ? ('switched to the ' + label + ' link') : ('already on ' + label + '; tunnel not rebuilt'), 8);
    if (j.changed) {
      expectVideo = true;
      showWait('switched to ' + label + '; waiting for the screen...');
      waitingKey = true; runFrames();
    } else {
      hideWait();
    }
  } finally {
    switching = false;
    tUsb.disabled = tWifi.disabled = false;
  }
}
tUsb.onclick = () => switchTransport('usb');
tWifi.onclick = () => switchTransport('wifi');

async function ensureDecoder(codec) {
  if (decoder || !codec || hevcFailed) return !!decoder;
  const cfg = { codec, optimizeForLatency: true, hevc: { format: 'annexb' } };
  if ('VideoDecoder' in window) {
    try {
      const ok = await VideoDecoder.isConfigSupported(cfg);
      if (!ok.supported) throw new Error('unsupported');
    } catch (e) {
      hevcFailed = true;
      notice = 'browser cannot decode HEVC (' + codec + ') -> open /stream.h265 in mpv/VLC';
      setStat(notice);
      hideWait();
      return false;
    }
    decoder = new VideoDecoder({
      output: (f) => {
        if (frameEl.width !== f.displayWidth || frameEl.height !== f.displayHeight) {
          frameEl.width = f.displayWidth; frameEl.height = f.displayHeight;
        }
        fctx.drawImage(f, 0, 0);
        f.close();
        const now = performance.now();
        drawn += 1;
        if (!drawnT0) drawnT0 = now;
        else if (now - drawnT0 >= 1000) { drawnFps = drawn * 1000 / (now - drawnT0); drawn = 0; drawnT0 = now; }
        layout();
        hideWait();
      },
      error: (e) => {
        errCount += 1;
        notice = 'decoder error: ' + e.message + ' (asked for a keyframe to restart, attempt ' + errCount + ')';
        setStat(notice);
        askKeyframe();
        try { decoder.close(); } catch (_) {}
        decoder = null; waitingKey = true;
      },
    });
    decoder.configure(cfg);
    waitingKey = true;
  } else {
    hevcFailed = true;
    notice = 'browser has no WebCodecs -> open /stream.h265 in mpv/VLC';
    setStat(notice);
    hideWait();
  }
  return !!decoder;
}

async function feedChunk(type, payload) {
  if (type === 0x03) {
    const meta = JSON.parse(new TextDecoder().decode(payload));
    metaCodec = meta.codec;
    await ensureDecoder(metaCodec);
    return;
  }
  const isKey = type === 0x04;
  if (!decoder) {
    if (isKey && metaCodec) await ensureDecoder(metaCodec);
    if (!decoder) return;
  }
  if (waitingKey && !isKey) return;
  if (!isKey && decoder.decodeQueueSize > 8) { waitingKey = true; askKeyframe(); return; }
  waitingKey = false;
  tsUs += 33333;
  decoder.decode(new EncodedVideoChunk({
    type: isKey ? 'key' : 'delta', timestamp: tsUs, data: payload,
  }));
}

let framesRunning = false;
async function runFrames() {
  if (framesRunning) return;
  framesRunning = true;
  try {
    for (;;) {
      let res = null;
      try { res = await fetch('/frames'); } catch (e) { res = null; }
      if (!res || !res.ok) {
        if (expectVideo && !waitOn) showWait('waiting for the stream...');
        await new Promise(r => setTimeout(r, 1500));
        continue;
      }
      resetDecoder();
      const reader = res.body.getReader();
      let buf = new Uint8Array(0);
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          const nb = new Uint8Array(buf.length + value.length);
          nb.set(buf); nb.set(value, buf.length); buf = nb;
          for (;;) {
            if (buf.length < 5) break;
            const len = ((buf[0] << 24) | (buf[1] << 16) | (buf[2] << 8) | buf[3]) >>> 0;
            if (buf.length < 4 + len || len < 1) break;
            await feedChunk(buf[4], buf.subarray(5, 4 + len));
            buf = buf.subarray(4 + len);
          }
        }
      } catch (e) { setStat('stream dropped, reconnecting: ' + e); }
      await new Promise(r => setTimeout(r, 1200));
    }
  } finally { framesRunning = false; }
}

grab();
runFrames();
setInterval(() => { if (expectVideo && waitingKey) askKeyframe(); }, 2000);
setInterval(async () => {
  try {
    const s = await fetch('/api/status').then(r => r.json());
    for (const bb of (s.buttons || [])) buttonHold[bb.name] = bb.hold;
    expectVideo = !!s.streaming;
    if (switching) return;
    if (!sBtn.disabled) { syncStreamBtn(!!s.streaming); }
    applyTransport(s.transport);
    applyPreflight(s);
    const info0 = s.info || {};
    devNameEl.textContent = s.connected === false
      ? 'device (disconnected)'
      : (info0.product_type || 'device') + ' iOS ' + (info0.product_version || '?');
    if (ctlLocked !== !!s.controls_locked) {
      ctlLocked = !!s.controls_locked;
      cvs.style.filter = ctlLocked ? 'grayscale(55%) brightness(.72)' : '';
      cvs.style.cursor = ctlLocked ? 'not-allowed' : '';
      if (ctlLocked) setStat('screen is down (usb-only policy: USB out): controls locked, plug the cable back in to resume');
    }
    const info = s.info || {};
    const quiet = performance.now() < msgUntil;
    if (s.connected === false) {
      if (!quiet) setStat(s.status || 'disconnected');
    } else {
      let t = (info.product_type || 'device') + ' iOS ' + (info.product_version || '?');
      if (s.h265) {
        t += '  ·  stream ' + (s.h265.bytes / 1048576).toFixed(1) + ' MB / '
           + s.h265.kbps + ' kbps' + (s.h265.path ? '  ·  ' + s.h265.path : '');
      }
      t += '  ·  decode ' + drawnFps.toFixed(1) + ' fps';
      if (!quiet) setStat(t + '  ·  controls ' + elements.length);
    }
  } catch (e) {}
}, 2000);
