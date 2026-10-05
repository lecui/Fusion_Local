"""Use catalog display names for F3D archives opened as new Fusion documents."""
import pathlib
from model_operations import identity

def choose_name(items,path,current):
    if path:
        matches=[i for i in items if identity(i['id'])==identity(path)]
    else:
        matches=[i for i in items if pathlib.PureWindowsPath(i['id']).name==current or pathlib.PureWindowsPath(i['id']).stem==current]
    if len(matches)!=1:return None
    item=matches[0];physical=pathlib.PureWindowsPath(item['id'])
    if current not in (physical.name,physical.stem):return None
    return item['name']

def restore(document,path=''):
    if not document:return False
    if not path and document.dataFile:
        path=document.dataFile.id
    import HomeModelsBridge
    name=choose_name(HomeModelsBridge.execute({'operation':'list'})['items'],path,document.name)
    if not name:return False
    old=document.name
    import logging
    logger=logging.getLogger("FusionPrivateServer")
    logger.info("Restoring model name: saved=%s, source=%s, display=%s",document.isSaved,old,name)
    if document.isSaved:
        data_file=document.dataFile
        if not data_file:
            raise RuntimeError('Сохранённый локальный документ не предоставляет DataFile: '+old)
        # Saved document names are owned by DataFile; Document.name is read-only.
        if not data_file._set_name(name):
            raise RuntimeError('Fusion отклонил изменение имени DataFile: '+old)
    else:
        if not document._set_name(name):raise RuntimeError('Fusion отклонил имя документа: '+old)
    # Only replace an inherited technical root name, not a user-assigned component name.
    import adsk.fusion
    design=adsk.fusion.Design.cast(document.products.itemByProductType('DesignProductType'))
    if design and design.rootComponent.name==old:design.rootComponent.name=name
    logger.info("Model name after restoration: %s",document.name)
    return True
