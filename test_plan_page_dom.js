/**
 * test_plan_page_dom.js — executes the Subscription Plans page JavaScript
 * and asserts the DOM the dealer actually sees.
 *
 * Server-side HTML assertions are not enough here: renderBanner() runs on
 * load and rewrites the banner and all three plan buttons. A bug there is
 * invisible to a Flask test client but obvious in a browser.
 *
 * Usage:
 *   python dump_plan_page.py            # writes no_plan.html + with_plan.html
 *   node test_plan_page_dom.js
 *
 * Requires: npm install jsdom
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

let pass = 0;
const failures = [];

function check(label, condition, detail) {
  if (condition) {
    pass++;
    console.log(`[  ok  ] ${label}`);
  } else {
    failures.push(label);
    console.log(`[ FAIL ] ${label}${detail ? `\n           → ${detail}` : ''}`);
  }
}

function load(file) {
  const html = fs.readFileSync(path.join(__dirname, file), 'utf8');
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/dealer/subscription',
    beforeParse(window) {
      // The page loads confetti + Font Awesome from CDNs; stub what it calls.
      window.confetti = () => {};
      window.fetch = () => new Promise(() => {});   // never resolves: no navigation
      window.HTMLElement.prototype.scrollIntoView = () => {};
    },
  });
  return dom.window.document;
}

console.log('\n── Page with NO active plan (onboarding) ─────────────────────');
{
  const d = load('no_plan.html');
  const bannerName = d.getElementById('bannerPlanName').textContent.trim();
  const bannerExp = d.getElementById('bannerExpiry').textContent.trim();
  const status = d.getElementById('bannerStatus').textContent.trim();
  const starter = d.getElementById('btnStarter');

  check('Banner does not claim a plan', bannerName === 'No active plan', `got "${bannerName}"`);
  check('Banner prompts the dealer to choose', /Choose a plan/i.test(bannerExp), `got "${bannerExp}"`);
  check('Status badge reads Not Active', /Not Active/i.test(status), `got "${status}"`);
  check('Status badge uses the pending style',
    d.getElementById('bannerStatus').className.includes('badge-pending'),
    d.getElementById('bannerStatus').className);

  check('Free plan button is enabled', starter && !starter.disabled);
  check('Free plan button is not labelled "Current Plan"',
    starter && !/Current Plan/i.test(starter.textContent), starter && starter.textContent.trim());
  check('Free plan button points at the dashboard',
    starter && /Dashboard/i.test(starter.textContent), starter && starter.textContent.trim());
  check('Free plan card is not marked as current',
    !d.getElementById('cardStarter').classList.contains('is-current'));

  check('Paid plans remain enabled',
    !d.getElementById('btnGrowth').disabled && !d.getElementById('btnPro').disabled);
  check('Growth keeps its price in the CTA', /999/.test(d.getElementById('btnGrowth').textContent));
  check('Pro keeps its price in the CTA', /1,999/.test(d.getElementById('btnPro').textContent));

  check('Plan Comparison is gone from the DOM',
    !/Plan Comparison/i.test(d.body.innerHTML) && d.querySelectorAll('table').length === 0,
    `${d.querySelectorAll('table').length} table(s) found`);
  check('No empty card left where the comparison was',
    ![...d.querySelectorAll('.card')].some(c => c.textContent.trim() === ''));
  check('Security footer still renders', !!d.querySelector('.security-footer'));
  check('Header action is Sign Out while the dashboard is locked',
    /Sign Out/i.test(d.getElementById('backToDmsBtn').textContent),
    d.getElementById('backToDmsBtn').textContent.trim());
}

console.log('\n── Page WITH an active Growth plan ───────────────────────────');
{
  const d = load('with_plan.html');
  const bannerName = d.getElementById('bannerPlanName').textContent.trim();
  const status = d.getElementById('bannerStatus').textContent.trim();

  check('Banner names the active plan', /Growth/i.test(bannerName), `got "${bannerName}"`);
  check('Status badge reads Active', /Active/i.test(status) && !/Not Active/i.test(status), `got "${status}"`);
  check('Growth is marked as the current plan',
    d.getElementById('btnGrowth').disabled &&
    /Current Plan/i.test(d.getElementById('btnGrowth').textContent));
  check('Growth card is highlighted',
    d.getElementById('cardGrowth').classList.contains('is-current'));
  check('Pro stays available as an upgrade',
    !d.getElementById('btnPro').disabled &&
    /Switch to|Activate/i.test(d.getElementById('btnPro').textContent));
  check('Downgrade to Free is still offered',
    !d.getElementById('btnStarter').disabled);
  check('Header action returns to the DMS',
    /Back to DMS/i.test(d.getElementById('backToDmsBtn').textContent));
  check('Plan Comparison is gone here too',
    !/Plan Comparison/i.test(d.body.innerHTML) && d.querySelectorAll('table').length === 0);
}

console.log('\n' + '='.repeat(62));
console.log(`  ${pass} passed, ${failures.length} failed`);
failures.forEach(f => console.log('   ✗ ' + f));
console.log('='.repeat(62));
process.exit(failures.length ? 1 : 0);
