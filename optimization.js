(function (root, factory) {
    const calculations = root && root.UPScalculations
        ? root.UPScalculations
        : typeof module === 'object' && module.exports ? require('./calculations.js') : null;
    const api = factory(calculations);
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.UPSOptimization = api;
}(typeof globalThis === 'undefined' ? this : globalThis, function (calculations) {
    'use strict';

    const MAX_DEVICE_INSTANCES = 10_000;

    function roundSix(value) {
        const rounded = Math.round(value * 1_000_000) / 1_000_000;
        if (!Number.isFinite(rounded) || rounded <= 0) throw new Error('กำลังไฟต้องเป็นค่าบวกที่มีความละเอียดเพียงพอ');
        return rounded;
    }

    function buildRequest(inputDevices, runtimeRequirementMinutes, deviceLoadWatts, totalLoadWatts) {
        const runtime = Number(runtimeRequirementMinutes);
        const deviceLoad = Number(deviceLoadWatts);
        const totalLoad = Number(totalLoadWatts);
        if (!Number.isFinite(runtime) || runtime <= 0) throw new Error('ระบุ Runtime ที่ต้องการเป็นนาทีให้มากกว่า 0');
        if (!Number.isFinite(deviceLoad) || deviceLoad <= 0 || !Number.isFinite(totalLoad) || totalLoad < deviceLoad) {
            throw new Error('ต้องมีโหลดอุปกรณ์และจำนวนปลั๊กก่อนคำนวณแบบจัดสรร');
        }
        if (!Array.isArray(inputDevices) || inputDevices.length === 0) throw new Error('เพิ่มรายการอุปกรณ์อย่างน้อยหนึ่งรายการ');

        const scale = totalLoad / deviceLoad;
        const devices = [];
        let totalOutletInstances = 0;
        inputDevices.forEach((row, index) => {
            if (row.load === undefined || row.load === null
                || (typeof row.load === 'string' && row.load.trim() === '')) return;
            const load = Number(row.load);
            if (!Number.isFinite(load) || load < 0) throw new Error(`ตรวจสอบกำลังไฟของอุปกรณ์ลำดับ ${index + 1}`);
            const quantity = row.quantity === undefined ? 1 : Number(row.quantity);
            if (!Number.isSafeInteger(quantity) || quantity < 1) throw new Error(`ตรวจสอบจำนวนอุปกรณ์ของลำดับ ${index + 1}`);
            const plugValue = row.plugs === undefined ? 1 : row.plugs;
            const plugs = (typeof plugValue === 'number'
                || (typeof plugValue === 'string' && plugValue.trim() !== ''))
                ? Number(plugValue) : NaN;
            if (!Number.isSafeInteger(plugs) || plugs < 1) throw new Error(`ตรวจสอบจำนวนปลั๊กของอุปกรณ์ลำดับ ${index + 1}`);
            const plugGroups = calculations.normalizePlugGroups(row.plugGroups, plugs);
            const outletQuantity = quantity * plugs;
            if (!Number.isSafeInteger(outletQuantity)
                || totalOutletInstances + outletQuantity > MAX_DEVICE_INSTANCES) {
                throw new Error('จำนวนเต้ารับรวมเกินขีดจำกัด 10,000 ช่องสำหรับการจัดสรร');
            }
            totalOutletInstances += outletQuantity;
            if (load === 0) {
                devices.push({
                    name: String(row.name || `อุปกรณ์ ${index + 1}`).trim().slice(0, 160) || `อุปกรณ์ ${index + 1}`,
                    quantity, watt: 0, va: 0, priority: 50,
                    plugs: plugGroups,
                });
                return;
            }
            const unit = row.unit === 'VA' ? 'VA' : 'W';
            const watts = unit === 'VA' ? load * 0.8 : load;
            const va = unit === 'VA' ? load : load / 0.8;
            devices.push({
                name: String(row.name || `อุปกรณ์ ${index + 1}`).trim().slice(0, 160) || `อุปกรณ์ ${index + 1}`,
                quantity,
                watt: roundSix(watts * scale),
                va: roundSix(va * scale),
                priority: 50,
                plugs: plugGroups,
            });
        });
        if (!devices.length) throw new Error('Forecast อย่างเดียวจัด outlet ไม่ได้ กรุณาเพิ่มโหลดอุปกรณ์ที่ระบุจำนวนปลั๊ก');
        return { runtimeRequirementMinutes: runtime, devices };
    }

    function buildFixedRequest(baseRequest, selectedModels) {
        if (!baseRequest || !Array.isArray(baseRequest.devices) || !baseRequest.devices.length
                || !Number.isFinite(Number(baseRequest.runtimeRequirementMinutes))
                || Number(baseRequest.runtimeRequirementMinutes) <= 0) {
            throw new Error('ตรวจสอบโหลดอุปกรณ์และ Runtime ก่อนจัดสรร');
        }
        if (!Array.isArray(selectedModels) || !selectedModels.length) {
            throw new Error('เลือกรุ่น UPS และชุดแบตเตอรี่อย่างน้อยหนึ่งรายการ');
        }
        if (selectedModels.length > 20) throw new Error('เลือกได้สูงสุด 20 ชุดรุ่นและแบตเตอรี่');
        const seen = new Set();
        let total = 0;
        const models = selectedModels.map(item => {
            if (!item || !Number.isSafeInteger(item.productId) || item.productId < 1 || item.productId > 2147483647) {
                throw new Error('รหัสรุ่น UPS ไม่ถูกต้อง');
            }
            if (typeof item.batteryConfigId !== 'string' || !/^[A-Za-z0-9_.:-]{1,80}$/.test(item.batteryConfigId)) {
                throw new Error('เลือกชุดแบตเตอรี่ที่ยืนยันแล้วสำหรับทุกรุ่น');
            }
            const key = JSON.stringify([item.productId, item.batteryConfigId]);
            if (seen.has(key)) throw new Error('มีรุ่นและชุดแบตเตอรี่ซ้ำ กรุณาตรวจสอบรายการ');
            seen.add(key);
            const value = item.quantity;
            const integerText = typeof value === 'string' && /^\d+$/.test(value.trim());
            const quantity = typeof value === 'number' || integerText ? Number(value) : NaN;
            if (!Number.isSafeInteger(quantity) || quantity < 1 || quantity > 500) {
                throw new Error('จำนวน UPS ต้องเป็นจำนวนเต็มตั้งแต่ 1 ถึง 500 เครื่อง');
            }
            total += quantity;
            return { productId: item.productId, batteryConfigId: item.batteryConfigId, quantity };
        });
        if (total > 500) throw new Error('จำนวน UPS รวมต้องไม่เกิน 500 เครื่อง');
        return { selectedUps: { mode: 'fixed', models },
            runtimeRequirementMinutes: Number(baseRequest.runtimeRequirementMinutes), devices: baseRequest.devices };
    }

    async function optimize(request) {
        if (typeof fetch !== 'function') throw new Error('เบราว์เซอร์นี้ไม่รองรับการเชื่อมต่อกับบริการจัดสรร');
        let response;
        try {
            response = await fetch('/api/optimizations', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(request),
            });
        } catch (error) {
            throw new Error('เชื่อมต่อบริการจัดสรรไม่ได้ กรุณาตรวจการเชื่อมต่อแล้วลองใหม่');
        }
        if (response.status === 401) throw new Error('กรุณาเข้าสู่ระบบเว็บนี้ก่อนคำนวณการจัดสรร');
        if (response.status === 404) throw new Error('เว็บนี้ยังไม่ได้เชื่อมบริการจัดสรร กรุณาเปิด URL ของบริการที่เชื่อม Turso');
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || `Optimizer ตอบกลับ HTTP ${response.status}`);
        return payload;
    }

    async function allocate(request) {
        if (typeof fetch !== 'function') throw new Error('เบราว์เซอร์นี้ไม่รองรับการเชื่อมต่อกับบริการจัดสรร');
        let response;
        try {
            response = await fetch('/api/allocations', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
            });
        } catch (_) {
            throw new Error('เชื่อมต่อบริการจัดสรรไม่ได้ กรุณาตรวจการเชื่อมต่อแล้วลองใหม่');
        }
        if (response.status === 401) throw new Error('กรุณาเข้าสู่ระบบเว็บนี้ก่อนคำนวณการจัดสรร');
        if (response.status === 404) throw new Error('เว็บนี้ยังไม่ได้เชื่อมบริการจัดสรร กรุณาเปิด URL ของบริการที่เชื่อม Turso');
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(payload && typeof payload.error === 'string'
            ? payload.error : `บริการจัดสรรตอบกลับ HTTP ${response.status}`);
        if (!payload || !payload.validation || !Array.isArray(payload.deviceAllocations)
                || !Array.isArray(payload.unassignedDevices) || !Array.isArray(payload.upsUtilization)
                || !Array.isArray(payload.outletAllocations)) {
            throw new Error('ข้อมูลผลจัดสรรไม่ครบ กรุณาลองใหม่');
        }
        return payload;
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function modelMixText(models) {
        return (models || []).map(model => {
            const battery = model.batteryConfiguration ? ` · ${model.batteryConfiguration}` : '';
            return `${model.manufacturer || ''} ${model.model || 'UPS'}${battery} × ${model.quantity}`.trim();
        }).join(' · ');
    }

    function showNumber(value, digits = 2) {
        if (value === null || value === undefined || value === '') return '—';
        return Number.isFinite(Number(value)) ? Number(value).toLocaleString('th-TH', { maximumFractionDigits: digits }) : '—';
    }

    function runtimeText(unit) {
        return unit.runtimeStatus === 'not_applicable_zero_load'
            ? 'Runtime N/A (0 W)'
            : `Runtime ${showNumber(unit.estimatedRuntimeMinutes)} นาที`;
    }

    function addEvidenceList(container, evidence) {
        const references = [];
        Object.entries(evidence || {}).forEach(([kind, items]) => {
            (Array.isArray(items) ? items : []).forEach(item => {
                if (!item) return;
                const reference = item.url || item.title || item.sourceId || (item.path
                    ? `${item.path}${item.locator ? ` · ${item.locator}` : ''}`
                    : '');
                if (reference) references.push(`${kind}: ${reference}`);
            });
        });
        if (!references.length) return;
        const details = element('details', 'mt-2 text-xs text-slate-600');
        details.appendChild(element('summary', 'cursor-pointer font-medium', 'แหล่งข้อมูลเทคนิค'));
        const list = element('ul', 'mt-1 list-disc pl-5 space-y-1');
        references.forEach(reference => list.appendChild(element('li', '', reference)));
        details.appendChild(list);
        container.appendChild(details);
    }

    function renderSolution(solution, title, compact = false) {
        const card = element('section', 'bg-white border border-slate-200 rounded-xl p-4 shadow-sm');
        const heading = element('h4', 'font-bold text-slate-800', title);
        card.appendChild(heading);
        card.appendChild(element('p', 'mt-1 text-sm text-slate-600', modelMixText(solution.models)));
        const metrics = element('div', 'grid grid-cols-2 md:grid-cols-4 gap-2 mt-3');
        [
            ['จำนวน UPS', `${solution.unitCount} เครื่อง`],
            ['Runtime ต่ำสุด', `${showNumber(solution.minimumEstimatedRuntimeMinutes)} นาที`],
            ['สำรองเหนือเป้า', `${showNumber(solution.runtimeReserveMinutes)} นาที`],
            ['Expansion ที่ยืนยัน', `${solution.expansion?.maxBatteryPacks || 0} packs`],
        ].forEach(([label, value]) => {
            const metric = element('div', 'bg-slate-50 rounded-lg px-3 py-2');
            metric.appendChild(element('p', 'text-xs text-slate-500', label));
            metric.appendChild(element('p', 'text-sm font-semibold text-slate-800 mt-1', value));
            metrics.appendChild(metric);
        });
        card.appendChild(metrics);
        const connectorStatus = solution.validation && solution.validation.connectorStatus;
        if (connectorStatus === 'not_checked' || connectorStatus === 'partial') {
            card.appendChild(element('p', 'mt-3 text-sm text-amber-800', connectorStatus === 'partial'
                ? 'ตรวจชนิดหัวปลั๊กบางส่วนแล้ว แต่ยังมีปลั๊กที่ไม่ได้ระบุชนิด'
                : 'ยังไม่ได้ตรวจชนิดหัวปลั๊ก กรุณาตรวจความเข้ากันได้ก่อนใช้งาน'));
        }
        if (!compact) {
            const units = element('div', 'mt-4 space-y-2');
            (solution.units || []).forEach(unit => {
                const unitCard = element('div', 'border border-slate-200 rounded-lg p-3');
                unitCard.appendChild(element('p', 'font-semibold text-sm text-slate-800', `${unit.unitId} · ${unit.manufacturer || ''} ${unit.model || ''}`));
                unitCard.appendChild(element('p', 'text-sm text-slate-600 mt-1',
                    `${showNumber(unit.usedLoad.watts)} / ${showNumber(unit.ratings.watts)} W · ${showNumber(unit.usedLoad.va)} / ${showNumber(unit.ratings.va)} VA · ${unit.outlets.used} / ${unit.outlets.available} outlets · ${runtimeText(unit)}`));
                unitCard.appendChild(element('p', 'text-xs text-slate-500 mt-1',
                    `Engineering headroom ${showNumber(unit.engineeringHeadroomPercent.watts)}% W / ${showNumber(unit.engineeringHeadroomPercent.va)}% VA`));
                addEvidenceList(unitCard, unit.technicalEvidence);
                units.appendChild(unitCard);
            });
            card.appendChild(units);
        } else {
            const unitLines = element('ul', 'mt-3 text-xs text-slate-600 space-y-1');
            (solution.units || []).forEach(unit => {
                unitLines.appendChild(element('li', '',
                    `${unit.model}: ${showNumber(unit.usedLoad.watts)} / ${showNumber(unit.ratings.watts)} W · ${showNumber(unit.usedLoad.va)} / ${showNumber(unit.ratings.va)} VA · ${unit.outlets.used} / ${unit.outlets.available} outlets · ${runtimeText(unit)}`));
            });
            card.appendChild(unitLines);
        }
        const justification = element('ul', 'mt-3 text-sm text-slate-700 space-y-1');
        (solution.engineeringJustification || []).forEach(reason => justification.appendChild(element('li', '', `• ${reason}`)));
        card.appendChild(justification);
        return card;
    }

    function render(target, result) {
        target.replaceChildren();
        const excluded = result.selectionDiagnostics && result.selectionDiagnostics.excludedModels || [];
        if (excluded.length) {
            const reasons = { not_an_available_ups: 'ไม่พบรุ่น UPS ในฐานข้อมูล', watt_capacity_unknown: 'ไม่ทราบ W',
                va_capacity_unknown: 'ไม่ทราบ VA', outlet_unknown: 'ไม่ทราบช่อง Battery Backup',
                outlet_capacity: 'ไม่มีช่องสำรองไฟที่ใช้ได้', identity_unverified: 'ยังไม่ยืนยันรหัสรุ่น',
                battery_configuration_required: 'เลือกชุดแบตเตอรี่ก่อน', runtime_unknown: 'ไม่มีกราฟ Runtime ที่ยืนยัน' };
            target.appendChild(element('p', 'bg-amber-50 border border-amber-200 rounded-lg p-3 text-sm text-amber-800',
                'รุ่นที่เลือกยังใช้จัดสรรไม่ได้: ' + excluded.map(item => '#' + item.productId + ' (' +
                    item.reasons.map(reason => reasons[reason] || reason).join(', ') + ')').join(' · ')));
        }
        const note = (result.notes || []).join(' ');
        if (note) target.appendChild(element('p', 'bg-amber-50 border border-amber-200 rounded-lg p-3 text-sm text-amber-800', note));
        if (!result.bestSolution) {
            target.appendChild(element('section', 'bg-white border border-amber-200 rounded-xl p-5 text-sm text-slate-700',
                'ไม่พบแบบที่ผ่าน W, VA, Runtime และ Outlet ครบทุกข้อในขอบเขตการค้นหานี้'));
        } else {
            target.appendChild(renderSolution(result.bestSolution, 'Best Overall · แบบแนะนำ', false));
        }

        const scenarios = element('div', 'grid grid-cols-1 md:grid-cols-2 gap-3');
        (result.scenarios || []).forEach(scenario => {
            const card = element('article', scenario.available
                ? 'bg-white border border-slate-200 rounded-xl p-4'
                : 'bg-amber-50 border border-amber-200 rounded-xl p-4');
            card.appendChild(element('h4', 'font-bold text-slate-800', `${scenario.code} · ${scenario.name}`));
            card.appendChild(element('p', 'text-xs text-slate-500 mt-1', scenario.objective));
            if (!scenario.available) {
                card.appendChild(element('p', 'text-sm text-amber-800 mt-2', scenario.reason || 'ยังไม่มีแบบที่ผ่านข้อกำหนด'));
            } else {
                const design = scenario.solution;
                card.appendChild(element('p', 'text-sm font-semibold text-slate-800 mt-2', modelMixText(design.modelMix)));
                const costText = design.totalCost === null || design.totalCost === undefined
                    ? '' : ` · ${showNumber(design.totalCost)} ${result.priceComparison?.currency || ''} (ไม่รวม VAT)`;
                card.appendChild(element('p', 'text-sm text-slate-600 mt-1',
                    `${design.unitCount} เครื่อง · Runtime ต่ำสุด ${showNumber(design.minimumEstimatedRuntimeMinutes)} นาที · Expansion ${design.expansion.maxBatteryPacks} packs${costText}`));
            }
            scenarios.appendChild(card);
        });
        target.appendChild(scenarios);

        const alternatives = element('section', 'space-y-3');
        alternatives.appendChild(element('h3', 'text-lg font-bold text-slate-800', `ทางเลือกที่ผ่านข้อกำหนด (${(result.topAlternatives || []).length})`));
        (result.topAlternatives || []).forEach((solution, index) => {
            alternatives.appendChild(renderSolution(solution, `ทางเลือก ${index + 1}`, true));
        });
        if (!(result.topAlternatives || []).length) {
            alternatives.appendChild(element('p', 'text-sm text-slate-600', 'ไม่มีแบบทางเลือกอื่นที่แตกต่างจากแบบแนะนำในขอบเขตข้อมูลนี้'));
        }
        target.appendChild(alternatives);
    }

    function renderAllocation(target, result, selections = []) {
        target.replaceChildren();
        const assigned = Array.isArray(result.deviceAllocations) ? result.deviceAllocations : [];
        const unassigned = Array.isArray(result.unassignedDevices) ? result.unassignedDevices : [];
        const units = Array.isArray(result.upsUtilization) ? result.upsUtilization : [];
        const outletAllocations = Array.isArray(result.outletAllocations) ? result.outletAllocations : [];
        const validation = result.validation || {};
        const unassignedCount = unassigned.reduce((sum, row) => sum
            + (Number.isSafeInteger(row.quantity) && row.quantity > 0 ? row.quantity : 0), 0);
        const summary = element('section', validation.isValid === true
            ? 'bg-green-50 border border-green-200 rounded-xl p-4'
            : 'bg-amber-50 border border-amber-200 rounded-xl p-4');
        summary.appendChild(element('h3', 'font-bold text-slate-800', validation.isValid === true
            ? 'จัดสรรครบตามจำนวน UPS ที่มี' : 'ยังจัดสรรไม่ครบหรือมีเงื่อนไขไม่ผ่าน'));
        summary.appendChild(element('p', 'text-sm text-slate-700 mt-1',
            `จัดได้ ${assigned.length} อุปกรณ์ · ยังจัดไม่ได้ ${unassignedCount} อุปกรณ์ · ต้องการ Runtime ${showNumber(result.runtimeRequirementMinutes)} นาที`));
        const connectorLabels = { verified: 'ตรวจชนิดหัวปลั๊กครบตามข้อมูลที่ระบุ',
            partial: 'ตรวจชนิดหัวปลั๊กบางส่วนแล้ว แต่ยังมีปลั๊กที่ไม่ได้ระบุชนิด',
            not_checked: 'ยังไม่ได้ตรวจชนิดหัวปลั๊ก กรุณาตรวจความเข้ากันได้ก่อนใช้งาน',
            not_verified: 'ยังยืนยันความเข้ากันได้ของชนิดหัวปลั๊กไม่ได้' };
        summary.appendChild(element('p', 'text-sm text-slate-700 mt-2',
            connectorLabels[validation.connectorStatus] || 'ยังไม่ทราบสถานะการตรวจชนิดหัวปลั๊ก'));
        if (validation.searchComplete === false || result.searchComplete === false) {
            summary.appendChild(element('p', 'text-sm text-amber-800 mt-2',
                'ระบบค้นหาได้ไม่ครบทุกแบบ ผลนี้ยังยืนยันไม่ได้ว่าไม่มีวิธีจัดสรรที่ผ่าน'));
        }
        if (outletAllocations.length) {
            const checked = outletAllocations.filter(outlet => outlet.compatibilityStatus === 'verified').length;
            const unchecked = outletAllocations.filter(outlet => outlet.compatibilityStatus === 'not_checked').length;
            summary.appendChild(element('p', 'text-xs text-slate-600 mt-1',
                `หัวปลั๊กที่ตรวจแล้ว ${checked} · ยังไม่ระบุชนิด ${unchecked}`));
        }
        target.appendChild(summary);

        const grouped = new Map();
        assigned.forEach((row, index) => {
            const deviceIndex = Number.isSafeInteger(row.deviceIndex) ? row.deviceIndex : index;
            const name = String(row.name || 'อุปกรณ์ไม่ระบุชื่อ');
            const key = JSON.stringify([deviceIndex, name]);
            if (!grouped.has(key)) grouped.set(key, { name, count: 0, units: new Map() });
            const group = grouped.get(key);
            group.count++;
            const unitId = String(row.upsUnitId || 'UPS ไม่ทราบ');
            group.units.set(unitId, (group.units.get(unitId) || 0) + 1);
        });
        const assignedSection = element('section', 'bg-white border border-slate-200 rounded-xl p-4');
        assignedSection.appendChild(element('h4', 'font-bold text-slate-800', 'อุปกรณ์ที่จัดได้'));
        if (grouped.size) {
            const list = element('ul', 'mt-2 space-y-1 text-sm text-slate-700');
            grouped.forEach(group => list.appendChild(element('li', '', `${group.name} × ${group.count} · `
                + Array.from(group.units, ([id, count]) => `${id} × ${count}`).join(', '))));
            assignedSection.appendChild(list);
        } else assignedSection.appendChild(element('p', 'text-sm text-slate-600 mt-2', 'ยังไม่มีอุปกรณ์ที่จัดได้'));
        target.appendChild(assignedSection);

        const reasons = { identity_unverified: 'ยังไม่ยืนยันรหัสรุ่น UPS',
            watt_capacity_unknown: 'ไม่ทราบกำลัง W', va_capacity_unknown: 'ไม่ทราบกำลัง VA',
            watt_overload: 'โหลด W เกินกำลัง UPS', va_overload: 'โหลด VA เกินกำลัง UPS',
            watt_capacity: 'กำลัง W ไม่พอเมื่อเผื่อ 20%', va_capacity: 'กำลัง VA ไม่พอเมื่อเผื่อ 20%',
            outlet_unknown: 'ยังไม่ทราบช่อง Battery Backup', outlet_capacity: 'ช่อง Battery Backup ไม่พอ',
            connector_unknown: 'ยังไม่ยืนยันชนิดหัวปลั๊กกับเต้ารับ', connector_mismatch: 'ชนิดหัวปลั๊กไม่ตรงกับเต้ารับ',
            runtime_unknown: 'ไม่ทราบ Runtime ในช่วงโหลดนี้', runtime_insufficient: 'Runtime ต่ำกว่าเวลาที่ต้องการ',
            unit_limit_reached: 'ถึงขีดจำกัดจำนวน UPS', no_usable_selected_ups: 'ไม่มี UPS ที่เลือกซึ่งจัดสรรได้' };
        const missing = element('section', 'bg-white border border-slate-200 rounded-xl p-4');
        missing.appendChild(element('h4', 'font-bold text-slate-800', 'อุปกรณ์ที่ยังจัดไม่ได้'));
        if (unassigned.length) {
            const list = element('ul', 'mt-2 space-y-2 text-sm text-amber-800');
            unassigned.forEach(row => {
                const details = (Array.isArray(row.reasons) ? row.reasons : [])
                    .map(reason => reasons[reason] || String(reason)).join(', ') || 'ยังไม่ทราบสาเหตุ';
                list.appendChild(element('li', '', `${row.name || 'อุปกรณ์'} × ${showNumber(row.quantity)} · ${details}`));
            });
            missing.appendChild(list);
        } else missing.appendChild(element('p', 'text-sm text-slate-600 mt-2', 'ไม่มีอุปกรณ์ที่ตกค้าง'));
        target.appendChild(missing);

        const labels = new Map((Array.isArray(selections) ? selections : []).map(item =>
            [JSON.stringify([item.productId, item.batteryConfigId]), item.label]));
        const unitSection = element('section', 'space-y-2');
        unitSection.appendChild(element('h4', 'font-bold text-slate-800', 'การใช้ UPS แต่ละเครื่อง'));
        units.forEach(unit => {
            const card = element('article', 'bg-white border border-slate-200 rounded-lg p-3');
            const label = labels.get(JSON.stringify([unit.productId, unit.batteryConfigId]));
            card.appendChild(element('p', 'font-semibold text-sm text-slate-800',
                `${unit.upsUnitId || 'UPS'} · ${label || [unit.manufacturer, unit.model].filter(Boolean).join(' ') || 'รุ่นไม่ทราบ'}`));
            const used = unit.usedLoad || {}, ratings = unit.ratings || {}, outlets = unit.outlets || {};
            const available = outlets.available === null || outlets.available === undefined ? 'ไม่ทราบ' : showNumber(outlets.available);
            card.appendChild(element('p', 'text-sm text-slate-600 mt-1',
                `${showNumber(used.watts)} / ${showNumber(ratings.watts)} W · ${showNumber(used.va)} / ${showNumber(ratings.va)} VA · ช่อง ${showNumber(outlets.used)} / ${available}`));
            const runtime = unit.runtimeStatus === 'unused' ? 'ยังไม่ใช้เครื่องนี้ · ไม่คำนวณ Runtime'
                : unit.runtimeStatus === 'not_applicable_zero_load' ? 'Runtime ไม่เกี่ยวข้อง (โหลด 0 W)'
                : unit.estimatedRuntimeMinutes === null || unit.estimatedRuntimeMinutes === undefined
                    ? 'ไม่ทราบ Runtime' : `Runtime ${showNumber(unit.estimatedRuntimeMinutes)} นาที`;
            card.appendChild(element('p', 'text-sm text-slate-600 mt-1', runtime));
            addEvidenceList(card, unit.technicalEvidence);
            unitSection.appendChild(card);
        });
        if (!units.length) unitSection.appendChild(element('p', 'text-sm text-slate-600', 'ไม่มี UPS ในผลจัดสรร'));
        target.appendChild(unitSection);
    }

    return { buildRequest, buildFixedRequest, optimize, allocate, render, renderAllocation };
}));
