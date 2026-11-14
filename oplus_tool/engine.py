from __future__ import annotations
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from .common import RESOURCE, HOME, CREATE_NO_WINDOW, Cancelled, check_cancel, file_hash, atomic_json
from .payload import SSUT_UNSUPPORTED,XISHANG_SUPPORTED,stage_subset

def engine_path(engine,custom=''):
    filename='payload-dumper-go.exe' if engine=='ssut' else 'payload-dumper-xishang.exe'
    candidates=([Path(custom)] if custom else [])+[HOME/'tools'/filename,RESOURCE/'tools'/filename]
    for path in candidates:
        if path.is_file():return path
    raise FileNotFoundError(f'未找到 {engine} 引擎。请在设置中指定路径，或运行 setup_tools.py。')

def run_process(args,cancel,progress,log,parts):
    proc=subprocess.Popen([str(a) for a in args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL,creationflags=CREATE_NO_WINDOW)
    lines=queue.Queue();tail=[];percents={p:0 for p in parts}
    def read():
        try:
            for line in iter(proc.stdout.readline,b''):
                lines.put(line.decode('utf-8','replace'))
        finally:lines.put(None)
    worker=threading.Thread(target=read,daemon=True);worker.start()
    ended=False
    try:
        while not ended or proc.poll() is None:
            check_cancel(cancel)
            try:line=lines.get(timeout=.15)
            except queue.Empty:continue
            if line is None:ended=True;continue
            line=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',line).strip()
            if not line:continue
            tail.append(line);tail=tail[-30:]
            m=re.fullmatch(r'([A-Za-z0-9_\-]+):(\d+)',line)
            if m and m[1] in percents:
                percents[m[1]]=min(100,int(m[2]))
                progress(sum(percents.values()),100*len(parts),f'提取 {m[1]} · {m[2]}%')
            else:log(line[:1400])
        code=proc.wait()
        if code:raise RuntimeError(f'解包引擎退出码 {code}\n'+ '\n'.join(tail[-8:]))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        proc.stdout.close();worker.join(timeout=2)

def extract(info,names,output,old_dir,cancel,progress,log,engine='auto',paths=None):
    names=list(dict.fromkeys(names));selected=[p for p in info.partitions if p.name in names]
    if not names or len(selected)!=len(names):raise ValueError('请选择包内实际存在的一个或多个镜像。')
    output=Path(output).expanduser().resolve();output.mkdir(parents=True,exist_ok=True)
    if info.remote and info.compressed:raise ValueError('此远程 ZIP 的 payload 被压缩。请下载完整固件后本地解包。')
    for part in selected:
        if (output/(part.name+'.img')).exists():raise FileExistsError(f'{part.name}.img 已存在，请选择一个新输出目录。')
    needed=sum(p.size for p in selected)
    staging=sum(p.download_size for p in selected) if info.remote else 0
    if shutil.disk_usage(output).free<needed+staging+128*1024*1024:
        raise ValueError('输出磁盘可用空间不足，需要镜像空间及所选分区的临时数据空间。')
    needs_source=any(p.needs_source for p in selected)
    if needs_source:
        if not old_dir:raise ValueError('所选分区为增量操作，需要对应基线版本的原始镜像目录。')
        old_dir=Path(old_dir).expanduser().resolve()
        if old_dir==output:raise ValueError('基线镜像目录不能与输出目录相同。')
        for part in selected:
            if not part.needs_source:continue
            old=old_dir/(part.name+'.img')
            if not old.is_file():raise ValueError('缺少基线镜像：'+str(old))
            if part.old_size and old.stat().st_size!=part.old_size:
                raise ValueError(part.name+' 基线镜像大小与 manifest 不匹配。')
            if part.old_sha256 and file_hash(old,'sha256',cancel,progress)!=part.old_sha256:
                raise ValueError(part.name+' 基线镜像 SHA256 不匹配，请选择正确的基线版本。')
    types={op['type'] for p in selected for op in p.operations}
    automatic=engine=='auto'
    if automatic:engine='xishang' if info.remote and not needs_source and types<=XISHANG_SUPPORTED else 'ssut'
    if engine=='ssut' and types & SSUT_UNSUPPORTED:
        from .payload import OP_NAMES
        raise ValueError('所选分区含当前 ssut 不支持的操作：'+', '.join(OP_NAMES[t] for t in sorted(types&SSUT_UNSUPPORTED))+'。请选择其他分区或全量包。')
    if engine=='xishang' and not types<=XISHANG_SUPPORTED:
        raise ValueError('所选分区含 xishang 未支持的操作，请选择 ssut 引擎或其他分区。')
    binary=engine_path(engine,(paths or {}).get(engine,''))
    log(f'使用 {engine} 引擎；输出镜像在独立临时目录生成，完成校验后发布。')
    with tempfile.TemporaryDirectory(prefix='.oplus-extract-',dir=output) as tmp:
        tmp=Path(tmp);input_source=info.source;image_dir=tmp/'images';image_dir.mkdir()
        if engine=='ssut' and info.remote:
            input_source=stage_subset(info,names,tmp/'subset.payload.bin',cancel,progress)
        if engine=='ssut':
            args=[binary,'-m','-c','4','-o',image_dir,'-p',','.join(names)]
            if needs_source:args+=['-old',old_dir]
            args+=[input_source]
        else:
            args=[binary,'extract-diff' if needs_source else 'extract',input_source,'-o',image_dir,'-p',','.join(names)]
            if needs_source:args+=['--old',old_dir,'-w','4']
            else:args+=['--workers','4','--http-workers','4']
        try:
            run_process(args,cancel,progress,log,names)
        except RuntimeError as e:
            if not automatic or engine!='xishang':raise
            check_cancel(cancel)
            log('xishang 未能完成，自动切换 ssut 按需提取：'+str(e).splitlines()[0])
            engine='ssut';binary=engine_path('ssut',(paths or {}).get('ssut',''))
            image_dir=tmp/'fallback-images';image_dir.mkdir()
            input_source=stage_subset(info,names,tmp/'fallback.payload.bin',cancel,progress) if info.remote else info.source
            args=[binary,'-m','-c','4','-o',image_dir,'-p',','.join(names),input_source]
            run_process(args,cancel,progress,log,names)
        report=[]
        for part in selected:
            check_cancel(cancel)
            image=image_dir/(part.name+'.img')
            if not image.is_file():raise RuntimeError('引擎未生成所选镜像：'+part.name)
            if part.size and image.stat().st_size!=part.size:raise RuntimeError(part.name+' 输出大小不匹配，拒绝发布。')
            actual=file_hash(image,'sha256',cancel,progress)
            if part.sha256 and actual!=part.sha256:raise RuntimeError(part.name+' SHA256 不匹配，拒绝发布。')
            if not part.sha256:log(part.name+'：manifest 未提供镜像哈希；已计算 SHA256，无法与官方值比对。')
            report.append({'partition':part.name,'size':image.stat().st_size,'sha256':actual,'manifest_verified':bool(part.sha256)})
        # Verify all files before publishing any of them. Refuse late collisions.
        for part in selected:
            if (output/(part.name+'.img')).exists():raise FileExistsError('输出文件在任务期间已被创建，请选择新目录。')
        for part in selected:os.replace(image_dir/(part.name+'.img'),output/(part.name+'.img'))
        atomic_json(output/'extraction-report.json',{'engine':engine,'source_type':'remote' if info.remote else 'local',
                    'metadata':info.metadata,'images':report})
    progress(1,1,f'完成：{len(selected)} 个镜像已提取并核验')
    return output
