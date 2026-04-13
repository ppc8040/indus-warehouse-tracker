/* ═══════════════════════════════════════════════════════════
   INDUS WAREHOUSE TRACKER — Single-Scan Architecture

   Worker scans the barcode (or types HU manually) → taps LOOKUP
   → app fetches Material + Qty from SAP API → STOCK IN or OUT.

   If SAP returns HU_NOT_FOUND, a supervisor override modal appears
   where the supervisor enters their PIN. On success the worker
   manually enters the Indus Part and Qty. Movement is then posted
   normally with is_override=true and the raw supervisor PIN.

   QR codes are no longer scanned. Camera is barcode-only.
   ═══════════════════════════════════════════════════════════ */
'use strict';

// ── HU Validation (same rules as backend validators.py) ──
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

// Called when camera successfully reads a barcode
function onBarcodeScanSuccess(decodedText) {
  const val = decodedText.trim();
  if (!val) return;

  // Only accept values that look like an HU (18-20 digits, starts with 10 or 11)
  if (!HU_PATTERN.test(val) || (!val.startsWith('10') && !val.startsWith('11'))) {
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
  if (!raw.startsWith('10') && !raw.startsWith('11')) {
    toast('Invalid HU. Must start with 10 or 11.', 'warn', 4000);
    return;
  }
  _doSAPLookup(raw);
}

// Core SAP lookup — shared by both camera scan and manual entry paths
async function _doSAPLookup(hu) {
  if (sapLookupInProgress) return;
  sapLookupInProgress = true;

  const btn = document.getElementById('btnLookupHU');
  if (btn) btn.disabled = true;

  showSpinner('Looking up in SAP\u2026');

  try {
    const resp = await fetch('/api/lookup-hu', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ handling_unit: hu }),
    });

    const data = await resp.json();

    if (data.success) {
      // ── Normal path: SAP found the HU ──
      scanned.handling_unit = data.handling_unit;
      scanned.indus_part    = data.indus_part;
      scanned.pallet_qty    = data.pallet_qty;
      scanned.customer_part = '';
      _isNotFoundOverride   = false;
      _nfoSupervisorPin     = '';

      updateScanDisplay();
      toast(`Found: ${data.indus_part} \u00d7 ${data.pallet_qty}`, 'ok', 3000);

      const proceedBtn = document.getElementById('btnProceed');
      if (proceedBtn) proceedBtn.disabled = false;

    } else if (data.error_code === 'HU_NOT_FOUND') {
      // ── Override path: HU not in SAP but physically in warehouse ──
      _openNotFoundOverrideModal(hu);

    } else {
      // ── All other failures (network, auth, parse) ──
      toast(data.message || 'SAP lookup failed. Try again.', 'fail', 6000);
      _resetLookupState();
    }

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
  scanned.indus_part    = '';
  scanned.pallet_qty    = '';
  scanned.customer_part = '';
  _isNotFoundOverride   = false;
  _nfoSupervisorPin     = '';
  updateScanDisplay();
  const proceedBtn = document.getElementById('btnProceed');
  if (proceedBtn) proceedBtn.disabled = true;
}


// ═══════════════════════════════════════════════════════════
//  SAP-NOT-FOUND SUPERVISOR OVERRIDE MODAL
//
//  Triggered only when error_code === 'HU_NOT_FOUND'.
//  Flow:
//    1. Show modal with HU number and explanation
//    2. Supervisor enters PIN → validated against /api/validate-pin
//       On success the PIN is stored in _nfoSupervisorPin.
//    3. Worker fills Indus Part + Qty and taps CONFIRM.
//    4. scanned state populated → PROCEED button enabled.
//       Movement is posted with is_override=true and
//       supervisor_pin = _nfoSupervisorPin.
// ═══════════════════════════════════════════════════════════

function _openNotFoundOverrideModal(hu) {
  _notFoundOverrideHU = hu;
  _nfoSupervisorPin   = '';  // clear any stale PIN

  // Populate the HU display inside the modal
  const huSpan = document.getElementById('nfoHUDisplay');
  if (huSpan) huSpan.textContent = hu;

  // Reset the modal to step 1 (PIN entry)
  _showNFOStep('pin');

  // Clear any previous inputs
  const pinInput  = document.getElementById('nfoPinInput');
  const partInput = document.getElementById('nfoIndusPart');
  const qtyInput  = document.getElementById('nfoQty');
  if (pinInput)  { pinInput.value  = ''; pinInput.disabled  = false; }
  if (partInput) partInput.value = '';
  if (qtyInput)  qtyInput.value  = '';

  // Clear any previous PIN error and re-enable verify button
  const pinErr    = document.getElementById('nfoPinError');
  const verifyBtn = document.getElementById('nfoBtnVerifyPin');
  if (pinErr)    pinErr.textContent = '';
  if (verifyBtn) verifyBtn.disabled = false;

  document.getElementById('sapNotFoundModal').classList.add('on');
}

function _showNFOStep(step) {
  // step: 'pin' | 'fields'
  const pinStep    = document.getElementById('nfoStepPin');
  const fieldsStep = document.getElementById('nfoStepFields');
  if (pinStep)    pinStep.style.display    = (step === 'pin')    ? '' : 'none';
  if (fieldsStep) fieldsStep.style.display = (step === 'fields') ? '' : 'none';
}

// Called when supervisor taps VERIFY PIN in the not-found override modal
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
      // ── Correct PIN — store it and show the fields step ──
      _nfoSupervisorPin = pin;
      _showNFOStep('fields');

    } else {
      // ── Wrong PIN ──
      if (pinErr) pinErr.textContent = data.message || 'Incorrect PIN. Please try again.';
      if (pinInput) { pinInput.value = ''; pinInput.focus(); }
      if (verifyBtn) verifyBtn.disabled = false;
    }

  } catch (e) {
    if (pinErr)    pinErr.textContent = 'Network error. Try again.';
    if (verifyBtn) verifyBtn.disabled = false;
  }
}

// Called when worker taps CONFIRM in the fields step of the not-found override modal
function nfoConfirmFields() {
  const partInput = document.getElementById('nfoIndusPart');
  const qtyInput  = document.getElementById('nfoQty');
  const fieldsErr = document.getElementById('nfoFieldsError');

  const rawPart = partInput ? partInput.value.trim().toUpperCase() : '';
  const rawQty  = qtyInput  ? qtyInput.value.trim()  : '';

  // Basic validation
  if (!rawPart) {
    if (fieldsErr) fieldsErr.textContent = 'Indus Part (Material) is required.';
    return;
  }
  const qtyNum = parseInt(rawQty, 10);
  if (!rawQty || isNaN(qtyNum) || qtyNum < 1) {
    if (fieldsErr) fieldsErr.textContent = 'Enter a valid quantity (whole number, at least 1).';
    return;
  }

  // PIN guard: must have a stored PIN before proceeding
  if (!_nfoSupervisorPin) {
    if (fieldsErr) fieldsErr.textContent = 'Session lost. Close this and re-enter the supervisor PIN.';
    return;
  }

  // Populate scanned state — same as if SAP had returned this data
  scanned.handling_unit = _notFoundOverrideHU;
  scanned.indus_part    = rawPart;
  scanned.pallet_qty    = String(qtyNum);
  scanned.customer_part = '';
  _isNotFoundOverride   = true;

  // Close the modal
  document.getElementById('sapNotFoundModal').classList.remove('on');
  _notFoundOverrideHU = '';

  // Update the scan display and enable PROCEED
  updateScanDisplay();

  const proceedBtn = document.getElementById('btnProceed');
  if (proceedBtn) proceedBtn.disabled = false;

  toast(
    `Override set: ${rawPart} \u00d7 ${qtyNum}. Choose STOCK IN or STOCK OUT.`,
    'warn',
    5000
  );
}

// Called when supervisor/worker taps Cancel in the not-found override modal
function nfoCancelOverride() {
  document.getElementById('sapNotFoundModal').classList.remove('on');
  _notFoundOverrideHU = '';
  _nfoSupervisorPin   = '';
  // Full reset so no stale override state lingers
  _resetLookupState();
}


// ═══════════════════════════════════════════════════════════
//  SCAN DISPLAY + RESET
// ═══════════════════════════════════════════════════════════

function updateScanDisplay() {
  _updateField('hu',    scanned.handling_unit);
  _updateField('indus', scanned.indus_part);
  _updateField('qty',   scanned.pallet_qty);

  // Show/hide the SAP result card
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
  sapLookupInProgress = false;
  _isNotFoundOverride = false;
  _notFoundOverrideHU = '';
  _nfoSupervisorPin   = '';

  const inp = document.getElementById('huManualInput');
  if (inp) inp.value = '';

  updateScanDisplay();
}


// ═══════════════════════════════════════════════════════════
//  MOVEMENT SUBMISSION
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
  showSpinner(type === 'STOCK_IN' ? 'Recording Stock IN\u2026' : 'Recording Stock OUT\u2026');

  const idempotencyKey = `${scanned.handling_unit}-${type}-${Date.now()}`;

  // For SAP-not-found overrides, send the stored supervisor PIN.
  const supervisorPin = _isNotFoundOverride ? _nfoSupervisorPin : '';

  const payload = {
    handling_unit:   scanned.handling_unit,
    movement_type:   type,
    indus_part:      scanned.indus_part,
    customer_part:   '',
    pallet_qty:      scanned.pallet_qty,
    job_no:          '',
    operator_id:     deviceId,
    device_id:       deviceId,
    location:        location,
    timestamp:       new Date().toISOString(),
    idempotency_key: idempotencyKey,
    is_override:     _isNotFoundOverride,
    override_reason: _isNotFoundOverride
      ? 'SAP-not-found supervisor override — data entered manually'
      : '',
    supervisor_pin:  supervisorPin,
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
      // ── Happy path — full reset before next scan ──
      toast(data.message || `${type} recorded.`, 'ok', 4000);
      setTimeout(goScan, 1800);

    } else if (data.status === 'rejected_duplicate') {
      // ── Exact duplicate: already recorded, never offer override ──
      toast(data.message || 'Already recorded \u2014 this movement is a duplicate.', 'warn', 6000);
      setTimeout(goScan, 2500);

    } else if (data.status === 'rejected_transition') {
      // ── Bad transition (e.g. STOCK_OUT when never stocked IN): supervisor can override ──
      _showOverrideOption(data.message, payload);

    } else if (data.status === 'rejected_validation') {
      // ── Structural validation failure (missing fields, bad PIN) ──
      toast(data.message || 'Movement blocked \u2014 check the details and try again.', 'fail', 6000);
      setTimeout(goScan, 2500);

    } else {
      // ── Write / server error — queue offline for retry ──
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
    toast('Saved offline \u2014 will sync when back online.', 'warn', 5000);
    setTimeout(goScan, 2000);
  });
}

// ── Movement-rejection supervisor override ──
// Only triggered for rejected_transition — bad state machine transitions.

function _showOverrideOption(serverMessage, originalPayload) {
  const modal = document.getElementById('overrideModal');
  const msg   = document.getElementById('overrideMessage');
  if (!modal) { toast(serverMessage, 'fail', 7000); return; }
  msg.textContent = serverMessage;

  const pinInput = document.getElementById('overridePinInput');
  const confirmBtn = document.getElementById('btnConfirmOverride');
  const ovErr = document.getElementById('overridePinError');
  if (pinInput)   { pinInput.value = ''; pinInput.disabled = false; }
  if (confirmBtn) confirmBtn.disabled = false;
  if (ovErr)      ovErr.textContent = '';

  modal.classList.add('on');
  // Store the original payload — override_reason and supervisor_pin
  // will be set when the supervisor confirms.
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

  // Build override payload — send numeric PIN directly.
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
    const data = await resp.json();

    hideSpinner();
    movementInFlight = false;

    if (data.success) {
      // ── Correct PIN, override accepted — full reset ──
      modal.classList.remove('on');
      toast(data.message || 'Override accepted.', 'ok', 4000);
      setTimeout(goScan, 1800);

    } else if (data.status === 'rejected_duplicate') {
      modal.classList.remove('on');
      toast(data.message || 'Already recorded \u2014 duplicate blocked.', 'warn', 6000);
      setTimeout(goScan, 2500);

    } else {
      // ── Wrong PIN or other rejection ──
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
  // Reset all override state so next scan starts clean
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
        // Remove from queue on success OR if the server confirms it is a
        // duplicate — both mean the movement is definitively handled.
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
