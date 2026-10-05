"""Extend the existing native CloudStatusMenu; no palette or command interception."""
import pathlib,json,logging,ctypes,os
HERE=pathlib.Path(__file__).resolve().parent
original_icon=None
last_color=None
native=None
export_native=None
export_callback=None
export_response=None

def update(app,online):
    global original_icon,last_color
    definition=app.userInterface.commandDefinitions.itemById('CloudStatusCommand')
    if not definition:raise RuntimeError('CloudStatusCommand не найден')
    if original_icon is None:original_icon=definition.resourceFolder
    definition.resourceFolder=str(HERE/'ServerIcons'/('online' if online else 'offline'))
    if last_color!=online:
        logging.getLogger('FusionPrivateServer').info('Clock color set: %s','green' if online else 'red')
        last_color=online

def start(app):
    global native,export_native,export_callback
    import CloudConnection
    from native_cache import _find_bin
    logger=logging.getLogger('FusionPrivateServer')
    if not logger.handlers:
        handler=logging.FileHandler(CloudConnection.ROOT/'panel.log',encoding='utf-8')
        logger.addHandler(handler);logger.setLevel(logging.INFO)
    # Remove the obsolete palette, including one restored from Fusion's UI state.
    old=app.userInterface.palettes.itemById('PortablePrivateServerPanel')
    if old:old.deleteMe()
    try:
        config=json.loads((HERE.parent/'HomeModels/config.json').read_text(encoding='utf-8'))
        config['log']=str(CloudConnection.ROOT/'native-menu.log')
        with os.add_dll_directory(str(_find_bin())):
            native=ctypes.CDLL(str(HERE/'NativeServerMenu.dll'))
        native.startNativeServerMenu.argtypes=[ctypes.c_char_p]
        native.startNativeServerMenu.restype=ctypes.c_int
        native.stopNativeServerMenu.argtypes=[]
        native.stopNativeServerMenu.restype=None
        result=native.startNativeServerMenu(json.dumps(config).encode('utf-8'))
        if result:raise RuntimeError('Не удалось расширить штатное меню: код '+str(result))
        logger.info('Existing native menu extension loaded; command interception disabled')
        with os.add_dll_directory(str(_find_bin())):
            export_native=ctypes.CDLL(str(HERE/'ProjectExportUI3.dll'))
        callback_type=ctypes.CFUNCTYPE(ctypes.c_void_p,ctypes.c_char_p)
        def export_call(raw):
            global export_response
            try:
                import ProjectExport,SyncBridge,SyncGuard
                request=json.loads(raw.decode('utf-8'))
                result=ProjectExport.execute(app,request)
                if request.get('operation')=='project-export-save':
                    SyncGuard.saved(result['id'])
                    if SyncBridge.runtime:
                        SyncBridge.runtime.catalog_revision+=1
                        SyncBridge.runtime.enqueue_sync()
            except Exception as exc:
                logger.exception('Project export failed')
                result={'ok':False,'error':str(exc)}
            export_response=ctypes.create_string_buffer(json.dumps(result,ensure_ascii=True).encode('utf-8'))
            return ctypes.addressof(export_response)
        export_callback=callback_type(export_call)
        export_native.startProjectExport.argtypes=[ctypes.c_char_p,callback_type]
        export_native.startProjectExport.restype=ctypes.c_int
        export_native.stopProjectExport.argtypes=[]
        export_native.stopProjectExport.restype=None
        if export_native.startProjectExport(json.dumps(config).encode('utf-8'),export_callback):
            raise RuntimeError('Не удалось подключить сохранение экспорта в проект')
    except Exception:
        logger.exception('Cannot load native menu extension')
        app.userInterface.messageBox('Не удалось загрузить управление сервером в штатное меню. Подробности сохранены в FusionPortableCloud/panel.log.','Подключение к серверу')
    try:update(app,False)
    except Exception:logger.exception('Cannot initialize clock color')

def stop(app):
    if export_native:export_native.stopProjectExport()
    if native:native.stopNativeServerMenu()
    if original_icon is not None:
        definition=app.userInterface.commandDefinitions.itemById('CloudStatusCommand')
        if definition:definition.resourceFolder=original_icon
