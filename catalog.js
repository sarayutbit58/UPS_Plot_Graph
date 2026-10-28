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

    function validConfigurationId(value) {
        return typeof value === 'string' && value.length > 0 && value.length <= 300
            && value === value.trim() && !/[\x00-\x1f]/.test(value);
    }

    function fixedQuantity(value) {
        const isIntegerText = typeof value === 'string' && /^\d+$/.test(value.trim());
        const parsed = typeof value === 'number' || isIntegerText ? Number(value) : NaN;
        if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 500) {
            throw new Error('จำนวน UPS ต้องเป็นจำนวนเต็มตั้งแต่ 1 ถึง 500 เครื่อง');
        }
        return parsed;
    }

    function createSelection() {
        const selected = new Map();
        const key = (productId, batteryConfigId) => JSON.stringify([productId, batteryConfigId]);
        return {
            configurations: () => Array.from(selected.values(), ({ productId, batteryConfigId }) => ({ productId, batteryConfigId })),
            entries: () => Array.from(selected.values(), item => ({ ...item })),
            has: (productId, batteryConfigId) => selected.has(key(productId, batteryConfigId)),
            clear: () => selected.clear(),
            setQuantity(productId, batteryConfigId, value) {
                const item = selected.get(key(productId, batteryConfigId));
                if (!item) throw new Error('ไม่พบชุดรุ่นและแบตเตอรี่ที่เลือก');
                item.quantity = value;
            },
            fixedModels() {
                const items = Array.from(selected.values());
                if (!items.length) throw new Error('เลือกรุ่น UPS และชุดแบตเตอรี่อย่างน้อยหนึ่งรายการ');
                if (items.length > 20) throw new Error('เลือกได้สูงสุด 20 ชุดรุ่นและแบตเตอรี่ในโหมดระบุจำนวน');
                let total = 0;
                const models = items.map(item => {
                    if (!Number.isSafeInteger(item.productId) || item.productId < 1 || item.productId > 2147483647) {
                        throw new Error('รหัสรุ่น UPS ไม่ถูกต้อง');
                    }
                    if (typeof item.batteryConfigId !== 'string'
                            || !/^[A-Za-z0-9_.:-]{1,80}$/.test(item.batteryConfigId)) {
                        throw new Error('เลือกชุดแบตเตอรี่ที่ยืนยันแล้วสำหรับทุกรุ่น');
                    }
                    const quantity = fixedQuantity(item.quantity);
                    total += quantity;
                    return { productId: item.productId, batteryConfigId: item.batteryConfigId, quantity };
                });
                if (total > 500) throw new Error('จำนวน UPS รวมต้องไม่เกิน 500 เครื่อง');
                return models;
            },
            toggle(productId, batteryConfigId, label, checked) {
                if (!Number.isSafeInteger(productId) || productId < 1) throw new Error('รหัสรุ่นไม่ถูกต้อง');
                if (batteryConfigId !== null && !validConfigurationId(batteryConfigId)) throw new Error('รหัสชุดแบตเตอรี่ไม่ถูกต้อง');
                if (typeof checked !== 'boolean') throw new Error('สถานะการเลือกไม่ถูกต้อง');
                const selectionKey = key(productId, batteryConfigId);
                if (checked && !selected.has(selectionKey) && selected.size >= 100) throw new Error('เลือกได้สูงสุด 100 ชุดรุ่นและแบตเตอรี่');
                if (checked) {
                    if (!selected.has(selectionKey)) {
                        selected.set(selectionKey, { productId, batteryConfigId, label: String(label || productId), quantity: '1' });
                    }
                } else selected.delete(selectionKey);
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
        return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'ไม่ทราบ'
            : Number(value).toLocaleString('th-TH', { maximumFractionDigits: 6 });
    }

    function describeCheck(key, check, model) {
        const labels = { capacityWatts: 'กำลัง W', capacityVa: 'กำลัง VA', outlets: 'ช่อง Battery Backup', runtime: 'Runtime นาที' };
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

    function manufacturerLink(source) {
        if (!source || typeof source.url !== 'string') return null;
        try {
            const url = new URL(source.url);
            if (url.protocol !== 'https:' || url.username || url.password || url.search) return null;
            if (Number.isSafeInteger(source.page) && source.page > 0) url.hash = 'page=' + source.page;
            const link = node('a', 'text-sm text-blue-700 underline', 'เอกสารผู้ผลิต');
            link.href = url.href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            return link;
        } catch (_) {
            return null;
        }
    }

    function validIsoDate(value) {
        if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
        const date = new Date(value + 'T00:00:00.000Z');
        return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
    }

    const lifecycleRegions = {
        thailand: 'th', 'kingdom of thailand': 'th', th: 'th', tha: 'th',
        singapore: 'sg', indonesia: 'id', malaysia: 'my', india: 'in',
        'south africa': 'za', belgium: 'be', 'united states': 'us', 'united kingdom': 'uk',
        italy: 'it', netherlands: 'nl', germany: 'de', egypt: 'eg', australia: 'au',
        'new zealand': 'nz', france: 'fr', philippines: 'ph', vietnam: 'vn', myanmar: 'mm',
    };

    function matchesLifecycleProduct(link, manufacturerSku, region) {
        if (typeof manufacturerSku !== 'string' || !manufacturerSku || !region) return false;
        try {
            const path = decodeURIComponent(new URL(link.href).pathname);
            const parts = path.replace(/^\/+|\/+$/g, '').split('/');
            return !path.includes('\\') && !parts.some(part => part === '.' || part === '..')
                && parts.length >= 4 && parts[0].toLowerCase() === region
                && parts[2].toLowerCase() === 'product'
                && parts[3].toLowerCase() === manufacturerSku.toLowerCase();
        } catch (_) { return false; }
    }

    function renderConfiguration(model, config, title, selection, onChange, onError) {
        const configId = config ? config.id : null;
        const configLabel = config ? String(config.label || config.id) : 'ยังไม่มีชุดแบตเตอรี่ที่ยืนยัน';
        const labelText = title + ' · ' + configLabel;
        const item = node('div', 'rounded-lg border border-slate-200 p-3 mt-2');
        const label = node('label', 'flex items-center gap-2 min-h-[44px] cursor-pointer text-sm text-slate-700');
        const checkbox = node('input', 'w-5 h-5');
        checkbox.type = 'checkbox';
        checkbox.checked = selection.has(model.productId, configId);
        checkbox.setAttribute('aria-label', 'เลือก ' + labelText);
        checkbox.addEventListener('change', () => {
            try {
                selection.toggle(model.productId, configId, labelText, checkbox.checked);
                onChange();
            } catch (error) {
                checkbox.checked = selection.has(model.productId, configId);
                onError(error.message);
            }
        });
        label.appendChild(checkbox);
        label.appendChild(node('span', '', config ? configLabel : 'เลือกเพื่อตรวจข้อมูลที่ขาด'));
        item.appendChild(label);
        if (!config) {
            item.appendChild(node('p', 'text-sm text-amber-800', 'ยังไม่มีข้อมูลชุดแบตเตอรี่และกราฟ Runtime ที่ยืนยันสำหรับจัดสรร'));
            return item;
        }
        const runtime = typeof config.runtimeMinutes === 'number' && Number.isFinite(config.runtimeMinutes)
            && config.runtimeMinutes >= 0 ? config.runtimeMinutes : null;
        item.appendChild(node('p', 'text-sm text-slate-600',
            'Runtime ที่โหลดปัจจุบัน: ' + (runtime === null ? 'ไม่ทราบ' : showNumber(runtime) + ' นาที')));
        if (!Array.isArray(config.extraPacks)) {
            item.appendChild(node('p', 'text-sm text-amber-800 mt-1', 'รายการและราคาแบตเตอรี่เสริมยังไม่ยืนยัน'));
        } else if (config.extraPacks.length) {
            const packs = config.extraPacks.map(pack => pack && pack.sku && Number.isSafeInteger(pack.quantity) && pack.quantity > 0
                ? String(pack.sku) + ' × ' + pack.quantity : 'รายละเอียดแพ็กยังไม่ครบ');
            item.appendChild(node('p', 'text-sm text-slate-600 mt-1', 'แบตเตอรี่เสริม: ' + packs.join(', ')));
            item.appendChild(node('p', 'text-sm text-amber-800 mt-1', 'ราคาแบตเตอรี่เสริมยังไม่ยืนยัน และยังไม่รวมในราคาเครื่อง'));
        }
        const link = manufacturerLink(config.source);
        if (link) item.appendChild(link);
        return item;
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
            const title = [model.manufacturer, model.model].filter(Boolean).join(' ') || 'รุ่นยังไม่ทราบ';
            const top = node('div', 'flex flex-wrap items-start justify-between gap-3');
            const heading = node('div', 'min-w-0');
            heading.appendChild(node('h4', 'font-semibold text-slate-800 break-words', title));
            if (model.sku && model.sku !== model.model) heading.appendChild(node('p', 'text-xs text-slate-500 mt-1', 'SKU ' + model.sku));
            heading.appendChild(node('p', model.status === 'matches' ? 'text-sm text-green-700 mt-1' : 'text-sm text-amber-800 mt-1', statuses[model.status]));
            top.appendChild(heading); card.appendChild(top);
            const checks = node('ul', 'mt-3 grid grid-cols-1 lg:grid-cols-2 gap-2 text-sm');
            Object.entries(model.checks).forEach(([key, check]) => {
                const color = !check.enabled ? 'text-slate-500' : check.status === 'pass' ? 'text-green-700' : 'text-amber-800';
                checks.appendChild(node('li', color, describeCheck(key, check, model)));
            });
            card.appendChild(checks);
            if (model.identityVerified !== true) {
                card.appendChild(node('p', 'text-sm text-amber-800 mt-2', 'รหัสรุ่นยังไม่ยืนยันกับผู้ผลิต'));
            }
            const ratingState = model.ratingsVerified || {};
            const unverifiedRatings = [ratingState.watts !== true ? 'W' : '', ratingState.va !== true ? 'VA' : ''].filter(Boolean);
            if (unverifiedRatings.length) {
                card.appendChild(node('p', 'text-sm text-amber-800 mt-2', 'กำลัง ' + unverifiedRatings.join(' / ') + ' ยังไม่ยืนยัน'));
            }
            if (!Number.isSafeInteger(model.batteryBackupOutletCount) || model.batteryBackupOutletCount < 0) {
                card.appendChild(node('p', 'text-sm text-amber-800 mt-2', 'จำนวนช่อง Battery Backup ยังไม่ยืนยัน'));
            } else {
                card.appendChild(node('p', 'text-sm text-slate-600 mt-2',
                    'ช่อง Battery Backup ที่ยืนยัน: ' + showNumber(model.batteryBackupOutletCount)));
            }
            const outletLabels = { IEC_C13: 'IEC C13', IEC_C19: 'IEC C19',
                NEMA_5_15R: 'NEMA 5-15R', UNIVERSAL: 'Universal', AS: 'AS', HARDWIRE: 'Hardwire' };
            const powerLabels = { battery: 'Battery Backup', surge: 'กันไฟกระชากเท่านั้น',
                unknown: 'ยังไม่ทราบประเภทการสำรองไฟ' };
            const groups = Array.isArray(model.outletGroups) ? model.outletGroups : [];
            const validGroups = groups.filter(group => group && Number.isSafeInteger(group.count)
                && group.count >= 0 && Object.hasOwn(powerLabels, group.power)
                && (group.type === null || Object.hasOwn(outletLabels, group.type)));
            if (validGroups.length) {
                const list = node('ul', 'mt-2 space-y-1 text-sm text-slate-600');
                validGroups.forEach(group => {
                    const type = group.type === null ? 'ชนิดหัวไม่ทราบ' : outletLabels[group.type];
                    const item = node('li', '', type + ' · ' + group.count + ' ช่อง · ' + powerLabels[group.power]);
                    const link = manufacturerLink(group.source);
                    if (link) { item.appendChild(node('span', '', ' · ')); item.appendChild(link); }
                    list.appendChild(item);
                });
                card.appendChild(list);
            }
            const evidence = model.technicalEvidence || {};
            const ratingSource = manufacturerLink(evidence.ratingWatts || evidence.ratingVa || evidence.identity);
            if (ratingSource) card.appendChild(ratingSource);
            const crosswalk = evidence.identityCrosswalk;
            if (model.identityVerified === true && crosswalk && typeof crosswalk.sourceId === 'string'
                    && /^[a-f0-9]{24}$/i.test(crosswalk.sourceId)) {
                const link = manufacturerLink({ url: crosswalk.manufacturerUrl });
                if (link) {
                    const reference = node('p', 'text-xs text-slate-600 mt-2', 'อ้างอิงฐาน ' + crosswalk.sourceId + ' · ');
                    link.textContent = 'เอกสารเชื่อมรหัสผู้ผลิต';
                    reference.appendChild(link);
                    card.appendChild(reference);
                }
            }
            const observations = Array.isArray(model.lifecycleObservations) ? model.lifecycleObservations : [];
            observations.forEach(observation => {
                if (model.identityVerified !== true || !observation || typeof observation !== 'object'
                        || typeof observation.country !== 'string'
                        || observation.country !== observation.country.trim()
                        || !observation.country || observation.country.length > 80
                        || !/^[A-Za-z]+(?:[ -][A-Za-z]+)*$/.test(observation.country)
                        || observation.thailandStatus !== 'unknown'
                        || !validIsoDate(observation.endOfSaleDate) || !validIsoDate(observation.observedOn)
                        || observation.observedOn > new Date(Date.now() + 7 * 3600000).toISOString().slice(0, 10)
                        || !observation.source || (observation.source.page != null
                            && (!Number.isSafeInteger(observation.source.page) || observation.source.page < 1))) return;
                const link = manufacturerLink(observation.source);
                if (!link) return;
                const region = lifecycleRegions[observation.country.toLowerCase()];
                if (!matchesLifecycleProduct(link, model.manufacturerSku, region)) return;
                const isThailand = region === 'th';
                const note = node('p', 'text-sm text-amber-800 mt-2',
                    (isThailand
                        ? 'ผู้ผลิตกำหนดวันสิ้นสุดการขายในประเทศไทย ' + observation.endOfSaleDate
                            + ' · สต็อกและการจำหน่ายจริงต้องตรวจสอบ'
                        : 'ผู้ผลิตกำหนดวันสิ้นสุดการขายใน ' + observation.country + ' ' + observation.endOfSaleDate
                            + ' · สถานะ/สต็อกไทยยังไม่ทราบ')
                    + ' · ข้อมูล ณ ' + observation.observedOn + ' · ');
                note.appendChild(link);
                card.appendChild(note);
            });
            const expansionData = model.expansion || {};
            const expansion = expansionData.supported === true ? 'รองรับ'
                : expansionData.supported === false ? 'ไม่รองรับ' : 'ไม่ทราบ';
            card.appendChild(node('p', 'text-sm text-slate-600 mt-3', 'แบตเตอรี่เสริม: ' + expansion));
            const priceReasons = { missing_price: 'ไม่มีราคา Dealer/NDP ที่ใช้เปรียบเทียบได้', missing_vat: 'ราคาไม่ระบุฐาน VAT',
                unverified_vat_rate: 'ราคาแบบรวม VAT ยังยืนยันช่วงเวลาที่ใช้แปลงไม่ได้',
                mixed_currency: 'สกุลเงินไม่ตรงกัน', non_thb_currency: 'ราคาเป็นสกุลเงินอื่น',
                bundle_identity_unverified: 'รอแยกชุดสินค้าและยืนยันราคาต้นทาง' };
            card.appendChild(node('p', 'text-sm text-slate-600 mt-1', model.price
                ? 'ราคาอ้างอิง ' + showNumber(model.price.amount) + ' บาท/เครื่อง ไม่รวม VAT · ตรวจราคาปัจจุบันก่อนสั่งซื้อ'
                : 'ราคาไม่รวม VAT: ' + (priceReasons[model.priceUnavailableReason] || 'ยังยืนยันฐานราคาไม่ได้')));
            if (model.hasSourceConflicts) {
                card.appendChild(node('p', 'text-sm text-amber-800 mt-2',
                    'ข้อมูลอ้างอิงบางรายการไม่ตรงกัน ควรตรวจสเปกของรุ่นและชุดแบตเตอรี่ก่อนเลือกจริง'));
            }
            if (!model.canOptimize) {
                card.appendChild(node('p', 'text-sm text-amber-800 mt-2',
                    'เลือกไว้ตรวจข้อมูลได้; การจัดสรรอัตโนมัติต้องยืนยันรุ่น กำลัง ช่อง Battery Backup และ Runtime'));
            }
            const rawConfigs = Array.isArray(model.batteryConfigurations) ? model.batteryConfigurations : [];
            const counts = new Map();
            rawConfigs.forEach(config => {
                if (config && validConfigurationId(config.id)) counts.set(config.id, (counts.get(config.id) || 0) + 1);
            });
            const configs = rawConfigs.filter(config => config && validConfigurationId(config.id) && counts.get(config.id) === 1);
            const options = node('fieldset', 'mt-3');
            options.appendChild(node('legend', 'text-sm font-semibold text-slate-800', 'เลือกชุดแบตเตอรี่ (เลือกได้หลายชุด)'));
            if (configs.length !== rawConfigs.length) {
                options.appendChild(node('p', 'text-sm text-amber-800 mt-2', 'ข้อมูลชุดแบตเตอรี่บางรายการไม่ถูกต้อง กรุณาค้นหาใหม่'));
            }
            (configs.length ? configs : [null]).forEach(config => {
                options.appendChild(renderConfiguration(model, config, title, selection, onChange, onError));
            });
            card.appendChild(options);
            target.appendChild(card);
        });
    }

    return { buildSearchRequest, createSelection, fixedQuantity, search, render, describeCheck };
}));
