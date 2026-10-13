"""Internal notification mail only. No customer reply or mailbox access."""
import hashlib
import re
import smtplib
import ssl
import certifi
from email.message import EmailMessage
from email.utils import formatdate


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and bool(re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value))


def validate_config(config):
    if not valid_email(config.get('address')): raise ValueError('请填写完整的发件邮箱地址')
    host = config.get('host', '')
    if not isinstance(host, str) or len(host)>253 or not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise ValueError('SMTP服务器请填写主机名，不要填写网址或路径')
    try: port = int(config.get('port', 465))
    except (TypeError, ValueError): raise ValueError('SMTP端口无效') from None
    if not 1<=port<=65535: raise ValueError('SMTP端口无效')
    if config.get('security') not in {'ssl', 'starttls'}: raise ValueError('SMTP必须使用加密连接')
    if not isinstance(config.get('password'), str) or not config['password']:
        raise ValueError('请填写客户端授权码')
    return {key: config[key] for key in ('address', 'host', 'security', 'password')} | {'port': port}


def error_text(error):
    if isinstance(error, smtplib.SMTPAuthenticationError): return '发件认证失败，请检查授权码和SMTP权限'
    if isinstance(error, smtplib.SMTPRecipientsRefused): return '收件地址被服务器拒绝，请检查负责人邮箱'
    if isinstance(error, ssl.SSLError): return 'SMTP加密验证失败，请核对服务器地址和加密方式'
    if isinstance(error, smtplib.SMTPResponseException): return f'邮件服务器拒绝请求（{error.smtp_code}），请检查发信限制'
    return '邮件提交失败，请检查SMTP配置和网络；失败通知将重试'


class SMTPSender:
    def __init__(self, config): self.config = validate_config(config)

    def message_id(self, recipient, identity):
        digest = hashlib.sha256((self.config['address']+'\0'+recipient+'\0'+identity).encode()).hexdigest()
        return f'<oneasy-{digest}@{self.config["address"].split("@")[1]}>'

    def send_text(self, recipient, text, subject='Oneasy 邮件提醒', identity='test'):
        if not valid_email(recipient): raise ValueError('必须指定单一负责人邮箱')
        message = EmailMessage()
        message['From'] = self.config['address']
        message['To'] = recipient
        message['Subject'] = ' '.join(subject.splitlines())[:180]
        message['Date'] = formatdate(localtime=True)
        message['Message-ID'] = self.message_id(recipient, identity)
        message['Auto-Submitted'] = 'auto-generated'
        message['X-Auto-Response-Suppress'] = 'All'
        message.set_content(text)
        context = ssl.create_default_context(cafile=certifi.where())
        config = self.config
        if config['security']=='ssl':
            connection = smtplib.SMTP_SSL(config['host'], config['port'], timeout=25, context=context)
        else:
            connection = smtplib.SMTP(config['host'], config['port'], timeout=25)
        with connection as client:
            if config['security']=='starttls':
                client.ehlo(); client.starttls(context=context); client.ehlo()
            client.login(config['address'], config['password'])
            refused = client.send_message(message, from_addr=config['address'], to_addrs=[recipient])
            if refused: raise smtplib.SMTPRecipientsRefused(refused)
