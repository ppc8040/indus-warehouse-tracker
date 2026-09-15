/* ═══════════════════════════════════════════════════════════
   INDUS WAREHOUSE TRACKER — Single-Scan Architecture

   Worker scans the barcode (or types HU manually) → taps LOOKUP
   → app fetches Material + Qty from SAP API → STOCK IN or OUT.

   If SAP returns HU_NOT_FOUND, a supervisor override modal appears
   where the supervisor enters their PIN. On success the worker
   manually enters the Indus Part and Qty. Movement is then posted
   normally with is_override=true and the raw supervisor PIN.

   STOCK_OUT: after the worker taps STOCK OUT and selects a location,
   a destination picker appears before the movement is posted. A
   destination MUST be selected — posting without a destination is
   not permitted. The list of destinations is loaded once from
   GET /api/destinations on DOMContentLoaded and cached in _destinations[].

   QR codes are no longer scanned. Camera is barcode-only.
   ═══════════════════════════════════════════════════════════ */
'use strict';

// ── HU Validation (same rules as backend validators.py) ──
const HU_PATTERN = /^\d{18,20}$/;

// ── State ──
let scanned = {
  handling_unit: '',
  indus_part: '',
  customer_part: '',
  pallet_qty: '',
  hu_batch: '',           // ← ADD
  hu_creation_date: '',   // ← ADD
};
let scanner = null;
let scannerRunning = false;
let sapLookupInProgress = false;

// Tracks whether the current scanned data came from a SAP-not-found
// supervisor override (used when building the movement payload).
let _isNotFoundOverride = false;
let _notFoundOverrideHU = '';  // HU being overridden (held while modal is open)

// Supervisor PIN entered in the NFO modal — sent as supervisor_pin in the
// movement POST for SAP-not-found overrides. Cleared after use or on reset.
let _nfoSupervisorPin = '';

// ── Submit lock: prevents double-tap duplicates on STOCK IN / STOCK OUT ──
let movementInFlight = false;

// ── Location modal state ──
let _pendingMovementType = '';   // 'STOCK_IN' or 'STOCK_OUT'
let _selectedLocation = '';      // chosen location value

// ── Destination picker state (STOCK_OUT only) ──
let _destinations = [];          // loaded once from /api/destinations
let _selectedDestination = '';   // chosen to_location value

// ── Pallet status from lookup (used to pre-fill from_location display) ──
let _lookupLastLocation = '';    // last_location returned by /api/lookup-hu
let _lookupCurrentStatus = '';   // current_status returned by /api/lookup-hu

// ── Device identity ──
// deviceId is a stable random ID stored in localStorage.
// Used as the operator identifier in all movement payloads.
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
  document.getElementById('dIndus').textContent = scanned.indus_part || '\u2014';
  document.getElementById('dCust').textContent = scanned.customer_part || '\u2014';
  document.getElementById('dQty').textContent = scanned.pallet_qty || '\u2014';
  // After existing dQty assignment:
  const dBatch   = document.getElementById('dBatch');
  const dCreated = document.getElementById('dCreated');
  const detBatch   = document.getElementById('det-batch');
  const detCreated = document.getElementById('det-created');
   
  const batch   = document.getElementById('st-batch')?.textContent   || '';
  const created = document.getElementById('st-created')?.textContent || '';
   
  if (dBatch)   dBatch.textContent   = batch   || '\u2014';
  if (dCreated) dCreated.textContent = created ? _fmtDubaiTime(created) : '\u2014';
   
  if (detBatch)   detBatch.style.display   = batch   ? '' : 'none';
  if (detCreated) detCreated.style.display = created ? '' : 'none';
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

document.addEventListener('DOMContentLoaded', () => {
  updateOnlineStatus();
  _loadDestinations();
});

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
//  DESTINATION LIST — loaded once on boot from /api/destinations
// ═══════════════════════════════════════════════════════════

function _loadDestinations() {
  fetch('/api/destinations')
    .then(r => r.json())
    .then(data => {
      if (data.destinations && data.destinations.length) {
        _destinations = data.destinations;
        _buildDestinationOptions();
      }
    })
    .catch(err => {
      console.warn('Could not load destinations:', err);
    });
}

function _buildDestinationOptions() {
  const grid = document.getElementById('destGrid');
  if (!grid || !_destinations.length) return;
  grid.innerHTML = '';
  _destinations.forEach(dest => {
    const btn = document.createElement('button');
    btn.className = 'dest-btn';
    btn.dataset.dest = dest;
    btn.textContent = dest;
    btn.onclick = () => selectDestination(dest);
    grid.appendChild(btn);
  });
}


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
    btn.innerHTML = '<span class="btn-icon">\u23f9</span> STOP CAMERA';
    toast('Camera active \u2014 point at the barcode', 'info', 2000);
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

function onBarcodeScanSuccess(decodedText) {
  const val = decodedText.trim();
  if (!val) return;

  if (!HU_PATTERN.test(val) || (!val.startsWith('10') && !val.startsWith('11'))) {
    toast('Scanned code is not a valid HU barcode. Point at the barcode only.', 'warn', 3000);
    return;
  }

  if (scanned.handling_unit) {
    toast('HU already loaded. Tap RESET to scan a different one.', 'warn', 3000);
    return;
  }

  stopScanner();
  if (navigator.vibrate) navigator.vibrate(100);

  const inp = document.getElementById('huManualInput');
  if (inp) inp.value = val;

  _doSAPLookup(val);
}


// ═══════════════════════════════════════════════════════════
//  HU LOOKUP — manual entry + SAP fetch
// ═══════════════════════════════════════════════════════════

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
  if (!raw.startsWith('10') && !raw.startsWith('11')) {
    toast('Invalid HU. Must start with 10 or 11.', 'warn', 4000);
    return;
  }
  _doSAPLookup(raw);
}

async function _doSAPLookup(hu) {
  if (sapLookupInProgress) return;
  sapLookupInProgress = true;

  const btn = document.getElementById('btnLookupHU');
  if (btn) btn.disabled = true;

  showSpinner('Looking up\u2026');

  try {
    const resp = await fetch('/api/lookup-hu', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ handling_unit: hu }),
    });

    const data = await resp.json();

    if (data.success) {
      scanned.handling_unit = data.handling_unit;
      scanned.indus_part    = data.indus_part;
      scanned.pallet_qty    = data.pallet_qty;
      scanned.customer_part = '';
      scanned.hu_batch         = data.hu_batch         || '';
      scanned.hu_creation_date = data.hu_creation_date || '';
      _isNotFoundOverride   = false;
      _nfoSupervisorPin     = '';

      _lookupLastLocation  = data.last_location    || '';
      _lookupCurrentStatus = data.current_status   || '';

      updateScanDisplay();

      const sourceLabel = data.source === 'pallet_status' ? 'Warehouse record' : 'SAP';
      toast(`Found (${sourceLabel}): ${data.indus_part} \u00d7 ${data.pallet_qty}`, 'ok', 3000);

      const proceedBtn = document.getElementById('btnProceed');
      if (proceedBtn) proceedBtn.disabled = false;

    } else if (data.error_code === 'HU_NOT_FOUND') {
      _lookupLastLocation  = '';
      _lookupCurrentStatus = '';
      _openNotFoundOverrideModal(hu);

    } else {
      toast(data.message || 'Lookup failed. Try again.', 'fail', 6000);
      _resetLookupState();
    }

  } catch (err) {
    console.error('Lookup network error:', err);
    toast('Cannot reach server. Check network and try again.', 'fail', 6000);
    _resetLookupState();
  } finally {
    hideSpinner();
    sapLookupInProgress = false;
    if (btn) btn.disabled = false;
  }
}

function _resetLookupState() {
  scanned.handling_unit = '';
  scanned.indus_part    = '';
  scanned.pallet_qty    = '';
  scanned.customer_part = '';
  _isNotFoundOverride   = false;
  _nfoSupervisorPin     = '';
  _lookupLastLocation   = '';
  _lookupCurrentStatus  = '';
  updateScanDisplay();
  const proceedBtn = document.getElementById('btnProceed');
  if (proceedBtn) proceedBtn.disabled = true;
}


// ═══════════════════════════════════════════════════════════
//  SAP-NOT-FOUND SUPERVISOR OVERRIDE MODAL
// ═══════════════════════════════════════════════════════════

function _openNotFoundOverrideModal(hu) {
  _notFoundOverrideHU = hu;
  _nfoSupervisorPin   = '';

  const huSpan = document.getElementById('nfoHUDisplay');
  if (huSpan) huSpan.textContent = hu;

  _showNFOStep('pin');

  const pinInput  = document.getElementById('nfoPinInput');
  const partInput = document.getElementById('nfoIndusPart');
  const qtyInput  = document.getElementById('nfoQty');
  if (pinInput)  { pinInput.value  = ''; pinInput.disabled  = false; }
  if (partInput) partInput.value = '';
  if (qtyInput)  qtyInput.value  = '';

  const pinErr    = document.getElementById('nfoPinError');
  const verifyBtn = document.getElementById('nfoBtnVerifyPin');
  if (pinErr)    pinErr.textContent = '';
  if (verifyBtn) verifyBtn.disabled = false;

  document.getElementById('sapNotFoundModal').classList.add('on');
}

function _showNFOStep(step) {
  const pinStep    = document.getElementById('nfoStepPin');
  const fieldsStep = document.getElementById('nfoStepFields');
  if (pinStep)    pinStep.style.display    = (step === 'pin')    ? '' : 'none';
  if (fieldsStep) fieldsStep.style.display = (step === 'fields') ? '' : 'none';
}

async function nfoVerifyPin() {
  const pinInput  = document.getElementById('nfoPinInput');
  const pinErr    = document.getElementById('nfoPinError');
  const verifyBtn = document.getElementById('nfoBtnVerifyPin');
  const pin = pinInput ? pinInput.value.trim() : '';

  if (!pin) {
    if (pinErr) pinErr.textContent = 'Enter the supervisor PIN.';
    return;
  }

  if (verifyBtn) verifyBtn.disabled = true;
  if (pinErr)    pinErr.textContent = '';

  try {
    const resp = await fetch('/api/validate-pin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin }),
    });
    const data = await resp.json();

    if (data.valid) {
      _nfoSupervisorPin = pin;
      _showNFOStep('fields');
    } else {
      if (pinErr) pinErr.textContent = data.message || 'Incorrect PIN. Please try again.';
      if (pinInput) { pinInput.value = ''; pinInput.focus(); }
      if (verifyBtn) verifyBtn.disabled = false;
    }
  } catch (e) {
    if (pinErr)    pinErr.textContent = 'Network error. Try again.';
    if (verifyBtn) verifyBtn.disabled = false;
  }
}

function nfoConfirmFields() {
  const partInput = document.getElementById('nfoIndusPart');
  const qtyInput  = document.getElementById('nfoQty');
  const fieldsErr = document.getElementById('nfoFieldsError');

  const rawPart = partInput ? partInput.value.trim().toUpperCase() : '';
  const rawQty  = qtyInput  ? qtyInput.value.trim()  : '';

  if (!rawPart) {
    if (fieldsErr) fieldsErr.textContent = 'Indus Part (Material) is required.';
    return;
  }
  const qtyNum = parseInt(rawQty, 10);
  if (!rawQty || isNaN(qtyNum) || qtyNum < 1) {
    if (fieldsErr) fieldsErr.textContent = 'Enter a valid quantity (whole number, at least 1).';
    return;
  }
  if (!_nfoSupervisorPin) {
    if (fieldsErr) fieldsErr.textContent = 'Session lost. Close this and re-enter the supervisor PIN.';
    return;
  }

  scanned.handling_unit = _notFoundOverrideHU;
  scanned.indus_part    = rawPart;
  scanned.pallet_qty    = String(qtyNum);
  scanned.customer_part = '';
  _isNotFoundOverride   = true;

  document.getElementById('sapNotFoundModal').classList.remove('on');
  _notFoundOverrideHU = '';

  updateScanDisplay();

  const proceedBtn = document.getElementById('btnProceed');
  if (proceedBtn) proceedBtn.disabled = false;

  toast(
    `Override set: ${rawPart} \u00d7 ${qtyNum}. Choose STOCK IN or STOCK OUT.`,
    'warn',
    5000
  );
}

function nfoCancelOverride() {
  document.getElementById('sapNotFoundModal').classList.remove('on');
  _notFoundOverrideHU = '';
  _nfoSupervisorPin   = '';
  _resetLookupState();
}


// ═══════════════════════════════════════════════════════════
//  SCAN DISPLAY + RESET
// ═══════════════════════════════════════════════════════════

function _fmtDubaiTime(iso) {
  if (!iso) return '\u2014';
  try {
    return new Date(iso).toLocaleString('en-GB', {
      timeZone: 'Asia/Dubai',
      day: '2-digit', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit'
    });
  } catch { return iso; }
}

function updateScanDisplay() {
  _updateField('hu',    scanned.handling_unit);
  _updateField('indus', scanned.indus_part);
  _updateField('qty',   scanned.pallet_qty);
   // After existing val-qty assignment:
   const batch   = scanned.hu_batch    || '';
   const created = scanned.hu_creation_date || '';
   
   const batchEl   = document.getElementById('val-batch');
   const createdEl = document.getElementById('val-created');
   const rowBatch   = document.getElementById('row-batch');
   const rowCreated = document.getElementById('row-created');
   
   if (batchEl)   batchEl.textContent   = batch   || '\u2014';
   if (createdEl) createdEl.textContent = created ? _fmtDubaiTime(created) : '\u2014';
   
   // Show rows only when data exists
   if (rowBatch)   rowBatch.style.display   = batch   ? '' : 'none';
   if (rowCreated) rowCreated.style.display = created ? '' : 'none';
   
   // Persist to hidden state spans
   const stBatch   = document.getElementById('st-batch');
   const stCreated = document.getElementById('st-created');
   if (stBatch)   stBatch.textContent   = batch;
   if (stCreated) stCreated.textContent = created;

  const card = document.getElementById('sapResultCard');
  if (card) {
    const hasData = !!(scanned.handling_unit && scanned.indus_part && scanned.pallet_qty);
    card.classList.toggle('visible', hasData);
    if (hasData) {
      card.classList.toggle('override', _isNotFoundOverride);
    }
  }

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
  sapLookupInProgress  = false;
  _isNotFoundOverride  = false;
  _notFoundOverrideHU  = '';
  _nfoSupervisorPin    = '';
  _lookupLastLocation  = '';
  _lookupCurrentStatus = '';
  _selectedDestination = '';

  const inp = document.getElementById('huManualInput');
  if (inp) inp.value = '';

  updateScanDisplay();
}


// ═══════════════════════════════════════════════════════════
//  LOCATION MODAL
// ═══════════════════════════════════════════════════════════

function promptLocation(type) {
  if (!scanned.handling_unit || !scanned.indus_part) {
    toast('Look up the HU first before recording a movement.', 'warn');
    return;
  }
  _pendingMovementType = type;
  _selectedLocation    = '';
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

  if (_pendingMovementType === 'STOCK_OUT') {
    _openDestinationModal(_selectedLocation);
  } else {
    doMovement(_pendingMovementType, _selectedLocation, '');
  }
}

function cancelLocation() {
  document.getElementById('locationModal').classList.remove('on');
  _pendingMovementType = '';
  _selectedLocation    = '';
}


// ═══════════════════════════════════════════════════════════
//  DESTINATION MODAL — STOCK_OUT only
//  Destination is MANDATORY — the modal cannot be submitted
//  without selecting a destination.
// ═══════════════════════════════════════════════════════════

function _openDestinationModal(location) {
  _selectedDestination = '';

  const fromBadge = document.getElementById('destFromBadge');
  if (fromBadge) {
    const from = _lookupLastLocation || location;
    fromBadge.textContent = from || '\u2014';
  }

  document.querySelectorAll('.dest-btn').forEach(b => b.classList.remove('selected'));
  document.getElementById('btnConfirmDestination').disabled = true;
  document.getElementById('destinationModal').classList.add('on');
}

function selectDestination(val) {
  _selectedDestination = val;
  document.querySelectorAll('.dest-btn').forEach(b => {
    b.classList.toggle('selected', b.dataset.dest === val);
  });
  document.getElementById('btnConfirmDestination').disabled = false;
}

function confirmDestination() {
  if (!_selectedDestination) return;
  document.getElementById('destinationModal').classList.remove('on');
  doMovement('STOCK_OUT', _selectedLocation, _selectedDestination);
}

function cancelDestination() {
  document.getElementById('destinationModal').classList.remove('on');
  _selectedDestination = '';
}


// ═══════════════════════════════════════════════════════════
//  MANUAL QTY EDIT — optional, available after any HU lookup
//  (both SAP-found and supervisor-override HUs)
// ═══════════════════════════════════════════════════════════

function openQtyEdit() {
  const input = document.getElementById('qtyEditInput');
  const err   = document.getElementById('qtyEditError');
  if (input) input.value = scanned.pallet_qty || '';
  if (err)   err.textContent = '';
  document.getElementById('qtyEditModal').classList.add('on');
  // Auto-select the number so the worker can type immediately
  setTimeout(() => { if (input) { input.focus(); input.select(); } }, 80);
}

function confirmQtyEdit() {
  const input = document.getElementById('qtyEditInput');
  const err   = document.getElementById('qtyEditError');
  const raw   = input ? input.value.trim() : '';
  const num   = parseInt(raw, 10);

  if (!raw || isNaN(num) || num < 1 || String(num) !== raw) {
    if (err) err.textContent = 'Enter a valid whole number (at least 1).';
    return;
  }

  const prev = scanned.pallet_qty;
  scanned.pallet_qty = String(num);
  document.getElementById('qtyEditModal').classList.remove('on');
  updateScanDisplay();
  toast(`\u2713 Qty updated: ${prev} \u2192 ${num}`, 'ok', 3500);
}

function cancelQtyEdit() {
  document.getElementById('qtyEditModal').classList.remove('on');
}


// ═══════════════════════════════════════════════════════════
//  MOVEMENT SUBMISSION
// ═══════════════════════════════════════════════════════════

async function doMovement(type, location, toLocation) {
  if (movementInFlight) {
    toast('Movement already in progress. Please wait.', 'warn');
    return;
  }
  if (!scanned.handling_unit) {
    toast('No HU loaded. Scan the barcode first.', 'warn');
    return;
  }

  // Gate: do not attempt server call when offline
  if (!navigator.onLine) {
    movementInFlight = true;
    const idempotencyKey = `${scanned.handling_unit}-${type}-${Date.now()}`;
    const payload = _buildPayload(type, location, toLocation, idempotencyKey);
    _queueOffline(payload);
    return;
  }

  movementInFlight = true;
  showSpinner(type === 'STOCK_IN' ? 'Recording Stock IN\u2026' : 'Recording Stock OUT\u2026');

  const idempotencyKey = `${scanned.handling_unit}-${type}-${Date.now()}`;
  const payload = _buildPayload(type, location, toLocation, idempotencyKey);

  try {
    const resp = await fetch('/api/movement', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    // Guard: if the server returns a non-JSON body (e.g. Heroku 503 HTML
    // error page), r.json() would throw and previously caused a silent
    // fall-through to _queueOffline. We parse safely here instead.
    let data;
    const contentType = resp.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      data = await resp.json();
    } else {
      // Non-JSON response — treat as a transient server error, show message,
      // do NOT queue offline (the server received the request, it just failed).
      hideSpinner();
      movementInFlight = false;
      toast(`Server error (HTTP ${resp.status}). Please try again.`, 'fail', 6000);
      return;
    }

    hideSpinner();
    movementInFlight = false;

    if (data.success) {
      const destLabel = toLocation ? ` \u2192 ${toLocation}` : '';
      toast((data.message || `${type} recorded.`) + destLabel, 'ok', 4500);
      setTimeout(goScan, 1800);

    } else if (data.status === 'rejected_duplicate') {
      toast(data.message || 'Already recorded \u2014 this movement is a duplicate.', 'warn', 6000);
      setTimeout(goScan, 2500);

    } else if (data.status === 'rejected_transition') {
      _showOverrideOption(data.message, payload);

    } else if (data.status === 'rejected_validation') {
      toast(data.message || 'Movement blocked \u2014 check the details and try again.', 'fail', 6000);
      setTimeout(goScan, 2500);

    } else if (data.status === 'failed_write_logged') {
      // Transient DB error — server received the request but could not write.
      // Do NOT queue offline (it would cause a duplicate when the DB recovers).
      // Show the error and let the worker retry.
      toast(data.message || 'Database write failed. Please try again in a moment.', 'fail', 7000);

    } else {
      // Unknown status — show what the server said, do not queue offline.
      toast(data.message || 'Unexpected server response. Please try again.', 'fail', 6000);
    }

  } catch (networkErr) {
    // True network failure (fetch itself threw — no response at all).
    // Safe to queue offline because the server never received the request.
    hideSpinner();
    movementInFlight = false;
    console.error('Movement network error:', networkErr);
    toast('No connection. Movement saved for later sync.', 'warn', 5000);
    _queueOffline(payload);
  }
}

// Builds the movement payload object from current state.
function _buildPayload(type, location, toLocation, idempotencyKey) {
  return {
    handling_unit:   scanned.handling_unit,
    movement_type:   type,
    indus_part:      scanned.indus_part,
    customer_part:   '',
    pallet_qty:      scanned.pallet_qty,
    job_no:          '',
    operator_id:     deviceId,
    device_id:       deviceId,
    location:        location,
    to_location:     toLocation || '',
    timestamp:       new Date().toISOString(),
    idempotency_key: idempotencyKey,
    is_override:     _isNotFoundOverride,
    override_reason: _isNotFoundOverride
      ? 'SAP-not-found supervisor override \u2014 data entered manually'
      : '',
    supervisor_pin:  _isNotFoundOverride ? _nfoSupervisorPin : '',
  };
}

function _queueOffline(payload) {
  try {
    OfflineQueue.add(payload).then(() => {
      updatePendingBadge();
      toast('Saved offline \u2014 will sync when back online.', 'warn', 5000);
      setTimeout(goScan, 2000);
    }).catch(err => {
      console.error('OfflineQueue.add failed:', err);
      toast('Could not save offline. Please retry when connected.', 'fail', 6000);
    });
  } catch (err) {
    console.error('OfflineQueue.add threw synchronously:', err);
    toast('Could not save offline. Please retry when connected.', 'fail', 6000);
  } finally {
    hideSpinner();
    movementInFlight = false;
  }
}

// ── Movement-rejection supervisor override ──
function _showOverrideOption(serverMessage, originalPayload) {
  const modal = document.getElementById('overrideModal');
  const msg   = document.getElementById('overrideMessage');
  if (!modal) { toast(serverMessage, 'fail', 7000); return; }
  msg.textContent = serverMessage;

  const pinInput   = document.getElementById('overridePinInput');
  const confirmBtn = document.getElementById('btnConfirmOverride');
  const ovErr      = document.getElementById('overridePinError');
  if (pinInput)   { pinInput.value = ''; pinInput.disabled = false; }
  if (confirmBtn) confirmBtn.disabled = false;
  if (ovErr)      ovErr.textContent = '';

  modal.classList.add('on');
  modal._overridePayload = {
    ...originalPayload,
    is_override:     false,
    supervisor_pin:  '',
    override_reason: '',
  };
}

async function confirmOverride() {
  const modal      = document.getElementById('overrideModal');
  const pinInput   = document.getElementById('overridePinInput');
  const confirmBtn = document.getElementById('btnConfirmOverride');
  const ovErr      = document.getElementById('overridePinError');

  const pin = pinInput ? pinInput.value.trim() : '';
  if (!pin) {
    if (ovErr) ovErr.textContent = 'Enter supervisor PIN.';
    else toast('Enter supervisor PIN.', 'warn');
    return;
  }

  if (confirmBtn) confirmBtn.disabled = true;

  const payload = {
    ...modal._overridePayload,
    is_override:     true,
    supervisor_pin:  pin,
    override_reason: 'Supervisor override at worker terminal',
  };

  try {
    movementInFlight = true;
    showSpinner('Submitting override\u2026');

    const resp = await fetch('/api/movement', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    let data;
    const contentType = resp.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      data = await resp.json();
    } else {
      hideSpinner();
      movementInFlight = false;
      if (ovErr) ovErr.textContent = `Server error (HTTP ${resp.status}). Please try again.`;
      if (confirmBtn) confirmBtn.disabled = false;
      return;
    }

    hideSpinner();
    movementInFlight = false;

    if (data.success) {
      modal.classList.remove('on');
      toast(data.message || 'Override accepted.', 'ok', 4000);
      setTimeout(goScan, 1800);

    } else if (data.status === 'rejected_duplicate') {
      modal.classList.remove('on');
      toast(data.message || 'Already recorded \u2014 duplicate blocked.', 'warn', 6000);
      setTimeout(goScan, 2500);

    } else {
      const errMsg = data.message || 'Override failed. Check PIN and try again.';
      if (ovErr) ovErr.textContent = errMsg;
      else toast(errMsg, 'fail', 6000);
      if (pinInput) { pinInput.value = ''; pinInput.focus(); }
      if (confirmBtn) confirmBtn.disabled = false;
    }

  } catch (err) {
    hideSpinner();
    movementInFlight = false;
    if (ovErr)      ovErr.textContent = 'Network error during override.';
    else toast('Network error during override.', 'fail', 5000);
    if (confirmBtn) confirmBtn.disabled = false;
  }
}

function cancelOverride() {
  const modal = document.getElementById('overrideModal');
  if (modal) modal.classList.remove('on');
  movementInFlight = false;
  _isNotFoundOverride = false;
  _nfoSupervisorPin   = '';
}


// ═══════════════════════════════════════════════════════════
//  OFFLINE SYNC
// ═══════════════════════════════════════════════════════════

async function syncPendingEvents() {
  const pending = await OfflineQueue.getAll();
  if (!pending.length) return;

  toast(`Syncing ${pending.length} offline event(s)\u2026`, 'info', 3000);
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

// ─────────────────────────────────────────────────────────────────────────────
// Loading Mode — isolated client-side draft queue.
// This code intentionally does not modify normal movement request payloads.
// ─────────────────────────────────────────────────────────────────────────────

const LOADING_DRAFT_KEY = 'indus.loadingDraft.v1';

let loadingDraft = loadLoadingDraft();

function newLoadingDraft() {
  return {
    idempotency_key: (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : `load-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    source_location: '',
    operator_id: '',
    device_id: '',
    items: []
  };
}

function loadLoadingDraft() {
  try {
    const raw = sessionStorage.getItem(LOADING_DRAFT_KEY);
    const parsed = raw ? JSON.parse(raw) : null;

    if (
      parsed &&
      typeof parsed === 'object' &&
      Array.isArray(parsed.items) &&
      parsed.idempotency_key
    ) {
      return parsed;
    }
  } catch (_) {
    // Start with a clean draft if the previous browser value is malformed.
  }

  return newLoadingDraft();
}

function saveLoadingDraft() {
  sessionStorage.setItem(LOADING_DRAFT_KEY, JSON.stringify(loadingDraft));
}

function getLoadingModeElements() {
  return {
    toggle: document.getElementById('loadingModeToggle'),
    queuePanel: document.getElementById('loadingQueuePanel'),
    queueCount: document.getElementById('loadingQueueCount'),
    queueBody: document.getElementById('loadingQueueBody'),
    addButton: document.getElementById('addToLoadingButton'),
    clearButton: document.getElementById('clearLoadingButton'),
    reviewButton: document.getElementById('reviewLoadingButton')
  };
}

function loadingModeEnabled() {
  return Boolean(document.getElementById('loadingModeToggle')?.checked);
}

function getCurrentLoadingSourceLocation() {
  return String(
    document.getElementById('locationSelect')?.value || ''
  ).trim().toUpperCase();
}

function getCurrentResolvedScanForLoading() {
  return {
    handling_unit: String(
      document.getElementById('huInput')?.value || ''
    ).trim(),

    indus_part: String(
      document.getElementById('indusPartInput')?.value || ''
    ).trim(),

    customer_part: String(
      document.getElementById('customerPartInput')?.value || ''
    ).trim(),

    pallet_qty: String(
      document.getElementById('palletQtyInput')?.value || ''
    ).trim()
  };
}

function showLoadingMessage(message, type = 'info') {
  showResult(message, type);
}

  window.alert(message);
}

function renderLoadingDraft() {
  const { queuePanel, queueCount, queueBody, reviewButton } = getLoadingModeElements();

  if (!queuePanel || !queueCount || !queueBody) return;

  queuePanel.hidden = !loadingModeEnabled();
  queueCount.textContent = `${loadingDraft.items.length} HU${loadingDraft.items.length === 1 ? '' : 's'}`;

  if (!loadingDraft.items.length) {
    queueBody.innerHTML = '<tr><td colspan="5">No HUs added yet.</td></tr>';
  } else {
    queueBody.innerHTML = loadingDraft.items.map((item, index) => `
      <tr>
        <td>${index + 1}</td>
        <td>${escapeLoadingHtml(item.handling_unit)}</td>
        <td>${escapeLoadingHtml(item.indus_part)}</td>
        <td>${escapeLoadingHtml(item.pallet_qty)}</td>
        <td>
          <button type="button" class="btn-link-danger"
            data-loading-remove-index="${index}">
            Remove
          </button>
        </td>
      </tr>
    `).join('');
  }

  queueBody.querySelectorAll('[data-loading-remove-index]').forEach((button) => {
    button.addEventListener('click', () => {
      const index = Number(button.dataset.loadingRemoveIndex);
      if (!Number.isInteger(index)) return;

      loadingDraft.items.splice(index, 1);
      saveLoadingDraft();
      renderLoadingDraft();
    });
  });

  if (reviewButton) {
    reviewButton.classList.toggle('is-disabled', loadingDraft.items.length === 0);
    reviewButton.setAttribute(
      'aria-disabled',
      loadingDraft.items.length === 0 ? 'true' : 'false'
    );
  }
}

function escapeLoadingHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function addCurrentScanToLoadingDraft() {
  if (!loadingModeEnabled()) {
    showLoadingMessage(
      'Turn on Loading Mode before adding to the loading queue.',
      'error'
    );
    return;
  }

  const item = getCurrentResolvedScanForLoading();
  const sourceLocation = getCurrentLoadingSourceLocation();

  if (!sourceLocation) {
    showLoadingMessage('Select a source location first.', 'error');
    return;
  }

  if (!item.handling_unit || !item.indus_part || !item.pallet_qty) {
    showLoadingMessage(
      'Scan and resolve a valid HU, part, and quantity before adding.',
      'error'
    );
    return;
  }

  if (
    loadingDraft.source_location &&
    loadingDraft.source_location !== sourceLocation
  ) {
    showLoadingMessage(
      `This queue is for ${loadingDraft.source_location}. ` +
      `Clear it before selecting ${sourceLocation}.`,
      'error'
    );
    return;
  }

  if (
    loadingDraft.items.some(
      existing => existing.handling_unit === item.handling_unit
    )
  ) {
    showLoadingMessage(
      `HU ${item.handling_unit} is already in this loading.`,
      'error'
    );
    return;
  }

  loadingDraft.source_location = sourceLocation;

  loadingDraft.operator_id = String(
    document.getElementById('operatorInput')?.value || 'WORKER'
  ).trim() || 'WORKER';

  loadingDraft.device_id = String(
    document.getElementById('deviceIdInput')?.value || ''
  ).trim();

  loadingDraft.items.push(item);

  saveLoadingDraft();
  renderLoadingDraft();

  showLoadingMessage(
    `Added HU ${item.handling_unit} to the loading queue.`,
    'success'
  );
}

function clearLoadingDraft() {
  if (!loadingDraft.items.length) return;

  const accepted = window.confirm(
    'Clear this loading queue? This cannot be undone, but nothing has been posted.'
  );

  if (!accepted) return;

  loadingDraft = newLoadingDraft();
  saveLoadingDraft();
  renderLoadingDraft();
}

function setupLoadingMode() {
  const {
    toggle,
    addButton,
    clearButton,
    reviewButton
  } = getLoadingModeElements();

  if (!toggle) return;

  toggle.checked = sessionStorage.getItem('indus.loadingMode.enabled') === 'true';

  toggle.addEventListener('change', () => {
    sessionStorage.setItem(
      'indus.loadingMode.enabled',
      toggle.checked ? 'true' : 'false'
    );
    renderLoadingDraft();
  });

  addButton?.addEventListener('click', addCurrentScanToLoadingDraft);

  clearButton?.addEventListener('click', clearLoadingDraft);

  reviewButton?.addEventListener('click', (event) => {
    if (!loadingDraft.items.length) {
      event.preventDefault();
      showLoadingMessage('Add at least one HU before reviewing the loading.');
    }
  });

  renderLoadingDraft();
}

document.addEventListener('DOMContentLoaded', setupLoadingMode);
