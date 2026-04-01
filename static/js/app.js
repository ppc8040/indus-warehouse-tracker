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
    Classification logic — ORDER-BASED for QR codes:
    
    HU barcode detection (only reliable format-based rule):
      - 10+ digits, all numeric → Handling Unit (Code 128 barcode)
    
    QR codes are assigned by order scanned (qrScanOrder counter):
      - 1st QR scanned → Indus Part
      - 2nd QR scanned → Customer Part
      - 3rd QR scanned → Pallet Qty
    
    This avoids misclassification when Customer Part is a plain number
    (e.g. "161878" — previously sent to Pallet Qty by the old heuristic).
  */
// NEW — paste this in place of the above:
   const isAllDigits = /^\d+$/.test(val);
   
  // Priority 1: HU barcode — must be 16-20 digits starting with 1000
  if (isAllDigits && val.length >= 16 && val.length <= 20 && val.startsWith('1000') && !scanned.handling_unit) {
    scanned.handling_unit = val;
    return;
  }

  // Reject other long digit strings (garbage partial barcode reads)
  if (isAllDigits && val.length >= 10) {
    allScannedCodes.delete(val); // allow a clean retry
    return;
  }

  // Priority 2: small number (1–4 digits) = Pallet Qty
  if (isAllDigits && val.length <= 4 && !scanned.pallet_qty) {
    scanned.pallet_qty = val;
    return;
  }

  // Priority 3: everything else → Indus Part, then Customer Part
  if (!scanned.indus_part) {
    scanned.indus_part = val;
    qrScanOrder++;
  } else if (!scanned.customer_part) {
    scanned.customer_part = val;
    qrScanOrder++;
  } else if (!scanned.pallet_qty) {
    scanned.pallet_qty = val;
    qrScanOrder++;
  }
  // Extra/duplicate values beyond 4 are silently ignored
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

function resetScans() {
  scanned = { handling_unit: '', indus_part: '', customer_part: '', pallet_qty: '' };
  allScannedCodes.clear();
  qrScanOrder = 0;
  updateScanDisplay();
  // Reset manual HU input
  const row = document.getElementById('huManualRow');
  const btn = document.getElementById('huManualBtn');
  if (row) row.style.display = 'none';
  if (btn) btn.style.display = 'block';
  const inp = document.getElementById('huManualInput');
  if (inp) inp.value = '';
}

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
    // html5-qrcode can scan a still image file for QR codes
    const html5QrCode = new Html5Qrcode('_photoScanWorker');
    const results = [];

    try {
      // scanFile returns the first code found; we need all 3 QRs
      // We call it once — it returns only one result per call on most builds
      // So we use the decodedText and also manually scan for the HU number in the image
      const decodedText = await html5QrCode.scanFile(file, false);
      if (decodedText) results.push(decodedText.trim());
    } catch (e) {
      // scanFile throws if nothing found — that's fine, we handle below
    }

    // html5-qrcode v2.3+ supports scanFileV2 which returns all codes
    // Try that if available
    if (typeof html5QrCode.scanFileV2 === 'function') {
      try {
        const res2 = await html5QrCode.scanFileV2(file, false);
        if (res2 && res2.decodedText) {
          const v = res2.decodedText.trim();
          if (v && !results.includes(v)) results.push(v);
        }
      } catch (e) { /* no more codes or not supported */ }
    }

    hideSpinner();

    // Also extract the HU number from image using canvas + text pattern
    // The HU number is printed in large text below the barcode on the label
    const huFromImage = await extractHUFromImage(file);
    if (huFromImage && !results.includes(huFromImage)) {
      results.push(huFromImage);
    }

    if (results.length === 0 && !huFromImage) {
      toast('No codes found in photo. Make sure the full label is visible and try again.', 'warn', 5000);
      input.value = '';
      return;
    }

    // Classify each found value into the correct field
    let filled = 0;
    for (const val of results) {
      const before = getScannedCount();
      if (!allScannedCodes.has(val)) {
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
      toast(`Read ${filled} field(s). Still missing: ${missing.join(', ')}. Take another photo or type HU manually.`, 'warn', 6000);
    } else {
      toast('Could not read codes from this photo. Hold phone steady, ensure good lighting, and try again.', 'warn', 5000);
    }

  } catch (err) {
    hideSpinner();
    console.error('Photo scan error:', err);
    toast('Photo scan failed. Try again with better lighting.', 'fail');
  }

  input.value = ''; // reset so same button can be tapped again
}


async function extractHUFromImage(file) {
  // Draw the image onto a canvas, then use html5-qrcode to try reading the barcode
  // The HU number is also printed as plain text under the barcode — extract via canvas OCR trick:
  // We look for a standalone 18-digit number in the image using a simple pixel scan approach.
  // For now: attempt to read the barcode (Code 128) from the still image using html5-qrcode.
  try {
    const reader = new FileReader();
    return await new Promise((resolve) => {
      reader.onload = async (e) => {
        const img = new Image();
        img.onload = async () => {
          // Create a narrow horizontal crop of the barcode region (bottom third of label)
          const canvas = document.createElement('canvas');
          canvas.width = img.width;
          canvas.height = Math.floor(img.height * 0.35); // bottom 35%
          const ctx = canvas.getContext('2d');
          // Draw just the lower portion of the image (where the barcode lives)
          ctx.drawImage(img, 0, img.height * 0.55, img.width, img.height * 0.45, 0, 0, canvas.width, canvas.height);

          // Convert cropped canvas back to a File and scan it
          canvas.toBlob(async (blob) => {
            if (!blob) { resolve(null); return; }
            const croppedFile = new File([blob], 'crop.jpg', { type: 'image/jpeg' });
            try {
              const scanner = new Html5Qrcode('_barcodeWorker');
              const result = await scanner.scanFile(croppedFile, false);
              if (result && /^\d{10,20}$/.test(result.trim())) {
                resolve(result.trim());
              } else {
                resolve(null);
              }
            } catch (e) { resolve(null); }
          }, 'image/jpeg', 0.95);
        };
        img.onerror = () => resolve(null);
        img.src = e.target.result;
      };
      reader.onerror = () => resolve(null);
      reader.readAsDataURL(file);
    });
  } catch (e) {
    return null;
  }
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
