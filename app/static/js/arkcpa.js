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
        const [status, protectedRows, controller] = await Promise.all([
            API.get('/ark-cpa/status'),
            API.get('/ark-cpa/protected-actions'),
            API.get(`/ark-cpa/entities/${this.activeEntity.id}/controller-runs`),
        ]);
        const c = status.counts;
        const latest = controller.runs[0];
        const issueRows = controller.issues.filter(row => row.status === 'open').slice(0, 6);
        return `<div class="arkcpa-page">
            <div class="arkcpa-hero">
                <div>
                    <div class="arkcpa-eyebrow">Ark Controller · ${escapeHtml(this.activeEntity.entity_type_label)}</div>
                    <h2>${escapeHtml(this.activeEntity.name)}</h2>
                    <p>${escapeHtml(this.activeEntity.jurisdiction)} · ${escapeHtml(this.activeEntity.currency)} · Fiscal year end ${escapeHtml(this.activeEntity.fiscal_year_end)}</p>
                </div>
                <div class="arkcpa-hero__states">
                    <span class="arkcpa-state ${this.activeEntity.status === 'active' ? 'is-good' : 'is-hold'}">${escapeHtml(this.activeEntity.status)}</span>
                    <span class="arkcpa-state ${this.activeEntity.facts_status === 'verified' ? 'is-good' : 'is-review'}">facts ${escapeHtml(this.activeEntity.facts_status)}</span>
                    <span class="arkcpa-state ${this.activeEntity.posting_mode === 'autopost_ordinary' ? 'is-good' : 'is-hold'}">${escapeHtml(this.activeEntity.posting_mode.replaceAll('_', ' '))}</span>
                </div>
            </div>
            <div class="arkcpa-metrics">
                ${this.metric('Evidence indexed', c.evidence, '#/ark-cpa/evidence')}
                ${this.metric('Review queue', c.review_queue, '#/ark-cpa/review')}
                ${this.metric('Controller issues', c.controller_issues, '#/ark-cpa/review')}
                ${this.metric('Human gates', c.protected_actions, '#/ark-cpa/review')}
                ${this.metric('Tax changes', c.tax_changes, '#/ark-cpa/compliance')}
                ${this.metric('Workpapers', c.workpapers, '#/ark-cpa/compliance')}
            </div>
            <div class="arkcpa-grid">
                <section class="arkcpa-panel">
                    <div class="arkcpa-panel__head"><h3>Controller posture</h3><span>${latest ? `run #${latest.id}` : 'no runs yet'}</span></div>
                    <ul class="arkcpa-checklist">
                        <li><span class="is-good">Bound</span> One ledger and evidence boundary per legal entity</li>
                        <li><span class="is-good">Dual</span> Independent Bookkeeper and Controller model families</li>
                        <li><span class="is-good">${Math.round(status.autonomous_confidence_threshold * 100)}%</span> Minimum confidence plus deterministic accounting checks</li>
                        <li><span class="is-hold">Owner</span> Filings, money movement, elections, close, credentials, payroll</li>
                    </ul>
                    ${latest ? `<div class="arkcpa-runline"><span>Latest ${escapeHtml(latest.run_type)}</span><strong>${escapeHtml(latest.status)}</strong><time>${escapeHtml((latest.completed_at || latest.started_at).slice(0, 16).replace('T', ' '))}</time></div>` : ''}
                </section>
                <section class="arkcpa-panel">
                    <div class="arkcpa-panel__head"><h3>Private evidence boundary</h3><span>${escapeHtml(status.policy_version)}</span></div>
                    <p class="arkcpa-copy">Raw files stay read-only in Ark Files. Only hashes, source citations, verified facts, decision receipts, and ledger results enter the Controller workflow.</p>
                    <div class="arkcpa-callout">No filing or payment is submitted by Ark CPA. Tax source changes can only become review proposals.</div>
                </section>
            </div>
            ${issueRows.length ? `<section class="arkcpa-panel arkcpa-panel--wide">
                <div class="arkcpa-panel__head"><h3>Needs attention</h3><span>${issueRows.length} open</span></div>
                <div class="arkcpa-issue-list">${issueRows.map(row => `<div class="arkcpa-issue"><span class="arkcpa-state ${row.severity === 'critical' || row.severity === 'high' ? 'is-bad' : 'is-review'}">${escapeHtml(row.severity)}</span><div><strong>${escapeHtml(row.title)}</strong><p>${escapeHtml(row.detail)}</p></div></div>`).join('')}</div>
            </section>` : ''}
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
        return `<div class="table-container"><table><thead><tr><th scope="col">Action</th><th scope="col">Reason</th><th scope="col">Status</th><th scope="col">Owner decision</th></tr></thead><tbody>${rows.map(row =>
            `<tr><td>${escapeHtml(row.action_type.replaceAll('_', ' '))}</td><td>${escapeHtml(row.reason)}</td><td><span class="arkcpa-state ${row.status === 'approved' ? 'is-good' : row.status === 'rejected' ? 'is-bad' : 'is-hold'}">${escapeHtml(row.status)}</span></td><td>${row.can_approve ? `<div class="arkcpa-actions"><button class="btn btn-sm btn-primary" onclick="ArkCPA.decideProtected(${row.id}, 'approved')">Approve record</button><button class="btn btn-sm btn-secondary" onclick="ArkCPA.decideProtected(${row.id}, 'rejected')">Reject</button></div>` : '<span class="arkcpa-muted">Jay-only gate</span>'}</td></tr>`
        ).join('')}</tbody></table></div>`;
    },

    async renderEvidence() {
        const missing = this._entityRequired();
        if (missing) return missing;
        const rows = await API.get(`/ark-cpa/entities/${this.activeEntity.id}/evidence`);
        return `<div class="arkcpa-page">
            <div class="page-header"><div><div class="arkcpa-eyebrow">Ark Files · ${escapeHtml(this.activeEntity.name)}</div><h2>Evidence</h2></div>
                <button class="btn btn-primary" onclick="ArkCPA.scanEvidence(this)">Scan & extract</button></div>
            <div class="arkcpa-callout">Source: ${escapeHtml(this.activeEntity.ark_files_path)}. The mount is read-only. OCR text is transient; Ark CPA retains hashes, citations, and the facts you verify.</div>
            ${rows.length ? `<div class="table-container"><table><thead><tr><th scope="col">Document</th><th scope="col">Folder</th><th scope="col">Type</th><th scope="col">Facts</th><th scope="col">Status</th><th scope="col">Actions</th></tr></thead><tbody>${rows.map(row => `<tr>
                <td title="${escapeHtml(row.source_path)}"><strong>${escapeHtml(row.source_path.split('/').pop())}</strong><small class="arkcpa-hash">${row.content_hash ? escapeHtml(row.content_hash.slice(0, 12)) : 'no hash'} · ${this.bytes(row.size_bytes)}</small></td>
                <td>${escapeHtml(row.category)}</td><td>${escapeHtml(row.mime_type)}</td>
                <td>${(row.facts || []).length ? `<button class="arkcpa-fact-count" onclick="ArkCPA.showFacts(${row.id})">${row.facts.length} structured</button>` : '<span class="arkcpa-muted">None</span>'}</td>
                <td><span class="arkcpa-state ${row.status === 'verified' ? 'is-good' : row.status === 'quarantined' ? 'is-bad' : 'is-review'}">${escapeHtml(row.status)}</span></td>
                <td>${row.status === 'extracted' ? `<button class="btn btn-sm btn-secondary" onclick="ArkCPA.verifyEvidence(${row.id})">Verify facts</button>` : row.status === 'quarantined' ? `<span class="arkcpa-muted">${escapeHtml(row.quarantine_reason || 'Blocked')}</span>` : ''}</td>
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
            const counts = result.counts || result;
            toast(`Scanned ${counts.scanned || 0}; extracted ${counts.extracted || 0}; quarantined ${counts.quarantined || 0}.`);
            App.navigate('#/ark-cpa/evidence');
        } catch (err) {
            toast(err.message, 'error');
            button.disabled = false;
            button.textContent = 'Scan & extract';
        }
    },

    async verifyEvidence(id) {
        try {
            await API.put(`/ark-cpa/evidence/${id}`, {status: 'verified'});
            toast('Evidence verified');
            App.navigate('#/ark-cpa/evidence');
        } catch (err) { toast(err.message, 'error'); }
    },

    async showFacts(id) {
        try {
            const rows = await API.get(`/ark-cpa/entities/${this.activeEntity.id}/evidence`);
            const evidence = rows.find(row => row.id === id);
            if (!evidence) return;
            openModal('Verified evidence facts', `<div class="arkcpa-callout">These are structured values and source locators, not the raw document text.</div><div class="arkcpa-facts">${(evidence.facts || []).map(fact => `<div><span>${escapeHtml(fact.key)}</span><strong>${escapeHtml(JSON.stringify(fact.value))}</strong><small>${escapeHtml(fact.locator)} · ${Math.round(fact.confidence * 100)}% · ${escapeHtml(fact.status)}</small></div>`).join('')}</div>`);
        } catch (err) { toast(err.message, 'error'); }
    },

    async renderReview() {
        const missing = this._entityRequired();
        if (missing) return missing;
        const [rows, protectedRows, controller] = await Promise.all([
            API.get(`/ark-cpa/entities/${this.activeEntity.id}/candidates`),
            API.get('/ark-cpa/protected-actions'),
            API.get(`/ark-cpa/entities/${this.activeEntity.id}/controller-runs`),
        ]);
        return `<div class="arkcpa-page">
            <div class="page-header"><div><div class="arkcpa-eyebrow">Independent two-agent gate</div><h2>Review Queue</h2></div></div>
            <div class="arkcpa-callout">No fixed dollar limit is used. Autonomous posting requires a verified entity profile, three matching posted examples, a normal 20% amount band, exact accounting checks, and two independent approvals at 98% or higher.</div>
            ${rows.length ? `<div class="arkcpa-review-list">${rows.map(row => this.candidateCard(row)).join('')}</div>` : '<div class="arkcpa-empty"><h3>No posting candidates</h3><p>Verified evidence can be proposed by the granted Ark Bookkeeper agent.</p></div>'}
            ${controller.runs.length ? `<section class="arkcpa-panel arkcpa-panel--wide"><div class="arkcpa-panel__head"><h3>Controller receipts</h3><span>Append-only</span></div><div class="arkcpa-run-list">${controller.runs.slice(0, 8).map(run => `<div class="arkcpa-runline"><span>#${run.id} · ${escapeHtml(run.run_type)}</span><strong>${escapeHtml(run.status)}</strong><time>${escapeHtml((run.completed_at || run.started_at).slice(0, 16).replace('T', ' '))}</time></div>`).join('')}</div></section>` : ''}
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
            <div class="arkcpa-card-actions"><span class="arkcpa-state ${row.status === 'posted' ? 'is-good' : row.status === 'blocked' ? 'is-bad' : 'is-review'}">${escapeHtml(row.status)}</span>
                ${!decisions.controller && row.status !== 'posted' ? `<button class="btn btn-sm btn-primary" onclick="ArkCPA.runController(${row.id}, this)">Run agents</button>` : ''}
                ${decisions.controller && row.status !== 'posted' ? `<button class="btn btn-sm btn-secondary" onclick="ArkCPA.evaluateCandidate(${row.id}, this)">Recheck</button>` : ''}
                ${row.status === 'ready' ? `<button class="btn btn-sm btn-primary" onclick="ArkCPA.postCandidate(${row.id}, this)">Post entry</button>` : ''}
            </div>
        </article>`;
    },

    async runController(id, button) {
        button.disabled = true;
        button.textContent = 'Running…';
        try {
            await API.post(`/ark-cpa/candidates/${id}/controller-run`, {});
            toast('Bookkeeper and Controller review recorded');
            App.navigate('#/ark-cpa/review');
        } catch (err) { toast(err.message, 'error'); App.navigate('#/ark-cpa/review'); }
    },

    async evaluateCandidate(id, button) {
        button.disabled = true;
        try {
            await API.post(`/ark-cpa/candidates/${id}/evaluate`, {});
            toast('Deterministic checks refreshed');
            App.navigate('#/ark-cpa/review');
        } catch (err) { toast(err.message, 'error'); button.disabled = false; }
    },

    async postCandidate(id, button) {
        button.disabled = true;
        try {
            await API.post(`/ark-cpa/candidates/${id}/post`, {});
            toast('Ledger entry posted');
            App.navigate('#/ark-cpa/review');
        } catch (err) { toast(err.message, 'error'); button.disabled = false; }
    },

    async renderCompliance() {
        const missing = this._entityRequired();
        if (missing) return missing;
        const [rows, authority, workpapers] = await Promise.all([
            API.get(`/ark-cpa/entities/${this.activeEntity.id}/obligations`),
            API.get('/ark-cpa/authority-sources'),
            API.get(`/ark-cpa/entities/${this.activeEntity.id}/workpapers`),
        ]);
        this.obligations = rows;
        return `<div class="arkcpa-page">
            <div class="page-header"><div><div class="arkcpa-eyebrow">${escapeHtml(this.activeEntity.jurisdiction)}</div><h2>Tax & Compliance</h2></div>
                ${this.activeEntity.protected_approver ? '<button class="btn btn-secondary" onclick="ArkCPA.refreshAuthority(this)">Check official sources</button>' : ''}</div>
            <div class="arkcpa-callout">Ark CPA monitors official sources and prepares evidence-backed workpapers. Web changes never become tax rules automatically, and no filing or election is submitted.</div>
            <div class="arkcpa-obligations">${rows.map(row => `<article class="arkcpa-obligation"><div><span>${escapeHtml(String(row.tax_year))}</span><h3>${escapeHtml(row.code)}</h3></div><div><strong>${escapeHtml(row.label)}</strong><p>${escapeHtml(row.authority)}</p></div><span class="arkcpa-state is-hold">${escapeHtml(row.status)}</span></article>`).join('')}</div>
            <div class="arkcpa-grid">
                <section class="arkcpa-panel"><div class="arkcpa-panel__head"><h3>Authority library</h3><span>${authority.sources.length} official sources</span></div>
                    <div class="arkcpa-source-list">${authority.sources.map(source => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer"><span>${escapeHtml(source.code)}</span><strong>${escapeHtml(source.title)}</strong><small>${escapeHtml(source.jurisdiction)} · ${source.current_hash ? escapeHtml(source.current_hash.slice(0, 10)) : 'baseline pending'}</small></a>`).join('')}</div>
                </section>
                <section class="arkcpa-panel"><div class="arkcpa-panel__head"><h3>Detected changes</h3><span>Manual promotion only</span></div>
                    ${authority.proposals.length ? `<div class="arkcpa-source-list">${authority.proposals.map(row => `<div><span>${escapeHtml(row.status)}</span><strong>${escapeHtml(row.title)}</strong><small>${escapeHtml(row.code)}</small></div>`).join('')}</div>` : '<p class="arkcpa-muted">No official-source change proposals.</p>'}
                </section>
            </div>
            <section class="arkcpa-panel arkcpa-panel--wide"><div class="arkcpa-panel__head"><h3>Filing workpapers</h3><button class="btn btn-sm btn-primary" onclick="ArkCPA.showWorkpaper()">New draft package</button></div>
                ${workpapers.length ? `<div class="arkcpa-run-list">${workpapers.map(row => `<div class="arkcpa-runline"><span>${row.tax_year} · ${row.obligation_id ? `obligation #${row.obligation_id}` : 'general'}</span><strong>${escapeHtml(row.status)}</strong><code>${escapeHtml(row.package_hash.slice(0, 12))}</code></div>`).join('')}</div>` : '<p class="arkcpa-muted">No workpaper packages yet.</p>'}
            </section>
        </div>`;
    },

    async refreshAuthority(button) {
        button.disabled = true;
        button.textContent = 'Checking…';
        try {
            const result = await API.post('/ark-cpa/authority-sources/refresh', {});
            const changed = result.results.filter(row => row.status === 'change_detected').length;
            toast(`Official sources checked; ${changed} change proposal${changed === 1 ? '' : 's'}.`);
            App.navigate('#/ark-cpa/compliance');
        } catch (err) { toast(err.message, 'error'); button.disabled = false; button.textContent = 'Check official sources'; }
    },

    showWorkpaper() {
        const obligations = this.obligations || [];
        openModal('New filing workpaper', `<form onsubmit="ArkCPA.createWorkpaper(event)">
            <div class="arkcpa-callout">Creates a hash-bound review package only. Ark CPA will not file or submit it.</div>
            <div class="form-grid">
                <div class="form-group"><label>Obligation</label><select name="obligation_id" required>${obligations.map(row => `<option value="${row.id}">${escapeHtml(row.code)} · ${row.tax_year}</option>`).join('')}</select></div>
                <div class="form-group"><label>Tax year</label><input name="tax_year" type="number" min="2000" max="2200" value="${new Date().getFullYear()}" required></div>
                <div class="form-group full-width"><label>Verified evidence IDs</label><input name="evidence_ids" placeholder="12, 18, 19" required><small>Use verified evidence from this entity only.</small></div>
            </div><div class="form-actions"><button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button><button class="btn btn-primary">Create draft</button></div>
        </form>`);
    },

    async createWorkpaper(event) {
        event.preventDefault();
        const form = event.target;
        const evidenceIds = form.evidence_ids.value.split(',').map(value => Number(value.trim())).filter(Number.isInteger);
        try {
            await API.post(`/ark-cpa/entities/${this.activeEntity.id}/workpapers`, {
                obligation_id: Number(form.obligation_id.value),
                tax_year: Number(form.tax_year.value),
                evidence_ids: evidenceIds,
            });
            closeModal();
            toast('Draft workpaper package created');
            App.navigate('#/ark-cpa/compliance');
        } catch (err) { toast(err.message, 'error'); }
    },

    async renderEntities() {
        return `<div class="arkcpa-page"><div class="page-header"><div><div class="arkcpa-eyebrow">Separate database and Ark Files boundary per legal entity</div><h2>Legal Entities</h2></div>
            ${this.userRole === 'admin' ? '<button class="btn btn-primary" onclick="ArkCPA.showCreateEntity()">Add entity</button>' : ''}</div>
            <div class="arkcpa-entity-grid">${this.entities.map(entity => `<article class="arkcpa-entity-card ${entity.active ? 'is-active' : ''}">
                <div><span class="arkcpa-state ${entity.status === 'active' ? 'is-good' : 'is-hold'}">${escapeHtml(entity.status)}</span><h3>${escapeHtml(entity.name)}</h3><p>${escapeHtml(entity.entity_type_label)} · ${escapeHtml(entity.jurisdiction)}</p></div>
                <dl><dt>Currency</dt><dd>${escapeHtml(entity.currency)}</dd><dt>Files</dt><dd>${escapeHtml(entity.ark_files_path)}</dd><dt>Access</dt><dd>${escapeHtml(entity.access_role || '')}${entity.protected_approver ? ' · protected approver' : ''}</dd><dt>Facts</dt><dd>${escapeHtml(entity.facts_status)}</dd><dt>Posting</dt><dd>${escapeHtml(entity.posting_mode.replaceAll('_', ' '))}</dd></dl>
                <div class="arkcpa-actions">${entity.active ? '<strong class="arkcpa-active-label">Open ledger</strong>' : `<button class="btn btn-secondary" onclick="ArkCPA.switchEntity('${escapeHtml(entity.slug)}')">Open</button>`}${entity.protected_approver ? `<button class="btn btn-secondary" onclick="ArkCPA.showGovernance(${entity.id})">Governance</button>` : ''}</div>
            </article>`).join('') || '<div class="arkcpa-empty"><h3>No legal entities configured</h3><p>Use exact registered legal names.</p></div>'}</div></div>`;
    },

    showGovernance(entityId) {
        const entity = this.entities.find(row => row.id === entityId);
        if (!entity) return;
        openModal(`Governance · ${escapeHtml(entity.name)}`, `<form onsubmit="ArkCPA.updateGovernance(event, ${entity.id})">
            <div class="arkcpa-callout">Only the protected owner can change these controls. Autonomous posting remains limited to learned routine patterns and never includes filings or money movement.</div>
            <div class="form-grid">
                <div class="form-group"><label>Entity facts</label><select name="facts_status"><option value="incomplete" ${entity.facts_status === 'incomplete' ? 'selected' : ''}>Incomplete</option><option value="verified" ${entity.facts_status === 'verified' ? 'selected' : ''}>Verified</option><option value="hold" ${entity.facts_status === 'hold' ? 'selected' : ''}>Hold</option></select></div>
                <div class="form-group"><label>Posting mode</label><select name="posting_mode"><option value="draft_only" ${entity.posting_mode === 'draft_only' ? 'selected' : ''}>Draft only</option><option value="assisted" ${entity.posting_mode === 'assisted' ? 'selected' : ''}>Assisted</option><option value="autopost_ordinary" ${entity.posting_mode === 'autopost_ordinary' ? 'selected' : ''}>Autopost ordinary patterns</option><option value="frozen" ${entity.posting_mode === 'frozen' ? 'selected' : ''}>Frozen</option></select></div>
                <div class="form-group full-width"><label>Verified profile JSON</label><textarea name="profile" rows="7">${escapeHtml(JSON.stringify(entity.profile || {}, null, 2))}</textarea><small>Do not enter SIN, EIN, tax IDs, bank details, credentials, email, or street address.</small></div>
            </div><div class="form-actions"><button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button><button class="btn btn-primary">Save governed settings</button></div>
        </form>`);
    },

    async updateGovernance(event, entityId) {
        event.preventDefault();
        const form = event.target;
        try {
            const profile = JSON.parse(form.profile.value || '{}');
            await API.put(`/ark-cpa/entities/${entityId}/governance`, {
                posting_mode: form.posting_mode.value,
                facts_status: form.facts_status.value,
                profile,
            });
            closeModal();
            toast('Entity governance updated');
            window.location.reload();
        } catch (err) { toast(err.message, 'error'); }
    },

    async decideProtected(id, decision) {
        const verb = decision === 'approved' ? 'Approve' : 'Reject';
        openModal(`${verb} protected action`, `<form onsubmit="ArkCPA.submitProtectedDecision(event, ${id}, '${decision}')"><div class="arkcpa-callout">This records an owner decision only. It does not execute a filing, payment, election, credential change, close, payroll action, or model promotion.</div><div class="form-group"><label>Decision note</label><textarea name="note" minlength="3" maxlength="1000" required></textarea></div><div class="form-actions"><button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button><button class="btn btn-primary">${verb} record</button></div></form>`);
    },

    async submitProtectedDecision(event, id, decision) {
        event.preventDefault();
        try {
            await API.put(`/ark-cpa/protected-actions/${id}/decision`, {decision, note: event.target.note.value});
            closeModal();
            toast('Protected decision recorded; no execution performed');
            App.navigate(window.location.hash || '#/ark-cpa');
        } catch (err) { toast(err.message, 'error'); }
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
