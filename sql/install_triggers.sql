-- ==============================================================================
-- WorkBuddy Multi-Account Universal Workspace Patch
-- 让所有已保存或新登录的 WorkBuddy 账号共享同一套工作区、历史对话与会话上下文
-- 与 `workbuddy init` 执行的语句保持一致；推荐直接使用 `workbuddy init`。
-- ==============================================================================

-- 0. 原始账号归属备份表：置空 user_id 之前先记录，`workbuddy rollback` 据此逐个恢复
CREATE TABLE IF NOT EXISTS wbtk_session_owner_backup (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    saved_at   INTEGER NOT NULL
);

-- 1. 备份并置空现存会话的 user_id，触发官方的跨账号全局放行逻辑：
-- (WHERE user_id = :currentUserId OR user_id IS NULL OR user_id = '')
INSERT OR REPLACE INTO wbtk_session_owner_backup (session_id, user_id, saved_at)
SELECT id, user_id, CAST(strftime('%s','now') AS INTEGER) FROM sessions
WHERE user_id IS NOT NULL AND user_id != '';

UPDATE sessions SET user_id = '' WHERE user_id IS NOT NULL AND user_id != '';

-- 2. 重建触发器（旧版本触发器不会备份归属）
DROP TRIGGER IF EXISTS trg_sessions_force_shared_insert;
DROP TRIGGER IF EXISTS trg_sessions_force_shared_update;

-- 新会话插入时：先备份原始归属，再置空
CREATE TRIGGER trg_sessions_force_shared_insert
AFTER INSERT ON sessions
WHEN NEW.user_id != '' AND NEW.user_id IS NOT NULL
BEGIN
    INSERT OR REPLACE INTO wbtk_session_owner_backup (session_id, user_id, saved_at)
    VALUES (NEW.id, NEW.user_id, CAST(strftime('%s','now') AS INTEGER));
    UPDATE sessions SET user_id = '' WHERE id = NEW.id;
END;

-- 会话 user_id 被更新为特定账号时：记录最新归属，再置空
CREATE TRIGGER trg_sessions_force_shared_update
AFTER UPDATE OF user_id ON sessions
WHEN NEW.user_id != '' AND NEW.user_id IS NOT NULL
BEGIN
    INSERT OR REPLACE INTO wbtk_session_owner_backup (session_id, user_id, saved_at)
    VALUES (NEW.id, NEW.user_id, CAST(strftime('%s','now') AS INTEGER));
    UPDATE sessions SET user_id = '' WHERE id = NEW.id;
END;
