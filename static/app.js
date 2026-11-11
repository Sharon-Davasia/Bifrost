/* ==========================================================================
   Bifrost frontend
   - Vanilla JavaScript, no frameworks, no inline handlers, no innerHTML for
     user-controlled data (everything is built with createElement/textContent).
   - One EventSource per authenticated tab; the inbox is reconciled against
     GET /api/items on every (re)connect with buffered event replay.
   ========================================================================== */
(function () {
  'use strict';

  var MAX_NOTE_CHARS = 100000; // fallback until the server reports its limit
  var DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024;
  var MAX_DEVICE_CHARS = 40;
  var MAX_CONCURRENT_UPLOADS = 3;
  var STORAGE_DEVICE = 'bifrost.device';
  var COMPACT_UPLOAD_ROW_MS = 4000;
  var LOOPBACK_HOSTS = ['localhost', '127.0.0.1', '::1', '[::1]'];
  var SVG_NS = 'http://www.w3.org/2000/svg';

  var state = {
    deviceName: '',
    authenticated: false,
    items: [],
    syncing: false,
    pendingEvents: [],
    syncChain: Promise.resolve(),
    eventSource: null,
    connected: false,
    everConnected: false,
    sseFailures: 0,
    uploads: [],
    uploadSeq: 0,
    noteBusy: false,
    authBusy: false,
    lockedUntil: 0,
    lockoutTimer: null,
    rowTimers: new Map(),
    maxNoteChars: MAX_NOTE_CHARS,
    maxUploadBytes: DEFAULT_MAX_UPLOAD_BYTES
  };

  var cards = new Map();

  var dom = {};

  /* ---------------------------------------------------------------- utils */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function iconEl(name, className) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', className || 'icon');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }

  function iconButton(iconName, label, onClick, extraClass) {
    var button = el('button', ('icon-btn ' + (extraClass || '')).trim());
    button.type = 'button';
    button.setAttribute('aria-label', label);
    button.title = label;
    button.appendChild(iconEl(iconName));
    button.addEventListener('click', onClick);
    return button;
  }

  function formatBytes(bytes) {
    var value = Number(bytes);
    if (!isFinite(value) || value < 0) return '';
    if (value < 1024) return value + ' B';
    var units = ['KB', 'MB', 'GB', 'TB'];
    var unit = 0;
    value = value / 1024;
    while (value >= 1024 && unit < units.length - 1) {
      value = value / 1024;
      unit += 1;
    }
    var rounded = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
    return rounded + ' ' + units[unit];
  }

  function formatRelative(isoString) {
    var then = Date.parse(isoString);
    if (isNaN(then)) return '';
    var seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 45) return 'just now';
    if (seconds < 90) return '1 min ago';
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + ' min ago';
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours === 1 ? '1 hour ago' : hours + ' hours ago';
    var days = Math.floor(hours / 24);
    if (days === 1) return 'yesterday';
    if (days < 7) return days + ' days ago';
    var date = new Date(then);
    var sameYear = date.getFullYear() === new Date().getFullYear();
    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: sameYear ? undefined : 'numeric'
    });
  }

  function formatAbsolute(isoString) {
    var parsed = Date.parse(isoString);
    if (isNaN(parsed)) return '';
    return new Date(parsed).toLocaleString();
  }

  function encodeHeader(value) {
    return encodeURIComponent(String(value == null ? '' : value));
  }

  function codePointLength(text) {
    return Array.from(text).length;
  }

  function sanitizeDeviceName(raw) {
    if (raw == null) return '';
    var text = String(raw).normalize('NFC');
    var out = '';
    for (var index = 0; index < text.length; index += 1) {
      var char = text.charAt(index);
      var code = text.charCodeAt(index);
      if (char === '\u200d') {
        out += char;
        continue;
      }
      var blocked =
        code <= 0x1f ||
        (code >= 0x7f && code <= 0x9f) ||
        (code >= 0x200b && code <= 0x200f) ||
        (code >= 0x202a && code <= 0x202e) ||
        (code >= 0x2060 && code <= 0x206f) ||
        code === 0xfeff;
      out += blocked ? ' ' : char;
    }
    out = out.replace(/\s+/gu, ' ').trim();
    if (out.length > MAX_DEVICE_CHARS) out = out.slice(0, MAX_DEVICE_CHARS).trim();
    return out;
  }

  function isLoopbackHost(hostname) {
    return LOOPBACK_HOSTS.indexOf(String(hostname || '').toLowerCase()) !== -1;
  }

  function readJsonSafe(response) {
    return response
      .json()
      .catch(function () {
        return null;
      });
  }

  function extractError(xhr) {
    try {
      var data = JSON.parse(xhr.responseText);
      if (data && typeof data.error === 'string' && data.error) return data.error;
    } catch (error) {
      /* ignore */
    }
    return '';
  }

  /* --------------------------------------------------------------- toasts */
  var TOAST_ICONS = {
    success: 'i-check',
    error: 'i-alert',
    warn: 'i-alert',
    info: 'i-info'
  };

  function toast(message, kind, timeout) {
    var variant = TOAST_ICONS[kind] ? kind : 'info';
    var node = el('div', 'toast toast-' + variant);
    node.appendChild(iconEl(TOAST_ICONS[variant]));
    node.appendChild(el('span', 'toast-text', message));
    node.addEventListener('click', function () {
      dismissToast(node);
    });
    dom.toasts.appendChild(node);
    while (dom.toasts.children.length > 4) {
      dom.toasts.removeChild(dom.toasts.firstElementChild);
    }
    window.setTimeout(function () {
      dismissToast(node);
    }, timeout || 4200);
  }

  function dismissToast(node) {
    if (!node.isConnected || node.classList.contains('is-leaving')) return;
    node.classList.add('is-leaving');
    window.setTimeout(function () {
      node.remove();
    }, 220);
  }

  /* --------------------------------------------------------------- device */
  function defaultDeviceName() {
    var ua = navigator.userAgent || '';
    if (/Android/i.test(ua)) return 'Android Phone';
    if (/iPhone/i.test(ua)) return 'iPhone';
    if (/iPad/i.test(ua)) return 'iPad';
    if (/Macintosh|Mac OS X/i.test(ua)) return 'Mac';
    if (/Windows/i.test(ua)) return 'Windows PC';
    if (/Linux/i.test(ua)) return 'Linux Device';
    return 'Browser';
  }

  function loadDeviceName() {
    var stored = '';
    try {
      stored = window.localStorage.getItem(STORAGE_DEVICE) || '';
    } catch (error) {
      stored = '';
    }
    return sanitizeDeviceName(stored) || defaultDeviceName();
  }

  function persistDeviceName(name) {
    try {
      window.localStorage.setItem(STORAGE_DEVICE, name);
    } catch (error) {
      /* private mode - keep the in-memory value only */
    }
  }

  function syncDeviceChip() {
    dom.deviceChipLabel.textContent = state.deviceName;
    dom.deviceChip.title = 'Device name: ' + state.deviceName + ' (tap to change)';
    dom.deviceChip.setAttribute('aria-label', 'Device name: ' + state.deviceName + '. Change device name');
  }

  function applyDeviceName(raw) {
    var cleaned = sanitizeDeviceName(raw) || defaultDeviceName();
    state.deviceName = cleaned;
    persistDeviceName(cleaned);
    syncDeviceChip();
    toast('Device name set to “' + cleaned + '”.', 'info', 2600);
  }

  function openDeviceDialog() {
    dom.deviceInput.value = state.deviceName;
    if (typeof dom.deviceDialog.showModal === 'function') {
      dom.deviceDialog.showModal();
      window.setTimeout(function () {
        dom.deviceInput.select();
      }, 10);
    } else {
      var value = window.prompt('Device name (up to 40 characters)', state.deviceName);
      if (value !== null) applyDeviceName(value);
    }
  }

  /* ------------------------------------------------------------ connection */
  function setConnection(name) {
    var labels = {
      live: 'Live',
      connecting: 'Connecting…',
      reconnecting: 'Reconnecting…',
      offline: 'Offline',
      idle: 'Not paired'
    };
    var label = labels[name] || 'Connecting…';
    dom.conn.dataset.state = name;
    dom.connLabel.textContent = label;
    dom.conn.setAttribute('aria-label', 'Connection status: ' + label);
  }

  function setAuthBusy(busy) {
    dom.authSubmit.classList.toggle('is-busy', busy);
    dom.authSubmit.textContent = busy ? 'Pairing…' : 'Pair device';
    updateAuthButton();
  }

  function setAuthError(message) {
    if (!message) {
      dom.authError.textContent = '';
      dom.authError.hidden = true;
      return;
    }
    dom.authError.textContent = message;
    dom.authError.hidden = false;
  }

  function showAuthScreen(message) {
    dom.appMain.hidden = true;
    dom.authScreen.hidden = false;
    setAuthError(message || '');
    updateAuthButton();
  }

  function handleSessionExpired() {
    if (!state.authenticated) return;
    state.authenticated = false;
    closeEvents();
    resetInbox();
    setConnection('idle');
    showAuthScreen('Your session expired. Enter the PIN shown in the Bifrost terminal to pair again.');
    toast('Session expired. Please pair this device again.', 'error');
    maybeShowHostPanel();
  }

  /* ------------------------------------------------------------- PIN entry */
  function pinInputs() {
    return Array.prototype.slice.call(dom.pinField.querySelectorAll('input'));
  }

  function pinValue() {
    return pinInputs()
      .map(function (input) {
        return input.value;
      })
      .join('');
  }

  function isPinComplete() {
    return /^\d{6}$/.test(pinValue());
  }

  function fillPinInputs(pin) {
    var digits = String(pin || '').replace(/\D/g, '').slice(0, 6).split('');
    pinInputs().forEach(function (input, index) {
      input.value = digits[index] || '';
    });
    var target = pinInputs()[Math.min(digits.length, 5)];
    if (target) target.focus();
  }

  function clearPinInputs() {
    pinInputs().forEach(function (input) {
      input.value = '';
    });
  }

  function focusPinAt(index) {
    var inputs = pinInputs();
    var target = inputs[Math.max(0, Math.min(index, inputs.length - 1))];
    if (target) {
      target.focus();
      target.select();
    }
  }

  function updateAuthButton() {
    var locked = state.lockedUntil > Date.now();
    var ready = isPinComplete() && !state.authBusy && !locked;
    dom.authSubmit.disabled = !ready;
    pinInputs().forEach(function (input) {
      input.disabled = locked;
    });
  }

  function startLockout(seconds) {
    state.lockedUntil = Date.now() + Math.max(1, seconds) * 1000;
    if (state.lockoutTimer) window.clearInterval(state.lockoutTimer);
    state.lockoutTimer = window.setInterval(function () {
      var remaining = Math.ceil((state.lockedUntil - Date.now()) / 1000);
      if (remaining <= 0) {
        window.clearInterval(state.lockoutTimer);
        state.lockoutTimer = null;
        state.lockedUntil = 0;
        setAuthError('');
        updateAuthButton();
        focusPinAt(0);
        return;
      }
      setAuthError('Too many incorrect attempts. Try again in ' + remaining + 's.');
      updateAuthButton();
    }, 250);
  }

  function parseRetrySeconds(message) {
    var match = /(\d+)\s*second/i.exec(String(message || ''));
    return match ? parseInt(match[1], 10) : 60;
  }

  function shakeAuth() {
    dom.authScreen.classList.remove('shake');
    void dom.authScreen.offsetWidth;
    dom.authScreen.classList.add('shake');
  }

  /* --------------------------------------------------------------- session */
  function deviceHeaders(extra) {
    var headers = { 'X-Device-Name': encodeHeader(state.deviceName) };
    if (extra) {
      Object.keys(extra).forEach(function (key) {
        headers[key] = extra[key];
      });
    }
    return headers;
  }

  function applyLimits(limits) {
    if (limits && typeof limits === 'object') {
      if (Number.isFinite(limits.max_upload_bytes) && limits.max_upload_bytes > 0) {
        state.maxUploadBytes = limits.max_upload_bytes;
      }
      if (Number.isFinite(limits.max_note_chars) && limits.max_note_chars > 0) {
        state.maxNoteChars = limits.max_note_chars;
      }
    }
    if (state.maxUploadBytes) {
      dom.uploadLimit.textContent =
        'Up to ' + formatBytes(state.maxUploadBytes) + ' per file · ' +
        MAX_CONCURRENT_UPLOADS + ' at a time';
      dom.uploadLimit.hidden = false;
    }
    updateNoteCount();
  }

  async function fetchItems() {
    var response = await fetch('/api/items', {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: deviceHeaders()
    });
    if (response.status === 401) {
      handleSessionExpired();
      return null;
    }
    if (!response.ok) throw new Error('Could not load the shared inbox.');
    var data = await readJsonSafe(response);
    // The snapshot also carries the server's configured limits.
    applyLimits(data && data.limits);
    return normalizeItems(data && data.items);
  }

  function normalizeItems(items) {
    // The server already returns the snapshot newest first; preserving that
    // order avoids losing precision when two items share a timestamp.
    if (!Array.isArray(items)) return [];
    return items.filter(function (item) {
      return item && typeof item.id === 'string';
    });
  }

  async function bootstrapSession() {
    try {
      var items = await fetchItems();
      if (items === null) return false;
      state.items = items;
      state.authenticated = true;
      renderInbox();
      await enterApp(true);
      return true;
    } catch (error) {
      showAuthScreen();
      return false;
    }
  }

  async function enterApp(alreadyLoaded) {
    state.authenticated = true;
    dom.authScreen.hidden = true;
    dom.appMain.hidden = false;
    setConnection(state.connected ? 'live' : 'connecting');
    if (!alreadyLoaded) {
      try {
        var items = await fetchItems();
        if (items) {
          state.items = items;
          renderInbox();
        }
      } catch (error) {
        /* the SSE resync will retry */
      }
    }
    connectEvents();
    scheduleResync('enter');
  }

  async function attemptAuth(pin) {
    if (state.authBusy) return;
    state.authBusy = true;
    setAuthBusy(true);
    setAuthError('');
    try {
      var response = await fetch('/api/auth', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: pin, device: state.deviceName })
      });
      if (response.ok) {
        clearPinInputs();
        state.lockedUntil = 0;
        await enterApp(false);
        toast('Paired. You are connected.', 'success');
        return;
      }
      var data = await readJsonSafe(response);
      var message = (data && data.error) || 'Pairing failed.';
      if (response.status === 429) {
        setAuthError(message);
        startLockout(parseRetrySeconds(message));
        toast('Too many attempts. Please wait a moment.', 'error');
      } else if (response.status === 401) {
        setAuthError('That PIN is not correct. Check the terminal and try again.');
        shakeAuth();
        toast('Incorrect PIN.', 'error');
      } else {
        setAuthError(message);
        toast(message, 'error');
      }
      clearPinInputs();
      if (!state.lockedUntil) focusPinAt(0);
    } catch (error) {
      setAuthError('Could not reach the Bifrost server. Check that it is running and on the same network.');
      toast('Connection failed.', 'error');
    } finally {
      state.authBusy = false;
      setAuthBusy(false);
    }
  }

  /* ------------------------------------------------------------------ SSE */
  function receiveEvent(type, raw) {
    var data = null;
    try {
      data = JSON.parse(raw);
    } catch (error) {
      return;
    }
    if (!data || typeof data.id !== 'string') return;
    if (state.syncing) {
      state.pendingEvents.push({ type: type, data: data });
      return;
    }
    applyEvent(type, data);
  }

  function applyEvent(type, data) {
    if (type === 'added') {
      upsertItem(data);
    } else if (type === 'deleted') {
      removeItem(data.id);
    }
  }

  function scheduleResync(reason) {
    state.syncChain = state.syncChain
      .then(function () {
        return resync(reason);
      })
      .catch(function () {
        /* keep the chain alive */
      });
    return state.syncChain;
  }

  async function resync(reason) {
    if (!state.authenticated) return;
    state.syncing = true;
    state.pendingEvents.length = 0;
    try {
      var items = await fetchItems();
      if (items === null) return;
      state.items = items;
      renderInbox();
      var buffered = state.pendingEvents.slice();
      state.pendingEvents.length = 0;
      buffered.forEach(function (event) {
        applyEvent(event.type, event.data);
      });
    } catch (error) {
      if (reason === 'open' && state.connected) {
        toast('Could not refresh the inbox. Retrying…', 'warn');
      }
    } finally {
      state.syncing = false;
    }
  }

  function connectEvents() {
    if (!state.authenticated || state.eventSource) return;
    var source;
    try {
      source = new EventSource('/api/events');
    } catch (error) {
      setConnection('offline');
      return;
    }
    state.eventSource = source;

    source.addEventListener('open', function () {
      var isReconnect = state.everConnected;
      state.connected = true;
      state.sseFailures = 0;
      state.everConnected = true;
      setConnection('live');
      if (isReconnect) toast('Connection restored.', 'success');
      scheduleResync('open');
    });

    source.addEventListener('error', function () {
      state.connected = false;
      state.sseFailures += 1;
      setConnection(navigator.onLine === false ? 'offline' : 'reconnecting');
      if (state.sseFailures === 1) {
        toast('Connection lost. Reconnecting…', 'warn');
      }
      if (state.sseFailures >= 3) {
        verifySession();
      }
      if (source.readyState === 2) {
        state.eventSource = null;
        window.setTimeout(function () {
          connectEvents();
        }, 3000);
      }
    });

    source.addEventListener('added', function (event) {
      receiveEvent('added', event.data);
    });

    source.addEventListener('deleted', function (event) {
      receiveEvent('deleted', event.data);
    });
  }

  function closeEvents() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    state.connected = false;
    state.everConnected = false;
    state.sseFailures = 0;
  }

  async function verifySession() {
    try {
      var response = await fetch('/api/items', {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: deviceHeaders()
      });
      if (response.status === 401) handleSessionExpired();
    } catch (error) {
      /* offline: EventSource keeps retrying */
    }
  }

  /* ---------------------------------------------------------------- inbox */
  function resetInbox() {
    state.items = [];
    state.pendingEvents.length = 0;
    state.syncing = false;
    cards.forEach(function (card) {
      card.remove();
    });
    cards.clear();
    renderInbox();
  }

  function upsertItem(item) {
    if (!item || typeof item.id !== 'string') return;
    for (var index = 0; index < state.items.length; index += 1) {
      if (state.items[index].id === item.id) {
        state.items[index] = item;
        renderInbox();
        return;
      }
    }
    // A freshly created item is always the newest, so it goes to the top.
    state.items.unshift(item);
    renderInbox();
  }

  function removeItem(id) {
    var next = state.items.filter(function (item) {
      return item.id !== id;
    });
    if (next.length === state.items.length) return;
    state.items = next;
    renderInbox();
  }

  function renderInbox() {
    var list = dom.inboxList;
    var seen = new Set();
    state.items.forEach(function (item) {
      seen.add(item.id);
      var card = cards.get(item.id);
      if (!card) {
        card = buildItemCard(item);
        cards.set(item.id, card);
      } else {
        updateItemTime(card, item);
      }
      list.appendChild(card);
    });
    cards.forEach(function (card, id) {
      if (!seen.has(id)) {
        card.remove();
        cards.delete(id);
      }
    });
    var count = state.items.length;
    dom.emptyState.hidden = count > 0;
    dom.inboxCount.textContent = count + (count === 1 ? ' item' : ' items');
  }

  function updateItemTime(card, item) {
    var time = card.querySelector('time[data-created-at]');
    if (!time) return;
    time.dateTime = item.created_at || '';
    time.dataset.createdAt = item.created_at || '';
    time.textContent = formatRelative(item.created_at);
    time.title = formatAbsolute(item.created_at);
  }

  function makeTime(isoString) {
    var time = document.createElement('time');
    time.className = 'item-time';
    time.dateTime = isoString || '';
    time.dataset.createdAt = isoString || '';
    time.textContent = formatRelative(isoString);
    time.title = formatAbsolute(isoString);
    return time;
  }

  function makeMedia(item) {
    var media = el('div', 'item-media');
    if (item.kind === 'file' && typeof item.thumb_url === 'string' && item.thumb_url) {
      var image = document.createElement('img');
      image.className = 'thumb';
      image.alt = '';
      image.loading = 'lazy';
      image.decoding = 'async';
      image.width = 56;
      image.height = 56;
      image.addEventListener(
        'error',
        function () {
          media.replaceChildren(buildFileFallback(item));
        },
        { once: true }
      );
      image.src = item.thumb_url;
      media.appendChild(image);
    } else if (item.kind === 'file') {
      media.appendChild(buildFileFallback(item));
    } else {
      var noteWrap = el('div', 'item-kind-icon');
      noteWrap.appendChild(iconEl('i-note'));
      media.appendChild(noteWrap);
    }
    return media;
  }

  function buildFileFallback(item) {
    var wrapper = el('div', 'item-kind-icon');
    var mime = typeof item.mime === 'string' ? item.mime : '';
    wrapper.appendChild(iconEl(mime.indexOf('image/') === 0 ? 'i-image' : 'i-file'));
    var name = typeof item.name === 'string' ? item.name : '';
    var dot = name.lastIndexOf('.');
    var extension = dot > 0 && dot < name.length - 1 ? name.slice(dot + 1) : '';
    if (extension && extension.length <= 5) {
      wrapper.appendChild(el('span', 'file-badge', extension.toUpperCase()));
    }
    return wrapper;
  }

  function buildMeta(item) {
    var meta = el('p', 'item-meta');
    var sender = el('span', 'meta-sender', item.sender || 'Unknown device');
    meta.appendChild(sender);
    meta.appendChild(el('span', 'sep', '·'));
    if (item.kind === 'note') {
      var chars = typeof item.size === 'number' ? item.size : 0;
      meta.appendChild(el('span', null, chars.toLocaleString() + (chars === 1 ? ' character' : ' characters')));
    } else {
      meta.appendChild(el('span', null, formatBytes(item.size)));
    }
    meta.appendChild(el('span', 'sep', '·'));
    meta.appendChild(makeTime(item.created_at));
    return meta;
  }

  function buildItemCard(item) {
    var card = el('article', 'item');
    card.dataset.itemId = item.id;
    card.dataset.kind = item.kind === 'note' ? 'note' : 'file';

    card.appendChild(makeMedia(item));

    var body = el('div', 'item-body');
    if (item.kind === 'note') {
      var text = el('p', 'note-text');
      text.textContent = item.text || '';
      body.appendChild(text);
    } else {
      var title = el('p', 'item-title');
      title.textContent = item.name || 'Untitled file';
      body.appendChild(title);
    }
    body.appendChild(buildMeta(item));
    card.appendChild(body);

    var actions = el('div', 'item-actions');
    var label = item.kind === 'note' ? 'this note' : item.name || 'this file';
    if (item.kind === 'file') {
      var link = document.createElement('a');
      link.className = 'icon-btn';
      link.href = '/api/files/' + encodeURIComponent(item.id);
      link.setAttribute('download', item.name || '');
      link.setAttribute('aria-label', 'Download ' + label);
      link.title = 'Download';
      link.appendChild(iconEl('i-download'));
      actions.appendChild(link);
    } else {
      actions.appendChild(
        iconButton('i-copy', 'Copy note from ' + (item.sender || 'unknown device'), function () {
          copyNote(item);
        })
      );
    }
    actions.appendChild(
      iconButton(
        'i-trash',
        'Delete ' + label,
        function () {
          deleteItem(item);
        },
        'is-danger'
      )
    );
    card.appendChild(actions);

    return card;
  }

  function refreshTimes() {
    var nodes = document.querySelectorAll('time[data-created-at]');
    Array.prototype.forEach.call(nodes, function (node) {
      node.textContent = formatRelative(node.dataset.createdAt);
    });
  }

  /* --------------------------------------------------------------- actions */
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (error) {
        /* fall through to the execCommand fallback */
      }
    }
    var helper = document.createElement('textarea');
    helper.className = 'clipboard-helper';
    helper.value = text;
    helper.setAttribute('readonly', '');
    document.body.appendChild(helper);
    var selection = document.getSelection();
    var previousRange = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
    helper.select();
    helper.setSelectionRange(0, helper.value.length);
    var copied = false;
    try {
      copied = document.execCommand('copy');
    } catch (error) {
      copied = false;
    }
    helper.remove();
    if (previousRange && selection) {
      selection.removeAllRanges();
      selection.addRange(previousRange);
    }
    return copied;
  }

  async function copyNote(item) {
    var ok = false;
    try {
      ok = await copyText(item.text || '');
    } catch (error) {
      ok = false;
    }
    toast(ok ? 'Note copied to clipboard.' : 'Could not copy the note.', ok ? 'success' : 'error');
  }

  async function deleteItem(item) {
    var label = item.kind === 'note' ? 'this note' : '"' + (item.name || 'this file') + '"';
    if (!window.confirm('Delete ' + label + '? This cannot be undone.')) return;
    try {
      var response = await fetch('/api/items/' + encodeURIComponent(item.id), {
        method: 'DELETE',
        credentials: 'same-origin',
        headers: deviceHeaders()
      });
      if (response.status === 401) {
        handleSessionExpired();
        return;
      }
      if (!response.ok) {
        var data = await readJsonSafe(response);
        throw new Error((data && data.error) || 'Could not delete the item.');
      }
      removeItem(item.id);
      toast(item.kind === 'note' ? 'Note deleted.' : 'File deleted.', 'success');
    } catch (error) {
      toast(error.message || 'Could not delete the item.', 'error');
    }
  }

  /* --------------------------------------------------------------- uploads */
  function makeUploadEntry(file) {
    state.uploadSeq += 1;
    return {
      id: state.uploadSeq,
      file: file,
      status: 'queued',
      progress: 0,
      error: '',
      xhr: null,
      row: null,
      nodes: null,
      localOnly: false
    };
  }

  function handleFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    var tooLarge = 0;
    files.forEach(function (file) {
      var entry = makeUploadEntry(file);
      // Fail fast rather than spending the user's bandwidth on a file the
      // server is guaranteed to refuse.
      if (state.maxUploadBytes && file.size > state.maxUploadBytes) {
        entry.status = 'failed';
        entry.localOnly = true;
        entry.error = 'Larger than the ' + formatBytes(state.maxUploadBytes) + ' per-file limit.';
        tooLarge += 1;
      }
      state.uploads.push(entry);
    });
    state.uploads.forEach(function (entry) {
      if (!entry.row) buildUploadRow(entry);
    });
    updateAllUploadRows();
    pumpQueue();
    if (tooLarge) {
      var limit = formatBytes(state.maxUploadBytes);
      toast(
        tooLarge === 1
          ? 'One file is larger than the ' + limit + ' limit.'
          : tooLarge + ' files are larger than the ' + limit + ' limit.',
        'error'
      );
    }
  }

  function buildUploadRow(entry) {
    var row = el('li', 'upload-row');
    row.dataset.uploadId = String(entry.id);

    var head = el('div', 'upload-head');
    var name = el('span', 'upload-name', entry.file.name);
    name.title = entry.file.name;
    head.appendChild(name);
    head.appendChild(el('span', 'upload-size', formatBytes(entry.file.size)));

    var progress = document.createElement('progress');
    progress.className = 'upload-progress';
    progress.max = 100;
    progress.value = 0;
    progress.setAttribute('aria-label', 'Upload progress for ' + entry.file.name);

    var foot = el('div', 'upload-foot');
    var status = el('span', 'upload-status', 'Waiting…');
    var percent = el('span', 'upload-pct', '');
    var actions = el('span', 'upload-actions');

    foot.appendChild(status);
    foot.appendChild(percent);
    foot.appendChild(actions);

    row.appendChild(head);
    row.appendChild(progress);
    row.appendChild(foot);
    dom.uploadQueue.appendChild(row);

    entry.row = row;
    entry.nodes = { progress: progress, status: status, percent: percent, actions: actions };
  }

  function updateUploadRow(entry) {
    if (!entry.row || !entry.nodes) return;
    var nodes = entry.nodes;
    entry.row.dataset.status = entry.status;
    var percentValue = Math.max(0, Math.min(100, Math.floor(entry.progress * 100)));

    if (entry.status === 'queued') {
      nodes.status.textContent = 'Waiting…';
      nodes.percent.textContent = '';
      nodes.progress.value = 0;
      nodes.progress.setAttribute('aria-valuenow', '0');
      nodes.actions.replaceChildren();
    } else if (entry.status === 'uploading') {
      if (entry.progress >= 1) {
        nodes.status.textContent = 'Finishing…';
      } else {
        nodes.status.textContent = 'Uploading…';
      }
      nodes.percent.textContent = percentValue + '%';
      nodes.progress.value = percentValue;
      nodes.progress.setAttribute('aria-valuenow', String(percentValue));
      nodes.actions.replaceChildren();
    } else if (entry.status === 'done') {
      nodes.status.textContent = 'Uploaded';
      nodes.percent.textContent = '100%';
      nodes.progress.value = 100;
      nodes.progress.setAttribute('aria-valuenow', '100');
      nodes.actions.replaceChildren();
    } else {
      nodes.status.textContent = entry.error || 'Upload failed.';
      nodes.percent.textContent = '';
      nodes.progress.value = 0;
      nodes.progress.setAttribute('aria-valuenow', '0');
      var dismiss = el('button', 'btn btn-ghost', 'Dismiss');
      dismiss.type = 'button';
      dismiss.addEventListener('click', function () {
        removeUpload(entry);
      });
      if (entry.localOnly) {
        nodes.actions.replaceChildren(dismiss);
      } else {
        var retry = el('button', 'btn btn-ghost', 'Retry');
        retry.type = 'button';
        retry.addEventListener('click', function () {
          retryUpload(entry);
        });
        nodes.actions.replaceChildren(retry, dismiss);
      }
    }
  }

  function updateAllUploadRows() {
    state.uploads.forEach(updateUploadRow);
  }

  function activeUploads() {
    return state.uploads.filter(function (entry) {
      return entry.status === 'uploading';
    }).length;
  }

  function pumpQueue() {
    var slots = MAX_CONCURRENT_UPLOADS - activeUploads();
    for (var index = 0; index < state.uploads.length && slots > 0; index += 1) {
      var entry = state.uploads[index];
      if (entry.status === 'queued') {
        startUpload(entry);
        slots -= 1;
      }
    }
  }

  function startUpload(entry) {
    entry.status = 'uploading';
    entry.progress = 0;
    entry.error = '';
    updateUploadRow(entry);

    var xhr = new XMLHttpRequest();
    entry.xhr = xhr;
    xhr.open('POST', '/api/upload', true);
    xhr.setRequestHeader('X-Filename', encodeHeader(entry.file.name));
    xhr.setRequestHeader('X-Device-Name', encodeHeader(state.deviceName));
    xhr.setRequestHeader('Content-Type', entry.file.type || 'application/octet-stream');

    xhr.upload.addEventListener('progress', function (event) {
      if (!event.lengthComputable || !event.total) return;
      var ratio = Math.min(1, event.loaded / event.total);
      if (ratio <= entry.progress) return;
      entry.progress = ratio;
      updateUploadRow(entry);
    });

    xhr.addEventListener('load', function () {
      onUploadComplete(entry, xhr);
    });
    xhr.addEventListener('error', function () {
      onUploadFailed(entry, 'Network error. Check the connection and retry.');
    });
    xhr.addEventListener('timeout', function () {
      onUploadFailed(entry, 'The upload timed out. Please retry.');
    });
    xhr.addEventListener('abort', function () {
      onUploadFailed(entry, 'Upload cancelled.');
    });

    xhr.send(entry.file);
  }

  function onUploadComplete(entry, xhr) {
    entry.xhr = null;
    if (xhr.status >= 200 && xhr.status < 300) {
      var payload = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch (error) {
        payload = null;
      }
      entry.status = 'done';
      entry.progress = 1;
      entry.error = '';
      updateUploadRow(entry);
      if (payload && payload.item) upsertItem(payload.item);
      toast(entry.file.name + ' uploaded.', 'success');
      if (!state.connected) scheduleResync('upload-fallback');
      scheduleRowRemoval(entry);
    } else if (xhr.status === 401) {
      entry.status = 'failed';
      entry.error = 'Session expired. Pair this device again.';
      updateUploadRow(entry);
      handleSessionExpired();
    } else {
      var message = extractError(xhr) || 'Upload failed (HTTP ' + xhr.status + ').';
      entry.status = 'failed';
      entry.error = message;
      updateUploadRow(entry);
      toast(entry.file.name + ': ' + message, 'error', 6000);
    }
    pumpQueue();
  }

  function onUploadFailed(entry, message) {
    if (entry.xhr) entry.xhr = null;
    if (entry.status === 'done' || entry.status === 'failed') return;
    entry.status = 'failed';
    entry.error = message;
    updateUploadRow(entry);
    toast(entry.file.name + ': ' + message, 'error');
    pumpQueue();
  }

  function retryUpload(entry) {
    if (!entry || entry.status !== 'failed' || entry.localOnly) return;
    entry.status = 'queued';
    entry.progress = 0;
    entry.error = '';
    updateUploadRow(entry);
    pumpQueue();
  }

  function scheduleRowRemoval(entry) {
    cancelRowRemoval(entry);
    var timer = window.setTimeout(function () {
      removeUpload(entry);
    }, COMPACT_UPLOAD_ROW_MS);
    state.rowTimers.set(entry.id, timer);
  }

  function cancelRowRemoval(entry) {
    var timer = state.rowTimers.get(entry.id);
    if (timer) {
      window.clearTimeout(timer);
      state.rowTimers.delete(entry.id);
    }
  }

  function removeUpload(entry) {
    cancelRowRemoval(entry);
    if (entry.xhr) {
      try {
        entry.xhr.abort();
      } catch (error) {
        /* ignore */
      }
      entry.xhr = null;
    }
    if (entry.row) entry.row.remove();
    state.uploads = state.uploads.filter(function (candidate) {
      return candidate.id !== entry.id;
    });
    pumpQueue();
  }

  /* ----------------------------------------------------------------- notes */
  function updateNoteCount() {
    var length = codePointLength(dom.noteInput.value);
    var limit = state.maxNoteChars;
    dom.noteCount.textContent = length.toLocaleString() + ' / ' + limit.toLocaleString();
    dom.noteCount.classList.toggle('is-near', length >= limit * 0.9 && length <= limit);
    dom.noteCount.classList.toggle('is-over', length > limit);
    var over = length > limit;
    dom.noteSend.disabled = over || state.noteBusy;
    if (over) {
      dom.noteCount.setAttribute('aria-label', 'Note is too long');
    } else {
      dom.noteCount.removeAttribute('aria-label');
    }
  }

  function setNoteBusy(busy) {
    dom.noteSend.classList.toggle('is-busy', busy);
    dom.noteSend.disabled = busy;
    var label = dom.noteSend.querySelector('span');
    if (label) label.textContent = busy ? 'Sending…' : 'Send note';
    updateNoteCount();
  }

  async function sendNote() {
    if (state.noteBusy) return;
    var text = dom.noteInput.value;
    if (!text.trim()) {
      toast('Write something to share first.', 'warn');
      dom.noteInput.focus();
      return;
    }
    if (codePointLength(text) > state.maxNoteChars) {
      toast('Notes are limited to ' + state.maxNoteChars.toLocaleString() + ' characters.', 'error');
      return;
    }
    state.noteBusy = true;
    setNoteBusy(true);
    try {
      var response = await fetch('/api/note', {
        method: 'POST',
        credentials: 'same-origin',
        headers: deviceHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ text: text })
      });
      if (response.status === 401) {
        handleSessionExpired();
        return;
      }
      if (!response.ok) {
        var data = await readJsonSafe(response);
        throw new Error((data && data.error) || 'Could not send the note.');
      }
      var payload = await readJsonSafe(response);
      if (payload && payload.item) upsertItem(payload.item);
      dom.noteInput.value = '';
      updateNoteCount();
      toast('Note sent.', 'success');
      if (!state.connected) scheduleResync('note-fallback');
    } catch (error) {
      toast(error.message || 'Could not send the note.', 'error');
    } finally {
      state.noteBusy = false;
      setNoteBusy(false);
      dom.noteInput.focus();
    }
  }

  /* ----------------------------------------------------------- host panel */
  async function maybeShowHostPanel() {
    if (!isLoopbackHost(window.location.hostname)) return;
    try {
      var response = await fetch('/api/pairing-info', {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'X-Device-Name': encodeHeader(state.deviceName) }
      });
      if (!response.ok) return;
      var data = await readJsonSafe(response);
      if (!data || typeof data.pin !== 'string' || !/^\d{6}$/.test(data.pin)) return;
      dom.hostPin.textContent = data.pin;
      if (typeof data.lan_url === 'string' && data.lan_url) {
        dom.hostLanUrl.textContent = data.lan_url;
      }
      dom.hostPanel.hidden = false;
    } catch (error) {
      /* Offline or remote: the panel simply stays hidden. */
    }
  }

  /* --------------------------------------------------------- PIN fragment */
  function readPinFragment() {
    var hash = window.location.hash || '';
    if (!hash) return null;
    var pin = null;
    var direct = /^#pin=(\d{6})$/.exec(hash);
    if (direct) {
      pin = direct[1];
    } else if (hash.charAt(0) === '#') {
      try {
        var params = new URLSearchParams(hash.slice(1));
        var candidate = params.get('pin');
        if (candidate && /^\d{6}$/.test(candidate)) pin = candidate;
      } catch (error) {
        pin = null;
      }
    }
    if (!pin) return null;
    // Remove the fragment before any network request so the PIN never travels
    // to the server and never lingers in the address bar or history.
    try {
      window.history.replaceState(null, document.title, window.location.pathname + window.location.search);
    } catch (error) {
      window.location.hash = '';
    }
    return pin;
  }

  /* ------------------------------------------------------------------ wiring */
  function setupPinField() {
    var inputs = pinInputs();
    inputs.forEach(function (input, index) {
      input.addEventListener('input', function () {
        var digits = input.value.replace(/\D/g, '');
        if (digits.length > 1) {
          fillPinInputs(pinValue());
          updateAuthButton();
          if (isPinComplete() && !state.authBusy) attemptAuth(pinValue());
          return;
        }
        input.value = digits;
        if (digits) focusPinAt(index + 1);
        updateAuthButton();
        if (isPinComplete() && !state.authBusy && state.lockedUntil <= Date.now()) {
          attemptAuth(pinValue());
        }
      });

      input.addEventListener('keydown', function (event) {
        if (event.key === 'Backspace') {
          if (!input.value) {
            focusPinAt(index - 1);
            event.preventDefault();
          }
          window.setTimeout(updateAuthButton, 0);
        } else if (event.key === 'ArrowLeft') {
          focusPinAt(index - 1);
          event.preventDefault();
        } else if (event.key === 'ArrowRight') {
          focusPinAt(index + 1);
          event.preventDefault();
        } else if (event.key === 'Enter') {
          event.preventDefault();
          if (isPinComplete()) attemptAuth(pinValue());
        }
      });

      input.addEventListener('paste', function (event) {
        var clipboard = event.clipboardData || window.clipboardData;
        if (!clipboard) return;
        var digits = String(clipboard.getData('text') || '').replace(/\D/g, '').slice(0, 6);
        if (!digits) return;
        event.preventDefault();
        fillPinInputs(digits);
        updateAuthButton();
        if (digits.length === 6 && !state.authBusy) attemptAuth(pinValue());
      });

      input.addEventListener('focus', function () {
        input.select();
      });
    });
  }

  function setupDropzone() {
    dom.dropzone.addEventListener('click', function () {
      dom.fileInput.click();
    });

    dom.fileInput.addEventListener('change', function () {
      handleFiles(dom.fileInput.files);
      dom.fileInput.value = '';
    });

    ['dragenter', 'dragover'].forEach(function (type) {
      dom.dropzone.addEventListener(type, function (event) {
        event.preventDefault();
        event.stopPropagation();
        dom.dropzone.classList.add('is-dragover');
      });
    });

    ['dragleave', 'dragend'].forEach(function (type) {
      dom.dropzone.addEventListener(type, function (event) {
        event.preventDefault();
        event.stopPropagation();
        dom.dropzone.classList.remove('is-dragover');
      });
    });

    dom.dropzone.addEventListener('drop', function (event) {
      event.preventDefault();
      event.stopPropagation();
      dom.dropzone.classList.remove('is-dragover');
      if (event.dataTransfer && event.dataTransfer.files) {
        handleFiles(event.dataTransfer.files);
      }
    });

    // Dropping outside the zone must not navigate away from the app.
    window.addEventListener('dragover', function (event) {
      if (!state.authenticated) return;
      event.preventDefault();
    });
    window.addEventListener('drop', function (event) {
      if (!state.authenticated) return;
      event.preventDefault();
    });
  }

  function setupNote() {
    dom.noteInput.addEventListener('input', updateNoteCount);
    dom.noteSend.addEventListener('click', sendNote);
    dom.noteInput.addEventListener('keydown', function (event) {
      var modifier = event.ctrlKey || event.metaKey;
      if (modifier && (event.key === 'Enter' || event.key === 'NumpadEnter')) {
        event.preventDefault();
        sendNote();
      }
    });
  }

  function setupAuthForm() {
    dom.authForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!isPinComplete()) {
        focusPinAt(0);
        return;
      }
      attemptAuth(pinValue());
    });
  }

  function setupDeviceDialog() {
    dom.deviceChip.addEventListener('click', openDeviceDialog);
    dom.deviceForm.addEventListener('submit', function (event) {
      event.preventDefault();
      applyDeviceName(dom.deviceInput.value);
      if (typeof dom.deviceDialog.close === 'function') dom.deviceDialog.close();
    });
    dom.deviceCancel.addEventListener('click', function () {
      if (typeof dom.deviceDialog.close === 'function') dom.deviceDialog.close();
    });
  }

  function setupConnectivity() {
    window.addEventListener('offline', function () {
      setConnection('offline');
      toast('You are offline.', 'warn');
    });
    window.addEventListener('online', function () {
      if (!state.authenticated) return;
      setConnection(state.connected ? 'live' : 'reconnecting');
      if (!state.eventSource) connectEvents();
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible' || !state.authenticated) return;
      if (!state.eventSource) connectEvents();
      refreshTimes();
    });
    window.setInterval(refreshTimes, 60000);
  }

  function cacheDom() {
    dom.deviceChip = document.getElementById('device-chip');
    dom.deviceChipLabel = document.getElementById('device-chip-label');
    dom.conn = document.getElementById('conn');
    dom.connLabel = document.getElementById('conn-label');
    dom.hostPanel = document.getElementById('host-panel');
    dom.hostPin = document.getElementById('host-pin');
    dom.hostLanUrl = document.getElementById('host-lan-url');
    dom.authScreen = document.getElementById('auth-screen');
    dom.authForm = document.getElementById('auth-form');
    dom.authError = document.getElementById('auth-error');
    dom.authSubmit = document.getElementById('auth-submit');
    dom.pinField = document.getElementById('pin-field');
    dom.appMain = document.getElementById('app-main');
    dom.dropzone = document.getElementById('dropzone');
    dom.fileInput = document.getElementById('file-input');
    dom.uploadQueue = document.getElementById('upload-queue');
    dom.uploadLimit = document.getElementById('upload-limit');
    dom.noteInput = document.getElementById('note-input');
    dom.noteCount = document.getElementById('note-count');
    dom.noteSend = document.getElementById('note-send');
    dom.inboxList = document.getElementById('inbox-list');
    dom.inboxCount = document.getElementById('inbox-count');
    dom.emptyState = document.getElementById('empty-state');
    dom.deviceDialog = document.getElementById('device-dialog');
    dom.deviceForm = document.getElementById('device-form');
    dom.deviceInput = document.getElementById('device-input');
    dom.deviceCancel = document.getElementById('device-cancel');
    dom.toasts = document.getElementById('toasts');
  }

  async function bootstrap() {
    cacheDom();
    state.deviceName = loadDeviceName();
    syncDeviceChip();
    setupPinField();
    setupAuthForm();
    setupDeviceDialog();
    setupDropzone();
    setupNote();
    setupConnectivity();
    updateNoteCount();
    setConnection('idle');

    maybeShowHostPanel();

    var fragmentPin = readPinFragment();
    if (fragmentPin) {
      // Pair silently from the QR link: no error-styled placeholder is shown
      // while the request is in flight, only the button's busy state.
      fillPinInputs(fragmentPin);
      updateAuthButton();
      await attemptAuth(fragmentPin);
      if (!state.authenticated) {
        dom.authScreen.hidden = false;
        focusPinAt(0);
      }
      return;
    }

    var restored = await bootstrapSession();
    if (!restored) {
      showAuthScreen();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      bootstrap();
    });
  } else {
    bootstrap();
  }
})();
