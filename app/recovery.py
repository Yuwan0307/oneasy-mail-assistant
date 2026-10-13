"""Local, bounded scan recovery; external error text is never stored."""
import ssl
from email.utils import parsedate_to_datetime

import httpx


def diagnose(failure, failures, current):
    action='manual'; reason='扫描响应异常，请管理员检查配置及操作记录'
    kind='unexpected'; delay=300
    cause=failure; certificate_error=False; visited=set()
    while cause is not None and id(cause) not in visited:
        visited.add(id(cause))
        certificate_error |= isinstance(cause,ssl.SSLCertVerificationError)
        cause=cause.__cause__ or cause.__context__
    if certificate_error:
        kind='certificate'; reason='安全证书校验未通过，请管理员检查'
    elif isinstance(failure,httpx.TransportError):
        kind='timeout' if isinstance(failure,httpx.TimeoutException) else 'connection'
        action='reconnect'; reason='连接超时或中断，将重建连接并重新扫描'
    elif isinstance(failure,httpx.HTTPStatusError):
        kind='http_status'; status=failure.response.status_code
        if status==401 and failure.request.url.path!='/oauth2/v2.0/token':
            action='refresh_token'; reason='访问凭据失效，将重新获取临时凭据并扫描'
        elif status==429:
            action='wait'; reason='接口访问受限，将等待后重新扫描'
        elif status>=500:
            action='wait'; reason='邮箱服务暂时异常，将等待后重新扫描'
        else:
            reason='接口拒绝访问或配置不匹配，请管理员检查授权及配置'
    if action!='manual': delay=(60,120,300)[min(failures-1,2)]
    if isinstance(failure,httpx.HTTPStatusError) and failure.response.status_code in {429,503}:
        value=failure.response.headers.get('Retry-After','')
        try: requested=int(value) if value.isdigit() else (parsedate_to_datetime(value)-current).total_seconds()
        except (ValueError,TypeError,OverflowError): requested=0
        delay=max(delay,min(max(requested,0),86400))
    result={'failure_type':kind,'reason':reason,'action':action,'delay':delay}
    if isinstance(failure,httpx.HTTPStatusError): result['http_status']=failure.response.status_code
    return result
