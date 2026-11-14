from __future__ import annotations
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlsplit, urljoin
import requests
from vendor.oplus_tracker import tomboy_pro as upstream
from vendor.oplus_tracker import downgrade_query
from .adb import Device
from .catalog import Package, date_from_version, major_from_version
from .common import check_cancel, valid_url

REGIONS = [r for r in upstream.REGION_CONFIG if r != 'sg_host']
CHANNELS = ['正式版', '灰度推送', '尝鲜版', '降级包', '定制版', '通道待确认']

@dataclass
class QueryOptions:
    model: str
    region: str
    ota: str
    components: str = ''
    guid: str = ''
    duid: str = ''
    project: str = ''
    serial: str = ''
    nvid: str = ''
    suffixes: str = 'A,C,F,H,J'
    extra_versions: str = ''
    include_custom: bool = False

    def validate(self):
        if not re.fullmatch(r'[A-Za-z0-9.\-]+', self.model):
            raise ValueError('机型代码无效，请使用例如 PHY110、CPHxxxx 的型号。')
        if self.region not in REGIONS:
            raise ValueError('无法确认固件地区，请在查询参数中选择地区。')
        if self.nvid and not re.fullmatch(r'\d{8}', self.nvid):
            raise ValueError('NVID 必须为 8 位数字。')
        for name in ('guid', 'duid'):
            if getattr(self, name) and not re.fullmatch(r'[0-9a-fA-F]{64}', getattr(self,name)):
                raise ValueError(name.upper() + ' 必须为 64 位十六进制字符。')
        if not re.fullmatch(r'[A-Za-z](?:\s*,\s*[A-Za-z])*', self.suffixes):
            raise ValueError('分支应使用 A,C,F,H,J 这样的逗号分隔字母。')
        if self.ota and not self.ota.startswith(self.model + '_'):
            raise ValueError('完整 OTA 版本与当前机型不一致。')
        for version in re.split(r'[,\n]+', self.extra_versions):
            if version.strip() and not version.strip().startswith(self.model + '_'):
                raise ValueError('补充版本必须属于当前机型：' + version.strip())

def build_queries(options: QueryOptions, baselines=()):
    options.validate()
    seeds = [options.model + '_11.' + s.strip().upper() for s in options.suffixes.split(',')]
    seeds += [v.strip() for v in re.split(r'[,\n]+', options.extra_versions) if v.strip()]
    if options.ota:
        seeds.append(options.ota)
    seeds = list(dict.fromkeys(seeds))
    jobs = []
    for seed in seeds:
        for channel, mode, recruit, pre, genshin in [
            ('正式版','manual',0,'0','0'), ('正式版','client_auto',0,'0','0'),
            ('正式版','server_auto',0,'0','0'), ('灰度推送','taste',1,'0','0'),
            ('尝鲜版','taste',0,'0','0')]:
            ota, model = upstream.process_ota_version(seed, options.region, genshin, pre, None)
            cfg = upstream.QueryConfig(ota, model, options.region, mode, options.guid or '0'*64,
                                       nvid=options.nvid or None, recruitID=recruit)
            jobs.append((channel,cfg))
        if options.guid:
            ota, model = upstream.process_ota_version(seed,options.region,'0','1',None)
            jobs.append(('尝鲜版', upstream.QueryConfig(ota,model,options.region,'taste',options.guid,pre='1',nvid=options.nvid or None)))
        if options.include_custom:
            for genshin in ('1','2'):
                ota, model = upstream.process_ota_version(seed,options.region,genshin,'0',None)
                jobs.append(('定制版',upstream.QueryConfig(ota,model,options.region,'manual',options.guid or '0'*64,genshin=genshin,nvid=options.nvid or None)))
    # Each delta request uses the component set belonging to THAT source OTA.
    # A latest-installed device alone cannot discover historical source->target pairs.
    profiles=[]
    if options.ota and options.components:
        profiles.append({'model':options.model,'region':options.region,'ota':options.ota,
                         'components':options.components,'nvid':options.nvid})
    profiles.extend(baselines)
    seen_profiles=set()
    for profile in profiles:
        if profile.get('model')!=options.model or profile.get('region')!=options.region:continue
        if profile.get('nvid') and options.nvid and profile['nvid']!=options.nvid:continue
        ota=profile.get('ota','');components=profile.get('components','')
        if not ota.startswith(options.model+'_') or not components:continue
        canonical=tuple(sorted((p['componentName'],p['componentVersion']) for p in upstream.parse_components(components)))
        key=(ota,canonical,profile.get('nvid') or options.nvid)
        if key in seen_profiles:continue
        seen_profiles.add(key)
        ota,model=upstream.process_ota_version(ota,options.region,'0','0',None)
        # Supplying a device GUID switches upstream to v6. Keep the public v3
        # queries as well, so qualification queries do not hide public deltas.
        identities=list(dict.fromkeys([options.guid or '0'*64,'0'*64]))
        for guid in identities:
            for channel,mode,recruit in [('正式版','manual',0),('正式版','client_auto',0),('正式版','server_auto',0),('灰度推送','taste',1),('尝鲜版','taste',0)]:
                jobs.append((channel,upstream.QueryConfig(ota,model,options.region,mode,guid,
                               components_input=components,nvid=profile.get('nvid') or options.nvid or None,recruitID=recruit)))
    return jobs

def integer(value):
    try:
        return int(value or 0)
    except (ValueError,TypeError):
        return 0

def normalize_result(result, cfg, channel, options):
    data = result.data or {}
    raw = data.get('raw_body', {})
    requested_channel=channel
    if cfg.genshin!='0':
        channel,release_evidence='定制版','YS / Ovt 专用请求'
    elif raw.get('isGray') is True or str(raw.get('status','')).lower() in ('gray','grayscale','grey'):
        channel,release_evidence='灰度推送','服务器灰度字段'
    elif cfg.pre=='1' or raw.get('isRecruit') is True or 'PRE_' in str(data.get('ota_version','')):
        channel,release_evidence='尝鲜版','PRE 或服务器招募字段'
    elif raw.get('status')=='published' and raw.get('isRecruit') is False:
        channel,release_evidence='正式版','服务器 published，isRecruit=false'
    else:
        channel,release_evidence='通道待确认',f'请求 {requested_channel}/{cfg.mode}；服务器未提供明确发布性质'
    published_ts=integer(raw.get('publishedTime'))
    pub = datetime.fromtimestamp(published_ts/1000,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') if published_ts else date_from_version(str(data.get('ota_version') or ''))
    date_source = '服务器发布时间（UTC+8）' if published_ts else '固件构建时间（非推送日期）' if pub else '服务器未提供'
    records = []
    raw_comps = {c.get('componentName'):c for c in raw.get('components',[]) if isinstance(c,dict)}
    for comp in result.components or []:
        rc = raw_comps.get(comp.name, {})
        packet = rc.get('componentPackets') or {}
        version = data.get('version') or comp.version
        ota = data.get('ota_version')
        if ota in ('N/A', 'Unknown', None, ''):
            ota = comp.version
        # Server metadata is preferred. Component-query intent alone is not
        # evidence that a returned package is incremental.
        hints = str(packet.get('type', packet.get('packageType',''))).lower()
        base = next((str(packet.get(k) or rc.get(k) or raw.get(k)) for k in
                     ('sourceVersion','baseVersion','fromVersion','oldVersion') if packet.get(k) or rc.get(k) or raw.get(k)), '')
        if base or hints in ('0','delta','incremental','diff') or packet.get('isIncremental') is True:
            kind, evidence = '增量包', '服务器包元数据'
            base = base or cfg.ota_version
        elif hints in ('1','full','complete') or packet.get('isIncremental') is False:
            kind, evidence = '全量包', '服务器包元数据'
        elif cfg.components_input:
            kind, evidence = '待核验', '组件查询返回；读取 payload 后确认全量/增量'
            base = cfg.ota_version
        else:
            kind, evidence = '全量包', '无组件基线的全量查询；解包时再次核验 payload'
        comp_pub = pub or date_from_version(ota)
        comp_date_source = date_source if pub else ('固件构建时间（非推送日期）' if comp_pub else '服务器未提供')
        url = comp.link if comp.link and comp.link != 'N/A' else comp.auto_url
        if not url or not url.startswith(('http://','https://')):
            continue
        recipe = asdict(cfg)
        recipe.pop('guid',None)
        records.append(Package(options.model,options.region,channel,kind,version,ota,comp.name,url,
                               comp.original_link if comp.original_link != 'N/A' else url,
                               integer(comp.size),comp_pub,comp_date_source,major_from_version(raw.get('realOsVersion') or raw.get('colorOSVersion') or version),base,
                               comp.md5 if re.fullmatch(r'[a-fA-F0-9]{32}', comp.md5 or '') else '',
                               str(packet.get('sha256','')) if re.fullmatch(r'[a-fA-F0-9]{64}',str(packet.get('sha256',''))) else '',
                               recipe=recipe,evidence=evidence,release_evidence=release_evidence,
                               public_query=not cfg.guid or cfg.guid=='0'*64))

    # OPEX 模块与热修复补丁包解析
    opex_data = raw.get('opex') if isinstance(raw.get('opex'), dict) else {}
    opex_version = opex_data.get('opexVersionName')
    opex_content = opex_data.get('content', '')
    opex_packages = opex_data.get('opexPackage', [])
    if isinstance(opex_packages, list) and opex_packages:
        for pkg in opex_packages:
            if not isinstance(pkg, dict): continue
            code = pkg.get('businessCode') or 'opex'
            info = pkg.get('info') if isinstance(pkg.get('info'), dict) else {}
            url = info.get('manualUrl') or info.get('autoUrl') or pkg.get('url') or pkg.get('autoUrl')
            if not url or not url.startswith(('http://','https://')): continue
            orig_url = pkg.get('originalUrl') or url
            size = integer(info.get('zipSize') or info.get('imgSize') or pkg.get('size'))
            md5_val = str(info.get('md5') or pkg.get('md5') or '')
            sha256_val = str(info.get('zipHash') or info.get('imgHash') or pkg.get('zipHash') or '')
            patch_ver = opex_version or (f"{data.get('version')} {code}" if data.get('version') else code)
            base_ver = data.get('version') or cfg.ota_version
            evidence_desc = f'OPEX 模块补丁 ({code})' + (f' · {opex_content}' if opex_content else '')
            recipe = asdict(cfg); recipe.pop('guid', None)
            records.append(Package(
                options.model, options.region, channel, '补丁包',
                patch_ver, data.get('ota_version') or cfg.ota_version, code, url,
                orig_url, size, pub, date_source,
                major_from_version(raw.get('realOsVersion') or raw.get('colorOSVersion') or patch_ver),
                f'适用底包: {base_ver}' if base_ver else '',
                md5_val if re.fullmatch(r'[a-fA-F0-9]{32}', md5_val) else '',
                sha256_val if re.fullmatch(r'[a-fA-F0-9]{64}', sha256_val) else '',
                recipe=recipe, evidence=evidence_desc, release_evidence=release_evidence,
                public_query=not cfg.guid or cfg.guid=='0'*64
            ))
    elif result.opex_list:
        for opex in result.opex_list:
            url = getattr(opex, 'auto_url', None) or getattr(opex, 'link', None)
            if not url or not url.startswith(('http://','https://')): continue
            code = getattr(opex, 'business_code', 'opex')
            patch_ver = getattr(opex, 'version_name', '') or (f"{data.get('version')} {code}" if data.get('version') else code)
            sha256_val = getattr(opex, 'zip_hash', '') or ''
            md5_val = getattr(opex, 'md5', '') or ''
            orig_url = getattr(opex, 'original_link', url) or url
            recipe = asdict(cfg); recipe.pop('guid', None)
            records.append(Package(
                options.model, options.region, channel, '补丁包',
                patch_ver, data.get('ota_version') or cfg.ota_version, code, url,
                orig_url, integer(getattr(opex, 'size', 0)), pub, date_source,
                major_from_version(raw.get('realOsVersion') or raw.get('colorOSVersion') or patch_ver),
                f'适用底包: {data.get("version") or cfg.ota_version}',
                md5_val if re.fullmatch(r'[a-fA-F0-9]{32}', md5_val) else '',
                sha256_val if re.fullmatch(r'[a-fA-F0-9]{64}', sha256_val) else '',
                recipe=recipe, evidence=f'OPEX 模块补丁 ({code})', release_evidence=release_evidence,
                public_query=not cfg.guid or cfg.guid=='0'*64
            ))
    return records

def query_all(options, cancel, progress, log, emit, cached=(), baselines=()):
    jobs = build_queries(options,baselines)
    # Previously discovered versions seed subsequent queries, making history grow
    # without claiming the update API is a complete historical index.
    seen = {(c, cfg.ota_version, cfg.mode, cfg.components_input) for c,cfg in jobs}
    for p in cached:
        if p.recipe and p.channel != '降级包':
            cfg = upstream.QueryConfig(**(p.recipe | {'guid':'0'*64 if p.public_query else options.guid or '0'*64}))
            key=(p.channel,cfg.ota_version,cfg.mode,cfg.components_input)
            if key not in seen and len(jobs)<100:
                jobs.append((p.channel,cfg)); seen.add(key)
    total = len(jobs) + (1 if options.duid and options.region in ('cn','cn_cmcc') else 0)
    count, failures, limited, fake_version = 0,0,0,None
    inspected={}
    for i,(channel,cfg) in enumerate(jobs):
        check_cancel(cancel)
        label='增量候选 / 基线' if cfg.components_input else channel
        progress(i,total,f'查询 {label} · {cfg.ota_version} · {cfg.mode}')
        result = upstream.query_update(cfg)
        if not result.success and result.response_code == 2004 and options.region == 'in' and not cfg.model.endswith('IN'):
            check_cancel(cancel)
            cfg = replace(cfg,model=cfg.model+'IN')
            result = upstream.query_update(cfg)
        if not cfg.components_input and not result.success and result.response_code == 2004 and cfg.mode == 'taste' and fake_version and cfg.ota_version != fake_version:
            check_cancel(cancel)
            cfg = replace(cfg,ota_version=fake_version)
            result = upstream.query_update(cfg)
        if result.success:
            fake = (result.data or {}).get('fake_ota_version')
            if fake and fake != 'N/A':
                fake_version = fake
            packages=normalize_result(result,cfg,channel,options)
            for package in packages:
                check_cancel(cancel)
                key=package.md5 or urlsplit(package.url).path
                if key not in inspected:
                    try:
                        from .payload import inspect
                        inspected[key]=inspect(package.url,cancel)
                    except Exception as e:
                        inspected[key]=None
                        log(f'{package.component}：未能核验 payload 类型（{str(e)[:120]}）')
                info=inspected[key]
                if info:
                    refine_package(package,info)
            emit(packages)
            count += len(packages)
            log(f'{channel} / {cfg.ota_version}：返回 {len(packages)} 个组件包')
            limited=0
        elif result.response_code == 308:
            limited += 1
            log('服务器限流，等待后继续。')
            if limited >= 2:
                log('连续限流，已停止本轮查询；保留已取得的记录。')
                break
            if cancel.wait(5):
                check_cancel(cancel)
        else:
            failures += result.response_code not in (2004,204,2200)
            friendly = {2004:'当前查询无可见更新',204:'设备不在测试名单',2200:'设备不在测试名单'}.get(result.response_code,result.error or '请求失败')
            log(f'{channel} / {cfg.ota_version}：{result.response_code} {friendly[:220]}')
        if cancel.wait(.15):
            check_cancel(cancel)
    if options.region in ('cn','cn_cmcc') and options.duid:
        check_cancel(cancel)
        if not re.fullmatch(r'\d{5}', options.project) or not options.serial:
            log('降级包未查询：需要 5 位项目号及设备 SN。')
        else:
            progress(total-1,total,'查询官方降级包')
            result=downgrade_query.query_downgrade(options.ota,options.project,options.serial,options.duid,cmcc=int(options.region=='cn_cmcc'))
            packages=normalize_downgrade(result,options)
            emit(packages);count+=len(packages)
            log(f'降级包：{len(packages)} 个；{result.get("error") or "查询成功"}')
    elif options.region in ('cn','cn_cmcc'):
        log('降级包未查询：缺少真实 DUID，可在“查询参数”补填（拨号 *#6776# 查看）。')
    if not options.guid:
        log('未提供 GUID：已查询公开 taste 通道，PRE 受限尝鲜查询需在参数中补填真实 GUID。')
    delta_bases={cfg.ota_version for _,cfg in jobs if cfg.components_input}
    if delta_bases:
        log(f'本轮按 {len(delta_bases)} 个匹配组件基线查询增量；无结果只表示这些基线当前没有可见返回，不代表机型没有历史增量包。')
    progress(total,total,f'查询完成：本轮返回 {count} 条包记录（含重复）；网络或服务器异常 {failures} 次。')
    return count

def refine_package(package, info):
    package.kind='增量包' if info.is_delta else '全量包'
    package.evidence='已读取 payload manifest 核验'
    if info.is_delta:
        manifest_base=info.metadata.get('pre-build-incremental','')
        if package.base_version and manifest_base and manifest_base not in package.base_version:
            package.base_version+=f'（构建 ID: {manifest_base}）'
        else:
            package.base_version=package.base_version or manifest_base or '未标明；必须与 manifest 中基线哈希一致'
    else:package.base_version=''
    display=info.metadata.get('os_version') or ('ColorOS'+info.metadata['display_os_version'] if info.metadata.get('display_os_version') else '') or info.metadata.get('version_name') or info.metadata.get('post-build-incremental','')
    major=major_from_version(display)
    if major!='未知' and not re.search(r'_11\.[A-Z]',display):package.major=major
    if not package.published:
        ts=integer(info.metadata.get('post-timestamp'))
        if ts:
            package.published=datetime.fromtimestamp(ts,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            package.date_source='固件构建时间（非推送日期）'
    return package

def normalize_downgrade(result, options):
    records=[]
    for p in result.get('packages') or []:
        if not p.get('downloadUrl'):
            continue
        ota=p.get('otaVersion','未知')
        version=p.get('colorosVersion') or ota
        pub=p.get('publishTime') or p.get('publishedTime')
        if isinstance(pub,(int,float)) or (isinstance(pub,str) and pub.isdigit()):
            ts=int(pub);pub=datetime.fromtimestamp(ts/1000 if ts>10**11 else ts,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        records.append(Package(options.model,options.region,'降级包','全量包',version,ota,'System',p['downloadUrl'],p['downloadUrl'],
                      integer(p.get('fileSize')),str(pub or date_from_version(ota)),
                      '服务器发布时间' if pub else '固件构建时间（非推送日期）',major_from_version('_'+version),
                      md5=p.get('fileMd5',''),recipe={'downgrade':True},evidence='官方降级接口'))
    return records

def refresh_package(package, options):
    if package.recipe.get('downgrade'):
        if not options.duid:
            raise ValueError('刷新降级包需要在参数中补填真实 DUID。')
        result=downgrade_query.query_downgrade(options.ota,options.project,options.serial,options.duid,cmcc=int(options.region=='cn_cmcc'))
        records=normalize_downgrade(result,options)
    else:
        cfg=upstream.QueryConfig(**(package.recipe | {'guid':'0'*64 if package.public_query else options.guid or '0'*64}))
        result=upstream.query_update(cfg)
        if not result.success:
            raise RuntimeError(f'刷新失败，接口 {result.response_code}：{result.error}')
        records=normalize_result(result,cfg,package.channel,options)
    match=next((p for p in records if p.ota==package.ota and p.component==package.component and
                (not package.md5 or p.md5==package.md5)),None)
    if not match:
        raise RuntimeError('该版本已不在接口当前返回结果中，无法生成新的签名链接。历史记录已保留。')
    # Keep payload-confirmed classification and baseline identity stable.
    match.kind,match.base_version,match.evidence=package.kind,package.base_version,package.evidence
    return match

def resolve_url(url):
    valid_url(url)
    if 'downloadCheck?' not in url and '/download?' not in url:
        return url
    with requests.get(url,headers={'userId':'oplus-ota|00000001','Range':'bytes=0-0'},stream=True,allow_redirects=False,timeout=(8,20)) as r:
        if r.status_code in (301,302,303,307,308) and r.headers.get('Location'):
            return valid_url(urljoin(url,r.headers['Location']))
        if '/download?' in url:
            raise RuntimeError('此下载接口要求设备 PKI 授权。普通请求无法解析，请使用设备官方授权获得的可下载链接。')
        if r.status_code>=400:
            raise RuntimeError(f'链接解析 HTTP {r.status_code}；请刷新签名链接。')
        return url

def expiry_text(url):
    q=parse_qs(urlsplit(url).query)
    for key in ('Expires','expires','x-oss-expires'):
        try:
            ts=int(q[key][0])
            if ts<10**9:
                return '签名采用相对有效期，请及时使用'
            dt=datetime.fromtimestamp(ts,timezone(timedelta(hours=8)))
            return dt.strftime('%Y-%m-%d %H:%M:%S UTC+8')+('（已过期）' if ts<time.time() else '')
        except (KeyError,ValueError,OverflowError,OSError):
            pass
    return '服务器未标明；刷新后及时使用'
