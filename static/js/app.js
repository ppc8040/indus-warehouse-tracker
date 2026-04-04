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

// ── State ──
let scanned = {
  handling_unit: '',   // from barcode (long numeric, 10-18 digits)
  indus_part: '',      // from QR 1 (alphanumeric part code)
  customer_part: '',   // from QR 2 (alphanumeric, may be pure number)
  pallet_qty: '',      // from QR 3 (numeric, small number)
};
let scanner = null;
let scannerRunning = false;
let allScannedCodes = new Set();  // track all raw scanned values to avoid re-processing
let qrScanOrder = 0;              // counts non-HU QR scans: 1=indus, 2=cust, 3=qty

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
  // Populate confirmation screen
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

  // Explicitly enable QR + Code 128 + other barcode formats
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

  // 16:9 aspect ratio gives the camera maximum horizontal width — essential for
  // wide Code 128 barcodes. Wider qrbox also improves barcode read reliability.
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
      btn.innerHTML = '<span class="btn-icon">📷</span> START CAMERA';
    }).catch(() => {
      scannerRunning = false;
      btn.innerHTML = '<span class="btn-icon">📷</span> START CAMERA';
    });
  }
}

function onScanSuccess(decodedText) {
  const val = decodedText.trim();
  if (!val || allScannedCodes.has(val)) return;  // already processed
  
  allScannedCodes.add(val);
  classifyAndStore(val);
  updateScanDisplay();
  
  // Vibrate feedback
  if (navigator.vibrate) navigator.vibrate(100);
  
  // Check if all 4 are done
  const count = getScannedCount();
  if (count >= 4) {
    toast('All codes scanned! Review and proceed.', 'ok', 3000);
    stopScanner();
  } else {
    toast(`Scanned: ${val.substring(0, 20)}${val.length > 20 ? '...' : ''}`, 'info', 1500);
  }
}

function classifyAndStore(val) {
  /*
    SMART CLASSIFICATION — value format rules based on label spec:

    HANDLING UNIT:  16–18 digits, starts with 1000
    INDUS PART:     3 uppercase letters followed by 4 digits  (e.g. CCR2324, CEJ1982)
    PALLET QTY:     1–3 digit integer (1–999)
    CUSTOMER PART:  anything else that doesn't match the above
                    (alphanumeric, mixed, long numbers like 8750722840, ENG-1440, etc.)

    Rules are checked in priority order. The FIRST matching rule wins.
    Customer Part is the catch-all — it gets whatever doesn't match the others.
  */

  const v = val.trim();
  if (!v) return;

  // ── 1. Handling Unit: 16–18 digits starting with 1000 ──
  if (/^\d{16,18}$/.test(v) && v.startsWith('1000')) {
    if (!scanned.handling_unit) scanned.handling_unit = v;
    return;
  }

  // ── 2. Indus Part: exactly 3 uppercase letters + 4 digits ──
  //    e.g. CCR2324, CEJ1982, CCG1900
  if (/^[A-Z]{3}\d{4}$/.test(v)) {
    if (!scanned.indus_part) scanned.indus_part = v;
    return;
  }

  // ── 3. Pallet Qty: small integer 1–999 (no letters, no dashes) ──
  if (/^\d{1,3}$/.test(v) && parseInt(v, 10) >= 1) {
    if (!scanned.pallet_qty) scanned.pallet_qty = v;
    return;
  }

  // ── 4. Customer Part: everything else ──
  //    covers: ENG-1440, 161878, 8750722840, mixed alphanumeric, etc.
  if (!scanned.customer_part) {
    scanned.customer_part = v;
    return;
  }

  // All slots full — silently ignore
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
  
  // Enable proceed button when at least HU + Indus Part scanned
  const btn = document.getElementById('btnProceed');
  btn.disabled = !(scanned.handling_unit && scanned.indus_part);
}

function _updateField(key, val) {
  const statusEl = document.getElementById(`st-${key}`);
  const valueEl = document.getElementById(`val-${key}`);
  if (val) {
    statusEl.textContent = '✓ SCANNED';
    statusEl.className = 'sf-status scanned';
    valueEl.textContent = val;
    valueEl.className = 'sf-value scanned';
  } else {
    statusEl.textContent = 'NOT SCANNED';
    statusEl.className = 'sf-status';
    valueEl.textContent = '—';
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
  const val = document.getElementById('huManualInput').value.trim();
  if (!val) { toast('Type the HU number from the label.', 'warn'); return; }
  if (!/^\d{10,20}$/.test(val)) { toast('HU should be 10-20 digits. Check the number.', 'warn'); return; }
  scanned.handling_unit = val;
  allScannedCodes.add(val);
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
  const val = document.getElementById('indusManualInput').value.trim().toUpperCase();
  if (!val) { toast('Type the Indus Part code from the label.', 'warn'); return; }
  scanned.indus_part = val;
  allScannedCodes.add(val);
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
  const val = document.getElementById('custManualInput').value.trim();
  if (!val) { toast('Type the Customer Part from the label.', 'warn'); return; }
  scanned.customer_part = val;
  allScannedCodes.add(val);
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
  const val = document.getElementById('qtyManualInput').value.trim();
  if (!val) { toast('Type the pallet quantity from the label.', 'warn'); return; }
  if (!/^\d{1,4}$/.test(val) || parseInt(val, 10) < 1) { toast('Qty should be a positive number (e.g. 39).', 'warn'); return; }
  scanned.pallet_qty = val;
  allScannedCodes.add(val);
  updateScanDisplay();
  document.getElementById('qtyManualRow').style.display = 'none';
  toast('Pallet Qty set manually.', 'ok', 2000);
}


// ═══════════════════════════════════════════════════════════
//  PHOTO SCAN — capture full label image for OCR parsing
// ═══════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════
//  PHOTO SCAN — reads QR codes + barcode number from a still image
//  Uses html5-qrcode (already loaded) — no server call needed
// ═══════════════════════════════════════════════════════════

async function processLabelPhoto(input) {
  const file = input.files[0];
  if (!file) return;

  showSpinner('Reading label from photo...');

  try {
    const results = await scanAllCodesFromImage(file);

    // Also try the barcode crop for HU
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

    // Classify each found value
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
      toast('✓ All fields read from photo!', 'ok', 3000);
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


// Scan a full image for ALL QR codes by scanning multiple crops of the image.
// html5-qrcode's scanFile only returns ONE code per call, so we crop the image
// into regions targeting each QR position on the label and scan each crop.
async function scanAllCodesFromImage(file) {
  /*
    Scans a still label photo for all 4 codes.

    Strategy:
      1. Full image scan first (catches whatever is most prominent)
      2. Targeted crops for each QR position (right side of label)
         — each crop is scaled up 2.5x before scanning for better decode accuracy
      3. Barcode crop is handled separately by extractHUFromImage()

    QR positions are based on the fixed Indus label layout:
      QR1 (Indus Part):    top-right,  ~8%–32% of height,  right 38% of width
      QR2 (Customer Part): middle-right, ~30%–54% of height, right 38% of width
      QR3 (Pallet Qty):    lower-right,  ~50%–70% of height, right 38% of width
  */

  async function cropAndScan(img, xRatio, yRatio, wRatio, hRatio, workerId) {
    const W = img.width, H = img.height;
    const sx = Math.floor(W * xRatio);
    const sy = Math.floor(H * yRatio);
    const sw = Math.floor(W * wRatio);
    const sh = Math.floor(H * hRatio);

    // Minimum crop size guard
    if (sw < 40 || sh < 40) return null;

    // Draw crop
    const crop = document.createElement('canvas');
    crop.width = sw; crop.height = sh;
    crop.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);

    // Scale up 2.5x — small QR codes decode much better at higher resolution
    const scale = document.createElement('canvas');
    scale.width = Math.round(sw * 2.5);
    scale.height = Math.round(sh * 2.5);
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

        const add = (v) => {
          if (v && v.trim() && !found.includes(v.trim())) found.push(v.trim());
        };

        // Pass 1: full image
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

        // Pass 2: targeted crops for each QR position
        // xRatio, yRatio, wRatio, hRatio
        const regions = [
          [0.58, 0.07, 0.42, 0.26, '_qrCrop1'],   // QR1: Indus Part
          [0.58, 0.28, 0.42, 0.26, '_qrCrop2'],   // QR2: Customer Part
          [0.58, 0.48, 0.42, 0.24, '_qrCrop3'],   // QR3: Pallet Qty
          // Also try slightly wider crops in case photo is angled
          [0.50, 0.05, 0.50, 0.30, '_qrCrop4'],   // QR1 wide
          [0.50, 0.26, 0.50, 0.30, '_qrCrop5'],   // QR2 wide
          [0.50, 0.46, 0.50, 0.28, '_qrCrop6'],   // QR3 wide
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
  // Crop the barcode strip (middle-lower area of label) and try to read Code 128
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = async (e) => {
      const img = new Image();
      img.onload = async () => {
        const W = img.width;
        const H = img.height;

        // Barcode sits roughly between 52%–72% of label height, full width
        const cropY = H * 0.50;
        const cropH = H * 0.25;

        const canvas = document.createElement('canvas');
        canvas.width = W;
        canvas.height = Math.floor(cropH);
        canvas.getContext('2d').drawImage(img, 0, cropY, W, cropH, 0, 0, W, Math.floor(cropH));

        // Scale up 2x — wider barcodes decode better at higher resolution
        const scaled = document.createElement('canvas');
        scaled.width = W * 2;
        scaled.height = Math.floor(cropH) * 2;
        scaled.getContext('2d').drawImage(canvas, 0, 0, scaled.width, scaled.height);

        scaled.toBlob(async (blob) => {
          if (!blob) { resolve(null); return; }
          const f = new File([blob], 'barcode.jpg', { type: 'image/jpeg' });
          try {
            const qr = new Html5Qrcode('_barcodeWorker');
            const result = await qr.scanFile(f, false);
            const v = result ? result.trim() : null;
            if (v && /^\d{16,20}$/.test(v) && v.startsWith('1000')) {
              resolve(v);
            } else {
              resolve(null);
            }
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
//  MOVEMENT
// ═══════════════════════════════════════════════════════════

async function doMovement(movementType, isOverride = false, pin = '', reason = '') {
  if (!scanned.handling_unit) { toast('Scan the barcode first.', 'warn'); return; }
  if (!scanned.indus_part) { toast('Scan the Indus Part QR code.', 'warn'); return; }

  const timestamp = new Date().toISOString();
  const idemKey = _makeIdemKey(scanned.handling_unit, movementType, timestamp);

  const payload = {
    handling_unit: scanned.handling_unit,
    movement_type: movementType,
    indus_part: scanned.indus_part,
    customer_part: scanned.customer_part,
    pallet_qty: scanned.pallet_qty,
    job_no: '',
    operator_id: 'WORKER',
    device_id: deviceId,
    is_override: isOverride,
    supervisor_pin: pin,
    override_reason: reason,
    timestamp: timestamp,
    idempotency_key: idemKey,
  };

  if (!navigator.onLine) {
    await OfflineQueue.add(payload);
    updatePendingBadge();
    showResult('pending', movementType, 'Saved offline. Will sync when internet returns.');
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
      showResult('ok', movementType, data.message, data.data);
    } else if (data.data && data.data.needs_sync) {
      await OfflineQueue.add(payload);
      updatePendingBadge();
      showResult('pending', movementType, data.message);
    } else {
      showResult('fail', movementType, data.message);
    }
  } catch (e) {
    hideSpinner();
    await OfflineQueue.add(payload);
    updatePendingBadge();
    showResult('pending', movementType, 'Network error. Saved locally.');
  }
}

function showResult(type, movementType, message, extra = {}) {
  const banner = document.getElementById('resultBanner');
  const icon = document.getElementById('resultIcon');
  const title = document.getElementById('resultTitle');
  const msg = document.getElementById('resultMsg');
  const info = document.getElementById('resultInfo');

  const displayType = movementType === 'STOCK_IN' ? 'Stock IN' : 'Stock OUT';

  if (type === 'ok') {
    banner.className = 'banner ok'; icon.textContent = '✓';
    title.textContent = `${displayType} — SUCCESS`;
  } else if (type === 'pending') {
    banner.className = 'banner pend'; icon.textContent = '⏳';
    title.textContent = `${displayType} — SAVED OFFLINE`;
  } else {
    banner.className = 'banner fail'; icon.textContent = '✕';
    title.textContent = `${displayType} — FAILED`;
  }
  msg.textContent = message;

  const now = new Date().toLocaleString();
  let html = '';
  html += _row('Handling Unit', scanned.handling_unit);
  html += _row('Indus Part', scanned.indus_part);
  html += _row('Customer Part', scanned.customer_part);
  html += _row('Pallet Qty', scanned.pallet_qty);
  html += _row('Time', now);
  if (extra.event_id) html += _row('Event ID', extra.event_id);
  if (extra.is_override) html += _row('Override', 'YES');
  info.innerHTML = html;

  showScreen('scrResult');
}

function _row(label, value) {
  return `<div class="row"><span class="lbl">${label}</span><span class="val">${value || '—'}</span></div>`;
}


// ═══════════════════════════════════════════════════════════
//  OVERRIDE
// ═══════════════════════════════════════════════════════════

function showOverrideModal() {
  document.getElementById('overrideModal').classList.add('on');
  document.getElementById('pinInput').value = '';
  document.getElementById('overrideReason').value = '';
  document.getElementById('pinInput').focus();
}
function closeOverride() { document.getElementById('overrideModal').classList.remove('on'); }

async function submitOverride() {
  const pin = document.getElementById('pinInput').value.trim();
  const reason = document.getElementById('overrideReason').value.trim();
  const movement = document.getElementById('overrideMovement').value;
  if (!pin) { toast('Enter supervisor PIN.', 'warn'); return; }
  if (!reason) { toast('Reason is required.', 'warn'); return; }
  closeOverride();
  await doMovement(movement, true, pin, reason);
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
