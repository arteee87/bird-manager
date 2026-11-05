/* app.js — Module 4 dashboard logic (vanilla JS, no framework). */
'use strict';

const API_BASE = window.location.origin;

/* ------------------------------------------------------------------ */
/* API client helpers                                                  */
/* ------------------------------------------------------------------ */

async function apiFetch(path, options) {
    const res = await fetch(API_BASE + path, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        // Translate by code when available, fall back to the server message.
        const codeKey = data.code ? ('err.' + data.code) : null;
        let detail = codeKey ? t(codeKey, data) : (data.error || data.message);
        // If the code is unknown or leaves unresolved placeholders, fall back.
        if (!detail || detail === codeKey || /\{[a-zA-Z_]+\}/.test(detail)) {
            detail = data.error || data.message;
        }
        const err = new Error(detail ? String(detail) : (t('m035') + res.status));
        err.status = res.status;
        err.data = data;   // structured body (e.g. the CIDR review on 409)
        throw err;
    }
    setOfflineBanner(false);   // the server answered, so the link is back
    return data;
}

/* ------------------------------------------------------------------ */
/* Offline banner: shown by safeFetch on any failure and cleared by    */
/* the next successful response (see apiFetch above), so a single       */
/* hiccup cannot leave it stuck.                                       */
/* ------------------------------------------------------------------ */

function setOfflineBanner(offline) {
    const banner = document.getElementById('offline-banner');
    if (!banner) return;
    banner.classList.toggle('hidden', !offline);
}

/* ------------------------------------------------------------------ */
/* Toast layer — every failed request has to reach the operator        */
/* ------------------------------------------------------------------ */

function showToast(kind, text, timeoutMs) {
    const stack = document.getElementById('toast-stack');
    if (!stack) return null;
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + (kind || 'info');
    toast.textContent = String(text == null ? '' : text);
    stack.appendChild(toast);
    const ttl = timeoutMs || (kind === 'error' ? 9000 : 4000);
    setTimeout(() => {
        toast.classList.add('toast-hide');
        setTimeout(() => { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 300);
    }, ttl);
    while (stack.children.length > 5) stack.removeChild(stack.firstElementChild);
    return toast;
}

/* Human-readable endpoint names so toasts do not leak raw URLs. */
const ERROR_LABELS = [
    ['/api/networks/bulk', t('m025')],
    ['/api/networks', t('m088')],
    ['/api/communities', t('m066')],
    ['/api/bgp_peers', t('m071')],
    ['/api/backup', t('m081')],
    ['/api/bird/', 'BIRD'],
    ['/api/config/apply', t('m030')],
    ['/api/config/rollback', t('m039')],
    ['/api/config/', t('m053')],
    ['/api/', t('m085')],
];

function endpointLabel(path) {
    const clean = String(path || '').split('?')[0];
    for (const [prefix, label] of ERROR_LABELS) {
        if (clean.startsWith(prefix)) return label;
    }
    return t('m085');
}

function reportError(context, error) {
    const status = error && error.status ? (' (HTTP ' + error.status + ')') : '';
    const message = (error && error.message) ? error.message : String(error);
    return showToast('error', context + ': ' + message + status);
}

/* Pre-built handler for safeFetch(path, options, onError). */
function toastError(context) {
    return (error) => reportError(context, error);
}

async function safeFetch(path, options, onError) {
    try {
        return await apiFetch(path, options);
    } catch (error) {
        setOfflineBanner(true);
        const handler = onError || toastError(endpointLabel(path));
        handler(error);
        return null;
    }
}

/* Nothing may fail silently: catch leftovers from loaders and timers. */
window.addEventListener('unhandledrejection', (event) => {
    const reason = event && event.reason ? event.reason : t('m037');
    reportError(t('m034'), reason);
});
window.addEventListener('error', (event) => {
    if (event && event.message) showToast('error', t('m042') + event.message);
});

/* Escape HTML to prevent XSS in dynamic row rendering. */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = String(text ?? '');
    return div.innerHTML;
}

/* Audit identifier configured through USERNAME in .env. */
function getUser() {
    const meta = document.querySelector('meta[name="bird-manager-user"]');
    const value = String((meta && meta.content) || '').trim().slice(0, 50);
    return value || 'User';
}

/* ------------------------------------------------------------------ */
/* community_ids parsing (stored as JSON string or comma-separated)   */
/* ------------------------------------------------------------------ */

function parseCommunityIds(raw) {
    if (raw === null || raw === undefined || raw === '') return [];
    const s = String(raw).trim();
    if (s.startsWith('[')) {
        try {
            const arr = JSON.parse(s);
            return Array.isArray(arr) ? arr.filter((x) => Number(x) > 0).map(Number) : [];
        } catch (e) {
            return [];
        }
    }
    return s.split(',').map((x) => parseInt(x, 10)).filter((n) => n > 0);
}

/* ------------------------------------------------------------------ */
/* Collapsible panel for networks & communities (R4.5.5, final fix)    */
/* ~4 lines: toggle hidden on .panel-body + lazy loading              */
/* ------------------------------------------------------------------ */

function toggleHierarchyPanel() {
    const body = document.getElementById('hierarchy-body');
    const btn = document.querySelector('#panel-networks .panel-toggle');
    const section = document.getElementById('panel-networks');
    const isExpanded = !body.classList.contains('hidden');
    if (isExpanded) {
        body.classList.add('hidden');          // collapse: hide the body
        btn.setAttribute('aria-expanded', 'false');
        if (section) section.classList.remove('expanded');
        body.innerHTML = '';                   // drop rows so lazy re-render works
    } else {
        body.classList.remove('hidden');       // expand: show the body
        btn.setAttribute('aria-expanded', 'true');
        if (section) section.classList.add('expanded');
        loadHierarchy();                       // lazy load on first expand
    }
}

/* ------------------------------------------------------------------ */
  /* Collapsible panels: "Add networks", "Add BGP Peer", "Log",            */
  /* "Config status". The same ▸/▾ button is used by the hierarchy          */
  /* and peer panels; those two keep their own lazy-load handlers because  */
  /* they drop their rows on collapse.                                    */
/* ------------------------------------------------------------------ */

function initCollapsiblePanels() {
    document.querySelectorAll('.panel-toggle').forEach((btn) => {
        /* Both lists have their own handler with lazy loading; binding the
           generic one here too would expand and immediately collapse the panel
           on every click — the fetch would still fire, into a hidden body. */
        if (btn.closest('#panel-networks') || btn.closest('#panel-peers')) return;
        if (btn.dataset.toggleBound === 'true') return;
        btn.dataset.toggleBound = 'true';
        btn.addEventListener('click', () => {
            const section = btn.closest('.panel');
            const body = section ? section.querySelector('.panel-body') : null;
            if (!section || !body) return;
            const isExpanded = !body.classList.contains('hidden');
            body.classList.toggle('hidden', isExpanded);
            section.classList.toggle('expanded', !isExpanded);
            btn.setAttribute('aria-expanded', isExpanded ? 'false' : 'true');
            if (!isExpanded) {
                // Re-render on expand if the language changed since last render.
                if (section.id === 'panel-bulk') renderBulkPanel();
                if (section.id === 'panel-bgp') renderBgpPeerPanel();
            }
            if (section.closest('#tab-panel-bird')) {
                window.requestAnimationFrame(resizeEditorPanes);
            }
            if (section.id === 'panel-bgp-sessions' || section.id === 'panel-service-protocols') {
                if (isExpanded) PeersPoll.stop(); else PeersPoll.start();
            }
        });
    });
}

/* ------------------------------------------------------------------ */
/* Hierarchy tree — grouped by AS:COMMUNITY value (variant A)         */
/* Lazy render: group headers + counters only until a header is       */
/* clicked; rows of one group render on demand.                       */
/* ------------------------------------------------------------------ */

const HIERARCHY = {
    groups: [],          // [{ key, community|null, networks:[] }]
    expandedKeys: new Set(),
    search: { query: '', matches: [], index: -1, expandedBefore: null }
};

async function loadHierarchy() {
    const netsData = await safeFetch('/api/networks', undefined);
    if (netsData === null) return;
    const commsData = await safeFetch('/api/communities', undefined);
    if (commsData === null) return;
    HIERARCHY.groups = buildGroups(netsData.networks, commsData.communities);
    renderHierarchy();
}

/* ------------------------------------------------------------------ */
/* Collapsible panel «BGP peers» — the database-side peer list.        */
/* Same contract as the network panel: collapsed on first paint, one   */
/* GET on the first expand, body dropped on collapse so a later expand */
/* re-renders from scratch. Purely DB data — the live birdc snapshot    */
/* lives in the "BIRD peers" block on the System tab.                  */
/* ------------------------------------------------------------------ */

let bgpPeersLoaded = false;
let bgpPeersCache = [];

async function loadBgpPeers() {
    const data = await safeFetch('/api/bgp_peers', undefined);
    if (data === null) return;
    bgpPeersLoaded = true;
    renderBgpPeerList(data.bgp_peers || []);
}

function togglePeersPanel() {
    const body = document.getElementById('bgp-peers-body');
    const btn = document.querySelector('#panel-peers .panel-toggle');
    const section = document.getElementById('panel-peers');
    if (!body || !btn || !section) return;
    const isExpanded = !body.classList.contains('hidden');
    if (isExpanded) {
        body.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
        section.classList.remove('expanded');
        body.innerHTML = '';          // drop rows so the next expand re-renders
    } else {
        body.classList.remove('hidden');
        btn.setAttribute('aria-expanded', 'true');
        section.classList.add('expanded');
        if (bgpPeersLoaded) renderBgpPeerList(bgpPeersCache); else loadBgpPeers();
    }
}

function renderBgpPeerList(peers) {
    bgpPeersCache = peers;
    const body = document.getElementById('bgp-peers-body');
    if (!body) return;
    body.innerHTML = '';
    if (!peers.length) {
        const empty = document.createElement('span');
        empty.className = 'placeholder';
        empty.textContent = t('m021');
        body.appendChild(empty);
        return;
    }
    const list = document.createElement('div');
    list.className = 'peer-list';
    /* Column headers stay put: a bare "64999" next to "65500" is unreadable
       once there is more than one peer. */
    list.appendChild(buildPeerHead());
    peers.forEach((peer) => list.appendChild(buildPeerRow(peer)));
    body.appendChild(list);
}

/* Header and rows share the same column classes, so they line up without a
   table element. Distinct from PEER_COLUMNS below, which labels the live
   birdc snapshot on the System tab. */
const DB_PEER_COLUMNS = [
    // Generator for the RouterOS side of this peer. Empty header like .peer-act:
    // the button carries the meaning, and the track is pinned in CSS so the
    // header and the rows stay aligned.
    { cls: 'peer-col-cfg', title: '' },
    { cls: 'peer-col-status', title: t('m086') },
    { cls: 'peer-col-as', title: 'Local AS' },
    { cls: 'peer-col-nb', title: 'Neighbor' },
    { cls: 'peer-col-nbas', title: 'Neighbor AS' },
];

function buildPeerHead() {
    const head = document.createElement('div');
    head.className = 'peer-head';
    const name = document.createElement('span');
    name.className = 'peer-name';
    name.textContent = 'Peer id';
    head.appendChild(name);
    DB_PEER_COLUMNS.forEach((col) => {
        const cell = document.createElement('span');
        cell.className = col.cls;
        cell.textContent = col.title;
        head.appendChild(cell);
    });
    const act = document.createElement('span');
    act.className = 'peer-act';
    head.appendChild(act);
    return head;
}

/* Small tag under the peer id: what the export filter will actually carry. */
function scopeTag(text, title) {
    const tag = document.createElement('span');
    tag.className = 'peer-scope-tag';
    tag.textContent = text;
    tag.title = title || '';
    return tag;
}

/* RouterOS configuration for one peer. Rebuilt from the peer record on every
   click and never stored. The placeholders are filled from the live peer, so a
   config always matches what is currently in the database. */
function rosConfigFor(peer, routerId) {
    const instance = 'birdmanager_' + peer.name;
    return [
        '/routing bgp instance',
        'add as=' + peer.neighbor_as + ' disabled=no name=' + instance,
        '/routing bgp template',
        'add as=' + peer.neighbor_as + ' disabled=no name=' + instance
            + ' output.network=bgp-networks .no-client-to-client-reflection=yes'
            + ' routing-table=main',
        '/routing bgp connection',
        'add as=' + peer.neighbor_as + ' connect=yes disabled=no hold-time=1h'
            + ' instance=' + instance + ' keepalive-time=30s listen=yes'
            + ' local.address=' + peer.neighbor + ' .role=ebgp multihop=yes'
            + ' name=' + instance
            + ' output.filter-chain=discard .network=bgp-networks'
            + ' .no-client-to-client-reflection=yes'
            + ' remote.address=' + routerId + '/32'
            + ' .as=' + peer.local_as + ' .port=179'
            + ' routing-table=main templates=' + instance,
        '',
    ].join('\n');
}

function openRosConfig(peer) {
    const dialog = document.getElementById('ros-config');
    const title = document.getElementById('ros-title');
    const warning = document.getElementById('ros-warning');
    const body = document.getElementById('ros-body');
    if (!dialog || !title || !warning || !body) return;
    title.textContent = t('m059') + peer.name;
    warning.textContent = t('m005')
        + t('m048');
    body.textContent = rosConfigFor(peer, peer.router_id || '');
    if (typeof dialog.showModal === 'function') {
        dialog.showModal();
    } else {
        dialog.setAttribute('open', 'open');
    }
}

function wireRosDialog() {
    const dialog = document.getElementById('ros-config');
    const body = document.getElementById('ros-body');
    const copyBtn = document.getElementById('ros-copy');
    const closeBtn = document.getElementById('ros-close');
    if (!dialog || !body || !copyBtn || !closeBtn) return;
    if (closeBtn.dataset.wired === 'true') return;
    closeBtn.dataset.wired = 'true';
    copyBtn.dataset.wired = 'true';
    closeBtn.addEventListener('click', () => dialog.close());
    copyBtn.addEventListener('click', async () => {
        const text = body.textContent;
        // navigator.clipboard needs a secure context, which a plain-http LAN
        // address is not; fall back so copying always has a path.
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(text);
                showToast('success', t('m019'));
                return;
            }
        } catch (error) {
            /* fall through to the legacy path */
        }
        try {
            const scratch = document.createElement('textarea');
            scratch.value = text;
            scratch.setAttribute('readonly', 'readonly');
            scratch.style.position = 'fixed';
            scratch.style.opacity = '0';
            document.body.appendChild(scratch);
            scratch.select();
            const copied = document.execCommand('copy');
            document.body.removeChild(scratch);
            if (copied) {
                showToast('success', t('m019'));
                return;
            }
        } catch (error) {
            /* fall through to manual selection */
        }
        // Last resort: select the text so Ctrl+C works by hand.
        const range = document.createRange();
        range.selectNodeContents(body);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        body.focus();
        showToast('warning', t('m093'));
    });
}

function buildPeerRow(peer) {
    const row = document.createElement('div');
    row.className = 'peer-row';
    row.dataset.id = peer.id;

    /* Name and the export scope share the flexible column. The fixed columns
       after it each carry their own values; .peer-col-cfg is the exception —
       it holds the RouterOS generator button and is pinned in CSS like
       .peer-act, because an unpinned track measures 0 in the header and by
       content in a row, which shifts everything after it. */
    const name = document.createElement('span');
    name.className = 'peer-name';
    name.appendChild(document.createTextNode(peer.name));
    if (peer.export_all) {
        name.appendChild(scopeTag(t('m076'), t('m017')));
    } else if (peer.asn_found === false) {
        name.appendChild(scopeTag(
            t('m046'),
            t('m011') + peer.local_as
                + t('m014')
        ));
    } else {
        name.appendChild(scopeTag(peer.export_count + t('m063'),
            t('m061') + peer.local_as));
    }
    row.appendChild(name);

    const cfg = document.createElement('span');
    cfg.className = 'peer-col-cfg';
    const rosBtn = document.createElement('button');
    rosBtn.type = 'button';
    rosBtn.className = 'btn btn-ros';
    rosBtn.textContent = 'ROS';
    rosBtn.title = t('m016') + peer.name;
    rosBtn.addEventListener('click', () => openRosConfig(peer));
    cfg.appendChild(rosBtn);
    row.appendChild(cfg);

    const status = document.createElement('span');
    status.className = 'peer-col-status';
    status.insertAdjacentHTML('beforeend', statusBadge(peer.status));
    row.appendChild(status);

    [['peer-col-as', peer.local_as], ['peer-col-nb', peer.neighbor],
        ['peer-col-nbas', peer.neighbor_as]].forEach(([cls, value]) => {
        const cell = document.createElement('span');
        cell.className = cls + ' peer-cell';
        cell.textContent = value === null || value === undefined || value === '' ? '—' : String(value);
        row.appendChild(cell);
    });

    const act = document.createElement('span');
    act.className = 'peer-act';
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'btn btn-danger';
    delBtn.textContent = t('m078');
    delBtn.addEventListener('click', async () => {
        if (!confirm(t('m056') + peer.name + ' (' + peer.neighbor + ')?')) return;
        delBtn.disabled = true;
        await safeFetch('/api/bgp_peers/' + peer.id, { method: 'DELETE' });
        await refreshDashboard();      // drop it from the list and refresh the diff
    });
    act.appendChild(delBtn);
    row.appendChild(act);
    return row;
}

/* Variant A: a network appears in all of its groups (no dedup). */
function buildGroups(networks, communities) {
    const groups = [];
    const commById = {};
    communities.forEach((comm) => {
        commById[comm.id] = comm;
        groups.push({ key: 'comm-' + comm.id, community: comm, networks: [] });
    });
    const defaultGroup = groups.find((group) => group.community.name.trim() === '65534');
    const fallbackGroup = defaultGroup || { key: 'no-community', community: null, networks: [] };

    networks.forEach((net) => {
        net.cids = parseCommunityIds(net.community_ids);
        if (net.cids.length === 0) {
            fallbackGroup.networks.push(net);
            return;
        }
        net.cids.forEach((cid) => {
            const g = commById[cid] !== undefined ? groups.find((x) => x.key === 'comm-' + cid) : null;
            if (g) g.networks.push(net);
        });
        if (!net.cids.some((cid) => commById[cid] !== undefined)) {
            fallbackGroup.networks.push(net);
        }
    });

    if (!defaultGroup) groups.push(fallbackGroup);
    return groups;
}

function renderHierarchy(expandKeys) {
    const body = document.getElementById('hierarchy-body');
    body.innerHTML = '';
    const toExpand = expandKeys || HIERARCHY.expandedKeys;
    HIERARCHY.groups.forEach((group) => {
        const el = buildGroupElement(group);
        if (toExpand && toExpand.has(group.key)) {
            el.classList.remove('collapsed');
            el.classList.add('expanded');
            renderGroupBody(el, group);
        }
        body.appendChild(el);
    });
    /* [+ New community] button at the bottom of the panel. */
    const newCommBtn = document.createElement('button');
    newCommBtn.type = 'button';
    newCommBtn.className = 'btn add-row-btn';
    newCommBtn.textContent = t('m038');
    newCommBtn.addEventListener('click', () => showNewCommunityRow(body));
    body.appendChild(newCommBtn);
}

/* ------------------------------------------------------------------ */
/* Search in "Сети и сообщества"                                      */
/* ------------------------------------------------------------------ */
function hierarchyRowText(row) {
    const group = row.closest('.hierarchy-group');
    const groupName = group ? (group.querySelector('.group-name')?.textContent || '') : '';
    return (groupName + ' ' + (row.textContent || '')).toLowerCase();
}

function clearHierarchySearch() {
    HIERARCHY.search = { query: '', matches: [], index: -1, expandedBefore: null };
    document.querySelectorAll('#hierarchy-body .network-row.search-hit, #hierarchy-body .network-row.search-current')
        .forEach((el) => el.classList.remove('search-hit', 'search-current'));
    const cnt = document.getElementById('hierarchy-search-count');
    if (cnt) cnt.textContent = '';
    const prev = document.getElementById('hierarchy-search-prev');
    const next = document.getElementById('hierarchy-search-next');
    if (prev) prev.disabled = true;
    if (next) next.disabled = true;
}

function runHierarchySearch(query, allMode) {
    const q = (query || '').trim().toLowerCase();
    clearHierarchySearch();
    if (!q) return;
    HIERARCHY.search.query = q;

    // Restore expansion if a previous all-search expanded groups.
    if (HIERARCHY.search.expandedBefore) {
        HIERARCHY.expandedKeys = HIERARCHY.search.expandedBefore;
        HIERARCHY.search.expandedBefore = null;
        renderHierarchy();
    }

    let expandedSet = null;
    if (allMode) {
        // Determine groups with matches from data model.
        const prevExpanded = new Set(HIERARCHY.expandedKeys);
        HIERARCHY.search.expandedBefore = prevExpanded;
        const toExpand = new Set(prevExpanded);
        HIERARCHY.groups.forEach((group) => {
            const groupText = (group.community ? group.community.name + ' ' + (group.community.description || '') : 'default 65534');
            const hasMatch = group.networks.some((net) =>
                (String(net.prefix || '') + ' ' + (net.description || '') + ' ' + groupText).toLowerCase().includes(q)
            );
            if (hasMatch) toExpand.add(group.key);
        });
        renderHierarchy(toExpand);
    }

    // Collect matching rows from DOM.
    const rows = Array.from(document.querySelectorAll('#hierarchy-body .network-row'));
    const matches = rows.filter((row) => hierarchyRowText(row).includes(q));
    HIERARCHY.search.matches = matches;
    matches.forEach((row) => row.classList.add('search-hit'));

    const cnt = document.getElementById('hierarchy-search-count');
    if (cnt) cnt.textContent = matches.length ? (1 + '/' + matches.length) : t('m169');
    const prev = document.getElementById('hierarchy-search-prev');
    const next = document.getElementById('hierarchy-search-next');
    if (prev) prev.disabled = matches.length <= 1;
    if (next) next.disabled = matches.length <= 1;
    if (matches.length) {
        HIERARCHY.search.index = 0;
        matches[0].classList.add('search-current');
        matches[0].scrollIntoView({ block: 'center' });
        if (cnt) cnt.textContent = '1/' + matches.length;
        if (prev) prev.disabled = true; // first
    }
}

function hierarchySearchNavigate(delta) {
    const st = HIERARCHY.search;
    if (!st.matches.length) return;
    st.matches[st.index]?.classList.remove('search-current');
    st.index = Math.max(0, Math.min(st.matches.length - 1, st.index + delta));
    const el = st.matches[st.index];
    if (el) {
        el.classList.add('search-current');
        el.scrollIntoView({ block: 'center' });
    }
    const cnt = document.getElementById('hierarchy-search-count');
    if (cnt) cnt.textContent = (st.index + 1) + '/' + st.matches.length;
    const prev = document.getElementById('hierarchy-search-prev');
    const next = document.getElementById('hierarchy-search-next');
    if (prev) prev.disabled = st.index === 0;
    if (next) next.disabled = st.index === st.matches.length - 1;
}

function wireHierarchySearch() {
    const input = document.getElementById('hierarchy-search');
    const prev = document.getElementById('hierarchy-search-prev');
    const next = document.getElementById('hierarchy-search-next');
    if (!input) return;
    let timer = null;
    input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => runHierarchySearch(input.value, false), 150);
    });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            runHierarchySearch(input.value, true);
        } else if (e.key === 'Escape') {
            input.value = '';
            clearHierarchySearch();
        }
    });
    if (prev) prev.addEventListener('click', () => hierarchySearchNavigate(-1));
    if (next) next.addEventListener('click', () => hierarchySearchNavigate(1));
}

function buildGroupElement(group) {
    const el = document.createElement('div');
    el.className = 'hierarchy-group collapsed';
    el.dataset.key = group.key;

    const headerRow = document.createElement('div');
    headerRow.className = 'group-header-row';

    const arrow = document.createElement('span');
    arrow.className = 'group-arrow';
    const name = document.createElement('span');
    name.className = 'group-name';
    name.textContent = group.community ? group.community.name : 'default (65534)';
    if (group.community && group.community.enabled === false) {
        name.classList.add('disabled-label');
        name.textContent += ' [disabled]';
    }
    const count = document.createElement('span');
    count.className = 'group-count';
    count.id = 'gcount-' + String(group.key);
    count.textContent = t('m064') + group.networks.length + ')';
    headerRow.appendChild(arrow);
    headerRow.appendChild(name);
    headerRow.appendChild(count);

    /* Description of the AS:COMMUNITY (always visible when present). */
    if (group.community && group.community.description) {
        const desc = document.createElement('span');
        desc.className = 'group-desc';
        desc.textContent = group.community.description;
        headerRow.appendChild(desc);
    }

    if (group.community) {
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'btn';
        editBtn.textContent = '✎';
        editBtn.title = t('m029');
        editBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            showCommunityEdit(el, group);
        });
        headerRow.appendChild(editBtn);

        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'btn btn-danger';
        delBtn.textContent = t('m078');
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            cascadeDeleteCommunity(el, group);
        });
        headerRow.appendChild(delBtn);
    }

    if (!group.community) {
        /* Default (65534) group — clear networks, the ASN itself cannot be deleted. */
        const clearBtn = document.createElement('button');
        clearBtn.type = 'button';
        clearBtn.className = 'btn btn-danger';
        clearBtn.textContent = t('m074');
        clearBtn.title = t('m074');
        clearBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            clearDefaultCommunity(el, group);
        });
        headerRow.appendChild(clearBtn);
    }

    el.appendChild(headerRow);

    const groupBody = document.createElement('div');
    groupBody.className = 'group-body';
    el.appendChild(groupBody);

    /* Click on the group name → toggle expand/collapse; first expand renders rows. */
    headerRow.addEventListener('click', () => {
        if (el.classList.contains('collapsed')) {
            el.classList.remove('collapsed');
            el.classList.add('expanded');
            HIERARCHY.expandedKeys.add(group.key);
            renderGroupBody(el, group);
        } else {
            el.classList.add('collapsed');
            el.classList.remove('expanded');
            HIERARCHY.expandedKeys.delete(group.key);
            groupBody.innerHTML = ''; // lazy: drop rows until next expand
        }
    });

    return el;
}

function renderGroupBody(el, group) {
    const groupBody = el.querySelector('.group-body');
    groupBody.innerHTML = '';
    if (!group.networks.length) {
        const empty = document.createElement('div');
        empty.className = 'placeholder';
        empty.textContent = t('m069');
        groupBody.appendChild(empty);
    } else {
        group.networks.forEach((net) => {
            groupBody.appendChild(buildNetworkRow(net, group));
        });
    }
    /* [+ network] in the group -> empty row with inputs (community = current AS:COMMUNITY). */
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn add-row-btn';
    addBtn.textContent = t('m080');
    addBtn.addEventListener('click', () => {
        showAddNetworkRow(groupBody, group);
    });
    groupBody.appendChild(addBtn);
}

function buildNetworkRow(net, group) {
    const row = document.createElement('div');
    row.className = 'network-row';
    row.dataset.id = net.id;
    row.innerHTML =
        '<span class="net-prefix">' + escapeHtml(net.prefix) + '</span>' +
        '<span class="placeholder">' + escapeHtml(net.description || '') + '</span>' +
        '<span class="net-status">' + statusBadge(net.status) + '</span>' +
        (net.enabled === false ? '<span class="badge badge-other">disabled</span>' : '');
    row.addEventListener('click', () => {
        if (row.classList.contains('editing')) return;
        showNetworkEdit(row, net);
    });
    return row;
}

/* Status badges (R4.1.2): pending grey, applied green, other orange. */
const STATUS_BADGES = { pending: 'badge-pending', applied: 'badge-applied' };

function statusBadge(status) {
    const cls = STATUS_BADGES[status] || 'badge-other';
    return '<span class="badge ' + cls + '">' + escapeHtml(status || '') + '</span>';
}

/* ------------------------------------------------------------------ */
/* Inline edit — network row / community header / new rows           */
/* ------------------------------------------------------------------ */

/* Only one network editor open at a time: clicking another row restores the
   previous one from its collapsed snapshot. */
let activeNetworkEdit = null; // { row, html } — collapsed state of the current row

function closeActiveNetworkEdit() {
    if (!activeNetworkEdit) return;
    const { row, html } = activeNetworkEdit;
    row.innerHTML = html;               // restore the collapsed state
    row.classList.remove('editing');
    activeNetworkEdit = null;
}

function showNetworkEdit(row, net) {
    /* Click on the already-open row — no-op. */
    if (activeNetworkEdit && activeNetworkEdit.row === row) return;

    /* Close all previously clicked networks. */
    closeActiveNetworkEdit();

    const snapshot = row.innerHTML;     // collapsed state to restore on cancel
    row.classList.add('editing');

    const edit = document.createElement('div');
    edit.className = 'inline-edit';

    const prefixInput = document.createElement('input');
    prefixInput.value = net.prefix || '';
    prefixInput.placeholder = t('m047');

    const descInput = document.createElement('input');
    descInput.value = net.description || '';
    descInput.placeholder = t('m073');

    const statusSelect = document.createElement('select');
    ['pending', 'applied'].forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s;
        opt.textContent = s;
        if (net.status === s) opt.selected = true;
    });
    if (!['pending', 'applied'].includes(net.status)) {
        const opt = document.createElement('option');
        opt.value = net.status;
        opt.textContent = net.status;
        opt.selected = true;
        statusSelect.appendChild(opt);
    }
    const enabledLabel = document.createElement('label');
    const enabledInput = document.createElement('input');
    enabledInput.type = 'checkbox';
    enabledInput.checked = net.enabled !== false;
    enabledLabel.appendChild(enabledInput);
    enabledLabel.appendChild(document.createTextNode(' enabled'));
    edit.appendChild(prefixInput);
    edit.appendChild(descInput);
    edit.appendChild(statusSelect);
    edit.appendChild(enabledLabel);
    const rowMsg = document.createElement('span');
    rowMsg.className = 'form-msg';

    /* Community checkboxes removed: community_ids is not changed in this menu —
       the field is not sent in PUT and stays unchanged on the backend. */

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn btn-primary';
    saveBtn.textContent = t('m070');
    saveBtn.addEventListener('click', async () => {
        if (!prefixInput.value.trim()) return;
        const saved = await saveNetworkReviewed('/api/networks/' + net.id, 'PUT', {
            prefix: prefixInput.value.trim(),
            description: descInput.value.trim(),
            status: statusSelect.value,
            enabled: enabledInput.checked
        }, rowMsg);
        if (!saved) return;            // keep the menu open for editing
        closeActiveNetworkEdit();       // collapse the menu after saving
        await refreshDashboard();       // re-render hierarchy and diff after change
    });

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'btn btn-danger';
    delBtn.textContent = t('m078');
    delBtn.addEventListener('click', async () => {
        if (!confirm(t('m051') + net.prefix + '?')) return;
        await safeFetch('/api/networks/' + net.id, { method: 'DELETE' });
        closeActiveNetworkEdit();       // close the menu
        await refreshDashboard();       // immediately remove from list and refresh diff
    });

    edit.appendChild(saveBtn);
    edit.appendChild(delBtn);
    edit.appendChild(rowMsg);
    /* Replace the row contents only once the editor is fully built. */
    row.innerHTML = '';
    row.appendChild(edit);
    activeNetworkEdit = { row, html: snapshot };
}

function showCommunityEdit(groupEl, group) {
    const body = groupEl.querySelector('.group-body');
    if (groupEl.classList.contains('collapsed')) return;
    /* Replace rows with the community editor while it is open. */
    if (body.dataset.editing === 'true') return;
    body.dataset.editing = 'true';
    body.innerHTML = '';

    const edit = document.createElement('div');
    edit.className = 'inline-edit';

    const nameInput = document.createElement('input');
    nameInput.value = group.community.name || '';
    nameInput.placeholder = t('m050');

    const descInput = document.createElement('input');
    descInput.value = group.community.description || '';
    descInput.placeholder = t('m073');
    const enabledLabel = document.createElement('label');
    const enabledInput = document.createElement('input');
    enabledInput.type = 'checkbox';
    enabledInput.checked = group.community.enabled !== false;
    enabledLabel.appendChild(enabledInput);
    enabledLabel.appendChild(document.createTextNode(' enabled'));

    edit.appendChild(nameInput);
    edit.appendChild(descInput);
    edit.appendChild(enabledLabel);

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn btn-primary';
    saveBtn.textContent = t('m070');
    saveBtn.addEventListener('click', async () => {
        if (!nameInput.value.trim()) return;
        await safeFetch('/api/communities/' + group.community.id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: nameInput.value.trim(),
                description: descInput.value.trim(),
                enabled: enabledInput.checked
            })
        });
        await refreshDashboard(); // re-render hierarchy and diff after change
    });

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'btn btn-danger';
    delBtn.textContent = t('m078');
    delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        cascadeDeleteCommunity(groupEl, group);
    });

    edit.appendChild(saveBtn);
    edit.appendChild(delBtn);
    body.appendChild(edit);
}

/* Cascade delete — always-on confirm, even for N=0 related networks. */
async function cascadeDeleteCommunity(groupEl, group) {
    const related = group.networks;
    const msg = t('m055') + group.community.name + t('m027') + related.length + t('m079');
    if (!confirm(msg)) return; // always-on confirm (N>0 and N=0)
    await safeFetch('/api/communities/' + group.community.id, { method: 'DELETE' });
    await refreshDashboard(); // confirmed deletion → update (immediate removal from the list)
}

/* Clear default (65534) group — delete all its networks, keep the ASN itself. */
async function clearDefaultCommunity(groupEl, group) {
    const related = group.networks;
    const msg = t('m010') + related.length + t('m033');
    if (!confirm(msg)) return; // always-on confirm (N>0 and N=0)
    for (const net of related) {
        await safeFetch('/api/networks/' + net.id, { method: 'DELETE' });
    }
    await refreshDashboard(); // confirmed clearing → update (immediate removal from the list)
}

function showAddNetworkRow(groupBody, group) {
    const edit = document.createElement('div');
    edit.className = 'inline-edit';

    const prefixInput = document.createElement('input');
    prefixInput.placeholder = t('m047');
    const descInput = document.createElement('input');
    descInput.placeholder = t('m073');
    const statusSelect = document.createElement('select');
    ['pending', 'applied'].forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s;
        opt.textContent = s;
        statusSelect.appendChild(opt);
    });

    const info = document.createElement('span');
    info.className = 'placeholder';
    info.textContent = group.community ? (t('m054') + group.community.name) : 'default (65534)';
    const rowMsg = document.createElement('span');
    rowMsg.className = 'form-msg';
    const enabledLabel = document.createElement('label');
    const enabledInput = document.createElement('input');
    enabledInput.type = 'checkbox';
    enabledInput.checked = true;
    enabledLabel.appendChild(enabledInput);
    enabledLabel.appendChild(document.createTextNode(' enabled'));

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn btn-primary';
    saveBtn.textContent = t('m072');
    saveBtn.addEventListener('click', async () => {
        if (!prefixInput.value.trim()) return;
        const cids = group.community ? [group.community.id] : [];
        const saved = await saveNetworkReviewed('/api/networks', 'POST', {
            prefix: prefixInput.value.trim(),
            description: descInput.value.trim(),
            status: statusSelect.value,
            enabled: enabledInput.checked,
            community_ids: JSON.stringify(cids)
        }, rowMsg);
        if (saved) {
            await refreshDashboard(); // re-render hierarchy and diff after adding
        }
    });

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn';
    cancelBtn.textContent = t('m083');
    cancelBtn.addEventListener('click', () => edit.remove());

    edit.appendChild(prefixInput);
    edit.appendChild(descInput);
    edit.appendChild(statusSelect);
    edit.appendChild(enabledLabel);
    edit.appendChild(info);
    edit.appendChild(rowMsg);
    edit.appendChild(saveBtn);
    edit.appendChild(cancelBtn);
    groupBody.appendChild(edit);
}

function showNewCommunityRow(panelBody) {
    /* Only one new-community form at a time. */
    if (panelBody.querySelector('.new-community-row')) return;

    const edit = document.createElement('div');
    edit.className = 'inline-edit new-community-row';

    const nameInput = document.createElement('input');
    nameInput.placeholder = t('m013');
    const descInput = document.createElement('input');
    descInput.placeholder = t('m073');
    const enabledLabel = document.createElement('label');
    const enabledInput = document.createElement('input');
    enabledInput.type = 'checkbox';
    enabledInput.checked = true;
    enabledLabel.appendChild(enabledInput);
    enabledLabel.appendChild(document.createTextNode(' enabled'));

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn btn-primary';
    saveBtn.textContent = t('m072');
    saveBtn.addEventListener('click', async () => {
        if (!nameInput.value.trim()) return;
        await safeFetch('/api/communities', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: nameInput.value.trim(),
                description: descInput.value.trim(),
                enabled: enabledInput.checked
            })
        });
        edit.remove(); // after "Add" the form closes
        await refreshDashboard(); // re-render hierarchy and diff after adding
    });

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn';
    cancelBtn.textContent = t('m083');
    cancelBtn.addEventListener('click', () => edit.remove());

    edit.appendChild(nameInput);
    edit.appendChild(descInput);
    edit.appendChild(enabledLabel);
    edit.appendChild(saveBtn);
    edit.appendChild(cancelBtn);
    panelBody.appendChild(edit);
}

async function refreshHierarchy() {
    /* Full re-render: drop the reference to the previously edited row. */
    activeNetworkEdit = null;
    const netsData = await safeFetch('/api/networks', undefined);
    if (netsData === null) return;
    const commsData = await fetchCommunities();
    if (commsData === null) return;
    COMMUNITY_CACHE = commsData.communities;
    HIERARCHY.groups = buildGroups(netsData.networks, commsData.communities);
    /* Keep expansion state while refreshing: counters + data of expanded groups. */
    const body = document.getElementById('hierarchy-body');
    body.innerHTML = '';
    HIERARCHY.groups.forEach((group) => {
        const el = buildGroupElement(group);
        if (HIERARCHY.expandedKeys.has(group.key)) {
            el.classList.remove('collapsed');
            el.classList.add('expanded');
            renderGroupBody(el, group);
        }
        body.appendChild(el);
    });
    /* Re-add [+ New community] button after re-render. */
    const newCommBtn = document.createElement('button');
    newCommBtn.type = 'button';
    newCommBtn.className = 'btn add-row-btn';
    newCommBtn.textContent = t('m038');
    newCommBtn.addEventListener('click', () => showNewCommunityRow(body));
    body.appendChild(newCommBtn);
}

/* ------------------------------------------------------------------ */
/* "Add" single row form (R4.2.3, final)                               */
/* Column 1 — AS:COMMUNITY select + manual input; empty = AS «65534» */
/* Column 2 — networks line, any format, height 25vh, scroll-y       */
/* ------------------------------------------------------------------ */

let COMMUNITY_CACHE = [];
let COMMUNITIES_PROMISE = null;
function fetchCommunities() {
    if (COMMUNITY_CACHE && COMMUNITY_CACHE.length) {
        return Promise.resolve({ communities: COMMUNITY_CACHE });
    }
    if (!COMMUNITIES_PROMISE) {
        COMMUNITIES_PROMISE = safeFetch('/api/communities', undefined).then((data) => {
            if (data && Array.isArray(data.communities)) {
                COMMUNITY_CACHE = data.communities;
            }
            COMMUNITIES_PROMISE = null;
            return data;
        });
    }
    return COMMUNITIES_PROMISE;
}

function renderBulkPanel() {
    const body = document.getElementById('bulk-body');
    const lang = window.I18n ? window.I18n.getLang() : 'ru';
    if (body.dataset.rendered === 'true' && body.dataset.lang === lang) return;
    body.innerHTML = '';
    body.dataset.rendered = 'true';
    body.dataset.lang = lang;

    /* Single row: AS:COMMUNITY + networks. */
    const formRow = document.createElement('div');
    formRow.className = 'bulk-form-row';

    const ascCol = document.createElement('div');
    ascCol.className = 'bulk-col-asc';
    const ascLabel = document.createElement('label');
    ascLabel.textContent = 'AS:COMMUNITY';
    const asSelect = document.createElement('select');
    asSelect.id = 'bulk-ascommunity';
    const emptyOpt = document.createElement('option');
    emptyOpt.value = '__default__';
    emptyOpt.textContent = 'default (65534)';
    asSelect.appendChild(emptyOpt);
    const customOpt = document.createElement('option');
    customOpt.value = '__custom__';
    customOpt.textContent = t('m044');
    asSelect.appendChild(customOpt);
    ascCol.appendChild(ascLabel);
    ascCol.appendChild(asSelect);

    const customInput = document.createElement('input');
    customInput.id = 'bulk-ascommunity-custom';
    customInput.className = 'hidden';
    customInput.placeholder = t('m018');
    ascCol.appendChild(customInput);

    const netsCol = document.createElement('div');
    netsCol.className = 'bulk-col-nets';
    const netsLabel = document.createElement('label');
    netsLabel.textContent = t('m020');
    const input = document.createElement('textarea');
    input.id = 'bulk-input';
    input.className = 'bulk-input';
    input.placeholder = '10.254.229.0/28, 192.168.0.0/16 …';
    netsCol.appendChild(netsLabel);
    netsCol.appendChild(input);

    formRow.appendChild(ascCol);
    formRow.appendChild(netsCol);

    const btnRow = document.createElement('div');
    btnRow.className = 'form-row';
    const commitBtn = document.createElement('button');
    commitBtn.type = 'button';
    commitBtn.className = 'btn btn-primary';
    commitBtn.textContent = t('m043');
    btnRow.appendChild(commitBtn);

    const msg = document.createElement('div');
    msg.id = 'bulk-msg';
    msg.className = 'form-msg';

    asSelect.addEventListener('change', () => {
        customInput.classList.toggle('hidden', asSelect.value !== '__custom__');
    });

    commitBtn.addEventListener('click', () => runBulkAdd(asSelect, customInput, input, msg));

    body.appendChild(formRow);
    body.appendChild(btnRow);
    body.appendChild(msg);

    /* Seed existing communities into the select. */
    fetchCommunities().then((data) => {
        if (data === null) return;
        COMMUNITY_CACHE = data.communities;
        /* Sorting: "default (65534)" and "enter manually" are always the first
       two options (added above before seeding); the rest — natural ascending. */
        data.communities.slice()
            .sort((a, b) => a.name.localeCompare(b.name, 'en', { numeric: true }))
            .forEach((comm) => {
                if (comm.name.trim() === '65534') return;
                const opt = document.createElement('option');
                opt.value = comm.name;
                opt.textContent = comm.name;
                asSelect.appendChild(opt);
            });
    });
}

/* Prefixes the server could not take as they are. One decision covers the
   whole batch instead of failing it. Resolves 'fix' to apply the suggested
   repairs, 'drop' to discard the entries; Esc and the backdrop both mean
   'drop', matching "only the correct ones get added". */
function reviewCidrs(entries) {
    return new Promise((resolve) => {
        const dialog = document.getElementById('cidr-review');
        const tbody = document.getElementById('cidr-review-rows');
        const note = document.getElementById('cidr-review-note');
        const fixBtn = document.getElementById('cidr-review-fix');
        const dropBtn = document.getElementById('cidr-review-drop');
        if (!dialog || !tbody || !fixBtn || !dropBtn || !entries.length) {
            resolve('drop');
            return;
        }

        tbody.replaceChildren();
        let unfixable = 0;
        entries.forEach((entry) => {
            const row = document.createElement('tr');
            const found = document.createElement('td');
            found.textContent = entry.input;
            const repair = document.createElement('td');
            if (entry.suggestion) {
                repair.textContent = entry.suggestion;
            } else {
                repair.textContent = t('m045');
                repair.className = 'cidr-empty';
                unfixable += 1;
            }
            row.appendChild(found);
            row.appendChild(repair);
            tbody.appendChild(row);
        });

        if (note) {
            note.textContent = unfixable > 0
                ? unfixable + t('m087') + entries.length + t('m092')
                : t('m041') + entries.length + t('m026');
        }

        let settled = false;
        const finish = (choice) => {
            if (settled) return;
            settled = true;
            fixBtn.removeEventListener('click', onFix);
            dropBtn.removeEventListener('click', onDrop);
            dialog.removeEventListener('cancel', onCancel);
            if (dialog.open) dialog.close();
            resolve(choice);
        };
        const onFix = () => finish('fix');
        const onDrop = () => finish('drop');
        const onCancel = () => finish('drop');

        fixBtn.addEventListener('click', onFix);
        dropBtn.addEventListener('click', onDrop);
        dialog.addEventListener('cancel', onCancel);
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else resolve('drop');
    });
}

/* Single-record save. The server answers 409 with the same review payload as
   the bulk parser when the prefix is not canonical; we ask once and re-send
   the repair only when the operator confirms. Returns true when the record
   was actually written. */
async function saveNetworkReviewed(url, method, payload, msgEl) {
    const send = (body) => apiFetch(url, {
        method: method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    const say = (text, kind) => {
        if (!msgEl) return;
        msgEl.textContent = text;
        msgEl.className = 'form-msg' + (kind ? ' ' + kind : '');
    };

    try {
        await send(payload);
        return true;
    } catch (err) {
        if (err.status !== 409) {
            toastError(endpointLabel(url))(err);
            return false;
        }
        const entries = (err.data && err.data.invalid) || [];
        const choice = await reviewCidrs(entries);
        if (choice !== 'fix') {
            say(t('m004'), 'error');
            return false;
        }
        const repair = entries.find((entry) => entry.suggestion);
        if (!repair) {
            say(t('m007'), 'error');
            return false;
        }
        try {
            await send({ ...payload, prefix: repair.suggestion });
        } catch (retryErr) {
            toastError(endpointLabel(url))(retryErr);
            return false;
        }
        say(t('m036') + repair.suggestion, 'success');
        return true;
    }
}

/* Bulk import is sent in pieces: the server rejects a body above
   BIRD_MAX_BODY_BYTES (1 MiB by default), which a geo feed crosses after
   roughly 13 400 addresses. The client is not told that limit, so the budget
   below stays deliberately under it — a few extra round trips cost nothing,
   a rejected request costs the whole import. */
const BULK_CHUNK_BYTES = 700 * 1024;
const BULK_COMMIT_RECORDS = 6000;

/* Order prefixes the same way the server does: by network address, then by
   mask length. Mask is simply added, which is safe because it never reaches
   33, so the pair keeps its meaning. This mirrors network_sort_key in
   app/parsing_engine.py — the two are pinned together by a test, because if
   they ever diverge the server would re-sort each chunk by its own key and
   the chunk order would come apart at the boundaries.

   Host bits are masked exactly like on the server, so the two keys agree even
   for a prefix that still carries them. */
function prefixSortKey(prefix) {
    const slash = prefix.indexOf('/');
    const octets = prefix.slice(0, slash).split('.');
    const address = ((+octets[0] << 24 >>> 0) + (+octets[1] << 16) +
                     (+octets[2] << 8) + +octets[3]) >>> 0;
    const mask = Number(prefix.slice(slash + 1));
    const shift = 32 - mask;
    // A shift by 32 wraps to 0 in JS, so /0 — whose network is the whole
    // space — is handled on its own; the shift back needs >>> 0 as well,
    // since << overflows into a negative int32 over the top of the range.
    const network = shift >= 32 ? 0 : (((address >>> shift) << shift) >>> 0);
    return network * 33 + mask;
}

/* Cut on a separator so a prefix can never be split in half: an address is
   digits, dots and a slash, none of which the separator class contains.
   Comma and semicolon count just as much as space — a comma-separated feed
   with no spaces at all would otherwise be cut mid-token, turning
   10.0.0.0/24 into a plausible 10.0.0.0/2. */
function splitForBulk(text, budget) {
    const chunks = [];
    let start = 0;
    while (start < text.length) {
        let end = Math.min(start + budget, text.length);
        if (end < text.length) {
            const window = text.slice(start, end);
            const separators = window.match(/[,;\s]/g);
            if (separators && separators.length) {
                end = start + window.lastIndexOf(separators[separators.length - 1]) + 1;
            }
        }
        if (end <= start) end = Math.min(start + budget, text.length);
        chunks.push(text.slice(start, end));
        start = end;
    }
    return chunks;
}

/* Blocking progress. A modal dialog makes the rest of the document inert, so
   the panel underneath cannot be touched while chunks are in flight. The
   abort button only stops the loop: whatever already committed stays, and a
   retry is safe because the server skips prefixes it already has. */
const BulkProgress = {
    _abort: false,

    open() {
        this._abort = false;
        const dialog = document.getElementById('bulk-progress');
        if (!dialog || typeof dialog.showModal !== 'function') return;
        dialog.showModal();
        const cancelBtn = document.getElementById('bulk-progress-cancel');
        if (cancelBtn) {
            cancelBtn.onclick = () => {
                this._abort = true;
                cancelBtn.disabled = true;
                const note = document.getElementById('bulk-progress-note');
                if (note) note.textContent = t('m015');
            };
        }
    },

    update(phase, index, total) {
        const label = document.getElementById('bulk-progress-label');
        const bar = document.getElementById('bulk-progress-bar');
        const cancelBtn = document.getElementById('bulk-progress-cancel');
        if (label) label.textContent = phase + t('m077') + index + t('m087') + total;
        if (bar) {
            bar.value = index;
            bar.max = total;
        }
        if (cancelBtn && !this._abort) cancelBtn.disabled = false;
    },

    /* Label for a stretch that is not a chunk loop — the review dialog and the
       community lookup. Without it the window keeps showing the last chunk of
       the previous phase, which reads as "stuck". */
    phase(text) {
        const label = document.getElementById('bulk-progress-label');
        if (label) label.textContent = text;
    },

    close() {
        const dialog = document.getElementById('bulk-progress');
        if (dialog && dialog.open) dialog.close();
    },

    get aborted() {
        return this._abort;
    },
};

async function runBulkAdd(asSelect, customInput, input, msg) {
    const raw = input.value;
    if (!raw || !raw.trim()) {
        setMsg(msg, t('m023'));
        return;
    }

    const chunks = splitForBulk(raw, BULK_CHUNK_BYTES);
    const parsed = { parsed_networks: [], invalid: [] };
    const repaired = [];
    let dropped = 0;
    let created = 0;
    let duplicates = 0;
    let invalidSkipped = 0;
    let aborted = false;

    /* One blocking dialog for the whole import: the parse chunks, the review
       dialog stacked on top of it, the community lookup and the commit chunks.
       Opening it twice left the panel live for the length of that lookup, and
       with a short paste the first dialog flashed for a few milliseconds only.
       finally guarantees it comes down on every exit, including a throw. */
    BulkProgress.open();
    try {
        let parseFailedAt = 0;
        for (let i = 0; i < chunks.length; i += 1) {
            BulkProgress.update(t('m084'), i + 1, chunks.length);
            const part = await safeFetch('/api/networks/bulk', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ raw_data: chunks[i], format: 'text' })
            }, function () { /* the progress dialog is already explaining it */ });
            if (part === null) {
                parseFailedAt = i + 1;
                break;
            }
            parsed.parsed_networks.push.apply(parsed.parsed_networks, part.parsed_networks || []);
            parsed.invalid.push.apply(parsed.invalid, part.invalid || []);
        }
        if (parseFailedAt) {
            setMsg(msg, t('m024') + parseFailedAt + t('m087') + chunks.length + t('m009'));
            return;
        }
        if (BulkProgress.aborted) {
            setMsg(msg, t('m012'));
            return;
        }

        /* Malformed prefixes: one decision for the whole batch, shown over the
           progress dialog so the operator never regains the panel mid-import. A
           repair the server could not produce is counted as dropped anyway. */
        if (parsed.invalid.length > 0) {
            BulkProgress.phase(t('m031'));
            const choice = await reviewCidrs(parsed.invalid);
            if (choice === 'fix') {
                parsed.invalid.forEach((entry) => {
                    if (entry.suggestion) {
                        repaired.push({ prefix: entry.suggestion, description: '', status: 'pending' });
                    } else {
                        dropped += 1;
                    }
                });
            } else {
                dropped = parsed.invalid.length;
            }
        }

        /* community_ids in every record. */
        BulkProgress.phase(t('m065'));
        let selected = asSelect.value;
        if (selected === '__custom__') {
            selected = customInput.value.trim();
        }

        let cid;
        try {
            cid = await resolveCommunityId(selected);
        } catch (e) {
            setMsg(msg, t('m075') + e.message);
            return;
        }

        const records = parsed.parsed_networks.concat(repaired).map((n) => ({
            ...n,
            community_ids: cid === null || cid === undefined ? [] : [cid]
        }));

        if (records.length === 0) {
            setMsg(msg, t('m008'));
            return;
        }

        /* Sort here, before the chunk split, so the chunks themselves are ordered.
           The server still sorts every chunk it receives, so this sort decides
           only where the boundaries fall — which is exactly what is needed for a
           big paste to land as one continuous ordered run instead of one
           arbitrarily ordered block per chunk. Costs about 30 ms for 50 000
           prefixes, against several seconds of parsing. */
        records.sort((a, b) => prefixSortKey(a.prefix) - prefixSortKey(b.prefix));

        /* Commit, chunked for the same reason. */
        const commitChunks = Math.ceil(records.length / BULK_COMMIT_RECORDS);
        for (let i = 0; i < commitChunks; i += 1) {
            if (BulkProgress.aborted) break;
            BulkProgress.update(t('m082'), i + 1, commitChunks);
            const slice = records.slice(i * BULK_COMMIT_RECORDS, (i + 1) * BULK_COMMIT_RECORDS);
            const data = await safeFetch('/api/networks/bulk/commit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ networks: slice, user_id: getUser() })
            }, function () { /* ditto */ });
            if (data === null) break;
            created += data.created || 0;
            duplicates += data.skipped_duplicates || 0;
            invalidSkipped += data.skipped_invalid || 0;
        }
        aborted = BulkProgress.aborted;

        let summary = t('m058') + created + t('m028') + duplicates;
        if (repaired.length) summary += t('m022') + repaired.length;
        if (dropped) summary += t('m049') + dropped;
        if (invalidSkipped) summary += t('m032') + invalidSkipped;
        if (aborted) summary += t('m006');
        setMsg(msg, summary);
    } finally {
        BulkProgress.close();
    }
    await refreshDashboard(); // confirmed added → update the list
}

function setMsg(msgEl, text) {
    msgEl.textContent = text;
    msgEl.className = 'form-msg';
    if (text.startsWith(t('m067'))) msgEl.classList.add('success');
    else msgEl.classList.add('error');
}

/* ------------------------------------------------------------------ */
/* "Add BGP Peer" panel                                                */
/* Create-only form: peer id, local AS (select or manual), neighbor IP */
/* (IPv4, no /mask), neighbor ASN (server rejects duplicate ASNs).     */
/* ------------------------------------------------------------------ */

function renderBgpPeerPanel() {
    const body = document.getElementById('bgp-body');
    if (!body) return;
    const lang = window.I18n ? window.I18n.getLang() : 'ru';
    if (body.dataset.rendered === 'true' && body.dataset.lang === lang) return;
    body.innerHTML = '';
    body.dataset.rendered = 'true';
    body.dataset.lang = lang;

    const formRow = document.createElement('div');
    formRow.className = 'bgp-form-row';

    const nameCol = document.createElement('div');
    nameCol.className = 'field';
    const nameLabel = document.createElement('label');
    nameLabel.textContent = 'Peer id';
    const nameInput = document.createElement('input');
    nameInput.id = 'bgp-name';
    nameInput.type = 'text';
    nameInput.autocomplete = 'off';
    nameInput.placeholder = 'peer_lan, edge_mt, ...';
    nameCol.appendChild(nameLabel);
    nameCol.appendChild(nameInput);

    const asCol = document.createElement('div');
    asCol.className = 'field';
    const asLabel = document.createElement('label');
    asLabel.textContent = 'Local AS';
    const asSelect = document.createElement('select');
    asSelect.id = 'bgp-local-as';
    const defOpt = document.createElement('option');
    defOpt.value = '65500';
    defOpt.textContent = '65500';
    asSelect.appendChild(defOpt);
    const customOpt = document.createElement('option');
    customOpt.value = '__custom__';
    customOpt.textContent = t('m044');
    asSelect.appendChild(customOpt);

    /* Seed every ASN found in the communities table, so a peer can be
       created for any community the operator has defined (manual entry
       stays available through the last option). */
    fetchCommunities().then((data) => {
        if (!data || !Array.isArray(data.communities)) return;
        const asns = new Set(['65534']);
        data.communities.forEach((comm) => {
            const asn = String(comm.name || '').split(':')[0].trim();
            if (asn) asns.add(asn);
        });
        Array.from(asns)
            .filter((asn) => asn !== '65500')
            .sort((a, b) => a.localeCompare(b, 'en', { numeric: true }))
            .forEach((asn) => {
                const opt = document.createElement('option');
                opt.value = asn;
                opt.textContent = asn;
                asSelect.insertBefore(opt, customOpt);
            });
    });
    const asCustom = document.createElement('input');
    asCustom.id = 'bgp-local-as-custom';
    asCustom.className = 'hidden';
    asCustom.type = 'text';
    asCustom.autocomplete = 'off';
    asCustom.placeholder = t('m060');
    asCol.appendChild(asLabel);
    asCol.appendChild(asSelect);
    asCol.appendChild(asCustom);

    const nbCol = document.createElement('div');
    nbCol.className = 'field';
    const nbLabel = document.createElement('label');
    nbLabel.textContent = 'Neighbor (IPv4)';
    const nbInput = document.createElement('input');
    nbInput.id = 'bgp-neighbor';
    nbInput.type = 'text';
    nbInput.autocomplete = 'off';
    nbInput.placeholder = '192.168.0.1';
    nbCol.appendChild(nbLabel);
    nbCol.appendChild(nbInput);

    const nbAsCol = document.createElement('div');
    nbAsCol.className = 'field';
    const nbAsLabel = document.createElement('label');
    nbAsLabel.textContent = 'Neighbor ASN';
    const nbAsInput = document.createElement('input');
    nbAsInput.id = 'bgp-neighbor-as';
    nbAsInput.type = 'text';
    nbAsInput.autocomplete = 'off';
    nbAsInput.placeholder = '64999';
    nbAsCol.appendChild(nbAsLabel);
    nbAsCol.appendChild(nbAsInput);

    formRow.appendChild(nameCol);
    formRow.appendChild(asCol);
    formRow.appendChild(nbCol);
    formRow.appendChild(nbAsCol);

    /* ASN-scoped export is the default: the peer only receives the prefixes
       whose community ASN equals its local AS, and an ASN that no Community
       carries yields an empty filter. The box opts out of that scope and
       sends every ASN's prefixes instead. Off unless the operator ticks it.

       Deliberately not carrying the generic `field` class: `.field input`
       stretches a control to 100% width, which pushed the checkbox away from
       its own label. */
    const scopeCol = document.createElement('label');
    scopeCol.className = 'bgp-scope-field';
    const scopeInput = document.createElement('input');
    scopeInput.type = 'checkbox';
    scopeInput.id = 'bgp-export-all';
    const scopeText = document.createElement('span');
    scopeText.textContent = t('m136');
    const scopeHint = document.createElement('div');
    scopeHint.className = 'form-hint';
    scopeHint.textContent = t('m104');
    scopeCol.appendChild(scopeInput);
    scopeCol.appendChild(scopeText);
    scopeCol.appendChild(scopeHint);

    const btnRow = document.createElement('div');
    btnRow.className = 'form-row';
    const submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.className = 'btn btn-primary';
    submitBtn.textContent = t('m143');
    btnRow.appendChild(submitBtn);

    const msg = document.createElement('div');
    msg.id = 'bgp-msg';
    msg.className = 'form-msg';

    asSelect.addEventListener('change', () => {
        asCustom.classList.toggle('hidden', asSelect.value !== '__custom__');
    });

    submitBtn.addEventListener('click', () => runBgpAdd(
        asSelect, asCustom, nameInput, nbInput, nbAsInput, scopeInput, msg));

    body.appendChild(formRow);
    body.appendChild(scopeCol);
    body.appendChild(btnRow);
    body.appendChild(msg);
}

async function runBgpAdd(asSelect, asCustom, nameInput, nbInput, nbAsInput, exportAll, msg) {
    let localAs = asSelect.value;
    if (asSelect.value === '__custom__') {
        localAs = asCustom.value.trim();
    }
    const payload = {
        name: nameInput.value.trim(),
        local_as: localAs,
        neighbor: nbInput.value.trim(),
        neighbor_as: nbAsInput.value.trim(),
        export_all: exportAll.checked,
        user_id: getUser(),
    };
    if (!payload.name || !payload.neighbor || !payload.neighbor_as) {
        setMsg(msg, t('m108'));
        return;
    }

    setMsg(msg, t('m158'));
    const data = await safeFetch('/api/bgp_peers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });

    if (data === null) {
        setMsg(msg, t('m119'));
        return;
    }
    if (data.error) {
        setMsg(msg, (data.message || data.error) + '');
        return;
    }
    const peer = data.bgp_peer;
    setMsg(msg, t('m_t3', { name: peer.name, neighbor: peer.neighbor, asn: peer.neighbor_as }));
    msg.classList.remove('error');
    msg.classList.add('success');
    /* A peer whose ASN matches no community gets an empty export filter, so it
       would come up and announce nothing. Say so here rather than leaving the
       operator to find it on the MikroTik. */
    if (!peer.export_all && peer.asn_found === false) {
        setMsg(msg, t('m_t2', { asn: peer.local_as }), 'warning');
    } else if (!peer.export_all) {
        msg.textContent += t('m_t1', { n: peer.export_count });
    }
    nameInput.value = '';
    nbInput.value = '';
    nbAsInput.value = '';
    await refreshDashboard();
}

/* Default selection resolves to the canonical persisted Community. */
async function resolveCommunityId(name) {
    const wanted = name.trim();
    if (wanted === '' || wanted === '__default__') {
        const data = await safeFetch('/api/communities', undefined);
        if (data === null) throw new Error(t('m130'));
        COMMUNITY_CACHE = data.communities;
        const existing = COMMUNITY_CACHE.find((c) => c.name.trim() === '65534');
        if (existing) return existing.id;
        const created = await safeFetch('/api/communities', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: '65534', values: '0', description: 'default', enabled: true
            })
        });
        if (created === null) throw new Error(t('m109'));
        return created.community.id;
    }

    const data = await safeFetch('/api/communities', undefined);
    if (data === null) throw new Error(t('m130'));
    COMMUNITY_CACHE = data.communities;
    const existing = COMMUNITY_CACHE.find((c) => c.name.trim() === wanted);
    if (existing) return existing.id;

    /* Manual input of a new AS:COMMUNITY → create it. */
    const created = await safeFetch('/api/communities', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: wanted, values: '', description: '' })
    });
    if (created === null) throw new Error(t('m122') + wanted + '»');
    return created.community.id;
}

/* ------------------------------------------------------------------ */
/* Config status + audit panels (R4.1.2)                               */
/* ------------------------------------------------------------------ */

async function loadConfigStatus() {
    const data = await safeFetch('/api/config/status?filename=networks.conf', undefined);
    if (data === null) return;
    renderConfigStatus(data);
    renderBackupStatus(data);
    wireConfigStatusActions();
}

/* ------------------------------------------------------------------ */
/* BIRD peers — live snapshot from `birdc show protocols all`          */
/* ------------------------------------------------------------------ */

const PEER_COLUMNS = [
    'Managed', 'Peer', 'Neighbor', 'Neighbor AS', 'Local AS',
    t('m159'), t('m161'), 'Since', 'Last error',
];

function peersPanelVisible() {
    const section = document.getElementById('panel-bgp-sessions');
    if (!section || !section.classList.contains('expanded')) return false;
    if (typeof currentTab !== 'undefined' && currentTab !== 'system') return false;
    if (typeof document !== 'undefined' && document.hidden) return false;
    return true;
}

async function loadBirdPeers(options) {
    const opts = options || {};
    const onError = opts.quiet
        ? function () { /* auto-poll: the inline message below is enough */ }
        : undefined;
    const data = await safeFetch('/api/bird/protocols', undefined, onError);
    if (data === null) {
        if (opts.quiet) setPeersMessage(t('m130'), 'error');
        return;
    }
    renderBirdPeers(data);
    PeersPoll.reset();
}

function setPeersMessage(text, kind) {
    const el = document.getElementById('peers-msg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'form-msg' + (kind ? ' ' + kind : '');
}

/* Two badges, both with the same pill design and a tooltip naming the birdc
   field the value came from: the channel state on top, then the BGP state
   (falling back to the summary State column) when it says something new. */
function peerStateCell(peer) {
    const cell = document.createElement('td');
    if (peer.in_bird === false) {
        const missing = document.createElement('span');
        missing.className = 'badge badge-other';
        missing.textContent = t('m156');
        missing.title = t('m105');
        cell.appendChild(missing);
        return cell;
    }

    const channel = peer.channel_state || peer.state || '';
    const channelBadge = document.createElement('span');
    const channelUp = String(peer.channel_state || '').toUpperCase() === 'UP';
    channelBadge.className = 'badge ' + (channelUp ? 'badge-applied' : 'badge-pending');
    channelBadge.textContent = channel || '—';
    channelBadge.title = (peer.channel_state
        ? t('m117')
        : t('m114')) + (channel || '—');
    cell.appendChild(channelBadge);

    const detail = peer.bgp_state || peer.state || '';
    if (!detail || detail.toLowerCase() === String(channel).toLowerCase()) return cell;
    const detailBadge = document.createElement('span');
    const detailKey = detail.toLowerCase();
    const established = detailKey === 'established' || detailKey === 'up';
    detailBadge.className = 'badge ' + (established ? 'badge-applied' : 'badge-other');
    detailBadge.textContent = detail;
    detailBadge.title = (peer.bgp_state
        ? 'BGP state: '
        : t('m114')) + detail;
    cell.appendChild(document.createElement('br'));
    cell.appendChild(detailBadge);
    return cell;
}

/* One rx/tx line inside the "Prefixes" cell — a span, not a nested cell. */
function peerCounterLine(prefix, value) {
    const line = document.createElement('span');
    line.className = 'peer-num';
    line.textContent = value === null || value === undefined
        ? prefix + ' —'
        : prefix + ' ' + value;
    return line;
}

function renderBirdPeers(data) {
    const bgpBody = document.getElementById('bgp-sessions-body');
    const svcBody = document.getElementById('service-protocols-body');
    if (!bgpBody || !svcBody) return;
    const peers = Array.isArray(data.peers) ? data.peers : [];
    const service = Array.isArray(data.service) ? data.service : [];
    bgpBody.innerHTML = '';
    svcBody.innerHTML = '';

    if (data.status === 'error') {
        const warn = document.createElement('div');
        warn.className = 'form-msg error';
        warn.textContent = 'BIRD: ' + (data.error || t('m155'));
        bgpBody.appendChild(warn);
        svcBody.appendChild(warn.cloneNode(true));
        return;
    }

    if (service.length) {
        const list = document.createElement('div');
        list.className = 'peer-service-list';
        const head = document.createElement('div');
        head.className = 'peer-service-row';
        [t('m162'), t('m167'), t('m159'), t('m165'), 'Since'].forEach((label) => {
            const cell = document.createElement('span');
            cell.className = 'peer-fsm';
            cell.textContent = label;
            head.appendChild(cell);
        });
        list.appendChild(head);
        service.forEach((item) => {
            const row = document.createElement('div');
            row.className = 'peer-service-row';
            const up = String(item.state || '').toLowerCase() === 'up';
            const name = document.createElement('span');
            name.className = 'peer-service-name';
            name.textContent = item.name;
            const proto = document.createElement('span');
            proto.textContent = item.proto || '';
            const state = document.createElement('span');
            const badge = document.createElement('span');
            badge.className = 'badge ' + (up ? 'badge-applied' : 'badge-pending');
            badge.textContent = item.state || '—';
            state.appendChild(badge);
            const channel = document.createElement('span');
            channel.textContent = item.channel_state || '—';
            const since = document.createElement('span');
            since.textContent = item.since || '—';
            row.append(name, proto, state, channel, since);
            list.appendChild(row);
        });
        svcBody.appendChild(list);
    }

    const wrap = document.createElement('div');
    wrap.className = 'peer-table-wrap';
    const table = document.createElement('table');
    table.className = 'record-table peer-table';
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    PEER_COLUMNS.forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    if (!peers.length) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = PEER_COLUMNS.length;
        cell.className = 'placeholder';
        cell.textContent = t('m142');
        row.appendChild(cell);
        tbody.appendChild(row);
    } else {
        peers.forEach((peer) => {
            const row = document.createElement('tr');
            const managed = document.createElement('td');
            managed.textContent = peer.managed ? 'yes' : 'no';
            const name = document.createElement('td');
            name.className = 'peer-service-name';
            name.textContent = peer.name;
            const neighbor = document.createElement('td');
            neighbor.textContent = peer.neighbor || '—';
            const neighborAs = document.createElement('td');
            neighborAs.textContent = peer.neighbor_as || '—';
            const localAs = document.createElement('td');
            localAs.textContent = peer.local_as || '—';
            const stateCell = peerStateCell(peer);
            const prefixes = document.createElement('td');
            prefixes.className = 'peer-num';
            prefixes.append(
                peerCounterLine('rx', peer.imported),
                document.createElement('br'),
                peerCounterLine('tx', peer.exported)
            );
            const since = document.createElement('td');
            since.textContent = peer.since || '—';
            const error = document.createElement('td');
            error.className = 'peer-error';
            error.textContent = peer.last_error || '';
            if (peer.last_error) error.title = peer.last_error;
            row.append(managed, name, neighbor, neighborAs, localAs,
                stateCell, prefixes, since, error);
            tbody.appendChild(row);
        });
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    bgpBody.appendChild(wrap);

    const message = document.createElement('div');
    message.className = 'form-msg';
    message.id = 'peers-msg';
    bgpBody.appendChild(message);
}

/* Auto-poll every 30s, only while the System tab shows an expanded block.
   Any manual load (header ⟳, tab activation, peer mutations) resets it. */
const PEERS_POLL_MS = 30000;
const PeersPoll = {
    timerId: null,

    start() {
        this.stop();
        if (!peersPanelVisible()) return;
        this.timerId = setInterval(() => this.tick(), PEERS_POLL_MS);
    },

    stop() {
        if (this.timerId !== null) {
            clearInterval(this.timerId);
            this.timerId = null;
        }
    },

    reset() {
        this.start();
    },

    async tick() {
        if (!peersPanelVisible()) {
            this.stop();
            return;
        }
        await loadBirdPeers({ quiet: true });
    },
};

function renderConfigStatus(data) {
    const body = document.getElementById('config-status-body');
    if (!body) return;
    const birdState = data.bird_running
        ? t('m101')
        : t('m100');
    const reloadState = data.reload_warning
        ? '<div class="form-row"><span class="field" title="' + escapeHtml(data.reload_detail || '') + t('m107') +
          escapeHtml(data.reload_detail || t('m154')) + '</span></div>'
        : '';
    body.innerHTML =
        t('m099') + escapeHtml(data.last_applied || '—') + '</strong></span></div>' +
        t('m103') + escapeHtml(data.action || '—') + '</strong></span></div>' +
        t('m102') + escapeHtml(data.user_id || '—') + '</strong></span></div>' +
        '<div class="form-row">' + birdState + '</div>' +
        '<div class="form-row"><span class="field"><strong>BIRD restart:</strong> ' + escapeHtml(data.bird_last_reboot || '—') + '</span></div>' +
        '<div class="form-row"><span class="field"><strong>BIRD reconfiguration:</strong> ' + escapeHtml(data.bird_last_reconfiguration || '—') + '</span></div>' +
        reloadState +
        '<div class="form-row config-status-actions">' +
        '<button type="button" class="btn" id="bird-reload-btn">BIRD reload</button>' +
        '</div>' +
        '<div class="form-row"><span id="config-status-msg" class="form-msg"></span></div>';
}

function renderBackupStatus(data) {
    const body = document.getElementById('backup-body');
    if (!body) return;
    const backup = data.backup || {};
    const items = Array.isArray(backup.items) ? backup.items : [];
    const slots = backup.slots || ((backup.used || 0) + '/' + (backup.retention || 0));
    body.innerHTML = '';

    const summary = document.createElement('div');
    summary.className = 'backup-summary';
    const slotText = document.createElement('span');
    slotText.className = 'backup-slots';
    slotText.textContent = t('m163') + slots;
    const freeText = document.createElement('span');
    freeText.textContent = t('m153') + (backup.free || 0);
    const createBtn = document.createElement('button');
    createBtn.type = 'button';
    createBtn.className = 'btn';
    createBtn.id = 'backup-btn';
    createBtn.textContent = t('m145');
    createBtn.addEventListener('click', () => createBackupSnapshot());
    summary.append(slotText, freeText, createBtn);
    body.appendChild(summary);

    const rollback = document.createElement('div');
    rollback.className = 'form-row';
    const rollbackBadge = document.createElement('span');
    rollbackBadge.className = 'badge ' + (data.has_backup ? 'badge-applied' : 'badge-pending');
    rollbackBadge.textContent = t('m137') + (data.has_backup ? t('m166') : t('m168'));
    rollback.appendChild(rollbackBadge);
    body.appendChild(rollback);

    const list = document.createElement('div');
    list.className = 'backup-list';
    if (!items.length) {
        const empty = document.createElement('span');
        empty.className = 'placeholder';
        empty.textContent = t('m149');
        list.appendChild(empty);
    } else {
        items.forEach((item) => {
            const row = document.createElement('div');
            row.className = 'backup-row';
            row.dataset.backupId = item.id;

            const name = document.createElement('span');
            name.className = 'backup-name';
            name.textContent = item.display_time || item.legacy_time || item.created_at || item.id;
            const restoreBtn = document.createElement('button');
            restoreBtn.type = 'button';
            restoreBtn.className = 'btn backup-restore-btn';
            restoreBtn.textContent = t('m095');
            const deleteBtn = document.createElement('button');
            deleteBtn.type = 'button';
            deleteBtn.className = 'btn btn-danger backup-delete-btn';
            deleteBtn.textContent = t('m078');

            restoreBtn.addEventListener('click', () => {
                restoreBtn.disabled = true;
                deleteBtn.disabled = true;
                restoreBackupSnapshot(item.id, name.textContent).finally(() => {
                    restoreBtn.disabled = false;
                    deleteBtn.disabled = false;
                });
            });
            deleteBtn.addEventListener('click', () => {
                restoreBtn.disabled = true;
                deleteBtn.disabled = true;
                deleteBackupSnapshot(item.id, name.textContent).finally(() => {
                    restoreBtn.disabled = false;
                    deleteBtn.disabled = false;
                });
            });
            row.append(name, restoreBtn, deleteBtn);
            list.appendChild(row);
        });
    }
    body.appendChild(list);

    const message = document.createElement('div');
    message.className = 'form-row';
    const messageText = document.createElement('span');
    messageText.id = 'backup-msg';
    messageText.className = 'form-msg';
    message.appendChild(messageText);
    body.appendChild(message);
}

function wireConfigStatusActions() {
    const reloadBtn = document.getElementById('bird-reload-btn');
    if (reloadBtn) reloadBtn.addEventListener('click', () => reloadBirdService());
}

function statusMsg(text, kind) {
    setPanelMessage('config-status-msg', text, kind);
}

function backupMsg(text, kind) {
    setPanelMessage('backup-msg', text, kind);
}

function setPanelMessage(id, text, kind) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = 'form-msg ' + (kind || '');
}

async function createBackupSnapshot() {
    backupMsg(t('m146'), '');
    let text = '';
    let kind = '';
    try {
        const res = await fetch(API_BASE + '/api/backup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: getUser() })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status === 'error') {
            text = data.error || t('m124');
            kind = 'error';
        } else {
            const slots = data.backup ? data.backup.slots : '';
            text = t('m147') + (slots ? ' (' + slots + ')' : '');
            kind = 'success';
        }
    } catch (e) {
        text = t('m075') + e;
        kind = 'error';
    }
    await loadConfigStatus();
    await loadAudit();
    backupMsg(text, kind);
}

async function restoreBackupSnapshot(backupId, displayTime) {
    if (!window.confirm(t('m096') + displayTime + t('m097'))) return;
    backupMsg(t('m140') + displayTime + '…', '');
    let text = '';
    let kind = '';
    try {
        const res = await fetch(API_BASE + '/api/backup/' + encodeURIComponent(backupId) + '/restore', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: getUser() })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status === 'error') {
            text = data.error || data.message || t('m128');
            kind = 'error';
        } else {
            text = data.message || t('m133');
            kind = 'success';
        }
    } catch (e) {
        text = t('m075') + e;
        kind = 'error';
    }
    await refreshHierarchy();
    await loadConfigStatus();
    await loadAudit();
    backupMsg(text, kind);
}

async function deleteBackupSnapshot(backupId, displayTime) {
    if (!window.confirm(t('m135') + displayTime + t('m123'))) return;
    backupMsg(t('m164') + displayTime + '…', '');
    let text = '';
    let kind = '';
    try {
        const res = await fetch(API_BASE + '/api/backup/' + encodeURIComponent(backupId), {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: getUser() })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status === 'error') {
            text = data.error || data.message || t('m125');
            kind = 'error';
        } else {
            text = data.message || t('m148');
            kind = 'success';
        }
    } catch (e) {
        text = t('m075') + e;
        kind = 'error';
    }
    await loadConfigStatus();
    await loadAudit();
    backupMsg(text, kind);
}

async function reloadBirdService() {
    statusMsg(t('m126'), '');
    let text = '';
    let kind = '';
    try {
        const res = await fetch(API_BASE + '/api/bird/reload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: getUser() })
        });
        const data = await res.json().catch(() => ({}));
        if (data.status === 'ok') {
            text = t('m120');
            kind = 'success';
        } else {
            text = data.error || t('m113');
            kind = 'error';
        }
    } catch (e) {
        text = t('m075') + e;
        kind = 'error';
    }
    await loadConfigStatus();
    statusMsg(text, kind);
}

async function loadAudit() {
    const body = document.getElementById('audit-body');
    const data = await safeFetch('/api/audit?limit=10', undefined);
    if (data === null) return;
    const entries = data.audit_log || [];
    if (!entries.length) {
        body.innerHTML = '';
        const span = document.createElement('span');
        span.className = 'placeholder';
        span.textContent = t('m150');
        body.appendChild(span);
        return;
    }
    body.innerHTML = '';
    entries.forEach((entry) => {
        const row = document.createElement('div');
        row.className = 'form-row';
        row.innerHTML =
            '<span class="field"><strong>' + escapeHtml(entry.action || '') + '</strong></span>' +
            '<span class="field">' + escapeHtml(entry.timestamp || '') + '</span>' +
            '<span class="field">' + escapeHtml(entry.user_id || '') + '</span>';
        body.appendChild(row);
    });
}

/* ------------------------------------------------------------------ */
/* Config editor (R4.6.x) — live config + diff preview + apply        */
/* ------------------------------------------------------------------ */

const ACTIVE_FILE = 'networks.conf';
const LINE_H = 20;
const PANE_PAD = 8;

let overviewDiffCounts = { adds: 0, dels: 0 };
let overviewNetworkDiffCounts = { adds: 0, dels: 0 };
let previewRows = [];
let errorRows = [];
let errorMessages = [];

/* Left pane: semi-transparent red bars over the lines that failed syntax
   validation. Aligned with the same LINE_H/PANE_PAD math as the diff rows.
   `rows` are 1-based editor lines for the edited file only; the caller is
   responsible for filtering out errors that belong to other bundle files. */
function renderErrorOverlay(rows, messages) {
    const el = document.getElementById('config-errors');
    const textarea = document.getElementById('active-config');
    if (!el || !textarea) return;
    el.innerHTML = '';
    errorRows = rows || [];
    errorMessages = messages || [];
    if (!errorRows.length) return;
    const width = Math.max(textarea.offsetWidth, 300);
    errorRows.forEach((row, i) => {
        if (!Number.isInteger(row) || row < 1) return;
        const bar = document.createElement('div');
        bar.className = 'cfg-error-row';
        bar.style.top = (PANE_PAD + (row - 1) * LINE_H) + 'px';
        bar.style.height = LINE_H + 'px';
        bar.style.width = width + 'px';
        bar.title = errorMessages[i] || t('m129');
        el.appendChild(bar);
    });
    const first = el.firstElementChild;
    if (first) {
        const scroller = document.getElementById('diff-scroll');
        if (scroller) scroller.scrollTop = Math.max(0, first.offsetTop - 40);
    }
}

function clearErrorOverlay() {
    errorRows = [];
    errorMessages = [];
    const el = document.getElementById('config-errors');
    if (el) el.innerHTML = '';
}

/* Both editor columns are grown to the same height, so the panel's single
   outer scrollbar carries the pair. The row model already guarantees the same
   number of rows on both sides, which is what keeps the columns aligned. */
function resizeEditorPanes() {
    const textarea = document.getElementById('active-config');
    const rows = document.getElementById('editor-rows');
    if (!textarea || !rows) return;
    const height = (textarea.value || '').split('\n').length * LINE_H + PANE_PAD * 2;
    textarea.style.height = height + 'px';
    rows.style.height = height + 'px';
}

/* GitHub-style "+N −M" counter in the panel title; hidden when no changes. */
/* A modified line counts once on each side, the same way a unified diff does. */
function countsFromRows(rows) {
    let adds = 0;
    let dels = 0;
    (rows || []).forEach((row) => {
        if (row.kind === 'added' || row.kind === 'changed') adds += 1;
        if (row.kind === 'removed' || row.kind === 'changed') dels += 1;
    });
    return { adds: adds, dels: dels };
}

/* The editor's rows are the authority for networks.conf while editing: they
   describe live -> merged, which includes edits not yet written to staged, so
   the overview's live -> staged count for that file must not be added on top. */
function updateDiffCounter(rows) {
    const el = document.getElementById('diff-count');
    if (!el) return;
    const editor = countsFromRows(rows);
    const networkAdds = (rows && rows.length) ? editor.adds : overviewNetworkDiffCounts.adds;
    const networkDels = (rows && rows.length) ? editor.dels : overviewNetworkDiffCounts.dels;
    const adds = overviewDiffCounts.adds + networkAdds;
    const dels = overviewDiffCounts.dels + networkDels;
    setSectionCount('networks-section-count', networkAdds, networkDels);
    if (adds === 0 && dels === 0) {
        el.classList.add('hidden');
        el.innerHTML = '';
        return;
    }
    el.classList.remove('hidden');
    el.innerHTML = '<span class="add">+' + adds + '</span> <span class="del">−' + dels + '</span>';
}

/* An unreadable file must not look like an unchanged one: reporting +0 −0 for
   a file the server could not decode would invite an Apply over it. */
function setSectionCount(id, adds, dels, note) {
    const el = document.getElementById(id);
    if (!el) return;
    if (note) {
        el.innerHTML = '<span class="unreadable">' + note + '</span>';
        el.classList.add('has-warning');
        return;
    }
    el.classList.remove('has-warning');
    el.innerHTML = '<span class="add">+' + (adds || 0) + '</span> <span class="del">−' + (dels || 0) + '</span>';
}

/* A file bigger than this is not opened at all: the header keeps its counters
   and the body only explains why. Tens of thousands of DOM rows buy nothing —
   the counters already say exactly what would be lost. */
const ROW_VIEW_MAX_ROWS = 5000;

/* Shown instead of the counters when a bundle file does not decode as UTF-8. */
const UNREADABLE_NOTE = t('m062');

/* Section view id -> the file it shows. Kept explicit rather than derived from
   the id, so a rename cannot silently point a pane at another file. */
const SECTION_FILES = {
    'manager-conf-rows': 'bird-manager.conf',
    'core-rows': 'core.conf',
};

function rowCell(className, text) {
    const cell = document.createElement('span');
    cell.className = className;
    cell.textContent = (text === null || text === undefined) ? '' : text;
    return cell;
}

/* Render the server's line model. Both sides carry the same number of rows, so
   the columns line up by index — no absolute offsets, no scroll arithmetic.
   mode 'both' shows live and staged side by side; 'new' shows only staged,
   because the editor already provides live on its left. Cells are filled with
   textContent, never innerHTML, so operator-written config cannot inject
   markup. */
function renderRowView(container, rows, mode, note) {
    container.textContent = '';
    const both = mode !== 'new';
    if (!rows || !rows.length) {
        const empty = document.createElement('div');
        empty.className = 'row-line';
        empty.appendChild(rowCell('row-cell row-note', note || t('m144')));
        container.appendChild(empty);
        return;
    }
    rows.forEach((row) => {
        const line = document.createElement('div');
        line.className = 'row-line row-' + row.kind;
        if (both) {
            line.appendChild(rowCell('row-cell row-old', row.old));
            line.appendChild(rowCell('row-cell row-new', row.new));
        } else {
            /* Editor mode: only the staged side is drawn, because live is
               already the left pane. A removed row has no staged text, so the
               placeholder carries the dropped line instead of a blank cell —
               the operator still sees what the edit deleted. */
            line.appendChild(rowCell(
                'row-cell row-new', row.kind === 'removed' ? row.old : row.new
            ));
        }
        container.appendChild(line);
    });
    if (note) {
        const tail = document.createElement('div');
        tail.className = 'row-line';
        tail.appendChild(rowCell('row-cell row-note', note));
        container.appendChild(tail);
    }
}

function rowsTruncationNote(data) {
    if (!data || !data.truncated) return '';
    return t('m138') + data.returned_rows + t('m087') + data.total_rows
        + t('m139') + data.adds + ' −' + data.dels + ')';
}

function tooLargeNote(rows) {
    return t('m112') + rows + t('m141')
        + ROW_VIEW_MAX_ROWS + t('m115');
}

/* Lazily fetched line models, keyed by filename plus the counters they were
   fetched for, so a file whose counts moved is never served a stale model.

   The key cannot cover every case: replacing one line with another keeps the
   counts at +1 −1 while the rows are completely different, and the key is
   then unchanged. Anything that recomputes the diff clears the cache, so a
   recompute always reaches the server instead of replaying a stale model. */
const fileRowsCache = new Map();
const FILE_ROWS_CACHE_MAX = 8;

function fileRowsCacheKey(row) {
    return row.filename + ':' + (row.adds || 0) + ':' + (row.dels || 0);
}

function clearFileRowsCache() {
    fileRowsCache.clear();
}

async function fetchFileRows(row) {
    const key = fileRowsCacheKey(row);
    const cached = fileRowsCache.get(key);
    if (cached) return cached;
    const data = await safeFetch(
        '/api/config/file-rows?filename=' + encodeURIComponent(row.filename)
    );
    if (data === null) return null;
    fileRowsCache.set(key, data);
    while (fileRowsCache.size > FILE_ROWS_CACHE_MAX) {
        fileRowsCache.delete(fileRowsCache.keys().next().value);
    }
    return data;
}

/* Counts travel on the section element, so expanding a section later can tell
   whether the file is worth fetching without another overview round trip. */
function applySectionMeta(section, meta) {
    if (!section) return;
    const source = meta || {};
    section.dataset.rows = String(source.rows || 0);
    section.dataset.adds = String(source.adds || 0);
    section.dataset.dels = String(source.dels || 0);
}

function sectionMeta(section) {
    return {
        rows: Number((section && section.dataset.rows) || 0),
        adds: Number((section && section.dataset.adds) || 0),
        dels: Number((section && section.dataset.dels) || 0),
    };
}

async function openFileSection(section, viewId, noteId) {
    const view = document.getElementById(viewId);
    const note = document.getElementById(noteId);
    if (!view) return;
    const meta = sectionMeta(section);
    if (meta.rows > ROW_VIEW_MAX_ROWS) {
        view.hidden = true;
        if (note) {
            note.textContent = tooLargeNote(meta.rows);
            note.hidden = false;
        }
        return;
    }
    if (note) {
        note.textContent = '';
        note.hidden = true;
    }
    view.hidden = false;
    renderRowView(view, [], 'both', t('m131'));
    const data = await fetchFileRows({
        filename: SECTION_FILES[viewId], adds: meta.adds, dels: meta.dels,
    });
    if (data === null) {
        renderRowView(view, [], 'both', t('m116'));
        return;
    }
    renderRowView(view, data.rows, 'both', rowsTruncationNote(data));
}

/* Files the operator has opened in the summary. renderConfigSummary rebuilds
   the list, so the open panes must be reopened by name afterwards — see
   refreshBirdTab. */
const summaryOpenViews = new Set();
let overviewSummary = [];

async function openSummaryFileView(item, row) {
    const pane = document.createElement('div');
    pane.className = 'row-view row-view-scroll config-summary-view';
    pane.dataset.viewFor = row.viewId;
    item.after(pane);
    item.classList.add('expanded');
    item.setAttribute('aria-expanded', 'true');
    summaryOpenViews.add(row.viewId);

    if (row.rows > ROW_VIEW_MAX_ROWS) {
        renderRowView(pane, [], 'both', tooLargeNote(row.rows));
        return;
    }
    renderRowView(pane, [], 'both', t('m131'));
    const data = await fetchFileRows(row);
    if (data === null) {
        renderRowView(pane, [], 'both', t('m116'));
        return;
    }
    renderRowView(pane, data.rows, 'both', rowsTruncationNote(data));
}

function closeSummaryFileView(item, viewId) {
    const existing = item.nextElementSibling;
    if (existing && existing.dataset.viewFor === viewId) existing.remove();
    item.classList.remove('expanded');
    item.setAttribute('aria-expanded', 'false');
    summaryOpenViews.delete(viewId);
}

async function toggleFileView(item, row) {
    const existing = item.nextElementSibling;
    if (existing && existing.dataset.viewFor === row.viewId) {
        closeSummaryFileView(item, row.viewId);
        return;
    }
    await openSummaryFileView(item, row);
}

function renderConfigSummary(rows) {
    const body = document.getElementById('config-summary-body');
    if (!body) return;
    body.innerHTML = '';
    overviewSummary = (rows || []).slice();
    /* A file that is gone, unreadable or over the row limit is no longer
       expandable; drop it so the reopen pass cannot resurrect a pane that has
       nothing to show. */
    const selectable = new Set(overviewSummary.filter((row) => {
        return row.readable !== false && (row.rows || 0) <= ROW_VIEW_MAX_ROWS;
    }).map((row) => 'summary-' + row.filename));
    for (const viewId of Array.from(summaryOpenViews)) {
        if (!selectable.has(viewId)) summaryOpenViews.delete(viewId);
    }
    let adds = 0;
    let dels = 0;
    (rows || []).forEach((row) => {
        const viewId = 'summary-' + row.filename;
        const unreadable = row.readable === false;
        const tooLarge = (row.rows || 0) > ROW_VIEW_MAX_ROWS;
        const changed = (row.adds || 0) > 0 || (row.dels || 0) > 0;
        const item = document.createElement(tooLarge || unreadable ? 'div' : 'button');
        item.dataset.viewId = viewId;
        item.className = 'config-summary-row'
            + (!tooLarge && !unreadable && changed ? ' expandable' : '')
            + (tooLarge ? ' too-large' : '')
            + (unreadable ? ' too-large' : '')
            + (!tooLarge && !unreadable && changed && row.filename !== 'networks.conf' ? ' changed' : '');
        if (unreadable) {
            item.title = t('m106');
        } else if (!tooLarge) {
            item.type = 'button';
            item.setAttribute('aria-expanded', 'false');
            item.title = t('m118');
            item.addEventListener('click', () => toggleFileView(item, {
                filename: row.filename, viewId: viewId,
                adds: row.adds, dels: row.dels, rows: row.rows,
            }));
        } else {
            item.title = tooLargeNote(row.rows);
        }
        const name = document.createElement('span');
        name.textContent = row.filename;
        const count = document.createElement('span');
        count.className = 'section-count';
        count.innerHTML = unreadable
            ? '<span class="unreadable">' + UNREADABLE_NOTE + '</span>'
            : '<span class="add">+' + (row.adds || 0)
                + '</span> <span class="del">−' + (row.dels || 0) + '</span>';
        item.appendChild(name);
        item.appendChild(count);
        body.appendChild(item);
        if (unreadable) return;
        adds += row.adds || 0;
        dels += row.dels || 0;
    });
    setSectionCount('summary-section-count', adds, dels);
    return { adds, dels };
}

async function loadConfigOverview() {
    const data = await safeFetch('/api/config/overview', undefined);
    if (data === null) return;
    const managed = data.managed_conf || {};
    const core = data.core || {};
    const networks = data.networks || {};

    /* Every file section starts collapsed and keeps only its counters; the
       rows are fetched when a section is opened. */
    applySectionMeta(document.querySelector('[data-section="networks"]'), networks);
    applySectionMeta(document.querySelector('[data-section="manager"]'), managed);
    applySectionMeta(document.querySelector('[data-section="core"]'), core);
    setSectionCount('networks-section-count', networks.adds, networks.dels,
        networks.readable === false ? UNREADABLE_NOTE : '');
    setSectionCount('manager-section-count', managed.adds, managed.dels,
        managed.readable === false ? UNREADABLE_NOTE : '');
    setSectionCount('core-section-count', core.adds, core.dels,
        core.readable === false ? UNREADABLE_NOTE : '');

    const summary = renderConfigSummary(data.summary || []);
    overviewNetworkDiffCounts = {
        adds: networks.adds || 0,
        dels: networks.dels || 0,
    };
    overviewDiffCounts = {
        adds: (managed.adds || 0) + (core.adds || 0) + (summary ? summary.adds : 0),
        dels: (managed.dels || 0) + (core.dels || 0) + (summary ? summary.dels : 0),
    };
    updateDiffCounter(previewRows);
}

/* The editor is loaded on first expand, not on page load: with every section
   collapsed there is nothing to show until the operator asks for it. */
async function ensureEditorLoaded(section) {
    if (!section || section.dataset.loaded === 'true') return;
    section.dataset.loaded = 'true';
    await loadActiveConfig();
}

async function loadActiveConfig() {
    const textarea = document.getElementById('active-config');
    if (!textarea) return;
    textarea.placeholder = t('m121');
    const data = await safeFetch('/api/config/staged?filename=' + encodeURIComponent(ACTIVE_FILE), undefined);
    if (data === null) {
        textarea.placeholder = t('m111');
        return;
    }
    textarea.placeholder = '';
    textarea.value = data.content || '';
    if (!data.content) {
        textarea.placeholder = t('m098');
    }
    await updateDiffPreview({ syncLeft: true }); // ghost rows aligned to the live file
}

async function updateDiffPreview(opts) {
    opts = opts || {};
    const textarea = document.getElementById('active-config');
    if (!textarea) return null;
    const data = await safeFetch('/api/config/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: ACTIVE_FILE, content: textarea.value })
    });
    if (data === null) return null;
    if (opts.syncLeft) {
        textarea.placeholder = '';
        textarea.value = data.editor_content || '';
        if (!data.editor_content) {
            textarea.placeholder = t('m098');
        }
    }
    previewRows = data.rows || [];
    resizeEditorPanes();
    const view = document.getElementById('editor-rows');
    /* The editor's rows are never capped: the pane is aligned to the textarea
       by index, so cutting the tail would shift nothing but would also leave
       the operator unable to see the end of their own file. */
    if (view) renderRowView(view, previewRows, 'new');
    updateDiffCounter(previewRows);
    applyPreviewSyntax(data);
    return data;
}

/* Highlight syntax errors from the preview: on an invalid merged config the
   editor snaps to the aligned editor_content so merged (staged) line numbers
   map 1:1 to visible rows, then error rows are marked in red.

   Rows arrive already filtered by the backend: the parse covers the whole
   bundle, so a line number from core.conf/export.conf/routes.conf
   is not mapped into this editor. Those errors show up as text only — see
   ConfigManager._editor_error_rows(). */
function applyPreviewSyntax(data) {
    const msg = document.getElementById('syntax-msg');
    const syntax = data && data.syntax;
    if (msg) msg.textContent = '';
    clearErrorOverlay();
    if (!syntax || syntax.valid) return;
    if (msg && syntax.errors && syntax.errors.length) {
        /* Show the error text itself: a bundle-level error carries no editor
           row, so the message is the only place the operator can see it. */
        msg.textContent = syntax.errors.join('; ');
    }
    const textarea = document.getElementById('active-config');
    if (!textarea) return;
    if (!data.syntax_error_rows || !data.syntax_error_rows.length) return;
    if (!data.editor_content || textarea.value !== data.editor_content) {
        textarea.value = data.editor_content || textarea.value;
        resizeEditorPanes();
    }
    renderErrorOverlay(data.syntax_error_rows, syntax.errors || []);
}

/* 3s countdown re-armed on every keystroke; one preview request at 0. */
const DiffTimer = {
    seconds: 0,
    intervalId: null,

    arm() {
        const el = document.getElementById('diff-timer');
        clearInterval(this.intervalId);
        this.seconds = 3;
        this.render(el);
        this.intervalId = setInterval(() => {
            this.seconds -= 1;
            if (this.seconds <= 0) {
                clearInterval(this.intervalId);
                this.intervalId = null;
                this.fire();
                return;
            }
            this.render(el);
        }, 1000);
    },

    reset() {
        clearInterval(this.intervalId);
        this.intervalId = null;
        this.seconds = 0;
        this.render(document.getElementById('diff-timer'));
    },

    async fire() {
        await updateDiffPreview();
        this.reset();
    },

    render(el) {
        if (!el) return;
        if (this.intervalId === null && this.seconds <= 0) {
            el.classList.remove('active');
            el.textContent = t('m052');
            return;
        }
        el.classList.add('active');
        el.textContent = t('m057') + this.seconds + t('m094');
    }
};

/* Proportional scroll sync removed: one shared .diff-scroll scrolls both panes. */

function setApplyMsg(msgEl, text, kind) {
    msgEl.textContent = text;
    msgEl.className = 'form-msg' + (kind ? ' ' + kind : '');
}

async function applyConfigChanges() {
    const btn = document.getElementById('apply-config-btn');
    const msg = document.getElementById('config-apply-msg');
    const textarea = document.getElementById('active-config');
    if (!btn || !msg || !textarea) return;
    btn.disabled = true;
    setApplyMsg(msg, t('m068'), '');
    try {
        const res = await fetch(API_BASE + '/api/config/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filename: ACTIVE_FILE,
                user_id: getUser(),
                content: textarea.value
            })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            setApplyMsg(msg, data.message || data.error || t('m040'), 'error');
            if (data.editor_content) {
                textarea.value = data.editor_content;
                resizeEditorPanes();
            }
            DiffTimer.reset();
            await updateDiffPreview(); // realigned editor + diff overlay
            const rows = data.error_rows || data.error_lines || [];
            if (rows.length) renderErrorOverlay(rows, data.errors || []);
            return; // textarea keeps the user's edits (aligned view)
        }
        if (data.status === 'success' || data.status === 'success_with_warning') {
            textarea.value = data.live_content || textarea.value;
            DiffTimer.reset();
            await updateDiffPreview(); // recalculated diff after apply is empty
            await loadConfigOverview();
            await loadConfigStatus();
            await loadAudit();
            await refreshHierarchy();  // pending/applied badges
            if (data.reload_warning) {
                setApplyMsg(msg, t('m110') + (data.reload_detail || t('m134')), 'warning');
            } else {
                setApplyMsg(msg, t('m127') + (data.applied_networks || []).length + t('m160'), 'success');
            }
        } else {
            setApplyMsg(msg, data.message || t('m040'), 'error');
        }
    } finally {
        btn.disabled = false;
    }
}

/* Section view ids, so expanding a section knows what to fetch. */
const SECTION_VIEWS = {
    manager: { viewId: 'manager-conf-rows', noteId: 'manager-note' },
    core: { viewId: 'core-rows', noteId: 'core-note' },
};

function wireConfigSections() {
    document.querySelectorAll('.config-section-toggle').forEach((button) => {
        button.addEventListener('click', () => {
            const section = button.closest('.config-section');
            if (!section) return;
            /* Don't start a fetch while the tab is hidden: the operator gets
               the rows on the first expand instead of a half-drawn panel. */
            if (section.closest('[hidden]')) return;
            const expanded = section.classList.toggle('expanded');
            button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
            if (!expanded) return;
            const name = section.dataset.section;
            if (name === 'networks') {
                ensureEditorLoaded(section).then(() => resizeEditorPanes());
                return;
            }
            const view = SECTION_VIEWS[name];
            if (view) openFileSection(section, view.viewId, view.noteId);
        });
    });
}

function wireConfigEditor() {
    wireConfigSections();
    const active = document.getElementById('active-config');
    const applyBtn = document.getElementById('apply-config-btn');
    if (active) {
        active.addEventListener('input', () => {
            resizeEditorPanes(); // keep the shared scrollbar in sync while typing
            clearErrorOverlay(); // stale highlight until the next preview
            DiffTimer.arm();
        });
    }
    window.addEventListener('resize', () => {
        resizeEditorPanes();
        if (errorRows.length) renderErrorOverlay(errorRows, errorMessages);
    });
    if (applyBtn) {
        applyBtn.addEventListener('click', applyConfigChanges);
    }
}

const TAB_NAMES = ['networks', 'bird', 'system'];
const loadedTabs = new Set();
let currentTab = 'networks';

function tabFromHash() {
    const name = (window.location.hash || '').replace(/^#\//, '');
    return TAB_NAMES.includes(name) ? name : 'networks';
}

/* ------------------------------------------------------------------ */
/* Recompute on entry to the BIRD tab.                                  */
/*                                                                      */
/* Counters in the headers come from /api/config/overview, which the     */
/* change handlers call, so they were already current — but an open pane */
/* kept the rows it was rendered with, and a line replaced by a line     */
/* leaves the counts at +1 −1 while the rows are entirely different.     */
/* So entering the tab clears the row cache and re-renders every pane    */
/* that is currently open, from the server.                             */
/* ------------------------------------------------------------------ */

function collectExpandedConfigSections() {
    return Array.from(document.querySelectorAll('.config-section.expanded'))
        .map((section) => section.dataset.section)
        .filter(Boolean);
}

async function reopenSummaryViews() {
    for (const viewId of Array.from(summaryOpenViews)) {
        const item = document.querySelector('.config-summary-row[data-view-id="' + viewId + '"]');
        const row = overviewSummary.find((entry) => 'summary-' + entry.filename === viewId);
        if (!item || !row || item.tagName !== 'BUTTON') {
            summaryOpenViews.delete(viewId);
            continue;
        }
        await openSummaryFileView(item, {
            filename: row.filename, viewId: viewId,
            adds: row.adds, dels: row.dels, rows: row.rows,
        });
    }
}

async function refreshOpenConfigPanes() {
    for (const name of collectExpandedConfigSections()) {
        const section = document.querySelector('[data-section="' + name + '"]');
        if (!section) continue;
        if (name === 'networks') {
            if (section.dataset.loaded !== 'true') continue;
            /* No syncLeft: the textarea holds the operator's own text and must
               not be overwritten by a recompute. applyPreviewSyntax inside
               updateDiffPreview re-establishes the syntax error state from the
               response, so nothing has to be restored here. */
            await updateDiffPreview();
            continue;
        }
        const view = SECTION_VIEWS[name];
        if (view) await openFileSection(section, view.viewId, view.noteId);
    }
}

let birdRefreshInFlight = null;

function refreshBirdTab() {
    if (birdRefreshInFlight) return birdRefreshInFlight;
    birdRefreshInFlight = (async () => {
        clearFileRowsCache();
        await loadConfigOverview();
        await reopenSummaryViews();
        await refreshOpenConfigPanes();
    })().finally(() => {
        birdRefreshInFlight = null;
    });
    return birdRefreshInFlight;
}

async function loadTabContent(name) {
    if (name === 'networks') {
        await refreshCounts();
        const body = document.getElementById('hierarchy-body');
        if (body && !body.classList.contains('hidden')) {
            await refreshHierarchy();
        }
        return;
    }
    if (name === 'bird') {
        /* Counters only: every file section starts collapsed and the editor is
           fetched when the operator opens it. Returning to the tab recomputes
           what is already open — see refreshBirdTab, which activateTab calls on
           every entry, not only on the first one. */
        await loadConfigOverview();
        return;
    }
    await loadBirdPeers();
    await loadConfigStatus();
    await loadAudit();
}

async function ensureTabLoaded(name, force) {
    if (!force && loadedTabs.has(name)) return;
    await loadTabContent(name);
    loadedTabs.add(name);
}

async function activateTab(name, updateHash) {
    if (name !== 'system') PeersPoll.stop();
    if (!TAB_NAMES.includes(name)) name = 'networks';
    currentTab = name;
    document.body.dataset.view = name;
    document.querySelectorAll('.tab-button').forEach((button) => {
        const active = button.dataset.tab === name;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
        button.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('[data-tab-panel]').forEach((panel) => {
        panel.hidden = panel.dataset.tabPanel !== name;
    });
    const hash = '#/' + name;
    if (updateHash && window.location.hash !== hash) {
        window.history.pushState(null, '', hash);
    }
    if (name === 'bird') {
        window.requestAnimationFrame(resizeEditorPanes);
        /* Recompute on every entry. ensureTabLoaded is a one-shot guard keyed on
           loadedTabs, so it returns immediately on the second and later entries
           and the tab would keep whatever it was rendered with; refreshBirdTab
           re-reads the overview and re-renders the open panes each time. */
        await refreshBirdTab();
        loadedTabs.add(name);
        return;
    }
    if (name === 'system') {
        /* Recompute on every entry, same contract as the BIRD tab. The one-shot
           guard in ensureTabLoaded made every entry after the first a no-op, and
           since PeersPoll.stop() runs when leaving the tab, nothing restarted
           the timer either: the session table kept the rows of the first visit
           and the 30s poll stayed dead until the operator pressed the header
           refresh. loadBirdPeers() ends in PeersPoll.reset(), so this also brings
           the poll back. */
        await loadTabContent(name);
        loadedTabs.add(name);
        return;
    }
    await ensureTabLoaded(name, false);
}

function wireTabs() {
    const buttons = Array.from(document.querySelectorAll('.tab-button'));
    buttons.forEach((button, index) => {
        button.addEventListener('click', () => activateTab(button.dataset.tab, true));
        button.addEventListener('keydown', (event) => {
            let next = index;
            if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
            else if (event.key === 'ArrowLeft') next = (index - 1 + buttons.length) % buttons.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = buttons.length - 1;
            else return;
            event.preventDefault();
            buttons[next].focus();
            activateTab(buttons[next].dataset.tab, true);
        });
    });
}

/* ------------------------------------------------------------------ */
/* Counters refresh on demand: after a successful bulk add, after a    */
/* cascade delete of AS:COMMUNITY, and via the ⟳ button in the header. */
/* The only polling in the app is PeersPoll (BIRD peers, 30 s), and it   */
/* is scoped to the System tab — see its block below.                   */
/* ------------------------------------------------------------------ */

let REFRESH_COUNTS_PROMISE = null;
async function refreshCounts() {
    if (REFRESH_COUNTS_PROMISE) return REFRESH_COUNTS_PROMISE;
    REFRESH_COUNTS_PROMISE = (async () => {
        const data = await safeFetch('/api/networks/count', undefined);
        const peerData = await safeFetch('/api/bgp_peers/count', undefined);
        if (data !== null) {
            const counter = document.getElementById('networks-count');
            if (counter) counter.textContent = data.count;
        }
        if (peerData !== null) {
            const peerCounter = document.getElementById('bgp-peers-count');
            if (peerCounter) peerCounter.textContent = peerData.count;
        }
    })();
    try { await REFRESH_COUNTS_PROMISE; } finally { REFRESH_COUNTS_PROMISE = null; }
}

async function refreshDashboard() {
    await refreshCounts();
    if (currentTab === 'networks') {
        const body = document.getElementById('hierarchy-body');
        if (body && !body.classList.contains('hidden')) {
            await refreshHierarchy();
        }
        /* Same contract for the peer panel: refresh only what the operator can
           actually see, so a collapsed list costs nothing. The BIRD tab
           recomputes its diff on entry, so no stale counters survive here. */
        const peerBody = document.getElementById('bgp-peers-body');
        if (peerBody && !peerBody.classList.contains('hidden')) {
            await loadBgpPeers();
        }
    } else if (currentTab === 'bird') {
        /* The button means the same as returning to the tab: same recompute,
           otherwise an open pane would keep stale rows here and be refreshed
           only after a tab switch. */
        await refreshBirdTab();
    } else {
        await loadBirdPeers();
        await loadConfigStatus();
        await loadAudit();
    }
    loadedTabs.add(currentTab);
}

function wireRefreshButton() {
    const btn = document.getElementById('refresh-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        btn.classList.add('spinning');
        try {
            await refreshDashboard();
        } finally {
            btn.classList.remove('spinning');
        }
    });
}

function wireLangToggle() {
    const btn = document.getElementById('lang-toggle-btn');
    if (!btn || !window.I18n) return;
    const updateLabel = () => { btn.textContent = window.I18n.getLang().toUpperCase(); };
    updateLabel();
    btn.addEventListener('click', () => {
        const next = window.I18n.getLang() === 'ru' ? 'en' : 'ru';
        window.I18n.setLang(next);
        updateLabel();
    });
    document.addEventListener('langchange', updateLabel);
    document.addEventListener('langchange', () => {
        // Re-render JS-built panels so their labels follow the new language.
        const bulkBody = document.getElementById('bulk-body');
        if (bulkBody && !bulkBody.classList.contains('hidden')) {
            bulkBody.innerHTML = '';
            bulkBody.dataset.rendered = '';
            renderBulkPanel();
        }
        const bgpBody = document.getElementById('bgp-body');
        if (bgpBody && !bgpBody.classList.contains('hidden')) {
            bgpBody.innerHTML = '';
            bgpBody.dataset.rendered = '';
            renderBgpPeerPanel();
        }
        refreshDashboard().catch(() => {});
    });
}

/* A fragment navigation delivers both hashchange and popstate, and handleRoute is
   bound to both, so one hash change activated the tab twice — and the second
   activation is not caught by the guards the tabs carry: ensureTabLoaded
   collapses it on the Networks tab, refreshBirdTab returns the same in-flight promise on
   BIRD, but the System tab reloads on entry by design and fetched everything
   twice. Guard on the location the event carries; clicking a tab still calls
   activateTab directly, so re-clicking the open tab keeps refreshing it. */
let lastHandledLocation = null;

function handleRoute() {
    const location = window.location.pathname + window.location.search + window.location.hash;
    if (location === lastHandledLocation) return undefined;
    lastHandledLocation = location;
    return activateTab(tabFromHash(), false);
}

/* ------------------------------------------------------------------ */
/* Initialization                                                      */
/* ------------------------------------------------------------------ */

function initPanels() {
    const hierarchyToggle = document.querySelector('#panel-networks .panel-toggle');
    if (hierarchyToggle) {
        hierarchyToggle.addEventListener('click', () => {
            if (hierarchyToggle.closest('[hidden]')) return;
            toggleHierarchyPanel();
        });
    }
    const peersToggle = document.querySelector('#panel-peers .panel-toggle');
    if (peersToggle) {
        peersToggle.addEventListener('click', () => {
            /* Same as the network tree: expanding a panel of a hidden tab is a
               no-op, so the fetch waits until the tab is actually shown. */
            if (peersToggle.closest('[hidden]')) return;
            togglePeersPanel();
        });
    }
    initCollapsiblePanels(); // 'Add networks' / 'Add BGP Peer' / 'Log' / 'Config status'
    renderBulkPanel();
    renderBgpPeerPanel();
    wireHierarchySearch();
    wireTabs();
    wireRefreshButton(); // ⟳ manual refresh in the header
    wireLangToggle(); // RU/EN language switcher
    wireConfigEditor(); // config editor panes + apply
}

document.addEventListener('DOMContentLoaded', () => {
    initPanels();
    wireRosDialog();
    window.addEventListener('popstate', handleRoute);
    window.addEventListener('hashchange', handleRoute);
    const initialTab = tabFromHash();
    if (window.location.hash !== '#/' + initialTab) {
        window.history.replaceState(null, '', '#/' + initialTab);
    }
    activateTab(initialTab, false);
});
