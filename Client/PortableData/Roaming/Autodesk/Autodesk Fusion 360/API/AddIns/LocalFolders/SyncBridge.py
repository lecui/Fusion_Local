"""Native dashboard transport; network checks stay on a worker thread."""
import base64,json,secrets,threading,time
runtime=None
jobs={}
lock=threading.Lock()
def encoded(payload):
 try:
  if runtime is None:raise ValueError('Обработчик моделей ещё не запущен. Перезапустите Fusion.')
  request=json.loads(base64.b64decode(payload,validate=True));op=request.get('operation')
  if op=='cloud-status':result=runtime.public_status()
  elif op=='cloud-local-files':
   import HomeModelsBridge
   result={'ok':True,'ids':[item['id'] for item in HomeModelsBridge.execute({'operation':'list'})['items']]}
  elif op=='cloud-check-open':
   job=secrets.token_hex(16)
   with lock:
    for key in list(jobs):
     if time.monotonic()-jobs[key]['created']>180:del jobs[key]
    if len(jobs)>=32:raise ValueError('Дождитесь завершения проверки моделей')
    jobs[job]={'created':time.monotonic(),'result':None}
   def run():
    try:value=runtime.check_open(request.get('id',''))
    except Exception as exc:value={'ok':False,'error':str(exc)}
    with lock:
     if job in jobs:jobs[job]['result']=value
   threading.Thread(target=run,daemon=True).start();result={'ok':True,'job':job}
  elif op=='cloud-open-result':
   with lock:
    item=jobs.get(request.get('job'))
    if item is None:raise ValueError('Проверка истекла. Повторите открытие модели.')
    result=item['result'] or {'ok':True,'pending':True}
    if item['result'] is not None:del jobs[request['job']]
  else:raise ValueError('Неизвестная операция')
 except Exception as exc:result={'ok':False,'error':str(exc)}
 return 'LOCAL_SYNC_RESULT:'+json.dumps(result,ensure_ascii=True,separators=(',',':'))
