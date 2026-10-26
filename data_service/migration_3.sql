-- 保留历史单时点和整日替换语义，新设置由管理员显式选择。
ALTER TABLE managed_sources ADD COLUMN schedule_times JSONB;
UPDATE managed_sources SET schedule_times=jsonb_build_array(lpad(schedule_hour::text,2,'0') || ':00');
ALTER TABLE managed_sources ALTER COLUMN schedule_times SET NOT NULL;
ALTER TABLE managed_sources ADD CONSTRAINT managed_sources_schedule_times_shape
 CHECK(jsonb_typeof(schedule_times)='array' AND jsonb_array_length(schedule_times) BETWEEN 1 AND 24);
ALTER TABLE managed_sources ADD COLUMN write_mode TEXT NOT NULL DEFAULT 'replace_day'
 CHECK(write_mode IN ('replace_day','upsert'));
-- 写入方式随任务入队冻结，配置调整不得改变进行中任务的发布含义。
ALTER TABLE batches ADD COLUMN write_mode TEXT NOT NULL DEFAULT 'replace_day'
 CHECK(write_mode IN ('replace_day','upsert'));
ALTER TABLE batches ADD COLUMN published_row_count BIGINT CHECK(published_row_count>=0);
UPDATE batches SET published_row_count=row_count WHERE status='succeeded';
COMMENT ON COLUMN batches.row_count IS '本批次从来源或调用方接收的记录数，不含合并保留的旧行';
COMMENT ON COLUMN batches.published_row_count IS '发布完成后的本业务日总记录数，包含按主键合并保留的旧行';
COMMENT ON COLUMN batches.write_mode IS '入队时确定的写入方式：replace_day整日替换，upsert按业务主键更新并保留未返回旧行';
COMMENT ON COLUMN managed_sources.schedule_times IS '每日执行时间，1至24个HH:mm，北京时间，唯一且升序';
