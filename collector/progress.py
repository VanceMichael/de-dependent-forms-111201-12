"""录入流程的本地持久化：每条申请独立的步骤进度、暂停原因与审计轨迹。"""
import json
import sqlite3
from contextlib import contextmanager

# 行状态
ROW_PENDING = 'pending'        # 尚未开始
ROW_RUNNING = 'running'        # 有已确认步骤，仍在推进
ROW_PAUSED = 'paused'          # 等待人工处理（冲突/批准/外部取消）
ROW_SUBMITTED = 'submitted'    # 门户已出具收据
ROW_CANCELED = 'canceled'      # 外部取消，恢复时不得再创建
ROW_FAILED = 'failed'          # 本行输入校验失败，仅影响本行

# 暂停原因（复核人员可读）
PAUSE_VERSION_CONFLICT = 'version_conflict'   # 外部改动草稿，等待人工裁定
PAUSE_AWAITING_APPROVAL = 'awaiting_approval'  # 等待主管针对当前内容批准
PAUSE_APPROVAL_STALE = 'approval_stale'        # 批准后影响审批的字段变化
PAUSE_CANCELED = 'canceled_externally'         # 申请已被外部取消
PAUSE_BAD_INPUT = 'invalid_input'              # 本地输入不合法

PAUSE_EXPLANATIONS = {
    PAUSE_VERSION_CONFLICT: '门户草稿被外部修改，版本号高于本地表单；'
                            '已重新读取并逐字段对照，等待人工裁定以哪份内容为准',
    PAUSE_AWAITING_APPROVAL: '已到达主管审批步骤，批准必须由主管针对当前内容指纹做出，程序不自行批准',
    PAUSE_APPROVAL_STALE: '主管批准后，影响审批的字段发生变化，原批准绑定的指纹与当前内容不符，需重新批准',
    PAUSE_CANCELED: '该申请已被外部取消，程序停止一切写入，恢复时也不会重新创建',
    PAUSE_BAD_INPUT: '本地输入未通过门户校验，已隔离本行，其他申请继续办理',
}


class ProgressStore:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS requests(
                request_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL,
                local_content TEXT NOT NULL,
                status TEXT NOT NULL,
                pause_reason TEXT,
                pause_detail TEXT,
                last_form TEXT);
            CREATE TABLE IF NOT EXISTS steps(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                step TEXT NOT NULL,
                draft_version INTEGER NOT NULL,
                confirmed_at TEXT NOT NULL DEFAULT (datetime('now')),
                snapshot TEXT NOT NULL,
                UNIQUE(request_id, step));
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
        """)

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def register(self, request_id, revision, content):
        with self.transaction():
            row = self.connection.execute(
                'SELECT status FROM requests WHERE request_id=?',
                (request_id,)).fetchone()
            if row is None:
                self.connection.execute(
                    'INSERT INTO requests(request_id,revision,local_content,status)'
                    ' VALUES (?,?,?,?)',
                    (request_id, revision,
                     json.dumps(content, ensure_ascii=False), ROW_PENDING))
                self._audit(request_id, revision, 'row_registered',
                            dict(content=content))
            else:
                self.connection.execute(
                    'UPDATE requests SET revision=?, local_content=? WHERE request_id=?',
                    (revision, json.dumps(content, ensure_ascii=False), request_id))
                self._audit(request_id, revision, 'row_revised',
                            dict(content=content))

    def _audit(self, request_id, revision, event, detail):
        self.connection.execute(
            'INSERT INTO audit(request_id,revision,event,detail) VALUES (?,?,?,?)',
            (request_id, revision, event,
             json.dumps(detail, ensure_ascii=False, default=str)))

    def audit(self, request_id, revision, event, detail):
        with self.transaction():
            self._audit(request_id, revision, event, detail)

    def get_row(self, request_id):
        row = self.connection.execute(
            'SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['local_content'] = json.loads(data['local_content'])
        data['pause_detail'] = (json.loads(data['pause_detail'])
                                if data['pause_detail'] else None)
        data['last_form'] = (json.loads(data['last_form'])
                             if data['last_form'] else None)
        return data

    def all_rows(self):
        return [self.get_row(row[0]) for row in
                self.connection.execute('SELECT request_id FROM requests ORDER BY request_id')]

    def set_status(self, request_id, status, pause_reason=None, pause_detail=None):
        with self.transaction():
            self.connection.execute(
                'UPDATE requests SET status=?, pause_reason=?, pause_detail=? WHERE request_id=?',
                (status, pause_reason,
                 json.dumps(pause_detail, ensure_ascii=False, default=str)
                 if pause_detail is not None else None,
                 request_id))

    def save_form(self, request_id, form_data):
        with self.transaction():
            self.connection.execute(
                'UPDATE requests SET last_form=? WHERE request_id=?',
                (json.dumps(form_data, ensure_ascii=False, default=str), request_id))

    def confirm_step(self, request_id, revision, step, draft_version, snapshot, event_detail):
        with self.transaction():
            self.connection.execute(
                'INSERT INTO steps(request_id,step,draft_version,snapshot) VALUES (?,?,?,?)'
                ' ON CONFLICT(request_id,step) DO UPDATE SET'
                ' draft_version=excluded.draft_version, snapshot=excluded.snapshot,'
                ' confirmed_at=datetime(\'now\')',
                (request_id, step, draft_version,
                 json.dumps(snapshot, ensure_ascii=False, default=str)))
            self.connection.execute(
                'UPDATE requests SET status=? WHERE request_id=?',
                (ROW_RUNNING, request_id))
            self._audit(request_id, revision, f'step_confirmed:{step}', event_detail)

    def mark_canceled(self, request_id, revision, detail):
        with self.transaction():
            self.connection.execute(
                'UPDATE requests SET status=?, pause_reason=?, pause_detail=? WHERE request_id=?',
                (ROW_CANCELED, PAUSE_CANCELED,
                 json.dumps(detail, ensure_ascii=False, default=str), request_id))
            self._audit(request_id, revision, 'row_canceled', detail)

    def mark_submitted(self, request_id, revision, receipt, detail):
        with self.transaction():
            self.connection.execute(
                'UPDATE requests SET status=?, pause_reason=NULL, pause_detail=NULL WHERE request_id=?',
                (ROW_SUBMITTED, request_id))
            self._audit(request_id, revision, 'row_submitted',
                        dict(receipt=receipt, **detail))

    def mark_failed(self, request_id, revision, detail):
        with self.transaction():
            self.connection.execute(
                'UPDATE requests SET status=?, pause_reason=?, pause_detail=? WHERE request_id=?',
                (ROW_FAILED, PAUSE_BAD_INPUT,
                 json.dumps(detail, ensure_ascii=False, default=str), request_id))
            self._audit(request_id, revision, 'row_failed', detail)

    def steps(self, request_id):
        return [dict(row, snapshot=json.loads(row['snapshot'])) for row in
                self.connection.execute(
                    'SELECT * FROM steps WHERE request_id=? ORDER BY id',
                    (request_id,))]

    def audit_trail(self, request_id):
        return [dict(row, detail=json.loads(row['detail'])) for row in
                self.connection.execute(
                    'SELECT * FROM audit WHERE request_id=? ORDER BY id',
                    (request_id,))]

    def close(self):
        self.connection.close()
