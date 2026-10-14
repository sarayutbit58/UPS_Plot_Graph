"""Sequential, source-traceable UPS candidate extraction. No commercial defaults.

Usage: python -X utf8 database/extract.py --source-root PATH --output-dir PATH
Readers are optional; missing readers and image OCR gaps remain inventory outcomes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import hashlib
import io
import json
import re
import sys
import subprocess
import tempfile
import shutil
import os
from contextlib import contextmanager, ExitStack
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
import xml.etree.ElementTree as ET

DEPS = Path(__file__).parent / 'local' / 'deps'
if DEPS.exists():
    sys.path.insert(0, str(DEPS))

BRANDS = {'apc': 'APC', 'cyberpower': 'CyberPower', 'eaton': 'Eaton',
          'vertiv': 'Vertiv', 'liebert': 'Liebert', 'syndome': 'Syndome',
          'chuphotic': 'Chuphotic', 'leonics': 'Leonics', 'delta': 'Delta',
          'socomec': 'Socomec', 'schneider electric': 'Schneider Electric'}
UPS = re.compile(r'\bUPS\b|uninterruptible|เครื่องสำรองไฟ', re.I)
RATING = re.compile(r'(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(k?VA|k?W(?:ATT)?S?)\b', re.I)
MODEL = re.compile(r'(?<![\w])([A-Z0-9]+(?:[-_/][A-Z0-9]+)*)(?![\w])', re.I)
PRICE_HEADER = re.compile(r'price|srp|ndp|dealer|partner|ราคา|cost|ราคา', re.I)
MAX_MEMBER = 512 * 1024 * 1024
LOCAL = Path(__file__).parent / 'local'
OCR = None
NATIVE_POWERSHELL = shutil.which('pwsh') or 'powershell.exe'

OCR_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]
$generic = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } | Select-Object -First 1
function AwaitResult($operation, $resultType) {
    $task = $generic.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US'))
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
while ($request = [Console]::ReadLine()) {
    try {
        $path = $request | ConvertFrom-Json
        $file = AwaitResult ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = AwaitResult ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        try {
            $decoder = AwaitResult ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = AwaitResult ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            try {
                $result = AwaitResult ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                $lines = @($result.Lines | ForEach-Object { $_.Text })
                [Console]::WriteLine((@{lines=$lines;error=$null} | ConvertTo-Json -Compress))
            } finally { $bitmap.Dispose() }
        } finally { $stream.Dispose() }
    } catch {
        [Console]::WriteLine((@{lines=@();error=$_.Exception.Message} | ConvertTo-Json -Compress))
    }
}
'''


class WindowsOCR:
    """One persistent child, one image at a time, no model downloads."""
    def __init__(self):
        LOCAL.mkdir(parents=True, exist_ok=True)
        helper = LOCAL / 'winrt_ocr.ps1'
        helper.write_text(OCR_SCRIPT, encoding='utf-8-sig')
        self.process = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(helper)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def read(self, handle, ext):
        with tempfile.NamedTemporaryFile(suffix=ext, dir=LOCAL, delete=False) as temporary:
            image_path = Path(temporary.name)
            handle.seek(0)
            shutil.copyfileobj(handle, temporary, length=1024*1024)
        try:
            self.process.stdin.write(json.dumps(str(image_path)) + '\n')
            self.process.stdin.flush()
            response = self.process.stdout.readline()
            if not response:
                raise RuntimeError('Windows OCR helper closed: ' + self.process.stderr.read()[:500])
            result = json.loads(response)
            if result.get('error'):
                raise RuntimeError(result['error'])
            return result['lines']
        finally:
            image_path.unlink(missing_ok=True)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()


class TeeRecords:
    def __init__(self, target, cache):
        self.target, self.cache = target, cache
    def write(self, value):
        self.target.write(value)
        self.cache.write(value)
    def flush(self):
        self.target.flush()
        self.cache.flush()


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def number(value):
    text = str(value).strip().replace(',', '')
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        return None
    try:
        return str(Decimal(text))
    except InvalidOperation:
        return None


def brand(text):
    for token, canonical in BRANDS.items():
        if re.search(r'(?<![a-z])' + re.escape(token) + r'(?![a-z])', text, re.I):
            return canonical
    return None


def model_in(text):
    for match in MODEL.finditer(text):
        token = match.group(1).strip('/_-')
        if not re.search(r'[A-Z]', token, re.I) or not re.search(r'\d', token):
            continue
        if not re.fullmatch(r'\d+(?:K?VA|K?W(?:ATT)?S?|V(?:OLTS?)?|A(?:H|MP(?:S)?)?|HZ|MM|CM|U|PHASE|P)', token, re.I) and not re.fullmatch(r'PF[01]',token,re.I) and not re.match(r'https?', token, re.I):
            return token
    return None


def headers_for(cells):
    if sum(bool(re.search(r'model|description|article|sku|brand|part.?n|รุ่น|ราคา|price|srp|ndp', c, re.I)) for c in cells) >= 2:
        return cells
    return None


def candidates(path, locator, cells, headers=None, context='', notes=None):
    raw = ' | '.join(cells).strip()
    if not raw:
        return
    # Context allows UPS-branded sheets to contribute models without repeating UPS.
    explicit_ups = bool(UPS.search(raw))
    ups_context = bool(UPS.search(context))
    ratings = list(RATING.finditer(raw))
    maker = brand(raw) or brand(context)
    hdr = headers or []
    for i, header in enumerate(hdr):
        if re.fullmatch(r'brand(?: name)?|manufacturer(?: name)?|ยี่ห้อ|ผู้ผลิต',header.strip(),re.I) and i < len(cells):
            explicit_maker = cells[i].strip()
            if explicit_maker and explicit_maker.casefold() not in {'n/a','na','unknown','-','ไม่ระบุ'}:
                maker = brand(explicit_maker) or explicit_maker
                break
    if not explicit_ups and not (ups_context and (ratings or maker)):
        return
    if raw.startswith('#') or re.fullmatch(r'[ |:\-]+', raw):
        return
    def field(pattern):
        for i, h in enumerate(hdr):
            if re.fullmatch(pattern, h.strip(), re.I) and i < len(cells) and cells[i].strip():
                return cells[i].strip()
        return None
    description_cell = field(r'article description|description|product description|รายละเอียด|รายละเอียดสินค้า')
    desc = description_cell or raw
    sku = field(r'article code|sku|part\s*(?:number|no\.?)|product code|item code|รหัสสินค้า')
    source_sku = field(r'article\s*(?:code|no\.?|number)|item code|รหัสสินค้า')
    manufacturer_sku = field(r'(?:manufacturer|mfr|vendor)\s*(?:part\s*(?:number|no\.?)|product code|sku)|part\s*(?:number|no\.?)|mpn')
    sku_type = 'manufacturer' if manufacturer_sku and sku == manufacturer_sku else ('distributor' if source_sku and sku == source_sku else 'unknown')
    explicit_model = field(r'model|vendor product code|รุ่น')
    inferred_model = model_in(description_cell) if description_cell else None
    identity_notes = list(notes or [])
    category = ' '.join(cells[i] for i,h in enumerate(hdr) if i < len(cells) and re.fullmatch(r'category\s*level\s*[23]|product category|category|หมวดหมู่|ประเภทสินค้า',h.strip(),re.I))
    sheet_context = context[len(path):].strip() if context.startswith(path) else context
    non_ups_context = (category + ' ' + sheet_context).replace('_',' ')
    if re.search(r'\b(?:AVR|inverter|EPS|generator|automatic voltage regulator)\b', non_ups_context, re.I) or re.search(r'\b(?:PC|notebook|laptop|desktop|server|monitor|printer|camera|CCTV|firewall|router|switch)\b',category,re.I):
        identity_notes.append('non_ups_category: explicit sheet/category denotes another product class; verify UPS classification')
    if explicit_model and inferred_model and explicit_model.casefold() != inferred_model.casefold():
        identity_notes.append('model_conflict: explicit model column differs from description; explicit=' + explicit_model)
    model = inferred_model or explicit_model or manufacturer_sku
    if not model:
        model = model_in(raw)
    if not model and not ratings:
        return
    rating_va = rating_w = None
    for match in ratings:
        val = float(match.group(1).replace(',', ''))
        unit = match.group(2).lower()
        if unit.startswith('k'):
            val *= 1000
        if unit.endswith('va') and rating_va is None:
            rating_va = int(val) if val.is_integer() else val
        elif 'w' in unit and rating_w is None:
            rating_w = int(val) if val.is_integer() else val
    topo = None
    if re.search(r'line[ -]?interactive', raw, re.I):
        topo = 'line-interactive'
    elif re.search(r'\b(?:on[ -]?line|double conversion)\b', raw, re.I):
        topo = 'online'
    elif re.search(r'\b(?:offline|standby)\b', raw, re.I):
        topo = 'standby'
    form = 'rack' if re.search(r'\brack(?:mount)?\b', raw, re.I) else ('tower' if re.search(r'\btower\b', raw, re.I) else None)
    product_type = 'battery' if re.search(r'\bbattery\b|แบตเตอรี่', desc, re.I) and not rating_va else 'ups'
    if re.search(r'\b(?:rail kit|network card|management card|snmp card|rack kit)\b', desc, re.I):
        product_type = 'accessory'
    currency_cell = field(r'currency|currency unit|สกุลเงิน') or ''
    currency = 'THB' if re.search(r'\bTHB\b|บาท|฿', currency_cell, re.I) else ('USD' if re.search(r'\bUSD\b', currency_cell, re.I) else None)
    base = dict(source_path=path, source_locator=locator, raw_text=raw, manufacturer=maker,
                model=model, sku=sku, description=desc, product_type=product_type,
                rating_va=rating_va, rating_w=rating_w, topology=topo, form_factor=form,
                price_amount=None, currency=currency, vat_treatment='unknown', price_type='unknown',
                source_date=None, confidence=0.8 if model and explicit_ups else 0.55,
                extraction_notes=identity_notes)
    base.update(source_headers=list(hdr), sku_type=sku_type, source_sku=source_sku, manufacturer_sku=manufacturer_sku)
    prices = []
    for i, h in enumerate(hdr):
        if PRICE_HEADER.search(h) and i < len(cells):
            amount = number(cells[i])
            if amount:
                vat = 'exclusive' if re.search(r'ex(?:c)?[ ._-]*vat|exclud|ไม่รวม', h, re.I) else ('inclusive' if re.search(r'inc[ ._-]*vat|includ|รวม.?vat', h, re.I) else 'unknown')
                price_currency = 'THB' if re.search(r'\bTHB\b|บาท|฿', h, re.I) else ('USD' if re.search(r'\bUSD\b', h, re.I) else currency)
                prices.append((i, h, amount, vat, price_currency))
    if prices:
        for i, h, amount, vat, price_currency in prices:
            yield dict(base, source_locator=locator + f';column:{i+1}:{h}', price_amount=amount,
                       vat_treatment=vat, price_type=h, currency=price_currency,
                       extraction_notes=identity_notes + ['price extracted from explicit header: ' + h])
    else:
        yield base


def text_rows(stream):
    headers = None
    section = ''
    for n, line in enumerate(stream, 1):
        line = line.rstrip('\r\n')
        if line.startswith('#'):
            section = line
            headers = None
        cells = [v.strip() for v in line.strip().strip('|').split('|')] if '|' in line else [line]
        new_headers = headers_for(cells)
        if new_headers:
            headers = new_headers
            continue
        yield f'line:{n}', cells, headers, section, []


def rows(handle, ext, name, metadata):
    if ext in {'.md', '.txt', '.csv', '.url'}:
        # UTF8 first; legacy Thai fallback kept explicit.
        handle.seek(0)
        data = handle.read()
        try:
            text = data.decode('utf-8-sig')
            metadata['encoding'] = 'utf-8-sig'
        except UnicodeDecodeError:
            text = data.decode('cp874', errors='replace')
            metadata['encoding'] = 'cp874_with_replacement'
        yield from text_rows(io.StringIO(text))
    elif ext in {'.xlsx', '.xlsm'}:
        handle.seek(0)
        signature = handle.read(8)
        handle.seek(0)
        if signature == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
            import olefile
            with olefile.OleFileIO(handle) as ole:
                streams = {p[-1].casefold() for p in ole.listdir()}
            handle.seek(0)
            metadata['file_signature'] = 'OLE compound document'
            if {'encryptedpackage', 'encryptioninfo'} & streams:
                metadata['encryption'] = 'encrypted OOXML; password required'
                raise ValueError('encrypted OOXML workbook; password required; original unchanged')
            if {'workbook','book'} & streams:
                metadata['actual_format'] = 'legacy XLS with XLSX extension'
                yield from rows(handle,'.xls',name,metadata)
                return
            raise ValueError('OLE document has no readable Workbook/Book stream')
        import openpyxl
        wb = openpyxl.load_workbook(handle, read_only=True, data_only=True, keep_links=False)
        metadata['sheets'] = wb.sheetnames
        metadata['formula_policy'] = 'cached_values_only; missing caches unavailable'
        try:
            for sheet in wb:
                sheet.reset_dimensions()
                headers = None
                for n, values in enumerate(sheet.iter_rows(values_only=True), 1):
                    cells = [str(v) if v is not None else '' for v in values]
                    new_headers = headers_for(cells)
                    if new_headers:
                        headers = new_headers
                        continue
                    yield f'sheet:{sheet.title};row:{n}', cells, headers, sheet.title, []
        finally:
            wb.close()
    elif ext == '.xls':
        import xlrd
        wb = xlrd.open_workbook(file_contents=handle.read(), on_demand=True)
        metadata['sheets'] = wb.sheet_names()
        try:
            for sheet in wb.sheets():
                headers = None
                for n in range(sheet.nrows):
                    cells = [str(v) if v is not None else '' for v in sheet.row_values(n)]
                    new_headers = headers_for(cells)
                    if new_headers:
                        headers = new_headers
                        continue
                    yield f'sheet:{sheet.name};row:{n+1}', cells, headers, sheet.name, []
                wb.unload_sheet(sheet.name)
        finally:
            wb.release_resources()
    elif ext == '.xlsb':
        from pyxlsb import open_workbook
        with open_workbook(handle) as wb:
            metadata['sheets'] = wb.sheets
            for sheet_name in wb.sheets:
                headers = None
                with wb.get_sheet(sheet_name) as sheet:
                    for row in sheet.rows():
                        cells = [str(c.v) if c.v is not None else '' for c in row]
                        new_headers = headers_for(cells)
                        if new_headers:
                            headers = new_headers
                            continue
                        yield f'sheet:{sheet_name};row:{row[0].r+1}', cells, headers, sheet_name, []
    elif ext == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(handle, strict=False)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('encrypted PDF requires password')
        metadata['page_count'] = len(reader.pages)
        empty = 0
        for n, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            if not text.strip():
                empty += 1
            for loc, cells, hdr, ctx, notes in text_rows(io.StringIO(text)):
                yield f'page:{n};{loc}', cells, hdr, ctx, notes + ['PDF text order may not preserve table columns; no OCR']
        metadata['pages_without_text'] = empty
        metadata['ocr'] = 'not_available'
    elif ext == '.pptx':
        with zipfile.ZipFile(handle) as archive:
            slides = sorted(n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n))
            metadata['slides'] = len(slides)
            metadata['coverage'] = 'slide text only; embedded images and objects not OCRed'
            for slide in slides:
                tree = ET.fromstring(archive.read(slide))
                text = '\n'.join(e.text or '' for e in tree.iter() if e.tag.endswith('}t'))
                for loc, cells, hdr, ctx, notes in text_rows(io.StringIO(text)):
                    yield f'{slide};{loc}', cells, hdr, ctx, notes
    elif ext == '.msg':
        import olefile
        with olefile.OleFileIO(handle) as ole:
            metadata['coverage'] = 'body text streams only; attachments inventoried but not parsed'
            metadata['attachments'] = [p for p in ole.listdir() if p[0].startswith('__attach')]
            for path in ole.listdir():
                leaf = path[-1]
                if leaf.startswith('__substg1.0_1000'):
                    data = ole.openstream(path).read()
                    text = data.decode('utf-16-le' if leaf.endswith('001F') else 'cp874', errors='replace')
                    for loc, cells, hdr, ctx, notes in text_rows(io.StringIO(text)):
                        yield f'stream:{"/".join(path)};{loc}', cells, hdr, ctx, notes
    elif ext in {'.png', '.jpg', '.jpeg', '.gif', '.bmp'}:
        global OCR
        if OCR is None:
            OCR = WindowsOCR()
        lines = OCR.read(handle, ext)
        metadata.update(ocr='Windows.Media.Ocr', ocr_language='en-US', ocr_line_count=len(lines),
                        coverage='English OCR only; Thai text unavailable; table column positions not reconstructed')
        for n, line in enumerate(lines, 1):
            yield f'ocr:en-US;line:{n}', [line], None, '', ['English OCR candidate; verify against image; numeric prices not inferred']
    else:
        raise NotImplementedError('image OCR not available' if ext in {'.png','.jpg','.jpeg','.gif','.bmp'} else 'no reader for extension')


def emit(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')
    stream.flush()


def source_files(root):
    """Native manifest includes existing long paths and hidden metadata files."""
    if os.name != 'nt':
        yield from (p for p in root.rglob('*') if p.is_file())
        return
    LOCAL.mkdir(parents=True,exist_ok=True)
    helper = LOCAL/'enumerate_sources.ps1'
    helper.write_text("param([string]$RootPath,[string]$OutputPath)\nGet-ChildItem -LiteralPath $RootPath -Recurse -File -Force -ErrorAction Stop | ForEach-Object { $_.FullName | ConvertTo-Json -Compress } | Set-Content -LiteralPath $OutputPath -Encoding UTF8\n",encoding='utf-8-sig')
    manifest = LOCAL/'source_manifest.jsonl'
    subprocess.run([NATIVE_POWERSHELL,'-NoProfile','-File',str(helper),str(root),str(manifest)],check=True,capture_output=True)
    with manifest.open(encoding='utf-8-sig') as saved:
        for line in saved:
            yield Path(json.loads(line))


@contextmanager
def source_input(root, path):
    """Only existing long originals are staged; generated paths stay short."""
    if not path.is_relative_to(root):
        raise ValueError('source path outside approved Pricelist root')
    if len(str(path)) < 250:
        with path.open('rb') as handle:
            yield handle, path.stat().st_size, False
        return
    LOCAL.mkdir(parents=True,exist_ok=True)
    helper = LOCAL/'stage_source.ps1'
    helper.write_text('param([string]$SourcePath,[string]$DestinationPath)\nCopy-Item -LiteralPath $SourcePath -Destination $DestinationPath -Force -ErrorAction Stop\n',encoding='utf-8-sig')
    with tempfile.NamedTemporaryFile(suffix=path.suffix,dir=LOCAL,delete=False) as seed:
        staged = Path(seed.name).resolve()
    local_root = LOCAL.resolve()
    if not staged.is_relative_to(local_root) or len(str(staged)) >= 260:
        raise ValueError('unsafe staging destination')
    try:
        result = subprocess.run([NATIVE_POWERSHELL,'-NoProfile','-File',str(helper),str(path),str(staged)],capture_output=True)
        if result.returncode:
            raise OSError('long-path source staging failed: '+result.stderr.decode('utf-8',errors='replace')[:500])
        with staged.open('rb') as handle:
            yield handle, staged.stat().st_size, True
    finally:
        if staged.is_relative_to(local_root):
            staged.unlink(missing_ok=True)


def scan_file(handle, relative, ext, inventory, records, info, archive_depth=0):
    info['status'] = 'scanned'
    count = 0
    try:
        if ext == '.zip':
            if archive_depth >= 3:
                raise NotImplementedError('nested archive depth limit 3')
            children = 0
            with zipfile.ZipFile(handle) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    children += 1
                    path = relative + '!' + member.filename
                    child = dict(relative_path=path, extension=Path(member.filename).suffix.lower(),
                                 size_bytes=member.file_size, sha256=None, status='pending', error=None,
                                 metadata_json={'archive_parent':relative}, scanned_at=stamp())
                    try:
                        if member.file_size > MAX_MEMBER:
                            raise NotImplementedError('archive member exceeds 100 MB memory safety limit')
                        with tempfile.SpooledTemporaryFile(max_size=1024*1024, dir=LOCAL) as content:
                            digest = hashlib.sha256()
                            with archive.open(member) as member_handle:
                                for block in iter(lambda: member_handle.read(1024*1024), b''):
                                    digest.update(block)
                                    content.write(block)
                            child['sha256'] = digest.hexdigest()
                            content.seek(0)
                            count += scan_file(content, path, child['extension'], inventory, records, child, archive_depth+1)
                    except Exception as exc:
                        child.update(status='unsupported' if isinstance(exc, NotImplementedError) else 'error', error=str(exc))
                    emit(inventory, child)
            info['metadata_json']['archive_member_count'] = children
        else:
            # Manufacturer inference can depend on the source path; cache only identical paths.
            cache_key = hashlib.sha256((info['sha256'] + '|' + ext + '|' + relative).encode('utf-8')).hexdigest()
            cache_dir = LOCAL / 'extract_cache_v4'
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_meta = cache_dir / (cache_key + '.json')
            cache_rows = cache_dir / (cache_key + '.jsonl')
            if cache_meta.exists() and cache_rows.exists():
                cached = json.loads(cache_meta.read_text(encoding='utf-8'))
                parent = info['metadata_json'].get('archive_parent')
                info['metadata_json'].update(cached['metadata_json'])
                if parent:
                    info['metadata_json']['archive_parent'] = parent
                else:
                    info['metadata_json'].pop('archive_parent',None)
                info['status'] = cached['status']
                info['metadata_json']['reused_identical_content'] = True
                with cache_rows.open(encoding='utf-8') as saved:
                    for line in saved:
                        record = json.loads(line)
                        record['source_path'] = relative
                        emit(records, record)
                        count += 1
                return count
            with cache_rows.open('w', encoding='utf-8') as saved:
                tee = TeeRecords(records, saved)
                for locator, cells, headers, context, notes in rows(handle, ext, relative, info['metadata_json']):
                    for record in candidates(relative, locator, cells, headers, relative + ' ' + context, notes):
                        emit(tee, record)
                        count += 1
            if ext in {'.pdf','.pptx','.msg','.png','.jpg','.jpeg','.gif','.bmp'}:
                info['status'] = 'partial'
            info['metadata_json']['candidate_count'] = count
            cache_meta.write_text(json.dumps({'status':info['status'], 'metadata_json':info['metadata_json']}), encoding='utf-8')
        info['metadata_json']['candidate_count'] = count
    except (ImportError, NotImplementedError) as exc:
        info.update(status='unsupported', error=str(exc))
    except Exception as exc:
        info.update(status='error', error=type(exc).__name__ + ': ' + str(exc))
        info['metadata_json']['candidate_count_before_error'] = count
    return count


def extract(source_root, output_dir, limit=None):
    global OCR
    root, out = Path(source_root).resolve(), Path(output_dir).resolve()
    if not root.is_dir():
        raise ValueError('source root is not a readable directory')
    if root == out or root in out.parents:
        raise ValueError('output directory must be outside source root')
    out.mkdir(parents=True, exist_ok=True)
    files = candidates_count = 0
    statuses = {}
    with (out/'inventory.jsonl').open('w', encoding='utf-8') as inventory, (out/'records.jsonl').open('w', encoding='utf-8') as records:
        for path in source_files(root):
            if limit is not None and files >= limit:
                break
            files += 1
            info = dict(relative_path=path.relative_to(root).as_posix(), extension=path.suffix.lower(),
                        size_bytes=None, sha256=None, status='pending', error=None,
                        metadata_json={}, scanned_at=stamp())
            try:
                digest = hashlib.sha256()
                with source_input(root,path) as (handle,size,staged):
                    info['size_bytes'] = size
                    if staged:
                        info['metadata_json']['source_read_method'] = 'native PowerShell read-only copy to short local staging'
                    for block in iter(lambda: handle.read(1024*1024), b''):
                        digest.update(block)
                    info['sha256'] = digest.hexdigest()
                    handle.seek(0)
                    candidates_count += scan_file(handle, info['relative_path'], info['extension'], inventory, records, info)
            except Exception as exc:
                info.update(status='error', error=type(exc).__name__ + ': ' + str(exc))
            emit(inventory, info)
            statuses[info['status']] = statuses.get(info['status'], 0) + 1
            if files % 25 == 0:
                print(json.dumps({'files': files, 'candidates': candidates_count, 'statuses':statuses}), flush=True)
            gc.collect()
    if OCR is not None:
        OCR.close()
        OCR = None
    summary = dict(files=files, candidates=candidates_count, statuses=statuses, completed_at=stamp(),
                   limitations=['Image OCR uses installed English language only; Thai OCR unavailable.',
                                'PDF text extraction only; scanned PDF pages not OCRed; text order may not preserve table columns.',
                                'MSG attachments not parsed; PPTX embedded objects/images not parsed.',
                                'Spreadsheet formulas use cached values only.',
                                'UPS candidate heuristics are conservative and may miss unlabelled product rows.'])
    (out/'extraction_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)
    return summary


@contextmanager
def candidate_source(root, relative):
    """Open only the source requested; archive names are never filesystem targets."""
    with ExitStack() as stack:
        split = re.match(r'^(.*?\.zip)!(.*)$', relative, re.I)
        if not split:
            source, _, _ = stack.enter_context(source_input(root,root/relative))
            yield source
            return
        archive_source, _, _ = stack.enter_context(source_input(root,root/split.group(1)))
        archive = stack.enter_context(zipfile.ZipFile(archive_source))
        member = split.group(2)
        while True:
            nested = re.match(r'^(.*?\.zip)!(.*)$', member, re.I)
            name = nested.group(1) if nested else member
            source = stack.enter_context(archive.open(name))
            spool = stack.enter_context(tempfile.SpooledTemporaryFile(max_size=1024*1024, dir=LOCAL))
            shutil.copyfileobj(source, spool, length=1024*1024)
            spool.seek(0)
            if not nested:
                yield spool
                return
            archive = stack.enter_context(zipfile.ZipFile(spool))
            member = nested.group(2)


def repair_candidates(source_root, output_dir):
    """Final field repair: reread only sources which emitted candidates in pass2."""
    global OCR
    root, out = Path(source_root).resolve(), Path(output_dir).resolve()
    # Scan the small set omitted by Python legacy long-path stat, not the corpus again.
    inventoried = set()
    with (out/'inventory.jsonl').open(encoding='utf-8') as inventory:
        for line in inventory:
            inventoried.add(json.loads(line)['relative_path'])
    missing_count = 0
    with (out/'inventory.jsonl').open('a',encoding='utf-8') as inventory, (out/'records.jsonl').open('a',encoding='utf-8') as records:
        for path in source_files(root):
            relative = path.relative_to(root).as_posix()
            if relative in inventoried:
                continue
            missing_count += 1
            item = dict(relative_path=relative,extension=path.suffix.lower(),size_bytes=None,sha256=None,
                        status='pending',error=None,metadata_json={'reconciled_pass':3},scanned_at=stamp())
            try:
                with source_input(root,path) as (source,size,staged):
                    item['size_bytes'] = size
                    if staged:
                        item['metadata_json']['source_read_method'] = 'native PowerShell read-only copy to short local staging'
                    digest = hashlib.sha256()
                    for block in iter(lambda:source.read(1024*1024),b''):
                        digest.update(block)
                    item['sha256'] = digest.hexdigest()
                    source.seek(0)
                    scan_file(source,relative,item['extension'],inventory,records,item)
            except Exception as exc:
                item.update(status='error',error=type(exc).__name__+': '+str(exc))
            emit(inventory,item)
            print(json.dumps({'reconciled_missing_source':missing_count,'status':item['status']}),flush=True)
    targets = set()
    with (out/'records.jsonl').open(encoding='utf-8') as saved:
        for line in saved:
            targets.add(json.loads(line)['source_path'])
    updates = {}
    with (out/'inventory.jsonl').open(encoding='utf-8') as inventory:
        for line in inventory:
            item = json.loads(line)
            if item['extension'] in {'.xlsx','.xlsm'} and item['status'] == 'error' and 'BadZipFile' in (item.get('error') or ''):
                targets.add(item['relative_path'])
            if item['relative_path'] in targets:
                updates[item['relative_path']] = item
    missing = targets - updates.keys()
    if missing:
        raise ValueError('Candidate sources missing inventory: ' + str(len(missing)))
    rows_path = out/'records.repaired.jsonl'
    count = 0
    with rows_path.open('w', encoding='utf-8') as records, (out/'repair_inventory.jsonl').open('w',encoding='utf-8') as side_inventory:
        for n, relative in enumerate(sorted(targets),1):
            item = updates[relative]
            item.update(status='pending', error=None, scanned_at=stamp())
            item['metadata_json'] = {k:v for k,v in item['metadata_json'].items() if k in {'archive_parent','source_read_method','reconciled_pass'}}
            item['metadata_json']['repaired_pass'] = 3
            try:
                with candidate_source(root,relative) as source:
                    count += scan_file(source, relative, item['extension'], side_inventory, records, item)
            except Exception as exc:
                item.update(status='error', error=type(exc).__name__ + ': ' + str(exc))
            if n % 10 == 0:
                print(json.dumps({'repair_sources':n,'total_sources':len(targets),'candidates':count}), flush=True)
            gc.collect()
    if OCR is not None:
        OCR.close()
        OCR = None
    rewritten = out/'inventory.repaired.jsonl'
    statuses = {}
    with (out/'inventory.jsonl').open(encoding='utf-8') as old, rewritten.open('w',encoding='utf-8') as new:
        for line in old:
            item = json.loads(line)
            final_item = updates.get(item['relative_path'],item)
            archive_path = re.match(r'^(.*\.zip)!',final_item['relative_path'],re.I)
            if archive_path:
                final_item['metadata_json']['archive_parent'] = archive_path.group(1)
            else:
                final_item['metadata_json'].pop('archive_parent',None)
            emit(new, final_item)
            if 'archive_parent' not in final_item['metadata_json']:
                statuses[final_item['status']] = statuses.get(final_item['status'],0) + 1
    rows_path.replace(out/'records.jsonl')
    rewritten.replace(out/'inventory.jsonl')
    summary_path = out/'extraction_summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    summary.update(files=summary['files']+missing_count, candidates=count, statuses=statuses,
                   reconciled_missing_sources=missing_count,repaired_sources=len(targets), repaired_pass=3, repaired_at=stamp())
    summary['limitations'].append('Source prices retain unknown currency unless explicit price unit/header/currency-column evidence exists.')
    summary_path.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'repair_complete':True,'sources':len(targets),'candidates':count}),flush=True)
    return summary


def retry_long_path_sources(source_root, output_dir):
    """Bounded completion of final pass3 using existing PowerShell7 reader."""
    global OCR
    root, out = Path(source_root).resolve(), Path(output_dir).resolve()
    updates = {}
    with (out/'inventory.jsonl').open(encoding='utf-8') as inventory:
        for line in inventory:
            item = json.loads(line)
            if item['status'] == 'error' and 'long-path source staging failed' in (item.get('error') or ''):
                updates[item['relative_path']] = item
    count = 0
    with (out/'records.jsonl').open('a',encoding='utf-8') as records, (out/'long_path_retry_inventory.jsonl').open('w',encoding='utf-8') as side_inventory:
        for n,(relative,item) in enumerate(updates.items(),1):
            item.update(status='pending',error=None,scanned_at=stamp())
            item['metadata_json']['source_read_method'] = 'existing PowerShell7 read-only copy to short local staging'
            try:
                with source_input(root,root/relative) as (source,size,staged):
                    item['size_bytes'] = size
                    digest = hashlib.sha256()
                    for block in iter(lambda:source.read(1024*1024),b''):
                        digest.update(block)
                    item['sha256'] = digest.hexdigest()
                    source.seek(0)
                    count += scan_file(source,relative,item['extension'],side_inventory,records,item)
            except Exception as exc:
                item.update(status='error',error=type(exc).__name__+': '+str(exc))
            print(json.dumps({'long_path_retry':n,'total':len(updates),'status':item['status']}),flush=True)
            gc.collect()
    if OCR is not None:
        OCR.close()
        OCR = None
    rewritten = out/'inventory.long_paths.jsonl'
    statuses = {}
    with (out/'inventory.jsonl').open(encoding='utf-8') as old, rewritten.open('w',encoding='utf-8') as new:
        for line in old:
            item = json.loads(line)
            item = updates.get(item['relative_path'],item)
            emit(new,item)
            if 'archive_parent' not in item['metadata_json']:
                statuses[item['status']] = statuses.get(item['status'],0)+1
    rewritten.replace(out/'inventory.jsonl')
    summary_path = out/'extraction_summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    summary.update(candidates=summary['candidates']+count,statuses=statuses,long_path_retry_sources=len(updates),
                   long_path_retry_reader=NATIVE_POWERSHELL,repaired_at=stamp())
    summary_path.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'long_path_retry_complete':True,'sources':len(updates),'new_candidates':count}),flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--output-dir', default=str(Path(__file__).parent/'local'))
    parser.add_argument('--limit', type=int, help='Diagnostic sample only; omit for full inventory')
    parser.add_argument('--repair-candidates', action='store_true', help='Final field repair of candidate sources only; retains full inventory')
    args = parser.parse_args()
    if args.repair_candidates:
        repair_candidates(args.source_root, args.output_dir)
    else:
        extract(args.source_root, args.output_dir, args.limit)
