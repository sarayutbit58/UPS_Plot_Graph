(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.UPSCatalog = api;
}(typeof globalThis === 'undefined' ? this : globalThis, function () {
    'use strict';

    function number(value, label, minimum, maximum) {
        if (typeof value === 'boolean' || value === null || value === undefined || String(value).trim() === '') {
            throw new Error('ระบุ' + label + 'ให้ครบ');
        }
        const parsed = Number(value);
        if (!Number.isFinite(parsed) || parsed < minimum || (maximum !== undefined && parsed > maximum)) {
            throw new Error('ตรวจสอบ' + label);
        }
        return parsed;
    }

    function buildSearchRequest(filters, load) {
        const watts = number(load.watts, 'โหลดรวม', 0);
        const va = number(load.va, 'โหลด VA', 0);
        const outlets = number(load.outlets, 'จำนวนเต้ารับ', 0);
        if (!Number.isSafeInteger(outlets)) throw new Error('จำนวนเต้ารับต้องเป็นจำนวนเต็ม');
        const page = number(filters.page === undefined ? 1 : filters.page, 'หน้า', 1);
        if (!Number.isSafeInteger(page)) throw new Error('เลขหน้าต้องเป็นจำนวนเต็ม');
        if (!['strict', 'nearby'].includes(filters.mode)) throw new Error('เลือกรูปแบบการค้นหา');
        const request = {
            query: String(filters.query || '').trim().slice(0, 160),
            manufacturer: String(filters.manufacturer || '').trim().slice(0, 120),
            mode: filters.mode,
            headroomPercent: number(filters.headroomPercent, 'กำลังสำรอง %', 0, 50),
            nearbyTolerancePercent: number(filters.nearbyTolerancePercent, 'ช่วงใกล้เคียง %', 0, 50),
            expansion: filters.expansion === 'supported' ? 'supported' : 'any',
            requireRuntime: Boolean(filters.requireRuntime),
            requireOutlets: Boolean(filters.requireOutlets),
            includeUnknown: filters.mode === 'nearby' && Boolean(filters.includeUnknown),
            requiredWatts: watts, requiredVa: va, requiredOutlets: outlets,
            runtimeRequirementMinutes: number(load.runtime, 'Runtime ที่ต้องการ', 0),
            page, pageSize: 25,
        };
        if (filters.maxPriceTHB !== undefined && String(filters.maxPriceTHB).trim() !== '') {
            request.maxPriceTHB = number(filters.maxPriceTHB, 'งบต่อ UPS ไม่รวม VAT', Number.MIN_VALUE);
        }
        return request;
    }

    function createSelection() {
        const selected = new Map();
        return {
            ids: () => Array.from(selected.keys()),
            entries: () => Array.from(selected.entries()),
            has: id => selected.has(id),
            clear: () => selected.clear(),
            toggle(id, label, checked) {
                if (!Number.isSafeInteger(id) || id < 1) throw new Error('รหัสรุ่นไม่ถูกต้อง');
                if (checked && !selected.has(id) && selected.size >= 100) throw new Error('เลือกรุ่นได้สูงสุด 100 รุ่น');
                if (checked) selected.set(id, String(label || id));
                else selected.delete(id);
            },
        };
    }

    async function search(request) {
        let response;
        try {
            response = await fetch('/api/catalog/search', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
            });
        } catch (_) {
            throw new Error('เชื่อมต่อฐานข้อมูลไม่ได้ กรุณาตรวจการเชื่อมต่อแล้วค้นหาใหม่');
        }
        if (response.status === 401) throw new Error('กรุณาเข้าสู่ระบบเว็บนี้ก่อนค้นหาฐานข้อมูล');
        if (response.status === 404) throw new Error('เว็บนี้ยังไม่ได้เชื่อม Catalog API กรุณาเปิด URL ของบริการที่เชื่อม Turso');
        const payload = await response.json().catch(() => null);
        if (!response.ok || !payload || !Array.isArray(payload.models)) {
            throw new Error(payload && payload.error || 'ฐานข้อมูลยังไม่พร้อมใช้งาน กรุณาลองใหม่');
        }
        return payload;
    }

    function showNumber(value) {
        return value === null || value === undefined ? 'ไม่ทราบ'
            : Number(value).toLocaleString('th-TH', { maximumFractionDigits: 6 });
    }

    function describeCheck(key, check, model) {
        const labels = { capacityWatts: 'กำลัง W', capacityVa: 'กำลัง VA', outlets: 'เต้ารับ', runtime: 'Runtime นาที' };
        const states = { pass: 'ผ่าน', fail: 'ไม่ผ่าน', unknown: 'ไม่ทราบ' };
        let text = labels[key] + ': ' + (check.enabled ? states[check.status] : 'ไม่ได้ใช้กรอง');
        text += ' · มี ' + showNumber(check.available) + ' / ต้องการ ' + showNumber(check.required);
        if (check.enabled && check.status === 'fail') text += ' · ขาด ' + showNumber(check.shortfall);
        if (key === 'runtime' && check.status === 'unknown' && model.runtimeLoadRangeWatts) {
            text += ' · กราฟรองรับโหลด ' + showNumber(model.runtimeLoadRangeWatts.minimum)
                + '–' + showNumber(model.runtimeLoadRangeWatts.maximum) + ' W';
        }
        return text;
    }

    function node(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = String(text);
        return element;
    }

    function render(target, result, selection, onChange, onError) {
        target.replaceChildren();
        if (!result.models.length) {
            target.appendChild(node('p', 'rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800',
                'ไม่มีรุ่นตรงกับตัวกรองนี้ ลองโหมดใกล้เคียง เปิดแสดงข้อมูลไม่ครบ หรือปรับตัวกรอง แล้วค้นหาอีกครั้ง'));
            return;
        }
        const statuses = { matches: 'ผ่านตัวกรองที่เปิดใช้', nearby: 'ใกล้เคียง · มีข้อไม่ผ่าน', unknown: 'ข้อมูลยังไม่ครบ' };
        result.models.forEach(model => {
            const card = node('article', 'rounded-xl border border-slate-200 bg-white p-4');
            const title = [model.manufacturer, model.model].filter(Boolean).join(' ');
            const top = node('div', 'flex flex-wrap items-start justify-between gap-3');
            const heading = node('div', 'min-w-0');
            heading.appendChild(node('h4', 'font-semibold text-slate-800 break-words', title));
            if (model.sku && model.sku !== model.model) heading.appendChild(node('p', 'text-xs text-slate-500 mt-1', 'SKU ' + model.sku));
            heading.appendChild(node('p', model.status === 'matches' ? 'text-sm text-green-700 mt-1' : 'text-sm text-amber-800 mt-1', statuses[model.status]));
            const label = node('label', 'flex items-center gap-2 min-h-[44px] cursor-pointer text-sm text-slate-700');
            const checkbox = node('input', 'w-5 h-5');
            checkbox.type = 'checkbox'; checkbox.checked = selection.has(model.productId);
            checkbox.setAttribute('aria-label', 'เลือกรุ่น ' + title);
            checkbox.addEventListener('change', () => {
                try {
                    selection.toggle(model.productId, title, checkbox.checked);
                    onChange();
                } catch (error) {
                    checkbox.checked = selection.has(model.productId);
                    onError(error.message);
                }
            });
            label.appendChild(checkbox); label.appendChild(node('span', '', 'เลือกรุ่นนี้'));
            top.appendChild(heading); top.appendChild(label); card.appendChild(top);
            const checks = node('ul', 'mt-3 grid grid-cols-1 lg:grid-cols-2 gap-2 text-sm');
            Object.entries(model.checks).forEach(([key, check]) => {
                const color = !check.enabled ? 'text-slate-500' : check.status === 'pass' ? 'text-green-700' : 'text-amber-800';
                checks.appendChild(node('li', color, describeCheck(key, check, model)));
            });
            card.appendChild(checks);
            const expansion = model.expansion.supported === true ? 'รองรับ'
                : model.expansion.supported === false ? 'ไม่รองรับ' : 'ไม่ทราบ';
            card.appendChild(node('p', 'text-sm text-slate-600 mt-3', 'แบตเตอรี่เสริม: ' + expansion));
            const priceReasons = { missing_price: 'ไม่มีราคาอ้างอิง', missing_vat: 'ราคาไม่ระบุฐาน VAT',
                unverified_vat_rate: 'ราคาแบบรวม VAT ยังยืนยันช่วงเวลาที่ใช้แปลงไม่ได้',
                mixed_currency: 'สกุลเงินไม่ตรงกัน', non_thb_currency: 'ราคาเป็นสกุลเงินอื่น' };
            card.appendChild(node('p', 'text-sm text-slate-600 mt-1', model.price
                ? 'ราคาอ้างอิง ' + showNumber(model.price.amount) + ' บาท/เครื่อง ไม่รวม VAT · ตรวจราคาปัจจุบันก่อนสั่งซื้อ'
                : 'ราคาไม่รวม VAT: ' + (priceReasons[model.priceUnavailableReason] || 'ยังยืนยันฐานราคาไม่ได้')));
            if (model.hasSourceConflicts) {
                card.appendChild(node('p', 'text-sm text-amber-800 mt-2',
                    'ข้อมูลอ้างอิงบางรายการไม่ตรงกัน ควรตรวจสเปกของรุ่นและชุดแบตเตอรี่ก่อนเลือกจริง'));
            }
            if (!model.canOptimize) {
                card.appendChild(node('p', 'text-sm text-amber-800 mt-2',
                    'เลือกไว้เปรียบเทียบได้; การจัดสรรอัตโนมัติต้องมี W, VA, เต้ารับ และกราฟ Runtime ครบ'));
            }
            target.appendChild(card);
        });
    }

    return { buildSearchRequest, createSelection, search, render, describeCheck };
}));
