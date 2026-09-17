(() => {
  'use strict';

  const DRAFT_KEY = 'indus.loadingDraft.v1';

  const statusEl = document.getElementById('reviewStatus');
  const sourceEl = document.getElementById('reviewSource');
  const countEl = document.getElementById('reviewCount');
  const itemsEl = document.getElementById('reviewItems');
  const overridePanel = document.getElementById('overridePanel');
  const supervisorPinEl = document.getElementById('supervisorPin');
  const overrideReasonEl = document.getElementById('overrideReason');
  const reviewAgainButton = document.getElementById('reviewAgainButton');
  const confirmButton = document.getElementById('confirmLoadingButton');
  const dialog = document.getElementById('confirmDialog');
  const finalPostButton = document.getElementById('finalPostButton');
  const confirmDialogText = document.getElementById('confirmDialogText');
  const shipmentReferenceEl = document.getElementById('shipmentReference');

  let draft = null;
  let lastReview = null;
  let postInFlight = false;

  function loadDraft() {
    try {
      const raw = sessionStorage.getItem(DRAFT_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function saveDraft() {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function setStatus(message, type = '') {
    statusEl.textContent = message;
    statusEl.className = `status-message ${type}`.trim();
  }

  function buildPayload(includeSupervisor) {
    return {
      idempotency_key: draft.idempotency_key,
      source_location: draft.source_location,
      operator_id: draft.operator_id || 'WORKER',
      device_id: draft.device_id || '',
      shipment_reference: shipmentReferenceEl
        ? shipmentReferenceEl.value.trim()
        : '',
      items: draft.items,
      supervisor_override: {
        approved: Boolean(
          includeSupervisor &&
          lastReview?.data?.requires_supervisor_approval
        ),
        pin: includeSupervisor ? supervisorPinEl.value : '',
        reason: includeSupervisor ? overrideReasonEl.value.trim() : ''
      }
    };
  }

  function renderDraft() {
    sourceEl.textContent = draft?.source_location || '—';
    countEl.textContent = String(draft?.items?.length || 0);
  
    if (!draft || !Array.isArray(draft.items) || draft.items.length === 0) {
      itemsEl.innerHTML = '<tr><td colspan="6">No loading draft found.</td></tr>';
      confirmButton.disabled = true;
      return;
    }
  
    itemsEl.innerHTML = draft.items.map((item, index) => `
      <tr>
        <td>${index + 1}</td>
        <td>${escapeHtml(item.handling_unit)}</td>
        <td>${escapeHtml(item.customer_part || '—')}</td>
        <td>${escapeHtml(item.indus_part)}</td>
        <td>${escapeHtml(item.pallet_qty)}</td>
        <td id="review-row-${index}">Not reviewed</td>
      </tr>
    `).join('');
  }

  function renderReview(result) {
    lastReview = result;
    const data = result.data || {};
    const reviewedItems = data.items || [];

    overridePanel.hidden = !data.requires_supervisor_approval;

    reviewedItems.forEach((item, index) => {
      const cell = document.getElementById(`review-row-${index}`);
      if (!cell) return;

      cell.textContent = item.message || item.resolution || 'Reviewed';
      cell.className = item.eligible ? 'review-ok' : 'review-error';
    });

    const blockedCount = Number(data.blocked_count || 0);
    const requiresSupervisor = Boolean(data.requires_supervisor_approval);

    if (blockedCount > 0) {
      setStatus('Some HUs are blocked. Return to the scanner list and correct them.', 'error');
      confirmButton.disabled = true;
      return;
    }

    if (requiresSupervisor) {
      setStatus('Supervisor approval is required for missing HUs.', 'warning');
      confirmButton.disabled = false;
      return;
    }

    setStatus('All HUs are eligible for posting to LOADING.', 'success');
    confirmButton.disabled = false;
  }

  async function review() {
    if (!draft || !draft.items?.length) {
      setStatus('No saved loading draft exists. Return to the scanner.', 'error');
      return;
    }

    confirmButton.disabled = true;
    setStatus('Checking HUs against warehouse records…');

    try {
      const response = await fetch('/api/loadings/review', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildPayload(false))
      });

      const result = await response.json();

      if (!response.ok && !result.data?.items) {
        setStatus(result.message || 'Loading review failed.', 'error');
        return;
      }

      renderReview(result);
    } catch {
      setStatus('Could not contact the server. Check your connection and try again.', 'error');
    }
  }

  function validateSupervisorFields() {
    if (!lastReview?.data?.requires_supervisor_approval) {
      return true;
    }

    if (!supervisorPinEl.value.trim()) {
      setStatus('Enter the supervisor PIN before posting.', 'error');
      supervisorPinEl.focus();
      return false;
    }

    if (!overrideReasonEl.value.trim()) {
      setStatus('Enter the supervisor override reason before posting.', 'error');
      overrideReasonEl.focus();
      return false;
    }

    return true;
  }

  async function postLoading() {
    if (postInFlight) return;
    if (!validateSupervisorFields()) return;

    postInFlight = true;
    finalPostButton.disabled = true;
    setStatus('Posting loading…');

    try {
      const response = await fetch('/api/loadings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildPayload(true))
      });

      const result = await response.json();

      if (!response.ok || !result.success) {
        setStatus(result.message || 'Could not post loading. The draft was kept.', 'error');
        return;
      }

      sessionStorage.removeItem(DRAFT_KEY);

      const replayed = result.data?.replayed ? ' (already posted safely)' : '';
      setStatus(`${result.message}${replayed}`, 'success');

      confirmButton.disabled = true;
      reviewAgainButton.disabled = true;

      setTimeout(() => {
        window.location.assign('/');
      }, 1200);
    } catch {
      setStatus('Could not post loading. The draft was kept; retry safely.', 'error');
    } finally {
      postInFlight = false;
      finalPostButton.disabled = false;
    }
  }

  reviewAgainButton.addEventListener('click', review);

  confirmButton.addEventListener('click', () => {
    if (!lastReview) {
      setStatus('Review the loading before confirming.', 'error');
      return;
    }

    if (!validateSupervisorFields()) return;

    const count = draft.items.length;
    confirmDialogText.textContent =
      `This will post ${count} HU(s) from ${draft.source_location} to LOADING. ` +
      'This action creates inventory movements and cannot be undone automatically.';
    
    dialog.showModal();
    
    if (shipmentReferenceEl) {
      shipmentReferenceEl.focus();
    }
  });

  finalPostButton.addEventListener('click', (event) => {
    event.preventDefault();
    dialog.close();
    postLoading();
  });

  draft = loadDraft();

  if (!draft) {
    setStatus('No saved loading draft exists. Return to the scanner.', 'error');
  }

  renderDraft();
  review();
})();
