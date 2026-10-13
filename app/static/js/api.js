/**
 * API wrapper — every page talks to the backend through this thin
 * fetch() layer.
 */
const API = {
    entity: null,
    principal: null,
    epoch: 0,
    switching: false,
    pendingWrites: 0,

    setContext(entity, principal = this.principal) {
        this.entity = entity || null;
        this.principal = principal || null;
        this.epoch += 1;
        // sessionStorage is private to this tab. Never read the old shared
        // localStorage selector when choosing a ledger for a request.
        if (entity) sessionStorage.setItem('arkcpa_context', JSON.stringify({entity, principal}));
        else sessionStorage.removeItem('arkcpa_context');
    },

    contextError() {
        const error = new Error('The company or sign-in changed. Reopen this page before continuing.');
        error.staleContext = true;
        return error;
    },

    assertCurrent(epoch) {
        if (this.switching || epoch !== this.epoch) throw this.contextError();
    },

    bindDOM(root) {
        if (!root) return;
        root.dataset.arkEntity = this.entity || '';
        root.dataset.arkPrincipal = this.principal || '';
        root.querySelectorAll('form').forEach(form => {
            form.dataset.arkEntity = this.entity || '';
            form.dataset.arkPrincipal = this.principal || '';
        });
        this.scopeLinks(root);
    },

    url(value, entity = this.entity, principal = this.principal) {
        const url = new URL(value, location.href);
        if (url.origin !== location.origin || !url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/auth/')) return value;
        if (entity) url.searchParams.set('_ark_entity', entity);
        if (principal) url.searchParams.set('_ark_principal', principal);
        return url.href;
    },

    scopeLinks(root) {
        const links = [...(root.matches?.('a[href]') ? [root] : []), ...root.querySelectorAll('a[href]')];
        links.forEach(link => {
            const bound = link.closest('[data-ark-entity]');
            link.href = this.url(link.href, bound ? bound.dataset.arkEntity : this.entity,
                bound ? bound.dataset.arkPrincipal : this.principal);
        });
    },

    async request(method, path, body = null) {
        const epoch = this.epoch;
        const opts = {
            method,
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
        };
        const companyId = localStorage.getItem('slowbooks_company');
        if (companyId) opts.headers['X-Company-Id'] = companyId;
        if (body) opts.body = JSON.stringify(body);
        let res;
        try {
            res = await fetch(`/api${path}`, opts);
        } catch (err) {
            if (err.staleContext) throw err;
            // The browser's bare "Failed to fetch" means the local server is
            // gone (desktop shell still showing the page). Say so.
            throw new Error("Ark CPA isn't responding. Check your connection and try again.");
        }
        if (res.status === 401 && window.SlowbooksAuth) {
            // Session expired, never authed, or fresh install -- let auth.js
            // re-check status and pick setup vs. login. Hardcoding promptLogin
            // here races with the DOMContentLoaded check on first install.
            window.SlowbooksAuth.promptAuth();
            throw new Error('Not authenticated');
        }
        if (res.status === 429) {
            throw new Error('Rate limit exceeded -- slow down and try again');
        }
        if (!res.ok) {
            const body = await res.json().catch(() => ({ detail: res.statusText }));
            // FastAPI HTTPException(detail=str) → string; HTTPException(detail=dict) → object;
            // pydantic validation (422) → array of {loc, msg} entries.
            // Carry both the human message and the structured body so callers can
            // introspect 409s, 422s, etc. without losing information.
            const detail = body && body.detail !== undefined ? body.detail : body;
            let message;
            if (typeof detail === 'string') {
                message = detail;
            } else if (Array.isArray(detail)) {
                // Name the field(s): "email: String should match pattern ..."
                // — a bare "Unprocessable Entity" left users guessing (#64).
                message = detail.map(d => {
                    const field = (d.loc || []).filter(p => p !== 'body').join('.');
                    return field ? `${field}: ${d.msg}` : d.msg;
                }).filter(Boolean).join('; ') || res.statusText || 'Request failed';
            } else {
                message = (detail && detail.message) || res.statusText || 'Request failed';
            }
            const err = new Error(message);
            err.status = res.status;
            err.detail = detail;
            err.body = body;
            throw err;
        }
        const result = await res.json();
        this.assertCurrent(epoch);
        return result;
    },
    // post/put accept an optional opts.query → appended as a query string.
    // Used e.g. by vendors/customers to retry with ?force=true after a
    // duplicate-warning 409.
    get(path)       { return this.request('GET', path); },
    post(path, data, opts) { return this.request('POST', path + _qs(opts), data); },
    put(path, data, opts)  { return this.request('PUT', path + _qs(opts), data); },
    del(path)       { return this.request('DELETE', path); },
};

// Legacy uploads use fetch directly and PDFs use native navigation. Apply
// the same explicit tab context to all same-origin API transports, including
// multipart bodies, without altering auth, static assets or external links.
(() => {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
        const url = new URL(input instanceof Request ? input.url : input, location.href);
        if (url.origin !== location.origin || !url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/auth/')) {
            return nativeFetch(input, init);
        }
        const request = new Request(input, init);
        API.assertCurrent(API.epoch);
        const epoch = API.epoch;
        const headers = new Headers(request.headers);
        const explicit = headers.get('X-Ark-Entity') || url.searchParams.get('_ark_entity');
        if (API.entity && explicit && explicit !== API.entity) throw API.contextError();
        if (API.entity) headers.set('X-Ark-Entity', API.entity);
        if (API.principal) headers.set('X-Ark-Principal', API.principal);
        const writing = !['GET', 'HEAD', 'OPTIONS'].includes(request.method);
        if (writing) API.pendingWrites += 1;
        try {
            const response = await nativeFetch(new Request(request, {headers}));
            API.assertCurrent(epoch);
            return response;
        } finally {
            if (writing) API.pendingWrites -= 1;
        }
    };
    const nativeOpen = window.open.bind(window);
    window.open = (url, ...args) => {
        API.assertCurrent(API.epoch);
        return nativeOpen(url ? API.url(url) : url, ...args);
    };
    for (const type of ['click', 'submit']) document.addEventListener(type, event => {
        const bound = event.target.closest?.('[data-ark-entity]');
        if (bound && (API.switching || bound.dataset.arkEntity !== (API.entity || '') || bound.dataset.arkPrincipal !== (API.principal || ''))) {
            event.preventDefault();
            event.stopImmediatePropagation();
            toast(API.contextError().message, 'error');
            return;
        }
        // Transient download anchors may be appended and clicked in the same
        // JavaScript turn, before MutationObserver can scope their URL.
        if (type === 'click') {
            const link = event.target.closest?.('a[href]');
            if (link) API.scopeLinks(link);
        }
    }, true);
    document.addEventListener('input', event => {
        const form = event.target.closest?.('form');
        if (form) form.dataset.arkDirty = 'true';
    }, true);
    const observer = new MutationObserver(records => records.forEach(record => {
        record.addedNodes.forEach(node => { if (node.nodeType === 1) API.scopeLinks(node); });
    }));
    document.addEventListener('DOMContentLoaded', () => observer.observe(document.body, {childList: true, subtree: true}));
})();

function _qs(opts) {
    if (!opts || !opts.query) return '';
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(opts.query)) {
        if (v !== undefined && v !== null) params.append(k, String(v));
    }
    const s = params.toString();
    return s ? '?' + s : '';
}
