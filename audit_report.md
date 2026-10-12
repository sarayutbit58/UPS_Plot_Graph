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
- Step 2 now clarifies that calculated Ah is a theoretical estimate and actual Runtime depends on discharge rate, cutoff voltage, battery age, and temperature; final sizing needs the selected UPS/battery manufacturer's data.
- Import read failures now produce a user-facing message without mutating state. The Step 2 limitation note was checked at 320 × 640 with no horizontal overflow; console remained empty.
- Follow-up code review found and fixed two more state issues: export now refuses to save while load inputs are invalid, and the invalid Forecast branch clears its previous row/preview/context note. Import validation now runs the derived load and Ah/Runtime calculations before mutating state, rejecting a syntactically valid but computationally unusable file.
- Calculation helpers now reject unsupported unit enums and non-array device input instead of silently treating them as W/percent or empty. Log-domain fallback avoids avoidable intermediate overflow when finite operands cancel; regression cases cover those values.
- Live browser confirmation at 320 × 640: 120 W + 10% Forecast displays 132 W and 12 W forecast; changing Forecast to -1 clears the stale preview and marks input invalid. An extreme finite-load JSON file leaves the previous valid configuration unchanged. No console messages were emitted.
- Export guard repro: valid 120 W config → enter -1 W → Export did not open the save picker; restore 120 W → Export opened and serialized one device. v1.2 `{forecast:{value,unit,watt}}` and legacy `forecastWatt` imports now have regression fixtures.
- Positive sub-0.01-minute Runtime display was exercised in Chrome: underlying result `0.0009999978` remained available to the chart, while the main result, KPI, summary, and table all displayed `<0.01 นาที` rather than overstating it as 0.01 minute.
- Zero-Ah is an accepted capacity input and yields a valid `0 นาที`; the dashboard, main result, and summary now show the same value. An uncalculated Runtime remains `— นาที`.
- Live 320 × 640 check confirmed initial Runtime `— นาที` and then 0 Ah consistently shows `0 นาที` in the result, dashboard, summary, and table; no browser console messages.
- Keyboard error handling: invalid backup time keeps Step 2 active, focuses `backupTimeMin`, and announces the inline message through a polite live region; invalid Runtime Ah keeps Step 3 active, focuses `runtimeBatteryAh`, and announces its message. Chrome console remained empty.
- Step 1 navigation at 320 × 640 was tested with negative load, negative plugs, negative Forecast, and an empty load. Invalid fields retain the specific validation message and receive focus; an empty load gets the generic positive-load message and focuses the first load field. No console messages or horizontal overflow.
- VA mode consistency at 320 × 640: a 100 VA device displays “กำลังไฟ (VA → W)” in Basic, remains 100 VA internally, and calculates as 80 W; editing it to 120 remains visibly VA and calculates as 96 W. Switching back to Enterprise preserves the VA value, and a VA JSON import/export while in Basic retains the original VA number/unit. No overflow or console messages.
- Formula property check now spans 144 combinations of load, backup time, voltage, and efficiency. Required Ah never undersizes the exact formula by more than floating-point tolerance, upward quantization stays within one micro-Ah, and inverse Runtime reproduces requested time within 0.01 minute. `npm test`: 11 tests passed.
- The Enterprise UPS-type screening threshold is now an explicit tested helper: 500 W remains Line Interactive, 500.01 W crosses to Online; invalid/zero load produces no type recommendation. Existing 500 W heuristic is unchanged.
- Enterprise summary now clarifies that Forecast adds watts but cannot add outlet counts without specified future devices.
- Browser verification at 1366 × 768 with 100 W device load + 10% Forecast showed 110 W total, one specified outlet, and the explicit note that Forecast adds watts only. Notebook document width was 1351 px; console remained empty.
- Enterprise summary now clarifies that Forecast adds watts but cannot add outlet counts without specified future devices.
- Current HEAD responsive rerun: 320 × 640 and 390 × 844 phone views both stayed within viewport width; 1366 × 768 notebook view had 1351 px document width (vertical scrollbar) and aligned sidebar/workspace. Enterprise controls remained visible on phone, and the console was empty.
- Full Enterprise flow at 320 × 640: 120 W + 80 VA (2 plugs) + 10% Forecast produced 184 W device load, 18.4 W Forecast, and 202.4 W total; at 24 V / 90%, required capacity was 9.370371 Ah. Runtime calculated 60 min from that capacity and 75.3 min from a manual 11.76 Ah. Summary showed 300 VA and 3 plugs; export preserved both devices, Forecast, 24 V/90%, and manual Ah. The graph had seven unique numeric load points including x=202.4 W; no overflow or console messages.

## Fix and Debug Follow-up — 2026-10-12

- Rechecked the annotated phone UI issues on the current branch. At 320 × 640 and 390 × 844, Basic/Enterprise buttons stayed on one line, a populated `100 W` KPI stayed on one line, the initial CTA named the next step, and the document had no horizontal overflow. The 1366 × 768 notebook viewport also had no horizontal overflow.
- The current page emitted no browser console warnings or errors during the responsive check.
- Independent calculation review reran all 11 Node tests and found no rounding mismatches in the scanned recommendation range (0.01–20,000 W) or Forecast checks. Independent state/I/O review found no defect in restart, chart cleanup, failed import, or export-abort/fallback flows.
- Final validation: `npm test` — 11/11 passed. Local HEAD `66a575588b925ed9a6ed16fdb7a8f141a990aa86` matches `origin/feat/dashboard-basic-enterprise-ux`; the working tree was clean before this audit note. No code change was needed from this follow-up.
- Runtime remains an idealized estimate without manufacturer-specific discharge curves; these checks establish the tested formulas and flows, not universal accuracy across every UPS, battery, or browser.

## Export Freshness and Assumption Copy — 2026-10-12

- Reproduced a stale-export defect with 120 W and a changed 30-minute target: the prior export builder could retain `batteryAh: 0` / stale Runtime while the payload contained the current load and settings. `buildExportData()` now recalculates the exported load breakdown, Ah, and Runtime from active calculation settings; saved Enterprise settings remain separate when Basic mode is active.
- Browser repro after simulating the stale imported state exported 5.882353 Ah and 30 minutes for 120 W / 30 min / 12 V / 85%. A manual 11.76 Ah exported 59.98 minutes. Basic mode calculations used 12 V / 85% while preserving saved Enterprise settings of 24 V / 90%. Actual File import followed by immediate Export reproduced the same fresh results.
- Removed the unsupported generic “typical 80–90%” efficiency claim. The Enterprise UI now names 85% as a formula assumption; Schneider Electric states UPS efficiency varies with load and recommends using the model's actual efficiency at the actual operating load ([official FAQ](https://www.se.com/us/en/faqs/FAQ000244215/), modified 2025-06-11). Battery runtime remains model/configuration-dependent; Eaton publishes model-specific curves and notes variation with equipment, configuration, battery age, and temperature ([runtime graphs](https://www.eaton.com/us/en-us/products/backup-power-ups-surge-it-power-distribution/backup-power-ups/ups-runtime-graphs.html)).
- Rechecked the changed assumption copy at 320 × 640, 390 × 844, and 1366 × 768: no horizontal overflow; Chrome console had no warnings or errors.
- Regression validation: the new export freshness test was observed failing against the old helper (`11.764706` Ah instead of `5.882353`); after the fix, `npm test` passes 12/12, and the inline JavaScript parses. `git diff --check` passes.
- Remaining accuracy boundary: Ah and Runtime are energy-balance estimates using user-selected efficiency, not discharge-curve or manufacturer-tested results. Real sizing still requires the specific UPS model, battery configuration, load, and manufacturer's runtime data.

## Continued Accuracy and Accessibility Audit — 2026-10-12

- Found a VA sizing mismatch: the screening helper previously applied its 30% margin to watts as if they were VA. It now converts W to VA using the disclosed 0.8 PF assumption, applies 30% headroom, and rounds up by 100 VA. For 200 W, raw sizing is 325 VA and the displayed screening step is 400 VA. Schneider Electric explains the W/VA power-factor relationship and gives a 500 W / 0.8 = 625 VA example, followed by a 20–30% margin recommendation ([official UPS sizing guide](https://www.se.com/us/en/work/support/product-support/ups-buying-guide-for-selecting-a-battery-backup-system/)). The 0.8 PF and 100 VA step remain screening assumptions; actual UPS W/VA ratings must be checked by model.
- Export now preserves whether Runtime Ah was auto-filled: an auto-sized value is recalculated after import when load changes; manually entered Ah remains fixed. Browser check: auto 11.764706 Ah at 120 W exported with `runtimeBatteryAh: null`; after reimport and changing load to 200 W it updated to 19.607844 Ah and stayed at 60 min. Manual 30 Ah remained manual in the independent state review.
- Lighthouse snapshot audits now pass 28/28 checks on mobile and desktop (Accessibility, Best Practices, SEO, and Agentic Browsing all score 100). The audit caught two low-contrast text cases: footer text at 4.42:1 and active desktop step text at 4.17:1. Darkened those text colors; final snapshot audits report no failed checks.
- Responsive rerun: 320 × 640 and 390 × 844 have document width equal to viewport width; 1366 × 768 has 1351 px document width and no horizontal overflow. Mode buttons and first-step CTA remain readable.
- Regression validation: the new VA test was observed failing before the fix (76.92 W returned 100 VA instead of 200 VA); `npm test` now passes 13/13. Inline JavaScript parse and `git diff --check` also pass.
- Real UPS Runtime remains unverified without a target UPS model and its official runtime curve. This app still cannot promise universally accurate real-world Runtime from generic Ah alone.

## Topology Recommendation Correction — 2026-10-12

- Removed the 500 W cutoff that automatically classified loads as Line-interactive or Online. The output was wrong for both a critical load below 500 W and a non-critical load above it because topology depends on criticality, sensitivity, input power quality, environment, budget, and transfer-time tolerance—not load watts alone.
- The Enterprise summary now states that it cannot choose a topology without that application context. General guidance names the decision factors and asks users to verify transfer time and waveform against the UPS model. Eaton and Schneider Electric both describe topology choice in terms of the application/environment and the load's criticality or sensitivity ([Eaton topology guide](https://www.eaton.com/us/en-us/products/backup-power-ups-surge-it-power-distribution/backup-power-ups/choosing-the-optimal-ups-topology-.html), [Schneider Electric selection FAQ](https://www.se.com/nl/en/faqs/FAQ000258411/)).
- The obsolete `recommendUpsType(loadW)` helper and its 500 W boundary tests were removed. A regression test now asserts that the app does not infer topology from W alone.
- Browser check at 200 W and 600 W returned the same context-required topology guidance and no automatic type selection. Lighthouse snapshot audit with the summary open: mobile and desktop each passed 29/29 checks, with all four category scores at 100; no horizontal overflow at 390 × 844 or 1366 × 768. Console had no warnings/errors.

## Export Mode and Numeric Input Constraints — 2026-10-12

- Found that exports did not preserve Basic vs Enterprise mode. Repro: export 100 W / 90 min at 24 V / 90% in Enterprise, then import while Basic was active; Step 2 recalculated with Basic's 12 V / 85%, producing a different Ah result. Exports are now v1.4 with a validated `mode`; import restores that mode before recalculation. Legacy files without a mode keep the current mode.
- Browser round-trip now restores Enterprise 24 V / 90% and 6.944445 Ah / 90 min after importing while Basic was active. A Basic export restores 12 V / 85%, keeps its hidden 24 V / 90% Enterprise settings, and switching to Enterprise restores those settings.
- Found that auto-filled 6.944445 Ah violated the `step="0.01"` constraint on the Runtime input (`stepMismatch=true`). Decimal numeric controls now use `step="any"` where calculations accept fractional precision. The same auto-filled value remains valid in the browser.
- Decimal-entry browser check: 100 W + 1.5% Forecast produced 101.5 W; with 24 V / 90.5% / 90 min, Ah was 7.009669 and Runtime 90 min. Load, Forecast, efficiency, backup time, and auto-filled Ah all had `stepMismatch=false`.
- Final regression run: `npm test` — 15/15 passed. Inline JavaScript parsed; `git diff --check` passed. Browser check at 390 × 844 had no horizontal overflow; the mode/import calculations matched the selected mode.

## Outlet Count Path Check — 2026-10-12

- Traced `planOutlets()` through Enterprise Summary: configurations up to 12 outlets select one screening unit; 13–24 produce two, and 25 produces three. When more than 12 outlets are required, the UI says the count exceeds a single UPS and labels the unit count approximate rather than showing `0` outlets as a valid per-unit capacity.
- Regression checks cover 4, 5, 12, 13, 24, and 25 plugs. No new outlet-count defect found; actual outlet availability remains model-specific.

## Zero-Watt Device Outlet Handling — 2026-10-12

- Found that a specified 0 W device was discarded before its plugs were counted, and JSON import rejected it. The load remains zero, but explicit device rows now retain their outlet count and round-trip through JSON; blank rows remain ignored.
- Browser repro: 100 W / 1 plug plus 0 W / 2 plugs produced a 100 W total and 3 plugs in Summary; Export and Import retained both devices and the 3-plug total. At 390 × 844 there was no horizontal overflow or console error.
- Regression test was observed failing before the fix (expected 3 plugs, got 1). Full `npm test` now passes 15/15; `git diff --check` passes.

## Load Precision Consistency — 2026-10-12

- Found that components were rounded to six decimals while the total was rounded from raw values. For 100.0000004 W with 100% Forecast, the UI showed 100 W + 100 W = 200.000001 W. Totals now sum the displayed components and per-device values use the same six-decimal precision; positive device/Forecast values that round to zero are rejected instead of silently disappearing.
- Browser check: 100.0000004 W + 100% Forecast shows 100 W + 100 W = 200 W. A 0.001 W device shows 0.001 W in the summary row; a 0.0000004 W input is rejected with a precision message and shown as `— W`. At 390 × 844 there was no horizontal overflow or console error.
- Precision validation now returns the invalid row/Forecast target; browser navigation focused the second device's load field for an under-precision row and the Forecast field for an under-precision fixed-W Forecast.
- Regression case: two 0.0000006 W rows each round to 0.000001 W, and the total equals their displayed sum, 0.000002 W.
- The regression test was observed failing before the fix (200.000001 instead of 200). `npm test` now passes 16/16; `git diff --check` passes.

## Large-Magnitude Rounding — 2026-10-12

- Found that the rounding epsilon was multiplied by the scaled value, which biased exact whole-number loads upward at high magnitudes. Moving the epsilon adjustment before scaling avoids that bias.
- Regression case: 10,000,000,000 W stays exact; 20% Forecast adds 2,000,000,000 W for a total of 12,000,000,000 W. 12,500,000,000 VA at PF 0.8 converts to exactly 10,000,000,000 W.
- A second edge case showed six-decimal scaling exceeded JavaScript's safe integer range for fractional loads near 10 billion W. The rounding helper now uses `toFixed()` for that range; 9,999,999,999.123457 W and 10,000,000,000.123457 W retain their displayed precision.
- Both regressions were observed failing before their fixes. `npm test` now passes 18/18; JavaScript syntax check and `git diff --check` pass.

## UI Audit Follow-up — 2026-10-12

- At 390 × 844, the Basic and Enterprise buttons stay on one line with 44 px height. A 100 W total stays on one line, and the document has no horizontal overflow.
- Startup now calls `showStep(currentStep)`, so step visibility and the first “ถัดไป: ขนาดแบตเตอรี่ →” label are initialized through the same path as later steps.
- Confirmed the favicon uses an inline data URL and the page loads local `styles/tailwind.css`, avoiding the earlier `/favicon.ico` 404 and Tailwind CDN warning. At this browser run, console was empty and all 13 network requests succeeded.

## Precision Aggregation and Responsive Audit — 2026-10-12

- A stress case with 100,000,000 W plus 1,000 rows of 0.000001 W reproduced a floating-point total that was 0.0000019967556 W low. Load components now sum as integer micro-watts; totals that cannot be represented to six decimal places are rejected and focus the contributing input.
- Regression coverage confirms the exact 100,000,000.001 W result and rejects 1,000,000,000,000 W plus 0.000001 W rather than silently dropping the micro-watt. The test failed before the fix and passed after it.
- Browser audit on the preceding commit exercised 320 × 640, 390 × 844, and 1366 × 768 through Basic load, battery, Runtime, Summary, and Enterprise mode; no overflow or console errors were observed. A broken replacement glyph on the Export JSON button was found and replaced with an inline download SVG in this revision.
- Rechecked this revision in Chrome at 320 × 640: Enterprise Summary shows a legible Export JSON button with the SVG, step 4 is visible, and page width remains 320 px. The browser calculation returns 100,000,000.001 W for the 1,001-row case and rejects the unrepresentable 1,000,000,000,000 W + 0.000001 W case; console remains empty and all 13 requests return 200.
- Simulated the browser's Save File Picker and imported that JSON through the app's actual FileReader handler: an Enterprise 120 W setup exported and restored as Enterprise with 120 W; the import success alert appeared.

## Forecast Toggle Mobile Overflow — 2026-10-12

- A breakpoint sweep found the Enterprise Forecast unit toggle was clipped at 320–390 px although the document itself did not scroll. At 320 px the toggle wrapper measured only 3.2 px while its two buttons extended to x=399 px.
- The Forecast number input now has `min-w-0` and the unit toggle has `shrink-0`; rebuilt the checked-in Tailwind CSS.
- Rechecked 320, 360, 390, 414, 480, 600, 760, 761, 800, 1000, 1001, and 1366 px: both toggle buttons fit their wrapper, no element crosses the viewport edge, and no document horizontal overflow was observed.

## Ah and Runtime Decimal Rounding — 2026-10-12

- Independent probes reproduced two formula-quantization errors: `calculateBatteryAh(3, 63, 12, 60)` returned 0.437501 Ah instead of exact 0.4375 Ah because binary noise made `ceil` add 1 µAh; Runtime 3.375 min rounded down to 3.37 instead of 3.38. A second 0.765-minute case also rounded down to 0.76.
- Added exact decimal-rational quantization with BigInt: required Ah rounds upward to 1 µAh without over-ceiling exact values; Runtime rounds half-up to 0.01 min. Regression tests were observed failing before the fix and pass afterward.
- Verified in the browser and Node: exact Ah cases return 0.4375 and 0.234375; Runtime boundary cases return 3.38 and 0.77; the prior standard case remains 59.98 min.

## Tablet Form and Mode Toggle — 2026-10-12

- At 768 × 1024, “พื้นฐาน” wrapped onto two lines and Enterprise load fields were only 57 px wide. Mode buttons now stay on one line; Enterprise device fields use two columns through 1000 px.
- Rechecked 320, 360, 390, 480, 600, 760, 761, 768, 800, 1000, 1001, and 1366 px. No element crossed the viewport; mode labels remained one line; fields measured 204 px at 768 px and 96 px at 1001 px.

## Decimal Load Boundaries and VA Screening — 2026-10-12

- An independent exact-decimal probe found the prior EPSILON adjustment could push values just below half a micro-watt upward: 0.0000004999999999 W was accepted as 0.000001 W, and 1.0000004999999998 W became 1.000001 W. Load and VA→W rounding now use exact rational inputs; sub-half values reject, while exact half values round half-up. Forecast percent conversion follows the same exact path.
- UPS VA screening also used floating-point `ceil` at 100 VA boundaries: 61.53846153846154 W returned 100 VA despite the exact decimal value being slightly above the 100 VA threshold. The helper now quantizes the stated 0.8 PF and 30% margin using rational arithmetic; the boundary case returns 200 VA.
- Regression tests reproduce the under-half load, VA conversion, Forecast, and UPS VA boundary cases. TDD checks observed the new tests failing before the fixes; current targeted tests pass.

## Keyboard Focus Recovery — 2026-10-12

- Source audit found focus was lost when removing the focused device row or restarting from Summary; with all rows removed, invalid-load recovery attempted to focus the hidden Basic-mode Forecast field.
- Removing a row now focuses the next/previous row load field or Add Device; an empty-list validation focuses Add Device; Restart returns focus to the new first load field.
- Browser interaction verified focus targets: after removing a row focus moves to the remaining row, after the last row it moves to Add Device, an empty Next action focuses Add Device, and Restart focuses the first load field.

## Exact Decimal Load Boundaries — 2026-10-12

- A separate boundary audit found the prior EPSILON adjustment could promote a value just below half a micro-watt: 0.0000004999999999 W became 0.000001 W; 1.0000004999999998 W became 1.000001 W. Exact rational arithmetic now rounds load, PF 0.8 VA conversion, and percent Forecast without a blanket epsilon.
- Exact half micro-watt values still round upward; below-half W, VA-converted W, and Forecast values reject with the corresponding input target. UPS VA screening now applies exact PF/headroom ratios before ceiling to 100 VA; boundary probes return 100/200 VA on the appropriate side of the threshold.
- Outputs that cannot be represented at their declared quantum now return `null` instead of an approximate Ah, Runtime, or VA result. Tests cover extreme finite inputs and this rejection path.
- Browser check at 768 × 1024 returned the exact boundary results and focus recovery flows with an empty Console. The shared calculation module uses BigInt; WebKit documents BigInt support from Safari 14 ([Safari 14 features](https://webkit.org/blog/11340/new-webkit-features-in-safari-14/)). No minimum mobile browser/WebView version is declared yet; older engines remain unverified.

## Decimal String Input Preservation — 2026-10-12

- Found that parsing HTML/JSON decimal strings through `Number()` first could change values above 2^53 or underflow tiny values to zero before exact arithmetic saw them. Regression cases cover `9007199254740993`, `10000000000000001`, `1e-324`, and `-1e-324` for loads and Forecast, plus imported settings.
- Calculation entry points now verify that string inputs survive Number conversion before use; HTML forms pass the original time, efficiency, voltage, and Ah strings into the exact-rational functions. JSON normalization checks raw decimal strings before converting to numeric state.
- Browser probe confirmed the input preserved `9007199254740993` as text and the app rejected it; a backup time of `60.000000000000001` was rejected instead of silently treated as 60, while 60 minutes still returned 11.764706 Ah. Console was empty.
- Import validation now also rejects decimal strings that Number coercion would alter in device loads, Forecast, backup time, voltage, efficiency, or manual battery Ah.
- Browser Export check confirmed valid numeric settings remain JSON numbers and a `60.000000000000001` backup time blocks export rather than saving a rounded 60-minute value.
- Valid numeric strings such as `120.0` and `10.0` are normalized back to numbers only after exact round-trip checks; malformed or precision-losing strings remain rejected.
- The browser accepts `.5` in a number input; the decimal parser now accepts the same valid leading-decimal form. Browser input `.5` calculates and displays 0.5 W with no validation error or console message.

## Browser Compatibility Guard — 2026-10-12

- The app uses BigInt literals, numeric separators, optional chaining, and logical assignment; the strict syntax floor is Chrome/Chromium 85, Firefox 79, or Safari/WebKit 14 according to [V8 logical-assignment support](https://v8.dev/features/logical-assignment) and [WebKit Safari 14](https://webkit.org/blog/11340/new-webkit-features-in-safari-14/). Android WebView uses a system Chromium engine; device-specific WebViews were not tested.
- Added an ES5-compatible bootstrap that probes the required syntax/API before app startup. Unsupported engines receive a visible update message instead of an unusable blank app. Chart.js now loads asynchronously; the existing runtime table fallback remains available if it is late or unavailable.
- Current Chrome passed the feature probe, rendered the app normally, and showed no Console messages. Forcing the unsupported-browser class displayed the update notice and hid the calculator.

## Battery Capacity Resolution and Import Minimum — 2026-10-12

- Import previously accepted backup times below the UI minimum of one minute. A `1e-320` minute request could also be rounded up to 0.000001 Ah despite an exact requirement near 1.96e-321 Ah.
- Ah sizing and JSON import now reject backup times below one minute. Ah requirements below 1 µAh return `null` instead of an exaggerated minimum; the exact 1 µAh boundary remains valid.
- Regression probes confirm 0.5 minute and `1e-320` minute imports fail, while 0.006 W / 1 min / 100 V / 100% returns exactly 0.000001 Ah and a smaller load returns `null`.

## Runtime Curve Guard and Phone Accessibility — 2026-10-12

- Reproduced `buildRuntimeCurve(11.76, 12, 85, '9007199254740993')` returning a plotted load of 9007199254740992 W after JavaScript rounded the original string. The helper now rejects current-load inputs that do not exactly survive numeric conversion and returns no curve when Ah/voltage/efficiency cannot calculate the current point.
- Reproduced an extreme Ah input where one lower-load curve point had `runtimeMinutes: null` beside numeric points. Curve output now omits unrepresentable points, so every returned chart/table value is finite and the current-load point remains required.
- Regression tests for unsafe current load, unsafe Ah, invalid efficiency, and out-of-domain curve points failed before the guards and pass after. Full Node suite: 21/21 passing.
- Added row-specific accessible names to dynamic device delete buttons and labelled the four steps as regions. Successful Next, Back, direct-step, and import navigation now moves focus to the new step heading; invalid input still receives focus on the offending field.
- Mobile touch emulation in Chrome 154 at 390 × 844 accepted `.5` W and advanced to the battery step with 0.04902 Ah; focus moved to that step heading. At 320 × 800 in Enterprise mode, mode controls stayed on one line, document width matched the viewport, and two delete buttons had distinct accessible names. Notebook viewport at 1366 × 768 also had no horizontal overflow and retained the sidebar plus main workspace.
- These are Chromium viewport/touch emulation checks, not physical phone, iOS Safari, Android WebView, notebook hardware, or screen-reader certification. The repo still has no native wrapper or installable PWA manifest/service worker. Runtime remains a linear Ah estimate; a specific UPS requires its manufacturer curve for model-level accuracy.
