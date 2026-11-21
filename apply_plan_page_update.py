#!/usr/bin/env python3
"""
apply_plan_page_update.py — Subscription Plans page update
══════════════════════════════════════════════════════════
Two changes to templates/dealer/subscription.html, plus a correctness fix
to the JavaScript that renders it.

1. Plan Comparison removed
   - the whole comparison card (heading, table, every feature row)
   - the CSS block that existed only to style that table
   - the card carried `mb-24`, so its bottom margin leaves with it and the
     plans grid sits directly above the security footer — no gap, no
     orphaned rule.

2. Free plan goes straight to the Dashboard
   - the Starter (₹0) CTA no longer opens the payment confirmation modal;
     one click activates and navigates.
   - activation is still persisted server-side FIRST
     (POST /dealer/subscription/activate → DealerSubscription row) and only
     then does the browser move to the dashboard, so the plan gate finds an
     active plan when it runs. No payment fields are involved.
   - the paid plans keep the modal, the demo-payment flow and the
     "Use Free For Now" trial button exactly as they were.

3. renderBanner() correctness fix  ← bug in the previous patch
   The v28 function ran on page load and unconditionally overwrote the
   server-rendered banner with `state ? state.plan : 'starter'`. For a
   dealer with no plan that printed "Starter / Free plan — no expiry /
   Active" and re-disabled the Starter button as "Current Plan" — in the
   browser only, which is why a server-side HTML assertion missed it. The
   function is now driven by HAS_ACTIVE_PLAN, injected from the database.

Usage (from the project root, after apply_saas_flow_patch.py):

    python apply_plan_page_update.py            # apply
    python apply_plan_page_update.py --check    # report only
    python apply_plan_page_update.py --revert   # restore .bak_planpage
"""

from __future__ import annotations

import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(ROOT, 'templates', 'dealer', 'subscription.html')
BACKUP_SUFFIX = '.bak_planpage'

CHECK_ONLY = '--check' in sys.argv
REVERT = '--revert' in sys.argv

_report: list[tuple[str, str]] = []


# ─────────────────────────────────────────────────────────────────────────
# 1. Plan Comparison — markup
# ─────────────────────────────────────────────────────────────────────────
# Matches the whole card, from its comment to the blank line before the
# security footer. Non-greedy and anchored on the footer comment so it can
# never swallow anything that follows.
COMPARISON_MARKUP = re.compile(
    r'\n  <!-- Plan Comparison Table -->.*?\n\n(  <!-- Security Footer -->)',
    re.S,
)
COMPARISON_MARKUP_NEW = r'\n\1'

# ─────────────────────────────────────────────────────────────────────────
# 2. Plan Comparison — CSS (verified unused elsewhere on this page)
# ─────────────────────────────────────────────────────────────────────────
COMPARISON_CSS = re.compile(
    r'\n  /\* ── Comparison Table ── \*/\n'
    r'(?:  (?:\.table-wrap|table|thead|tbody|\.check-icon|\.dash)[^\n]*\n)+',
    re.S,
)
COMPARISON_CSS_NEW = '\n'


# ─────────────────────────────────────────────────────────────────────────
# 3. renderBanner() — server-truth rewrite + free-plan wiring
# ─────────────────────────────────────────────────────────────────────────

RENDER_BANNER_OLD = """  function renderBanner() {
    var state = loadState();
    var planKey = state ? state.plan : 'starter';
    var plan = PLANS[planKey] || PLANS.starter;

    document.getElementById('bannerPlanName').textContent = plan.name;

    if (state && state.expiresAt) {
      document.getElementById('bannerExpiry').textContent = 'Valid until ' + formatDate(state.expiresAt);
    } else {
      document.getElementById('bannerExpiry').textContent = 'Free plan — no expiry';
    }

    var statusEl = document.getElementById('bannerStatus');
    if (state && state.paymentStatus === 'Pending') {
      statusEl.className = 'badge-pending';
      statusEl.innerHTML = '<i class="fas fa-clock"></i> Payment Pending';
    } else {
      statusEl.className = 'badge-success';
      statusEl.innerHTML = '<i class="fas fa-check-circle"></i> Active';
    }

    // Highlight + disable the matching plan card
    ['starter', 'growth', 'pro'].forEach(function(key) {
      var card = document.getElementById('card' + key.charAt(0).toUpperCase() + key.slice(1));
      var btn  = document.getElementById('btn' + key.charAt(0).toUpperCase() + key.slice(1));
      if (!card || !btn) return;
      if (key === planKey) {
        card.classList.add('is-current');
        btn.disabled = true;
        btn.className = 'btn btn-success btn-full';
        btn.innerHTML = '<i class="fas fa-check"></i> Current Plan';
        btn.onclick = null;
      } else {
        card.classList.remove('is-current');
        if (key !== 'starter') {
          btn.disabled = false;
          btn.className = 'btn btn-primary btn-full';
          btn.innerHTML = '<i class="fas fa-bolt"></i> Activate ' + PLANS[key].name.split(' (')[0] + ' — ₹' + PLANS[key].price.toLocaleString('en-IN') + '/mo';
          btn.onclick = (function(k) { return function() { openConfirmModal(k); }; })(key);
        }
      }
    });
"""

RENDER_BANNER_NEW = """  function renderBanner() {
    /* v29.1: driven by HAS_ACTIVE_PLAN (injected from the database), not by
       "is there any state object?". The old version defaulted planKey to
       'starter' whenever state was null, so an unsubscribed dealer saw
       "Starter · Active" and a disabled "Current Plan" button. */
    var state   = loadState();
    var hasPlan = !!HAS_ACTIVE_PLAN;
    var planKey = hasPlan ? ((state && state.plan) || 'starter') : null;
    var plan    = planKey ? (PLANS[planKey] || PLANS.starter) : null;

    document.getElementById('bannerPlanName').textContent =
      plan ? plan.name : 'No active plan';

    var expiryEl = document.getElementById('bannerExpiry');
    if (hasPlan && state && state.expiresAt) {
      expiryEl.textContent = 'Valid until ' + formatDate(state.expiresAt);
    } else if (hasPlan) {
      expiryEl.textContent = 'Active';
    } else {
      expiryEl.textContent = 'Choose a plan below to unlock your dashboard';
    }

    var statusEl = document.getElementById('bannerStatus');
    if (!hasPlan) {
      statusEl.className = 'badge-pending';
      statusEl.innerHTML = '<i class="fas fa-clock"></i> Not Active';
    } else if (state && state.paymentStatus === 'Pending') {
      statusEl.className = 'badge-pending';
      statusEl.innerHTML = '<i class="fas fa-clock"></i> Payment Pending';
    } else {
      statusEl.className = 'badge-success';
      statusEl.innerHTML = '<i class="fas fa-check-circle"></i> Active';
    }

    /* Mark the current plan; keep every other plan selectable. */
    ['starter', 'growth', 'pro'].forEach(function(key) {
      var card = document.getElementById('card' + key.charAt(0).toUpperCase() + key.slice(1));
      var btn  = document.getElementById('btn' + key.charAt(0).toUpperCase() + key.slice(1));
      if (!card || !btn) return;

      if (hasPlan && key === planKey) {
        card.classList.add('is-current');
        btn.disabled  = true;
        btn.className = 'btn btn-success btn-full';
        btn.innerHTML = '<i class="fas fa-check"></i> Current Plan';
        btn.onclick   = null;
        return;
      }

      card.classList.remove('is-current');
      btn.disabled  = false;
      btn.className = 'btn btn-primary btn-full';

      if (PLANS[key].price === 0) {
        /* Free plan: no payment step, no modal — straight to the dashboard. */
        btn.innerHTML = '<i class="fas fa-arrow-right"></i> Continue Free — Go to Dashboard';
        btn.onclick   = activateFreePlan;
      } else {
        btn.innerHTML = '<i class="fas fa-bolt"></i> ' + (hasPlan ? 'Switch to ' : 'Activate ') +
                        PLANS[key].name.split(' (')[0] + ' — ₹' +
                        PLANS[key].price.toLocaleString('en-IN') + '/mo';
        btn.onclick   = (function(k) { return function() { openConfirmModal(k); }; })(key);
      }
    });
"""


# ─────────────────────────────────────────────────────────────────────────
# 4. Free plan CTA in the server-rendered markup
# ─────────────────────────────────────────────────────────────────────────

STARTER_BTN_OLD = """        <button class="btn btn-primary btn-full" id="btnStarter" onclick="openConfirmModal('starter')">
          <i class="fas fa-play"></i> Start Free — Starter
        </button>"""

STARTER_BTN_NEW = """        <button class="btn btn-primary btn-full" id="btnStarter" onclick="activateFreePlan()">
          <i class="fas fa-arrow-right"></i> Continue Free — Go to Dashboard
        </button>"""


# ─────────────────────────────────────────────────────────────────────────
# 5. activateFreePlan()
# ─────────────────────────────────────────────────────────────────────────

INIT_ANCHOR = """  /* ── Init ──────────────────────────────────────────────────────────── */
  renderBanner();"""

FREE_PLAN_FN = """  /* ── Free plan: one click, no payment step ──────────────────────────────
     The ₹0 plan carries no charge, so it skips the confirmation modal and
     every payment field. The order matters: the server writes the
     subscription row first, and only on a confirmed success does the browser
     navigate. That way the plan gate on /dealer/dashboard always finds an
     active plan, and the state survives a refresh, a logout and a new device
     because it lives in the database rather than in this page. */
  function activateFreePlan() {
    if (_busy) return;

    var btn = document.getElementById('btnStarter');
    var originalHtml = btn ? btn.innerHTML : null;

    _busy = true;
    if (btn) {
      btn.disabled  = true;
      btn.innerHTML = '<i class="fas fa-spinner spin"></i> Activating…';
    }

    var restore = function() {
      _busy = false;
      if (btn) {
        btn.disabled = false;
        if (originalHtml) btn.innerHTML = originalHtml;
      }
    };

    fetch(ACTIVATE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ plan: 'starter', method: 'free' })
    })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(res) {
      if (res.ok && res.data && res.data.success) {
        HAS_ACTIVE_PLAN = true;
        window.location.href = res.data.redirect_url || DASHBOARD_URL;
      } else {
        restore();
        showToast('error', 'Could not activate the Free plan',
                  (res.data && res.data.error) || 'Please try again.');
      }
    })
    .catch(function() {
      restore();
      showToast('error', 'Could not reach the server',
                'Please check your connection and try again.');
    });
  }

"""


# ─────────────────────────────────────────────────────────────────────────
# runner
# ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    print('Subscription Plans page update — comparison removal + free-plan CTA')
    print('=' * 70)

    if REVERT:
        bak = TEMPLATE + BACKUP_SUFFIX
        if os.path.exists(bak):
            shutil.copy2(bak, TEMPLATE)
            os.remove(bak)
            print('  restored templates/dealer/subscription.html')
        else:
            print('  nothing to revert')
        return 0

    if not os.path.isfile(TEMPLATE):
        print(f'  ERROR: {TEMPLATE} not found. Run this from the project root.')
        return 2

    with open(TEMPLATE, 'r', encoding='utf-8') as fh:
        src = fh.read()
    out = src

    # 1. comparison markup
    if 'Plan Comparison' not in out:
        _report.append(('SKIP', 'comparison markup already removed'))
    else:
        out, n = COMPARISON_MARKUP.subn(COMPARISON_MARKUP_NEW, out, count=1)
        _report.append(('OK' if n == 1 else 'ERROR',
                        f'comparison markup removed ({n} match)'))

    # 2. comparison CSS
    if '── Comparison Table ──' not in out:
        _report.append(('SKIP', 'comparison CSS already removed'))
    else:
        out, n = COMPARISON_CSS.subn(COMPARISON_CSS_NEW, out, count=1)
        _report.append(('OK' if n == 1 else 'ERROR',
                        f'comparison CSS removed ({n} match)'))

    # 3. renderBanner rewrite
    if 'v29.1: driven by HAS_ACTIVE_PLAN' in out:
        _report.append(('SKIP', 'renderBanner already fixed'))
    elif out.count(RENDER_BANNER_OLD) == 1:
        out = out.replace(RENDER_BANNER_OLD, RENDER_BANNER_NEW, 1)
        _report.append(('OK', 'renderBanner rewritten (server truth)'))
    else:
        _report.append(('ERROR',
                        f'renderBanner anchor matched {out.count(RENDER_BANNER_OLD)}x'))

    # 4. starter CTA
    if 'onclick="activateFreePlan()"' in out:
        _report.append(('SKIP', 'free-plan CTA already wired'))
    elif out.count(STARTER_BTN_OLD) == 1:
        out = out.replace(STARTER_BTN_OLD, STARTER_BTN_NEW, 1)
        _report.append(('OK', 'free-plan CTA wired to activateFreePlan()'))
    else:
        _report.append(('ERROR',
                        f'starter button anchor matched {out.count(STARTER_BTN_OLD)}x '
                        '(run apply_saas_flow_patch.py first)'))

    # 5. activateFreePlan()
    if 'function activateFreePlan()' in out:
        _report.append(('SKIP', 'activateFreePlan() already present'))
    elif out.count(INIT_ANCHOR) == 1:
        out = out.replace(INIT_ANCHOR, FREE_PLAN_FN + INIT_ANCHOR, 1)
        _report.append(('OK', 'activateFreePlan() added'))
    else:
        _report.append(('ERROR', f'init anchor matched {out.count(INIT_ANCHOR)}x'))

    print()
    for status, note in _report:
        print(f'  [{status:<5}] {note}')

    errors = [r for r in _report if r[0] == 'ERROR']
    print()
    if CHECK_ONLY:
        print('--check: no files were modified.')
    elif out != src:
        bak = TEMPLATE + BACKUP_SUFFIX
        if not os.path.exists(bak):
            shutil.copy2(TEMPLATE, bak)
        with open(TEMPLATE, 'w', encoding='utf-8') as fh:
            fh.write(out)
        print(f'Written. Backup at templates/dealer/subscription.html{BACKUP_SUFFIX}')
    else:
        print('Nothing to do — already up to date.')

    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
