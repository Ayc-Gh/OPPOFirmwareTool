from __future__ import annotations
import argparse
import json
import os
import sys
import threading
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,
    QFormLayout,QLabel,QPushButton,QComboBox,QLineEdit,QPlainTextEdit,QTabWidget,QGroupBox,
    QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QFileDialog,QMessageBox,
    QProgressBar,QCheckBox,QSplitter,QScrollArea,QFrame,QSizePolicy)
from oplus_tool import __version__
from oplus_tool import adb,tracker,payload,engine,models
from oplus_tool.catalog import Catalog
from oplus_tool.common import DATA,HOME,RESOURCE,Cancelled,atomic_json,size_text,safe_name
from oplus_tool.network import download

class JobSignals(QObject):
    progress=Signal(object,object,str)
    log=Signal(str)
    packages=Signal(object)
    result=Signal(object)
    error=Signal(str)
    finished=Signal()

class Job(QRunnable):
    def __init__(self,fn):
        super().__init__();self.fn=fn;self.signals=JobSignals();self.cancel=threading.Event()
    def run(self):
        try:self.signals.result.emit(self.fn(self.cancel,self.signals))
        except Cancelled as e:self.signals.log.emit(str(e));self.signals.progress.emit(0,1,str(e))
        except Exception as e:
            self.signals.log.emit(type(e).__name__+': '+str(e));self.signals.error.emit(str(e))
        finally:self.signals.finished.emit()

class CopyEdit(QLineEdit):
    copied=Signal(str)
    def mousePressEvent(self,event):
        super().mousePressEvent(event)
        if self.text():
            QApplication.clipboard().setText(self.text());self.copied.emit('下载链接已复制')

STYLE='''
QMainWindow,QWidget {background:#f3f6f5;color:#172e29;font-family:"Microsoft YaHei UI";font-size:13px;}
QGroupBox {background:white;border:1px solid #dce6e1;border-radius:10px;margin-top:14px;padding:18px 12px 10px;font-weight:600;}
QGroupBox::title {subcontrol-origin:margin;left:16px;padding:0 5px;}
QLabel {background:transparent;}
QLabel#title {font-size:27px;font-weight:700;color:#123b2f;}
QLabel#subtitle {color:#61756b;font-size:12px;}
QLabel#value {font-size:17px;font-weight:600;}
QLabel#hint {color:#61756b;}
QLineEdit,QComboBox,QPlainTextEdit {background:white;border:1px solid #cddbd3;border-radius:6px;padding:7px;selection-background-color:#0d815d;}
QLineEdit:focus,QComboBox:focus {border:1px solid #0d815d;}
QLineEdit:read-only {background:#f6f9f7;}
QPushButton {background:white;border:1px solid #ccdbd2;border-radius:6px;padding:8px 14px;}
QPushButton:hover {background:#e8f2ed;border-color:#75a58b;}
QPushButton#primary {background:#087f5b;color:white;border:1px solid #087f5b;font-weight:600;}
QPushButton#primary:hover {background:#066748;}
QPushButton:disabled {background:#edf1ee;color:#9aa8a0;border-color:#e0e7e2;}
QTabWidget::pane {border:0;}
QTabBar::tab {padding:13px 25px;margin:7px 6px 8px 0;border-radius:6px;background:#e5ede8;color:#567264;}
QTabBar::tab:selected {background:#d5eee0;color:#07573d;font-weight:700;}
QTableWidget {background:white;alternate-background-color:#f5f9f6;border:1px solid #dce6df;border-radius:7px;gridline-color:#edf2ee;selection-background-color:#d7efdf;selection-color:#123a29;}
QTableWidget::item {padding:6px;}
QHeaderView::section {background:#eaf1ed;color:#496354;border:0;padding:9px;text-align:left;}
QProgressBar {border:0;background:#e0eae3;border-radius:4px;text-align:center;min-height:18px;max-height:18px;font-size:11px;}
QProgressBar::chunk {background:#0c9366;border-radius:4px;}
QCheckBox {spacing:6px;background:transparent;}
QScrollArea {border:0;}
QFrame#taskPanel {background:white;border:1px solid #dce6df;border-radius:7px;}
'''

def button(text,fn,primary=False):
    b=QPushButton(text);b.clicked.connect(fn)
    if primary:b.setObjectName('primary')
    return b

def row(*widgets):
    w=QWidget();layout=QHBoxLayout(w);layout.setContentsMargins(0,0,0,0)
    for item in widgets:
        if isinstance(item,int):layout.addStretch(item)
        else:layout.addWidget(item)
    return w

def table(headers):
    t=QTableWidget(0,len(headers));t.setHorizontalHeaderLabels(headers)
    t.setAlternatingRowColors(True);t.setShowGrid(False);t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectRows);t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers);t.verticalHeader().setDefaultSectionSize(37)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t

class Window(QMainWindow):
    def __init__(self,no_auto=False):
        super().__init__();self.setWindowTitle('OPPO 固件工作台');self.resize(1380,960);self.setMinimumSize(1060,740)
        self.catalog=Catalog();self.devices=[];self.packages=[];self.visible_packages=[];self.online_packages=[]
        self.selected_package=None;self.info=None;self.current_device=None;self.job=None;self.scan_job=None;self.closing=False
        self.root_identifiers={}
        self.model_profiles=models.load_profiles();self.catalog_scope=None
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(3);self.busy_buttons=[];self.params={}
        self.settings={}
        try:self.settings=json.loads((DATA/'settings.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        self.no_auto=no_auto
        root=QWidget();self.setCentralWidget(root);layout=QVBoxLayout(root);layout.setContentsMargins(25,18,25,18);layout.setSpacing(9)
        title=QLabel('OPPO 固件工作台');title.setObjectName('title')
        sub=QLabel(f'设备连接  /  固件获取  /  镜像提取     ·     v{__version__}');sub.setObjectName('subtitle')
        layout.addWidget(row(title,1,sub))
        self.target_mode=QComboBox();self.target_mode.addItems(['ADB 已连接设备','手动选择机型'])
        self.device_combo=QComboBox();self.device_combo.setMinimumWidth(310);self.device_combo.currentIndexChanged.connect(self.select_device)
        self.scan_button=button('刷新 ADB 设备',self.scan_devices)
        self.auto_check=QCheckBox('连接后自动查询固件');self.auto_check.setChecked(self.settings.get('auto_query',True))
        self.connection=QLabel('正在检查已连接设备…');self.connection.setObjectName('hint')
        layout.addWidget(row(self.target_mode,self.device_combo,self.scan_button,self.auto_check,1,self.connection))
        self.model_brand=QComboBox()
        for label,value in [('OPPO','OPPO'),('一加 OnePlus','OnePlus'),('真我 realme','realme')]:self.model_brand.addItem(label,value)
        self.model_search=QLineEdit();self.model_search.setPlaceholderText('搜索名称 / 型号 / 设备代号');self.model_search.setClearButtonEnabled(True)
        self.model_search.setMinimumWidth(190);self.model_search.setMaximumWidth(300)
        self.model_combo=QComboBox();self.model_combo.setMinimumWidth(320);self.model_combo.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.model_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);self.model_combo.setMinimumContentsLength(28)
        self.model_combo.setToolTip('公开机型目录；条目不代表官方当前有可见固件。目录未收录的型号可选择“自定义机型”。')
        self.apply_model_button=button('填入机型信息',self.apply_manual_model)
        self.manual_row=row(QLabel('机型'),self.model_brand,self.model_search,self.model_combo,self.apply_model_button)
        layout.addWidget(self.manual_row);self.manual_row.hide()
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        self.build_devices();self.build_firmware();self.build_extract()
        self.status=QLabel('就绪');self.status.setWordWrap(True);self.status.setTextFormat(Qt.PlainText)
        self.status.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred)
        self.status.setMinimumHeight(self.status.fontMetrics().lineSpacing()*2+6)
        self.status.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        self.cancel_button=button('取消当前任务',self.cancel_job);self.cancel_button.setEnabled(False)
        self.progress=QProgressBar();self.progress.setRange(0,100);self.progress.setValue(0)
        self.log_toggle=QCheckBox('显示任务日志');self.log_toggle.toggled.connect(lambda x:self.log_view.setVisible(x))
        self.task_panel=QFrame();self.task_panel.setObjectName('taskPanel')
        task_layout=QHBoxLayout(self.task_panel);task_layout.setContentsMargins(12,6,12,6);task_layout.setSpacing(14)
        task_layout.addWidget(self.status,1);task_layout.addWidget(self.log_toggle,0,Qt.AlignVCenter);task_layout.addWidget(self.cancel_button,0,Qt.AlignVCenter)
        self.progress.setMinimumHeight(18);self.progress.setMaximumHeight(18)
        layout.addWidget(self.task_panel);layout.addWidget(self.progress)
        self.log_view=QPlainTextEdit();self.log_view.setReadOnly(True);self.log_view.setMaximumBlockCount(700);self.log_view.setFixedHeight(135);self.log_view.hide();layout.addWidget(self.log_view)
        self.statusBar().showMessage('查询记录保存在 '+str(DATA))
        self.catalog_timer=QTimer(self);self.catalog_timer.setSingleShot(True);self.catalog_timer.setInterval(180)
        self.catalog_timer.timeout.connect(self.load_catalog)
        self.params['model'].textChanged.connect(lambda:self.catalog_timer.start())
        self.params['region'].currentIndexChanged.connect(lambda:self.catalog_timer.start())
        self.params['region'].activated.connect(self.region_changed)
        self.target_mode.currentIndexChanged.connect(self.target_mode_changed)
        self.model_brand.currentIndexChanged.connect(self.filter_models);self.model_search.textChanged.connect(self.filter_models)
        self.model_combo.activated.connect(self.apply_manual_model);self.filter_models()
        QTimer.singleShot(200,self.scan_devices)

    def build_devices(self):
        area=QScrollArea();area.setWidgetResizable(True);page=QWidget();area.setWidget(page);lay=QVBoxLayout(page)
        grid=QGridLayout();self.device_labels={}
        for i,(key,label) in enumerate([('name','设备名称'),('model','机型 / 设备代号'),('serial','ADB 序列号'),('firmware','当前系统固件'),('slot','当前 A/B 槽位'),('region','地区 / ColorOS')]):
            box=QGroupBox(label);bl=QVBoxLayout(box);v=QLabel('—');v.setWordWrap(True);v.setTextInteractionFlags(Qt.TextSelectableByMouse);v.setObjectName('value');bl.addWidget(v);grid.addWidget(box,i//3,i%3);self.device_labels[key]=v
        lay.addLayout(grid)
        query=QGroupBox('查询参数 · ADB 读取或手选机型填入，可补充');qgrid=QGridLayout(query)
        fields=[('model','OTA 机型'),('region','固件地区'),('ota','完整 OTA 版本'),('nvid','NV 运营商 ID'),('project','项目号 PrjNum'),('serial','设备 SN'),('guid','GUID（尝鲜 PRE）'),('duid','DUID（官方降级）'),('suffixes','查询分支'),('extra_versions','补充完整 OTA 版本')]
        for i,(key,label) in enumerate(fields):
            edit=QComboBox() if key=='region' else QLineEdit()
            if key=='region':edit.addItem('请选择地区','');[edit.addItem(r,r) for r in tracker.REGIONS]
            if key=='suffixes':edit.setText('A,C,F,H,J')
            if key in ('guid','duid'):edit.setEchoMode(QLineEdit.PasswordEchoOnEdit);edit.setPlaceholderText('无法自动读取时手动填写；仅保存在内存中')
            if key=='extra_versions':edit.setPlaceholderText('逗号分隔，用于补充历史版本的查询种子')
            if key=='ota':edit.setPlaceholderText('例如 PHY110_11.H.10_3100_202610291237')
            self.params[key]=edit
            r,c=i//2,(i%2)*2;qgrid.addWidget(QLabel(label),r,c);qgrid.addWidget(edit,r,c+1)
        self.components=QLineEdit();self.components.setPlaceholderText('从 ro.oplus.version.* 自动获取；可按 name:version,... 修正')
        qgrid.addWidget(QLabel('增量组件基线'),5,0);qgrid.addWidget(self.components,5,1,1,3)
        self.root_button=button('尝试 root 获取 GUID 和 DUID',self.try_root_identifiers)
        self.root_button.setToolTip('仅点击此按钮时请求 su；只读取当前设备的系统配置与 OTA 缓存。')
        self.root_button.setEnabled(False);self.busy_buttons.append(self.root_button)
        root_hint=QLabel('默认不使用 root；读取结果仅保存在本次运行的内存中。');root_hint.setObjectName('hint');root_hint.setWordWrap(True)
        qgrid.addWidget(self.root_button,6,0,1,2);qgrid.addWidget(root_hint,6,2,1,2)
        self.custom_check=QCheckBox('同时查询原神定制版（YS / Ovt）');qgrid.addWidget(self.custom_check,7,0,1,2)
        self.query_button=button('查询全部可见固件',self.query_firmware,True);self.busy_buttons.append(self.query_button)
        qgrid.addWidget(self.query_button,7,3);lay.addWidget(query)
        hint=QLabel('发布类型优先采用服务器字段；未明确标记的包列入“通道待确认”。官方接口不提供完整历史目录，工作台会累积查询历史。\n'
                    'GUID / DUID 不能从普通序列号可靠推导。降级包需要真实 DUID、项目号及 SN；可在手机拨号 *#6776# 查看。\n'
                    '系统包支持含 payload.bin 的 OTA ZIP；SOTA / OPEX 属于模块或应用更新，不作为完整系统固件混入。')
        hint.setWordWrap(True);hint.setObjectName('hint');lay.addWidget(hint)
        settings=QGroupBox('本机工具路径');form=QFormLayout(settings);self.tool_edits={}
        for key,label in [('adb','ADB'),('ssut','ssut 解包引擎'),('xishang','xishang 在线引擎')]:
            edit=QLineEdit(self.settings.get(key,''));edit.setPlaceholderText('留空使用随程序附带的工具')
            self.tool_edits[key]=edit
            form.addRow(label,row(edit,button('浏览',lambda checked=False,k=key:self.choose_tool(k))))
        form.addRow('',button('保存工具设置',self.save_settings));lay.addWidget(settings);lay.addStretch()
        self.tabs.addTab(area,'01  设备与参数')

    def build_firmware(self):
        page=QWidget();lay=QVBoxLayout(page);lay.setContentsMargins(0,5,0,0)
        self.channel_filter=QComboBox();self.major_filter=QComboBox();self.kind_filter=QComboBox()
        self.channel_filter.addItems(['全部类型']+tracker.CHANNELS);self.major_filter.addItem('全部 ColorOS')
        self.kind_filter.addItems(['全部包','全量包','增量包','待核验'])
        self.search=QLineEdit();self.search.setPlaceholderText('搜索版本 / OTA 代码 / 组件 / 基线版本…');self.search.setClearButtonEnabled(True)
        refresh=button('刷新固件列表',self.query_firmware,True);export=button('导出查询记录',self.export_catalog)
        self.busy_buttons.extend([refresh,export])
        lay.addWidget(row(self.channel_filter,self.major_filter,self.kind_filter,self.search,refresh,export))
        self.count=QLabel('连接设备或手选机型后，可查询固件并在线提取镜像。');self.count.setObjectName('hint');lay.addWidget(self.count)
        self.package_table=table(['包版本（单击复制 OTA 名称）','包类型','组件','大小','更新日期 ↓','ColorOS','增量安装基线'])
        self.package_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        self.package_table.horizontalHeader().setStretchLastSection(False)
        self.package_table.horizontalHeader().setSectionResizeMode(6,QHeaderView.Interactive)
        self.package_table.setColumnWidth(6,260);lay.addWidget(self.package_table,1)
        self.package_table.itemSelectionChanged.connect(self.select_package);self.package_table.cellClicked.connect(self.copy_package_name)
        detail=QGroupBox('所选固件');dl=QVBoxLayout(detail)
        self.package_detail=QLabel('请选择一个固件包。');self.package_detail.setWordWrap(True);self.package_detail.setTextInteractionFlags(Qt.TextSelectableByMouse);dl.addWidget(self.package_detail)
        self.url_edit=CopyEdit();self.url_edit.setReadOnly(True);self.url_edit.setPlaceholderText('选择固件后显示下载链接；单击即可复制');self.url_edit.copied.connect(self.toast);dl.addWidget(self.url_edit)
        self.refresh_link_button=button('刷新此包链接',self.refresh_link)
        self.download_button=button('下载完整固件',self.download_package,True)
        self.online_button=button('在线选择镜像',self.use_online_package)
        self.busy_buttons.extend([self.refresh_link_button,self.download_button,self.online_button])
        dl.addWidget(row(button('复制包名',lambda:self.copy_text(self.selected_package.ota if self.selected_package else '')),button('复制链接',lambda:self.copy_text(self.url_edit.text())),self.refresh_link_button,1,self.online_button,self.download_button));lay.addWidget(detail)
        for combo in (self.channel_filter,self.major_filter,self.kind_filter):combo.currentIndexChanged.connect(self.filter_packages)
        self.search.textChanged.connect(self.filter_packages)
        self.tabs.addTab(page,'02  固件获取')

    def build_extract(self):
        page=QWidget();lay=QVBoxLayout(page);lay.setContentsMargins(0,5,0,0)
        box=QGroupBox('镜像来源');form=QVBoxLayout(box)
        self.source_mode=QComboBox();self.source_mode.addItems(['本地固件包','在线固件包 / 链接'])
        self.online_channel=QComboBox();self.online_channel.addItems(['全部类型']+tracker.CHANNELS)
        self.online_major=QComboBox();self.online_major.addItem('全部 ColorOS')
        self.online_search=QLineEdit();self.online_search.setPlaceholderText('搜索在线版本…');self.online_search.setClearButtonEnabled(True)
        form.addWidget(row(QLabel('来源'),self.source_mode,self.online_channel,self.online_major,self.online_search))
        self.online_combo=QComboBox();self.online_combo.setMinimumWidth(600);form.addWidget(row(QLabel('在线固件'),self.online_combo))
        self.source_edit=QLineEdit();self.source_edit.setPlaceholderText('选择本地 OTA ZIP / payload.bin，或粘贴可下载的在线链接')
        self.file_button=button('选择本地文件',self.choose_local)
        self.inspect_button=button('读取镜像列表',self.inspect_source,True);self.busy_buttons.append(self.inspect_button)
        form.addWidget(row(self.source_edit,self.file_button,self.inspect_button))
        self.extract_hint=QLabel('读取后展示包内全部分区，可勾选单个、多个或全部镜像。在线提取要求服务器支持 HTTP Range。')
        self.extract_hint.setWordWrap(True);self.extract_hint.setObjectName('hint');form.addWidget(self.extract_hint);lay.addWidget(box)
        self.image_search=QLineEdit();self.image_search.setPlaceholderText('筛选镜像名称…');self.image_search.setClearButtonEnabled(True)
        self.selection_count=QLabel('已选 0 个')
        lay.addWidget(row(self.image_search,self.selection_count,button('全选全部镜像',lambda:self.check_images(True)),button('全部取消',lambda:self.check_images(False)),button('反选',self.invert_images)))
        self.image_table=table(['选择','镜像名称','镜像大小','依赖基线','操作类型','所需包数据'])
        self.image_table.horizontalHeader().setSectionResizeMode(4,QHeaderView.Stretch);self.image_table.horizontalHeader().setStretchLastSection(False)
        self.image_table.itemChanged.connect(self.update_selection_count);self.image_table.cellClicked.connect(self.toggle_image)
        lay.addWidget(self.image_table,1)
        output=QGroupBox('输出与解包');grid=QGridLayout(output)
        self.output_edit=QLineEdit(self.settings.get('output_dir',str(HOME/'images')))
        self.old_edit=QLineEdit();self.old_edit.setPlaceholderText('增量分区需要对应基线版本的 *.img 目录；全量包无需填写')
        self.engine_combo=QComboBox();self.engine_combo.addItems(['自动选择引擎','ssut（本地 / 在线按需 / 增量）','xishang（在线 / 本地）'])
        grid.addWidget(QLabel('镜像输出目录'),0,0);grid.addWidget(self.output_edit,0,1);grid.addWidget(button('选择目录',lambda:self.choose_dir(self.output_edit)),0,2)
        grid.addWidget(QLabel('基线镜像目录'),1,0);grid.addWidget(self.old_edit,1,1);grid.addWidget(button('选择目录',lambda:self.choose_dir(self.old_edit)),1,2)
        self.extract_button=button('提取所选镜像',self.extract_images,True);self.busy_buttons.append(self.extract_button)
        grid.addWidget(QLabel('解包引擎'),2,0);grid.addWidget(self.engine_combo,2,1);grid.addWidget(self.extract_button,2,2)
        grid.addWidget(button('打开输出目录',lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_edit.text()))),3,2)
        lay.addWidget(output)
        self.source_mode.currentIndexChanged.connect(self.source_mode_changed)
        self.online_combo.currentIndexChanged.connect(self.select_online)
        self.online_channel.currentIndexChanged.connect(self.filter_online);self.online_major.currentIndexChanged.connect(self.filter_online)
        self.online_search.textChanged.connect(self.filter_online);self.source_edit.textChanged.connect(self.invalidate_info)
        self.image_search.textChanged.connect(self.filter_images)
        self.source_mode_changed();self.tabs.addTab(page,'03  镜像解包')

    def toast(self,text):self.statusBar().showMessage(text,7000)
    def log(self,text):self.log_view.appendPlainText(datetime.now().strftime('%H:%M:%S')+'  '+text)
    def copy_text(self,text):
        if text:QApplication.clipboard().setText(text);self.toast('已复制到剪贴板')
    def copy_package_name(self,r,c):
        if c==0 and 0<=r<len(self.visible_packages):self.copy_text(self.visible_packages[r].ota)
    def choose_tool(self,key):
        path,_=QFileDialog.getOpenFileName(self,'选择 '+key,'','可执行程序 (*.exe);;所有文件 (*)')
        if path:self.tool_edits[key].setText(path)
    def choose_dir(self,edit):
        path=QFileDialog.getExistingDirectory(self,'选择目录',edit.text() or str(HOME))
        if path:edit.setText(path)
    def save_settings(self):
        self.settings.update({k:v.text().strip() for k,v in self.tool_edits.items()})
        self.settings['auto_query']=self.auto_check.isChecked();self.settings['output_dir']=self.output_edit.text()
        atomic_json(DATA/'settings.json',self.settings);self.toast('工具设置已保存；GUID / DUID 不写入设置文件')
    def show_error(self,text):
        self.status.setText('任务未完成：'+text.splitlines()[0]);self.log_toggle.setChecked(True)
        QMessageBox.warning(self,'任务未完成',text[:2500])
    def set_progress(self,done,total,text):
        self.status.setText(text)
        if total>0:self.progress.setRange(0,1000);self.progress.setValue(min(1000,int(done/total*1000)))
        else:self.progress.setRange(0,0)
    def start_job(self,fn,result=None,title='正在处理…'):
        if self.job:
            self.toast('当前任务正在执行，可先取消。');return False
        self.job=Job(fn);self.job.signals.progress.connect(self.set_progress);self.job.signals.log.connect(self.log)
        self.job.signals.error.connect(self.show_error);self.job.signals.packages.connect(self.receive_packages)
        if result:self.job.signals.result.connect(result)
        self.job.signals.finished.connect(self.finish_job)
        for b in self.busy_buttons:b.setEnabled(False)
        self.set_target_controls(False);self.scan_button.setEnabled(False);self.cancel_button.setEnabled(True)
        self.status.setText(title);self.progress.setRange(0,0);self.pool.start(self.job);return True
    def finish_job(self):
        self.job=None
        for b in self.busy_buttons:b.setEnabled(True)
        self.set_target_controls(True);self.scan_button.setEnabled(not bool(self.scan_job));self.cancel_button.setEnabled(False)
        self.update_root_button()
        if self.progress.maximum()==0:self.progress.setRange(0,100);self.progress.setValue(0)
        if self.closing:self.close()
    def cancel_job(self):
        if self.job:self.job.cancel.set();self.status.setText('正在取消，等待当前操作退出…')
    def set_target_controls(self,enabled):
        self.target_mode.setEnabled(enabled)
        self.device_combo.setEnabled(enabled and self.target_mode.currentIndex()==0)
        for widget in (self.model_brand,self.model_search,self.model_combo,self.apply_model_button):widget.setEnabled(enabled)
    def filter_models(self,*args):
        old=self.model_combo.currentData();self.model_combo.blockSignals(True);self.model_combo.clear()
        profiles=[p for p in self.model_profiles if p.brand==self.model_brand.currentData() and p.matches(self.model_search.text())]
        self.model_combo.addItem(f'请选择机型（{len(profiles)} 项）',None)
        for p in profiles:
            self.model_combo.addItem(p.label,p.model);self.model_combo.setItemData(self.model_combo.count()-1,p.label,Qt.ToolTipRole)
        self.model_combo.addItem('自定义机型 · 在下方填写 OTA 机型和地区','__custom__')
        self.model_combo.setCurrentIndex(max(0,self.model_combo.findData(old)));self.model_combo.blockSignals(False)
    def clear_target(self):
        self.current_device=None
        for label in self.device_labels.values():label.setText('—')
        for key,edit in self.params.items():
            if key=='region':edit.setCurrentIndex(0)
            elif key=='suffixes':edit.setText('A,C,F,H,J')
            else:edit.clear()
        self.components.clear();self.custom_check.setChecked(False);self.catalog_timer.stop()
        self.load_catalog();self.update_root_button()
    def target_mode_changed(self,index):
        self.manual_row.setVisible(index==1);self.auto_check.setEnabled(index==0)
        self.set_target_controls(not bool(self.job))
        self.clear_target()
        if index==0:
            self.select_device(self.device_combo.currentIndex())
            if not self.current_device:self.status.setText('请刷新并选择 ADB 设备，或切换为“手动选择机型”。')
        elif self.model_combo.currentData():self.apply_manual_model()
        else:self.status.setText('请选择机型；无需连接手机。选择后可调整固件地区，再查询和在线解包。')
    def apply_manual_model(self,*args):
        if self.job or self.target_mode.currentIndex()!=1:return
        code=self.model_combo.currentData()
        if not code:self.toast('请先选择机型，或选择“自定义机型”。');return
        profile=next((p for p in self.model_profiles if p.model==code and p.brand==self.model_brand.currentData()),None)
        self.clear_target()
        if not profile:
            self.device_labels['name'].setText('自定义机型')
            self.status.setText('请填写 OTA 机型和固件地区，再点击“查询全部可见固件”。');self.params['model'].setFocus();return
        self.params['model'].setText(profile.model)
        self.params['region'].setCurrentIndex(max(0,self.params['region'].findData(profile.region)))
        self.region_changed()
        self.device_labels['name'].setText(profile.name)
        self.device_labels['model'].setText(profile.model+' / '+('、'.join(profile.codenames) or '代号未知'))
        self.device_labels['serial'].setText('手选机型 · 未读取设备')
        self.device_labels['firmware'].setText('未连接设备，可补充 OTA / 组件基线')
        self.device_labels['slot'].setText('未读取')
        self.device_labels['region'].setText((profile.region or '请选择固件地区')+' / 版本以查询结果为准')
        self.catalog_timer.stop();self.load_catalog()
        self.status.setText('已填入 '+profile.label+'；'+('可调整地区后查询固件。' if profile.region else '请先选择固件地区，再查询固件。'))
    def scan_devices(self):
        if self.scan_job or self.job:return
        self.scan_button.setEnabled(False);self.connection.setText('正在刷新 ADB…')
        custom=self.tool_edits['adb'].text().strip()
        self.scan_job=Job(lambda c,s:adb.scan(custom));self.scan_job.signals.result.connect(self.devices_found)
        self.scan_job.signals.error.connect(self.show_error);self.scan_job.signals.finished.connect(self.scan_finished);self.update_root_button();self.pool.start(self.scan_job)
    def scan_finished(self):
        self.scan_job=None;self.scan_button.setEnabled(not bool(self.job));self.update_root_button()
    def devices_found(self,result):
        devices,notes=result;old=self.current_device.serial if self.current_device else ''
        self.devices=devices;self.device_combo.blockSignals(True);self.device_combo.clear()
        if not devices:self.device_combo.addItem('未发现已授权的 OPlus ADB 设备')
        for d in devices:self.device_combo.addItem(d.label)
        index=next((i for i,d in enumerate(devices) if d.serial==old),0);self.device_combo.setCurrentIndex(index);self.device_combo.blockSignals(False)
        for note in notes:self.log(note)
        self.connection.setText(f'{len(devices)} 台 OPlus 设备已连接'+(' · 有未授权设备，见日志' if notes else ''))
        if self.target_mode.currentIndex()==1:
            self.update_root_button();return
        if devices:self.select_device(index)
        else:
            self.clear_target()
            self.status.setText('请启用手机 USB 调试并授权；也可切换为“手动选择机型”查询，或直接解包本地固件。')
    def select_device(self,index):
        if self.job or self.target_mode.currentIndex()!=0 or not 0<=index<len(self.devices):return
        self.clear_target()
        d=self.devices[index];self.current_device=d
        values={'name':d.name,'model':d.model+' / '+d.codename,'serial':d.serial,'firmware':d.firmware,'slot':d.slot.upper(),'region':(d.region or '未知')+' / ColorOS '+(d.coloros or '未知')}
        for key,value in values.items():self.device_labels[key].setText(value or '—')
        for key in ('model','ota','nvid','project','guid','duid'):
            value=getattr(d,key)
            if key in ('guid','duid'):value=value or self.root_identifiers.get(d.serial,{}).get(key,'')
            self.params[key].setText(value)
        self.params['serial'].setText(d.hardware_serial)
        self.params['region'].setCurrentIndex(max(0,self.params['region'].findData(d.region)))
        self.components.setText(d.components);self.components.setCursorPosition(0)
        self.catalog.remember_baseline(d.model,d.region,d.ota,d.components,d.nvid)
        self.load_catalog();self.status.setText('已读取设备信息：'+d.name)
        self.update_root_button()
        if self.auto_check.isChecked() and not self.no_auto:
            QTimer.singleShot(100,lambda:self.query_connected_device(d))
    def query_connected_device(self,device):
        if self.target_mode.currentIndex()==0 and self.current_device is device and self.auto_check.isChecked() and not self.job:
            self.query_firmware()
    def update_root_button(self):
        self.root_button.setEnabled(self.target_mode.currentIndex()==0 and bool(self.current_device) and not self.job and not self.scan_job)
    def try_root_identifiers(self):
        if self.job or self.scan_job:
            self.toast('请等待当前任务结束后再尝试 root 读取。');return
        if self.target_mode.currentIndex()!=0 or not self.current_device:
            self.toast('请先选择已连接的 OPlus 设备。');return
        serial=self.current_device.serial;custom=self.tool_edits['adb'].text().strip()
        original={key:self.params[key].text() for key in ('guid','duid')}
        def done(result):
            if not self.current_device or self.current_device.serial!=result.serial:
                self.status.setText('设备已更换，未填入之前设备的 root 读取结果。');return
            filled=[]
            for key in ('guid','duid'):
                value=getattr(result,key)
                if not value:continue
                self.root_identifiers.setdefault(serial,{})[key]=value
                if self.params[key].text()!=original[key]:
                    self.log(f'{key.upper()} 输入在读取期间已更改，保留手动输入。');continue
                self.params[key].setText(value);self.params[key].setCursorPosition(0);filled.append(key.upper())
            for note in result.notes:self.log(note)
            message=('已通过 root 填入 '+'、'.join(filled)+'；仅保存在本次运行内存中。') if filled else 'root 读取结束，未替换已有输入；详情见任务日志。'
            self.set_progress(1,1,message);self.log(message)
            if result.notes:self.log_toggle.setChecked(True)
        self.start_job(lambda cancel,signals:adb.read_root_identifiers(serial,custom,cancel),done,
                       '正在尝试 root 读取 GUID / DUID；如手机弹出授权请求，请在手机上处理。')
    def get_options(self):
        values={k:(v.currentData() if k=='region' else v.text().strip()) for k,v in self.params.items()}
        return tracker.QueryOptions(**values,components=self.components.text().strip(),include_custom=self.custom_check.isChecked())
    def region_changed(self,*args):
        region=self.params['region'].currentData()
        if region:
            _,config=tracker.upstream.get_public_key_for_region(region)
            self.params['nvid'].setText(config['carrier_id'])
        else:self.params['nvid'].clear()
        if self.target_mode.currentIndex()==1:self.device_labels['region'].setText((region or '请选择固件地区')+' / 版本以查询结果为准')
    def load_catalog(self):
        opts=self.get_options();scope=(opts.model,opts.region)
        if scope!=self.catalog_scope:
            self.catalog_scope=scope
            if self.source_mode.currentIndex()==1:
                self.source_edit.clear();self.invalidate_info()
                self.extract_hint.setText('查询机型或地区已更换，请重新选择在线固件并读取镜像。')
        self.packages=self.catalog.list(opts.model,opts.region)
        self.populate_filters();self.filter_packages();self.filter_online()
    def populate_filters(self):
        majors=sorted({p.major for p in self.packages if p.major!='未知'},key=lambda x:int(x) if x.isdigit() else 0,reverse=True)
        if any(p.major=='未知' for p in self.packages):majors.append('未知')
        for combo in (self.major_filter,self.online_major):
            old=combo.currentText();combo.blockSignals(True);combo.clear();combo.addItems(['全部 ColorOS']+majors)
            combo.setCurrentIndex(max(0,combo.findText(old)));combo.blockSignals(False)
    def query_firmware(self):
        try:opts=self.get_options();opts.validate()
        except Exception as e:self.show_error(str(e));return
        self.load_catalog();cached=self.packages[:]
        self.catalog.import_known_baselines(opts.model,opts.region);baselines=self.catalog.baselines(opts.model,opts.region)
        def work(cancel,signals):
            def emit(packages):self.catalog.put(packages);signals.packages.emit(packages)
            return tracker.query_all(opts,cancel,signals.progress.emit,signals.log.emit,emit,cached,baselines)
        self.tabs.setCurrentIndex(1);self.start_job(work,title='正在查询多个更新通道…')
    def receive_packages(self,packages):
        self.load_catalog()
    def filtered(self,channel,major,kind,keyword):
        keyword=keyword.strip().lower()
        return [p for p in self.packages if (channel=='全部类型' or p.channel==channel) and
                (major=='全部 ColorOS' or p.major==major) and (kind=='全部包' or p.kind==kind) and
                (not keyword or keyword in ' '.join([p.version,p.ota,p.component,p.base_version,p.channel,p.kind]).lower())]
    def filter_packages(self):
        selected_id=self.selected_package.id if self.selected_package else ''
        self.visible_packages=self.filtered(self.channel_filter.currentText(),self.major_filter.currentText(),self.kind_filter.currentText(),self.search.text())
        self.package_table.blockSignals(True);self.package_table.setRowCount(len(self.visible_packages));select=-1
        for r,p in enumerate(self.visible_packages):
            values=[p.version+'\n'+p.ota,p.kind,p.component,size_text(p.size),p.published or '未提供',p.major,p.base_version or ('无需基线' if p.kind=='全量包' else '未标明')]
            for c,value in enumerate(values):
                item=QTableWidgetItem(str(value));item.setToolTip(str(value)+(('\n'+p.date_source) if c==4 else ''));self.package_table.setItem(r,c,item)
            if p.id==selected_id:select=r
        self.package_table.verticalHeader().setDefaultSectionSize(54);self.package_table.blockSignals(False)
        self.count.setText(f'显示 {len(self.visible_packages)} / {len(self.packages)} 条记录 · 日期最新在前 · 同版全量/增量分别保留 · 构建日期会在详情中标注')
        if select>=0:self.package_table.selectRow(select)
        elif self.visible_packages:self.package_table.selectRow(0)
        else:self.selected_package=None;self.url_edit.clear();self.package_detail.setText('此条件下暂无固件记录。更改筛选或刷新查询。')
    def select_package(self):
        r=self.package_table.currentRow()
        if not 0<=r<len(self.visible_packages):return
        p=self.visible_packages[r];self.selected_package=p;self.url_edit.setText(p.url)
        self.package_detail.setText(f'{p.channel} · {p.kind} · {p.component} · ColorOS {p.major}\n'
            f'OTA：{p.ota}\n安装基线：{p.base_version or ("无需旧镜像" if p.kind=="全量包" else "尚未确认")}\n'
            f'日期依据：{p.date_source or "未提供"} · 包类型依据：{p.evidence}\n发布性质：{p.release_evidence or p.channel}\n'
            f'MD5：{p.md5 or "未提供"} · 链接有效期：{tracker.expiry_text(p.url)}')
    def refresh_link(self):
        if not self.selected_package:return
        p=self.selected_package;opts=self.get_options();old_id=p.id
        def done(new):
            self.catalog.replace(old_id,new);self.selected_package=new;self.load_catalog();self.status.setText('所选包链接已重新查询')
        self.start_job(lambda c,s:tracker.refresh_package(p,opts),done,'正在重新查询所选固件的签名链接…')
    def download_package(self):
        if not self.selected_package:return
        p=self.selected_package;name=safe_name(p.ota+'_'+p.component+'_'+p.kind)+'.zip'
        dest,_=QFileDialog.getSaveFileName(self,'保存完整固件',str(Path(self.settings.get('download_dir',str(HOME/'downloads')))/name),'固件 ZIP (*.zip);;所有文件 (*)')
        if not dest:return
        self.settings['download_dir']=str(Path(dest).parent)
        def work(c,s):
            url=tracker.resolve_url(p.original_url or p.url)
            return download(url,dest,c,s.progress.emit,p.size,p.md5,p.sha256)
        self.start_job(work,lambda path:self.toast('已下载到 '+str(path)),'正在准备下载；支持断点续传…')
    def export_catalog(self):
        opts=self.get_options();dest,_=QFileDialog.getSaveFileName(self,'导出固件目录',str(HOME/(safe_name(opts.model)+'_firmware.json')),'JSON (*.json)')
        if dest:self.catalog.export(opts.model,opts.region,Path(dest));self.toast('查询记录已导出；临时链接仍受有效期限制')
    def source_mode_changed(self):
        online=self.source_mode.currentIndex()==1
        for widget in (self.online_channel,self.online_major,self.online_search,self.online_combo):widget.setEnabled(online)
        self.file_button.setEnabled(not online)
    def filter_online(self):
        old=self.online_combo.currentData();self.online_combo.blockSignals(True);self.online_combo.clear()
        self.online_combo.addItem('选择在线固件，或在下面粘贴下载链接','')
        self.online_packages=self.filtered(self.online_channel.currentText(),self.online_major.currentText(),'全部包',self.online_search.text())
        for p in self.online_packages:self.online_combo.addItem(f'{p.version} · {p.kind} · {p.component} · {size_text(p.size)} · {p.published or "日期未知"}',p.id)
        self.online_combo.setCurrentIndex(max(0,self.online_combo.findData(old)));self.online_combo.blockSignals(False)
    def select_online(self,index):
        id=self.online_combo.currentData();p=next((p for p in self.online_packages if p.id==id),None)
        if p:
            self.source_mode.setCurrentIndex(1);self.source_edit.setText(p.url)
            self.extract_hint.setText(p.channel+' · '+p.kind+' · 安装基线：'+(p.base_version or '无 / 待读取核验'))
    def use_online_package(self):
        if not self.selected_package:return
        p=self.selected_package;self.source_mode.setCurrentIndex(1)
        self.online_channel.setCurrentIndex(0);self.online_major.setCurrentIndex(0);self.online_search.clear();self.filter_online()
        self.online_combo.setCurrentIndex(max(0,self.online_combo.findData(p.id)))
        self.source_edit.setText(p.url);self.tabs.setCurrentIndex(2)
        self.output_edit.setText(str(Path(self.settings.get('output_dir',str(HOME/'images')))/safe_name(p.ota+'_'+p.kind)))
        self.inspect_source()
    def choose_local(self):
        path,_=QFileDialog.getOpenFileName(self,'选择 OTA ZIP 或 payload.bin',str(HOME),'OTA 包 (*.zip *.bin);;所有文件 (*)')
        if path:self.source_mode.setCurrentIndex(0);self.source_edit.setText(path);self.inspect_source()
    def invalidate_info(self):
        self.info=None;self.image_table.setRowCount(0);self.selection_count.setText('已选 0 个')
    def inspect_source(self):
        source=self.source_edit.text().strip().strip('"')
        if not source:self.toast('请先选择固件文件或在线链接');return
        def work(c,s):
            resolved=tracker.resolve_url(source) if source.startswith(('http://','https://')) else source
            s.progress.emit(0,0,'正在读取 ZIP 目录和 payload manifest…')
            return payload.inspect(resolved,c)
        def done(info):
            # Edits during a background read must not leave a stale source active.
            if self.source_edit.text().strip().strip('"')!=source:
                self.toast('来源已更改，请重新读取镜像列表');return
            self.info=info;self.populate_images()
            id=self.online_combo.currentData();p=next((p for p in self.packages if p.id==id),None)
            if p and source in (p.url,p.original_url):
                old_id=p.id;tracker.refine_package(p,info);self.catalog.replace(old_id,p);self.load_catalog()
            self.status.setText(f'已读取 {len(info.partitions)} 个镜像；'+('包含增量操作' if info.is_delta else '各镜像无需旧版本数据'))
        self.start_job(work,done,'正在读取固件镜像列表…')
    def populate_images(self):
        self.image_table.blockSignals(True);self.image_table.setRowCount(len(self.info.partitions))
        for r,p in enumerate(self.info.partitions):
            check=QTableWidgetItem();check.setFlags(Qt.ItemIsEnabled|Qt.ItemIsUserCheckable|Qt.ItemIsSelectable);check.setCheckState(Qt.Unchecked);self.image_table.setItem(r,0,check)
            for c,value in enumerate([p.name+'.img',size_text(p.size),'需要' if p.needs_source else '无需',p.operations_text,size_text(p.download_size)],1):
                item=QTableWidgetItem(value);item.setToolTip(value);self.image_table.setItem(r,c,item)
        self.image_table.blockSignals(False)
        base=self.info.metadata.get('pre-build-incremental') or self.info.metadata.get('pre-build') or '未标明（以分区基线哈希为准）'
        self.extract_hint.setText(f'{len(self.info.partitions)} 个镜像 · '+('增量包，安装基线：'+base if self.info.is_delta else '全量/独立分区数据，无需旧镜像')+
                                  (' · 远程 ZIP 中 payload 被压缩，需先完整下载' if self.info.remote and self.info.compressed else ''))
        self.filter_images();self.update_selection_count()
    def filter_images(self):
        keyword=self.image_search.text().lower()
        for r in range(self.image_table.rowCount()):self.image_table.setRowHidden(r,keyword not in self.image_table.item(r,1).text().lower())
    def check_images(self,checked):
        self.image_table.blockSignals(True)
        for r in range(self.image_table.rowCount()):self.image_table.item(r,0).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.image_table.blockSignals(False);self.update_selection_count()
    def invert_images(self):
        for r in range(self.image_table.rowCount()):
            item=self.image_table.item(r,0);item.setCheckState(Qt.Unchecked if item.checkState()==Qt.Checked else Qt.Checked)
        self.update_selection_count()
    def toggle_image(self,r,c):
        if c>0:
            item=self.image_table.item(r,0);item.setCheckState(Qt.Unchecked if item.checkState()==Qt.Checked else Qt.Checked)
    def image_names(self):
        if not self.info:return []
        return [p.name for r,p in enumerate(self.info.partitions) if self.image_table.item(r,0).checkState()==Qt.Checked]
    def update_selection_count(self,*args):
        names=self.image_names();self.selection_count.setText(f'已选 {len(names)} / {self.image_table.rowCount()} 个（含筛选隐藏项）')
    def extract_images(self):
        if not self.info:self.toast('请先读取镜像列表');return
        names=self.image_names()
        if not names:self.toast('请先勾选要提取的镜像');return
        output=self.output_edit.text().strip()
        if not output:self.toast('请选择镜像输出目录');return
        info=self.info;old=self.old_edit.text().strip();choice=['auto','ssut','xishang'][self.engine_combo.currentIndex()]
        paths={k:v.text().strip() for k,v in self.tool_edits.items()}
        self.start_job(lambda c,s:engine.extract(info,names,output,old,c,s.progress.emit,s.log.emit,choice,paths),
                       lambda path:self.toast('镜像已保存：'+str(path)),'正在提取所选镜像…')
    def closeEvent(self,event):
        if self.job or self.scan_job:
            self.closing=True
            if self.job:self.job.cancel.set()
            if self.scan_job:self.scan_job.cancel.set()
            self.status.setText('正在停止后台任务，完成后自动关闭…');event.ignore();QTimer.singleShot(500,self.close);return
        self.save_settings();event.accept()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--no-auto-query',action='store_true');parser.add_argument('--self-check',metavar='REPORT_JSON');args=parser.parse_args()
    if args.self_check:os.environ['QT_QPA_PLATFORM']='offscreen'
    app=QApplication(sys.argv[:1]);app.setApplicationName('OPPO 固件工作台');app.setStyle('Fusion');app.setStyleSheet(STYLE)
    window=Window(args.no_auto_query or bool(args.self_check))
    if args.self_check:
        from oplus_tool.selfcheck import run
        try:run(window,args.self_check)
        except Exception:
            atomic_json(Path(args.self_check),{'ok':False,'error':traceback.format_exc()});sys.exit(1)
        return
    window.show();sys.exit(app.exec())

if __name__=='__main__':main()
