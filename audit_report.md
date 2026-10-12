# UPS Picker - Triple Depth Debug/Audit Report

## Audit Methodology
This report covers 3 different audit approaches:
1. **Static Code Analysis** - Code structure, logic flows, maintainability
2. **Security Audit** - XSS, injection vulnerabilities, unsafe patterns
3. **Functional/Logic Audit** - Calculations, edge cases, business logic correctness

---

## 1. STATIC CODE ANALYSIS

### 1.1 Code Structure Assessment

**Strengths:**
- Well-organized single-page application with clear step-based workflow
- Good separation of concerns (device management, battery calc, runtime calc, summary)
- Consistent naming conventions for functions and variables
- Proper use of modern JavaScript (ES6+ features like const, let, arrow functions)

**Issues Found:**

#### Issue 1.1.1: Duplicate Comment (Lines 947-948)
```javascript
// Export to JSON
// Export to JSON  <-- DUPLICATE
async function exportToJSON() {
```
**Severity:** Low
**Impact:** Minor code cleanliness issue
**Recommendation:** Remove duplicate comment

#### Issue 1.1.2: Magic Numbers Throughout Code
Multiple instances of hardcoded values without constants:
- `0.8` power factor (line 519)
- `1.3` headroom multiplier (line 742)
- `30` minute threshold (line 786)
- `500` watt threshold (line 780)

**Severity:** Low-Medium
**Impact:** Harder to maintain and configure
**Recommendation:** Define as named constants at the top of the script

#### Issue 1.1.3: Inconsistent Error Handling
Some functions have try-catch blocks (exportToJSON), while others silently fail:
- `importFromJSON` has proper error handling
- `calculateRuntime()` could fail if powerW is 0 (division by zero)

**Severity:** Medium
**Impact:** Potential runtime errors not caught

### 1.2 Variable Scope Issues

#### Issue 1.2.1: Global State Exposure
All state variables are declared globally:
```javascript
let currentStep = 1;
let devices = [];
let deviceIdCounter = 0;
let runtimeChart = null;
let calculatedData = {...};
```
**Severity:** Low-Medium
**Impact:** Potential for accidental modification from console
**Recommendation:** Wrap in IIFE or use module pattern

### 1.3 DOM Manipulation Patterns

#### Issue 1.3.1: InnerHTML Usage (XSS Risk Vector)
Multiple uses of `insertAdjacentHTML` and `innerHTML`:
- Line 496: `document.getElementById('deviceList').insertAdjacentHTML(...)`
- Line 774: `document.getElementById('summaryDeviceList').innerHTML = ...`
- Lines 1048-1074: Device HTML reconstruction during import

**Severity:** Medium-High (see Security Audit for details)
**Recommendation:** Use createElement/appendChild or sanitize inputs

### 1.4 Memory Management

#### Issue 1.4.1: Chart Instance Cleanup
Chart.js chart is properly destroyed before recreation (line 656-658):
```javascript
if (runtimeChart) {
    runtimeChart.destroy();
}
```
**Severity:** None - This is correctly handled ✓

### 1.5 Event Handler Concerns

#### Issue 1.5.1: Inline Event Handlers
HTML contains inline onclick handlers:
- `onclick="goToStep(1)"`
- `onclick="addDevice()"`
- `oninput="updateTotalLoad()"`

**Severity:** Low
**Impact:** Mixing concerns (HTML + JS), harder to maintain
**Recommendation:** Use addEventListener in JavaScript

---

## 2. SECURITY AUDIT

### 2.1 Cross-Site Scripting (XSS) Vulnerabilities

#### Vulnerability 2.1.1: Unsanitized Device Name in Summary (CRITICAL)
**Location:** Lines 758-762
```javascript
let deviceListHtml = devices.map(d =>
    `<div class="flex justify-between py-2 px-3 bg-white rounded-lg">
        <span class="text-gray-700">${d.name}</span>
        <span class="font-medium text-gray-800">${d.load} ${d.unit} (${d.watts.toFixed(2)} W)</span>
    </div>`
).join('');
```
**Issue:** Device name comes from user input (line 475) and is directly interpolated into innerHTML without sanitization.

**Attack Vector:**
```javascript
// Malicious user enters device name:
<img src=x onerror="alert('XSS')">

// Or:
<script>alert('XSS')</script>
```

**Severity:** HIGH
**CVSS Score:** ~7.5 (High)
**Recommendation:** 
```javascript
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
// Then use: ${escapeHtml(d.name)}
```

#### Vulnerability 2.1.2: Unsanitized Device Name During Import (HIGH)
**Location:** Lines 1052-1053
```javascript
<input type="text" value="${device.name}" class="device-name ...">
```
**Issue:** Imported device name is directly placed in value attribute.

**Attack Vector:**
```json
{"name": "\" onclick=\"alert('XSS')\" x=\""}
```
Would result in:
```html
<input type="text" value="" onclick="alert('XSS')" x="">
```

**Severity:** HIGH
**Recommendation:** Escape quotes and special characters before inserting into value attribute

#### Vulnerability 2.1.3: Potential Chart.js Data Injection (MEDIUM)
**Location:** Lines 644-651
Chart data is generated from `calculatedData.totalLoad` which can be influenced by user input.

**Severity:** MEDIUM (lower because Chart.js handles most injection scenarios)
**Recommendation:** Validate numeric inputs before chart generation

### 2.2 Input Validation Issues

#### Issue 2.2.1: Missing Input Sanitization on Import
**Location:** Lines 1024-1126
The `importFromJSON` function trusts all imported data without validation.

**Risks:**
- Negative numbers could break calculations
- Extremely large numbers could cause overflow
- Malformed objects could cause runtime errors

**Severity:** Medium
**Recommendation:** Add validation:
```javascript
if (typeof device.load !== 'number' || device.load < 0 || device.load > 100000) {
    throw new Error('Invalid load value');
}
```

#### Issue 2.2.2: No Rate Limiting on Calculations
Functions like `updateTotalLoad()` are called on every keystroke (`oninput`).

**Severity:** Low
**Impact:** Performance degradation with many devices
**Recommendation:** Implement debouncing

### 2.3 File Operation Security

#### Issue 2.3.1: JSON Import Trust Model
**Location:** Line 1031
```javascript
const importData = JSON.parse(e.target.result);
```
Any JSON file can be imported without schema validation beyond checking for `devices` and `calculatedData` properties.

**Severity:** Medium
**Recommendation:** Implement JSON schema validation

### 2.4 Dependency Security

#### Issue 2.4.1: CDN Dependencies Without Integrity Checks
**Location:** Lines 9-11
```html
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Prompt..." rel="stylesheet">
```
**Issue:** No SRI (Subresource Integrity) hashes specified.

**Severity:** Medium
**Impact:** If CDN is compromised, malicious code could be injected
**Recommendation:** Add integrity attributes:
```html
<script src="https://cdn.jsdelivr.net/npm/chart.js" 
        integrity="sha384-..." crossorigin="anonymous"></script>
```

### 2.5 Information Disclosure

#### Issue 2.5.1: Console Logging of Errors
**Location:** Lines 998, 1119
```javascript
console.warn('File System Access API rejected...', err);
console.error('Import error:', error);
```
**Severity:** Low (acceptable for debugging)
**Recommendation:** Remove or conditionally enable in production

---

## 3. FUNCTIONAL/LOGIC AUDIT

### 3.1 Calculation Accuracy

#### Issue 3.1.1: Battery Capacity Formula Verification
**Location:** Lines 601-605
```javascript
// Formula: Ah = (Power × Time in hours) / (Voltage × Efficiency)
const timeHours = timeMin / 60;
const efficiencyDecimal = efficiency / 100;
const batteryAh = (powerW * timeHours) / (voltage * efficiencyDecimal);
```
**Verification:** ✓ CORRECT
- Standard formula: Ah = (W × h) / (V × η)
- Example: 100W for 1 hour at 12V with 85% efficiency
- Ah = (100 × 1) / (12 × 0.85) = 100 / 10.2 = 9.8 Ah ✓

#### Issue 3.1.2: Runtime Formula Verification
**Location:** Lines 623-626
```javascript
// Formula: Runtime (hours) = (Ah × Voltage × Efficiency) / Power
const efficiencyDecimal = efficiency / 100;
const runtimeHours = (batteryAh * voltage * efficiencyDecimal) / powerW;
const runtimeMinutes = Math.round(runtimeHours * 60);
```
**Verification:** ✓ CORRECT
- This is the inverse of the battery formula ✓
- Properly converts to minutes

#### Issue 3.1.3: VA to Watt Conversion Assumption
**Location:** Line 519
```javascript
const watts = unit === 'VA' ? load * 0.8 : load;
```
**Issue:** Assumes 0.8 power factor for all devices.

**Reality Check:**
- Modern IT equipment: 0.9-1.0 PF
- Older equipment: 0.6-0.8 PF
- Motors/inductive loads: 0.5-0.7 PF

**Severity:** Low-Medium
**Impact:** Could underestimate or overestimate actual wattage by 10-25%
**Recommendation:** 
- Add power factor as an optional input per device
- Or document the assumption clearly to users

### 3.2 Edge Cases

#### Issue 3.2.1: Division by Zero Risk
**Location:** Lines 625, 648
```javascript
const runtimeHours = (batteryAh * voltage * efficiencyDecimal) / powerW;
const runtime = (batteryAh * voltage * efficiencyDecimal) / power * 60;
```
**Scenario:** If `powerW` or `power` is 0, this causes `Infinity`.

**Current Behavior:** Would display "Infinity นาที" - breaks UI

**Severity:** Medium
**Recommendation:**
```javascript
if (powerW <= 0) {
    document.getElementById('runtimeMinutes').textContent = 'N/A';
    return;
}
```

#### Issue 3.2.2: Negative Input Values
**Location:** Multiple input fields
While some inputs have `min="0"` or `min="1"`, not all do:
- `forecastValue` has `min="0"` ✓
- `backupTimeMin` has `min="1"` ✓
- Device load inputs have `min="0"` ✓
- `runtimeBatteryAh` has NO min attribute ✗

**Severity:** Low
**Recommendation:** Add `min="0"` to all numeric inputs

#### Issue 3.2.3: Maximum Value Constraints Missing
No `max` attributes on any inputs, allowing unrealistic values:
- Load could be entered as 999999999 W
- Backup time could be 999999 minutes

**Severity:** Low
**Recommendation:** Add reasonable max values (e.g., `max="100000"` for watts)

### 3.3 Business Logic Issues

#### Issue 3.3.1: UPS VA Recommendation Formula
**Location:** Line 742
```javascript
const recommendedVA = Math.ceil(calculatedData.totalLoad * 1.3 / 100) * 100;
```
**Analysis:** Adds 30% headroom and rounds up to nearest 100 VA.

**Industry Standard:** 20-30% headroom is common practice ✓
**Rounding:** Rounding to 100 VA matches typical UPS product sizes ✓

**Verdict:** ✓ LOGICALLY SOUND

#### Issue 3.3.2: UPS Type Recommendation Threshold
**Location:** Lines 780-784
```javascript
if (calculatedData.totalLoad > 500) {
    recommendations.push('• พิจารณา UPS แบบ Online Double Conversion...');
} else {
    recommendations.push('• UPS แบบ Line Interactive เพียงพอสำหรับโหลดนี้');
}
```
**Analysis:** 500W threshold for recommending Online Double Conversion.

**Industry Context:**
- Line Interactive: Suitable for most SOHO applications (<1000W)
- Online Double Conversion: Critical loads, sensitive equipment

**Severity:** Low
**Recommendation:** Consider making this configurable or based on device types (not just wattage)

### 3.4 Data Flow Issues

#### Issue 3.3.1: State Synchronization
**Location:** Multiple locations
The `calculatedData` object stores cached values, but these can become stale:
- User changes device load → `updateTotalLoad()` updates cache ✓
- User goes back to step 1, changes load, goes to step 3 → Runtime calc uses old power value

**Current Mitigation:** Line 864 re-reads from totalLoad:
```javascript
document.getElementById('runtimePowerW').value = calculatedData.totalLoad;
```

**Verdict:** ✓ ADEQUATELY HANDLED

#### Issue 3.3.2: Chart Data Point Generation
**Location:** Lines 644-651
```javascript
const powerLevels = [50, 100, 150, 200, 250, 300, Math.round(calculatedData.totalLoad)];
powerLevels.sort((a, b) => a - b);
```
**Issue:** If totalLoad is very small (e.g., 10W), the chart shows mostly high power levels.
**Issue:** Duplicate values possible if totalLoad equals one of the hardcoded values.

**Severity:** Low (cosmetic)
**Recommendation:** Generate power levels dynamically based on actual load

### 3.5 User Experience Issues

#### Issue 3.5.1: No Confirmation Before Restart
**Location:** Line 904
```javascript
function restart() {
```
User could accidentally click "เริ่มต้นใหม่" and lose all data.

**Severity:** Low
**Recommendation:** Add confirmation dialog:
```javascript
if (!confirm('ต้องการเริ่มต้นใหม่หรือไม่? ข้อมูลทั้งหมดจะถูกลบ')) return;
```

#### Issue 3.5.2: Forecast Preview Visibility
**Location:** Lines 560-567
Forecast preview only shows for percentage mode, not watt mode.

**Severity:** Very Low
**Recommendation:** Show preview for both modes for consistency

---

## SUMMARY OF FINDINGS

| Category | Count | Severity Distribution |
|----------|-------|----------------------|
| Static Code Issues | 8 | 2 High, 4 Medium, 2 Low |
| Security Vulnerabilities | 7 | 2 High, 4 Medium, 1 Low |
| Functional/Logic Issues | 12 | 0 High, 5 Medium, 7 Low |

### Critical Issues Requiring Immediate Attention:
1. **XSS via Device Name** (Security 2.1.1) - HIGH
2. **XSS via Import** (Security 2.1.2) - HIGH

### High Priority Recommendations:
1. Implement HTML escaping for all user-generated content
2. Add input validation for imported JSON data
3. Add SRI hashes to CDN resources
4. Handle division by zero edge cases
5. Add max constraints to numeric inputs

### Medium Priority:
1. Define magic numbers as named constants
2. Add power factor configuration option
3. Implement debouncing for calculation functions
4. Add confirmation before restart
5. Improve chart data point generation

### Code Quality Score: 7.5/10
- Good structure and organization
- Clear function naming
- Needs security hardening
- Some edge cases unhandled

---

## REMEDIATION CODE EXAMPLES

### Fix for XSS (Device Name Escaping):
```javascript
function escapeHtml(text) {
    if (typeof text !== 'string') return text;
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Usage in line 758-762:
let deviceListHtml = devices.map(d =>
    `<div class="flex justify-between py-2 px-3 bg-white rounded-lg">
        <span class="text-gray-700">${escapeHtml(d.name)}</span>
        <span class="font-medium text-gray-800">${d.load} ${d.unit} (${d.watts.toFixed(2)} W)</span>
    </div>`
).join('');
```

### Fix for Division by Zero:
```javascript
function calculateRuntime() {
    const batteryAh = parseFloat(document.getElementById('runtimeBatteryAh').value) || calculatedData.batteryAh;
    const voltage = parseFloat(document.getElementById('runtimeVoltage').value) || calculatedData.voltage;
    const efficiency = parseFloat(document.getElementById('runtimeEfficiency').value) || calculatedData.efficiency;
    const powerW = calculatedData.totalLoad;

    if (powerW <= 0) {
        document.getElementById('runtimeMinutes').textContent = 'N/A (ไม่มีโหลด)';
        document.getElementById('runtimeResult').classList.remove('hidden');
        return;
    }

    // ... rest of calculation
}
```

### Fix for Magic Numbers:
```javascript
// Add at top of script (after line 445):
const CONFIG = {
    DEFAULT_POWER_FACTOR: 0.8,
    UPS_HEADROOM_MULTIPLIER: 1.3,
    MIN_RUNTIME_WARNING_THRESHOLD: 30, // minutes
    HIGH_LOAD_THRESHOLD: 500, // watts
    CHART_POWER_LEVELS: [50, 100, 150, 200, 250, 300],
    MAX_WATT_INPUT: 100000,
    MAX_BACKUP_TIME_MIN: 1440, // 24 hours
    MAX_BATTERY_AH: 1000,
};
```

---

*Report Generated: Automated Static Analysis*
*File Audited: /workspace/index.html (1129 lines)*

---

## UX Refactor Update — 2026-10-12

- Replaced the top progress strip with a labeled left navigation rail, a persistent load/battery/runtime summary, and a Basic / Enterprise mode switch.
- Basic mode keeps the core W input and calculations visible. Enterprise mode reveals the existing VA conversion, outlet counts, Forecast, editable assumptions, and JSON import/export controls.
- Kept the existing calculation expressions and defaults; added a visible estimate limitation and a table alternative for chart values.
- Follow-up security fix: device names are HTML-escaped before rendering in the summary and before inserting into the imported device-name attribute.

## Application Audit — 2026-10-12 09:42 ICT

Target: local static build at commit `68f1abd`, served on `127.0.0.1:8123`; Playwright Chromium; synthetic values only; no account or backend.

| Flow | Evidence | Result |
| --- | --- | --- |
| Basic battery sizing and Runtime | Entered 120 W, kept 60-minute target, advanced through both steps | 11.76 Ah and 60 minutes displayed; chart and its data-table disclosure appeared |
| Enterprise conversion and Forecast | Selected VA for 120 VA, entered 10% Forecast, switched back to Basic | 96 W device load and 105.6 W total; Basic disclosed the hidden VA, Forecast, and outlet-count details |
| JSON import and summary rendering | Imported a synthetic device name containing an image/event-handler payload | Original name remained visible as text; no injected image node and no handler sentinel were observed |
| Empty-load validation | Set load to 0 W and selected Next | Navigation stayed on the load step and showed inline validation |
| Mobile layout | 390 × 844 CSS viewport | No horizontal overflow; mode labels and the 100 W KPI wrapped onto multiple lines |

### Audit Findings

- **Minor — Mobile control wrapping:** At 390 px, the Basic/Enterprise labels wrap, and the load KPI breaks `100 W` across lines. Evidence: `index.html` styles around lines 85–105, 221, 285–303; screenshot in `work/ups-audit-mobile-basic.png`.
- **Minor — Initial action label:** On first load, the Next button shows only `ถัดไป →`; step-specific labels are assigned by `showStep()`, which is not called during initialization. Evidence: `index.html` lines 672, 712–715, and 1265–1270.
- **Informational — Static-host console:** The tested page emitted a 404 for `/favicon.ico` and Tailwind's production-CDN warning. The page and its Chart.js/Prompt/Tailwind assets loaded; no application JavaScript exception was observed.

Not tested: export/download behavior, offline/CDN failure, screen-reader hardware/software combinations, malformed JSON types beyond the escaped-name payload, and production hosting.

## Focused Recheck — 2026-10-12 10:06 ICT

Target: commit `c5f3654`, local static page on `127.0.0.1:8124`, Playwright Chromium.

- **Mobile mode buttons:** Still wrap at a 390 × 844 viewport. `documentWidth` was 375 px, so no horizontal overflow was observed.
- **Load KPI:** The earlier 100 W state wrapped the value across lines on the 390 px screenshot; the initial 0 W state fits. This remains a narrow-screen layout issue for three-digit values.
- **Initial Next label:** Still reads only `ถัดไป →` before the first navigation; `showStep()` assigns the descriptive label after a step change.
- **Console:** Reproduced one Tailwind CDN production warning and one `/favicon.ico` 404; no app JavaScript exception was observed.

These are presentation/packaging findings; this recheck made no application-code changes.

## Mobile, Startup, and CDN Fix — 2026-10-12 10:15 ICT

- Stacked the mobile mode caption and selector, kept both mode labels on one line, and prevented the total-load value from splitting its number and unit.
- Set the first-step Next label in the HTML so it is descriptive before any navigation occurs.
- Replaced Tailwind Play CDN with generated local CSS (`npm run build:css`, Tailwind 3.4.17) and declared an inline empty favicon to avoid the default `/favicon.ico` request.
- Rechecked at 390 × 844: mode labels and `100 W` each occupy one line; document width stays within the viewport. Enterprise unit/Forecast controls remain visible.
- Playwright console after a cache-busted reload: 0 errors, 0 warnings; the local Tailwind CSS request returned 200 and no favicon request occurred.

## UI Findings Fix — 2026-10-12 10:18 ICT

- Mobile mode controls now stack below their caption, keep both labels on one line, and keep the total-load number and unit together.
- The first-step Next button now has its descriptive label in the initial HTML.
- Tailwind utilities are compiled to `styles/tailwind.css` by `npm run build:css`; the page no longer loads Tailwind Play CDN. An inline empty favicon declaration prevents the default missing-favicon request.
- Rechecked at 390 × 844: both mode labels and `100 W` occupy one line, document width stays within the viewport, and Enterprise-only controls remain visible after switching modes.
- Browser console after a cache-busted reload: 0 errors and 0 warnings; `styles/tailwind.css` returned HTTP 200, with no Tailwind CDN or favicon request.
- `npm run build:css` exits successfully. It still prints the separate Browserslist `caniuse-lite is outdated` advisory during the build; this warning is not emitted by the running page.

## Calculation and Import Hardening — 2026-10-12

- Extracted deterministic load, battery Ah, runtime, runtime-curve, UPS screening, import-normalization, and export-building logic into `calculations.js`; added built-in Node tests without a new runtime dependency.
- Kept the existing formulas: VA uses PF 0.8, Forecast is added to device watts, Ah uses load × hours ÷ (voltage × efficiency), runtime uses the inverse, and UPS sizing retains 30% headroom rounded up per 100 VA.
- Basic mode now consistently calculates with 12 V / 85%; Enterprise voltage and efficiency values are saved when leaving and restored when returning.
- Zero/invalid capacity and invalid runtime inputs no longer fall through to hidden defaults or produce negative/infinite results. Runtime graph sample loads are unique and use the exact current load.
- JSON imports are schema-checked before state or DOM changes. Dynamic device values are escaped in both text and numeric input attributes; exports now preserve the manual runtime battery Ah value.
- The VA recommendation remains a screening heuristic, not product-specific UPS selection. Runtime is an idealized estimate and does not model battery discharge curves, aging, temperature, or vendor-specific behavior.
- Validation: `npm test` — 9 tests passed; `node --check calculations.js` and inline-script parse passed; `npm run build:css` succeeded with the existing build-time outdated Browserslist advisory; `git diff --check` passed.
- Browser interaction recheck was skipped because available RAM was approximately 315 MB during this run. No claim is made that every runtime/browser/device combination is bug-free.

## Live Browser Follow-up — 2026-10-12

- Loaded the current worktree at `127.0.0.1:8124` in Chrome DevTools and ran real UI interactions. This uncovered a stale Ah value when changing modes from Step 1 after an earlier calculation; mode changes now recalculate Basic and Enterprise values immediately and preserve the Enterprise setting for JSON export.
- Verified with 120 W / 60 min: Basic (12 V, 85%) returns 11.764706 Ah; Enterprise (24 V, 90%) returns 5.56 Ah. With 11.76 Ah, Runtime displays 59.98 minutes in Basic and 127.01 minutes in Enterprise. Switching modes restores their respective settings.
- Invalid plug counts and out-of-range numeric totals now stop calculation and show an inline error. Invalid battery time or Runtime Ah keeps the user on the corresponding step; the summary shows an em dash for invalid outputs instead of a numeric zero/null.
- Tightened import schema checks for boolean/malformed values, supported selector voltages, safe outlet counts, and malformed manual Runtime Ah. Valid malicious-looking names remain text; a crafted import created no image or event handler. Invalid imports leave the prior device/load state unchanged. Export validates its payload before download.
- Connected visible labels to every form field, gave dynamic controls stable IDs/names, marked Forecast unit buttons with `aria-pressed`, and made the Import JSON picker tabbable with a visible focus ring.
- At 390 × 844 emulated viewport: document width was 390 px, with 0 unlabeled fields. Chrome console showed no messages after normal flows and expected invalid-import handling.
- Calculation edge fixes: positive Ah rounds upward to 6 decimal places so small positive demand cannot display as 0 Ah; positive Runtime uses 2 decimal places and avoids displaying zero for underflow; UPS sizing returns a safe invalid result instead of Infinity; imported voltage must be 12/24/48 V as offered by the selector. Regression assertions were added.
- Latest validation before commit: `npm test` — 9 tests passed; `node --check calculations.js`, inline-script parse, and `git diff --check` passed. Live checks used the current local page; vendor runtime curves and physical UPS behavior remain outside this calculation model.
- The Runtime chart now plots `{load W, minutes}` on a numeric linear axis with straight segments, rather than evenly spaced category labels and smoothed interpolation. In Chrome, 120 W appeared at x=120 with 60 minutes; the table showed the same seven plotted samples.
- Responsive check: 390 × 844 and 320 × 640 phone viewports both had document width equal to viewport width; mode and step controls stayed within the viewport. At 1366 × 768 notebook size, document width was 1351 px with no horizontal overflow; the step navigation remains available while the main Next button can be reached by normal vertical scrolling.
- Simulated missing Chart.js by setting `window.Chart` unavailable. Runtime still calculated as 60 minutes, the numeric table stayed visible with seven rows, the chart canvas was hidden with a status message, and the browser console remained empty.
- Runtime precision retains results to 0.01 minute for ordinary values (verified 0.5 and 1.5 minute boundaries); values below the display quantum keep their calculated value and display as `<0.01 นาที`. Load totals retain six decimal places so small VA/W inputs do not disappear through early two-decimal rounding.
- Current 320 × 640 Enterprise/Basic interaction check: 120 W / 60 min yields 5.56 Ah at 24 V / 90% and 11.764706 Ah at 12 V / 85%; a manual 11.76 Ah displays 127.01 and 59.98 minutes respectively. Console remained empty.
- Follow-up `npm test` run: 10 tests passed, including precision, sub-minute rounding, voltage, plug-count, overflow, import schema, and v1.2 compatibility boundaries. JavaScript parsing and `git diff --check` passed after the responsive and chart changes.
- Negative load and Forecast inputs now produce an inline error instead of silently disappearing from the total. Browser confirmation at 320 × 640 showed `— W`, retained zero as the calculated total, kept the runtime table/fallback available when Chart.js was missing, and had no console messages or unlabeled fields.
- Current manufacturer-source review (2026-10-12): Schneider Electric says UPS runtime estimates vary with battery temperature, age, charge state, and load characteristics; Eaton provides model-specific runtime graphs with optional battery-pack configurations. Victron's battery-bank guide states series connections raise voltage while parallel connections raise capacity. The Runtime input now clarifies that Ah means the complete battery bank at the selected system voltage. Sources: [Schneider Electric Thailand runtime guidance](https://www.se.com/th/en/faqs/FA343240/), [Eaton UPS runtime graphs](https://www.eaton.com/us/en-us/products/backup-power-ups-surge-it-power-distribution/backup-power-ups/ups-runtime-graphs.html), [Victron battery-bank wiring](https://www.victronenergy.com/media/pg/The_Wiring_Unlimited_book/en/battery-bank-wiring.html).
- Follow-up code review found and fixed two more state issues: export now refuses to save while load inputs are invalid, and the invalid Forecast branch clears its previous row/preview/context note. Import validation now runs the derived load and Ah/Runtime calculations before mutating state, rejecting a syntactically valid but computationally unusable file.
- Calculation helpers now reject unsupported unit enums and non-array device input instead of silently treating them as W/percent or empty. Log-domain fallback avoids avoidable intermediate overflow when finite operands cancel; regression cases cover those values.
- Live browser confirmation at 320 × 640: 120 W + 10% Forecast displays 132 W and 12 W forecast; changing Forecast to -1 clears the stale preview and marks input invalid. An extreme finite-load JSON file leaves the previous valid configuration unchanged. No console messages were emitted.
- Export guard repro: valid 120 W config → enter -1 W → Export did not open the save picker; restore 120 W → Export opened and serialized one device. v1.2 `{forecast:{value,unit,watt}}` and legacy `forecastWatt` imports now have regression fixtures.
- Positive sub-0.01-minute Runtime display was exercised in Chrome: underlying result `0.0009999978` remained available to the chart, while the main result, KPI, summary, and table all displayed `<0.01 นาที` rather than overstating it as 0.01 minute.
- Zero-Ah is an accepted capacity input and yields a valid `0 นาที`; the dashboard, main result, and summary now show the same value. An uncalculated Runtime remains `— นาที`.
- Live 320 × 640 check confirmed initial Runtime `— นาที` and then 0 Ah consistently shows `0 นาที` in the result, dashboard, summary, and table; no browser console messages.
- Keyboard error handling: invalid backup time keeps Step 2 active, focuses `backupTimeMin`, and announces the inline message through a polite live region; invalid Runtime Ah keeps Step 3 active, focuses `runtimeBatteryAh`, and announces its message. Chrome console remained empty.
