import hashlib
import json
from html.parser import HTMLParser


class Form(HTMLParser):
    """解析门户返回的真实步骤表单。

    除隐藏域（CSRF、draft_version 等）外，还收集下拉选项与附件要求；
    选项 ID 一律以表单返回的 value 为准，不能用显示文字猜测。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.action = ''
        self.step = ''
        self.options = {}
        self.attachments = []
        self._select = None
        self._attachment_block = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form':
            self.action = values.get('action', '')
            self.step = values.get('data-step', '')
        elif tag == 'input' and 'name' in values:
            self.fields[values['name']] = values.get('value', '')
        elif tag == 'select' and 'name' in values:
            self._select = values['name']
            self.options[self._select] = []
        elif tag == 'option' and self._select is not None:
            self.options[self._select].append(dict(
                value=values.get('value', ''),
                label=(values.get('data-label', '') or values.get('value', ''))))
        elif tag == 'ul' and 'data-attachments' in values:
            self._attachment_block = True
        elif tag == 'li' and self._attachment_block:
            self.attachments.append(dict(
                key=values.get('data-attachment', ''),
                required=values.get('data-required') == 'true',
                project=values.get('data-project', ''),
                label=values.get('data-label', '')))

    def handle_endtag(self, tag):
        if tag == 'select':
            self._select = None
        elif tag == 'ul' and self._attachment_block:
            self._attachment_block = False

    def select_values(self, name):
        return [option['value'] for option in self.options.get(name, [])]


def bound_fingerprint(content, bound_fields):
    """对影响审批的字段计算内容指纹；批准只对具体内容有效。"""
    material = {field: content.get(field) for field in bound_fields}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True,
                         allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]


def diff_content(local, remote, fields):
    """逐字段对照本地输入与门户草稿，外部改动绝不能被悄悄覆盖。"""
    return {
        field: dict(local=local.get(field), remote=remote.get(field))
        for field in fields
        if str(local.get(field, '')) != str(remote.get(field, ''))
    }


class Drafts:
    def __init__(self, options):
        self.options = options
        self.rows = {}

    def save(self, identifier, content, expected_version=0):
        current = self.rows.get(identifier, dict(version=0))
        if current['version'] != expected_version:
            raise ValueError('草稿版本冲突')
        institution = content['institution']
        if content['project'] not in self.options.get(institution, []):
            raise ValueError('项目不属于所选机构')
        result = dict(content=content, version=current['version'] + 1)
        self.rows[identifier] = result
        return result


def collect(payload):
    drafts = Drafts(payload['options'])
    return [dict(key=row['id'], **drafts.save(row['id'], row['content'])) for row in payload['requests']]
