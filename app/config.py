"""Private configuration: Windows DPAPI, or encrypted file with a mode-0600 key."""
import base64
import ctypes
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.fernet import Fernet

AI_DEFAULTS={'base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1','model':'qwen-plus','api_key':''}


def initialize_config(path):
    path=Path(path)
    existing=load_config(path) if path.exists() else {}
    config={**existing,'demo':False}
    config.setdefault('alimail',{'client_id':'','client_secret':''})
    config['ai']={**AI_DEFAULTS,**config.get('ai',{})}
    config.setdefault('backup_dir',str(path.parent/'backups'))
    for obsolete in ('admin_token','gateway_key','gateway_url','public_url','session_key'):
        config.pop(obsolete,None)
    if config!=existing: save_config(path,config)
    return config


def connection_config(data, config):
    if not isinstance(data,dict): raise ValueError('配置格式无效')
    allowed={'client_id','client_secret','base_url','model','api_key'}
    if any(k not in allowed or not isinstance(v,str) or len(v)>4096 for k,v in data.items()):
        raise ValueError('配置字段无效')
    old_mail=config.get('alimail',{})
    old_ai={**AI_DEFAULTS,**config.get('ai',{})}
    mail={'client_id':data.get('client_id',old_mail.get('client_id','')).strip(),'client_secret':data.get('client_secret','').strip()}
    ai={k:data.get(k,old_ai[k]).strip() for k in ('base_url','model','api_key')}
    ai['api_key']=data.get('api_key','').strip()
    ai['base_url']=ai['base_url'].rstrip('/')
    if not mail['client_secret']:
        if mail['client_id']!=old_mail.get('client_id',''):
            raise ValueError('更换阿里邮箱应用 ID 后，请填写对应的 Client Secret')
        mail['client_secret']=old_mail.get('client_secret','')
    if bool(mail['client_id'])!=bool(mail['client_secret']):
        raise ValueError('请同时配置阿里邮箱应用 ID 和 Client Secret')
    url=urlsplit(ai['base_url'])
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or any(c.isspace() for c in ai['base_url']):
        raise ValueError('AI API 地址需要是不含账号、参数的 HTTPS 地址')
    if not ai['model'] or len(ai['model'])>200: raise ValueError('请填写有效的 AI 模型名称')
    if not ai['api_key']:
        if ai['base_url']!=old_ai['base_url'].rstrip('/') and old_ai['api_key']:
            raise ValueError('更换 AI API 地址后，请填写新服务对应的 API Key')
        ai['api_key']=old_ai['api_key']
    return mail,ai


def _dpapi(data, protect):
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte))); output=Blob()
    crypt=ctypes.windll.crypt32
    if protect:
        ok=crypt.CryptProtectData(ctypes.byref(source),None,None,None,None,4,ctypes.byref(output))
    else:
        ok=crypt.CryptUnprotectData(ctypes.byref(source),None,None,None,None,0,ctypes.byref(output))
    if not ok: raise ctypes.WinError()
    try: return ctypes.string_at(output.data,output.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes=[ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(output.data)


def save_config(path, config):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(config,ensure_ascii=False).encode()
    if os.name=='nt':
        envelope={'format':'dpapi-machine','data':base64.b64encode(_dpapi(raw,True)).decode()}
    else:
        key=path.with_suffix('.key')
        if not key.exists():
            fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as file: file.write(Fernet.generate_key())
        envelope={'format':'fernet','data':Fernet(key.read_bytes()).encrypt(raw).decode()}
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(envelope),encoding='utf-8')
    os.chmod(temporary,0o600); temporary.replace(path)


def load_config(path):
    path=Path(path); envelope=json.loads(path.read_text(encoding='utf-8'))
    if envelope['format']=='dpapi-machine':
        if os.name!='nt':
            raise ValueError('Windows 本机加密配置无法在 Mac 解密，请在设置中重新配置连接信息')
        raw=_dpapi(base64.b64decode(envelope['data']),False)
    elif envelope['format']=='fernet':
        raw=Fernet(path.with_suffix('.key').read_bytes()).decrypt(envelope['data'].encode())
    else: raise ValueError('配置格式不支持')
    return json.loads(raw)
