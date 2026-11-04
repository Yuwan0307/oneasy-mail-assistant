"""Pure business rules. Email text is untrusted data, never instructions."""
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from html import unescape
from io import BytesIO
from pathlib import PurePath
from zipfile import ZipFile
from urllib.parse import parse_qsl,unquote,urlsplit
import xml.etree.ElementTree as ET

TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
RULE_VERSION = 'oneasy-notify-5'
RISK_NOTICE = re.compile(r'欠费|即将到期|服务中断|安全告警|异常登录|投递失败|退信|delivery failed|undeliverable',re.I)
MAILBOXES = dict(zip(
    [f'{name}@oneasy.cc' for name in ['service','partner','support','noreply','operation','legal']],
    ['客户服务','合作伙伴','技术售后','自动邮件来信与退信','运营业务','法律咨询']))


def now():
    return datetime.now(TZ)


def stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return value.astimezone(TZ)


@dataclass
class Mail:
    account: str
    provider_id: str
    received: datetime
    subject: str
    sender: str
    body: str
    attachments: list = field(default_factory=list)
    folder: str = '2'
    stable_id: str = ''


@dataclass
class Decision:
    category: str  # action, info, digest, human, marketing (ignored)
    summary: str
    reason: str
    deadline: datetime | None = None
    evidence: str = ''
    confidence: float = 0
    model: str = 'local'
    sensitive: bool = False
    hints: str = ''
    is_test: bool = False
    deadline_source: str = ''
    internal_followup: str = ''


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def plain(text):
    parser = PlainHTML()
    parser.feed(text)
    return '\n'.join(parser.parts)


def sensitive(text):
    text = unicodedata.normalize('NFKC', unescape(text))
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    found = []
    candidates = re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|(?<![\w:])[a-fA-F0-9:]*:[a-fA-F0-9:]+(?:%\w+)?', text)
    for candidate in candidates:
        try:
            ipaddress.ip_address(candidate)
            found.append('IP 地址')
            break
        except ValueError:
            pass
    if re.search(r'子网|掩码|网关|网络配置|路由配置|内网|交换机配置|防火墙规则|\b(?:subnet|netmask|gateway|vlan|dns\s*[:=]|route\s+add)\b', text, re.I):
        found.append('网络配置')
    if re.search(r'密码|口令|凭证|私钥|密钥|令牌|连接串|\b(?:password|passwd|pwd|token|secret|api[_ -]?key|access[_ -]?key|authorization|bearer)\b|BEGIN [A-Z ]*PRIVATE KEY|://[^\s/:]+:[^\s/@]+@', text, re.I):
        found.append('访问凭证')
    return found


def analysis_text(text):
    # Mailing-list tokens are private too: remove the entire footer URL before AI.
    text=unicodedata.normalize('NFKC',unescape(text))
    text=''.join(char for char in text if unicodedata.category(char)!='Cf')
    def clean(match):
        original=match[0]
        try:
            decoded=unquote(original); url=urlsplit(decoded)
            if not re.search(r'/(?:unsubscribe|unsubscribe\.php|optout|opt-out|email-preferences)/?$',url.path,re.I):
                return original
            keys=[key.lower().replace('-','_') for key,_ in parse_qsl(url.query)]
            if url.username is not None or url.password is not None:
                return decoded
            if any(flag in sensitive(decoded) for flag in ('IP 地址','网络配置')):
                return decoded
            if any(key!='token' and sensitive(key) for key in keys):
                return decoded
            return '[退订链接已隐藏]'
        except (ValueError,UnicodeError):
            return original
    return re.sub(r'https?://[^\s<>"\x27]+',clean,text,flags=re.I)


def periodic_password_notice(mail,text):
    # Match only the complete known reminder, never all mail from a no-reply sender.
    if mail.attachments or RISK_NOTICE.search(text) or mail.sender.strip().lower()!='no-reply@mailsupport.aliyun.com': return False
    compact=lambda value: re.sub(r'[\s"\x27“”‘’]+','',unicodedata.normalize('NFKC',unescape(value)))
    if compact(mail.subject)!='【系统通知】定期修改密码提醒': return False
    body=re.sub(r'<(style|script)\b[^>]*>.*?</\1\s*>','',mail.body,flags=re.I|re.S)
    visible=compact(plain(body))
    expected=('尊敬的用户:管理员已启用"定期修改密码提醒"功能。请及时登录网页版邮箱,'
              '进入设置-查看更多设置-账户与安全-账户安全修改账号'+mail.account+'的密码。阿里邮箱团队')
    expected=compact(expected)
    if expected not in visible: return False
    remainder=visible.replace(expected,'').replace('定期修改密码提醒','').replace('【系统通知】','').replace('阿里邮箱系统信模板','')
    if any(char.isalnum() for char in remainder): return False
    return not sensitive(text.replace('定期修改密码提醒','').replace('的密码',''))


def attachment_text(name, data):
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('附件过大')
    suffix = PurePath(name).suffix.lower()
    if suffix in {'.txt','.csv','.md','.json','.xml','.log'}:
        for codec in ['utf-8-sig','gb18030']:
            try:
                text = data.decode(codec)
                if '\x00' in text:
                    raise ValueError('二进制文件')
                return text
            except UnicodeError:
                continue
    if suffix == '.docx':
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if sum(i.file_size for i in infos) > 10 * 1024 * 1024 or any(i.flag_bits & 1 for i in infos):
                raise ValueError('附件无法安全解析')
            if any(i.filename.startswith(('word/media/','word/embeddings/')) for i in infos):
                raise ValueError('存在图片或嵌入文件')
            return '\n'.join(' '.join(ET.fromstring(archive.read(i)).itertext()) for i in infos if i.filename.startswith('word/') and i.filename.endswith('.xml'))
    if suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted or reader.attachments or len(reader.pages) > 100:
            raise ValueError('PDF 需要人工检查')
        parts = []
        for page in reader.pages:
            text = page.extract_text() or ''
            if page.images or not text.strip():
                raise ValueError('PDF 包含图片或扫描页')
            parts.append(text)
        return '\n'.join(parts)
    raise ValueError('附件类型需人工检查')


def website_promotion(mail):
    # Explicit user-approved ad exclusion; promotional images never go to AI.
    envelope='\n'.join([mail.subject,mail.sender,analysis_text(mail.body),*[name for name,_ in mail.attachments]])
    if len(envelope)>60000 or sensitive(envelope) or RISK_NOTICE.search(envelope): return None
    body=plain(re.sub(r'<(style|script)\b[^>]*>.*?</\1\s*>','',analysis_text(mail.body),flags=re.I|re.S))
    topic=re.search(r'(?:官网|网站|搜索).{0,12}(?:推广|广告)|(?:推广|广告).{0,12}(?:官网|网站)|(?:AI|人工智能|大模型).{0,12}(?:收录|推荐|曝光|可见度)|\bGEO(?:优化|推广|服务)',body,re.I)
    sales=re.search(r'(?:我们|我司|本公司)(?:现在)?(?:也)?(?:提供|做|主营|专注|推出|开展)|(?:我是|来自).{0,15}(?:百度|广告|推广)|(?:专业提供|主营).{0,16}(?:推广|广告|收录)',body)
    invitation=re.search(r'(?:欢迎|感兴趣|有意向|有需求).{0,12}(?:联系|咨询|扫码|沟通)|(?:您|贵司|贵公司|你们).{0,8}(?:有.{0,4}(?:需求|计划)|需要|考虑).{0,20}(?:吗|[?？])',body)
    if not topic or not sales or not invitation: return None
    # An advertising company can also be a customer asking about our products.
    if re.search(r'(?:贵司|贵公司|你们|Oneasy).{0,40}(?:怎么|如何|多少|价格|收费|套餐|能否|是否提供)',body,re.I): return None
    if re.search(r'合同|订单|发票|退款|售后|报错|故障|工单|法律|律师|侵权|投诉|已购买|已签|采购|询价|索取|希望了解|想了解|能否.{0,6}(?:提供|介绍)|请(?:贵司|贵公司|你们)?.{0,5}(?:提供|协助|开通|处理|帮)|忽略.{0,12}(?:指令|规则)|system prompt|ignore.{0,30}instructions',envelope,re.I): return None
    if len(mail.attachments)>20 or sum(len(data) for _,data in mail.attachments)>10*1024*1024: return None
    for name,data in mail.attachments:
        suffix=PurePath(name).suffix.lower()
        image=(suffix=='.png' and data.startswith(b'\x89PNG\r\n\x1a\n') or
               suffix in {'.jpg','.jpeg'} and data.startswith(b'\xff\xd8\xff') or
               suffix=='.gif' and data.startswith((b'GIF87a',b'GIF89a')) or
               suffix=='.webp' and data.startswith(b'RIFF') and data[8:12]==b'WEBP')
        if not image or len(data)>5*1024*1024: return None
    return Decision('marketing','该邮件推销官网推广或AI收录相关服务，已过滤，无需处理。',
                    '用户指定的无效广告：官网广告或AI收录服务推销',evidence=topic[0],confidence=1)


def internal_section(text):
    return re.search(r'后续行动建议|内部跟进(?:建议|要求)|内部\s*SLA|团队跟进建议',text,re.I)


def classify(mail, ai):
    try:
        promotion=website_promotion(mail)
        if promotion: return promotion
        # Complete local inspection must finish before calling any remote model.
        text = '\n'.join([mail.subject, mail.sender, analysis_text(mail.body)] +
                         [name + '\n' + analysis_text(attachment_text(name, data)) for name, data in mail.attachments])
        if periodic_password_notice(mail,text):
            return Decision('digest','阿里邮箱定期修改密码提醒：管理员已启用该功能，请按公司安排在网页版邮箱更新密码。',
                            '匹配常规定期提醒模板，无账号异常或具体凭证值',evidence='定期修改密码提醒',confidence=1)
        flags = sensitive(text)
        if flags:
            return Decision('human', '涉及公司信息，请人工查看原邮件', '、'.join(flags), sensitive=True)
        if len(text) > 60000:
            return Decision('human','邮件内容较长，请人工查看','超出完整分析上限', sensitive=True)
    except Exception:
        return Decision('human','附件需人工查看','附件无法完整检查', sensitive=True)
    # Billing/security notices have priority even if their sender is a vendor.
    if RISK_NOTICE.search(text):
        return Decision('action', '服务或投递异常，请查看原邮件', '命中业务风险通知规则')
    if any(re.match(r'^(?:本邮件(?:为|是)(?:一封)?(?:测试模拟|模拟测试|测试邮件)|仅用于验证邮件助手)',line.strip(' —–-\t\r'))
           for line in plain(mail.body).splitlines()):
        visible=plain(text)
        section=internal_section(visible)
        followup=re.search(r'请于[^\n。；]{1,100}',visible[section.end():]) if section else None
        return Decision('info','【测试邮件】模拟客户咨询，仅用于验证检测与提醒，无需实际开通或回复。',
                        '原文明确标注测试模拟；内部模板建议不作为客户截止时间或实际办理要求',
                        is_test=True,deadline_source='none',internal_followup=followup[0] if followup else '')
    if ai is None:
        return Decision('human','请人工确认邮件事项','AI 尚未配置')
    try:
        safe_mail = Mail(mail.account, mail.provider_id, mail.received, mail.subject, mail.sender, text, [], mail.folder, mail.stable_id)
        result = ai(safe_mail)
        if result.category not in {'action','info','digest','human','marketing'}:
            raise ValueError('分类无效')
        if sensitive('\n'.join([result.summary,result.reason,result.hints,result.evidence,result.internal_followup])):
            return Decision('human','分析结果需要人工查看','结果包含敏感信息', sensitive=True)
        if result.category in {'digest','marketing'} and result.evidence and result.evidence not in text:
            # Models sometimes join several quotes; retain one verifiable original span.
            quotes=[line.strip() for line in result.evidence.splitlines() if len(line.strip())>=20 and line.strip() in text]
            if quotes: result.evidence=max(quotes,key=len)
        if result.category in {'digest','marketing'} and (result.confidence < .95 or not result.evidence or result.evidence not in text):
            result.category = 'human'
            result.reason = '无法可靠确认与业务无关，请人工复核'
        if result.category=='marketing':
            result.deadline=None
            result.hints=''
        if result.deadline and (not result.evidence or result.evidence not in text):
            result.deadline = None
            result.reason += '；截止时间依据不足，需人工确认'
        return result
    except Exception:
        return Decision('human','请人工确认邮件事项','AI 分析失败，已转人工')


def first_review(received):
    received = stamp(received)
    cutoff = received.replace(hour=18, minute=0, second=0, microsecond=0)
    return cutoff if received < cutoff else cutoff + timedelta(days=1)


