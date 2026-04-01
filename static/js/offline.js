/* ═══════════════════════════════════════════════════════════
   Offline Queue — IndexedDB storage for pending events
   ═══════════════════════════════════════════════════════════ */
'use strict';

const OfflineQueue = (() => {
  const DB_NAME = 'IndusWarehouseOffline';
  const DB_VERSION = 1;
  const STORE = 'pending_events';

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains(STORE)) {
          const store = db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
          store.createIndex('idempotency_key', 'idempotency_key', { unique: true });
          store.createIndex('created_at', 'created_at', { unique: false });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function add(eventPayload) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      const store = tx.objectStore(STORE);
      const record = {
        payload: eventPayload,
        idempotency_key: eventPayload.idempotency_key || '',
        created_at: new Date().toISOString(),
        attempts: 0,
      };
      const req = store.add(record);
      req.onsuccess = () => {
        console.log('Offline event queued:', record.idempotency_key);
        resolve(req.result);
      };
      req.onerror = () => {
        // Duplicate key — already queued, that's fine
        if (req.error && req.error.name === 'ConstraintError') {
          console.log('Event already in offline queue (duplicate key).');
          resolve(null);
        } else {
          reject(req.error);
        }
      };
    });
  }

  async function getAll() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly');
      const store = tx.objectStore(STORE);
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }

  async function removeByKey(idempotencyKey) {
    if (!idempotencyKey) return;
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      const store = tx.objectStore(STORE);
      const idx = store.index('idempotency_key');
      const req = idx.openCursor(IDBKeyRange.only(idempotencyKey));
      req.onsuccess = (e) => {
        const cursor = e.target.result;
        if (cursor) {
          cursor.delete();
          console.log('Removed synced event:', idempotencyKey);
        }
        resolve();
      };
      req.onerror = () => reject(req.error);
    });
  }

  async function count() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly');
      const store = tx.objectStore(STORE);
      const req = store.count();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function clear() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      const store = tx.objectStore(STORE);
      const req = store.clear();
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
  }

  return { add, getAll, removeByKey, count, clear };
})();
