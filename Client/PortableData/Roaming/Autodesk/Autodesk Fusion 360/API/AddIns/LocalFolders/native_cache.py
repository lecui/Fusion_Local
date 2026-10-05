import ctypes as C, pathlib, hashlib, os, sys

def _loaded_path(name):
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.GetModuleHandleW.argtypes=[C.c_wchar_p];kernel.GetModuleHandleW.restype=C.c_void_p
    kernel.GetModuleFileNameW.argtypes=[C.c_void_p,C.c_wchar_p,C.c_uint32];kernel.GetModuleFileNameW.restype=C.c_uint32
    handle=kernel.GetModuleHandleW(name)
    if not handle:return None
    buffer=C.create_unicode_buffer(32768);length=kernel.GetModuleFileNameW(handle,buffer,len(buffer))
    return pathlib.Path(buffer.value) if length else None

def _find_bin():
    loaded=_loaded_path('NsCloudBrowser10.dll')
    if loaded and loaded.is_file():return loaded.parent
    candidates=[]
    process=_loaded_path(None)
    if process:candidates.append(process.parent)
    if sys.executable:candidates.append(pathlib.Path(sys.executable).resolve().parent)
    here=pathlib.Path(__file__).resolve()
    candidates.extend(parent/'bin' for parent in here.parents)
    configured=os.environ.get('FUSION_PORTABLE_ROOT','').strip()
    if configured:candidates.insert(0,pathlib.Path(configured).expanduser()/'bin')
    seen=set()
    for candidate in candidates:
        try:candidate=candidate.resolve()
        except OSError:continue
        key=str(candidate).casefold()
        if key in seen:continue
        seen.add(key)
        if (candidate/'NsCloudBrowser10.dll').is_file() and (candidate/'NsBaseCore10.dll').is_file():return candidate
    raise FileNotFoundError('Не найдена папка bin запущенного Fusion Portable')

class NativeCache:
    def __init__(self):
        bin_dir=_find_bin()
        expected='401b59dcd36750168ea78ad1983aed051e5133596fa1a2c73d316d6eb7b68796'
        if hashlib.sha256((bin_dir/'NsCloudBrowser10.dll').read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Unsupported Fusion binary; local folder patch disabled')
        self.dll=C.WinDLL(str(bin_dir/'NsCloudBrowser10.dll'))
        self.base=self.dll._handle
        self.get=C.CFUNCTYPE(C.c_void_p)(self.base+0x22e240)
        self.save=C.CFUNCTYPE(None,C.c_void_p)(self.base+0x233840)
        self.copy_path=C.CFUNCTYPE(C.c_void_p,C.c_void_p,C.c_void_p)(self.base+0xf8f0)
        self.load_fn=C.CFUNCTYPE(C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p)(self.base+0x22e0e0)
        self.ns=C.WinDLL(str(bin_dir/'NsBaseCore10.dll'))
        self.status_destroy=getattr(self.ns,'??1Status@Ns@@UEAA@XZ');self.status_destroy.argtypes=[C.c_void_p];self.status_destroy.restype=None
        self.status_ok=getattr(self.ns,'?isOk@Status@Ns@@QEBA_NXZ');self.status_ok.argtypes=[C.c_void_p];self.status_ok.restype=C.c_bool
        self.ptr=self.get()
        if not self.ptr:raise RuntimeError('Cache not initialized')
        get_browser=getattr(self.dll,'?_get@Browser@CloudBrowser@Ns@@CAPEAV123@XZ')
        get_browser.restype=C.c_void_p
        is_online=getattr(self.dll,'?isOnline@Browser@CloudBrowser@Ns@@QEAA_NXZ')
        is_online.argtypes=[C.c_void_p];is_online.restype=C.c_bool
        if is_online(get_browser()):raise RuntimeError('Local folders require offline mode')
    def path(self):
        addr=self.ptr+0xe0
        n=C.c_uint64.from_address(addr+16).value;cap=C.c_uint64.from_address(addr+24).value
        if not 1<n<32768:raise RuntimeError('Unexpected cache path')
        ptr=C.c_void_p.from_address(addr).value if cap>7 else addr
        return pathlib.Path(C.wstring_at(ptr,n))
    def flush(self):
        self.save(self.ptr)
        import CacheMaintenance
        path=self.path();data=path.read_bytes();canonical=CacheMaintenance.compact(data)
        if canonical!=data:
            temporary=path.with_suffix('.compact.tmp');temporary.write_bytes(canonical)
            os.replace(temporary,path)
    def load(self,path):
        import CacheMaintenance,os
        path=pathlib.Path(path)
        original=path.read_bytes();canonical=CacheMaintenance.compact(original)
        view=CacheMaintenance.reload_view(canonical)
        recovery=path.with_suffix('.reload-recovery')
        if recovery.exists():raise RuntimeError('Cache reload recovery file exists; restore it before continuing')
        # Persist a complete recoverable catalog before exposing the selective
        # reload view. Geometry files are never involved in this transaction.
        with recovery.open('xb') as out:
            out.write(canonical);out.flush();os.fsync(out.fileno())
        temporary=path.with_suffix('.reload-view')
        try:
            temporary.write_bytes(view);os.replace(temporary,path)
            self._load_native(path)
        finally:
            os.replace(recovery,path)
            temporary.unlink(missing_ok=True)

    def _load_native(self,path):
        text=str(path);chars=C.create_unicode_buffer(text)
        reference=(C.c_uint64*4)(C.addressof(chars),0,len(text),len(text))
        native=(C.c_uint64*4)()
        self.copy_path(native,reference)
        status=C.create_string_buffer(128)
        self.load_fn(self.ptr,status,native)
        try:
            if not self.status_ok(status):raise RuntimeError('Fusion rejected cache')
        finally:self.status_destroy(status)
