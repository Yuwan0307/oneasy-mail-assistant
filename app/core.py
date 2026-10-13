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
import xml.etree.ElementTree as ET

TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
RULE_VERSION = 'oneasy-notify-1'
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
    category: str  # action, info, digest, human
    summary: str
    reason: str
    deadline: datetime | None = None
    evidence: str = ''
    confidence: float = 0
    model: str = 'local'
    sensitive: bool = False
    hints: str = ''


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


def classify(mail, ai):
    try:
        # Complete local inspection must finish before calling any remote model.
        text = '\n'.join([mail.subject, mail.sender, mail.body] + [name + '\n' + attachment_text(name, data) for name, data in mail.attachments])
        flags = sensitive(text)
        if flags:
            return Decision('human', '涉及公司信息，请人工查看原邮件', '、'.join(flags), sensitive=True)
        if len(text) > 60000:
            return Decision('human','邮件内容较长，请人工查看','超出完整分析上限', sensitive=True)
    except Exception:
        return Decision('human','附件需人工查看','附件无法完整检查', sensitive=True)
    # Billing/security notices have priority even if their sender is a vendor.
    if re.search(r'欠费|即将到期|服务中断|安全告警|异常登录|投递失败|退信|delivery failed|undeliverable', text, re.I):
        return Decision('action', '服务或投递异常，请查看原邮件', '命中业务风险通知规则')
    if ai is None:
        return Decision('human','请人工确认邮件事项','AI 尚未配置')
    try:
        safe_mail = Mail(mail.account, mail.provider_id, mail.received, mail.subject, mail.sender, text, [], mail.folder, mail.stable_id)
        result = ai(safe_mail)
        if result.category not in {'action','info','digest','human'}:
            raise ValueError('分类无效')
        if sensitive('\n'.join([result.summary,result.reason,result.hints,result.evidence])):
            return Decision('human','分析结果需要人工查看','结果包含敏感信息', sensitive=True)
        if result.category == 'digest' and (result.confidence < .95 or not result.evidence or result.evidence not in text):
            result.category = 'human'
            result.reason = '无法可靠确认与业务无关，请人工复核'
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


