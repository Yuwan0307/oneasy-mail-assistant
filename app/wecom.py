"""Internal WeCom group notifications with explicit member mentions only."""
import re

import httpx


_WEBHOOK = re.compile(
    r'https://qyapi\.weixin\.qq\.com(?::443)?/cgi-bin/webhook/send\?key=[A-Za-z0-9_-]{1,200}')
_USERID = re.compile(r'[A-Za-z0-9_.@-]{1,64}')
_LONG_CONTENT = '\n内容较长，请登录原邮箱或邮件助手查看完整信息。'


def validate_config(data, old=None):
    """Merge form values without clearing a previously saved webhook."""
    if not isinstance(data, dict) or (old is not None and not isinstance(old, dict)):
        raise ValueError('企业微信群配置无效')
    if any(key not in {'enabled', 'webhook'} for key in data):
        raise ValueError('企业微信群配置无效')
    old = old or {}
    enabled = data.get('enabled', old.get('enabled', False))
    webhook = data.get('webhook', '')
    if type(enabled) is not bool or not isinstance(webhook, str):
        raise ValueError('企业微信群配置无效')
    if not webhook.strip():
        webhook = old.get('webhook', '')
    if not isinstance(webhook, str) or (webhook and not _WEBHOOK.fullmatch(webhook)):
        raise ValueError('企业微信群配置无效')
    if enabled and not webhook:
        raise ValueError('企业微信群配置无效')
    return {'enabled': enabled, 'webhook': webhook}


class WeComError(RuntimeError):
    """Safe to show or log: never includes a URL, response, or raw exception."""
    _MESSAGES = {
        'rejected': '企业微信群通知被拒绝，请检查配置和成员账号',
        'transport': '企业微信群连接失败，请检查网络和配置',
        'response': '企业微信群响应异常，请检查服务状态',
    }

    def __init__(self, code):
        self.code = code
        super().__init__(self._MESSAGES[code])


class WeComSender:
    def __init__(self, config, client=None):
        try:
            self.config = validate_config(config)
        except ValueError:
            raise WeComError('rejected') from None
        self.client = client

    def send_text(self, text, userids, identity=''):
        if not self.config['enabled'] or not isinstance(text, str):
            raise WeComError('rejected')
        if not isinstance(userids, (list, tuple)):
            raise WeComError('rejected')
        if any(not isinstance(value, str) or not _USERID.fullmatch(value)
               or value.lower() in {'all', '@all'} for value in userids):
            raise WeComError('rejected')
        try:
            raw = text.encode('utf-8')
        except UnicodeEncodeError:
            raise WeComError('rejected') from None
        if len(raw) > 2048:
            available = 2048 - len(_LONG_CONTENT.encode('utf-8'))
            text = raw[:available].decode('utf-8', errors='ignore') + _LONG_CONTENT
        payload = {'msgtype': 'text', 'text': {'content': text}}
        if userids: payload['text']['mentioned_list'] = list(dict.fromkeys(userids))
        try:
            if self.client is not None:
                response = self.client.post(self.config['webhook'], json=payload,
                                            timeout=15, follow_redirects=False)
            else:
                with httpx.Client(verify=True, timeout=15, follow_redirects=False,
                                  trust_env=False) as client:
                    response = client.post(self.config['webhook'], json=payload)
        except Exception:
            raise WeComError('transport') from None
        if not response.is_success:
            raise WeComError('response')
        try:
            result = response.json()
        except (ValueError, UnicodeError):
            raise WeComError('response') from None
        if not isinstance(result, dict) or type(result.get('errcode')) is not int:
            raise WeComError('response')
        if result['errcode'] != 0:
            raise WeComError('rejected')
