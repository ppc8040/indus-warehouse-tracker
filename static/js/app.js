/* ═══════════════════════════════════════════════════════════
   INDUS WAREHOUSE TRACKER — Main App Logic
   ═══════════════════════════════════════════════════════════ */
'use strict';

// ── State ──
let currentPallet = null;   // pallet data from last lookup
let codeReader = null;       // ZXing barcode reader
let scanning = false;
let deviceId = localStorage.getItem('deviceId') || (() => {
  const id = 'DEV-' + Math.random().toString(36).substring(2, 10).toUpperCase();
  localStorage.setItem('deviceId', id);
  return id;
})();

// ── Screen switching ──
function showScreen(id) {
  document.querySelectorAll('.scr').forEach(s => s.classList.remove('on'));
  const el = document.getElementById(id);
  if (el) el.classList.add('on');
}

function goScan() {
  currentPallet = null;
  stopScanner();
  showScreen('scrScan');
  document.getElementById('manualBox').classList.add('hide');
  document.getElementById('manualHU').value = '';
  document.getElementById('notesInput').value = '';
}

// ── Toast notifications ──
function toast(msg, type = 'info', duration = 4000) {
  const box = document.getElementById('toastBox');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => { el.remove(); }, duration);
}

// ── Spinner ──
function showSpinner(msg) {
  const sp = document.getElementById('spinner');
  sp.querySelector('.spin-txt').textContent = msg || 'Loading...';
  sp.classList.add('on');
}
function hideSpinner() {
  document.getElementById('spinner').classList.remove('on');
}

// ── Online/Offline status ──
function updateOnlineStatus() {
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  if (navigator.onLine) {
    dot.className = 'dot';
    txt.textContent = 'Online';
    // Try to sync pending events
    syncPendingEvents();
  } else {
    dot.className = 'dot off';
    txt.textContent = 'Offline';
  }
}
window.addEventListener('online', updateOnlineStatus);
window.addEventListener('offline', updateOnlineStatus);
document.addEventListener('DOMContentLoaded', updateOnlineStatus);

function updatePendingBadge() {
  OfflineQueue.count().then(n => {
    const badge = document.getElementById('pendingBadge');
    if (n > 0) {
      badge.textContent = `${n} pending`;
      badge.classList.add('show');
    } else {
      badge.classList.remove('show');
    }
  });
}
setInterval(updatePendingBadge, 5000);
document.addEventListener('DOMContentLoaded', updatePendingBadge);


// ═══════════════════════════════════════════════════════════
//  BARCODE SCANNING (ZXing)
// ═══════════════════════════════════════════════════════════

function startScanner() {
  const video = document.getElementById('camVideo');
  const hint = document.getElementById('camHint');
  const btn = document.getElementById('btnStartScan');

  if (scanning) { stopScanner(); return; }

  // Try to use ZXing BrowserMultiFormatReader
  if (typeof ZXing === 'undefined') {
    toast('Camera scanner not loaded. Use manual entry.', 'warn');
    showManualEntry();
    return;
  }

  try {
    codeReader = new ZXing.BrowserMultiFormatReader();
  } catch (e) {
    toast('Scanner init failed. Use manual entry.', 'warn');
    showManualEntry();
    return;
  }

  scanning = true;
  btn.innerHTML = '<span class="btn-icon">⏹</span> STOP SCANNER';
  hint.textContent = 'Scanning... hold barcode in the frame';

  codeReader.decodeFromVideoDevice(null, video, (result, err) => {
    if (result) {
      const code = result.getText().trim();
      if (code) {
        stopScanner();
        hint.textContent = `Scanned: ${code}`;
        lookupPallet(code);
      }
    }
    // Ignore errors (NotFoundException is normal during scanning)
  }).catch(e => {
    console.error('Camera error:', e);
    scanning = false;
    btn.innerHTML = '<span class="btn-icon">📷</span> TAP TO SCAN';
    hint.textContent = 'Camera not available. Use manual entry.';
    toast('Camera access denied or not available.', 'fail');
    showManualEntry();
  });
}

function stopScanner() {
  if (codeReader) {
    try { codeReader.reset(); } catch (e) { /* ignore */ }
  }
  scanning = false;
  const btn = document.getElementById('btnStartScan');
  if (btn) btn.innerHTML = '<span class="btn-icon">📷</span> TAP TO SCAN';
}

function showManualEntry() {
  document.getElementById('manualBox').classList.remove('hide');
  document.getElementById('manualHU').focus();
}

function manualLookup() {
  const val = document.getElementById('manualHU').value.trim();
  if (!val) { toast('Enter a barcode first.', 'warn'); return; }
  lookupPallet(val);
}


// ═══════════════════════════════════════════════════════════
//  PALLET LOOKUP
// ═══════════════════════════════════════════════════════════

async function lookupPallet(hu) {
  showSpinner('Looking up pallet...');

  try {
    const resp = await fetch('/api/lookup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({handling_unit: hu}),
    });
    const data = await resp.json();
    hideSpinner();

    if (!data.success) {
      toast(data.message, 'fail', 5000);
      return;
    }

    currentPallet = data.data;
    showPalletDetails(currentPallet);
    showScreen('scrAction');

  } catch (e) {
    hideSpinner();
    console.error('Lookup error:', e);
    toast('Network error. Check connection or try manual entry.', 'fail');
  }
}

function showPalletDetails(p) {
  document.getElementById('huDisplay').textContent = p.handling_unit;
  document.getElementById('dIndus').textContent = p.indus_part || '—';
  document.getElementById('dCust').textContent = p.customer_part || '—';
  document.getElementById('dQty').textContent = p.pallet_qty || '—';
  document.getElementById('dJob').textContent = p.job_no || '—';
  document.getElementById('dDesc').textContent = p.description || '—';

  // Status tag
  const tag = document.getElementById('statusTag');
  const status = (p.current_status || '').toUpperCase();
  if (status === 'IN_WAREHOUSE') {
    tag.className = 'st-tag in';
    tag.textContent = 'IN WAREHOUSE';
  } else if (status === 'OUT_OF_WAREHOUSE') {
    tag.className = 'st-tag out';
    tag.textContent = 'OUT';
  } else {
    tag.className = 'st-tag unk';
    tag.textContent = status || 'NEW';
  }
}


// ═══════════════════════════════════════════════════════════
//  MOVEMENT
// ═══════════════════════════════════════════════════════════

async function doMovement(movementType, isOverride = false, pin = '', reason = '') {
  if (!currentPallet) { toast('No pallet scanned.', 'warn'); return; }

  const notes = document.getElementById('notesInput').value.trim();
  const timestamp = new Date().toISOString();
  const idemKey = _makeIdemKey(currentPallet.handling_unit, movementType, timestamp);

  const payload = {
    handling_unit: currentPallet.handling_unit,
    movement_type: movementType,
    operator_id: 'WORKER',
    device_id: deviceId,
    notes: notes,
    is_override: isOverride,
    supervisor_pin: pin,
    override_reason: reason,
    timestamp: timestamp,
    idempotency_key: idemKey,
  };

  // If offline, queue it
  if (!navigator.onLine) {
    await OfflineQueue.add(payload);
    updatePendingBadge();
    showResult('pending', movementType, currentPallet,
      'Saved offline. Will sync when internet returns.');
    return;
  }

  showSpinner('Recording movement...');

  try {
    const resp = await fetch('/api/movement', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    hideSpinner();

    if (data.success) {
      showResult('ok', movementType, currentPallet, data.message, data.data);
    } else if (data.data && data.data.needs_sync) {
      // Server failed to write but we should queue offline
      await OfflineQueue.add(payload);
      updatePendingBadge();
      showResult('pending', movementType, currentPallet, data.message);
    } else {
      showResult('fail', movementType, currentPallet, data.message);
    }
  } catch (e) {
    hideSpinner();
    console.error('Movement error:', e);
    // Network error — queue offline
    await OfflineQueue.add(payload);
    updatePendingBadge();
    showResult('pending', movementType, currentPallet,
      'Network error. Saved locally — will sync automatically.');
  }
}

function showResult(type, movementType, pallet, message, extra = {}) {
  const banner = document.getElementById('resultBanner');
  const icon = document.getElementById('resultIcon');
  const title = document.getElementById('resultTitle');
  const msg = document.getElementById('resultMsg');
  const info = document.getElementById('resultInfo');

  const displayType = movementType === 'STOCK_IN' ? 'Stock IN' : 'Stock OUT';

  if (type === 'ok') {
    banner.className = 'banner ok';
    icon.textContent = '✓';
    title.textContent = `${displayType} — SUCCESS`;
    msg.textContent = message;
  } else if (type === 'pending') {
    banner.className = 'banner pend';
    icon.textContent = '⏳';
    title.textContent = `${displayType} — SAVED OFFLINE`;
    msg.textContent = message;
  } else {
    banner.className = 'banner fail';
    icon.textContent = '✕';
    title.textContent = `${displayType} — FAILED`;
    msg.textContent = message;
  }

  // Detail rows
  const now = new Date().toLocaleString();
  let html = '';
  html += _infoRow('Pallet', pallet.handling_unit);
  html += _infoRow('Indus Part', pallet.indus_part);
  html += _infoRow('Customer Part', pallet.customer_part);
  html += _infoRow('Qty', pallet.pallet_qty);
  html += _infoRow('Job No', pallet.job_no);
  html += _infoRow('Time', now);
  if (extra.event_id) html += _infoRow('Event ID', extra.event_id);
  if (extra.is_override) html += _infoRow('Override', 'YES');
  info.innerHTML = html;

  showScreen('scrResult');
}

function _infoRow(label, value) {
  return `<div class="row"><span class="lbl">${label}</span><span class="val">${value || '—'}</span></div>`;
}


// ═══════════════════════════════════════════════════════════
//  SUPERVISOR OVERRIDE
// ═══════════════════════════════════════════════════════════

function showOverrideModal() {
  document.getElementById('overrideModal').classList.add('on');
  document.getElementById('pinInput').value = '';
  document.getElementById('overrideReason').value = '';
  document.getElementById('pinInput').focus();
}

function closeOverride() {
  document.getElementById('overrideModal').classList.remove('on');
}

async function submitOverride() {
  const pin = document.getElementById('pinInput').value.trim();
  const reason = document.getElementById('overrideReason').value.trim();
  const movement = document.getElementById('overrideMovement').value;

  if (!pin) { toast('Enter supervisor PIN.', 'warn'); return; }
  if (!reason) { toast('Reason is required for overrides.', 'warn'); return; }

  closeOverride();
  await doMovement(movement, true, pin, reason);
}


// ═══════════════════════════════════════════════════════════
//  OFFLINE SYNC
// ═══════════════════════════════════════════════════════════

async function syncPendingEvents() {
  const events = await OfflineQueue.getAll();
  if (!events.length) return;

  console.log(`Syncing ${events.length} offline events...`);

  try {
    const resp = await fetch('/api/sync', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({events: events.map(e => e.payload)}),
    });
    const data = await resp.json();

    if (data.results) {
      let synced = 0;
      for (const r of data.results) {
        if (r.success || r.status === 'rejected_validation') {
          // Either synced or was a duplicate — remove from queue
          await OfflineQueue.removeByKey(r.idempotency_key);
          synced++;
        }
      }
      if (synced > 0) toast(`Synced ${synced} offline event(s).`, 'ok');
    }
    updatePendingBadge();
  } catch (e) {
    console.warn('Sync failed:', e);
  }
}

// Auto-sync every 30 seconds when online
setInterval(() => {
  if (navigator.onLine) syncPendingEvents();
}, 30000);


// ═══════════════════════════════════════════════════════════
//  HELPERS
// ═══════════════════════════════════════════════════════════

function _makeIdemKey(hu, mt, ts) {
  // Simple client-side hash to match server-side idempotency
  const raw = `${hu}:${mt}:${ts}`;
  let hash = 0;
  for (let i = 0; i < raw.length; i++) {
    const c = raw.charCodeAt(i);
    hash = ((hash << 5) - hash) + c;
    hash |= 0;
  }
  return 'CL-' + Math.abs(hash).toString(36).toUpperCase().padStart(8, '0');
}
