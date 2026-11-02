"""Prepare complete JSON/CSV artifacts without blocking Streamlit reruns."""
import csv
import inspect
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

def request_timeout(path):
    def positive(name,default):
        try:value=float(os.environ.get(name,default))
        except (TypeError,ValueError):return default
        return value if math.isfinite(value) and 0<value<=86400 else default
    connect=positive('LITKG_CONNECT_TIMEOUT',10)
    if '/export' in str(path):read=positive('LITKG_EXPORT_READ_TIMEOUT',1200)
    else:read=positive('LITKG_API_READ_TIMEOUT',180)
    return connect,read


class ExportJobs:
    def __init__(self,root=None,max_workers=2,max_pending=4,retention_seconds=3600):
        self.root=Path(root) if root is not None else Path(tempfile.mkdtemp(prefix='litkg-exports-'))
        self.root.mkdir(parents=True,exist_ok=True)
        self.pool=ThreadPoolExecutor(max_workers=max_workers,thread_name_prefix='litkg-export')
        self.max_pending=max_pending;self.retention_seconds=retention_seconds
        self.lock=threading.RLock();self.jobs={}

    def _remove_files(self,job_id):
        directory=(self.root/job_id).resolve()
        directory.relative_to(self.root.resolve())
        if not re.fullmatch(r'[a-f0-9]{32}',job_id):raise ValueError('EXPORT_JOB_ID_INVALID')
        for name in ('data.json','data.csv','data.json.part','data.csv.part'):
            path=directory/name
            if path.is_file():path.unlink()
        if directory.is_dir():
            try:directory.rmdir()
            except OSError:pass

    def _prune(self):
        now=time.monotonic()
        done=sorted((job['updated'],key) for key,job in self.jobs.items() if job['status'] in {'ready','failed'})
        keep={key for _,key in done[-8:]}
        for updated,key in done:
            if now-updated>self.retention_seconds or key not in keep:
                self._remove_files(key);self.jobs.pop(key,None)

    def submit(self,fetch,path,params,kind,basename):
        if kind not in {'claims','facts'}:raise ValueError('EXPORT_KIND_INVALID')
        with self.lock:
            self._prune()
            if sum(j['status'] in {'queued','running'} for j in self.jobs.values())>=self.max_pending:
                raise RuntimeError('EXPORT_QUEUE_FULL')
            job_id=uuid.uuid4().hex
            name=basename if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',basename or '') else kind
            self.jobs[job_id]={'status':'queued','updated':time.monotonic(),'basename':name}
            future=self.pool.submit(self._prepare,job_id,fetch,path,dict(params or {}),kind)
            self.jobs[job_id]['future']=future
            return job_id

    def _prepare(self,job_id,fetch,path,params,kind):
        with self.lock:self.jobs[job_id].update(status='running',updated=time.monotonic())
        try:
            params['format']='json'
            payload=fetch(path,params=params)
            rows=payload.get(kind) if isinstance(payload,dict) else None
            total=payload.get('total') if isinstance(payload,dict) else None
            exported=payload.get('exported_count') if isinstance(payload,dict) else None
            if not isinstance(rows,list) or type(total) is not int or type(exported) is not int or total!=exported or exported!=len(rows):
                raise ValueError('EXPORT_COUNT_MISMATCH')
            if any(not isinstance(row,dict) for row in rows):raise ValueError('EXPORT_ROWS_INVALID')
            directory=self.root/job_id;directory.mkdir(mode=0o700)
            with (directory/'data.json.part').open('w',encoding='utf-8',newline='\n') as handle:
                json.dump(payload,handle,ensure_ascii=False,indent=2)
            fields=list(dict.fromkeys(key for row in rows for key in row)) or ['id','document_id']
            with (directory/'data.csv.part').open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({key:json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else value for key,value in row.items()})
            os.replace(directory/'data.json.part',directory/'data.json')
            os.replace(directory/'data.csv.part',directory/'data.csv')
            document_count=payload.get('document_count')
            if type(document_count) is not int:document_count=None
            with self.lock:self.jobs[job_id].update(status='ready',updated=time.monotonic(),rows=len(rows),total=total,document_count=document_count)
        except Exception as exc:
            # Never retain or display response text, URLs, credentials or exception text.
            code='EXPORT_PREPARE_TIMEOUT' if type(exc).__name__ in {'ReadTimeout','ConnectTimeout','Timeout','TimeoutError'} else 'EXPORT_PREPARE_FAILED'
            with self.lock:self.jobs[job_id].update(status='failed',updated=time.monotonic(),error_code=code)

    def status(self,job_id):
        with self.lock:
            self._prune();job=self.jobs.get(job_id)
            if job is None:return None
            return {key:value for key,value in job.items() if key!='future'}

    def files(self,job_id):
        state=self.status(job_id)
        if state is None or state['status']!='ready':raise ValueError('EXPORT_NOT_READY')
        directory=(self.root/job_id).resolve();directory.relative_to(self.root.resolve())
        return directory/'data.json',directory/'data.csv'


_manager=None
_manager_lock=threading.Lock()

def manager():
    global _manager
    with _manager_lock:
        if _manager is None:_manager=ExportJobs()
        return _manager


def render_prepared_export(st,fetch,*,path,params,state_key,kind,basename,prepare_label):
    jobs=manager();job_key=state_key+'_job_id';error_key=state_key+'_job_error'
    job_id=st.session_state.get(job_key)
    state=jobs.status(job_id) if job_id else None
    pending=state is not None and state['status'] in {'queued','running'}

    def start():
        existing=st.session_state.get(job_key)
        current=jobs.status(existing) if existing else None
        if current and current['status'] in {'queued','running'}:return
        try:
            st.session_state[job_key]=jobs.submit(fetch,path,params,kind,basename)
            st.session_state.pop(error_key,None)
        except Exception:
            st.session_state[error_key]='EXPORT_QUEUE_FULL'

    st.button(prepare_label,key=state_key+'_prepare',on_click=start,disabled=pending)
    if st.session_state.get(error_key):st.error('导出任务较多，请稍后重试。')
    if state is None:
        if job_id:st.info('导出文件已过期，请重新准备。')
        return
    if pending:
        st.info('正在后台准备完整导出，完成后将显示下载按钮。')
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=2000,key=state_key+'_poll')
        except ImportError:
            st.button('刷新导出状态',key=state_key+'_refresh')
        return
    if state['status']=='failed':
        st.error('导出准备超时，请重试。' if state.get('error_code')=='EXPORT_PREPARE_TIMEOUT' else '导出准备失败，请重试。')
        return
    message=f"已准备 {state['rows']} / {state['total']} 条记录"
    if state.get('document_count') is not None:message+=f"，涉及 {state['document_count']} 篇文献"
    st.success(message)
    json_file,csv_file=jobs.files(job_id)
    download_options={}
    try:
        if inspect.signature(st.download_button).parameters['on_click'].default in ('rerun','ignore'):download_options['on_click']='ignore'
    except (KeyError,TypeError,ValueError):pass
    with json_file.open('rb') as handle:
        st.download_button('下载 JSON',handle,file_name=state['basename']+'.json',mime='application/json',key=state_key+'_json',**download_options)
    with csv_file.open('rb') as handle:
        st.download_button('下载 CSV',handle,file_name=state['basename']+'.csv',mime='text/csv',key=state_key+'_csv',**download_options)
