/** Ark CPA control plane: entity switching, evidence, review, and compliance. */
const ArkCPA = {
    entities: [],
    activeEntity: null,
    userRole: 'readonly',

    async init(authStatus) {
        this.userRole = (authStatus.user && authStatus.user.role) || 'readonly';
        try {
            this.entities = await API.get('/ark-cpa/entities');
        } catch (err) {
            console.error('Ark CPA entity bootstrap failed', err);
            this.entities = [];
            return;
        }
        if (!this.entities.length) {
            this._renderSelector();
            if (!window.location.hash || window.location.hash === '#/') {
                window.location.hash = '#/ark-cpa/entities';
            }
            return;
        }
        const saved = localStorage.getItem('arkcpa_entity');
        const selected = this.entities.find(e => e.slug === saved)
            || this.entities.find(e => e.active)
            || this.entities[0];
        try {
            await API.post(`/ark-cpa/entities/${encodeURIComponent(selected.slug)}/activate`, {});
            localStorage.setItem('arkcpa_entity', selected.slug);
            this.activeEntity = selected;
            this.entities.forEach(e => { e.active = e.slug === selected.slug; });
            const statusEntity = document.getElementById('status-company');
            if (statusEntity) statusEntity.textContent = `Entity: ${selected.name}`;
        } catch (err) {
            console.error('Ark CPA entity activation failed', err);
        }
        this._renderSelector();
    },

    _renderSelector() {
        const select = document.getElementById('ark-entity-select');
        if (!select || !this.entities.length) {
            if (select) select.hidden = true;
            return;
        }
        select.innerHTML = this.entities.map(entity =>
            `<option value="${escapeHtml(entity.slug)}" ${entity.active ? 'selected' : ''}>${escapeHtml(entity.name)}</option>`
        ).join('');
        select.hidden = false;
        select.onchange = () => this.switchEntity(select.value);
    },

    async switchEntity(slug) {
        const entity = this.entities.find(row => row.slug === slug);
        if (!entity || (this.activeEntity && entity.slug === this.activeEntity.slug)) return;
        const select = document.getElementById('ark-entity-select');
        if (select) select.disabled = true;
        try {
            await API.post(`/ark-cpa/entities/${encodeURIComponent(slug)}/activate`, {});
            localStorage.setItem('arkcpa_entity', slug);
            window.location.reload();
        } catch (err) {
            toast(err.message, 'error');
            if (select) select.disabled = false;
        }
    },

    _entityRequired() {
        if (this.activeEntity) return null;
        return `<div class="arkcpa-empty">
            <h2>No legal entity is configured</h2>
            <p>Add the exact legal names before any documents are indexed or entries are proposed.</p>
            ${this.userRole === 'admin' ? '<button class="btn btn-primary" onclick="ArkCPA.showCreateEntity()">Add first entity</button>' : ''}
        </div>`;
    },

    async renderControlCenter() {
        const missing = this._entityRequired();
        if (missing) return missing;
        const [status, protectedRows] = await Promise.all([
            API.get('/ark-cpa/status'),
            API.get('/ark-cpa/protected-actions'),
        ]);
        const c = status.counts;
        return `<div class="arkcpa-page">
            <div class="arkcpa-hero">
                <div>
                    <div class="arkcpa-eyebrow">${escapeHtml(this.activeEntity.entity_type_label)}</div>
                    <h2>${escapeHtml(this.activeEntity.name)}</h2>
                    <p>${escapeHtml(this.activeEntity.jurisdiction)} · ${escapeHtml(this.activeEntity.currency)} · Fiscal year end ${escapeHtml(this.activeEntity.fiscal_year_end)}</p>
                </div>
                <span class="arkcpa-state ${this.activeEntity.status === 'active' ? 'is-good' : 'is-hold'}">${escapeHtml(this.activeEntity.status)}</span>
            </div>
            <div class="arkcpa-metrics">
                ${this.metric('Evidence indexed', c.evidence, '#/ark-cpa/evidence')}
                ${this.metric('Review queue', c.review_queue, '#/ark-cpa/review')}
                ${this.metric('Human gates', c.protected_actions, '#/ark-cpa/review')}
                ${this.metric('Compliance items', c.obligations, '#/ark-cpa/compliance')}
            </div>
            <div class="arkcpa-grid">
                <section class="arkcpa-panel">
                    <div class="arkcpa-panel__head"><h3>Autonomy contract</h3><span>${Math.round(status.autonomous_confidence_threshold * 100)}% minimum</span></div>
                    <ul class="arkcpa-checklist">
                        <li><span class="is-good">Ready</span> Separate Bookkeeper and Controller decisions</li>
                        <li><span class="is-good">Ready</span> Independent model-family requirement</li>
                        <li><span class="is-good">Ready</span> Balanced ledger and active-account checks</li>
                        <li><span class="is-hold">Human</span> Filings, elections, money movement, credentials, close, payroll</li>
                    </ul>
                </section>
                <section class="arkcpa-panel">
                    <div class="arkcpa-panel__head"><h3>Data boundary</h3><span>${escapeHtml(status.policy_version)}</span></div>
                    <p class="arkcpa-copy">Raw documents stay in Ark Files. Ark CPA stores hashes, provenance, decisions, and ledger results. Cloud fallback receives redacted inputs only.</p>
                    <div class="arkcpa-callout">Payroll is evidence and reminders only. No autonomous payroll calculation, remittance, or filing.</div>
                </section>
            </div>
            ${protectedRows.length ? `<section class="arkcpa-panel arkcpa-panel--wide">
                <div class="arkcpa-panel__head"><h3>Human-required actions</h3><span>${protectedRows.length}</span></div>
                ${this.protectedTable(protectedRows.slice(0, 8))}
            </section>` : ''}
        </div>`;
    },

    metric(label, value, href) {
        return `<a class="arkcpa-metric" href="${href}"><strong>${Number(value || 0).toLocaleString()}</strong><span>${escapeHtml(label)}</span></a>`;
    },

    protectedTable(rows) {
        return `<div class="table-container"><table><thead><tr><th scope="col">Action</th><th scope="col">Reason</th><th scope="col">Status</th></tr></thead><tbody>${rows.map(row =>
            `<tr><td>${escapeHtml(row.action_type.replaceAll('_', ' '))}</td><td>${escapeHtml(row.reason)}</td><td><span class="arkcpa-state is-hold">${escapeHtml(row.status)}</span></td></tr>`
        ).join('')}</tbody></table></div>`;
    },

    async renderEvidence() {
        const missing = this._entityRequired();
        if (missing) return missing;
        const rows = await API.get(`/ark-cpa/entities/${this.activeEntity.id}/evidence`);
        return `<div class="arkcpa-page">
            <div class="page-header"><div><div class="arkcpa-eyebrow">Ark Files · ${escapeHtml(this.activeEntity.name)}</div><h2>Evidence</h2></div>
                <button class="btn btn-primary" onclick="ArkCPA.scanEvidence(this)">Scan Ark Files</button></div>
            <div class="arkcpa-callout">Source: ${escapeHtml(this.activeEntity.ark_files_path)}. Files are read-only; executable, empty, unsupported, and oversized inputs are quarantined.</div>
            ${rows.length ? `<div class="table-container"><table><thead><tr><th scope="col">Document</th><th scope="col">Folder</th><th scope="col">Type</th><th scope="col">Size</th><th scope="col">Status</th><th scope="col">Actions</th></tr></thead><tbody>${rows.map(row => `<tr>
                <td title="${escapeHtml(row.source_path)}">${escapeHtml(row.source_path.split('/').pop())}</td>
                <td>${escapeHtml(row.category)}</td><td>${escapeHtml(row.mime_type)}</td><td>${this.bytes(row.size_bytes)}</td>
                <td><span class="arkcpa-state ${row.status === 'verified' ? 'is-good' : row.status === 'quarantined' ? 'is-bad' : 'is-review'}">${escapeHtml(row.status)}</span></td>
                <td>${row.status === 'indexed' ? `<button class="btn btn-sm btn-secondary" onclick="ArkCPA.verifyEvidence(${row.id})">Verify</button>` : ''}</td>
            </tr>`).join('')}</tbody></table></div>` : '<div class="arkcpa-empty"><h3>No evidence indexed</h3><p>Place documents in the entity folders in Ark Files, then scan.</p></div>'}
        </div>`;
    },

    bytes(value) {
        const size = Number(value || 0);
        if (size < 1024) return `${size} B`;
        if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
        return `${(size / (1024 * 1024)).toFixed(1)} MB`;
    },

    async scanEvidence(button) {
        button.disabled = true;
        button.textContent = 'Scanning…';
        try {
            const result = await API.post(`/ark-cpa/entities/${this.activeEntity.id}/evidence/scan`, {});
            toast(`Indexed ${result.indexed}; quarantined ${result.quarantined}.`);
            App.navigate('#/ark-cpa/evidence');
        } catch (err) {
            toast(err.message, 'error');
            button.disabled = false;
            button.textContent = 'Scan Ark Files';
        }
    },

    async verifyEvidence(id) {
        try {
            await API.put(`/ark-cpa/evidence/${id}`, {status: 'verified'});
            toast('Evidence verified');
            App.navigate('#/ark-cpa/evidence');
        } catch (err) { toast(err.message, 'error'); }
    },

    async renderReview() {
        const missing = this._entityRequired();
        if (missing) return missing;
        const [rows, protectedRows] = await Promise.all([
            API.get(`/ark-cpa/entities/${this.activeEntity.id}/candidates`),
            API.get('/ark-cpa/protected-actions'),
        ]);
        return `<div class="arkcpa-page">
            <div class="page-header"><div><div class="arkcpa-eyebrow">Independent two-agent gate</div><h2>Agent Review</h2></div></div>
            <div class="arkcpa-callout">An ordinary entry can post at any amount only when verified evidence, deterministic checks, and independent Bookkeeper and Controller approvals all pass at 98% or higher.</div>
            ${rows.length ? `<div class="arkcpa-review-list">${rows.map(row => this.candidateCard(row)).join('')}</div>` : '<div class="arkcpa-empty"><h3>No posting candidates</h3><p>Verified evidence can be proposed by the granted Ark Bookkeeper agent.</p></div>'}
            ${protectedRows.length ? `<section class="arkcpa-panel arkcpa-panel--wide"><div class="arkcpa-panel__head"><h3>Protected queue</h3><span>Never auto-executed</span></div>${this.protectedTable(protectedRows)}</section>` : ''}
        </div>`;
    },

    candidateCard(row) {
        const decisions = Object.fromEntries((row.decisions || []).map(d => [d.agent_role, d]));
        const decision = role => decisions[role]
            ? `${Math.round(decisions[role].confidence * 100)}% · ${decisions[role].decision}`
            : 'Awaiting';
        return `<article class="arkcpa-review-card">
            <div><div class="arkcpa-eyebrow">${escapeHtml(row.transaction_date)} · ${escapeHtml(row.currency)}</div><h3>${escapeHtml(row.description)}</h3><p>${row.lines.length} ledger lines · Evidence #${row.evidence_id}</p></div>
            <div class="arkcpa-agent-pair"><span>Bookkeeper <strong>${escapeHtml(decision('bookkeeper'))}</strong></span><span>Controller <strong>${escapeHtml(decision('controller'))}</strong></span></div>
            <div><span class="arkcpa-state ${row.status === 'posted' ? 'is-good' : row.status === 'blocked' ? 'is-bad' : 'is-review'}">${escapeHtml(row.status)}</span></div>
        </article>`;
    },

    async renderCompliance() {
        const missing = this._entityRequired();
        if (missing) return missing;
        const rows = await API.get(`/ark-cpa/entities/${this.activeEntity.id}/obligations`);
        return `<div class="arkcpa-page">
            <div class="page-header"><div><div class="arkcpa-eyebrow">${escapeHtml(this.activeEntity.jurisdiction)}</div><h2>Compliance</h2></div></div>
            <div class="arkcpa-callout">Ark CPA prepares evidence-backed workpapers and exports. A human remains responsible for every filing, election, and submission.</div>
            <div class="arkcpa-obligations">${rows.map(row => `<article class="arkcpa-obligation"><div><span>${escapeHtml(String(row.tax_year))}</span><h3>${escapeHtml(row.code)}</h3></div><div><strong>${escapeHtml(row.label)}</strong><p>${escapeHtml(row.authority)}</p></div><span class="arkcpa-state is-hold">${escapeHtml(row.status)}</span></article>`).join('')}</div>
        </div>`;
    },

    async renderEntities() {
        return `<div class="arkcpa-page"><div class="page-header"><div><div class="arkcpa-eyebrow">Separate database and Ark Files boundary per legal entity</div><h2>Legal Entities</h2></div>
            ${this.userRole === 'admin' ? '<button class="btn btn-primary" onclick="ArkCPA.showCreateEntity()">Add entity</button>' : ''}</div>
            <div class="arkcpa-entity-grid">${this.entities.map(entity => `<article class="arkcpa-entity-card ${entity.active ? 'is-active' : ''}">
                <div><span class="arkcpa-state ${entity.status === 'active' ? 'is-good' : 'is-hold'}">${escapeHtml(entity.status)}</span><h3>${escapeHtml(entity.name)}</h3><p>${escapeHtml(entity.entity_type_label)} · ${escapeHtml(entity.jurisdiction)}</p></div>
                <dl><dt>Currency</dt><dd>${escapeHtml(entity.currency)}</dd><dt>Files</dt><dd>${escapeHtml(entity.ark_files_path)}</dd><dt>Access</dt><dd>${escapeHtml(entity.access_role || '')}</dd></dl>
                ${entity.active ? '<strong class="arkcpa-active-label">Open ledger</strong>' : `<button class="btn btn-secondary" onclick="ArkCPA.switchEntity('${escapeHtml(entity.slug)}')">Open</button>`}
            </article>`).join('') || '<div class="arkcpa-empty"><h3>No legal entities configured</h3><p>Use exact registered legal names.</p></div>'}</div></div>`;
    },

    showCreateEntity() {
        openModal('Add legal entity', `<form onsubmit="ArkCPA.createEntity(event)">
            <div class="arkcpa-callout">Use the exact legal or registered name. Ark CPA will create a separate PostgreSQL ledger and Ark Files boundary.</div>
            <div class="form-grid">
                <div class="form-group full-width"><label>Exact legal name</label><input name="name" required maxlength="240"></div>
                <div class="form-group"><label>Entity type</label><select name="entity_type" onchange="ArkCPA.entityTypeChanged(this.form)">
                    <option value="ca_personal">Canadian personal</option><option value="ca_ab_corporation">Alberta corporation</option><option value="ca_ab_family_trust">Alberta family trust</option><option value="us_tx_llc">Texas LLC</option>
                </select></div>
                <div class="form-group"><label>Status</label><select name="status"><option value="active">Active</option><option value="dormant">Dormant</option></select></div>
                <div class="form-group"><label>Slug</label><input name="slug" required pattern="[a-z][a-z0-9-]+[a-z0-9]"></div>
                <div class="form-group"><label>Database name</label><input name="database_name" required pattern="[a-z][a-z0-9_]+[a-z0-9]"></div>
                <div class="form-group"><label>Jurisdiction</label><input name="jurisdiction" value="Canada / Alberta" required></div>
                <div class="form-group"><label>Currency</label><select name="currency"><option>CAD</option><option>USD</option></select></div>
                <div class="form-group"><label>Fiscal year-end month</label><input name="fiscal_year_end_month" type="number" min="1" max="12" value="12"></div>
                <div class="form-group"><label>Fiscal year-end day</label><input name="fiscal_year_end_day" type="number" min="1" max="31" value="31"></div>
            </div><div class="form-actions"><button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button><button class="btn btn-primary" type="submit">Create isolated ledger</button></div>
        </form>`);
    },

    entityTypeChanged(form) {
        const texas = form.entity_type.value === 'us_tx_llc';
        form.currency.value = texas ? 'USD' : 'CAD';
        form.jurisdiction.value = texas ? 'United States / Texas' : 'Canada / Alberta';
        if (texas) form.status.value = 'dormant';
    },

    async createEntity(event) {
        event.preventDefault();
        const form = event.target;
        const data = Object.fromEntries(new FormData(form).entries());
        data.fiscal_year_end_month = Number(data.fiscal_year_end_month);
        data.fiscal_year_end_day = Number(data.fiscal_year_end_day);
        const button = form.querySelector('[type="submit"]');
        button.disabled = true;
        try {
            const entity = await API.post('/ark-cpa/entities', data);
            localStorage.setItem('arkcpa_entity', entity.slug);
            window.location.reload();
        } catch (err) {
            toast(err.message, 'error');
            button.disabled = false;
        }
    },
};
