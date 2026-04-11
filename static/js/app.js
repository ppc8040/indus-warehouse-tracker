/* ═══════════════════════════════════════════════════════════
   INDUS WAREHOUSE TRACKER — Scan-to-Log Architecture

   Pallet label has 4 codes:
     QR 1 (top):    Indus Part   (e.g. "CCR2324")
     QR 2 (middle): Customer Part (e.g. "161878" or "ENG-1440")
     QR 3 (bottom): Pallet Qty   (e.g. "39")
     Barcode (Code 128): Handling Unit (e.g. "100000000000839157")

   QR codes are assigned by SCAN ORDER (1st→indus, 2nd→cust, 3rd→qty),
   not by value format, to avoid misclassification when customer part
   is a plain number.

   Worker scans all 4 → reviews → Stock IN or OUT → done.
   ═══════════════════════════════════════════════════════════ */
'use strict';

// ── Field Validation Rules ──
// HU: 18-20 digits, all numeric, no special chars
const HU_PATTERN       = /^\d{18,20}$/;
// Indus Part: exactly 7 chars, first 3 uppercase letters, next 4 letters/digits
const INDUS_PATTERN    = /^[A-Z]{3}[A-Z0-9]{4}$/;
// Customer Part: letters, digits, hyphen, dot, underscore, slash, space
const CUST_PART_ALLOWED = /^[A-Za-z0-9\-._\/ ]+$/;
// Qty: 1-4 digits, positive integer only
const QTY_PATTERN      = /^\d{1,4}$/;

// ── State ──
let scanned = {
  handling_unit: '',   // from barcode (18-20 digits)
  indus_part: '',      // from QR 1 (7-char alphanumeric part code)
  customer_part: '',   // from QR 2 (alphanumeric, may be pure number)
  pallet_qty: '',      // from QR 3 (numeric, small number)
};
let scanner = null;
let scannerRunning = false;
let allScannedCodes = new Set();  // track all raw scanned values to avoid re-processing
let qrScanOrder = 0;              // counts non-HU QR scans: 1=indus, 2=cust, 3=qty

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
//  SCANNER — html5-qrcode (reads both QR and barcodes)
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
    Html5QrcodeSupportedFormats.QR_CODE,
    Html5QrcodeSupportedFormats.CODE_128,
    Html5QrcodeSupportedFormats.CODE_39,
    Html5QrcodeSupportedFormats.EAN_13,
    Html5QrcodeSupportedFormats.EAN_8,
    Html5QrcodeSupportedFormats.ITF,
  ];

  scanner = new Html5Qrcode("scannerContainer", {
    formatsToSupport: formats,
    verbose: false,
  });

  const scanConfig = {
    fps: 15,
    qrbox: { width: 340, height: 160 },
    aspectRatio: 1.7777,
    disableFlip: false,
  };

  scanner.start(
    { facingMode: "environment" },
    scanConfig,
    onScanSuccess,
    () => {}
  ).then(() => {
    scannerRunning = true;
    btn.innerHTML = '<span class="btn-icon">⏹</span> STOP CAMERA';
    toast('Camera active — point at label codes', 'info', 2000);
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

function onScanSuccess(decodedText) {
  const val = decodedText.trim();
  if (!val || allScannedCodes.has(val)) return;

  allScannedCodes.add(val);
  classifyAndStore(val);
  updateScanDisplay();

  if (navigator.vibrate) navigator.vibrate(100);

  const count = getScannedCount();
  if (count >= 4) {
    toast('All codes scanned! Review and proceed.', 'ok', 3000);
    stopScanner();
  } else {
    toast(`Scanned: ${val.substring(0, 20)}${val.length > 20 ? '...' : ''}`, 'info', 1500);
  }
}

function classifyAndStore(val) {
  const v = val.trim();
  if (!v) return;

  // HU: 18-20 digits, all numeric, starts with 1000
  if (HU_PATTERN.test(v) && v.startsWith('1000')) {
    if (!scanned.handling_unit) scanned.handling_unit = v;
    return;
  }
  // Indus Part: exactly 7 chars, first 3 uppercase letters + 4 alphanumeric
  if (INDUS_PATTERN.test(v.toUpperCase())) {
    if (!scanned.indus_part) scanned.indus_part = v.toUpperCase();
    return;
  }
  // Qty: 1-4 digits, positive integer
  if (QTY_PATTERN.test(v) && parseInt(v, 10) >= 1) {
    if (!scanned.pallet_qty) scanned.pallet_qty = v;
    return;
  }
  // Customer Part: fallback — accepts anything with no strict format
  if (!scanned.customer_part) {
    scanned.customer_part = v;
    return;
  }
}

function getScannedCount() {
  let c = 0;
  if (scanned.handling_unit) c++;
  if (scanned.indus_part) c++;
  if (scanned.customer_part) c++;
  if (scanned.pallet_qty) c++;
  return c;
}

function updateScanDisplay() {
  _updateField('hu', scanned.handling_unit);
  _updateField('indus', scanned.indus_part);
  _updateField('cust', scanned.customer_part);
  _updateField('qty', scanned.pallet_qty);

  const count = getScannedCount();
  document.getElementById('scanProgress').textContent = `${count} / 4`;

  const btn = document.getElementById('btnProceed');
  btn.disabled = !(scanned.handling_unit && scanned.indus_part);
}

function _updateField(key, val) {
  const statusEl = document.getElementById(`st-${key}`);
  const valueEl = document.getElementById(`val-${key}`);
  if (val) {
    statusEl.textContent = '\u2713 SCANNED';
    statusEl.className = 'sf-status scanned';
    valueEl.textContent = val;
    valueEl.className = 'sf-value scanned';
  } else {
    statusEl.textContent = 'NOT SCANNED';
    statusEl.className = 'sf-status';
    valueEl.textContent = '\u2014';
    valueEl.className = 'sf-value';
  }
}

function _resetManualField(rowId, btnId, inputId) {
  const row = document.getElementById(rowId);
  const btn = document.getElementById(btnId);
  const inp = document.getElementById(inputId);
  if (row) row.style.display = 'none';
  if (btn) btn.style.display = 'block';
  if (inp) inp.value = '';
}

function resetScans() {
  scanned = { handling_unit: '', indus_part: '', customer_part: '', pallet_qty: '' };
  allScannedCodes.clear();
  qrScanOrder = 0;
  updateScanDisplay();
  _resetManualField('huManualRow',    'huManualBtn',    'huManualInput');
  _resetManualField('indusManualRow', 'indusManualBtn', 'indusManualInput');
  _resetManualField('custManualRow',  'custManualBtn',  'custManualInput');
  _resetManualField('qtyManualRow',   'qtyManualBtn',   'qtyManualInput');
}

// ── Manual entry: Handling Unit ──
function showManualHU() {
  document.getElementById('huManualRow').style.display = 'block';
  document.getElementById('huManualBtn').style.display = 'none';
  document.getElementById('huManualInput').focus();
}
function manualHU() {
  const raw = document.getElementById('huManualInput').value.trim();
  if (!raw) { toast('Type the HU number from the label.', 'warn'); return; }
  if (!HU_PATTERN.test(raw)) {
    toast('Invalid HU. Must be 18\u201320 digits (numbers only, no letters or special characters).', 'warn', 5000);
    return;
  }
  scanned.handling_unit = raw;
  allScannedCodes.add(raw);
  updateScanDisplay();
  document.getElementById('huManualRow').style.display = 'none';
  toast('Handling Unit set manually.', 'ok', 2000);
}

// ── Manual entry: Indus Part ──
function showManualIndus() {
  document.getElementById('indusManualRow').style.display = 'block';
  document.getElementById('indusManualBtn').style.display = 'none';
  document.getElementById('indusManualInput').focus();
}
function manualIndus() {
  const raw = document.getElementById('indusManualInput').value.trim().toUpperCase();
  if (!raw) { toast('Type the Indus Part code from the label.', 'warn'); return; }
  if (raw.length !== 7) {
    toast(`Invalid Indus Part (${raw.length} chars). Must be exactly 7 characters (e.g. CEJ1773).`, 'warn', 5000);
    return;
  }
  if (!/^[A-Z]{3}/.test(raw)) {
    toast('Invalid Indus Part. First 3 characters must be uppercase letters (e.g. CEJ1773).', 'warn', 5000);
    return;
  }
  if (!INDUS_PATTERN.test(raw)) {
    toast('Invalid Indus Part. Must be 3 letters followed by 4 letters/digits, no special characters (e.g. CEJ1773).', 'warn', 5000);
    return;
  }
  scanned.indus_part = raw;
  allScannedCodes.add(raw);
  updateScanDisplay();
  document.getElementById('indusManualRow').style.display = 'none';
  toast('Indus Part set manually.', 'ok', 2000);
}

// ── Manual entry: Customer Part ──
function showManualCust() {
  document.getElementById('custManualRow').style.display = 'block';
  document.getElementById('custManualBtn').style.display = 'none';
  document.getElementById('custManualInput').focus();
}
function manualCust() {
  const raw = document.getElementById('custManualInput').value.trim();
  if (!raw) { toast('Type the Customer Part from the label.', 'warn'); return; }
  if (!CUST_PART_ALLOWED.test(raw)) {
    toast('Invalid Customer Part. Only letters, digits, hyphens (-), dots (.), underscores (_), slashes (/), and spaces are allowed. No other special characters.', 'warn', 6000);
    return;
  }
  if (scanned.indus_part && raw.toUpperCase() === scanned.indus_part.toUpperCase()) {
    toast('Customer Part cannot be the same as Indus Part. Check the label.', 'warn', 5000);
    return;
  }
  scanned.customer_part = raw;
  allScannedCodes.add(raw);
  updateScanDisplay();
  document.getElementById('custManualRow').style.display = 'none';
  toast('Customer Part set manually.', 'ok', 2000);
}

// ── Manual entry: Pallet Qty ──
function showManualQty() {
  document.getElementById('qtyManualRow').style.display = 'block';
  document.getElementById('qtyManualBtn').style.display = 'none';
  document.getElementById('qtyManualInput').focus();
}
function manualQty() {
  const raw = document.getElementById('qtyManualInput').value.trim();
  if (!raw) { toast('Type the pallet quantity from the label.', 'warn'); return; }
  if (!QTY_PATTERN.test(raw)) {
    toast('Invalid Quantity. Must be a whole number (1\u20139999) with no decimals, no letters, and no special characters.', 'warn', 5000);
    return;
  }
  if (parseInt(raw, 10) < 1) {
    toast('Quantity must be a positive number (at least 1).', 'warn');
    return;
  }
  scanned.pallet_qty = raw;
  allScannedCodes.add(raw);
  updateScanDisplay();
  document.getElementById('qtyManualRow').style.display = 'none';
  toast('Pallet Qty set manually.', 'ok', 2000);
}


// ═══════════════════════════════════════════════════════════
//  PHOTO SCAN — reads QR codes + barcode number from a still image
// ═══════════════════════════════════════════════════════════

async function processLabelPhoto(input) {
  const file = input.files[0];
  if (!file) return;

  showSpinner('Reading label from photo...');

  try {
    const results = await scanAllCodesFromImage(file);

    const huFromBarcode = await extractHUFromImage(file);
    if (huFromBarcode && !results.includes(huFromBarcode)) {
      results.push(huFromBarcode);
    }

    hideSpinner();

    if (results.length === 0) {
      toast('No codes found. Hold the phone steady over the full label and try again.', 'warn', 5000);
      input.value = '';
      return;
    }

    let filled = 0;
    for (const val of results) {
      if (!allScannedCodes.has(val)) {
        const before = getScannedCount();
        allScannedCodes.add(val);
        classifyAndStore(val);
        if (getScannedCount() > before) filled++;
      }
    }

    updateScanDisplay();

    const missing = [];
    if (!scanned.handling_unit) missing.push('HU Barcode');
    if (!scanned.indus_part)    missing.push('Indus Part');
    if (!scanned.customer_part) missing.push('Customer Part');
    if (!scanned.pallet_qty)    missing.push('Pallet Qty');

    if (missing.length === 0) {
      toast('\u2713 All fields read from photo!', 'ok', 3000);
      stopScanner();
    } else if (filled > 0) {
      toast(`Read ${filled} field(s) from photo. Still missing: ${missing.join(', ')}. Take another photo or type manually.`, 'warn', 7000);
    } else {
      toast('Codes were found but could not be matched to fields. Try again.', 'warn', 5000);
    }

  } catch (err) {
    hideSpinner();
    console.error('Photo scan error:', err);
    toast('Photo scan failed. Ensure good lighting and a flat, clear view of the label.', 'fail');
  }

  input.value = '';
}

async function scanAllCodesFromImage(file) {
  async function cropAndScan(img, xRatio, yRatio, wRatio, hRatio, workerId) {
    const W = img.width, H = img.height;
    const sx = Math.floor(W * xRatio), sy = Math.floor(H * yRatio);
    const sw = Math.floor(W * wRatio), sh = Math.floor(H * hRatio);
    if (sw < 40 || sh < 40) return null;
    const crop = document.createElement('canvas');
    crop.width = sw; crop.height = sh;
    crop.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
    const scale = document.createElement('canvas');
    scale.width = Math.round(sw * 2.5); scale.height = Math.round(sh * 2.5);
    scale.getContext('2d').drawImage(crop, 0, 0, scale.width, scale.height);
    return new Promise((resolve) => {
      scale.toBlob(async (blob) => {
        if (!blob) { resolve(null); return; }
        const f = new File([blob], 'crop.jpg', { type: 'image/jpeg' });
        try {
          const qr = new Html5Qrcode(workerId);
          const result = await qr.scanFile(f, false);
          resolve(result ? result.trim() : null);
        } catch (e) { resolve(null); }
      }, 'image/jpeg', 0.96);
    });
  }

  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = async (e) => {
      const img = new Image();
      img.onload = async () => {
        const found = [];
        const add = (v) => { if (v && v.trim() && !found.includes(v.trim())) found.push(v.trim()); };

        const fullCanvas = document.createElement('canvas');
        fullCanvas.width = img.width; fullCanvas.height = img.height;
        fullCanvas.getContext('2d').drawImage(img, 0, 0);
        await new Promise((res) => {
          fullCanvas.toBlob(async (blob) => {
            if (!blob) { res(); return; }
            const f = new File([blob], 'full.jpg', { type: 'image/jpeg' });
            try {
              const qr = new Html5Qrcode('_photoScanWorker');
              const result = await qr.scanFile(f, false);
              add(result);
            } catch (e) {}
            res();
          }, 'image/jpeg', 0.95);
        });

        const regions = [
          [0.58, 0.07, 0.42, 0.26, '_qrCrop1'],
          [0.58, 0.28, 0.42, 0.26, '_qrCrop2'],
          [0.58, 0.48, 0.42, 0.24, '_qrCrop3'],
          [0.50, 0.05, 0.50, 0.30, '_qrCrop4'],
          [0.50, 0.26, 0.50, 0.30, '_qrCrop5'],
          [0.50, 0.46, 0.50, 0.28, '_qrCrop6'],
        ];
        for (const [x, y, w, h, id] of regions) {
          const result = await cropAndScan(img, x, y, w, h, id);
          add(result);
        }
        resolve(found);
      };
      img.onerror = () => resolve([]);
      img.src = e.target.result;
    };
    reader.onerror = () => resolve([]);
    reader.readAsDataURL(file);
  });
}

async function extractHUFromImage(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = async (e) => {
      const img = new Image();
      img.onload = async () => {
        const W = img.width, H = img.height;
        const cropY = H * 0.50, cropH = H * 0.25;
        const canvas = document.createElement('canvas');
        canvas.width = W; canvas.height = Math.floor(cropH);
        canvas.getContext('2d').drawImage(img, 0, cropY, W, cropH, 0, 0, W, Math.floor(cropH));
        const scaled = document.createElement('canvas');
        scaled.width = W * 2; scaled.height = Math.floor(cropH) * 2;
        scaled.getContext('2d').drawImage(canvas, 0, 0, scaled.width, scaled.height);
        scaled.toBlob(async (blob) => {
          if (!blob) { resolve(null); return; }
          const f = new File([blob], 'barcode.jpg', { type: 'image/jpeg' });
          try {
            const qr = new Html5Qrcode('_barcodeWorker');
            const result = await qr.scanFile(f, false);
            const v = result ? result.trim() : null;
            // Accept 18-20 digit numeric barcodes starting with 1000
            if (v && HU_PATTERN.test(v) && v.startsWith('1000')) resolve(v);
            else resolve(null);
          } catch (e) { resolve(null); }
        }, 'image/jpeg', 0.97);
      };
      img.onerror = () => resolve(null);
      img.src = e.target.result;
    };
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}


// ═══════════════════════════════════════════════════════════
//  LOCATION MODAL
// ═══════════════════════════════════════════════════════════

// Called by STOCK IN / STOCK OUT buttons — opens the location popup first
function openLocationModal(movementType) {
  _pendingMovementType = movementType;
  _selectedLocation = '';

  // Reset button highlights
  document.querySelectorAll('.loc-btn').forEach(b => b.classList.remove('selected'));

  // Update modal title to show the movement type
  const label = movementType === 'STOCK_IN' ? 'Stock IN' : 'Stock OUT';
  document.getElementById('locModalTitle').textContent = `Select Location \u2014 ${label}`;

  // Disable POST until a location is chosen
  document.getElementById('locPostBtn').disabled = true;

  document.getElementById('locationModal').classList.add('on');
}

function selectLocation(loc) {
  _selectedLocation = loc;

  // Highlight the selected button, clear others
  document.querySelectorAll('.loc-btn').forEach(b => {
    b.classList.toggle('selected', b.textContent.trim() === loc);
  });

  // Enable the POST button now that a location is chosen
  document.getElementById('locPostBtn').disabled = false;
}

function closeLocationModal() {
  document.getElementById('locationModal').classList.remove('on');
  _pendingMovementType = '';
  _selectedLocation = '';
  document.querySelectorAll('.loc-btn').forEach(b => b.classList.remove('selected'));
  document.getElementById('locPostBtn').disabled = true;
}

function confirmLocation() {
  if (!_selectedLocation) {
    toast('Please select a location first.', 'warn');
    return;
  }
  const movementType = _pendingMovementType;
  const loc = _selectedLocation;
  closeLocationModal();
  doMovement(movementType, false, '', '', loc);
}


// ═══════════════════════════════════════════════════════════
//  MOVEMENT
// ═══════════════════════════════════════════════════════════

function _lockMovementButtons() {
  const btnIn  = document.querySelector('.btn-ok[onclick*="STOCK_IN"]');
  const btnOut = document.querySelector('.btn-err[onclick*="STOCK_OUT"]');
  if (btnIn)  { btnIn.disabled  = true;  btnIn.style.opacity  = '0.5'; }
  if (btnOut) { btnOut.disabled = true;  btnOut.style.opacity = '0.5'; }
}

function _unlockMovementButtons() {
  const btnIn  = document.querySelector('.btn-ok[onclick*="STOCK_IN"]');
  const btnOut = document.querySelector('.btn-err[onclick*="STOCK_OUT"]');
  if (btnIn)  { btnIn.disabled  = false; btnIn.style.opacity  = ''; }
  if (btnOut) { btnOut.disabled = false; btnOut.style.opacity = ''; }
}

// location parameter added; called by confirmLocation() for normal movements
// and by submitOverride() for supervisor overrides.
async function doMovement(movementType, isOverride = false, pin = '', reason = '', location = '') {
  if (movementInFlight) return;
  movementInFlight = true;
  _lockMovementButtons();

  try {
    if (!scanned.handling_unit) { toast('Scan the barcode first.', 'warn'); return; }
    if (!scanned.indus_part)    { toast('Scan the Indus Part QR code.', 'warn'); return; }

    const timestamp = new Date().toISOString();
    const idemKey   = _makeIdemKey(scanned.handling_unit, movementType, timestamp);

    const payload = {
      handling_unit:   scanned.handling_unit,
      movement_type:   movementType,
      indus_part:      scanned.indus_part,
      customer_part:   scanned.customer_part,
      pallet_qty:      scanned.pallet_qty,
      job_no:          '',
      operator_id:     'WORKER',
      device_id:       deviceId,
      is_override:     isOverride,
      supervisor_pin:  pin,
      override_reason: reason,
      timestamp:       timestamp,
      idempotency_key: idemKey,
      location:        location,
    };

    if (!navigator.onLine) {
      await OfflineQueue.add(payload);
      updatePendingBadge();
      showResult('pending', movementType, 'Saved offline. Will sync when internet returns.', {}, location);
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
        showResult('ok', movementType, data.message, data.data, location);
      } else if (data.data && data.data.needs_sync) {
        await OfflineQueue.add(payload);
        updatePendingBadge();
        showResult('pending', movementType, data.message, {}, location);
      } else {
        showResult('fail', movementType, data.message, {}, location);
      }
    } catch (e) {
      hideSpinner();
      await OfflineQueue.add(payload);
      updatePendingBadge();
      showResult('pending', movementType, 'Network error. Saved locally.', {}, location);
    }
  } finally {
    movementInFlight = false;
    _unlockMovementButtons();
  }
}

function showResult(type, movementType, message, extra = {}, location = '') {
  const banner = document.getElementById('resultBanner');
  const icon   = document.getElementById('resultIcon');
  const title  = document.getElementById('resultTitle');
  const msg    = document.getElementById('resultMsg');
  const info   = document.getElementById('resultInfo');

  const displayType = movementType === 'STOCK_IN' ? 'Stock IN' : 'Stock OUT';

  if (type === 'ok') {
    banner.className = 'banner ok';   icon.textContent = '\u2713';
    title.textContent = `${displayType} \u2014 SUCCESS`;
  } else if (type === 'pending') {
    banner.className = 'banner pend'; icon.textContent = '\u23f3';
    title.textContent = `${displayType} \u2014 SAVED OFFLINE`;
  } else {
    banner.className = 'banner fail'; icon.textContent = '\u2715';
    title.textContent = `${displayType} \u2014 FAILED`;
  }
  msg.textContent = message;

  const now = new Date().toLocaleString();
  let html = '';
  html += _row('Handling Unit', scanned.handling_unit);
  html += _row('Indus Part', scanned.indus_part);
  html += _row('Customer Part', scanned.customer_part);
  html += _row('Pallet Qty', scanned.pallet_qty);
  html += _row('Location', location || (extra && extra.location) || '\u2014');
  html += _row('Time', now);
  if (extra && extra.event_id) html += _row('Event ID', extra.event_id);
  if (extra && extra.is_override) html += _row('Override', 'YES');
  info.innerHTML = html;

  showScreen('scrResult');
}

function _row(label, value) {
  return `<div class="row"><span class="lbl">${label}</span><span class="val">${value || '\u2014'}</span></div>`;
}


// ═══════════════════════════════════════════════════════════
//  OVERRIDE
// ═══════════════════════════════════════════════════════════

function showOverrideModal() {
  document.getElementById('overrideModal').classList.add('on');
  document.getElementById('pinInput').value = '';
  document.getElementById('overrideReason').value = '';
  document.getElementById('overrideLocation').value = '';
  document.getElementById('pinInput').focus();
}
function closeOverride() { document.getElementById('overrideModal').classList.remove('on'); }

async function submitOverride() {
  const pin      = document.getElementById('pinInput').value.trim();
  const reason   = document.getElementById('overrideReason').value.trim();
  const movement = document.getElementById('overrideMovement').value;
  const location = document.getElementById('overrideLocation').value;
  if (!pin)      { toast('Enter supervisor PIN.', 'warn'); return; }
  if (!location) { toast('Select a location for this override.', 'warn'); return; }
  if (!reason)   { toast('Reason is required.', 'warn'); return; }
  closeOverride();
  await doMovement(movement, true, pin, reason, location);
}


// ═══════════════════════════════════════════════════════════
//  OFFLINE SYNC
// ═══════════════════════════════════════════════════════════

async function syncPendingEvents() {
  const events = await OfflineQueue.getAll();
  if (!events.length) return;
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
          await OfflineQueue.removeByKey(r.idempotency_key);
          synced++;
        }
      }
      if (synced > 0) toast(`Synced ${synced} event(s).`, 'ok');
    }
    updatePendingBadge();
  } catch (e) { console.warn('Sync failed:', e); }
}
setInterval(() => { if (navigator.onLine) syncPendingEvents(); }, 30000);


// ═══════════════════════════════════════════════════════════
//  HELPERS
// ═══════════════════════════════════════════════════════════

function _makeIdemKey(hu, mt, ts) {
  const raw = `${hu}:${mt}:${ts}`;
  let hash = 0;
  for (let i = 0; i < raw.length; i++) {
    hash = ((hash << 5) - hash) + raw.charCodeAt(i);
    hash |= 0;
  }
  return 'CL-' + Math.abs(hash).toString(36).toUpperCase().padStart(8, '0');
}
