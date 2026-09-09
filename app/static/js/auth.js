/**
 * Ark CPA — native ARK sign-in and SlowBooks local setup.
 *
 * Renders a dedicated login/setup state when the API returns 401, or when
 * /api/auth/status reports first-time setup is needed. The accounting shell
 * stays inert and hidden until the status probe confirms a session.
 *
 * Two views:
 *   - login: Authentik first, with loopback-only local recovery when allowed
 *   - setup: full first-run wizard (company info, operator name/email,
 *            defaults, password) with a "Already set up? Sign in →" link
 *
 * The initial view is decided by /api/auth/status; the user can flip
 * between them via the cross-links. Race-safe: a flurry of 401s from
 * parallel API calls can only ever paint one overlay.
 */
(function () {
    "use strict";

    const AUTH_STATUS_URL = "/api/auth/status";
    const AUTH_SETUP_URL = "/api/auth/setup";
    const AUTH_LOGIN_URL = "/api/auth/login";
    const AUTHENTIK_LOGIN_URL = "/api/auth/authentik";

    const OVERLAY_ID = "auth-overlay";
    const MIN_PASSWORD_LEN = 8;

    // ----- network ---------------------------------------------------------

    async function checkStatus() {
        try {
            const res = await fetch(AUTH_STATUS_URL, {
                credentials: "same-origin",
            });
            if (!res.ok) return { authenticated: false, setup_needed: false };
            return await res.json();
        } catch (e) {
            return { authenticated: false, setup_needed: false };
        }
    }

    // One shared startup probe prevents the auth overlay and the main SPA
    // from racing each other and firing protected dashboard calls first.
    const initialStatus = checkStatus();

    async function postJSON(url, body) {
        const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            let detail = "Request failed";
            try {
                const data = await res.json();
                detail = data.detail || detail;
            } catch (e) {}
            const err = new Error(detail);
            err.status = res.status;
            throw err;
        }
        return res.json();
    }

    // ----- shared chrome ---------------------------------------------------

    function lockApplication() {
        document.body.classList.add("auth-pending");
        ["app", "splash"].forEach(function (id) {
            const element = document.getElementById(id);
            if (!element) return;
            element.setAttribute("aria-hidden", "true");
            element.inert = true;
        });
    }

    function revealApplication() {
        document.body.classList.remove("auth-pending");
        ["app", "splash"].forEach(function (id) {
            const element = document.getElementById(id);
            if (!element) return;
            element.removeAttribute("aria-hidden");
            element.inert = false;
        });
        if (window.location.pathname === "/login") {
            window.history.replaceState(null, "", "/" + window.location.hash);
        }
    }

    function useLoginLocation() {
        if (window.location.pathname !== "/login") {
            window.history.replaceState(
                null,
                "",
                "/login" + window.location.search
            );
        }
    }

    function livingMarkHTML() {
        return (
            '<span class="ark-mark" role="img" aria-label="Noah\'s Ark, the ARK mark">' +
            '<span class="ark-mark__aura" aria-hidden="true"></span>' +
            '<img class="ark-mark__image" src="/static/brand/ark-living-mark.png" alt="">' +
            '<span class="ark-mark__water" aria-hidden="true"></span>' +
            "</span>"
        );
    }

    function buildShell(innerHTML, mode) {
        const root = document.createElement("div");
        root.id = OVERLAY_ID;
        root.className =
            "ark-auth-page" + (mode === "setup" ? " ark-auth-page--setup" : "");
        root.innerHTML =
            '<main class="ark-auth-stage">' +
            '<section class="ark-auth-shell" aria-labelledby="auth-title">' +
            '<header class="ark-auth-identity">' +
            livingMarkHTML() +
            '<p class="ark-auth-eyebrow">MAGA Energy / Accounting</p>' +
            '<h1 class="ark-auth-title" id="auth-title">Ark CPA</h1>' +
            '<p class="ark-auth-subtitle">' +
            (mode === "setup" ? "Prepare your private accounting workspace" : "Sign in to continue") +
            "</p></header>" +
            innerHTML +
            '<p class="ark-auth-provenance">Powered by SlowBooks Pro 2026 · Private operator workspace</p>' +
            "</section></main>";
        return root;
    }

    function inputAttributes(opts) {
        const attrs = [];
        if (opts.required) attrs.push('required aria-required="true"');
        if (opts.minlength) attrs.push('minlength="' + opts.minlength + '"');
        if (opts.placeholder) {
            attrs.push('placeholder="' + escapeText(opts.placeholder) + '"');
        }
        if (opts.autocomplete) {
            attrs.push('autocomplete="' + escapeText(opts.autocomplete) + '"');
        }
        if (opts.value) attrs.push('value="' + escapeText(opts.value) + '"');
        return attrs.length ? " " + attrs.join(" ") : "";
    }

    function field(id, label, opts) {
        opts = opts || {};
        const type = opts.type || "text";
        const asterisk = opts.required
            ? ' <span class="ark-auth-required" aria-hidden="true">*</span>'
            : "";
        return (
            '<div class="ark-auth-field">' +
            '<label class="ark-auth-label" for="' + id + '">' +
            label + asterisk +
            "</label>" +
            '<input class="ark-auth-input" id="' + id + '" name="' + id +
            '" type="' + type + '"' + inputAttributes(opts) + ">" +
            "</div>"
        );
    }

    function escapeText(text) {
        return String(text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function row(...cells) {
        return '<div class="ark-auth-field-row">' + cells.join("") + "</div>";
    }

    // ----- login view ------------------------------------------------------

    // Server Edition: set from /api/auth/status — when more than one user
    // exists, the login form gains a username field. Single-user installs
    // never see it.
    let multiUser = false;
    let authentikEnabled = false;
    let localPasswordLogin = true;

    function loginViewHTML() {
        const authentikButton = authentikEnabled
            ? '<a class="ark-auth-button" id="authentik-login" href="' + AUTHENTIK_LOGIN_URL + '">' +
              'Continue with Authentik</a>'
            : "";
        const divider = authentikEnabled && localPasswordLogin
            ? '<div class="ark-auth-divider">Recovery</div>'
            : "";
        const passwordFields = localPasswordLogin
            ? (multiUser
                ? field("auth-username", "Username", {
                      required: true,
                      autocomplete: "username",
                  })
                : "") +
              field("auth-password", "Password", {
                  type: "password",
                  required: true,
                  autocomplete: "current-password",
              }) +
              '<button class="ark-auth-button ark-auth-button--secondary" type="submit" id="auth-submit">Sign in locally</button>'
            : "";
        const setupLink = localPasswordLogin
            ? '<div class="ark-auth-switch">' +
              '<button class="ark-auth-link" type="button" id="auth-switch-setup">First time? Set up SlowBooks</button></div>'
            : "";
        const localAccess = localPasswordLogin
            ? (authentikEnabled
                ? '<details class="ark-auth-recovery"><summary>Local recovery access</summary>' +
                  '<div class="ark-auth-local-form">' + passwordFields + setupLink + "</div></details>"
                : '<div class="ark-auth-local-form ark-auth-local-form--direct">' +
                  passwordFields + setupLink + "</div>")
            : "";
        const authError = new URLSearchParams(window.location.search).get("auth_error");
        const errorMessages = {
            unavailable: "Authentik is temporarily unavailable. Try again shortly.",
            denied: "Authentik sign-in was cancelled.",
            invalid_state: "That sign-in expired. Start again.",
            forbidden: "This Authentik account is not approved for Ark CPA.",
            failed: "Ark CPA could not complete sign-in. Try again.",
        };
        return (
            '<div class="ark-auth-panel"><form id="auth-form" aria-label="Ark CPA sign in">' +
            '<p class="ark-auth-intro">' +
            (authentikEnabled ? "Use your ARK identity to continue." :
                (multiUser ? "Sign in to continue." : "Enter your password to continue.")) +
            "</p>" + authentikButton + divider + localAccess +
            '<p class="ark-auth-error" id="auth-error" role="alert" aria-live="assertive">' +
            escapeText(errorMessages[authError] || "") +
            "</p></form></div>"
        );
    }

    function wireLogin(overlay, onSuccess) {
        const form = overlay.querySelector("#auth-form");
        const input = overlay.querySelector("#auth-password");
        const userInput = overlay.querySelector("#auth-username");
        const errBox = overlay.querySelector("#auth-error");
        const btn = overlay.querySelector("#auth-submit");
        const switchBtn = overlay.querySelector("#auth-switch-setup");

        const authentikLink = overlay.querySelector("#authentik-login");
        if (authentikLink) {
            authentikLink.href = AUTHENTIK_LOGIN_URL + "?next=%2F";
            authentikLink.focus();
        } else if (userInput || input) {
            (userInput || input).focus();
        }

        if (switchBtn) {
            switchBtn.addEventListener("click", function () {
                renderView("setup", onSuccess);
            });
        }

        form.addEventListener("submit", async function (e) {
            e.preventDefault();
            if (!input || !btn) return;
            errBox.textContent = "";
            btn.disabled = true;
            btn.textContent = "...";
            try {
                const body = { password: input.value };
                if (userInput) body.username = userInput.value.trim();
                await postJSON(AUTH_LOGIN_URL, body);
                removeOverlay();
                if (onSuccess) onSuccess();
                else window.location.reload();
            } catch (err) {
                // 409 means no password is set yet — bounce to setup so the
                // user isn't stuck staring at "setup required" with no path.
                if (err.status === 409) {
                    errBox.textContent =
                        "No password set yet. Switching to first-time setup.";
                    setTimeout(function () {
                        renderView("setup", onSuccess);
                    }, 800);
                    return;
                }
                errBox.textContent = err.message;
                btn.disabled = false;
                btn.textContent = "Sign in locally";
            }
        });
    }

    // ----- setup view ------------------------------------------------------

    // Filled from /api/auth/status before the setup view renders.
    let existingCompany = { name: "", hasData: false };

    function setupViewHTML() {
        const existingNotice = existingCompany.hasData
            ? '<p class="ark-auth-notice">' +
              "This company file already contains books" +
              (existingCompany.name ? " for <strong>" + escapeText(existingCompany.name) + "</strong>" : "") +
              ". Setup only adds your operator password; keep the company name unless you mean to rename these books." +
              "</p>"
            : "";
        return (
            '<div class="ark-auth-panel"><form class="ark-auth-setup-form" id="auth-form" aria-label="Set up Ark CPA">' +
            '<p class="ark-auth-intro">' +
            "Just enough to get you in. You can configure everything else later." +
            "</p>" +
            existingNotice +
            row(
                field("operator_name", "Your name", { required: true }),
                field("operator_email", "Your email", {
                    type: "email",
                    required: true,
                    autocomplete: "email",
                })
            ) +
            field("company_name", "Company name", {
                required: true,
                placeholder: "My Company",
                value: existingCompany.name,
            }) +
            field("company_email", "Company email", {
                type: "email",
                placeholder: "Leave blank if same as yours",
            }) +
            field("auth-password", "Password", {
                type: "password",
                required: true,
                minlength: MIN_PASSWORD_LEN,
                autocomplete: "new-password",
                placeholder: MIN_PASSWORD_LEN + "+ characters",
            }) +
            field("auth-password-confirm", "Confirm password", {
                type: "password",
                required: true,
                minlength: MIN_PASSWORD_LEN,
                autocomplete: "new-password",
            }) +
            '<button class="ark-auth-button" type="submit" id="auth-submit">Set up and continue</button>' +
            '<p class="ark-auth-error" id="auth-error" role="alert" aria-live="assertive"></p>' +
            '<p class="ark-auth-helper">' +
            "You can add your address, phone, tax ID, payment defaults, " +
            "and integrations in Settings after you sign in." +
            "</p>" +
            '<div class="ark-auth-switch">' +
            '<button class="ark-auth-link" type="button" id="auth-switch-login">Already set up? Sign in</button>' +
            "</div></form></div>"
        );
    }

    function collectSetupPayload(overlay) {
        const ids = [
            "operator_name",
            "operator_email",
            "company_name",
            "company_email",
        ];
        const out = {
            password: overlay.querySelector("#auth-password").value,
        };
        ids.forEach(function (id) {
            const el = overlay.querySelector("#" + id);
            if (el && el.value.trim() !== "") {
                out[id] = el.value.trim();
            }
        });
        return out;
    }

    function wireSetup(overlay, onSuccess) {
        const form = overlay.querySelector("#auth-form");
        const pw = overlay.querySelector("#auth-password");
        const pw2 = overlay.querySelector("#auth-password-confirm");
        const errBox = overlay.querySelector("#auth-error");
        const btn = overlay.querySelector("#auth-submit");
        const switchBtn = overlay.querySelector("#auth-switch-login");
        const firstField = overlay.querySelector("#operator_name");

        if (firstField) firstField.focus();

        switchBtn.addEventListener("click", function () {
            renderView("login", onSuccess);
        });

        form.addEventListener("submit", async function (e) {
            e.preventDefault();
            errBox.textContent = "";

            if (pw.value.length < MIN_PASSWORD_LEN) {
                errBox.textContent =
                    "Password must be at least " + MIN_PASSWORD_LEN + " characters.";
                return;
            }
            if (pw.value !== pw2.value) {
                errBox.textContent = "Passwords do not match.";
                return;
            }

            btn.disabled = true;
            btn.textContent = "...";
            try {
                await postJSON(AUTH_SETUP_URL, collectSetupPayload(overlay));
                removeOverlay();
                if (onSuccess) onSuccess();
                else window.location.reload();
            } catch (err) {
                // 409 means the password was set between status check and
                // submit — guide the user to the login view rather than
                // leaving them stuck.
                if (err.status === 409) {
                    errBox.textContent =
                        "Setup is already complete on this server. Switching to sign in.";
                    setTimeout(function () {
                        renderView("login", onSuccess);
                    }, 800);
                    return;
                }
                errBox.textContent = err.message;
                btn.disabled = false;
                btn.textContent = "Set up and continue";
            }
        });
    }

    // ----- view orchestration ---------------------------------------------

    function removeOverlay() {
        const existing = document.getElementById(OVERLAY_ID);
        if (existing) existing.remove();
    }

    function renderView(mode, onSuccess) {
        lockApplication();
        useLoginLocation();
        removeOverlay();
        const html = mode === "setup" ? setupViewHTML() : loginViewHTML();
        const overlay = buildShell(html, mode);
        const root = document.getElementById("auth-root");
        (root || document.body).appendChild(overlay);
        if (mode === "setup") {
            wireSetup(overlay, onSuccess);
        } else {
            wireLogin(overlay, onSuccess);
        }
    }

    // Single entry point used by api.js (on 401) and the DOMContentLoaded
    // handler. Login is the canonical entry view — setup is reachable via the
    // hyperlink on the login form. Race-safe: a flurry of 401s from parallel
    // API calls can only ever paint one overlay.
    let authPromptInFlight = false;
    async function promptAuth(onSuccess) {
        if (authPromptInFlight) return;
        if (document.getElementById(OVERLAY_ID)) return;
        authPromptInFlight = true;
        try {
            const status = await initialStatus;
            multiUser = status.multi_user === true;
            authentikEnabled = status.authentik_enabled === true;
            localPasswordLogin = status.local_password_login !== false;
            existingCompany = {
                name: status.company_name || "",
                hasData: status.has_data === true,
            };
            if (status.authenticated) {
                revealApplication();
                return;
            }
            renderView("login", onSuccess);
        } finally {
            authPromptInFlight = false;
        }
    }

    // Expose globals so api.js can prompt on 401
    window.SlowbooksAuth = {
        ready: initialStatus,
        promptAuth: promptAuth,
        // Back-compat: explicit view requests still work
        promptLogin: function () {
            renderView("login");
        },
        promptSetup: function () {
            renderView("setup");
        },
    };

    document.addEventListener("DOMContentLoaded", function () {
        promptAuth();
    });
})();
