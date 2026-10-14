(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.UPSOptimization = api;
}(typeof globalThis === 'undefined' ? this : globalThis, function () {
    'use strict';

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
        inputDevices.forEach((row, index) => {
            if (row.load === undefined || row.load === null
                || (typeof row.load === 'string' && row.load.trim() === '')) return;
            const load = Number(row.load);
            if (!Number.isFinite(load) || load < 0) throw new Error(`ตรวจสอบกำลังไฟของอุปกรณ์ลำดับ ${index + 1}`);
            const plugs = Number(row.plugs || 1);
            if (!Number.isSafeInteger(plugs) || plugs < 1) throw new Error(`ตรวจสอบจำนวนปลั๊กของอุปกรณ์ลำดับ ${index + 1}`);
            if (load === 0) {
                devices.push({
                    name: String(row.name || `อุปกรณ์ ${index + 1}`).trim().slice(0, 160) || `อุปกรณ์ ${index + 1}`,
                    quantity: plugs, watt: 0, va: 0, priority: 50,
                });
                return;
            }
            const unit = row.unit === 'VA' ? 'VA' : 'W';
            const watts = unit === 'VA' ? load * 0.8 : load;
            const va = unit === 'VA' ? load : load / 0.8;
            devices.push({
                name: String(row.name || `อุปกรณ์ ${index + 1}`).trim().slice(0, 160) || `อุปกรณ์ ${index + 1}`,
                quantity: plugs,
                watt: roundSix(watts / plugs * scale),
                va: roundSix(va / plugs * scale),
                priority: 50,
            });
        });
        if (!devices.length) throw new Error('Forecast อย่างเดียวจัด outlet ไม่ได้ กรุณาเพิ่มโหลดอุปกรณ์ที่ระบุจำนวนปลั๊ก');
        return { runtimeRequirementMinutes: runtime, devices };
    }

    async function optimize(request) {
        if (typeof fetch !== 'function') throw new Error('เบราว์เซอร์นี้ไม่รองรับการเชื่อมต่อกับ local optimizer');
        const hostname = typeof location === 'undefined' ? '' : location.hostname;
        if (!['127.0.0.1', 'localhost', '::1'].includes(hostname)) {
            throw new Error('การค้นหาใช้ catalog ในเครื่องเพื่อรักษาข้อมูลราคา กรุณาเปิดแอปผ่าน http://127.0.0.1:8000');
        }
        let response;
        try {
            response = await fetch('/api/optimizations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(request),
            });
        } catch (error) {
            throw new Error('เชื่อมต่อ local optimizer ไม่ได้ กรุณาเริ่ม recommendation service แล้วเปิด http://127.0.0.1:8000');
        }
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || `Optimizer ตอบกลับ HTTP ${response.status}`);
        return payload;
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function modelMixText(models) {
        return (models || []).map(model => `${model.manufacturer || ''} ${model.model || 'UPS'} × ${model.quantity}`.trim()).join(' · ');
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
                const reference = item.path
                    ? `${item.path}${item.locator ? ` · ${item.locator}` : ''}`
                    : item.url || '';
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

    return { buildRequest, optimize, render };
}));
