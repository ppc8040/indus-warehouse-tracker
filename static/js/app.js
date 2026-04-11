/* ═══════════════════════════════════════════════════════════
   INDUS WAREHOUSE TRACKER — Single-Scan Architecture

   Pallet label has one barcode (Code 128): Handling Unit (18–20 digits)

   Worker scans the barcode (or types HU manually) → taps LOOKUP
   → app fetches Material + Qty from SAP API → STOCK IN or OUT.

   QR codes are no longer scanned. Camera is barcode-only (no QR).
   ═══════════════════════════════════════════════════════════ */
'use strict';

// ── HU Validation (same rules as backend validators.py) ──
// 18-20 digits, all numeric, no special chars
const HU_PATTERN = /^\d{18,20}$/;

// ── State ──
let scanned = {
  handling_unit: '',
  indus_part: '',
  customer_part: '',  // always '' in the new workflow, kept for payload compatibility
  pallet_qty: '',
};
let scanner = null;
let scannerRunning = false;
let sapLookupInProgress = false;

// ── Submit lock: prevents double-tap duplicates on STOCK IN / STOCK OUT ──
let movementInFlight = false;

// ── Location modal state ──
let _pendingMovementType = '';   // 'STOCK_IN' or 'STOCK_OUT'
let _selectedLocation = '';      // chosen location value

let deviceId = localStorage.getItem('deviceId') || (() => {
  const id = 'DEV-' + Math.random().toString(36).substring(2, 10).toUpperCase();
  localStorage.setItem('deviceId', id);
  return id;
})();

// ── Screen switching ──
function showScreen(id) {
  document.querySelectorAll('.scr').forEach(s => s.classList.remove('on'));
  document.getElementById(id).classList.add('on');
}

function goScan() {
  resetScans();
  stopScanner();
  showScreen('scrScan');
}

function goAction() {
  document.getElementById('huDisplay').textContent = scanned.handling_unit;
  document.getElementById('dIndus').textContent = scanned.indus_part || '—';
  document.getElementById('dCust').textContent = scanned.customer_part || '—';
  document.getElementById('dQty').textContent = scanned.pallet_qty || '—';
  stopScanner();
  showScreen('scrAction');
}

// ── Toast ──
function toast(msg, type = 'info', duration = 4000) {
  const box = document.getElementById('toastBox');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ── Spinner ──
function showSpinner(msg) {
  const sp = document.getElementById('spinner');
  sp.querySelector('.spin-txt').textContent = msg || 'Loading...';
  sp.classList.add('on');
}
function hideSpinner() { document.getElementById('spinner').classList.remove('on'); }

// ── Online/Offline ──
function updateOnlineStatus() {
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  if (navigator.onLine) {
    dot.className = 'dot'; txt.textContent = 'Online';
    syncPendingEvents();
  } else {
    dot.className = 'dot off'; txt.textContent = 'Offline';
  }
}
window.addEventListener('online', updateOnlineStatus);
window.addEventListener('offline', updateOnlineStatus);
document.addEventListener('DOMContentLoaded', updateOnlineStatus);

function updatePendingBadge() {
  OfflineQueue.count().then(n => {
    const badge = document.getElementById('pendingBadge');
    if (n > 0) { badge.textContent = `${n} pending`; badge.classList.add('show'); }
    else { badge.classList.remove('show'); }
  });
}
setInterval(updatePendingBadge, 5000);
document.addEventListener('DOMContentLoaded', updatePendingBadge);


// ═══════════════════════════════════════════════════════════
//  BARCODE CAMERA SCANNER — barcode formats only, NO QR codes
// ═══════════════════════════════════════════════════════════

function toggleScanner() {
  if (scannerRunning) { stopScanner(); }
  else { startScanner(); }
}

function startScanner() {
  const btn = document.getElementById('btnToggleScan');

  if (typeof Html5Qrcode === 'undefined') {
    toast('Camera library not loaded. Check internet.', 'fail');
    return;
  }

  // Barcode formats ONLY — QR_CODE is intentionally excluded
  const formats = [
    Html5QrcodeSupportedFormats.CODE_128,
    Html5QrcodeSupportedFormats.CODE_39,
    Html5QrcodeSupportedFormats.EAN_13,
    Html5QrcodeSupportedFormats.EAN_8,
    Html5QrcodeSupportedFormats.ITF,
  ];

  scanner = new Html5Qrcode('scannerContainer', {
    formatsToSupport: formats,
    verbose: false,
  });

  const scanConfig = {
    fps: 15,
    qrbox: { width: 340, height: 120 },
    aspectRatio: 1.7777,
    disableFlip: false,
  };

  scanner.start(
    { facingMode: 'environment' },
    scanConfig,
    onBarcodeScanSuccess,
    () => {}
  ).then(() => {
    scannerRunning = true;
    btn.innerHTML = '<span class="btn-icon">⏹</span> STOP CAMERA';
    toast('Camera active — point at the barcode', 'info', 2000);
  }).catch(err => {
    console.error('Camera error:', err);
    toast('Camera access denied or not available.', 'fail');
  });
}

function stopScanner() {
  const btn = document.getElementById('btnToggleScan');
  if (scanner && scannerRunning) {
    scanner.stop().then(() => {
      scannerRunning = false;
      btn.innerHTML = '<span class="btn-icon">&#128247;</span> START CAMERA';
    }).catch(() => {
      scannerRunning = false;
      btn.innerHTML = '<span class="btn-icon">&#128247;</span> START CAMERA';
    });
  }
}

// Called when camera successfully reads a barcode
function onBarcodeScanSuccess(decodedText) {
  const val = decodedText.trim();
  if (!val) return;

  // Only accept values that look like an HU (18-20 digits, starts with 1000)
  if (!HU_PATTERN.test(val) || !val.startsWith('1000')) {
    toast('Scanned code is not a valid HU barcode. Point at the barcode only.', 'warn', 3000);
    return;
  }

  // If an HU is already loaded, do not overwrite without reset
  if (scanned.handling_unit) {
    toast('HU already loaded. Tap RESET to scan a different one.', 'warn', 3000);
    return;
  }

  stopScanner();
  if (navigator.vibrate) navigator.vibrate(100);

  // Populate the manual input field so the user can see what was scanned
  const inp = document.getElementById('huManualInput');
  if (inp) inp.value = val;

  // Auto-trigger the SAP lookup
  _doSAPLookup(val);
}


// ═══════════════════════════════════════════════════════════
//  HU LOOKUP — manual entry + SAP fetch
// ═══════════════════════════════════════════════════════════

// Called by the LOOKUP button (manual entry path)
function confirmHULookup() {
  const raw = document.getElementById('huManualInput').value.trim();
  if (!raw) {
    toast('Enter or scan the HU number first.', 'warn');
    return;
  }
  if (!HU_PATTERN.test(raw)) {
    toast('Invalid HU. Must be 18\u201320 digits (numbers only, no letters or special characters).', 'warn', 5000);
    return;
  }
  if (!raw.startsWith('1000')) {
    toast('Invalid HU. Must start with 1000.', 'warn', 4000);
    return;
  }
  _doSAPLookup(raw);
}

// Core SAP lookup — shared by both camera scan and manual entry paths
async function _doSAPLookup(hu) {
  if (sapLookupInProgress) return;
  sapLookupInProgress = true;

  // Block the lookup button while in progress
  const btn = document.getElementById('btnLookupHU');
  if (btn) btn.disabled = true;

  showSpinner('Looking up in SAP…');

  try {
    const resp = await fetch('/api/lookup-hu', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ handling_unit: hu }),
    });

    const data = await resp.json();

    if (!data.success) {
      toast(data.message || 'SAP lookup failed. Try again.', 'fail', 6000);
      _resetLookupState();
      return;
    }

    // Populate state from SAP response
    scanned.handling_unit = data.handling_unit;
    scanned.indus_part    = data.indus_part;
    scanned.pallet_qty    = data.pallet_qty;
    scanned.customer_part = '';  // not available from SAP HU item

    updateScanDisplay();
    toast(`Found: ${data.indus_part} × ${data.pallet_qty}`, 'ok', 3000);

    // Enable the PROCEED button
    const proceedBtn = document.getElementById('btnProceed');
    if (proceedBtn) proceedBtn.disabled = false;

  } catch (err) {
    console.error('SAP lookup network error:', err);
    toast('Cannot reach server. Check network and try again.', 'fail', 6000);
    _resetLookupState();
  } finally {
    hideSpinner();
    sapLookupInProgress = false;
    if (btn) btn.disabled = false;
  }
}

function _resetLookupState() {
  // Do NOT clear the input so the user can try again without re-typing
  scanned.handling_unit = '';
  scanned.indus_part = '';
  scanned.pallet_qty = '';
  scanned.customer_part = '';
  updateScanDisplay();
  const proceedBtn = document.getElementById('btnProceed');
  if (proceedBtn) proceedBtn.disabled = true;
}


// ═══════════════════════════════════════════════════════════
//  SCAN DISPLAY + RESET
// ═══════════════════════════════════════════════════════════

function updateScanDisplay() {
  _updateField('hu', scanned.handling_unit);
  _updateField('indus', scanned.indus_part);
  _updateField('qty', scanned.pallet_qty);

  const ready = !!(scanned.handling_unit && scanned.indus_part && scanned.pallet_qty);
  const proceedBtn = document.getElementById('btnProceed');
  if (proceedBtn) proceedBtn.disabled = !ready;
}

function _updateField(key, val) {
  const statusEl = document.getElementById(`st-${key}`);
  const valueEl  = document.getElementById(`val-${key}`);
  if (!statusEl || !valueEl) return;
  if (val) {
    statusEl.textContent = '\u2713 FOUND';
    statusEl.className   = 'sf-status scanned';
    valueEl.textContent  = val;
    valueEl.className    = 'sf-value scanned';
  } else {
    statusEl.textContent = 'PENDING';
    statusEl.className   = 'sf-status';
    valueEl.textContent  = '\u2014';
    valueEl.className    = 'sf-value';
  }
}

function resetScans() {
  scanned = { handling_unit: '', indus_part: '', customer_part: '', pallet_qty: '' };
  sapLookupInProgress = false;

  const inp = document.getElementById('huManualInput');
  if (inp) inp.value = '';

  updateScanDisplay();
}


// ═══════════════════════════════════════════════════════════
//  MOVEMENT SUBMISSION — unchanged logic
// ═══════════════════════════════════════════════════════════

function promptLocation(type) {
  if (!scanned.handling_unit || !scanned.indus_part) {
    toast('Look up the HU first before recording a movement.', 'warn');
    return;
  }
  _pendingMovementType = type;
  _selectedLocation = '';
  const modal = document.getElementById('locationModal');
  const title = document.getElementById('locationModalTitle');
  const opts  = document.querySelectorAll('.loc-btn');
  title.textContent = type === 'STOCK_IN' ? 'Select IN Location' : 'Select OUT Location';
  opts.forEach(b => b.classList.remove('selected'));
  document.getElementById('btnConfirmLocation').disabled = true;
  modal.classList.add('on');
}

function selectLocation(val) {
  _selectedLocation = val;
  document.querySelectorAll('.loc-btn').forEach(b => {
    b.classList.toggle('selected', b.dataset.loc === val);
  });
  document.getElementById('btnConfirmLocation').disabled = false;
}

function confirmLocation() {
  if (!_selectedLocation) return;
  document.getElementById('locationModal').classList.remove('on');
  doMovement(_pendingMovementType, _selectedLocation);
}

function cancelLocation() {
  document.getElementById('locationModal').classList.remove('on');
  _pendingMovementType = '';
  _selectedLocation    = '';
}

function doMovement(type, location) {
  if (movementInFlight) {
    toast('Movement already in progress. Please wait.', 'warn');
    return;
  }
  if (!scanned.handling_unit) {
    toast('No HU loaded. Scan the barcode first.', 'warn');
    return;
  }

  movementInFlight = true;
  showSpinner(type === 'STOCK_IN' ? 'Recording Stock IN…' : 'Recording Stock OUT…');

  const idempotencyKey = `${scanned.handling_unit}-${type}-${Date.now()}`;
  const payload = {
    handling_unit:   scanned.handling_unit,
    movement_type:   type,
    indus_part:      scanned.indus_part,
    customer_part:   '',  // not available in single-scan workflow
    pallet_qty:      scanned.pallet_qty,
    job_no:          '',
    operator_id:     'WORKER',
    device_id:       deviceId,
    location:        location,
    timestamp:       new Date().toISOString(),
    idempotency_key: idempotencyKey,
    is_override:     false,
  };

  if (!navigator.onLine) {
    _queueOffline(payload);
    return;
  }

  fetch('/api/movement', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  .then(r => r.json())
  .then(data => {
    hideSpinner();
    movementInFlight = false;
    if (data.success) {
      toast(data.message || `${type} recorded.`, 'ok', 4000);
      setTimeout(goScan, 1800);
    } else if (data.status === 'rejected_duplicate') {
      toast(data.message || 'Duplicate — already recorded.', 'warn', 6000);
      setTimeout(goScan, 2500);
    } else if (data.status === 'rejected_transition' || data.status === 'rejected_validation') {
      _showOverrideOption(data.message, payload);
    } else {
      toast(data.message || 'Server error. Saving offline.', 'fail', 5000);
      _queueOffline(payload);
    }
  })
  .catch(() => {
    hideSpinner();
    movementInFlight = false;
    toast('Network error. Movement saved for later sync.', 'warn', 5000);
    _queueOffline(payload);
  });
}

function _queueOffline(payload) {
  hideSpinner();
  movementInFlight = false;
  OfflineQueue.add(payload).then(() => {
    updatePendingBadge();
    toast('Saved offline — will sync when back online.', 'warn', 5000);
    setTimeout(goScan, 2000);
  });
}

function _showOverrideOption(serverMessage, originalPayload) {
  const modal = document.getElementById('overrideModal');
  const msg   = document.getElementById('overrideMessage');
  if (!modal) { toast(serverMessage, 'fail', 7000); return; }
  msg.textContent = serverMessage;
  modal.classList.add('on');
  modal._overridePayload = originalPayload;
}

function confirmOverride() {
  const modal = document.getElementById('overrideModal');
  const pin   = document.getElementById('overridePinInput').value.trim();
  if (!pin) { toast('Enter supervisor PIN.', 'warn'); return; }

  const payload = {
    ...modal._overridePayload,
    is_override: true,
    supervisor_pin: pin,
    override_reason: 'Supervisor override at worker terminal',
  };
  modal.classList.remove('on');
  movementInFlight = false;
  doMovement(payload.movement_type, payload.location);
  // Re-merge override fields
  setTimeout(() => {
    // The doMovement above will rebuild payload without override flag,
    // so submit directly here instead:
    movementInFlight = true;
    showSpinner('Submitting override…');
    fetch('/api/movement', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    .then(r => r.json())
    .then(data => {
      hideSpinner();
      movementInFlight = false;
      if (data.success) {
        toast(data.message || 'Override accepted.', 'ok', 4000);
        setTimeout(goScan, 1800);
      } else {
        toast(data.message || 'Override failed.', 'fail', 6000);
      }
    })
    .catch(() => {
      hideSpinner();
      movementInFlight = false;
      toast('Network error during override.', 'fail', 5000);
    });
  }, 0);
}

function cancelOverride() {
  const modal = document.getElementById('overrideModal');
  if (modal) modal.classList.remove('on');
  movementInFlight = false;
}


// ═══════════════════════════════════════════════════════════
//  OFFLINE SYNC — unchanged logic
// ═══════════════════════════════════════════════════════════

async function syncPendingEvents() {
  const pending = await OfflineQueue.getAll();
  if (!pending.length) return;

  toast(`Syncing ${pending.length} offline event(s)…`, 'info', 3000);
  const payloads = pending.map(r => r.payload);

  try {
    const resp = await fetch('/api/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: payloads }),
    });
    const data = await resp.json();
    if (data.results) {
      for (const r of data.results) {
        if (r.success || r.status === 'rejected_duplicate') {
          await OfflineQueue.removeByKey(r.idempotency_key);
        }
      }
    }
    updatePendingBadge();
    toast('Offline events synced.', 'ok', 3000);
  } catch (e) {
    console.error('Sync failed:', e);
  }
}
