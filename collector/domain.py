import hashlib
import json
from html.parser import HTMLParser


class Form(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.action = ''

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form':
            self.action = values.get('action', '')
        if tag == 'input' and 'name' in values:
            self.fields[values['name']] = values.get('value', '')


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
