-- ==============================================================================
-- WorkBuddy Multi-Account Universal Workspace Rollback
-- 移除共享触发器，按备份恢复每个会话 / 自动化任务的原始账号归属。
-- 与 `workbuddy rollback [UID]` 执行的语句保持一致；推荐直接使用该命令。
-- ==============================================================================

-- 1. 移除触发器
DROP TRIGGER IF EXISTS trg_sessions_force_shared_insert;
DROP TRIGGER IF EXISTS trg_sessions_force_shared_update;

-- 2. 按备份恢复会话原始归属（需要先完成 install_triggers.sql 中的备份表）
UPDATE sessions
SET user_id = (SELECT b.user_id FROM wbtk_session_owner_backup b WHERE b.session_id = sessions.id)
WHERE (user_id IS NULL OR user_id = '')
  AND id IN (SELECT session_id FROM wbtk_session_owner_backup);
DROP TABLE IF EXISTS wbtk_session_owner_backup;

-- 3. 恢复切号时被改写的自动化任务所有者（仅当存在该备份表时执行）
-- UPDATE automations
-- SET owner_user_id = (SELECT b.owner_user_id FROM wbtk_automation_owner_backup b WHERE b.automation_id = automations.id)
-- WHERE id IN (SELECT automation_id FROM wbtk_automation_owner_backup);
-- DROP TABLE IF EXISTS wbtk_automation_owner_backup;

-- 4. 没有备份记录的会话（旧版本 init 时已被清空）如需统一归属到某个账号：
-- UPDATE sessions SET user_id = '<YOUR_UID>' WHERE user_id = '';
