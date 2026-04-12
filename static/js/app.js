/* ═══════════════════════════════════════════════════════════
   INDUS WAREHOUSE TRACKER — Single-Scan Architecture

   Worker scans the barcode (or types HU manually) → taps LOOKUP
   → app fetches Material + Qty from SAP API → STOCK IN or OUT.

   If SAP returns HU_NOT_FOUND, a supervisor override modal appears
   where the supervisor enters their PIN and the worker manually
   enters the Indus Part and Qty. Movement is then posted normally
   with is_override=true and override_token from /validate-pin.

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

// Fix 2: server-issued one-use token returned by /validate-pin on success.
// Sent as supervisor_pin in the movement POST for SAP-not-found overrides.
// Cleared after use or on any reset.
let _nfoOverrideToken = '';

// ── Submit lock: prevents double-tap duplicates on STOCK IN / STOCK OUT ──
let movementInFlight = false;

// ── Location modal state ──
let _pendingMovementType = '';   // 'STOCK_IN' or 'STOCK_OUT'
let _selectedLocation = '';      // chosen location value

// ── Fix 5: Device identity ──
// deviceId is a stable random ID stored in localStorage (used only for
// registration lookup — the movement payload sends deviceName instead).
// deviceName is the human-readable name entered on first visit.
let deviceId = localStorage.getItem('deviceId') || (() => {
  const id = 'DEV-' + Math.random().toString(36).substring(2, 10).toUpperCase();
  localStorage.setItem('deviceId', id);
  return id;
})();
let deviceName = localStorage.getItem('deviceName') || '';

// ── PIN lockout state — client-side display only ──
// The server enforces the real 3-strike / 30-second lockout per-IP.
// These variables drive the countdown UI and attempt counter display.
// Each override operation gets a fresh counter (_nfoPinAttempts resets
// inside _openNotFoundOverrideModal so previous failures don't carry over).
const _PIN_MAX_ATTEMPTS  = 3;
const _PIN_LOCKOUT_SECS  = 30;
let _nfoPinAttempts      = 0;   // wrong guesses in the current modal session
let _nfoPinCountdownId   = null; // setInterval handle for the countdown timer

// Same counters for the rejection-override modal
let _ovPinAttempts     = 0;
let _ovPinCountdownId  = null;


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
  _initDeviceRegistration();
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
//  FIX 5 — DEVICE NAME REGISTRATION
//
//  On DOMContentLoaded, check localStorage for a saved deviceName.
//  If none, show the registration overlay (not dismissible).
//  On submit: POST to /api/register-device.
//    - If the name is taken by another device: show inline error.
//    - If the same deviceId is already in the sheet: restore saved name.
//    - On success: save to localStorage, update deviceName, hide overlay.
// ═══════════════════════════════════════════════════════════

function _initDeviceRegistration() {
  if (deviceName) {
    // Already registered on this device — show name in header and proceed
    _setDeviceNameDisplay(deviceName);
    return;
  }
  // No name in localStorage — show the registration overlay
  document.getElementById('deviceRegOverlay').classList.add('on');
}

function _setDeviceNameDisplay(name) {
  const el = document.getElementById('deviceNameDisplay');
  if (el) el.textContent = name;
}

async function submitDeviceName() {
  const input = document.getElementById('deviceNameInput');
  const errEl = document.getElementById('deviceNameError');
  const btn   = document.getElementById('deviceNameBtn');

  const name = input ? input.value.trim() : '';
  if (!name) {
    if (errEl) errEl.textContent = 'Please enter your name before continuing.';
    return;
  }
  if (name.length > 40) {
    if (errEl) errEl.textContent = 'Name is too long. Keep it under 40 characters.';
    return;
  }

  if (errEl) errEl.textContent = '';
  if (btn) btn.disabled = true;

  try {
    const resp = await fetch('/api/register-device', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: deviceId, device_name: name }),
    });
    const data = await resp.json();

    if (data.success) {
      // Use the name the server confirmed (handles already_registered case)
      const confirmedName = data.device_name || name;
      deviceName = confirmedName;
      localStorage.setItem('deviceName', confirmedName);
      _setDeviceNameDisplay(confirmedName);
      document.getElementById('deviceRegOverlay').classList.remove('on');
    } else if (data.taken) {
      if (errEl) errEl.textContent = data.message ||
        'That name is already in use on another scanner. Please choose a different name.';
    } else {
      if (errEl) errEl.textContent = data.message ||
        'Could not register. Please try again.';
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'Network error. Make sure you are connected and try again.';
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Allow Enter key to submit the device name form
document.addEventListener('DOMContentLoaded', () => {
  const inp = document.getElementById('deviceNameInput');
  if (inp) inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') submitDeviceName();
  });
});


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
      _nfoOverrideToken     = '';

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
  _nfoOverrideToken     = '';
  updateScanDisplay();
  const proceedBtn = document.getElementById('btnProceed');
  if (proceedBtn) proceedBtn.disabled = true;
}


// ═══════════════════════════════════════════════════════════
//  PIN LOCKOUT HELPERS
//
//  _startPinCountdown(secs, errEl, inputEl, btnEl, onUnlock)
//    Disables inputEl + btnEl, shows a live "Try again in Xs…"
//    countdown in errEl, then re-enables everything when done
//    and calls onUnlock() so the caller can do any extra reset.
//
//  _clearPinCountdown(timerId)
//    Clears a running countdown interval safely.
//
//  _pinAttemptsMessage(remaining)
//    Returns a human-readable string for remaining attempts.
// ═══════════════════════════════════════════════════════════

function _startPinCountdown(secs, errEl, inputEl, btnEl, onUnlock) {
  let remaining = secs;

  function _tick() {
    remaining -= 1;
    if (errEl) errEl.textContent = `Too many failed attempts. Try again in ${remaining}s.`;
    if (remaining <= 0) {
      clearInterval(timerId);
      if (inputEl) { inputEl.disabled = false; inputEl.value = ''; inputEl.focus(); }
      if (btnEl)   btnEl.disabled = false;
      if (errEl)   errEl.textContent = '';
      if (typeof onUnlock === 'function') onUnlock();
    }
  }

  // Disable immediately and show first tick message
  if (inputEl) inputEl.disabled = true;
  if (btnEl)   btnEl.disabled   = true;
  if (errEl)   errEl.textContent = `Too many failed attempts. Try again in ${remaining}s.`;

  const timerId = setInterval(_tick, 1000);
  return timerId;
}

function _clearPinCountdown(timerId) {
  if (timerId !== null) clearInterval(timerId);
  return null;
}

function _pinAttemptsMessage(remaining) {
  if (remaining === 1) return 'Incorrect PIN. 1 attempt remaining.';
  if (remaining > 1)  return `Incorrect PIN. ${remaining} attempts remaining.`;
  return 'Incorrect PIN.';
}


// ═══════════════════════════════════════════════════════════
//  SAP-NOT-FOUND SUPERVISOR OVERRIDE MODAL
//
//  Triggered only when error_code === 'HU_NOT_FOUND'.
//  Flow:
//    1. Show modal with HU number and explanation
//    2. Supervisor enters PIN → validated against /api/validate-pin
//       On success the server returns an override_token (Fix 2).
//    3. Token stored in _nfoOverrideToken (in-memory only).
//    4. Worker fills Indus Part + Qty and taps CONFIRM.
//    5. scanned state populated → PROCEED button enabled.
//       Movement is posted with is_override=true and
//       supervisor_pin = _nfoOverrideToken.
// ═══════════════════════════════════════════════════════════

function _openNotFoundOverrideModal(hu) {
  _notFoundOverrideHU = hu;
  _nfoOverrideToken   = '';  // clear any stale token

  // ── Per-operation state reset ──
  // Clear any countdown that is still running from a previous override
  // operation so it never bleeds into this new one.
  _nfoPinCountdownId = _clearPinCountdown(_nfoPinCountdownId);
  _nfoPinAttempts    = 0;

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

    if (data.valid && data.override_token) {
      // ── Correct PIN ──
      // Per-operation reset: clear attempt counter so next override starts fresh
      _nfoPinAttempts    = 0;
      _nfoPinCountdownId = _clearPinCountdown(_nfoPinCountdownId);

      // Fix 2: store the server-issued token for use in the movement POST
      _nfoOverrideToken = data.override_token;
      _showNFOStep('fields');

    } else if (resp.status === 429) {
      // ── Server-enforced lockout ──
      // Start the visual countdown; input+button stay disabled until it ends
      _nfoPinCountdownId = _clearPinCountdown(_nfoPinCountdownId);
      _nfoPinCountdownId = _startPinCountdown(
        _PIN_LOCKOUT_SECS,
        pinErr,
        pinInput,
        verifyBtn,
        () => { _nfoPinAttempts = 0; }
      );

    } else {
      // ── Wrong PIN (not yet locked) ──
      _nfoPinAttempts += 1;
      const remaining = _PIN_MAX_ATTEMPTS - _nfoPinAttempts;
      if (pinErr) pinErr.textContent = remaining > 0
        ? _pinAttemptsMessage(remaining)
        : 'Incorrect PIN.';
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

  // Token guard: should not be reachable without a valid token, but defend anyway
  if (!_nfoOverrideToken) {
    if (fieldsErr) fieldsErr.textContent = 'Session expired. Close this and re-enter the supervisor PIN.';
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
  // Stop any running countdown before closing
  _nfoPinCountdownId = _clearPinCountdown(_nfoPinCountdownId);
  _nfoPinAttempts    = 0;

  document.getElementById('sapNotFoundModal').classList.remove('on');
  _notFoundOverrideHU = '';
  _nfoOverrideToken   = '';
  // Leave the HU input populated so they can try again or use a different HU
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
  _nfoOverrideToken   = '';

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

  // Fix 2: for SAP-not-found overrides, send the one-use token as supervisor_pin.
  // For all other movements (normal or rejection override), send '' or the PIN
  // as usual — the rejection override path sets its own pin via confirmOverride().
  const supervisorPin = _isNotFoundOverride ? _nfoOverrideToken : '';

  const payload = {
    handling_unit:   scanned.handling_unit,
    movement_type:   type,
    indus_part:      scanned.indus_part,
    customer_part:   '',
    pallet_qty:      scanned.pallet_qty,
    job_no:          '',
    // Fix 5: use the registered device name instead of the random device ID
    operator_id:     deviceName || 'WORKER',
    device_id:       deviceName || deviceId,
    location:        location,
    timestamp:       new Date().toISOString(),
    idempotency_key: idempotencyKey,
    is_override:     _isNotFoundOverride,
    override_reason: _isNotFoundOverride
      ? 'SAP-not-found supervisor override — data entered manually'
      : '',
    supervisor_pin:  supervisorPin,
  };

  // Clear the token immediately after building the payload — single use
  if (_isNotFoundOverride) _nfoOverrideToken = '';

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
      // ── Happy path ──
      toast(data.message || `${type} recorded.`, 'ok', 4000);
      setTimeout(goScan, 1800);

    } else if (data.status === 'rejected_duplicate') {
      // ── Exact duplicate: already recorded, never offer override ──
      toast(data.message || 'Already recorded \u2014 this movement is a duplicate.', 'warn', 6000);
      setTimeout(goScan, 2500);

    } else if (data.status === 'rejected_transition') {
      // ── Bad transition (e.g. STOCK_IN when already IN): supervisor can override ──
      _showOverrideOption(data.message, payload);

    } else if (data.status === 'rejected_validation') {
      // ── Structural validation failure (missing fields, bad PIN, expired token) ──
      // Never offer a supervisor override for validation errors — show and reset.
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
// Sends the numeric PIN directly through the rate limiter (Fix 1).

function _showOverrideOption(serverMessage, originalPayload) {
  const modal = document.getElementById('overrideModal');
  const msg   = document.getElementById('overrideMessage');
  if (!modal) { toast(serverMessage, 'fail', 7000); return; }
  msg.textContent = serverMessage;

  // Per-operation state reset for the rejection override PIN
  _ovPinCountdownId = _clearPinCountdown(_ovPinCountdownId);
  _ovPinAttempts    = 0;

  const pinInput = document.getElementById('overridePinInput');
  const confirmBtn = document.getElementById('btnConfirmOverride');
  const ovErr = document.getElementById('overridePinError');
  if (pinInput)   { pinInput.value = ''; pinInput.disabled = false; }
  if (confirmBtn) confirmBtn.disabled = false;
  if (ovErr)      ovErr.textContent = '';

  modal.classList.add('on');
  modal._overridePayload = originalPayload;
}

async function confirmOverride() {
  const modal      = document.getElementById('overrideModal');
  const pinInput   = document.getElementById('overridePinInput');
  const confirmBtn = document.getElementById('btnConfirmOverride');
  // Use a dedicated error element if present; fall back to toast
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
    is_override: true,
    supervisor_pin: pin,
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
      // ── Correct PIN, override accepted ──
      _ovPinAttempts    = 0;
      _ovPinCountdownId = _clearPinCountdown(_ovPinCountdownId);
      modal.classList.remove('on');
      toast(data.message || 'Override accepted.', 'ok', 4000);
      setTimeout(goScan, 1800);

    } else if (data.status === 'rejected_duplicate') {
      // Even overrides can hit the duplicate guard — block cleanly
      _ovPinCountdownId = _clearPinCountdown(_ovPinCountdownId);
      modal.classList.remove('on');
      toast(data.message || 'Already recorded \u2014 duplicate blocked.', 'warn', 6000);
      setTimeout(goScan, 2500);

    } else if (resp.status === 429) {
      // ── Server-enforced PIN lockout ──
      _ovPinCountdownId = _clearPinCountdown(_ovPinCountdownId);
      _ovPinCountdownId = _startPinCountdown(
        _PIN_LOCKOUT_SECS,
        ovErr || { textContent: '' },  // if no dedicated el, silently degrade
        pinInput,
        confirmBtn,
        () => { _ovPinAttempts = 0; }
      );
      if (!ovErr) toast('Too many failed attempts. Wait 30 seconds.', 'fail', 6000);

    } else {
      // ── Wrong PIN (not yet locked) or other rejection ──
      _ovPinAttempts += 1;
      const remaining = _PIN_MAX_ATTEMPTS - _ovPinAttempts;
      const msg = (data.status === 'rejected_validation' && data.message &&
                   data.message.toLowerCase().includes('pin'))
        ? (remaining > 0 ? _pinAttemptsMessage(remaining) : 'Incorrect PIN.')
        : (data.message || 'Override failed.');
      if (ovErr) ovErr.textContent = msg;
      else toast(msg, 'fail', 6000);
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
  // Stop any running countdown before closing
  _ovPinCountdownId = _clearPinCountdown(_ovPinCountdownId);
  _ovPinAttempts    = 0;

  const modal = document.getElementById('overrideModal');
  if (modal) modal.classList.remove('on');
  movementInFlight = false;
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
